"""Learning Sidecar — latched controller that owns the closed loop.

Planner (VisionLearningAgent.propose_actions) proposes actions.
Sidecar executes, measures, detects codes, decides next round.
Sidecar NEVER submits codes — returns them to orchestrator.
Promotion is two-phase: build candidate during run → finalize after confirmed advance.
"""

import hashlib
import re
import time

from code_scorer import harvest_and_score, is_valid_code
from log import log_stage
from primitives import (
    extract_code_js, read_progress, get_locator_cascade,
    enumerate_code_candidates,
    annotate_elements, render_bid_overlay, remove_bid_overlay,
    find_challenge_region, enumerate_frames, enumerate_all_frames,
    enumerate_shadow_roots,
    is_decoy_element,
    drain_state_changes, peek_state_changes, classify_state_changes,
    match_state_changes_to_catalog,
    discover_interactive_containers,
    classify_challenge_dom,
    PROGRESS_CHANGE_TYPES,
)

CHARSET_RE = re.compile(r'^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}$')


def _xy_bucket(action: dict) -> tuple[int, int]:
    """Round coordinates to nearest 50px grid for near-duplicate detection."""
    x = int(round(action.get('x', action.get('x1', 0)) / 50) * 50)
    y = int(round(action.get('y', action.get('y1', 0)) / 50) * 50)
    return (x, y)


def _action_sig(action: dict) -> tuple:
    """Compute a signature for an action (type + element_id or xy_bucket).

    Scroll actions include direction so up/down on the same element are not
    collapsed. Drag actions with source_id/target_id get a unique pair
    signature instead of falling through to _xy_bucket (which defaults to 0,0).
    """
    atype = action.get('type', '').lower()
    eid = action.get('element_id')
    # Scroll: include direction so up/down on same element aren't collapsed
    if atype == 'scroll':
        d = action.get('direction', 'down')
        if eid is not None:
            return (atype, f'{d}:bid:{eid}')
        return (atype, f'{d}:page')
    if eid is not None:
        return (atype, f'bid:{eid}')
    # Drag actions with BID-based source/target
    src = action.get('source_id')
    tgt = action.get('target_id')
    if src is not None or tgt is not None:
        return (atype, f'bid:{src}→{tgt}')
    return (atype, _xy_bucket(action))


def _element_sig(el: dict) -> str:
    """Stable signature for matching elements across re-annotations.

    BIDs change every round (re-assigned by annotate_elements), so we match
    by intrinsic properties. Includes size buckets + aria to prevent collisions
    between multiple "Submit" buttons or empty elements at similar positions.
    """
    tag = el.get('tag', '')
    text = (el.get('text', '') or '')[:20].strip()
    role = el.get('role', '')
    aria = (el.get('ariaLabel', '') or '')[:20].strip()
    x = int(round(el.get('x', 0) / 50) * 50)
    y = int(round(el.get('y', 0) / 50) * 50)
    w = int(round(el.get('w', 0) / 50) * 50)
    h = int(round(el.get('h', 0) / 50) * 50)
    return f"{tag}:{text}:{role}:{aria}:{x},{y},{w},{h}"


def _nearest_element_sig(action: dict, element_catalog: list) -> str | None:
    """Find the catalog element closest to an action's x,y coordinates.

    Returns element signature if within 30px, else None.
    Used to mark coord-based actions as spent (not just element_id ones).
    """
    ax = action.get('x', action.get('x1'))
    ay = action.get('y', action.get('y1'))
    if ax is None or ay is None:
        return None
    ax, ay = float(ax), float(ay)
    best_sig, best_dist = None, 30.0  # 30px threshold
    for el in element_catalog:
        dx = el.get('x', 0) - ax
        dy = el.get('y', 0) - ay
        dist = (dx * dx + dy * dy) ** 0.5
        if dist < best_dist:
            best_dist = dist
            best_sig = _element_sig(el)
    return best_sig


STRATEGIES = {
    'click_buttons': "click non-gradient buttons in the challenge region",
    'hover_reveal': "hover interactive elements for 2+ seconds to trigger reveals",
    'drag_drop': "find draggable elements and drop them on targets",
    'iframe_switch': "enumerate iframes, switch into them, interact inside",
    'keyboard': "try Tab, Enter, Space, arrow keys on focused elements",
    'scroll_explore': "scroll 500px to find below-fold elements, then re-annotate",
    'form_fill': "type into visible input fields, toggle checkboxes/radios",
    'canvas_draw': "click/draw on canvas elements",
    'container_scroll': "scroll INSIDE nested containers/panels to reveal hidden buttons, then click them",
}


class StrategyBandit:
    """Priority-ordered strategy selector for exploration when stuck."""

    def __init__(self):
        self.scores = {k: 0.0 for k in STRATEGIES}
        self.visits = {k: 0 for k in STRATEGIES}

    def select(self, tried_this_step: set) -> str:
        """Untried strategies first, then highest reward per visit."""
        untried = [k for k in STRATEGIES if k not in tried_this_step]
        if untried:
            return untried[0]
        scored = [(self.scores[k] / max(self.visits[k], 1), k) for k in STRATEGIES]
        scored.sort(reverse=True)
        return scored[0][1]

    def update(self, strategy: str, reward: float):
        """Update reward. reward = progress_delta*2 + dom_change + new_elements*0.5"""
        self.scores[strategy] += reward
        self.visits[strategy] += 1


class LearningSidecar:
    MAX_ROUNDS = 3
    MAX_ROUNDS_EXTENDED = 10  # when making continuous progress
    MAX_ACTIONS_PER_ROUND = 6
    MAX_CONSECUTIVE_STALLS = 2

    def __init__(self, vision_learner, recipe_executor, dna_reasoner, knowledge_reader):
        self.vision_learner = vision_learner
        self.recipe_executor = recipe_executor
        self.dna_reasoner = dna_reasoner
        self.knowledge_reader = knowledge_reader
        self.strategy_bandit = StrategyBandit()

        # Per-step rejection state (reset on step change)
        self._rejected_codes = set()       # exact code strings
        self._rejected_signatures = set()  # (source, evidence_kind) tuples
        self._current_step = None
        self._stale_instr_candidates = set()  # instruction-zone codes from prev harvest

        # Frame-aware BID state (rebuilt each round)
        self._frame_elements = {}  # bid -> {frame_id, x, y, ...}
        self._frame_refs = {}      # frame_id -> Playwright Frame object

        # Per-run promotion tracking: prevent worse overwrites of same-run recipes
        self._promoted_this_run = {}  # {ctype: recipe_len}

    # ── Main entry point ──────────────────────────────────────────────────

    def run(self, page, step: int, version: int, context: dict = None) -> dict:
        """Closed-loop controller: propose → execute → observe → iterate.

        Returns dict with: success, code, rounds, actions_executed,
        termination_reason, action_log, promotion_candidate, iframe_count.
        """
        context = context or {}

        # Step-boundary: reset rejection state when step changes
        if step != self._current_step:
            self._current_step = step
            self._rejected_codes = set()
            self._rejected_signatures = set()
            self._stale_instr_candidates = set()

        # Absorb codes already rejected by prior phases (recipe, passive)
        for rc in context.get('prior_rejected_codes', []):
            self._rejected_codes.add(rc.upper())

        # Pass rejection info to planner via context (ephemeral, not persisted)
        if self._rejected_codes:
            context = dict(context)  # don't mutate caller's dict
            context['rejected_codes'] = list(self._rejected_codes)[:5]
            context['last_rejection_reason'] = (
                f"{len(self._rejected_codes)} codes rejected this step"
            )

        # 1. Reset + baseline
        self.vision_learner.reset_conversation()
        self._progress_binding = None
        self._active_frame = None
        self._active_frame_index = None
        self._code_source = None
        self._frame_elements = {}
        self._frame_refs = {}
        self._round_observations = []   # Layer A: reset per step
        self._round_reasonings = []     # Layer A: reset per step

        # Clear popups before starting (same burst VL did)
        self._dismiss_popups(page)

        baseline_progress = self._read_bound_progress(page)
        baseline_dom_sig = self._get_dom_signature(page)
        baseline_codes = self._harvest_codes_set(page)
        try:
            iframe_count = page.evaluate("() => document.querySelectorAll('iframe').length")
        except Exception:
            iframe_count = 0
        clickable_hash = self._get_clickable_hash(page)
        body_text_fingerprint = self._get_body_text_fingerprint(page)

        # State watcher reset is handled by orchestrator._step_setup() — do NOT reset
        # here or we wipe state changes accumulated during Phase 1/2.

        already_clicked_sweep = set()
        action_log = []
        spent_elements = set()  # Catalog filter: element sigs that were tried and failed
        bid_to_elsig = {}  # BID → element signature mapping (rebuilt each round)
        tried_strategies = set()  # Strategy bandit: track tried strategies this step
        current_round_strategy = None  # Most recently selected strategy for reward attribution

        history = []
        known_codes = set(baseline_codes)
        consecutive_stalls = 0
        total_actions = 0
        last_progress_time = time.time()
        total_actions_at_code_time = 0
        effective_max_rounds = self.MAX_ROUNDS  # dynamically extended on progress

        # 2. Round loop
        for round_num in range(self.MAX_ROUNDS_EXTENDED):
            round_start_sig = self._get_dom_signature(page)
            round_start_body_fp = body_text_fingerprint  # snapshot for round-level progress
            round_entries = []

            # a. Determine timeout
            time_since_progress = time.time() - last_progress_time
            if consecutive_stalls >= 1 or time_since_progress > 20:
                timeout_s = 30.0
            else:
                timeout_s = None

            # b. Annotate elements for BID grounding
            element_catalog = annotate_elements(page)
            challenge_region = find_challenge_region(page)

            # Mark decoys and challenge-region membership in catalog
            for el in element_catalog:
                el['decoy'] = is_decoy_element(el, challenge_region)
                if challenge_region:
                    rx, ry = challenge_region['x'], challenge_region['y']
                    rw, rh = challenge_region['w'], challenge_region['h']
                    margin = 50
                    el['in_challenge_region'] = (
                        el['x'] >= rx - margin and
                        el['x'] <= rx + rw + margin and
                        el['y'] >= ry - margin and
                        el['y'] <= ry + rh + margin
                    )
                else:
                    el['in_challenge_region'] = el.get('in_viewport', True)

            # Build BID → element-sig mapping for this round
            # (BIDs are ephemeral per-round; element sigs are stable across rounds)
            bid_to_elsig = {}
            for el in element_catalog:
                if 'bid' in el:
                    bid_to_elsig[el['bid']] = _element_sig(el)

            # Surface shadow DOM on first round (static); frames on EVERY round
            # (iframes may appear/change as user navigates deeper levels)
            if round_num == 0:
                frames_info = enumerate_frames(page)
                shadow_info = enumerate_shadow_roots(page)
            else:
                frames_info = enumerate_frames(page) if len(page.frames) > 1 else None
                shadow_info = None

            # Annotate elements inside ALL iframes (including nested) — every round
            all_frames = enumerate_all_frames(page)
            self._frame_elements = {}
            self._frame_refs = {}
            if all_frames:
                next_bid = max((el.get('bid', -1) for el in element_catalog), default=-1) + 1
                for fi in all_frames:
                    if not fi.get('visible'):
                        continue
                    fid = fi['frame_id']
                    frame = fi['pw_frame']
                    box = fi['bounding_box']
                    self._frame_refs[fid] = frame
                    try:
                        # Set data-bid attributes inside the frame and collect elements
                        frame_els = frame.evaluate(r'''() => {
                            // Clear stale bids in this frame
                            document.querySelectorAll('[data-bid]').forEach(
                                el => el.removeAttribute('data-bid'));
                            const sels = 'button, input, select, textarea, canvas, ' +
                                '[role="button"], [role="checkbox"], [role="slider"], ' +
                                '[tabindex], [draggable="true"], [onclick]';
                            const els = [...document.querySelectorAll(sels)];
                            // Also cursor:pointer elements
                            for (const el of document.querySelectorAll('div, span, p, li, label')) {
                                if (els.includes(el)) continue;
                                const r = el.getBoundingClientRect();
                                if (r.width <= 10 || r.height <= 10) continue;
                                const cs = getComputedStyle(el);
                                if (cs.cursor === 'pointer' && cs.display !== 'none')
                                    els.push(el);
                            }
                            const vh = window.innerHeight;
                            return els.map((el, i) => {
                                const r = el.getBoundingClientRect();
                                if (r.width === 0 && r.height === 0) return null;
                                return {
                                    tag: el.tagName.toLowerCase(),
                                    type: el.type || el.getAttribute('role') || '',
                                    text: (el.textContent || '').trim().substring(0, 40),
                                    role: el.getAttribute('role') || '',
                                    ariaLabel: el.getAttribute('aria-label') || '',
                                    // frame-local coords for hit-test
                                    local_x: Math.round(r.x + r.width/2),
                                    local_y: Math.round(r.y + r.height/2),
                                    w: Math.round(r.width), h: Math.round(r.height),
                                    draggable: el.draggable || false,
                                    in_viewport: r.bottom > 0 && r.top < vh,
                                };
                            }).filter(Boolean);
                        }''') or []
                        for fel in frame_els:
                            bid = next_bid
                            next_bid += 1
                            # Set data-bid inside the frame DOM
                            try:
                                frame.evaluate(f'''(coords) => {{
                                    const el = document.elementFromPoint(
                                        coords.x, coords.y);
                                    if (el) el.setAttribute('data-bid', '{bid}');
                                }}''', {'x': fel['local_x'], 'y': fel['local_y']})
                            except Exception:
                                pass
                            # Page-space coords from Playwright bounding box
                            page_x = box['x'] + fel['local_x']
                            page_y = box['y'] + fel['local_y']
                            fel['bid'] = bid
                            fel['x'] = page_x
                            fel['y'] = page_y
                            fel['frame'] = fid
                            fel['frame_depth'] = fi['depth']
                            fel['in_viewport'] = True
                            fel['interactable'] = True
                            fel['interactable_type'] = fel.get('type', '') or 'button'
                            element_catalog.append(fel)
                            # Track for frame-aware BID resolution
                            self._frame_elements[bid] = {
                                'frame_id': fid,
                                'x': page_x,
                                'y': page_y,
                                'local_x': fel['local_x'],
                                'local_y': fel['local_y'],
                            }
                    except Exception:
                        pass
                if self._frame_elements:
                    log_stage("sidecar",
                              f"frame scan: {len(self._frame_elements)} elements "
                              f"across {len(self._frame_refs)} frames "
                              f"(max depth {max(f['depth'] for f in all_frames)})")

            # Trim catalog for planner (AFTER iframe elements are merged):
            # 1. Exclude decoys (planner is told not to click them)
            # 2. Exclude spent elements (tried in prior rounds, no effect)
            # 3. Prioritize: in_viewport > in_challenge_region
            # 4. Cap at 50 elements
            non_decoy = [e for e in element_catalog
                         if not e.get('decoy')
                         and _element_sig(e) not in spent_elements]
            def _catalog_sort_key(el):
                score = 0
                if el.get('in_viewport'):
                    score += 2
                if el.get('in_challenge_region'):
                    score += 1
                return -score
            non_decoy.sort(key=_catalog_sort_key)
            element_catalog_for_planner = non_decoy[:50]

            # Build set of interactable BIDs for enforcement gate
            interactable_bids = {e['bid'] for e in element_catalog_for_planner
                                 if e.get('interactable', True) and 'bid' in e}

            # Discover interactive containers (fake iframes, panels, scrollable divs)
            containers_info = discover_interactive_containers(page)
            if containers_info:
                log_stage("sidecar",
                          f"containers: found {len(containers_info)} interactive containers "
                          f"({sum(c['interactive_count'] for c in containers_info)} children total)")

            # Classify challenge archetype (soft hint, ~5ms)
            challenge_archetype = None
            if round_num == 0:
                challenge_archetype = classify_challenge_dom(page)
                if challenge_archetype and challenge_archetype.get('confidence', 0) > 0.5:
                    log_stage("sidecar",
                              f"archetype: {challenge_archetype['archetype']} "
                              f"(conf={challenge_archetype['confidence']:.2f})")

            # Inject into context for planner (trimmed catalog for prompt efficiency)
            context = dict(context)  # don't mutate caller

            # Add aria_snapshot for semantic page representation (~93% smaller than DOM)
            aria_yaml = ''
            try:
                from agents.v4.helpers import get_aria_snapshot
                aria_yaml = get_aria_snapshot(page)
            except Exception:
                pass

            context['_page_info_extra'] = {
                'element_catalog': element_catalog_for_planner,
                'challenge_region': challenge_region,
                'frames': frames_info,
                'shadow_roots': shadow_info,
                'containers': containers_info if containers_info else None,
                'aria_snapshot': aria_yaml[:2000] if aria_yaml else None,
            }
            if challenge_archetype and challenge_archetype.get('confidence', 0) > 0.5:
                context['challenge_archetype'] = challenge_archetype

            # Inject strategy hint directly into context for planner visibility
            if consecutive_stalls >= 1 and tried_strategies:
                selected = self.strategy_bandit.select(tried_strategies)
                context['suggested_strategy'] = {
                    'name': selected,
                    'description': STRATEGIES[selected],
                }

            # Log catalog stats
            n_viewport = sum(1 for e in element_catalog if e.get('in_viewport'))
            n_region = sum(1 for e in element_catalog if e.get('in_challenge_region'))
            n_decoy = sum(1 for e in element_catalog if e.get('decoy'))
            n_spent = len(spent_elements)
            n_interact = sum(1 for e in element_catalog if e.get('interactable', True))
            n_context = sum(1 for e in element_catalog if not e.get('interactable', True))
            n_drop = sum(1 for e in element_catalog
                         if e.get('interactable_type') == 'drop_target')
            log_stage("sidecar",
                      f"BID catalog: {len(element_catalog)} total, "
                      f"{n_interact} interactable ({n_drop} drop_target), "
                      f"{n_context} context, "
                      f"{n_viewport} viewport, {n_region} region, "
                      f"{n_decoy} decoy, {n_spent} spent, "
                      f"{len(element_catalog_for_planner)} sent to planner"
                      + (f", region={challenge_region}" if challenge_region else ", no region"))

            # Drain accumulated state changes (clears JS buffer).
            # This gets changes from: previous round's actions + inter-round period.
            raw_state_changes = drain_state_changes(page)
            if raw_state_changes:
                state_changes = classify_state_changes(raw_state_changes)
                match_state_changes_to_catalog(state_changes, element_catalog)
                # Only send POSITIVE (actionable) state changes to planner
                ACTIONABLE_CHANGES = {'enabled', 'appeared', 'new_element',
                    'became_clickable', 'activated', 'revealed',
                    'became_interactive', 'became_vibrant',
                    'turned_green', 'turned_red', 'turned_grey',
                    'text_changed'}
                planner_changes = [
                    sc for sc in state_changes
                    if sc.get('matched_bid') is not None
                    and sc.get('matched_type') not in (None, 'context')
                    and any(ch in ACTIONABLE_CHANGES for ch in sc.get('changes', []))
                ][:10]
                if planner_changes:
                    context['state_changes'] = planner_changes
                    top_str = ', '.join(
                        f"{','.join(c['changes'])}:{c.get('text','')[:15]}"
                        for c in planner_changes[:3]
                    )
                    log_stage("sidecar",
                              f"state changes: {len(state_changes)} raw, "
                              f"{len(planner_changes)} matched, top: [{top_str}]")
                else:
                    context.pop('state_changes', None)
            else:
                state_changes = []
                context.pop('state_changes', None)

            # Render BID overlay ONLY for planner-eligible elements
            # (not decoys/spent — keeps screenshot clean and matches what planner sees)
            render_bid_overlay(page, element_catalog_for_planner)

            # c. Call planner (takes screenshot internally)
            proposal = self.vision_learner.propose_actions(
                page, step, version, context, history, timeout_s
            )

            # Remove overlay immediately after planner captured screenshot
            remove_bid_overlay(page)

            # Strip ephemeral page info from context (don't leak into recipes/logs)
            context.pop('_page_info_extra', None)
            context.pop('suggested_strategy', None)

            # c. Check stop signal
            if proposal.get('stop'):
                return self._make_result(
                    False, None, round_num + 1, total_actions,
                    'planner_stop', action_log, None, iframe_count
                )

            # d. Check planner-extracted codes (skip rejected)
            for code in proposal.get('extracted_codes', []):
                c = code.upper() if isinstance(code, str) else str(code).upper()
                if c not in self._rejected_codes and self._validate_code(page, c):
                    return self._finalize_code_return(
                        c, 'planner', page, baseline_codes, action_log,
                        round_num, total_actions, step, version, context,
                        iframe_count,
                    )

            # e. Layer A: accumulate planner observations per round (for recipe review)
            round_observation = proposal.get('observation', '')
            round_reasoning = proposal.get('reasoning', '')
            if round_observation:
                if not hasattr(self, '_round_observations'):
                    self._round_observations = []
                self._round_observations.append(round_observation)
            if round_reasoning:
                if not hasattr(self, '_round_reasonings'):
                    self._round_reasonings = []
                self._round_reasonings.append(round_reasoning)

            # e. Execute actions
            actions = proposal.get('actions', [])[:self.MAX_ACTIONS_PER_ROUND]
            viewport_state = self._capture_viewport_state(page)

            for action in actions:
                action_type = action.get('type', '').lower()

                # Handle frame switching
                if action_type == 'switch_frame':
                    self._handle_switch_frame(page, action)
                    continue

                sig = _action_sig(action)

                # Interactability gate: reject actions targeting context-only elements
                rejected_reason = None
                if action_type in ('click', 'hover'):
                    _eid = action.get('element_id')
                    if _eid is not None and _eid not in interactable_bids:
                        rejected_reason = f"BID {_eid} is context-only (not interactable)"
                    elif _eid is None and 'x' in action and 'y' in action:
                        log_stage("sidecar", f"warning: raw coord {action_type} "
                                  f"({action.get('x')},{action.get('y')})")
                elif action_type == 'drag':
                    src_id = action.get('source_id')
                    tgt_id = action.get('target_id')
                    if src_id is not None and src_id not in interactable_bids:
                        rejected_reason = f"drag source BID {src_id} is not interactable"
                    elif tgt_id is not None and tgt_id not in interactable_bids:
                        rejected_reason = f"drag target BID {tgt_id} is not interactable"
                if rejected_reason:
                    log_stage("sidecar", f"rejected: {rejected_reason}")
                    round_entries.append({
                        'round': round_num + 1,
                        'action': action,
                        'rejected_by_controller': True,
                        'reason': rejected_reason,
                        'dom_change_score': 0,
                    })
                    continue

                # Capture pre-state
                dom_sig_before = self._get_dom_signature(page)
                progress_before = self._read_bound_progress(page)

                # Drain state change buffer BEFORE action (baseline clear)
                drain_state_changes(page)

                # Layer B: capture pre-click locator BEFORE execution when coords
                # are already available (non-BID actions). For BID actions, x/y is
                # set inside _execute_action so we capture immediately after.
                pre_click_locator = None
                has_coords_before = 'x' in action and 'y' in action
                if has_coords_before and action_type in ('click', 'hover', 'double_click', 'focus'):
                    try:
                        pre_click_locator = get_locator_cascade(
                            page, float(action['x']), float(action['y'])
                        )
                    except Exception:
                        pass

                # Execute
                hit_info = self._execute_action(
                    page, action, viewport_state,
                    element_catalog=element_catalog)
                total_actions += 1

                # For BID-resolved actions, capture post-exec locator as pre-click
                # (best available — BID resolution happens inside _execute_action)
                if not has_coords_before and 'x' in action and 'y' in action:
                    if action_type in ('click', 'hover', 'double_click', 'focus'):
                        try:
                            pre_click_locator = get_locator_cascade(
                                page, float(action['x']), float(action['y'])
                            )
                        except Exception:
                            pass

                # Tiny settle for hooks + microtasks to fire
                page.wait_for_timeout(75)

                # Get post-settle locator (may reflect DOM changes from action)
                locator_info = None
                if action_type in ('click', 'hover', 'double_click', 'focus') and 'x' in action and 'y' in action:
                    try:
                        locator_info = get_locator_cascade(
                            page, float(action['x']), float(action['y'])
                        )
                    except Exception:
                        pass

                # Drain state changes AFTER action (causally attributable)
                post_action_state = drain_state_changes(page)

                # Capture post-state
                dom_sig_after = self._get_dom_signature(page)
                dom_change_score = self._compute_change_score(dom_sig_before, dom_sig_after)
                progress_after = self._read_bound_progress(page)

                # Completion sweep: if progress just hit 100%, click reveal/complete button
                if (progress_after and progress_after.get('fraction', 0) >= 1.0 and
                        (not progress_before or progress_before.get('fraction', 0) < 1.0)):
                    log_stage("sidecar", "progress reached 100% — scanning for completion button")
                    sweep_code = self._completion_sweep(page)
                    if sweep_code:
                        action_log.append({
                            'round': round_num + 1, 'action': action,
                            'hit': hit_info,
                            'pre_click_locator': pre_click_locator,
                            'locator': locator_info,
                            'hit_tag': hit_info.get('hit_tag'),
                            'dom_change_score': dom_change_score,
                            'progress_before': progress_before.get('fraction') if progress_before else None,
                            'progress_after': 1.0,
                            'found_code': True,
                        })
                        return self._finalize_code_return(
                            sweep_code, 'completion_sweep', page,
                            baseline_codes, action_log, round_num, total_actions,
                            step, version, context, iframe_count,
                        )

                # Detect meaningful state changes (color/targetability)
                _MEANINGFUL_STATE = {
                    'turned_green', 'turned_red', 'enabled', 'appeared',
                    'became_clickable', 'activated', 'became_vibrant',
                    'revealed', 'became_interactive', 'new_element',
                    'text_changed',
                }
                meaningful_state_change = False
                for sc in (post_action_state or []):
                    for ch in sc.get('changes', []):
                        if ch in _MEANINGFUL_STATE:
                            meaningful_state_change = True
                            # Notify code scorer for causality tracking
                            try:
                                from code_scorer import note_state_change
                                note_state_change(ch)
                            except Exception:
                                pass

                # Record in action log
                entry = {
                    'round': round_num + 1,
                    'action': action,
                    'hit': hit_info,
                    'pre_click_locator': pre_click_locator,  # Layer B: stable intended target
                    'locator': locator_info,
                    'hit_tag': hit_info.get('hit_tag'),
                    'dom_sig_before': dom_sig_before,
                    'dom_sig_after': dom_sig_after,
                    'dom_change_score': dom_change_score,
                    'progress_before': progress_before.get('fraction') if progress_before else None,
                    'progress_after': progress_after.get('fraction') if progress_after else None,
                    'found_code': False,
                    'post_state_changes': post_action_state,  # For promotion (Fix 6)
                    'triggered_by_state_change': bool(
                        context.get('state_changes') and
                        any(sc.get('matched_bid') == action.get('element_id')
                            for sc in context.get('state_changes', []))
                    ),
                }
                action_log.append(entry)
                round_entries.append(entry)

                # Track failed actions for anti-loop
                # Also check body text fingerprint — catches tiny text changes
                # like "3 more times" → "2 more times" that DOM signature misses
                text_fp_after = self._get_body_text_fingerprint(page)
                text_changed = text_fp_after != body_text_fingerprint
                if text_changed:
                    body_text_fingerprint = text_fp_after  # update baseline
                progress_appeared = (progress_before is None and progress_after is not None)
                action_had_no_effect = (
                    dom_change_score < 0.01 and not text_changed and not progress_appeared
                    and not meaningful_state_change  # Color/targetability changes count
                    and (
                        progress_after is None or progress_before is None or
                        (progress_after.get('fraction', 0) <= (progress_before or {}).get('fraction', 0))
                    )
                )
                # Scroll that actually moved viewport is not "no effect"
                if action_had_no_effect and hit_info.get('scroll_moved'):
                    action_had_no_effect = False

                # Overlay retry: if a click had no effect, resolve the true
                # interactable element via elementsFromPoint and retry once
                if (action_had_no_effect and action_type == 'click'
                        and hit_info.get('executed')
                        and action.get('x') is not None):
                    try:
                        from primitives import resolve_click_target
                        x, y = float(action['x']), float(action['y'])
                        resolution = resolve_click_target(page, x, y)
                        center = resolution.get('chosen_center')
                        chosen = resolution.get('chosen')
                        dossier = resolution.get('dossier', '')
                        log_stage("sidecar",
                                  f"click no-effect → elementsFromPoint dossier: {dossier[:200]}")
                        if center and chosen:
                            retry_worked = False
                            # If resolved center differs from original, retry mouse click there
                            if abs(center['x'] - x) > 3 or abs(center['y'] - y) > 3:
                                log_stage("sidecar",
                                          f"retrying click at resolved target "
                                          f"<{chosen.get('tag')}> ({center['x']},{center['y']}) "
                                          f"vs original ({x},{y})")
                                page.mouse.click(center['x'], center['y'])
                                page.wait_for_timeout(100)
                                retry_sig = self._get_dom_signature(page)
                                retry_score = self.knowledge_reader.compute_dom_change_score(
                                    dom_sig_after, retry_sig) if hasattr(self, 'knowledge_reader') else 0
                                if retry_score > 0.01:
                                    retry_worked = True

                            # JS .click() fallback — mouse click may fail on
                            # deeply nested React components (e.g. recursive_iframe)
                            if not retry_worked and chosen.get('tag', '').upper() == 'BUTTON':
                                log_stage("sidecar",
                                          f"JS click fallback on <BUTTON> at ({center['x']},{center['y']})")
                                page.evaluate(f'''() => {{
                                    const el = document.elementFromPoint({center['x']}, {center['y']});
                                    if (el) el.click();
                                }}''')
                                page.wait_for_timeout(200)
                                retry_sig = self._get_dom_signature(page)
                                retry_score = self.knowledge_reader.compute_dom_change_score(
                                    dom_sig_after, retry_sig) if hasattr(self, 'knowledge_reader') else 0
                                if retry_score > 0.01:
                                    retry_worked = True

                            if retry_worked:
                                action_had_no_effect = False
                                entry['retry_resolved'] = True
                                hit_info['retry_tag'] = chosen.get('tag')
                    except Exception as e:
                        log_stage("sidecar", f"overlay retry failed: {e}")

                if action_had_no_effect and action_type not in ('wait', 'wait_for_state'):
                    # Mark the targeted element as spent so it's filtered
                    # from subsequent rounds' catalogs entirely
                    eid = action.get('element_id')
                    if eid is not None and eid in bid_to_elsig:
                        spent_elements.add(bid_to_elsig[eid])
                    else:
                        # Coord-based action: find nearest catalog element
                        nearest = _nearest_element_sig(
                            action, element_catalog)
                        if nearest:
                            spent_elements.add(nearest)

                # Early code check — fast path before full harvest (~1ms vs ~50ms)
                quick_code = extract_code_js(page)
                if quick_code and is_valid_code(quick_code) and quick_code not in self._rejected_codes:
                    mid_progress = self._read_bound_progress(page)
                    if not (mid_progress and mid_progress.get('fraction', 1.0) < 1.0):
                        entry['found_code'] = True
                        return self._finalize_code_return(
                            quick_code, 'early_extract', page,
                            baseline_codes, action_log, round_num, total_actions,
                            step, version, context, iframe_count,
                            last_dom_change=dom_change_score,
                        )

                # Passive harvest after action (main page + frames)
                code = self._harvest_code(page)
                if not code and len(page.frames) > 1:
                    code = self._frame_harvest(page)
                if code:
                    # Check if challenge progress is incomplete before terminating
                    mid_progress = self._read_bound_progress(page)
                    mid_incomplete = (
                        mid_progress is not None
                        and mid_progress.get('fraction', 1.0) < 1.0
                    )
                    if mid_incomplete:
                        log_stage("sidecar",
                                  f"mid-action code {code} but progress "
                                  f"{mid_progress.get('current')}/{mid_progress.get('total')} "
                                  f"— continuing")
                        known_codes.add(code)
                    else:
                        entry['found_code'] = True
                        return self._finalize_code_return(
                            code, self._code_source or 'harvest', page,
                            baseline_codes, action_log, round_num, total_actions,
                            step, version, context, iframe_count,
                            last_dom_change=dom_change_score,
                        )

            # f. Post-round settle
            self._wait_for_settle(page, max_ms=600)
            self._dismiss_popups(page)

            # Post-round PEEK: read state changes caused by THIS round's actions
            # WITHOUT clearing the buffer. The next round's pre-round drain will
            # get these changes AND forward them to the planner.
            post_round_raw = peek_state_changes(page)
            if post_round_raw:
                post_classified = classify_state_changes(post_round_raw)
                match_state_changes_to_catalog(post_classified, element_catalog)
                state_changes = state_changes + post_classified

            code = self._harvest_code(page)
            if code:
                # Before terminating, check if challenge progress is incomplete.
                # Rotating code challenges show codes at all times (decoy/stale)
                # but require multiple actions (e.g., "Capture 0/3").
                cur_progress = self._read_bound_progress(page)
                progress_incomplete = (
                    cur_progress is not None
                    and cur_progress.get('fraction', 1.0) < 1.0
                )
                if progress_incomplete:
                    log_stage("sidecar",
                              f"code {code} found but progress incomplete "
                              f"({cur_progress.get('current')}/{cur_progress.get('total')}) "
                              f"— continuing")
                    known_codes.add(code)
                else:
                    return self._finalize_code_return(
                        code, self._code_source or 'harvest', page,
                        baseline_codes, action_log, round_num, total_actions,
                        step, version, context, iframe_count,
                    )

            # g. Re-observe + broad progress definition
            new_progress = self._read_bound_progress(page)
            new_dom_sig = self._get_dom_signature(page)
            round_dom_change = self._compute_change_score(round_start_sig, new_dom_sig)
            new_codes = self._harvest_codes_set(page) - known_codes
            new_clickable_hash = self._get_clickable_hash(page)
            new_body_fp = self._get_body_text_fingerprint(page)

            # Compute progress delta
            if baseline_progress and new_progress:
                progress_delta = new_progress['fraction'] - baseline_progress['fraction']
            elif new_progress and not baseline_progress:
                progress_delta = new_progress['fraction']
            else:
                progress_delta = None

            # Only count matched, high-confidence state changes as progress
            progress_state_changes = [
                sc for sc in state_changes
                if sc.get('matched_bid') is not None
                and any(ch in PROGRESS_CHANGE_TYPES for ch in sc.get('changes', []))
            ]
            made_progress = (
                (progress_delta is not None and progress_delta > 0)
                or round_dom_change >= 0.05
                or len(new_codes) > 0
                or new_clickable_hash != clickable_hash
                or new_body_fp != round_start_body_fp  # compare vs round START, not per-action updated
                or len(progress_state_changes) > 0  # real state transitions = progress
            )

            if made_progress:
                last_progress_time = time.time()
                clickable_hash = new_clickable_hash
                body_text_fingerprint = new_body_fp
                self.clear_soft_rejections_on_progress()
                # Extend round budget when making real progress
                if round_num >= effective_max_rounds - 1:
                    effective_max_rounds = min(
                        effective_max_rounds + 1, self.MAX_ROUNDS_EXTENDED)
                    log_stage("sidecar",
                              f"progress detected — extending to {effective_max_rounds} rounds")
                # State changed — previously-spent elements may now be relevant
                if spent_elements:
                    log_stage("sidecar",
                              f"clearing {len(spent_elements)} spent elements (progress advanced)")
                    spent_elements.clear()

            # h. Build reflection on failed rounds + strategy suggestion
            reflection = None
            if not made_progress and round_entries:
                lines = []
                for entry in round_entries:
                    # Controller rejections get their own format
                    if entry.get('rejected_by_controller'):
                        act = entry['action']
                        atype = act.get('type', '?')
                        lines.append(f"  - {atype} REJECTED: {entry['reason']}")
                        continue
                    dc = entry.get('dom_change_score', 0)
                    act = entry['action']
                    hit = entry.get('hit_tag', '?')
                    effect = 'NO EFFECT' if dc < 0.01 else f'small change ({dc:.2f})'
                    atype = act.get('type', '?')
                    # Format coordinates — BID-based drag shows source→target
                    if atype == 'drag' and act.get('source_id') is not None:
                        coord = f"bid={act['source_id']}→{act.get('target_id','?')}"
                    else:
                        eid = act.get('element_id', '')
                        coord = f"bid={eid}" if eid else f"({act.get('x','?')},{act.get('y','?')})"
                    # Append inline progress and drag method if available
                    extras = []
                    if entry.get('inline_progress'):
                        extras.append(f"progress: {entry['inline_progress']}")
                    dr = entry.get('drag_result')
                    if dr and isinstance(dr, dict):
                        extras.append(f"method: {dr.get('method', '?')}")
                    suffix = f" ({', '.join(extras)})" if extras else ""
                    lines.append(f"  - {atype} at {coord} hit={hit} → {effect}{suffix}")
                # Strategy bandit suggestion
                selected_strategy = self.strategy_bandit.select(tried_strategies)
                current_round_strategy = selected_strategy
                tried_strategies.add(selected_strategy)
                strategy_hint = STRATEGIES[selected_strategy]
                reflection = ("REFLECTION: Previous round made NO progress.\n"
                              + "\n".join(lines)
                              + f"\nDo NOT repeat these. TRY THIS STRATEGY: {strategy_hint}")
                # If there are state changes despite no "progress", highlight them
                if raw_state_changes:
                    sc_lines = []
                    for sc in state_changes[:5]:
                        bid_str = f" [BID {sc['matched_bid']}]" if sc.get('matched_bid') is not None else ""
                        sc_lines.append(
                            f"  * {','.join(sc['changes'])}: {sc.get('tag','')} "
                            f"\"{sc.get('text','')}\" at ({sc.get('x','?')},{sc.get('y','?')}){bid_str} "
                            f"(priority: {sc.get('priority', 0):.1f})"
                        )
                    if sc_lines:
                        reflection += ("\n\nSTATE CHANGES DETECTED (try these first!):\n"
                                       + "\n".join(sc_lines))
            else:
                # Update strategy bandit with positive reward for progress
                if made_progress and current_round_strategy:
                    reward = (
                        ((progress_delta or 0) * 2)
                        + round_dom_change
                        + len(new_codes) * 0.5
                    )
                    self.strategy_bandit.update(current_round_strategy, reward)
                    current_round_strategy = None  # Reset to avoid double-credit

            # i. Build history entry
            history_entry = {
                'round': round_num + 1,
                'actions': [
                    {'type': a['action'].get('type', '?'), 'hit': a.get('hit_tag')}
                    for a in round_entries
                ],
                'progress_delta': progress_delta,
                'dom_change_score': round_dom_change,
                'codes_found': list(new_codes),
                'outcome': (
                    'code_found' if new_codes
                    else ('progress' if made_progress else 'no_change')
                ),
            }
            if reflection:
                history_entry['reflection'] = reflection
            history.append(history_entry)

            # j. Stall detection
            if not made_progress:
                consecutive_stalls += 1
            else:
                consecutive_stalls = 0

            # Check dynamic round cap (progress extends it, no progress stops at base)
            if round_num >= effective_max_rounds - 1 and not made_progress:
                break

            if consecutive_stalls >= self.MAX_CONSECUTIVE_STALLS:
                # Try completion sweep — always try, not just when progress=100%
                # (websocket/service_worker have no progress bar)
                stall_progress = self._read_bound_progress(page)
                stall_should_sweep = (
                    (stall_progress and stall_progress.get('fraction', 0) >= 1.0)
                    or not stall_progress
                )
                if stall_should_sweep:
                    log_stage("sidecar", f"stalled — trying completion sweep (progress={stall_progress})")
                    comp_code = self._completion_sweep(page)
                    if comp_code:
                        return self._finalize_code_return(
                            comp_code, 'completion_sweep', page, baseline_codes,
                            action_log, round_num, total_actions,
                            step, version, context, iframe_count,
                        )
                # Single bounded sweep of 4 non-anchor buttons, then stop
                sweep_code = self._clickable_sweep(
                    page, already_clicked_sweep, budget=4,
                    action_log=action_log)
                if sweep_code:
                    return self._finalize_code_return(
                        sweep_code, 'sweep', page, baseline_codes,
                        action_log, round_num, total_actions,
                        step, version, context, iframe_count,
                    )
                self._log_state_change_hit_rate(action_log)
                return self._make_result(
                    False, None, round_num + 1, total_actions,
                    'stalled', action_log, None, iframe_count
                )

            known_codes |= new_codes

        # 3. Max rounds exhausted — last-ditch completion sweep
        #    Always try sweep (not just when progress=100%) because some challenges
        #    (websocket, service_worker) have no progress bar but DO have Reveal Code buttons.
        rounds_used = round_num + 1 if 'round_num' in dir() else effective_max_rounds
        last_progress = self._read_bound_progress(page)
        should_sweep = (
            (last_progress and last_progress.get('fraction', 0) >= 1.0)
            or not last_progress  # no progress bar → sweep unconditionally
            or (last_progress and last_progress.get('fraction') is None)
        )
        if should_sweep:
            log_stage("sidecar", f"max rounds — trying completion sweep (progress={last_progress})")
            sweep_code = self._completion_sweep(page)
            if sweep_code:
                return self._finalize_code_return(
                    sweep_code, 'completion_sweep', page,
                    baseline_codes, action_log, rounds_used, total_actions,
                    step, version, context, iframe_count,
                )
        self._log_state_change_hit_rate(action_log)
        return self._make_result(
            False, None, rounds_used, total_actions,
            'max_rounds', action_log, None, iframe_count
        )

    # ── Promotion ─────────────────────────────────────────────────────────

    @staticmethod
    def _clean_recipe_steps(recipe: list[dict]) -> list[dict]:
        """Strip noise from recipe steps regardless of source (sidecar or exploration).

        Removes:
        - Popup dismiss clicks (by text matching)
        - Coord-only steps with no text AND no assertions (pure noise)
        - Redundant consecutive clicks on same target (keeps last, preserves assertions)
        - Steps targeting 6-char code strings (session-specific)
        - Converts bare 'wait' to 'wait_for_state'
        """
        from config import CHARSET
        code_re = re.compile(r'^[' + re.escape(CHARSET) + r']{6}$')

        # Expanded popup label set — includes partial matches
        _POPUP_LABELS_EXACT = {
            'close', 'dismiss', 'accept', 'decline', 'got it', 'ok',
            'no thanks', 'submit & continue', 'close (fake)',
            'close modal', 'i understand', 'continue',
        }
        _POPUP_LABELS_PARTIAL = {'dismiss', 'close', 'got it', 'no thanks'}

        def _is_popup_step(step_):
            text = (step_.get('target_text') or '').strip().lower()
            if not text:
                return False
            if text in _POPUP_LABELS_EXACT:
                return True
            # Partial match: "Close X", "Dismiss overlay", etc.
            for label in _POPUP_LABELS_PARTIAL:
                if text.startswith(label):
                    return True
            return False

        def _has_any_assertion(step_):
            return (step_.get('expect_code_visible')
                    or step_.get('expect_progress_delta') is not None
                    or step_.get('expect_state_changes')
                    or step_.get('expect_dom_change_score') is not None
                    or step_.get('expect_selector_visible')
                    or step_.get('expect_text_contains'))

        def _is_coord_only_noise(step_):
            """Coord-only click/hover with no text and no assertions = noise."""
            atype = step_.get('action_type', '')
            if atype not in ('click', 'hover', 'double_click', 'focus'):
                return False
            has_text = bool(step_.get('target_text') or step_.get('target_name')
                           or step_.get('target_selector') or step_.get('target_role'))
            if has_text:
                return False
            if _has_any_assertion(step_):
                return False
            return bool(step_.get('target_coords'))

        def _is_code_target(step_):
            """Target text is a session-specific code or fragment.

            Catches: 6-char codes (e.g. "ABCD23"), 2-3 char uppercase fragments
            (e.g. "SC", "BK", "QK" from split_parts challenges).
            """
            text = (step_.get('target_text') or '').strip()
            if not text:
                return False
            if code_re.match(text):
                return True
            # 2-3 char uppercase code fragments (split_parts): require both alpha AND digit
            # Pure-alpha like "OK", "TAB" are legitimate UI labels, not code fragments
            if 2 <= len(text) <= 3 and re.fullmatch(r'[A-Z0-9]{2,3}', text):
                if any(c.isalpha() for c in text) and any(c.isdigit() for c in text):
                    return True
            return False

        cleaned = []
        stripped_count = 0
        for step_ in recipe:
            # Strip popup dismiss clicks
            if _is_popup_step(step_):
                stripped_count += 1
                continue
            # Strip coord-only noise
            if _is_coord_only_noise(step_):
                stripped_count += 1
                continue
            # Strip steps targeting 6-char codes
            if _is_code_target(step_):
                stripped_count += 1
                continue
            # Smart wait conversion:
            # - wait_for_state + expect_code_visible → wait (timer pattern: need full duration)
            # - Short wait (<3s) without code expectation → wait_for_state (state polling is faster)
            # - Long wait (≥3s) → keep as wait (timer-based challenges need full duration)
            atype = step_.get('action_type', '')
            if atype == 'wait_for_state' and step_.get('expect_code_visible'):
                step_ = dict(step_)
                step_['action_type'] = 'wait'
                # Ensure minimum delay for code to render
                if (step_.get('delay_ms') or 0) < 3000:
                    step_['delay_ms'] = 7000
            elif atype == 'wait' and not step_.get('expect_code_visible'):
                if (step_.get('delay_ms') or 0) < 3000:
                    step_ = dict(step_)
                    step_['action_type'] = 'wait_for_state'

            # ── Phase 2 cleanup passes on target_text ──
            txt = step_.get('target_text')
            if txt:
                step_ = dict(step_)  # Don't mutate original
                # 2A: Strip status symbols (✓ ✔ ✖ ✘ ● ○ •)
                _STATUS_SYMBOLS = '\u2713\u2714\u2716\u2718\u25cf\u25cb\u2022'
                txt = txt.translate(str.maketrans('', '', _STATUS_SYMBOLS)).strip()
                # 2B: Strip tab label session data ("Tab 1: KA" → "Tab 1")
                txt = re.sub(r':\s*[A-Z0-9]{2,4}$', '', txt).strip()
                # 2C: Strip frame numbers ("Frame 44" → "Frame")
                txt = re.sub(r'^(Frame)\s+\d+$', r'\1', txt)
                # 2D: Strip parenthetical counters ("Capture (0/3)" → "Capture",
                #      "Click Me (2/5)" → "Click Me") — counters are session-specific
                txt = re.sub(r'\s*\(\d+/\d+\)\s*$', '', txt).strip()
                # 2E: Deduplicate repeated words ("Connected Connected" → "Connected")
                words = txt.split()
                if len(words) >= 2 and len(set(words)) == 1:
                    txt = words[0]
                if txt:
                    step_['target_text'] = txt
                    # Regenerate target_pattern from cleaned text
                    pattern = LearningSidecar._generate_target_pattern(txt)
                    if pattern:
                        step_['target_pattern'] = pattern
                else:
                    del step_['target_text']

            cleaned.append(step_)

        # Deduplicate consecutive clicks on same target (keep last one)
        deduped = []
        for i, step_ in enumerate(cleaned):
            atype = step_.get('action_type', '')
            text = (step_.get('target_text') or '').strip().lower()
            if (atype == 'click' and text and i + 1 < len(cleaned)
                    and cleaned[i + 1].get('action_type') == 'click'
                    and (cleaned[i + 1].get('target_text') or '').strip().lower() == text):
                # Next step clicks same target — skip this one, keep next (has assertions)
                stripped_count += 1
                continue
            deduped.append(step_)

        if stripped_count > 0:
            log_stage("sidecar", f"recipe cleanup: stripped {stripped_count} noise steps "
                      f"({len(recipe)} -> {len(deduped)})")
        return deduped

    @staticmethod
    def _lint_recipe(recipe: list[dict]) -> tuple[float, list[str]]:
        """Score a recipe for replayability. Returns (score 0-1, list of issues).

        Checks for:
        - Unsupported action types (fatal)
        - Session-specific data (6-char codes, dynamic labels)
        - Coord-only steps without text/selector fallbacks
        - Hardcoded puzzle answers
        """
        from config import CHARSET
        SUPPORTED_TYPES = {
            'click', 'hover', 'type', 'press', 'keyboard_sequence',
            'scroll', 'drag', 'wait', 'wait_for_state',
            'run_agent', 'switch_tab', 'switch_frame',
            'element_scroll', 'draw', 'canvas_draw',
            'double_click', 'focus', 'repeat_click', 'drag_drop_auto', 'reactive_click',
            'fiber_bypass', 'finish_navigation',
        }
        CODE_PATTERN = re.compile(
            r'^[' + re.escape(CHARSET) + r']{6}$'
        )
        issues = []
        if not recipe:
            return 0.0, ['empty_recipe']

        total_steps = len(recipe)
        coord_only_count = 0
        session_specific_count = 0

        for s in recipe:
            atype = s.get('action_type', '')

            # Check unsupported action types
            if atype not in SUPPORTED_TYPES:
                issues.append(f'unsupported:{atype}')
                return 0.0, issues  # Fatal — immediately block

            # Check for session-specific text in target_text
            txt = s.get('target_text', '') or ''
            if txt and CODE_PATTERN.match(txt.strip()):
                session_specific_count += 1
                issues.append(f'session_specific_text:"{txt}"')

            # Check for tab labels with dynamic codes (e.g. "Tab 1: 6H")
            if txt and re.match(r'^Tab \d+:\s*[A-Z0-9]{2}', txt):
                session_specific_count += 1
                issues.append(f'dynamic_tab_label:"{txt}"')

            # Check for hardcoded puzzle answers in type actions
            # Skip if a resolver is attached — it'll compute the value dynamically
            if atype == 'type' and s.get('value') and not s.get('resolver'):
                val = s['value'].strip()
                # Pure numeric answers are likely session-specific math solutions
                if val.isdigit() and len(val) <= 4:
                    session_specific_count += 1
                    issues.append(f'hardcoded_answer:"{val}"')

            # Check for post-completion symbols in target_text (✓ ● etc.)
            if txt and re.search(r'[\u2713\u2714\u2716\u2718\u25cf\u25cb\u2022]', txt):
                session_specific_count += 1
                issues.append(f'post_completion_symbol:"{txt}"')

            # Check targeting quality for actions that need a target
            has_text = bool(s.get('target_text') or s.get('target_name'))
            has_selector = bool(s.get('target_selector'))
            has_role = bool(s.get('target_role'))
            has_pattern = bool(s.get('target_pattern'))
            has_coords = bool(s.get('target_coords'))
            target_actions = ('click', 'hover', 'double_click', 'focus', 'drag')
            # Fatal: no targeting at all — can't locate element
            if atype in target_actions and not (has_text or has_selector or has_role or has_pattern or has_coords):
                issues.append(f'no_target:{atype}')
                return 0.0, issues
            # Fragile: coord-only without text/selector fallback
            if atype in target_actions and has_coords and not (has_text or has_selector or has_role or has_pattern):
                coord_only_count += 1

        # Scoring
        score = 1.0
        # Session-specific data is very bad
        if session_specific_count > 0:
            score -= 0.4 * min(session_specific_count / total_steps, 1.0)
            # If majority of actions use session data, block it
            if session_specific_count >= total_steps * 0.5:
                issues.append('majority_session_specific')
                return 0.1, issues
        # Coord-only is fragile — stronger penalty for 100% coord-only
        if coord_only_count > 0:
            coord_ratio = coord_only_count / total_steps
            score -= 0.5 * coord_ratio
            if coord_ratio > 0.7:
                issues.append(f'mostly_coord_only({coord_only_count}/{total_steps})')

        return max(0.0, score), issues

    def _ai_review_recipe(self, page, heuristic_recipe: list[dict],
                          promotion_candidate: dict,
                          step_snapshot=None) -> tuple[list[dict] | None, list[str]]:
        """Claude reviews heuristic recipe for targeting quality.

        NOT challenge-type-specific. Only improves mechanical quality:
        stable locators, stripped session data, patterns for variable text.
        Uses step_snapshot screenshot (pristine state) instead of live page (step N+1).
        Returns (improved_recipe_or_None, ai_keywords).
        """
        import re as _re
        try:
            import base64
            import json as _json
            import os
            import anthropic

            api_key = os.getenv("ANTHROPIC_API_KEY")
            if not api_key:
                return None, []

            # Skip review for simple recipes that already have stable locators
            needs_review = False
            for s in heuristic_recipe:
                if s.get('action_type') in ('wait', 'wait_for_state', 'scroll',
                                             'drag_drop_auto', 'draw'):
                    continue  # These don't need locator improvement
                has_text = bool(s.get('target_text') or s.get('target_name')
                               or s.get('target_role'))
                has_only_coords = (s.get('target_coords') and not has_text
                                   and not s.get('target_selector'))
                has_session_data = bool(
                    _re.search(r'[\(\[]\d+/\d+[\)\]]|:\s*[A-Z0-9]{2,4}$',
                               s.get('target_text', '')))
                if has_only_coords or has_session_data:
                    needs_review = True
                    break

            if not needs_review:
                return None, []  # Heuristic recipe is already clean

            recipe_text = _json.dumps(heuristic_recipe, indent=2)

            # Gather round observations from Layer A
            observations = promotion_candidate.get('_round_observations', [])
            obs_text = '\n'.join(observations[:5]) if observations else '(none captured)'

            # Use step-start snapshot screenshot (pristine challenge state).
            # The live page is already on step N+1 by the time this runs.
            if step_snapshot and step_snapshot.screenshot_b64:
                screenshot_b64 = step_snapshot.screenshot_b64
            else:
                # No snapshot available — skip AI review rather than using wrong-page screenshot
                log_stage("sidecar", "AI review skipped: no step-start screenshot available")
                return None, []

            # Build confusion context for keyword generation
            ctype = promotion_candidate.get('challenge_type', 'unknown')
            confusion_ctx = ""
            confusion_learning = self.knowledge_reader.get_by_type(ctype)
            if confusion_learning and getattr(confusion_learning, 'confused_with', []):
                names = [e['actual_type'] for e in confusion_learning.confused_with]
                confusion_ctx = (f"This type has been confused with: {', '.join(names)}. "
                                f"Choose keywords that distinguish it from those types.")

            prompt = f"""Review this browser automation recipe and improve its targeting quality.

RECIPE (from heuristic extraction):
{recipe_text}

CONTEXT (what was observed during solving):
{obs_text}

RULES — you are ONLY improving mechanical quality, not changing the strategy:
1. Replace coordinate-only targeting with text or role-based targeting. Use the screenshot to identify visible element labels.
2. Strip session-specific data from target_text: remove counters "(0/3)", suffixes ": AB", frame numbers. Keep the stable root word(s).
3. For text that varies between sessions, add target_pattern with regex word boundaries. Example: "Capture (0/3)" → target_text: "Capture", target_pattern: "\\\\bCapture\\\\b"
4. Do NOT add new steps. Do NOT remove steps. Do NOT change action_type or value fields.
5. Do NOT reference challenge types or solving strategies.
6. Every click/hover/focus step must have at least one of: target_text, target_pattern, target_role + target_name, or target_selector.
7. target_coords should remain as a fallback but never be the ONLY locator.

Return ONLY a JSON array of the improved recipe steps. No explanation.

ALSO: On a separate line, output exactly:
KEYWORDS_JSON={{"keywords": ["word1", "word2", ...]}}
containing 3-8 words visible in the screenshot that UNIQUELY identify this challenge.
Avoid generic words (click, submit, next, challenge, step, code).
{confusion_ctx}"""

            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-sonnet-4-5-20250929",
                max_tokens=1024,
                temperature=0.1,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64", "media_type": "image/png",
                            "data": screenshot_b64}},
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            # Token tracking for AI review
            if hasattr(response, 'usage') and response.usage:
                u = response.usage
                log_stage("sidecar", f"AI review tokens: {getattr(u, 'input_tokens', 0)} in / "
                          f"{getattr(u, 'output_tokens', 0)} out")

            text = response.content[0].text.strip()

            # Parse AI keywords from KEYWORDS_JSON sentinel
            ai_keywords = []
            kw_marker = text.find('KEYWORDS_JSON=')
            if kw_marker >= 0:
                kw_text = text[kw_marker + len('KEYWORDS_JSON='):]
                try:
                    kw_obj = _json.loads(kw_text.strip().split('\n')[0])
                    ai_keywords = [str(k).lower().strip() for k in kw_obj.get('keywords', [])
                                  if isinstance(k, str) and 2 < len(str(k).strip()) <= 30][:8]
                except (_json.JSONDecodeError, IndexError):
                    pass

            # Strip KEYWORDS_JSON line before looking for recipe JSON
            # (model outputs recipe array, then KEYWORDS_JSON on next line —
            # rfind(']') would match the ] inside KEYWORDS_JSON keywords list)
            recipe_text_clean = text
            if kw_marker >= 0:
                recipe_text_clean = text[:kw_marker].rstrip()

            # Find the recipe JSON array — must contain action_type objects
            start = recipe_text_clean.find('[')
            end = recipe_text_clean.rfind(']')
            if start < 0 or end <= start:
                log_stage("sidecar", "AI review rejected: no JSON array in response")
                return None, ai_keywords

            ai_recipe = _json.loads(recipe_text_clean[start:end + 1])

            # ── Programmatic validation ──
            # Guard 1: Must be a list with EXACT same length
            if not isinstance(ai_recipe, list) or len(ai_recipe) != len(heuristic_recipe):
                log_stage("sidecar", f"AI review rejected: step count "
                          f"{len(ai_recipe) if isinstance(ai_recipe, list) else 'N/A'} "
                          f"vs expected {len(heuristic_recipe)}")
                return None, ai_keywords

            # Guard 2: action_type sequence must match exactly
            orig_types = [s.get('action_type') for s in heuristic_recipe]
            ai_types = [s.get('action_type') for s in ai_recipe]
            if orig_types != ai_types:
                log_stage("sidecar", f"AI review rejected: action_type sequence mismatch "
                          f"{ai_types} vs {orig_types}")
                return None, ai_keywords

            # Guard 3: value fields must be preserved exactly
            for i, (orig, ai_step) in enumerate(zip(heuristic_recipe, ai_recipe)):
                orig_val = orig.get('value')
                ai_val = ai_step.get('value')
                if orig_val != ai_val:
                    log_stage("sidecar", f"AI review rejected: step {i} value changed "
                              f"'{orig_val}' → '{ai_val}'")
                    return None, ai_keywords

            # Guard 4: Each step must be a dict with required fields
            for i, step in enumerate(ai_recipe):
                if not isinstance(step, dict):
                    log_stage("sidecar", f"AI review rejected: step {i} is not a dict")
                    return None, ai_keywords
                if 'action_type' not in step:
                    log_stage("sidecar", f"AI review rejected: step {i} missing action_type")
                    return None, ai_keywords

            log_stage("sidecar", f"AI review improved {len(ai_recipe)} recipe steps")
            return ai_recipe, ai_keywords

        except Exception as e:
            log_stage("sidecar", f"AI recipe review failed: {e}")

        return None, []

    def finalize_promotion(self, page, step: int, version: int,
                           promotion_candidate: dict,
                           step_snapshot=None) -> bool:
        """3-tier promotion. Called by orchestrator ONLY after confirmed step advancement.

        Tier 1 "soft recipe": promote if ANY stable locator OR (non-wait action + post-baseline code).
        Starts with low confidence and TTL=5. Hardens to Tier 2 after 1 replay success,
        Tier 3 after 3 replay successes.

        step_snapshot: StepContextSnapshot captured at step start (pristine challenge state).
        Used for fingerprinting and AI review instead of live page (which is now step N+1).

        Returns True if a new recipe was promoted, False otherwise.
        """
        from agents.recipe_executor import RecipeExecutor
        from knowledge_reader import StrategyVariant, wilson_score_lower

        ctype = promotion_candidate.get('challenge_type', 'unknown')

        # Never promote under generic fallback type names — they match ALL steps
        # Check base type (strip version suffix like "_v3")
        GENERIC_TYPES = {'simple', 'unknown', 'default', 'basic'}
        base_type = re.sub(r'_v\d+$', '', ctype)
        if base_type in GENERIC_TYPES:
            log_stage("sidecar", f"skip promotion: generic type '{ctype}' would match all steps")
            return False

        recipe = promotion_candidate.get('recipe', [])
        dna_sig = promotion_candidate.get('dna_signature')
        code_source = promotion_candidate.get('code_source', 'unknown')
        has_stable_locator = promotion_candidate.get('assertions_present', False)

        # Tier 1 gate: need at least ONE signal that recipe is replayable
        non_wait_actions = [s for s in recipe
                           if s.get('action_type', 'wait') not in ('wait', 'wait_for_state')]
        has_dna = dna_sig is not None
        qualifies = (has_stable_locator
                     or len(non_wait_actions) > 0
                     or has_dna)  # Wait-only with DNA = delayed reveal

        if not qualifies:
            log_stage("sidecar", f"skip promotion: no locator/action/dna for {ctype}")
            return False

        if not recipe:
            log_stage("sidecar", f"skip promotion: empty recipe for {ctype}")
            return False

        # Hard-fail: reject recipes with missing required fields
        for i, s in enumerate(recipe):
            at = s.get('action_type', '')
            if at == 'keyboard_sequence' and not s.get('value'):
                log_stage("sidecar", f"BLOCKED promotion for {ctype}: "
                          f"step {i} keyboard_sequence missing 'value'")
                return False
            if at in ('draw', 'canvas_draw') and not s.get('value'):
                log_stage("sidecar", f"BLOCKED promotion for {ctype}: "
                          f"step {i} {at} missing 'value' (path)")
                return False
            if at == 'type' and not s.get('value') and not s.get('resolver'):
                log_stage("sidecar", f"BLOCKED promotion for {ctype}: "
                          f"step {i} type missing both 'value' and 'resolver'")
                return False

        # Strip internal fields before storage
        clean_recipe = []
        for s in recipe:
            step_clean = {k: v for k, v in s.items() if not k.startswith('_')}
            clean_recipe.append(step_clean)

        # Universal recipe cleanup: strip popup dismiss clicks, coord-only noise,
        # redundant duplicates, session-specific targets, convert wait→wait_for_state.
        # This catches noise from ALL sources (sidecar, exploration, seq_click).
        clean_recipe = LearningSidecar._clean_recipe_steps(clean_recipe)
        if not clean_recipe:
            # Timer/delay challenges: sidecar found code via mutation hooks while
            # waiting for API response. Synthesize a wait recipe so System 1 can
            # replay in ~6s instead of ~20s sidecar round-trip.
            TIMER_TYPES = {'delay', 'delayed_reveal', 'delay_memory', 'timing'}
            if base_type in TIMER_TYPES:
                clean_recipe = [{
                    'action_type': 'wait',
                    'delay_ms': 7000,
                    'expect_code_visible': True,
                }]
                log_stage("sidecar", f"synthesized wait recipe for timer challenge {ctype}")
            else:
                log_stage("sidecar", f"skip promotion: recipe empty after cleanup for {ctype}")
                return False

        # For drag_drop: strip session-specific piece labels (coords are version-fixed)
        if 'drag_drop' in ctype:
            for s in clean_recipe:
                if 'target_text' in s and s.get('target_coords'):
                    del s['target_text']

        # Convert coord-only drag sequences to drag_drop_auto (auto-discovers puzzle)
        drag_steps = [s for s in clean_recipe if s.get('action_type') == 'drag']
        if len(drag_steps) >= 2:
            non_text_drags = [s for s in drag_steps if not s.get('target_text')]
            if len(non_text_drags) == len(drag_steps):
                non_drag_steps = [s for s in clean_recipe
                                  if s.get('action_type') != 'drag']
                auto_step = {'action_type': 'drag_drop_auto',
                             'expect_code_visible': True}
                clean_recipe = non_drag_steps + [auto_step]
                log_stage("sidecar", f"converted {len(drag_steps)} coord-only drags "
                          f"to drag_drop_auto for {ctype}")

        compressed = RecipeExecutor.compress_recipe(clean_recipe)

        # ── Parameterized action collapsing ──
        # Pattern: N consecutive clicks on same target → click_until_progress
        if (len(compressed) >= 3 and
                all(s.get('action_type') == 'click' for s in compressed) and
                all(s.get('target_role') == compressed[0].get('target_role')
                    and s.get('target_text') == compressed[0].get('target_text')
                    for s in compressed)):
            template = dict(compressed[0])
            template['action_type'] = 'click_until_progress'
            template['params'] = {'threshold': 1.0, 'max_clicks': len(compressed) + 5}
            template['expect_code_visible'] = True
            # Remove repeat field if set (click_until_progress handles iteration)
            template.pop('repeat', None)
            compressed = [template]
            log_stage("sidecar", f"collapsed {len(clean_recipe)} clicks → click_until_progress "
                      f"for {ctype}")

        # ── AI Recipe Review (Layer C): Claude improves heuristic recipe ──
        ai_reviewed, ai_keywords = self._ai_review_recipe(
            page, compressed, promotion_candidate, step_snapshot=step_snapshot)
        if ai_reviewed is not None:
            compressed = ai_reviewed

        # ── Replayability linter ──────────────────────────────────────
        # Score the recipe for replay viability. Block or downgrade fragile ones.
        replay_score, lint_issues = self._lint_recipe(compressed)
        if lint_issues:
            log_stage("sidecar", f"promotion lint for {ctype}: "
                      f"score={replay_score:.1f}, issues={lint_issues}")
        if replay_score < 0.2:
            log_stage("sidecar", f"BLOCKED promotion for {ctype}: "
                      f"replayability too low ({replay_score:.1f})")
            return False

        # ── Block incomplete completion-only recipes ──
        COMPLETION_KEYWORDS = {'complete', 'finish', 'done', 'submit', 'next'}
        if len(compressed) == 1:
            sole = compressed[0]
            txt = (sole.get('target_text') or '').lower()
            is_completion_click = any(kw in txt for kw in COMPLETION_KEYWORDS)
            has_code = sole.get('expect_code_visible', False)
            has_progress = (sole.get('expect_progress_delta') is not None
                           and sole['expect_progress_delta'] > 0)
            has_positive_state = bool(sole.get('expect_state_changes'))
            if is_completion_click and not has_code and not has_progress and not has_positive_state:
                log_stage("sidecar", f"BLOCKED promotion for {ctype}: "
                          f"single completion-click with no observable effect")
                return False

        # ── Verify-Before-Write ──
        # NOTE: Locator verification removed — by the time finalize_promotion() runs,
        # the page has advanced to step N+1, so checking locators against the live page
        # was verifying against the wrong challenge. The lint score check above provides
        # structural quality gating without needing the live page.

        # Session data is mostly stripped by _clean_recipe_steps(), but run
        # detection as a safety net for edge cases cleanup misses.
        is_session_specific = self._detect_session_specific(compressed)

        # ── Generate detection keywords ──
        instruction_text = promotion_candidate.get('instruction_text', '')
        det_keywords = self._generate_detection_keywords(ctype, compressed, instruction_text)

        # Merge AI keywords (from Layer C) with heuristic keywords
        if ai_keywords:
            merged = list(dict.fromkeys(ai_keywords + det_keywords))
            log_stage("sidecar", f"merged {len(ai_keywords)} AI keywords for {ctype}")
            det_keywords = merged

        existing = self.knowledge_reader.get_by_type(ctype)
        vid = promotion_candidate.get('variant_id', f'sidecar_{ctype}')

        # ── Tier 3 Freeze: never overwrite proven winners ──
        if existing:
            variant = existing.get_active_variant()
            if variant and variant.tier >= 3 and variant.replay_successes >= 3:
                rate = variant.replay_successes / max(variant.replay_attempts, 1)
                if rate >= 0.9:
                    log_stage("sidecar", f"FREEZE: {ctype} tier-{variant.tier} at {rate:.0%} with "
                              f"{variant.replay_successes} successes — keeping existing recipe")
                    return False

        # ── Same-run overwrite guard: only replace if new recipe is longer ──
        prev_len = self._promoted_this_run.get(ctype)
        if prev_len is not None:
            new_len = len(compressed)
            if new_len <= prev_len:
                log_stage("sidecar", f"skip overwrite for {ctype}: "
                          f"new recipe ({new_len} steps) <= existing ({prev_len} steps)")
                return False

        if existing:
            variant = existing.get_active_variant()
            if variant:
                # Update existing: store recipe, reset failure counters.
                # Do NOT bump successes/attempts — those track replay stats only.
                # Sidecar discovery is a different signal than recipe replay success.
                variant.action_recipe = compressed
                variant.consecutive_failures = 0  # Fresh recipe = clean slate
                if variant.tier == 0:
                    variant.tier = 1
                    variant.recipe_ttl = 5
                if dna_sig:
                    variant.successful_dna_signature = dna_sig
                variant.assertions_present = has_stable_locator
                variant.non_replayable = is_session_specific
            # Update learning-level keywords (merge new + existing)
            merged_kw = sorted(set(existing.detection_keywords) | set(det_keywords))
            existing.detection_keywords = merged_kw
            if not variant:
                # No active variant — create one
                new_v = StrategyVariant(
                    variant_id=vid,
                    suggested_action=f"sidecar recipe ({len(compressed)} steps)",
                    action_type='sidecar_recipe',
                    action_recipe=compressed,
                    successful_dna_signature=dna_sig or {},
                    assertions_present=has_stable_locator,
                    created_by='system2',
                    confidence=wilson_score_lower(0, 0),
                    attempts=0, successes=0, verified=True,
                    tier=1, recipe_ttl=5,
                    non_replayable=is_session_specific,
                )
                existing.variants.append(new_v)
                existing.active_variant_id = vid
        else:
            variant = StrategyVariant(
                variant_id=vid,
                suggested_action=f"sidecar recipe ({len(compressed)} steps)",
                action_type='sidecar_recipe',
                action_recipe=compressed,
                successful_dna_signature=dna_sig or {},
                assertions_present=has_stable_locator,
                created_by='system2',
                confidence=wilson_score_lower(0, 0),
                attempts=0, successes=0, verified=True,
                tier=1, recipe_ttl=5,
                non_replayable=is_session_specific,
            )
            self.knowledge_reader.create_learning(
                ctype, variant, detection_keywords=det_keywords)

        # ── Capture DOM fingerprint for detection ──
        # Use step-start snapshot (pristine challenge state) instead of live page
        # (which is now step N+1 after code submission).
        snapshot_fp = step_snapshot.fingerprint if step_snapshot else {}
        if snapshot_fp:
            target_learning = self.knowledge_reader.get_by_type(ctype)
            if target_learning:
                target_learning.dom_fingerprint = snapshot_fp
        elif not step_snapshot:
            # Legacy fallback if no snapshot available
            try:
                from knowledge_reader import DOM_FINGERPRINT_JS
                live_fp = page.evaluate(DOM_FINGERPRINT_JS)
                if live_fp:
                    target_learning = self.knowledge_reader.get_by_type(ctype)
                    if target_learning:
                        target_learning.dom_fingerprint = live_fp
            except Exception:
                pass

        # ── Populate page_text_context from snapshot ──
        # Uses snapshot text_ctx (captured at step start) to feed the 15% text_ctx
        # detection weight. Without this, all recipes have empty page_text_context.
        if step_snapshot and getattr(step_snapshot, 'text_ctx', None):
            target_learning = self.knowledge_reader.get_by_type(ctype)
            if target_learning:
                self.knowledge_reader._update_type_text_context(
                    target_learning, step_snapshot.text_ctx)

        # ── Populate dom_signals from snapshot (Fix 4a) ──
        # Structural DOM features (canvas, audio, iframe, etc.) captured at step start.
        if step_snapshot and getattr(step_snapshot, 'dom_signals', None):
            target_learning = self.knowledge_reader.get_by_type(ctype)
            if target_learning and not target_learning.dom_signals:
                target_learning.dom_signals = step_snapshot.dom_signals
                log_stage("sidecar", f"dom_signals: {len(step_snapshot.dom_signals)} features "
                          f"for {ctype}")

        # ── Aggregate dna_signatures at type level (Fix 4b) ──
        if dna_sig:
            target_learning = self.knowledge_reader.get_by_type(ctype)
            if target_learning:
                self.knowledge_reader._aggregate_dna_signature(target_learning, dna_sig)

        try:
            self.knowledge_reader._save()
        except Exception as e:
            log_stage("sidecar", f"warning: knowledge_reader save failed: {e}")

        # Track this promotion for same-run overwrite guard
        self._promoted_this_run[ctype] = len(compressed)

        log_stage("sidecar", f"promoted {ctype} → tier 1: {len(compressed)} steps, "
                  f"locator={'yes' if has_stable_locator else 'no'}, "
                  f"dna={'yes' if dna_sig else 'no'}")
        return True

    # ── Keyword Generation & Session-Token Detection ─────────────────────

    @staticmethod
    def _generate_detection_keywords(ctype: str, recipe: list[dict],
                                     instruction_text: str = '') -> list[str]:
        """Auto-generate detection keywords from challenge type, recipe actions,
        and instruction text. Used at promotion time to populate detection_keywords
        on the learning, which feeds into the 0.35-weight keyword score."""
        keywords = set()

        # 1. Challenge type tokens (e.g., "drag_drop" → {"drag", "drop"})
        base_type = re.sub(r'_v\d+$', '', ctype)
        for token in base_type.split('_'):
            if len(token) >= 3:  # Skip short tokens like "v2"
                keywords.add(token.lower())

        # 2. Action types in the recipe
        ACTION_KEYWORDS = {
            'hover': ['hover'],
            'drag': ['drag', 'drop'],
            'draw': ['draw', 'canvas'],
            'canvas_draw': ['draw', 'canvas', 'stroke'],
            'scroll': ['scroll'],
            'element_scroll': ['scroll', 'container'],
            'press': ['key', 'press', 'keyboard'],
            'keyboard_sequence': ['keyboard', 'sequence', 'key'],
            'type': ['type', 'input'],
            'wait': ['wait', 'delay'],
            'wait_for_state': ['wait', 'appear'],
            'double_click': ['double', 'click'],
        }
        for step in recipe:
            at = step.get('action_type', '')
            for kw in ACTION_KEYWORDS.get(at, []):
                keywords.add(kw)

        # 3. Target text tokens from recipe steps (skip generic/short ones)
        SKIP_TEXT = {'click', 'submit', 'next', 'close', 'dismiss', 'accept',
                     'continue', 'proceed', 'here', 'button', 'ok', 'yes', 'no',
                     'challenge', 'complete', 'reveal', 'step', 'code', 'enter',
                     'advance', 'forward', 'page', 'section', 'reading',
                     'navigation', 'browser'}
        for step in recipe:
            for field in ('target_text', 'target_name'):
                text = step.get(field, '') or ''
                for word in re.findall(r'[a-zA-Z]{3,}', text.lower()):
                    if word not in SKIP_TEXT and len(word) <= 20:
                        keywords.add(word)

        # 4. Instruction text hints (top keywords from challenge instructions)
        if instruction_text:
            INSTRUCTION_KEYWORDS = {
                'canvas', 'draw', 'stroke', 'gesture', 'swipe',
                'drag', 'drop', 'slot', 'piece', 'puzzle',
                'hover', 'reveal', 'hidden',
                'scroll', 'container', 'box',
                'audio', 'listen', 'play', 'sound',
                'video', 'watch', 'frame',
                'decode', 'encoded', 'base64', 'rot13', 'cipher',
                'keyboard', 'sequence', 'press', 'key',
                'tab', 'visit', 'multi',
                'timer', 'delay', 'wait', 'seconds',
                'service', 'worker', 'offline',
                'websocket', 'connect',
                'mutation', 'observe',
                'split', 'parts', 'scattered',
                'remember', 'memorize', 'flash',
            }
            text_lower = instruction_text.lower()
            for kw in INSTRUCTION_KEYWORDS:
                if kw in text_lower:
                    keywords.add(kw)

        # Remove noise — words that appear on every challenge page or are decoy labels
        NOISE = {'', 'challenge', 'complete', 'reveal', 'step', 'code',
                 'enter', 'navigation', 'browser', 'proceed', 'continue',
                 'advance', 'forward', 'page', 'section', 'reading',
                 'button', 'click', 'submit',
                 # Decoy button labels — appear on every page, zero signal
                 'next', 'keep', 'going', 'move', 'journey', 'load',
                 'here', 'try', 'this', 'new'}
        keywords -= NOISE
        return sorted(keywords)

    @staticmethod
    def _detect_session_specific(recipe: list[dict]) -> bool:
        """Detect if a recipe contains session-specific values that won't replay.

        Checks for:
        - 6-char code-like strings in type values (e.g., "DCME15")
        - Short numeric-only type values (math answers like "34", "36")
        - 2-char code fragments in target_text (tab suffixes like "BE", "NU")
        """
        from config import CHARSET
        code_re = re.compile(r'^[A-HJ-NP-Z2-9]{6}$')  # Exact 6-char code

        for step in recipe:
            at = step.get('action_type', '')

            # Check 'type' actions for session-specific values
            if at == 'type':
                # Steps with a resolver are parameterized — not session-specific
                if step.get('resolver'):
                    continue
                val = (step.get('value') or '').strip()
                if not val:
                    continue
                # Pure numeric answer (math puzzle solutions)
                if val.isdigit() and len(val) <= 4:
                    return True
                # 6-char code pattern in typed text
                if code_re.match(val):
                    return True
                # Short uppercase + digit strings that look like code fragments
                # Require BOTH letters AND digits — pure alpha words like
                # "DECODE" are constant keywords, not session-specific
                if len(val) <= 8 and re.match(r'^[A-Z0-9]+$', val):
                    has_alpha = any(c.isalpha() for c in val)
                    has_digit = any(c.isdigit() for c in val)
                    if has_alpha and has_digit:
                        return True

            # Check target_text for per-session tab content like "Tab 1: BE"
            text = step.get('target_text', '') or ''
            # Pattern: "Tab N: XX" where XX is session code fragment
            if re.search(r'Tab \d+:\s*[A-Z0-9]{2}', text):
                return True
            # Pattern: "Frame N" where N changes per session
            if re.search(r'Frame \d+', text):
                return True
            # Pattern: bare 2-3 char uppercase code fragments (require alpha+digit mix)
            t = text.strip() if text else ''
            if t and 2 <= len(t) <= 3 and re.fullmatch(r'[A-Z0-9]{2,3}', t):
                if any(c.isalpha() for c in t) and any(c.isdigit() for c in t):
                    return True

        return False

    @staticmethod
    def _detect_non_transferable(recipe: list[dict]) -> bool:
        """Detect recipes that can't transfer across sessions (pixel-only targeting).

        If 40%+ of targeting actions use coordinates without any stable
        locator (text, name, selector, role), the recipe is inherently fragile.
        """
        coord_only_count = 0
        total_targeting = 0
        for step in recipe:
            atype = step.get('action_type', '')
            if atype in ('click', 'hover', 'drag', 'draw', 'canvas_draw',
                         'double_click', 'focus'):
                total_targeting += 1
                has_stable = bool(
                    step.get('target_text') or step.get('target_name') or
                    step.get('target_selector') or step.get('target_role')
                )
                has_coords = bool(step.get('target_coords') or step.get('dest_coords'))
                if has_coords and not has_stable:
                    coord_only_count += 1
        return total_targeting > 0 and coord_only_count / total_targeting > 0.4

    # ── Action Execution (sidecar-owned) ──────────────────────────────────

    def _execute_action(self, page, action: dict, viewport_state: dict,
                        element_catalog: list[dict] | None = None) -> dict:
        """Execute a single action. Returns hit verification info."""
        action_type = action.get('type', '').lower()
        hit_info = {'type': action_type, 'executed': False}

        # BID resolution: convert element_id to x,y coordinates
        # Frame elements use frame.locator click; main-page uses page.evaluate()
        if 'element_id' in action:
            bid = action['element_id']
            # Capture element text from catalog for recipe locator enrichment
            if element_catalog:
                for el in element_catalog:
                    if el.get('bid') == bid:
                        text = (el.get('text') or '').strip()
                        if text:
                            hit_info['hit_text'] = text[:30]
                            hit_info['hit_tag'] = el.get('tag', '').upper()
                        break
            frame_el = self._frame_elements.get(bid)
            if frame_el:
                # Frame element — try frame.locator click first (most reliable)
                frame = self._frame_refs.get(frame_el['frame_id'])
                if frame and action_type == 'click':
                    try:
                        frame.locator(f'[data-bid="{bid}"]').click(timeout=3000)
                        hit_info['executed'] = True
                        hit_info['resolved_bid'] = bid
                        hit_info['frame_id'] = frame_el['frame_id']
                        return hit_info
                    except Exception as e:
                        log_stage("sidecar",
                                  f"frame click BID {bid} failed: {e}, "
                                  f"falling back to page-space coords")
                elif frame and action_type in ('drag', 'draw', 'double_click'):
                    # For non-click frame actions, locator approach is unreliable.
                    # Log and let page-space coords handle it (may be imprecise).
                    log_stage("sidecar",
                              f"frame {action_type} BID {bid}: using page-space coords "
                              f"(frame.locator not supported for {action_type})")
                # Fallback: set page-space coords (viewport coords from enumerate_all_frames)
                action['x'] = frame_el['x']
                action['y'] = frame_el['y']
                hit_info['resolved_bid'] = bid
                hit_info['frame_id'] = frame_el['frame_id']
            else:
                # Main-page BID resolution via page.evaluate()
                try:
                    box = page.evaluate(f'''() => {{
                        const el = document.querySelector('[data-bid="{bid}"]');
                        if (!el) return null;
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 && r.height === 0) return null;
                        return {{ x: r.x, y: r.y, width: r.width, height: r.height }};
                    }}''')
                    if box:
                        action['x'] = box['x'] + box['width'] / 2
                        action['y'] = box['y'] + box['height'] / 2
                        hit_info['resolved_bid'] = bid
                        hit_info['hit_width'] = box['width']
                        hit_info['hit_height'] = box['height']
                    else:
                        log_stage("sidecar", f"BID {bid} not found, skipping")
                        return hit_info
                except Exception as e:
                    log_stage("sidecar", f"BID {bid} resolution failed: {e}")
                    return hit_info

        # Action validity gate — catch corruption before wasting execution
        from primitives import validate_action
        challenge_region = find_challenge_region(page) if element_catalog else None
        valid, reason = validate_action(action, element_catalog, challenge_region)
        if not valid:
            log_stage("sidecar", f"action rejected by validity gate: {reason}")
            return hit_info

        try:
            if action_type == 'click':
                x, y = float(action.get('x', 0)), float(action.get('y', 0))
                x = max(0, min(x, viewport_state.get('viewport_w', 1280) - 1))
                y = max(0, min(y, viewport_state.get('viewport_h', 1024) - 1))

                # Hit test BEFORE click — use frame context if this is a frame element
                frame_ctx = None
                if hit_info.get('frame_id'):
                    fid = hit_info['frame_id']
                    fe = self._frame_elements.get(action.get('element_id'))
                    frame_ctx = self._frame_refs.get(fid)
                    if frame_ctx and fe:
                        # Use frame-local coords for hit-test inside the frame
                        lx, ly = fe['local_x'], fe['local_y']
                        eval_ctx = frame_ctx
                    else:
                        eval_ctx = self._get_eval_context(page)
                        lx, ly = self._translate_coords(page, x, y)
                else:
                    eval_ctx = self._get_eval_context(page)
                    lx, ly = self._translate_coords(page, x, y)
                before_hit = eval_ctx.evaluate(f'''() => {{
                    const el = document.elementFromPoint({lx}, {ly});
                    if (!el) return null;
                    return {{
                        tag: el.tagName,
                        text: (el.textContent || '').trim().substring(0, 40),
                        classes: (el.className?.toString() || '').substring(0, 60),
                        isAnchor: !!el.closest('a[href]')
                    }};
                }}''')

                if before_hit and before_hit.get('isAnchor'):
                    log_stage("sidecar", f"blocked click at ({x},{y}) — anchor tag")
                    hit_info['blocked'] = 'anchor'
                    return hit_info

                url_before = page.url
                page.mouse.click(x, y)
                page.wait_for_timeout(100)  # React batches synchronously
                self._check_navigation(page, url_before)

                hit_info.update({
                    'executed': True, 'x': x, 'y': y,
                    'hit_tag': before_hit['tag'] if before_hit else None,
                    'hit_text': before_hit['text'][:30] if before_hit else None,
                    'hit_classes': before_hit['classes'] if before_hit else None,
                })

            elif action_type == 'hover':
                x, y = float(action.get('x', 0)), float(action.get('y', 0))
                seconds = min(float(action.get('seconds', 2)), 5)
                log_stage("sidecar", f"hover ({x},{y}) for {seconds}s")
                page.mouse.move(x, y, steps=5)
                polls = int(seconds * 4)
                for _ in range(polls):
                    page.wait_for_timeout(250)
                    code = self._harvest_code(page)
                    if code:
                        hit_info['found_code_during'] = True
                        break
                hit_info['executed'] = True

            elif action_type == 'scroll':
                direction = action.get('direction', 'down')
                amount = min(int(action.get('amount', 300)), 2000)
                if direction in ('down', 'right'):
                    delta = amount
                elif direction in ('up', 'left'):
                    delta = -amount
                else:
                    delta = amount  # default down
                h_delta = delta if direction in ('left', 'right') else 0
                v_delta = delta if direction in ('up', 'down') else 0
                scroll_eid = action.get('element_id')
                # Capture scroll position before to detect if viewport moved
                _sy_before = page.evaluate('() => window.scrollY') if scroll_eid is None else None
                # Capture element scrollTop before for element-targeted scroll
                _el_scroll_before = None
                if scroll_eid is not None:
                    _el_scroll_before = page.evaluate(f'''() => {{
                        const el = document.querySelector('[data-bid="{scroll_eid}"]');
                        return el ? el.scrollTop : null;
                    }}''')
                if scroll_eid is not None:
                    # Scroll inside a specific element (e.g., "scroll box" tasks)
                    log_stage("sidecar", f"scroll {direction} {amount}px inside BID {scroll_eid}")
                    try:
                        box = page.evaluate(f'''() => {{
                            const el = document.querySelector('[data-bid="{scroll_eid}"]');
                            if (!el) return null;
                            const r = el.getBoundingClientRect();
                            if (r.width === 0 && r.height === 0) return null;
                            return {{ x: r.x, y: r.y, width: r.width, height: r.height }};
                        }}''')
                        if box:
                            cx = box['x'] + box['width'] / 2
                            cy = box['y'] + box['height'] / 2
                            page.mouse.move(cx, cy)
                            page.mouse.wheel(h_delta, v_delta)
                            hit_info['hit_tag'] = page.evaluate(
                                f'document.querySelector(\'[data-bid="{scroll_eid}"]\')?.tagName || null'
                            )
                        else:
                            page.evaluate(f'window.scrollBy({h_delta}, {v_delta})')
                    except Exception:
                        page.evaluate(f'window.scrollBy({h_delta}, {v_delta})')
                else:
                    log_stage("sidecar", f"scroll {direction} {amount}px")
                    page.evaluate(f'window.scrollBy({h_delta}, {v_delta})')
                page.wait_for_timeout(200)  # Content settle
                # Check if page-level scroll actually moved the viewport
                if _sy_before is not None:
                    _sy_after = page.evaluate('() => window.scrollY')
                    hit_info['scroll_moved'] = abs(_sy_after - _sy_before) > 5
                elif _el_scroll_before is not None and scroll_eid is not None:
                    _el_scroll_after = page.evaluate(f'''() => {{
                        const el = document.querySelector('[data-bid="{scroll_eid}"]');
                        return el ? el.scrollTop : null;
                    }}''')
                    if _el_scroll_after is not None:
                        hit_info['scroll_moved'] = abs(_el_scroll_after - _el_scroll_before) > 5
                hit_info['executed'] = True

            elif action_type == 'type':
                text = str(action.get('text', ''))
                log_stage("sidecar", f"type '{text[:30]}'")
                page.keyboard.type(text)
                page.wait_for_timeout(150)  # Settle for input handlers
                hit_info['executed'] = True

            elif action_type == 'press':
                keys = str(action.get('keys', ''))
                keys = keys.replace('Ctrl', 'Control').replace('ctrl', 'Control')
                keys = keys.replace('cmd', 'Meta').replace('Cmd', 'Meta')
                log_stage("sidecar", f"press {keys}")
                page.keyboard.press(keys)
                page.wait_for_timeout(150)  # Input handling is synchronous
                hit_info['executed'] = True

            elif action_type == 'wait':
                seconds = min(float(action.get('seconds', 1)), 10)
                log_stage("sidecar", f"wait {seconds}s")
                page.wait_for_timeout(int(seconds * 1000))
                hit_info['executed'] = True

            elif action_type == 'drag':
                from primitives import smart_drag
                src_id = action.get('source_id')
                tgt_id = action.get('target_id')
                drag_executed = False

                # Prefer Playwright locator.drag_to() for BID-based drags (most reliable)
                if src_id is not None and tgt_id is not None:
                    try:
                        src_loc = page.locator(f'[data-bid="{src_id}"]').first
                        tgt_loc = page.locator(f'[data-bid="{tgt_id}"]').first
                        # Ensure both elements are visible and in viewport
                        src_loc.wait_for(state="visible", timeout=2000)
                        tgt_loc.wait_for(state="visible", timeout=2000)
                        src_loc.scroll_into_view_if_needed(timeout=2000)
                        tgt_loc.scroll_into_view_if_needed(timeout=2000)
                        log_stage("sidecar", f"drag BID {src_id}→{tgt_id} via drag_to()")
                        src_loc.drag_to(tgt_loc, timeout=3000, force=True)
                        page.wait_for_timeout(150)
                        drag_executed = True
                        hit_info['executed'] = True
                        hit_info['drag_result'] = {'method': 'drag_to', 'success': True}
                        hit_info['resolved_source_bid'] = src_id
                        hit_info['resolved_target_bid'] = tgt_id
                        # Capture coords for recipe storage (BIDs are session-ephemeral)
                        try:
                            src_box = src_loc.bounding_box(timeout=1000)
                            tgt_box = tgt_loc.bounding_box(timeout=1000)
                            if src_box:
                                action['x1'] = src_box['x'] + src_box['width'] / 2
                                action['y1'] = src_box['y'] + src_box['height'] / 2
                            if tgt_box:
                                action['x2'] = tgt_box['x'] + tgt_box['width'] / 2
                                action['y2'] = tgt_box['y'] + tgt_box['height'] / 2
                        except Exception:
                            pass  # Coords unavailable; recipe will be coord-only
                    except Exception as e:
                        log_stage("sidecar", f"drag_to() failed: {e}, falling back to smart_drag")

                # Fallback: resolve BIDs to coords → smart_drag (mouse-only with outcome check)
                if not drag_executed:
                    if src_id is not None or tgt_id is not None:
                        try:
                            if src_id is not None:
                                src_box = page.evaluate(f'''() => {{
                                    const el = document.querySelector('[data-bid="{src_id}"]');
                                    if (!el) return null;
                                    const r = el.getBoundingClientRect();
                                    return {{x: r.x, y: r.y, width: r.width, height: r.height}};
                                }}''')
                                if src_box:
                                    action['x1'] = src_box['x'] + src_box['width'] / 2
                                    action['y1'] = src_box['y'] + src_box['height'] / 2
                                    hit_info['resolved_source_bid'] = src_id
                                else:
                                    log_stage("sidecar",
                                              f"drag source BID {src_id} not found")
                                    return hit_info
                            if tgt_id is not None:
                                tgt_box = page.evaluate(f'''() => {{
                                    const el = document.querySelector('[data-bid="{tgt_id}"]');
                                    if (!el) return null;
                                    const r = el.getBoundingClientRect();
                                    return {{x: r.x, y: r.y, width: r.width, height: r.height}};
                                }}''')
                                if tgt_box:
                                    action['x2'] = tgt_box['x'] + tgt_box['width'] / 2
                                    action['y2'] = tgt_box['y'] + tgt_box['height'] / 2
                                    hit_info['resolved_target_bid'] = tgt_id
                                else:
                                    log_stage("sidecar",
                                              f"drag target BID {tgt_id} not found")
                                    return hit_info
                        except Exception as e:
                            log_stage("sidecar", f"drag BID resolution error: {e}")
                            return hit_info

                    # Validate both endpoints exist and aren't near-origin
                    raw_x1 = action.get('x1')
                    raw_y1 = action.get('y1')
                    raw_x2 = action.get('x2')
                    raw_y2 = action.get('y2')
                    missing_src = raw_x1 is None or raw_y1 is None
                    missing_dst = raw_x2 is None or raw_y2 is None
                    if missing_src or missing_dst:
                        which = ('source' if missing_src else '') + \
                                (' + ' if missing_src and missing_dst else '') + \
                                ('destination' if missing_dst else '')
                        log_stage("sidecar", f"drag: missing {which} endpoint — "
                                  f"src=({raw_x1},{raw_y1}) dst=({raw_x2},{raw_y2})")
                        return hit_info
                    x1, y1 = float(raw_x1), float(raw_y1)
                    x2, y2 = float(raw_x2), float(raw_y2)
                    if (abs(x1) < 2 and abs(y1) < 2) or (abs(x2) < 2 and abs(y2) < 2):
                        log_stage("sidecar", f"drag: rejecting near-origin coords — "
                                  f"src=({x1},{y1}) dst=({x2},{y2})")
                        return hit_info

                    # Clamp raw coords to canvas bounds — find nearest canvas to source
                    if element_catalog:
                        best_canvas = None
                        best_dist = float('inf')
                        for el in element_catalog:
                            cb = el.get('canvas_bounds')
                            if cb:
                                # Pick the canvas closest to the drag source
                                cx = (cb['left'] + cb['right']) / 2
                                cy = (cb['top'] + cb['bottom']) / 2
                                dist = ((x1 - cx) ** 2 + (y1 - cy) ** 2) ** 0.5
                                if dist < best_dist:
                                    best_dist = dist
                                    best_canvas = cb
                        if best_canvas:
                            cb = best_canvas
                            ox1, oy1, ox2, oy2 = x1, y1, x2, y2
                            x1 = max(cb['left'] + 2, min(x1, cb['right'] - 2))
                            y1 = max(cb['top'] + 2, min(y1, cb['bottom'] - 2))
                            x2 = max(cb['left'] + 2, min(x2, cb['right'] - 2))
                            y2 = max(cb['top'] + 2, min(y2, cb['bottom'] - 2))
                            if (ox1, oy1, ox2, oy2) != (x1, y1, x2, y2):
                                log_stage("sidecar",
                                          f"clamped drag to canvas bounds "
                                          f"({ox1},{oy1})->({ox2},{oy2}) => "
                                          f"({x1},{y1})->({x2},{y2})")

                    log_stage("sidecar", f"drag ({x1},{y1}) -> ({x2},{y2})"
                              + (f" [BID {src_id}→{tgt_id}]" if src_id is not None else ""))
                    drag_result = smart_drag(page, x1, y1, x2, y2)
                    hit_info['executed'] = True
                    hit_info['drag_result'] = drag_result

                # Per-drag inline progress check
                drag_progress = self._read_inline_progress(page)
                if drag_progress:
                    hit_info['inline_progress'] = drag_progress
                    log_stage("sidecar", f"  drag progress: {drag_progress}")

            elif action_type == 'double_click':
                x, y = float(action.get('x', 0)), float(action.get('y', 0))
                x = max(0, min(x, viewport_state.get('viewport_w', 1280) - 1))
                y = max(0, min(y, viewport_state.get('viewport_h', 1024) - 1))
                log_stage("sidecar", f"double_click ({x},{y})")
                page.mouse.dblclick(x, y)
                hit_info['executed'] = True

            elif action_type == 'focus':
                bid = action.get('element_id')
                if bid is not None:
                    try:
                        page.locator(f'[data-bid="{bid}"]').first.click(timeout=2000)
                        page.wait_for_timeout(50)
                        hit_info['executed'] = True
                        log_stage("sidecar", f"focus BID {bid}")
                    except Exception as e:
                        log_stage("sidecar", f"focus BID {bid} failed: {e}")
                else:
                    x, y = float(action.get('x', 0)), float(action.get('y', 0))
                    page.mouse.click(x, y)
                    page.wait_for_timeout(50)
                    hit_info['executed'] = True
                    log_stage("sidecar", f"focus ({x},{y})")

            elif action_type in ('draw', 'canvas_draw'):
                from primitives import draw_stroke_on_canvas
                path = action.get('path', [])
                bid = action.get('element_id')
                canvas_ref = bid if bid is not None else 'canvas'
                log_stage("sidecar",
                          f"draw {len(path)} points on "
                          f"{'BID ' + str(bid) if bid else 'canvas'}")
                result = draw_stroke_on_canvas(page, canvas_ref, path)
                hit_info['executed'] = result.get('success', False)
                hit_info['draw_result'] = result
                if result.get('progress_changed'):
                    log_stage("sidecar", f"  draw progress: {result.get('progress_after')}")

            elif action_type == 'element_scroll':
                # React-compatible container scroll via shared primitive
                from primitives import scroll_container_js
                bid = action.get('element_id')
                direction = action.get('direction', 'down')
                amount = int(action.get('amount', 300))
                result = scroll_container_js(page, bid=bid, direction=direction, amount=amount)
                if result.get('success'):
                    page.wait_for_timeout(200)
                    hit_info['executed'] = True
                    hit_info['scroll_moved'] = result.get('moved', False)
                    log_stage("sidecar",
                              f"element_scroll {direction} {amount}px "
                              f"(moved={result.get('moved')}, "
                              f"tag={result.get('tag')}, "
                              f"pos={result.get('scrollTop')}/{result.get('scrollHeight')})")
                else:
                    # Fallback: mouse.wheel if JS scroll found no container
                    if bid is not None and element_catalog:
                        el = next((e for e in element_catalog if e.get('bid') == bid), None)
                        if el:
                            # el['x']/el['y'] are already center coords
                            cx = el['x']
                            cy = el['y']
                            page.mouse.move(cx, cy)
                            v_delta = amount if direction == 'down' else -amount
                            page.mouse.wheel(0, v_delta)
                            page.wait_for_timeout(200)
                            hit_info['executed'] = True
                            hit_info['scroll_moved'] = True  # wheel was dispatched
                            log_stage("sidecar", f"element_scroll fallback wheel BID {bid} {direction}")
                        else:
                            log_stage("sidecar", f"element_scroll BID {bid} not found")
                    elif action.get('x') is not None and action.get('y') is not None:
                        x, y = float(action['x']), float(action['y'])
                        page.mouse.move(x, y)
                        v_delta = amount if direction == 'down' else -amount
                        page.mouse.wheel(0, v_delta)
                        page.wait_for_timeout(200)
                        hit_info['executed'] = True
                        hit_info['scroll_moved'] = True  # wheel was dispatched
                        log_stage("sidecar", f"element_scroll fallback wheel ({x},{y}) {direction}")
                    else:
                        log_stage("sidecar",
                                  f"element_scroll: no scrollable container found "
                                  f"({result.get('reason', 'unknown')})")

            elif action_type == 'wait_for_state':
                from primitives import wait_for_state
                change_types = set(action.get('change_types',
                                              ['enabled', 'appeared', 'new_element',
                                               'became_clickable', 'activated']))
                timeout = min(int(action.get('timeout_ms', 3000)), 5000)
                match_text = action.get('match_text')
                log_stage("sidecar",
                          f"wait_for_state types={change_types} timeout={timeout}ms")
                result = wait_for_state(
                    page, change_types=change_types, timeout_ms=timeout,
                    match_text=match_text,
                    element_catalog=element_catalog,
                )
                hit_info['executed'] = True
                if result:
                    hit_info['state_change'] = result
                    bid = result.get('matched_bid')
                    if bid is not None:
                        hit_info['next_target_bid'] = bid
                        log_stage("sidecar",
                                  f"  state change detected: "
                                  f"{','.join(result.get('changes',[]))} "
                                  f"on BID {bid} \"{result.get('text','')}\"")
                    else:
                        log_stage("sidecar",
                                  f"  state change detected: "
                                  f"{','.join(result.get('changes',[]))} "
                                  f"(no BID match)")
                else:
                    log_stage("sidecar", "  wait_for_state: timeout, no matching change")

            else:
                log_stage("sidecar", f"unknown action type: {action_type}")

        except Exception as e:
            log_stage("sidecar", f"action error ({action_type}): {e}")
            hit_info['error'] = str(e)

        return hit_info

    # ── Inline Progress (Fix C) ──────────────────────────────────────────

    @staticmethod
    def _read_inline_progress(page) -> str | None:
        """Parse structured progress from page text: '2/6 filled', 'Strokes: 1/3', etc.

        Returns a compact string like '2/6' or None if no progress pattern found.
        Used for per-action feedback so the planner knows which drags worked.
        """
        try:
            return page.evaluate(r'''() => {
                const text = (document.body?.innerText || '').substring(0, 3000);
                // Match patterns: "N/M filled", "N/M found", "N/M complete",
                // "Strokes: N/M", "Progress: N/M", "(N/M)"
                const patterns = [
                    /(\d+)\/(\d+)\s*filled/i,
                    /(\d+)\/(\d+)\s*found/i,
                    /(\d+)\/(\d+)\s*complete/i,
                    /strokes?:\s*(\d+)\/(\d+)/i,
                    /progress:\s*(\d+)\/(\d+)/i,
                    /\((\d+)\/(\d+)\)/,
                ];
                for (const p of patterns) {
                    const m = text.match(p);
                    if (m) return m[1] + '/' + m[2];
                }
                return null;
            }''')
        except Exception:
            return None

    # ── Frame Handling ────────────────────────────────────────────────────

    def _handle_switch_frame(self, page, action: dict):
        """Switch active frame for JS eval context."""
        frame_val = action.get('frame', 'main')
        if frame_val == 'main':
            self._active_frame = None
            self._active_frame_index = None
            log_stage("sidecar", "switched to main frame")
        elif isinstance(frame_val, str) and frame_val.startswith('index:'):
            try:
                idx = int(frame_val.split(':')[1])
                iframes = page.frames[1:]  # Skip main frame
                if idx < len(iframes):
                    self._active_frame = iframes[idx]
                    self._active_frame_index = idx
                    log_stage("sidecar", f"switched to frame index {idx}")
                else:
                    log_stage("sidecar", f"frame index {idx} out of range ({len(iframes)} frames)")
            except (ValueError, IndexError) as e:
                log_stage("sidecar", f"frame switch error: {e}")

    def _get_eval_context(self, page):
        """Get the appropriate JS eval context (frame or main page)."""
        return self._active_frame if self._active_frame else page

    def _translate_coords(self, page, x: float, y: float) -> tuple[float, float]:
        """Translate main-page coords to frame-local coords if in a frame."""
        if not self._active_frame or self._active_frame_index is None:
            return x, y
        try:
            iframe_rect = page.evaluate('''(idx) => {
                const frames = document.querySelectorAll('iframe');
                if (idx < frames.length) {
                    const r = frames[idx].getBoundingClientRect();
                    return {x: r.x, y: r.y};
                }
                return {x: 0, y: 0};
            }''', self._active_frame_index)
            return x - iframe_rect['x'], y - iframe_rect['y']
        except Exception:
            return x, y

    def _frame_sweep(self, page, step: int, version: int,
                     context: dict, history: list) -> str | None:
        """Try each iframe for codes when stalled."""
        log_stage("sidecar", "frame sweep: checking all iframes")
        try:
            frames = page.frames[1:]  # Skip main
            for idx, frame in enumerate(frames):
                try:
                    codes_raw = frame.evaluate(
                        "() => window.__getAllCodes ? window.__getAllCodes() : []"
                    ) or []
                    for c in (codes_raw if isinstance(codes_raw, list) else []):
                        code = c.get('c', c) if isinstance(c, dict) else str(c)
                        if self._validate_code(page, code):
                            log_stage("sidecar", f"frame sweep: found code {code} in frame {idx}")
                            self._code_source = f'frame_sweep_{idx}'
                            return code.upper()
                except Exception:
                    pass

                # Also try extract_code_js equivalent in frame
                try:
                    from config import CHARSET
                    code = frame.evaluate(f'''() => {{
                        const pattern = new RegExp('[{CHARSET}]{{6}}', 'g');
                        const text = document.body?.innerText || '';
                        const matches = text.match(pattern) || [];
                        for (const m of matches) {{
                            if (m === m.toUpperCase()) return m;
                        }}
                        return null;
                    }}''')
                    if code and self._validate_code(page, code):
                        log_stage("sidecar", f"frame sweep: extracted code {code} from frame {idx}")
                        self._code_source = f'frame_sweep_{idx}'
                        return code.upper()
                except Exception:
                    pass
        except Exception as e:
            log_stage("sidecar", f"frame sweep error: {e}")

        self._active_frame = None
        self._active_frame_index = None
        return None

    # ── Clickable Sweep (stall / rejection nudge) ─────────────────────────

    def _completion_sweep(self, page) -> str | None:
        """Click 'Reveal Code' / 'Complete' button after progress reaches 100%.

        Scans for buttons containing completion keywords, clicks the best match,
        waits briefly, then harvests the code.
        """
        try:
            clicked = page.evaluate(r'''() => {
                const KW = ['reveal code', 'reveal', 'complete challenge', 'complete',
                    'all tabs visited', 'show code', 'finish', 'get code', 'done'];
                const SKIP = /^(next|continue|proceed|advance|click here|move on|keep going|go forward)/i;
                for (const btn of document.querySelectorAll('button, [role="button"]')) {
                    const t = (btn.textContent || '').trim();
                    const tl = t.toLowerCase();
                    if (btn.disabled) continue;
                    if (SKIP.test(t)) continue;
                    const r = btn.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) continue;
                    for (const kw of KW) {
                        if (tl.includes(kw)) {
                            btn.click();
                            return t;
                        }
                    }
                }
                return null;
            }''')
            if clicked:
                log_stage("sidecar", f"completion sweep clicked: '{clicked}'")
                page.wait_for_timeout(400)  # Reduced from 800ms
                self._dismiss_popups(page)
                code = self._harvest_code(page)
                if code:
                    log_stage("sidecar", f"completion sweep found code: {code}")
                    return code
                # Try once more with shorter wait
                page.wait_for_timeout(250)  # Reduced from 500ms
                code = self._harvest_code(page)
                if code:
                    log_stage("sidecar", f"completion sweep found code (2nd try): {code}")
                    return code
                log_stage("sidecar", "completion sweep: clicked but no code found")
            else:
                log_stage("sidecar", "completion sweep: no completion button found")
        except Exception as e:
            log_stage("sidecar", f"completion sweep error: {e}")
        return None

    def _clickable_sweep(self, page, already_clicked: set,
                         budget: int = 3,
                         action_log: list | None = None) -> str | None:
        """Click top interactive non-anchor elements to force state changes.

        Returns found code or None.  Budget limits total clicks.
        After each click, re-enumerates candidates and stops early on
        progress signals (new candidates, dom_change >= 0.05, code appears).
        If action_log is provided, appends sweep click entries for promotion.
        """
        log_stage("sidecar", f"clickable sweep (budget={budget})")
        dom_sig_before = self._get_dom_signature(page)

        try:
            clickables = page.evaluate(r'''() => {
                const results = [];
                const sels = 'button, [role="button"], [data-testid], [tabindex]:not(a):not(input)';
                // Pink gradient decoy buttons appear on every page — always skip
                const DECOY_RE = /^(next|next step|next page|next section|next part|continue|continue journey|continue reading|proceed|proceed forward|advance|move on|keep going|go forward|click here|submit|submit code)$/i;
                for (const el of document.querySelectorAll(sels)) {
                    if (el.closest('a[href]')) continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) continue;
                    if (rect.bottom < 0 || rect.top > window.innerHeight) continue;
                    if (rect.width > 600 || rect.height > 600) continue;
                    const text = (el.textContent || '').trim().substring(0, 50);
                    if (DECOY_RE.test(text)) continue;
                    const sig = el.tagName + ':' +
                                (el.dataset?.testid || '') + ':' +
                                text.substring(0, 20);
                    results.push({
                        x: Math.round(rect.x + rect.width / 2),
                        y: Math.round(rect.y + rect.height / 2),
                        w: Math.round(rect.width), text, sig,
                    });
                }
                results.sort((a, b) => {
                    const aM = (a.w >= 80 && a.w <= 300) ? 1 : 0;
                    const bM = (b.w >= 80 && b.w <= 300) ? 1 : 0;
                    if (aM !== bM) return bM - aM;
                    return a.y - b.y;
                });
                return results;
            }''')
        except Exception:
            clickables = []

        codes_before = self._harvest_codes_set(page)
        clicked = 0
        for el in (clickables or []):
            if clicked >= budget:
                break
            sig = el['sig']
            if sig in already_clicked:
                continue

            x, y = el['x'], el['y']
            log_stage("sidecar",
                      f"sweep click ({x},{y}) '{el['text'][:25]}'")

            # Locator cascade for promotion recipe
            locator_info = get_locator_cascade(page, x, y)

            url_before = page.url
            page.mouse.click(x, y)
            page.wait_for_timeout(200)  # Reduced from 350ms
            self._check_navigation(page, url_before)
            already_clicked.add(sig)
            clicked += 1

            # Record sweep click in action_log for promotion
            if action_log is not None:
                action_log.append({
                    'round': -1,
                    'action': {'type': 'click', 'x': x, 'y': y},
                    'hit': {'type': 'click', 'executed': True,
                            'hit_tag': 'BUTTON', 'hit_text': el['text'][:30]},
                    'locator': locator_info,
                    'hit_tag': 'BUTTON',
                    'dom_change_score': 0.0,
                    'found_code': False,
                    '_sweep': True,
                })

            code = self._harvest_code(page)
            if code:
                log_stage("sidecar", f"sweep found code: {code}")
                if action_log is not None and action_log:
                    action_log[-1]['found_code'] = True
                return code

            # Progress-signal early stop: dom change + new candidates
            dom_sig_after = self._get_dom_signature(page)
            dom_change = self._compute_change_score(dom_sig_before, dom_sig_after)
            new_codes = self._harvest_codes_set(page) - codes_before
            if dom_change >= 0.05 or new_codes:
                log_stage("sidecar",
                          f"sweep early-stop: dom_change={dom_change:.2f}, "
                          f"new_codes={len(new_codes)}")
                for nc in new_codes:
                    if nc not in self._rejected_codes and self._validate_code(page, nc):
                        log_stage("sidecar", f"sweep found new code: {nc}")
                        return nc
                break  # stop clicking, let planner analyze new state

            dom_sig_before = dom_sig_after

        if clicked > 0:
            log_stage("sidecar",
                      f"sweep: {clicked} clicks, no code found")
        return None

    # ── Code Harvesting (unified validation) ──────────────────────────────

    def _validate_code(self, page, code: str) -> bool:
        """Validate code: charset regex + JS oracle."""
        if not code:
            return False
        code = code.upper()
        if not CHARSET_RE.match(code):
            return False
        try:
            return page.evaluate(
                f"() => !window.__isValidCode || window.__isValidCode('{code}')"
            )
        except Exception:
            return CHARSET_RE.match(code) is not None

    def _harvest_code(self, page) -> str | None:
        """Three-layer harvest with unified validation. Skips rejected codes."""
        # 1. Observers
        try:
            all_codes = page.evaluate(
                "() => window.__getAllCodes ? window.__getAllCodes() : []"
            ) or []
            if isinstance(all_codes, dict):
                items = all_codes.get('bus', []) + all_codes.get('mut', [])
            else:
                items = all_codes
            for item in items:
                c = item.get('c', item) if isinstance(item, dict) else str(item)
                if (self._validate_code(page, c)
                        and c.upper() not in self._rejected_codes):
                    self._code_source = 'observer'
                    return c.upper()
        except Exception:
            pass

        # 2. JS extraction
        code = extract_code_js(page)
        if (code and self._validate_code(page, code)
                and code.upper() not in self._rejected_codes):
            self._code_source = 'extract_js'
            return code.upper()

        # 3. Harvest and score
        try:
            score, code = harvest_and_score(page, '', 0)
            if (code and score >= 0.4 and self._validate_code(page, code)
                    and code.upper() not in self._rejected_codes):
                self._code_source = 'harvest_score'
                return code.upper()
        except Exception:
            pass

        return None

    def _frame_harvest(self, page) -> str | None:
        """Lightweight code scan inside all iframes. Only runs when main harvest fails."""
        from config import CHARSET
        try:
            for frame in page.frames[1:]:
                try:
                    code = frame.evaluate(f'''() => {{
                        const pattern = new RegExp('[{CHARSET}]{{6}}', 'g');
                        const text = document.body?.innerText || '';
                        const matches = text.match(pattern) || [];
                        for (const m of matches) {{
                            if (m === m.toUpperCase()) return m;
                        }}
                        return null;
                    }}''')
                    if code and self._validate_code(page, code) \
                            and code.upper() not in self._rejected_codes:
                        self._code_source = 'frame_harvest'
                        return code.upper()
                except Exception:
                    pass
        except Exception:
            pass
        return None

    def _harvest_codes_set(self, page) -> set:
        """Harvest ALL valid codes (not just first)."""
        codes = set()
        try:
            raw = page.evaluate(
                "() => window.__getAllCodes ? window.__getAllCodes() : []"
            ) or []
            if isinstance(raw, dict):
                items = raw.get('bus', []) + raw.get('mut', [])
            else:
                items = raw
            for item in items:
                c = item.get('c', item) if isinstance(item, dict) else str(item)
                if self._validate_code(page, c):
                    codes.add(c.upper())
        except Exception:
            pass

        code = extract_code_js(page)
        if code and self._validate_code(page, code):
            codes.add(code.upper())

        return codes

    # ── Rejection Tracking (ephemeral, per-step only) ─────────────────────

    def note_rejection(self, code: str, candidate_meta: dict = None) -> None:
        """Remember a rejected code for this step only. Never persisted."""
        self._rejected_codes.add(code.upper())
        if candidate_meta:
            source = candidate_meta.get('source', '')
            evidence_kind = ''
            evidence = candidate_meta.get('evidence')
            if isinstance(evidence, dict):
                evidence_kind = evidence.get('kind', '')
            if source:
                self._rejected_signatures.add((source, evidence_kind))

    def clear_soft_rejections_on_progress(self) -> None:
        """Clear source-signature rejections when progress happens.

        Keeps exact code blacklist (never re-submit same literal).
        Clears source signatures so codes from same source type can be tried.
        """
        if self._rejected_signatures:
            log_stage("sidecar",
                      f"clearing {len(self._rejected_signatures)} soft rejections "
                      "(progress detected)")
            self._rejected_signatures.clear()

    # ── Candidate Collection & Ranking ────────────────────────────────────

    def _harvest_all_candidates(self, page, baseline_codes: set,
                                last_dom_change: float = 0.0,
                                last_progress_delta: float | None = None,
                                ) -> list[dict]:
        """Harvest ALL valid, non-rejected codes with provenance and ranking.

        Uses enumerate_code_candidates() for context-aware scanning
        (instruction-zone detection, positive labels, bbox metadata),
        then overlays sidecar-specific signals:
        - Hard boost for observer/bus/mutation codes
        - DOM change and progress delta bonuses
        - Post-rejection: extra instruction-zone penalty
        - Stability penalty: instruction-zone codes seen across rounds
        - Source-signature rejection penalty
        """
        raw = enumerate_code_candidates(page, include_frames=True)
        candidates = []
        has_rejections = len(self._rejected_codes) > 0
        current_instr_codes = set()

        for c in raw:
            code = c['code']
            if code in self._rejected_codes:
                continue
            if not self._validate_code(page, code):
                continue

            # Start from enumerate's context-aware score
            score = c['score']
            source = c.get('source', '')

            # Hard boost: observer/bus/mutation codes bypass DOM noise
            if source in ('codebus', 'mutation'):
                score += 0.2

            # Sidecar overlay: DOM change after actions
            if last_dom_change >= 0.05:
                score += 0.1

            # Sidecar overlay: progress delta
            if last_progress_delta is not None and last_progress_delta > 0:
                score += 0.1

            # Post-rejection: aggressive instruction-zone penalty
            if has_rejections and c.get('is_in_instruction_zone'):
                score -= 0.2

            # Stability penalty: same instruction-zone code seen in prev harvest
            if c.get('is_in_instruction_zone'):
                current_instr_codes.add(code)
                if code in self._stale_instr_candidates:
                    score -= 0.15

            # Source-signature rejection penalty
            for sig in self._rejected_signatures:
                if sig[0] == source:
                    score -= 0.3
                    break

            candidates.append({
                'code': code,
                'score': max(0.0, min(1.0, score)),
                'source': source,
                'evidence': {'kind': source},
                'seen_after_baseline': c.get('appeared_after_baseline', False),
                'dom_change_score': last_dom_change,
                'progress_delta': last_progress_delta,
                'text_context': c.get('text_context', ''),
                'is_in_instruction_zone': c.get('is_in_instruction_zone', False),
                'selector': c.get('selector'),
                'bbox': c.get('bbox'),
            })

        self._stale_instr_candidates = current_instr_codes
        candidates.sort(key=lambda x: x['score'], reverse=True)
        return candidates

    def _finalize_code_return(self, found_code: str, source: str, page,
                              baseline_codes: set, action_log: list,
                              round_num: int, total_actions: int,
                              step: int, version: int, context: dict,
                              iframe_count: int,
                              last_dom_change: float = 0.0,
                              last_progress_delta: float | None = None,
                              ) -> dict:
        """Build return dict with ranked candidates when a code is found."""
        self._code_source = source
        code = found_code.upper()

        # Collect all candidates from page
        candidates = self._harvest_all_candidates(
            page, baseline_codes, last_dom_change, last_progress_delta
        )

        # Ensure the found code is in candidates (at top if not already)
        if not any(c['code'] == code for c in candidates):
            candidates.insert(0, {
                'code': code, 'score': 0.9, 'source': source,
                'evidence': {}, 'seen_after_baseline': code not in baseline_codes,
                'dom_change_score': last_dom_change,
                'progress_delta': last_progress_delta,
            })

        # Log top candidates with instruction-zone context
        if candidates:
            top5 = candidates[:5]
            log_stage("sidecar", "candidates: " + ", ".join(
                f"{c['code']}({c['source']},{c['score']:.2f},"
                f"new={c['seen_after_baseline']},"
                f"instr={c.get('is_in_instruction_zone', '?')})"
                for c in top5
            ))
            # Detailed context for top candidate
            top = candidates[0]
            if top.get('text_context'):
                log_stage("sidecar",
                          f"top ctx: \"{top['text_context'][:60]}\" "
                          f"sel={top.get('selector', '?')}")

        promo = self._build_promotion_candidate(
            action_log, code, step, version, context, page
        )

        return self._make_result(
            True, code, round_num + 1, total_actions,
            'code_found', action_log, promo, iframe_count,
            candidates=candidates,
        )

    # ── DOM Signature (lightweight, 2-level) ──────────────────────────────

    def _get_dom_signature(self, page) -> str:
        """Lightweight DOM signature: top-level tag counts + text length."""
        try:
            ctx = self._get_eval_context(page)
            return ctx.evaluate(r'''() => {
                const tags = {};
                const els = document.querySelectorAll(
                    'body > *:not(script):not(style), body > * > *:not(script):not(style)'
                );
                els.forEach(el => { tags[el.tagName] = (tags[el.tagName] || 0) + 1; });
                const textLen = (document.body?.innerText || '').length;
                const keys = Object.keys(tags).sort();
                return keys.map(k => k + ':' + tags[k]).join(',') + '|' + textLen;
            }''')
        except Exception:
            return ''

    def _compute_change_score(self, before: str, after: str) -> float:
        """Quick structural change score 0.0-1.0 (copied from base.py)."""
        if before == after:
            return 0.0
        if not before or not after:
            return 1.0
        try:
            b_parts = before.split('|')
            a_parts = after.split('|')
            b_tags = dict(p.split(':') for p in b_parts[0].split(',') if ':' in p) if b_parts[0] else {}
            a_tags = dict(p.split(':') for p in a_parts[0].split(',') if ':' in p) if a_parts[0] else {}
            b_len = int(b_parts[1]) if len(b_parts) > 1 else 0
            a_len = int(a_parts[1]) if len(a_parts) > 1 else 0

            all_tags = set(b_tags) | set(a_tags)
            if all_tags:
                tag_diff = sum(
                    abs(int(b_tags.get(t, 0)) - int(a_tags.get(t, 0)))
                    for t in all_tags
                )
                tag_total = (
                    sum(int(b_tags.get(t, 0)) for t in all_tags)
                    + sum(int(a_tags.get(t, 0)) for t in all_tags)
                )
                tag_score = tag_diff / max(tag_total, 1)
            else:
                tag_score = 0.0

            len_score = abs(a_len - b_len) / max(a_len, b_len, 1)
            return min(1.0, tag_score * 0.6 + len_score * 0.4)
        except Exception:
            return 0.5

    # ── Clickable Inventory Hash (Upgrade A) ──────────────────────────────

    def _get_body_text_fingerprint(self, page) -> str:
        """Hash of the first ~1500 chars of visible body text.

        Cheap progress signal for actions that change page content without
        changing DOM structure (e.g., drag-and-drop filling slots changes
        '0/6 filled' to '3/6 filled' but doesn't add/remove elements).
        """
        try:
            return page.evaluate(r'''() => {
                const text = (document.body?.innerText || '').substring(0, 2000);
                // Hash every char for sensitivity to single-digit changes
                // (e.g. "3 more times" → "2 more times")
                let hash = text.length;
                for (let i = 0; i < text.length; i++) {
                    hash = ((hash << 5) - hash + text.charCodeAt(i)) | 0;
                }
                return String(hash);
            }''')
        except Exception:
            return ''

    def _get_clickable_hash(self, page) -> str:
        """Hash of visible interactive elements — cheap progress signal."""
        try:
            return page.evaluate(r'''() => {
                const items = [];
                for (const el of document.querySelectorAll(
                    'button, [role="button"], [data-testid], input, select, textarea'
                )) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0 &&
                        r.top < window.innerHeight && r.bottom > 0) {
                        items.push(
                            el.tagName + ':' +
                            (el.textContent || '').trim().substring(0, 20)
                        );
                    }
                }
                return items.sort().join('|');
            }''')
        except Exception:
            return ''

    # ── Post-Action Settle (Upgrade B) ────────────────────────────────────

    def _wait_for_settle(self, page, max_ms: int = 600) -> None:
        """Wait for React rendering to stabilize via RAF text-length check."""
        try:
            page.evaluate(f'''() => new Promise(resolve => {{
                let last = 0, stable = 0;
                const deadline = Date.now() + {max_ms};
                function tick() {{
                    const h = (document.body?.innerText || '').length;
                    if (h === last) stable++;
                    else stable = 0;
                    last = h;
                    if (stable >= 3) resolve(true);
                    else if (Date.now() > deadline) resolve(false);
                    else requestAnimationFrame(tick);
                }}
                requestAnimationFrame(tick);
            }})''')
        except Exception:
            pass

    # ── Progress Binding (Change #4) ──────────────────────────────────────

    def _read_bound_progress(self, page) -> dict | None:
        """Read progress with binding: first read anchors total, subsequent must match."""
        raw = read_progress(page)
        if raw is None:
            return None

        if self._progress_binding is None:
            self._progress_binding = {
                'total': raw['total'],
                'text_prefix': raw['text'][:10],
            }
            return raw

        if raw['total'] != self._progress_binding['total']:
            return None  # Different denominator — ignore

        return raw

    # ── Viewport capture ──────────────────────────────────────────────────

    def _capture_viewport_state(self, page) -> dict:
        """Capture viewport geometry."""
        try:
            return page.evaluate('''() => ({
                scroll_x: Math.round(window.scrollX),
                scroll_y: Math.round(window.scrollY),
                viewport_w: window.innerWidth,
                viewport_h: window.innerHeight,
                dpr: window.devicePixelRatio || 1
            })''')
        except Exception:
            return {'scroll_x': 0, 'scroll_y': 0, 'viewport_w': 1280, 'viewport_h': 1024, 'dpr': 1}

    # ── Popup Dismissal ───────────────────────────────────────────────────

    def _dismiss_popups(self, page):
        """Dismiss popups using shared popup module + clear orange blockers."""
        from agents.popup import dismiss_all_popups, CLEAR_BLOCKERS_JS
        try:
            for _ in range(8):
                if not dismiss_all_popups(page):
                    break
            page.evaluate(CLEAR_BLOCKERS_JS)
        except Exception:
            pass

    # ── Navigation guard ──────────────────────────────────────────────────

    @staticmethod
    def _check_navigation(page, url_before: str):
        """If we accidentally navigated away, go back."""
        url_after = page.url
        if url_after == url_before:
            return
        if '/step' in url_after and 'version=' in url_after:
            return
        log_stage("sidecar", f"navigation away detected: {url_after[:60]}")
        # Don't use go_back() — SPA risk. Sidecar will terminate.

    # ── Promotion Candidate Builder ───────────────────────────────────────

    def _build_promotion_candidate(self, action_log: list, code: str,
                                   step: int, version: int,
                                   context: dict, page) -> dict:
        """Build a promotion candidate with recipe steps, assertions, and guards."""
        from config import get_challenge_type
        from knowledge_reader import detect_challenge_type

        # Use config type when specific (steps 16+), but for generic types
        # ("simple" for steps 1-15), detect from page text so recipes get
        # promoted under their actual challenge type (scroll, hover, etc.)
        #
        # CRITICAL: page is on step N+1 by the time this runs.
        # Must use step_snapshot or context hint — NEVER live page.evaluate().
        GENERIC_TYPES = {'simple', 'unknown', 'default', 'basic'}
        config_type = get_challenge_type(step, version)
        if config_type in GENERIC_TYPES:
            # Priority 1: orchestrator's pre-advance detection (most reliable)
            ctx_hint = context.get('detected_challenge_type')
            if ctx_hint:
                ctype = f"{ctx_hint}_v{version}"
            else:
                # Priority 2: step snapshot instruction text (pristine)
                step_snap = getattr(self, '_step_snapshot', None)
                snap_text = getattr(step_snap, 'instruction_text', '') if step_snap else ''
                if snap_text:
                    detected = detect_challenge_type(snap_text)
                else:
                    detected = None
                if detected:
                    ctype = f"{detected}_v{version}"
                else:
                    ctype = f"{config_type}_v{version}"
        else:
            ctype = f"{config_type}_v{version}"

        recipe_steps = []

        # ── Causal Filter ─────────────────────────────────────────────
        # STRICT: only keep steps that produced a POSITIVE signal:
        #   - Progress increased
        #   - Positive state change (green, vibrant, clickable, enabled, revealed)
        #   - Code found
        #   - Intentional text entry (type, press, keyboard_sequence)
        #   - Scroll that actually moved content (scroll_moved == True)
        # Everything else is noise and gets dropped.
        _POSITIVE_SIGNALS = {
            'turned_green', 'became_vibrant', 'became_clickable',
            'enabled', 'appeared', 'revealed', 'became_interactive',
            'new_element', 'activated',
        }
        # Targetability signals: changes that make the NEXT action possible
        # (distinct from action-result signals like turned_green)
        _TARGETABILITY_SIGNALS = {
            'became_clickable', 'enabled', 'appeared', 'revealed',
        }

        def _has_positive_signal(entry_) -> bool:
            """Does this action log entry have a positive observable effect?"""
            # Progress increased
            pb_ = entry_.get('progress_before')
            pa_ = entry_.get('progress_after')
            if pb_ is not None and pa_ is not None and pa_ > pb_:
                return True
            # Positive state change (green, vibrant, clickable, etc.)
            for sc_ in (entry_.get('post_state_changes') or []):
                for ch_ in sc_.get('changes', []):
                    if ch_ in _POSITIVE_SIGNALS:
                        return True
            return False

        def _get_positive_changes(entry_) -> list[str]:
            """Extract positive state change names from an entry."""
            changes = []
            for sc_ in (entry_.get('post_state_changes') or []):
                for ch_ in sc_.get('changes', []):
                    if ch_ in _POSITIVE_SIGNALS and ch_ not in changes:
                        changes.append(ch_)
            return changes

        def _get_targetability_changes(entry_) -> list[str]:
            """Extract targetability-related changes (setup for next action)."""
            changes = []
            for sc_ in (entry_.get('post_state_changes') or []):
                for ch_ in sc_.get('changes', []):
                    if ch_ in _TARGETABILITY_SIGNALS and ch_ not in changes:
                        changes.append(ch_)
            return changes

        # Popup dismiss labels — these clicks are handled by the auto-dismiss
        # handler and should never appear in recipes. They produce positive
        # signals (underlying elements become visible) but are not challenge
        # actions.
        _POPUP_DISMISS_LABELS_EXACT = {
            'close', 'dismiss', 'accept', 'decline',
            'submit & continue', 'got it', 'ok', 'no thanks',
            'close (fake)', 'close modal', 'i understand', 'continue',
        }
        _POPUP_DISMISS_PREFIXES = ('close', 'dismiss', 'got it', 'no thanks')

        def _is_popup_click(entry_) -> bool:
            """Check if this click targeted a popup dismiss button."""
            if entry_.get('action', {}).get('type') != 'click':
                return False
            texts_to_check = []
            hit = entry_.get('hit', {})
            hit_text = (hit.get('hit_text') or '').strip().lower()
            if hit_text:
                texts_to_check.append(hit_text)
            loc = entry_.get('locator') or {}
            for part in (loc.get('ancestor') or {}, loc.get('leaf') or {}):
                text = (part.get('text') or '').strip().lower()
                if text:
                    texts_to_check.append(text)
            for text in texts_to_check:
                if text in _POPUP_DISMISS_LABELS_EXACT:
                    return True
                # Partial match: "Close X", "Dismiss overlay", etc.
                for prefix in _POPUP_DISMISS_PREFIXES:
                    if text.startswith(prefix):
                        return True
            return False

        # Pass 1: mark causal entries
        causal_indices = set()
        last_idx = len(action_log) - 1
        popup_dropped = 0
        for i, entry in enumerate(action_log):
            if entry.get('found_code'):
                causal_indices.add(i)
                break
            action_type = entry.get('action', {}).get('type', '')
            # Text input is always intentional
            if action_type in ('type', 'press', 'keyboard_sequence'):
                causal_indices.add(i)
                continue
            # Popup dismiss clicks: always drop (auto-handler covers these)
            if _is_popup_click(entry):
                popup_dropped += 1
                continue
            # Wait handling (3-tier):
            # 1. Own post_state_changes has positive signal → keep, use own changes
            # 2. Next entry is causal → keep, but only copy targetability changes
            # 3. Neither → drop
            if action_type in ('wait', 'wait_for_state'):
                if _has_positive_signal(entry):
                    causal_indices.add(i)
                elif i < last_idx and _has_positive_signal(action_log[i + 1]):
                    causal_indices.add(i)
                continue
            # Scroll: keep only if it actually moved content
            if action_type in ('scroll', 'element_scroll'):
                hit = entry.get('hit', {})
                if hit.get('scroll_moved', False):
                    causal_indices.add(i)
                continue
            # All other actions: keep only with positive signal
            if _has_positive_signal(entry):
                causal_indices.add(i)
        if popup_dropped > 0:
            log_stage("sidecar", f"recipe filter: dropped {popup_dropped} popup dismiss clicks")

        # Pass 2: 2-step lookback with flailing guard
        # For each causal step at index i, keep setup steps:
        #   i-1 if not already causal
        #   i-2 if: not causal, i-1 was kept as setup, and i-2 is
        #           click/hover/focus/scroll (not another neutral wait)
        # Flailing guard: if 3+ consecutive non-causal before causal,
        #   only keep last 2 (ignore earlier flailing)
        # At most ONE neutral wait allowed as setup
        setup_indices = set()
        for ci in sorted(causal_indices):
            if ci <= 0:
                continue
            # Count consecutive non-causal before this causal step
            run_start = ci - 1
            while run_start > 0 and run_start not in causal_indices:
                run_start -= 1
            if run_start in causal_indices:
                run_start += 1
            non_causal_run = ci - run_start  # length of non-causal run

            # i-1 setup
            prev1 = ci - 1
            if prev1 not in causal_indices:
                # Flailing guard: skip if too far back in a long run
                if non_causal_run <= 3 or (ci - prev1) <= 2:
                    setup_indices.add(prev1)
                    # i-2 setup (only for action types, not waits)
                    prev2 = ci - 2
                    if (prev2 >= 0 and prev2 not in causal_indices
                            and prev2 not in setup_indices):
                        prev2_type = action_log[prev2].get('action', {}).get('type', '')
                        if prev2_type in ('click', 'hover', 'focus', 'scroll', 'element_scroll'):
                            if non_causal_run <= 3 or (ci - prev2) <= 2:
                                setup_indices.add(prev2)
        # Enforce: at most one neutral wait in setup per causal step.
        # Only look at setups in the gap between previous causal and this one.
        sorted_causal = sorted(causal_indices)
        for idx, ci in enumerate(sorted_causal):
            # Find the previous causal boundary
            prev_causal = sorted_causal[idx - 1] if idx > 0 else -1
            wait_setups = []
            for si in sorted(setup_indices):
                if prev_causal < si < ci and si not in causal_indices:
                    st = action_log[si].get('action', {}).get('type', '')
                    if st == 'wait':
                        wait_setups.append(si)
            # Keep only the last (closest to causal) wait in this gap
            for ws in wait_setups[:-1]:
                setup_indices.discard(ws)
        causal_indices |= setup_indices

        # Pass 3: build recipe, converting waits to state-aware
        kept, dropped = 0, 0
        for i, entry in enumerate(action_log):
            if i not in causal_indices:
                dropped += 1
                continue
            is_last = entry.get('found_code', False) or (
                causal_indices and i == max(causal_indices))
            recipe_step = self._action_to_recipe_step(entry, is_last)

            # Convert wait steps to state-aware expectations
            # Tier 1: wait's OWN post_state_changes → use those directly
            # Tier 2: NEXT entry is causal → copy only targetability changes
            #         (became_clickable, enabled, appeared, revealed)
            #         NOT action-result changes (turned_green happens from
            #         the NEXT click, not from waiting)
            action_type = entry.get('action', {}).get('type', '')
            if action_type in ('wait', 'wait_for_state') and i < last_idx:
                own_changes = _get_positive_changes(entry)
                if own_changes:
                    recipe_step['expect_state_changes'] = own_changes[:3]
                else:
                    next_entry = action_log[i + 1]
                    tgt_changes = _get_targetability_changes(next_entry)
                    if tgt_changes:
                        recipe_step['expect_state_changes'] = tgt_changes[:3]

            # 3C: Async-status clicks — if a click produced async state changes,
            # attach them as expect_state_changes so executor waits for settle
            if action_type in ('click', 'double_click') and not recipe_step.get('expect_state_changes'):
                _ASYNC_SIGNALS = {
                    'registered', 'enabled', 'became_clickable',
                    'appeared', 'activated', 'revealed',
                }
                click_changes = _get_positive_changes(entry)
                async_changes = [c for c in click_changes if c in _ASYNC_SIGNALS]
                if async_changes:
                    recipe_step['expect_state_changes'] = async_changes[:3]

            recipe_steps.append(recipe_step)
            kept += 1
            if entry.get('found_code'):
                break
        if dropped > 0:
            log_stage("sidecar", f"recipe filter: kept {kept}, dropped {dropped} "
                      f"no-effect actions")

        # Pass 4: Auto-compress consecutive clicks on same target to repeat_click
        compressed_steps = []
        i_rs = 0
        while i_rs < len(recipe_steps):
            step_r = recipe_steps[i_rs]
            if step_r.get('action_type') != 'click':
                compressed_steps.append(step_r)
                i_rs += 1
                continue
            # Look ahead for consecutive clicks on the same target
            run = [step_r]
            j_rs = i_rs + 1
            while j_rs < len(recipe_steps) and recipe_steps[j_rs].get('action_type') == 'click':
                s_a, s_b = step_r, recipe_steps[j_rs]
                same_text = (s_a.get('target_text') and s_a['target_text'] == s_b.get('target_text'))
                same_coords = False
                if s_a.get('target_coords') and s_b.get('target_coords'):
                    dx = abs(s_a['target_coords'][0] - s_b['target_coords'][0])
                    dy = abs(s_a['target_coords'][1] - s_b['target_coords'][1])
                    same_coords = dx <= 10 and dy <= 10
                if same_text or same_coords:
                    run.append(recipe_steps[j_rs])
                    j_rs += 1
                else:
                    break
            if len(run) >= 2:
                merged = dict(run[0])  # keep first step's locator (pre-completion state)
                merged['action_type'] = 'repeat_click'
                merged['repeat'] = min(len(run), 12)
                # Collect state changes across the run
                all_state = set()
                for r_step in run:
                    for sc in (r_step.get('expect_state_changes') or []):
                        all_state.add(sc)
                if all_state:
                    merged['expect_state_changes'] = list(all_state)[:5]
                compressed_steps.append(merged)
                log_stage("sidecar", f"compressed {len(run)} clicks → repeat_click")
            else:
                compressed_steps.append(step_r)
            i_rs = j_rs
        recipe_steps = compressed_steps

        # Check for strong assertions
        has_strong_assertions = any(
            s.get('expect_code_visible')
            or (s.get('expect_progress_delta') is not None
                and s['expect_progress_delta'] >= 0.2)
            or s.get('expect_selector_visible')
            or (s.get('expect_dom_change_score') is not None
                and s['expect_dom_change_score'] >= 0.05)
            for s in recipe_steps
        )

        # Check for replayable locators (not coords-only)
        has_replayable_step = any(
            s.get('_has_stable_locator') for s in recipe_steps
        )

        # DNA signature
        dna_sig = None
        try:
            elements = self.dna_reasoner.scan(page)
            if elements:
                dna_sig = self.dna_reasoner.find_signature_for_code(elements, code)
        except Exception:
            pass

        total_actions_at_code = sum(
            1 for e in action_log if e.get('hit', {}).get('executed', False)
        )

        # Build causal actions: actions that actually caused progress or DOM changes
        causal_actions = []
        for entry in action_log:
            dc = entry.get('dom_change_score') or 0
            prog_before = entry.get('progress_before')
            prog_after = entry.get('progress_after')
            caused_progress = (prog_before is not None and prog_after is not None
                               and prog_after > prog_before)
            caused_dom_change = dc > 0.05
            if caused_progress or caused_dom_change or entry.get('found_code'):
                loc = entry.get('locator') or {}
                anc = loc.get('ancestor') or {}
                leaf = loc.get('leaf') or {}
                causal_actions.append({
                    'type': entry.get('action', {}).get('type'),
                    'target_role': anc.get('role') or leaf.get('role'),
                    'target_text': (anc.get('text') or leaf.get('text') or '')[:120],
                    'target_aria': anc.get('aria_label') or leaf.get('aria_label') or '',
                    'target_testid': anc.get('data_testid') or leaf.get('data_testid') or '',
                    'effect': 'progress' if caused_progress else (
                        'code_found' if entry.get('found_code') else 'dom_change'
                    ),
                })

        # Use snapshot instruction_text (captured at step start, pristine state).
        # The live page is already on step N+1 by the time this runs.
        step_snapshot = getattr(self, '_step_snapshot', None)
        if step_snapshot and getattr(step_snapshot, 'instruction_text', None):
            instruction_text = step_snapshot.instruction_text
        else:
            try:
                instruction_text = page.evaluate(
                    '() => document.body?.innerText?.substring(0, 300)') or ''
            except Exception:
                instruction_text = ''

        return {
            'challenge_type': ctype,
            'variant_id': f"sidecar_{step}_{hashlib.md5(code.encode()).hexdigest()[:8]}",
            'recipe': recipe_steps,
            '_round_observations': getattr(self, '_round_observations', []),  # Layer A
            '_round_reasonings': getattr(self, '_round_reasonings', []),      # Layer A
            'semantic_steps': causal_actions,
            'assertions_present': has_strong_assertions and has_replayable_step,
            'dna_signature': dna_sig,
            'code_source': self._code_source,
            'actions_until_code_index': total_actions_at_code,
            'instruction_text': instruction_text,
        }

    def _action_to_recipe_step(self, entry: dict, is_last: bool) -> dict:
        """Convert one action log entry to a recipe step dict."""
        action = entry.get('action', {})
        # Layer B: prefer pre-click locator (stable intended target) over post-click
        locator = entry.get('pre_click_locator') or entry.get('locator')
        action_type = action.get('type', 'wait')

        step = {'action_type': action_type}
        has_stable_locator = False

        if locator:
            anc = locator.get('ancestor') or {}
            leaf = locator.get('leaf') or {}

            # Priority 1: data-testid
            testid = anc.get('data_testid') or leaf.get('data_testid')
            if testid:
                step['target_selector'] = f'[data-testid="{testid}"]'
                has_stable_locator = True

            # Priority 2: role + accessible name
            elif anc.get('role') and anc.get('aria_label'):
                step['target_role'] = anc['role']
                step['target_name'] = anc['aria_label']
                has_stable_locator = True

            # Priority 3: aria-label alone
            elif anc.get('aria_label'):
                step['target_name'] = anc['aria_label']
                has_stable_locator = True

            # Priority 4: text targeting (hit_text preferred over post-click locator text)
            else:
                hit = entry.get('hit', {})
                hit_text = (hit.get('hit_text') or hit.get('text', '') or '').strip()
                # 4a: pre-click hit_text (no checkmarks/status indicators)
                if hit_text and len(hit_text) <= 120:
                    step['target_text'] = hit_text
                    has_stable_locator = True
                # 4b: ancestor text (post-click fallback)
                elif anc.get('text') and len(anc['text']) <= 120:
                    step['target_text'] = anc['text']
                    has_stable_locator = True
                # 4c: leaf text (post-click fallback)
                elif leaf.get('text') and len(leaf['text']) <= 120:
                    step['target_text'] = leaf['text']
                    has_stable_locator = True

            # Priority 5: coords as last resort
            raw_coords = locator.get('coords', [])
            step['target_coords'] = tuple(raw_coords) if len(raw_coords) == 2 else None

            # Store for replay flexibility
            step['_locator_leaf'] = leaf
            step['_locator_ancestor'] = anc

        elif action_type in ('click', 'hover', 'double_click', 'focus'):
            x = action.get('x')
            y = action.get('y')
            if x is not None and y is not None:
                step['target_coords'] = (float(x), float(y))
            # No locator → use hit_text for text targeting (pre-click, clean)
            hit = entry.get('hit', {})
            hit_text = (hit.get('hit_text') or hit.get('text', '') or '').strip()
            if hit_text and len(hit_text) <= 120:
                step['target_text'] = hit_text
                has_stable_locator = True

        # Store hover duration for recipe replay (hovers need 2s+ to trigger reveals)
        if action_type == 'hover':
            seconds = action.get('seconds', 2)
            step['delay_ms'] = int(float(seconds) * 1000)

        # Value fields
        if action_type == 'type':
            typed_val = action.get('text', '')
            step['value'] = typed_val
            # Infer resolver for session-specific typed values:
            # Math answers (pure digits ≤4 chars) → eval_expression
            # Short uppercase codes → rot13 (if page mentions decode/rot13)
            if typed_val.strip():
                v = typed_val.strip()
                if v.isdigit() and len(v) <= 4:
                    step['resolver'] = 'eval_expression'
                elif len(v) <= 30 and re.match(r'^[A-Za-z]+$', v):
                    # Only tag as rot13 if page text mentions decode/rot13/cipher
                    hit = entry.get('hit', {})
                    hit_text = hit.get('hit_text') or hit.get('text', '') or ''
                    # Also check the action's instruction context if available
                    action_ctx = action.get('reasoning', '') or ''
                    page_ctx = hit_text + ' ' + action_ctx
                    if re.search(r'rot13|decode|cipher', page_ctx, re.IGNORECASE):
                        step['resolver'] = 'rot13'
        elif action_type == 'press':
            step['value'] = action.get('keys', '')
        elif action_type == 'scroll':
            direction = action.get('direction', 'down')
            amount = action.get('amount', 500)
            # If scroll targeted a specific element, convert to element_scroll for replay
            if action.get('element_id') is not None:
                step['action_type'] = 'element_scroll'
                step['value'] = f"{direction}:{amount}"
            else:
                step['value'] = f"{direction}:{amount}"
        elif action_type in ('draw', 'canvas_draw'):
            path = action.get('path')
            if path:
                import json as _json
                step['value'] = _json.dumps(path)
            step['target_selector'] = 'canvas'
            has_stable_locator = True
        elif action_type == 'element_scroll':
            direction = action.get('direction', 'down')
            amount = action.get('amount', 300)
            step['value'] = f"{direction}:{amount}"
        elif action_type == 'keyboard_sequence':
            keys = action.get('keys', '')
            # Normalize key names for Playwright (word-boundary safe)
            if keys:
                import re as _re
                keys = _re.sub(r'\benter\b', 'Enter', keys, flags=_re.IGNORECASE)
                keys = _re.sub(r'\bctrl\b', 'Control', keys, flags=_re.IGNORECASE)
                keys = _re.sub(r'\btab\b', 'Tab', keys, flags=_re.IGNORECASE)
                keys = _re.sub(r'\bescape\b', 'Escape', keys, flags=_re.IGNORECASE)
                keys = _re.sub(r'\besc\b', 'Escape', keys, flags=_re.IGNORECASE)
                keys = _re.sub(r'\bspace\b', ' ', keys, flags=_re.IGNORECASE)
            step['value'] = keys
        elif action_type == 'wait_for_state':
            timeout_ms = action.get('timeout_ms', 3000)
            step['delay_ms'] = int(timeout_ms)
            # Store only meaningful state change types
            MEANINGFUL_CHANGES = {
                'turned_green', 'became_clickable', 'enabled', 'appeared',
                'revealed', 'became_vibrant', 'activated', 'became_interactive',
            }
            change_types = action.get('change_types') or action.get('expect_state_changes', [])
            if isinstance(change_types, (list, set)):
                filtered = [c for c in change_types if c in MEANINGFUL_CHANGES]
                if filtered:
                    step['expect_state_changes'] = filtered
        elif action_type == 'wait':
            secs = action.get('seconds', 1)
            step['delay_ms'] = int(float(secs) * 1000)
        elif action_type == 'drag':
            x1 = action.get('x1')
            y1 = action.get('y1')
            x2 = action.get('x2')
            y2 = action.get('y2')
            if x1 is not None and y1 is not None:
                step['target_coords'] = (float(x1), float(y1))  # source
            if x2 is not None and y2 is not None:
                step['dest_coords'] = (float(x2), float(y2))    # destination
                step['value'] = f"{float(x2)},{float(y2)}"      # legacy compat

        # Assertions from observed changes
        dom_score = entry.get('dom_change_score', 0)
        if dom_score >= 0.05:
            step['expect_dom_change_score'] = max(0.05, dom_score * 0.5)

        prog_before = entry.get('progress_before')
        prog_after = entry.get('progress_after')
        if prog_before is not None and prog_after is not None:
            delta = prog_after - prog_before
            if delta >= 0.1:
                step['expect_progress_delta'] = round(delta, 2)

        if is_last:
            step['expect_code_visible'] = True

        # Capture state changes for state-aware recipe replay (Fix 6d)
        STATE_PRIORITY = [
            'turned_green', 'became_clickable', 'enabled', 'revealed',
            'appeared', 'new_element', 'became_vibrant', 'activated',
            'turned_red', 'became_interactive', 'turned_grey',
        ]
        if entry.get('post_state_changes'):
            seen = []
            for sc in entry['post_state_changes']:
                for ch in sc.get('changes', []):
                    if ch not in seen:
                        seen.append(ch)
            # Keep top 3 by priority
            ranked = sorted(seen, key=lambda c: STATE_PRIORITY.index(c)
                            if c in STATE_PRIORITY else 99)[:3]
            if ranked:
                step['expect_state_changes'] = ranked

        # ── Stable locator enforcement for targeting actions ──
        TARGETING_ACTIONS = {'click', 'hover', 'drag', 'double_click', 'focus'}
        if action_type in TARGETING_ACTIONS and not has_stable_locator:
            # Try harder: build CSS selector from leaf tag + classes
            if locator:
                leaf = locator.get('leaf') or {}
                tag = leaf.get('tag', '')
                classes = leaf.get('classes', '')
                if tag and classes:
                    # Build e.g. "button.primary-btn"
                    cls_parts = [c.strip() for c in classes.split() if c.strip()
                                 and not c.startswith('css-')]  # skip CSS-in-JS hashes
                    if cls_parts:
                        css_sel = f"{tag}.{'.'.join(cls_parts[:2])}"
                        step['target_selector'] = css_sel
                        has_stable_locator = True
            if not has_stable_locator:
                step['_coords_only'] = True

        # Generate target_pattern for flexible text matching on replay
        target_text = step.get('target_text', '')
        if target_text:
            pattern = self._generate_target_pattern(target_text)
            if pattern:
                step['target_pattern'] = pattern

        # HybridSimilo fingerprint: multi-attribute element signature for fallback matching
        if action_type in ('click', 'hover', 'double_click', 'focus') and locator:
            hit = entry.get('hit', {})
            anc = locator.get('ancestor') or {}
            leaf = locator.get('leaf') or {}
            fingerprint = {
                'tag': (leaf.get('tag') or anc.get('tag') or hit.get('hit_tag') or '').upper(),
                'text': (step.get('target_text') or leaf.get('text') or '')[:50],
                'role': anc.get('role') or leaf.get('role') or '',
                'aria_label': anc.get('aria_label') or leaf.get('aria_label') or '',
                'neighbor_keywords': [],
                'size': [hit.get('hit_width', 0) or 0, hit.get('hit_height', 0) or 0],
            }
            # Extract neighbor keywords from parent/context text
            parent_text = anc.get('text', '') or hit.get('hit_context', '') or ''
            if parent_text:
                stop_words = {'about', 'after', 'before', 'below', 'above', 'their',
                              'there', 'these', 'those', 'which', 'where', 'would',
                              'could', 'should', 'click', 'button', 'submit', 'enter'}
                words = set(w for w in parent_text.split()
                           if len(w) > 4 and w.lower() not in stop_words)
                fingerprint['neighbor_keywords'] = list(words)[:8]
            # Only store if at least tag + one other attribute is present
            if fingerprint['tag'] and (fingerprint['text'] or fingerprint['role'] or
                                       fingerprint['aria_label'] or fingerprint['neighbor_keywords']):
                step['target_fingerprint'] = fingerprint

        step['_has_stable_locator'] = has_stable_locator
        return step

    # ── Pattern Generation for Flexible Text Matching ───────────────────

    @staticmethod
    def _generate_target_pattern(text: str) -> str:
        """Convert exact target text to a flexible regex pattern.

        Strips dynamic parts (counters, session codes, status symbols) and
        builds a word-boundary pattern from significant words. Returns empty
        string if text is too short or all dynamic.

        Examples:
          "Capture (0/3)" → r"\\bCapture\\b"
          "Reveal Code"   → r"(?=.*\\bReveal\\b)(?=.*\\bCode\\b).*"
          "✓ Tab 1: KA"   → r"\\bTab\\b"
        """
        if not text or len(text.strip()) < 3:
            return ''
        base = text.strip()
        # Strip dynamic parts
        base = re.sub(r'\s*[\(\[]\d+\s*/\s*\d+[\)\]]\s*', '', base).strip()
        base = re.sub(r':\s*[A-Z0-9]{2,4}$', '', base).strip()
        base = re.sub(r'\s+\d+$', '', base).strip()
        base = re.sub(r'[✓✔✖✘●○•]\s*', '', base).strip()
        if not base or len(base) < 3:
            return ''
        # Split into significant words (≥3 chars)
        words = [w for w in re.split(r'\s+', base) if len(w) >= 3]
        if not words:
            return ''
        if len(words) == 1:
            return rf'\b{re.escape(words[0])}\b'
        # Multi-word: order-independent lookahead
        return ''.join([rf'(?=.*\b{re.escape(w)}\b)' for w in words[:3]]) + r'.*'

    # ── State-Change Hit Rate Metric ─────────────────────────────────────

    @staticmethod
    def _log_state_change_hit_rate(action_log: list):
        """Log effectiveness of state-change-triggered actions."""
        sc_actions = sum(1 for e in action_log if e.get('triggered_by_state_change'))
        if sc_actions > 0:
            sc_progress = sum(1 for e in action_log
                              if e.get('triggered_by_state_change')
                              and e.get('dom_change_score', 0) >= 0.01)
            log_stage("sidecar",
                      f"state-change hit rate: {sc_progress}/{sc_actions} "
                      f"({sc_progress/sc_actions*100:.0f}%)")

    # ── Result builder ────────────────────────────────────────────────────

    def _make_result(self, success: bool, code: str | None,
                     rounds: int, actions_executed: int,
                     termination_reason: str, action_log: list,
                     promotion_candidate: dict | None,
                     iframe_count: int,
                     candidates: list | None = None) -> dict:
        return {
            'success': success,
            'code': code,
            'rounds': rounds,
            'actions_executed': actions_executed,
            'termination_reason': termination_reason,
            'action_log': action_log,
            'promotion_candidate': promotion_candidate,
            'iframe_count': iframe_count,
            'candidates': candidates or [],
        }
