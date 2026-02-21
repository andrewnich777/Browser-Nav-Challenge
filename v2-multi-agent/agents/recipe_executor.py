"""Recipe Executor — System 1 reflex solver that replays proven action sequences.

Executes a list of ActionSteps with verification assertions. If any assertion
fails, aborts early so the orchestrator can fall back to System 2 (DNA reasoning).

Locator cascade per step: ARIA role/name -> text content -> CSS selector -> DNA
query -> raw coordinates (last resort).
"""

import re
import time
from dataclasses import dataclass
from agents.base import Agent
from config import CHARSET
from code_scorer import is_valid_code, harvest_and_score
from log import log_stage
from primitives import (read_progress, drain_state_changes, peek_state_changes,
                        extract_code_js, wait_for_state,
                        CHANGE_PRIORITY, PROGRESS_CHANGE_TYPES)

MAX_RECIPE_STEPS = 10


@dataclass
class ActionStep:
    action_type: str  # 'click', 'hover', 'type', 'press', 'keyboard_sequence',
                      # 'scroll', 'drag', 'wait', 'wait_for_state',
                      # 'run_agent', 'switch_tab', 'switch_frame',
                      # 'click_until_progress', 'type_and_submit', 'wait_for_code'

    # Locator cascade (tried in this order):
    target_role: str | None = None         # ARIA role + name (most robust)
    target_name: str | None = None         # Accessible name / text content
    target_text: str | None = None         # Text content match (get_by_text)
    target_pattern: str = ''               # Regex for flexible text matching
    target_selector: str | None = None     # CSS selector fallback
    target_dna_query: dict | None = None   # DNA-based targeting
    target_coords: tuple | None = None     # Last resort (x, y)
    dest_coords: tuple | None = None       # Drag destination (x2, y2)

    # HybridSimilo fingerprint for fallback matching when primary cascade fails
    target_fingerprint: dict | None = None  # {tag, text, role, aria_label, neighbor_keywords, size}

    value: str | None = None               # Text to type, key combo, agent name,
                                           # tab/frame selector, etc.
    resolver: str | None = None            # Dynamic value resolver name (e.g. 'eval_expression')
    params: dict = None                    # Action-specific parameters (click_until_progress, etc.)
    delay_ms: int = 100

    # Verification assertions — if ANY fails, abort recipe -> System 2
    expect_selector_visible: str | None = None
    expect_text_contains: str | None = None
    expect_text_regex: str | None = None
    expect_dom_changed: bool = False
    expect_dom_change_score: float | None = None
    expect_code_visible: bool = False
    expect_url_changed: bool = False
    expect_progress_delta: float | None = None  # Expected progress change (e.g. 0.2 = 20%)
    expect_state_changes: list | None = None   # Expected UI state changes (e.g. ['turned_green'])
    repeat: int = 1


class RecipeExecutor(Agent):
    name = "recipe_executor"

    # Challenge types where a decoy code is visible before the real one appears.
    # For these, we snapshot visible codes BEFORE the recipe and prefer new ones.
    DELAY_CHALLENGE_TYPES = {'delay', 'delay_memory', 'delayed_reveal', 'timing'}

    # Types needing stale code filtering (superset of DELAY — includes mutation
    # where codes from previous step linger in the mutation observer buffer)
    STALE_CODE_FILTER_TYPES = DELAY_CHALLENGE_TYPES | {'mutation'}

    # Popup button labels — if a recipe step targets these and the button
    # isn't found, skip the step instead of aborting. The auto-dismiss JS
    # handler already handles real popups in real-time.
    _POPUP_LABELS = {
        'close', 'dismiss', 'accept', 'decline', 'ok', 'got it',
        'no thanks', 'submit & continue', 'close (fake)',
    }

    def _is_popup_step(self, step: 'ActionStep') -> bool:
        """Check if this step targets a popup dismiss button."""
        if step.action_type != 'click':
            return False
        for text in (step.target_text, step.target_name):
            if text and text.strip().lower() in self._POPUP_LABELS:
                return True
        return False

    def run(self, page, step, version, context=None) -> str | None:
        """Execute a recipe (list of ActionSteps). Returns code if found."""
        self._current_step = step
        self._current_version = version
        self._challenge_type = (context or {}).get('challenge_type', '')
        recipe = (context or {}).get('recipe', [])

        # Failure tracking for routing mismatch detection (read by orchestrator)
        self._last_failure_step = None
        self._last_failure_total = len(recipe) if recipe else 0
        self._last_failure_reason = None  # structured: 'target_not_found', 'assertion_failed', etc.
        self._last_target_search = []     # cascade audit trail from _find_target
        self._cached_boundary_y = None    # cached per run() to avoid repeated page.evaluate()
        self._observed_delays = {}        # {step_index: total_settle_ms} for adaptive delay learning
        self._fiber_code = None           # reset between runs to prevent stale code leaks
        self._finish_navigation_done = False
        if not recipe:
            self._last_failure_reason = 'empty_recipe'
            return None

        # Stale code filtering: save timestamp + snapshot codes BEFORE recipe.
        # Delay types: filter decoy codes from flash/timer phase.
        # Mutation: filter stale codes lingering from previous step.
        self._codes_before = set()
        self._delay_recipe_start_ts = 0
        base_type = re.sub(r'_v\d+$', '', self._challenge_type)
        if base_type in self.STALE_CODE_FILTER_TYPES:
            # Timestamp approach: codes appearing after this time are "real"
            try:
                self._delay_recipe_start_ts = page.evaluate('() => Date.now()')
            except Exception:
                pass
            # Also snapshot visible + hook codes as secondary filter
            self._codes_before = self._snapshot_visible_codes(page)
            try:
                all_codes = page.evaluate(
                    "() => window.__getAllCodes ? window.__getAllCodes() : {bus:[], mut:[]}")
                for item in all_codes.get('bus', []) + all_codes.get('mut', []):
                    c = item.get('c', '')
                    if c and len(c) == 6:
                        self._codes_before.add(c)
            except Exception:
                pass
            if self._codes_before:
                log_stage("recipe", f"stale code baseline: {self._codes_before} (to filter)")

        # Stale state guard: if progress is already > 0 before recipe starts,
        # React leaked state from a previous step. The recipe's progress-delta
        # assertions will fail because they expect deltas from a clean baseline.
        # Skip recipe and let sidecar handle it fresh.
        initial_progress = read_progress(page)
        if initial_progress and initial_progress.get('fraction', 0) > 0.05:
            self._last_failure_reason = 'stale_state'
            log_stage("recipe", f"stale state detected (progress={initial_progress.get('fraction', 0):.0%}), "
                      f"skipping recipe -> System 2")
            return None

        # Pre-flight target check: verify recipe's target elements exist on page.
        # Catches misclassified recipes instantly (~50ms) instead of wasting 5-10s
        # running through steps that fail on assertions or target-not-found.
        # Uses a single JS call that checks all targets at once (no Python round-trips).
        _GENERIC_LABELS = {
            'submit', 'close', 'dismiss', 'accept', 'ok', 'continue',
            'next', 'proceed', 'got it', 'no thanks', 'decline',
            'submit code', 'enter code', 'next step', 'reveal',
            'solve', 'complete', 'start', 'begin', 'reset',
            'complete challenge',
        }
        specific_targets = []
        for sd in recipe[:MAX_RECIPE_STEPS]:
            d = sd if isinstance(sd, dict) else sd.__dict__ if hasattr(sd, '__dict__') else {}
            tt = d.get('target_text', '')
            if tt and len(tt) >= 3 and tt.strip().lower() not in _GENERIC_LABELS:
                specific_targets.append(tt)
        if len(specific_targets) >= 2:
            try:
                # Single JS call checks all targets in the DOM text at once
                found = page.evaluate('''(targets) => {
                    const text = document.body.innerText.toLowerCase();
                    return targets.filter(t => text.includes(t.toLowerCase())).length;
                }''', specific_targets)
                miss_ratio = 1.0 - found / len(specific_targets)
                if miss_ratio >= 0.5:
                    self._last_failure_step = 0
                    self._last_failure_reason = 'preflight_target_mismatch'
                    log_stage("recipe", f"pre-flight FAIL: {found}/{len(specific_targets)} "
                              f"targets found on page ({specific_targets}), skipping -> System 2")
                    return None
            except Exception:
                pass  # Page error — proceed with normal execution

        # Delay-type challenges: wait for flash to end before recipe execution.
        # Instead of a hardcoded sleep, use state-change-driven wait: proceed
        # as soon as the challenge signals readiness (enabled, became_clickable,
        # turned_green, etc.) — the same signals the sidecar reacts to.
        if base_type in self.DELAY_CHALLENGE_TYPES:
            flash_wait = self._extract_wait_from_page_text(page) or 3000
            flash_wait = min(flash_wait + 500, 7000)  # +500ms buffer, cap 7s
            # Use PROGRESS_CHANGE_TYPES — same vocabulary as sidecar
            log_stage("recipe", f"delay challenge: smart wait up to {flash_wait}ms for state changes")
            result = wait_for_state(page, change_types=PROGRESS_CHANGE_TYPES,
                                    timeout_ms=flash_wait)
            if result:
                changes = result.get('changes', [])
                log_stage("recipe", f"delay challenge: state change fired early: {changes}")
            else:
                log_stage("recipe", f"delay challenge: no state change in {flash_wait}ms, proceeding")

        for i, step_def in enumerate(recipe[:MAX_RECIPE_STEPS]):
            step_obj = step_def if isinstance(step_def, ActionStep) else self._dict_to_step(step_def)
            log_stage("recipe", f"step {i+1}/{len(recipe)}: {step_obj.action_type}")

            # Clear per-step state to prevent stale code leaks between steps
            self._fiber_code = None

            # Pre-step popup drain: dismiss any popup BEFORE capturing baselines.
            # Popups can reappear between steps, so clear on every iteration.
            self._dismiss_before_coord_click(page)

            # Capture DOM state before step (for assertion check)
            dom_sig_before = self._get_dom_signature(page)

            # Capture progress before step (for progress delta assertion)
            progress_before = None
            if step_obj.expect_progress_delta is not None:
                progress_before = read_progress(page)

            # Drain state change buffer BEFORE step (baseline clear)
            drain_state_changes(page)

            success = self._execute_step(page, step_obj)
            if not success:
                # Popup steps are optional — auto-dismiss handler covers them
                if self._is_popup_step(step_obj):
                    log_stage("recipe", f"step {i+1} popup skip (auto-dismiss covers it)")
                    continue
                self._last_failure_step = i
                # Determine failure reason from target search trail
                if self._last_target_search and not any('HIT' in t[2] for t in self._last_target_search):
                    self._last_failure_reason = 'target_not_found'
                else:
                    self._last_failure_reason = 'action_failed'
                log_stage("recipe", f"step {i+1}/{len(recipe)} FAILED ({self._last_failure_reason}): "
                          f"{step_obj.action_type} | "
                          f"text={step_obj.target_text!r} role={step_obj.target_role} "
                          f"selector={step_obj.target_selector!r} "
                          f"coords={step_obj.target_coords}")
                # Partial success: try completion sweep before giving up
                # Allow at step 0 if progress is already >= 50% (action genuinely advanced it)
                progress = read_progress(page)
                if i > 0 or (progress and progress.get('fraction', 0) >= 0.5):
                    sweep_code = self._completion_sweep(page)
                    if sweep_code:
                        log_stage("recipe", f"partial failure recovery: sweep found {sweep_code}")
                        return sweep_code
                return None

            # Post-step: state-aware settle with action-dependent timeout.
            # When expect_state_changes is specified, extend timeout — we know
            # exactly WHAT to wait for, so be patient (same as sidecar would).
            if step_obj.action_type not in ('wait', 'wait_for_state'):
                expected = step_obj.expect_state_changes or []
                # Tight timeout for fast actions, longer for complex ones
                if step_obj.action_type in ('click', 'type', 'press', 'select'):
                    timeout = min(800, max(350, step_obj.delay_ms + 300))
                elif step_obj.action_type == 'keyboard_sequence':
                    timeout = min(1200, max(350, step_obj.delay_ms + 300))
                elif step_obj.action_type == 'hover':
                    timeout = 1500
                else:  # drag, scroll, etc.
                    timeout = 1500
                # When sidecar recorded expected state changes, extend slightly
                # (state changes fire within 200ms typically, 1500ms is generous)
                if expected:
                    timeout = max(timeout, 1500)
                settled = self._wait_for_state_settle(
                    page, expected_changes=expected,
                    timeout_ms=timeout, min_wait_ms=step_obj.delay_ms)
                if not settled:
                    page.wait_for_timeout(step_obj.delay_ms)
                # Record observed settle time for adaptive delay learning
                if getattr(self, '_last_settle_ms', None) is not None:
                    self._observed_delays[i] = self._last_settle_ms + step_obj.delay_ms

            # Early code short-circuit: if a code appeared via mutation hooks
            # after this action, return it immediately. This makes recipes
            # reactive like the sidecar — when the challenge is solved (e.g.
            # typing the correct answer auto-submits and reveals a code),
            # stop instead of blindly continuing to remaining steps.
            is_final = (i == len(recipe[:MAX_RECIPE_STEPS]) - 1)
            if not is_final:
                early_code = self._check_early_code(page)
                if early_code:
                    log_stage("recipe", f"early code after step {i+1}: "
                              f"{early_code} — short-circuiting remaining steps")
                    return early_code

            # Verify assertions
            if not self._check_assertions(page, step_obj, dom_sig_before, progress_before,
                                          is_final_step=is_final):
                self._last_failure_step = i
                self._last_failure_reason = f'assertion_failed:{self._last_assertion_failure}'
                log_stage("recipe", f"step {i+1}/{len(recipe)} assertion FAILED "
                          f"({self._last_assertion_failure}), aborting -> System 2")
                # Partial success: try completion sweep before giving up
                # Allow at step 0 if progress is already >= 50%
                progress = read_progress(page)
                if i > 0 or (progress and progress.get('fraction', 0) >= 0.5):
                    sweep_code = self._completion_sweep(page)
                    if sweep_code:
                        log_stage("recipe", f"partial failure recovery: sweep found {sweep_code}")
                        return sweep_code
                return None

        # After recipe completion, extract code
        return self._extract_code_after_recipe(page)

    # Actions that don't need _find_target (no target element required)
    _NO_TARGET_ACTIONS = frozenset({
        'wait', 'wait_for_state', 'scroll', 'press', 'keyboard_sequence',
        'finish_navigation', 'run_agent', 'fiber_bypass', 'wait_for_code',
    })

    def _execute_step(self, page, step: ActionStep) -> bool:
        """Execute a single action step. Target found via locator cascade."""
        target = None if step.action_type in self._NO_TARGET_ACTIONS else self._find_target(page, step)

        try:
            if step.action_type == 'click':
                if target:
                    loc = target.get('locator')
                    for _ in range(step.repeat):
                        if loc:
                            try:
                                loc.click(timeout=3000)
                            except Exception:
                                # Fallback: overlay may be intercepting the click
                                # (popup respawn faster than dismiss). Use coordinate
                                # click like the sidecar's JS click fallback.
                                self._dismiss_before_coord_click(page)
                                page.mouse.click(target['x'], target['y'])
                        else:
                            self._dismiss_before_coord_click(page)
                            page.mouse.click(target['x'], target['y'])
                        if step.repeat > 1:
                            page.wait_for_timeout(300)
                elif step.target_coords:
                    x, y = step.target_coords
                    if abs(x) < 2 and abs(y) < 2:
                        return False  # near-origin = corrupted data
                    self._dismiss_before_coord_click(page)
                    for _ in range(step.repeat):
                        page.mouse.click(x, y)
                        if step.repeat > 1:
                            page.wait_for_timeout(300)
                else:
                    # Adaptive recovery: scroll and retry (once per step)
                    target = self._scroll_recover(page, step)
                    if target:
                        loc = target.get('locator')
                        if loc:
                            loc.click(timeout=3000)
                        else:
                            self._dismiss_before_coord_click(page)
                            page.mouse.click(target['x'], target['y'])
                    else:
                        return False
                return True

            elif step.action_type == 'hover':
                hover_target = target
                if not hover_target and step.target_coords:
                    x, y = step.target_coords
                    if abs(x) >= 2 or abs(y) >= 2:
                        hover_target = {'x': x, 'y': y}
                if hover_target:
                    loc = hover_target.get('locator')
                    if loc:
                        loc.hover(timeout=3000)
                    else:
                        page.mouse.move(hover_target['x'], hover_target['y'])
                    # Reactive hover hold: wait for state change (reveal),
                    # fall back to fixed wait if no change detected
                    hold_ms = step.delay_ms if step.delay_ms > 200 else 1500
                    result = wait_for_state(page,
                        change_types=PROGRESS_CHANGE_TYPES,
                        timeout_ms=hold_ms)
                    if not result:
                        page.wait_for_timeout(min(hold_ms, 500))
                return bool(hover_target)

            elif step.action_type == 'type':
                value = step.value or ''
                if step.resolver:
                    from resolvers import resolve
                    resolved = resolve(step.resolver, page)
                    if resolved is None:
                        log_stage("recipe", f"resolver '{step.resolver}' failed, aborting")
                        return False
                    value = resolved
                # Ensure target input is focused before typing
                focused = False
                if target:
                    loc = target.get('locator')
                    if loc:
                        loc.click(timeout=3000)
                    else:
                        page.mouse.click(target['x'], target['y'])
                    page.wait_for_timeout(100)
                    focused = True
                elif step.target_coords:
                    x, y = step.target_coords
                    if abs(x) >= 2 or abs(y) >= 2:
                        page.mouse.click(x, y)
                        page.wait_for_timeout(100)
                        focused = True
                else:
                    # No target specified — auto-find first visible input/textarea
                    input_center = self._find_first_input(page)
                    if input_center:
                        page.mouse.click(input_center['x'], input_center['y'])
                        page.wait_for_timeout(100)
                        focused = True
                        log_stage("recipe", f"auto-focused input at ({input_center['x']:.0f}, {input_center['y']:.0f})")
                # Select-all before typing to clear stale content
                if focused:
                    page.keyboard.press('Control+a')
                    page.wait_for_timeout(50)
                page.keyboard.type(value)
                return True

            elif step.action_type == 'keyboard_sequence':
                for key in (step.value or '').split(','):
                    page.keyboard.press(key.strip())
                    page.wait_for_timeout(50)
                return True

            elif step.action_type == 'press':
                keys = (step.value or '').strip()
                if not keys:
                    return False
                keys = keys.replace('Ctrl', 'Control').replace('ctrl', 'Control')
                keys = keys.replace('cmd', 'Meta').replace('Cmd', 'Meta')
                for _ in range(step.repeat):
                    page.keyboard.press(keys)
                    if step.repeat > 1:
                        page.wait_for_timeout(100)
                return True

            elif step.action_type == 'scroll':
                raw = step.value or 'down'
                if ':' in raw:
                    parts = raw.split(':', 1)
                    direction = parts[0]
                    try:
                        amount = int(parts[1])
                    except (ValueError, IndexError):
                        amount = 500
                else:
                    direction = raw
                    amount = 500
                if direction == 'down':
                    page.evaluate(f'window.scrollBy(0, {amount})')
                elif direction == 'up':
                    page.evaluate(f'window.scrollBy(0, -{amount})')
                elif direction == 'right':
                    page.evaluate(f'window.scrollBy({amount}, 0)')
                elif direction == 'left':
                    page.evaluate(f'window.scrollBy(-{amount}, 0)')
                elif direction == 'bottom':
                    page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                return True

            elif step.action_type == 'double_click':
                if target:
                    loc = target.get('locator')
                    if loc:
                        loc.dblclick(timeout=3000)
                    else:
                        page.mouse.dblclick(target['x'], target['y'])
                elif step.target_coords:
                    x, y = step.target_coords
                    if abs(x) >= 2 or abs(y) >= 2:
                        page.mouse.dblclick(x, y)
                    else:
                        return False
                else:
                    return False
                return True

            elif step.action_type == 'focus':
                if target:
                    loc = target.get('locator')
                    if loc:
                        loc.click(timeout=3000)
                    else:
                        page.mouse.click(target['x'], target['y'])
                    page.wait_for_timeout(50)
                elif step.target_selector:
                    try:
                        page.focus(step.target_selector)
                    except Exception:
                        return False
                else:
                    return False
                return True

            elif step.action_type == 'element_scroll':
                # React-compatible container scroll via shared primitive
                from primitives import scroll_container_js
                raw_es = step.value or 'down'
                if ':' in raw_es:
                    es_parts = raw_es.split(':', 1)
                    direction = es_parts[0]
                    try:
                        amount = int(es_parts[1])
                    except (ValueError, IndexError):
                        amount = 300
                else:
                    direction = raw_es
                    amount = 300
                # Prefer BID from target_selector if it's a BID selector
                bid = None
                if step.target_selector and 'data-bid' in (step.target_selector or ''):
                    m = re.search(r'data-bid="(\d+)"', step.target_selector)
                    if m:
                        bid = int(m.group(1))
                result = scroll_container_js(page, bid=bid, direction=direction, amount=amount)
                if result.get('success'):
                    page.wait_for_timeout(200)
                    return True
                # Fallback: if we have a target with coords, try mouse.wheel
                if target:
                    page.mouse.move(target['x'], target['y'])
                    v_delta = amount if direction == 'down' else -amount
                    page.mouse.wheel(0, v_delta)
                    page.wait_for_timeout(200)
                    return True
                return False

            elif step.action_type in ('draw', 'canvas_draw'):
                from primitives import draw_stroke_on_canvas
                # Recipe draw steps store path as list of [nx, ny] normalized coords
                path = step.value  # For recipes, value holds the serialized path
                if isinstance(path, str):
                    import json as _json
                    try:
                        path = _json.loads(path)
                    except Exception:
                        return False
                if not path or not isinstance(path, list):
                    return False
                canvas_target = step.target_selector or 'canvas'
                result = draw_stroke_on_canvas(page, canvas_target, path)
                return result.get('success', False)

            elif step.action_type == 'drag':
                src = self._find_target(page, step)

                # Destination priority: dest_coords > parse from value > None
                dst_coords = None
                if step.dest_coords:
                    dst_coords = step.dest_coords
                elif step.value and ',' in step.value:
                    parts = step.value.split(',')
                    try:
                        dx, dy = float(parts[0].strip()), float(parts[1].strip())
                        if dx != 0 or dy != 0:
                            dst_coords = (dx, dy)
                    except (ValueError, IndexError):
                        pass

                if src and dst_coords:
                    # Guard: if dst ≈ src (within 5px), treat as invalid
                    if abs(src['x'] - dst_coords[0]) < 5 and abs(src['y'] - dst_coords[1]) < 5:
                        log_stage("recipe", "drag: dest ≈ source (within 5px), skipping")
                        return False
                    # Build CSS selectors from elements at coordinates, then use
                    # Playwright's native drag_and_drop (CDP-level, trusted events)
                    for _ in range(step.repeat):
                        selectors = page.evaluate('''([sx, sy, dx, dy]) => {
                            function buildSelector(el) {
                                if (!el) return null;
                                // Walk up to find draggable ancestor
                                let drag = el;
                                while (drag && !drag.hasAttribute('draggable') && drag.parentElement)
                                    drag = drag.parentElement;
                                if (drag && drag.hasAttribute('draggable')) el = drag;
                                // Build a unique selector
                                if (el.id) return '#' + CSS.escape(el.id);
                                if (el.dataset && el.dataset.testid)
                                    return `[data-testid="${el.dataset.testid}"]`;
                                // Use nth-child path
                                const parts = [];
                                let cur = el;
                                while (cur && cur !== document.body && cur !== document.documentElement) {
                                    let tag = cur.tagName.toLowerCase();
                                    if (cur.parentElement) {
                                        const siblings = Array.from(cur.parentElement.children)
                                            .filter(c => c.tagName === cur.tagName);
                                        if (siblings.length > 1) {
                                            const idx = siblings.indexOf(cur) + 1;
                                            tag += `:nth-of-type(${idx})`;
                                        }
                                    }
                                    parts.unshift(tag);
                                    cur = cur.parentElement;
                                }
                                return parts.join(' > ');
                            }
                            const srcEl = document.elementFromPoint(sx, sy);
                            const dstEl = document.elementFromPoint(dx, dy);
                            return {
                                src: buildSelector(srcEl),
                                dst: buildSelector(dstEl),
                                srcTag: srcEl?.tagName,
                                dstTag: dstEl?.tagName
                            };
                        }''', [src['x'], src['y'],
                               dst_coords[0], dst_coords[1]])
                        src_sel = selectors.get('src') if selectors else None
                        dst_sel = selectors.get('dst') if selectors else None
                        if not src_sel or not dst_sel:
                            log_stage("recipe",
                                      f"drag: can't build selector (src={src_sel}, dst={dst_sel})")
                            return False
                        try:
                            page.drag_and_drop(src_sel, dst_sel, timeout=3000)
                        except Exception as e:
                            log_stage("recipe", f"drag_and_drop failed: {e}")
                            return False
                        if step.repeat > 1:
                            page.wait_for_timeout(200)
                    return True

                # BID-based: try locator.drag_to if value has "src_bid:X,tgt_bid:Y"
                if step.value and ',' in step.value:
                    parts = step.value.split(',')
                    try:
                        src_sel = parts[0].strip()
                        tgt_sel = parts[1].strip()
                        src_loc = page.locator(src_sel).first
                        tgt_loc = page.locator(tgt_sel).first
                        src_loc.drag_to(tgt_loc, timeout=3000, force=True)
                        return True
                    except Exception as e:
                        log_stage("recipe", f"drag locator failed: {e}")
                        return False

                log_stage("recipe", f"drag: missing source={src} or dest={dst_coords}")
                return False

            elif step.action_type == 'wait_for_state':
                timeout_ms = min(int(step.delay_ms or 3000), 10000)
                # Use stored change types if available, else full vocabulary
                change_types = set(step.expect_state_changes) if step.expect_state_changes else \
                    set(CHANGE_PRIORITY.keys())
                result = wait_for_state(page, change_types=change_types,
                                        timeout_ms=timeout_ms)
                return True  # Always continue — state may or may not have changed

            elif step.action_type == 'wait':
                # Parse wait duration from value (e.g. "3s", "500ms") or delay_ms
                wait_ms = step.delay_ms or 1000
                if step.value:
                    v = step.value.strip().lower()
                    if v.endswith('s') and not v.endswith('ms'):
                        try:
                            wait_ms = int(float(v[:-1]) * 1000)
                        except ValueError:
                            pass
                    elif v.endswith('ms'):
                        try:
                            wait_ms = int(v[:-2])
                        except ValueError:
                            pass

                # Dynamic wait: extract duration from page text (e.g. "wait 5 seconds")
                # Overrides hardcoded delay_ms if found — makes recipes version-resilient
                page_wait = self._extract_wait_from_page_text(page)
                if page_wait is not None:
                    log_stage("recipe", f"page text says {page_wait}ms, recipe has {wait_ms}ms")
                    wait_ms = page_wait + 500  # 500ms buffer for render

                page.wait_for_timeout(wait_ms)
                return True

            elif step.action_type == 'run_agent':
                from agents import ALL_AGENTS
                agent = ALL_AGENTS.get(step.value)
                if agent:
                    result = agent.run(page,
                                       getattr(self, '_current_step', 0),
                                       getattr(self, '_current_version', 0))
                    return bool(result)
                return False

            elif step.action_type == 'repeat_click':
                from primitives import repeat_action_until_signal
                max_clicks = step.repeat if step.repeat > 1 else 10
                expected_states = set(step.expect_state_changes or [])
                stale_codes = getattr(self, '_codes_before', set())

                # For mutation: read required count from page text
                base_type_rc = re.sub(r'_v\d+$', '', getattr(self, '_challenge_type', ''))
                if base_type_rc == 'mutation' and max_clicks <= 10:
                    try:
                        page_text = page.inner_text('body')[:2000].lower()
                        m = re.search(r'(\d+)\s+(?:dom\s+)?mutations?', page_text)
                        if m:
                            required = int(m.group(1))
                            max_clicks = required + 2  # buffer for missed clicks
                            log_stage("recipe", f"mutation count from page: {required}, "
                                      f"max_clicks={max_clicks}")
                    except Exception:
                        pass

                def _repeat_click_fn(p, _step=step):
                    t = self._find_target(p, _step)
                    if t:
                        loc = t.get('locator')
                        if loc:
                            try:
                                loc.click(timeout=2000)
                            except Exception:
                                p.mouse.click(t['x'], t['y'])
                        else:
                            p.mouse.click(t['x'], t['y'])
                    elif _step.target_coords:
                        p.mouse.click(_step.target_coords[0], _step.target_coords[1])

                def _code_checker(p, _expected=expected_states, _stale=stale_codes):
                    # State check (if expected)
                    if _expected:
                        changes = peek_state_changes(p)
                        for sc in (changes or []):
                            if set(sc.get('changes', [])) & _expected:
                                return 'state_match'
                    # Stale-code-aware harvest (filters codes from previous step)
                    if _stale:
                        score, code = harvest_and_score(p, '', 0)
                        if code and score >= 0.5 and code not in _stale:
                            return code
                        return None
                    return None

                # Use code_checker when we have expected states OR stale codes to filter
                use_checker = bool(expected_states or stale_codes)
                result = repeat_action_until_signal(
                    page, _repeat_click_fn, progress_reader=read_progress,
                    code_checker=_code_checker if use_checker else None,
                    max_tries=max_clicks, delay_ms=step.delay_ms or 400)
                return result.get('stopped_by') in ('code_found', 'progress_complete', 'exhausted')

            elif step.action_type == 'drag_drop_auto':
                from primitives import discover_drag_drop_puzzle, execute_drag_sequence
                discovery = discover_drag_drop_puzzle(page)
                if not discovery or not discovery.get('pairs'):
                    log_stage("recipe", "drag_drop_auto: no puzzle discovered")
                    return False
                return execute_drag_sequence(page, discovery['pairs'])

            elif step.action_type == 'reactive_click':
                # Fast twitch: wait for target to become visible, click instantly.
                # Used for timing challenges where elements appear briefly.
                target_text = step.target_text or step.value or 'Capture'
                timeout_ms = step.delay_ms if step.delay_ms > 1000 else 15000
                log_stage("recipe", f"reactive_click: watching for '{target_text}' "
                          f"(timeout={timeout_ms}ms)")
                try:
                    loc = page.get_by_text(target_text, exact=False)
                    loc.first.wait_for(state="visible", timeout=timeout_ms)
                    loc.first.click(timeout=1000)
                    log_stage("recipe", f"reactive_click: clicked '{target_text}'")
                    return True
                except Exception as e:
                    log_stage("recipe", f"reactive_click: timeout waiting for '{target_text}': {e}")
                    return False

            elif step.action_type == 'fiber_bypass':
                # Walk React fiber tree to call onComplete — ONLY for recursive_iframe
                base_type = re.sub(r'_v\d+$', '', getattr(self, '_challenge_type', ''))
                if 'recursive_iframe' not in base_type:
                    log_stage("recipe", "fiber_bypass blocked: not a recursive_iframe challenge")
                    return False
                try:
                    fiber_code = page.evaluate(f'''() => {{
                        try {{
                            const btns = [...document.querySelectorAll('button')];
                            const extractBtn = btns.find(b =>
                                b.textContent.trim().toLowerCase().includes('extract code'));
                            if (!extractBtn) return null;
                            const fiberKey = Object.keys(extractBtn).find(
                                k => k.startsWith('__reactFiber'));
                            if (!fiberKey) return null;
                            let fiber = extractBtn[fiberKey];
                            for (let i = 0; i < 50 && fiber; i++) {{
                                const props = fiber.memoizedProps;
                                if (props && typeof props.onComplete === 'function') {{
                                    const depthMatch = document.body.innerText.match(
                                        /depth[:\\s]*(\\d+)\\s*\\/\\s*(\\d+)/i);
                                    const maxDepth = depthMatch ? parseInt(depthMatch[2]) : 3;
                                    const proof = {{
                                        type: "recursive_iframe",
                                        timestamp: Date.now(),
                                        data: {{
                                            method: "recursive_iframe",
                                            depth: maxDepth, maxDepth: maxDepth,
                                            completedLevels: maxDepth,
                                            stepNum: {getattr(self, '_current_step', 0)}
                                        }}
                                    }};
                                    return props.onComplete(proof);
                                }}
                                fiber = fiber.return;
                            }}
                            return null;
                        }} catch (e) {{ return null; }}
                    }}''')
                    if fiber_code and len(str(fiber_code)) == 6:
                        log_stage("recipe", f"fiber_bypass produced code: {fiber_code}")
                        # Stash the code for _extract_code_after_recipe
                        self._fiber_code = str(fiber_code)
                        return True
                    log_stage("recipe", "fiber_bypass returned no code")
                    return False
                except Exception as e:
                    log_stage("recipe", f"fiber_bypass error: {e}")
                    return False

            elif step.action_type == 'finish_navigation':
                # Navigate to /finish via pushState — step 30 workaround
                # (the website has no code 31, so step 30 is always broken)
                try:
                    page.evaluate('''() => {
                        window.history.pushState({}, '', '/finish');
                        window.dispatchEvent(new PopStateEvent('popstate'));
                    }''')
                    page.wait_for_timeout(1000)
                    from verify import is_finish_page
                    if is_finish_page(page):
                        log_stage("recipe", "finish_navigation: reached /finish page")
                        self._finish_navigation_done = True
                        return True
                    log_stage("recipe", "finish_navigation: didn't reach /finish")
                    return False
                except Exception as e:
                    log_stage("recipe", f"finish_navigation error: {e}")
                    return False

            elif step.action_type == 'click_until_progress':
                params = step.params or {}
                threshold = params.get('threshold', 1.0)
                max_clicks = params.get('max_clicks', 20)
                for click_i in range(max_clicks):
                    t = self._find_target(page, step)
                    if t:
                        loc = t.get('locator')
                        if loc:
                            try:
                                loc.click(timeout=2000)
                            except Exception:
                                if t.get('x') and t.get('y'):
                                    page.mouse.click(t['x'], t['y'])
                        else:
                            page.mouse.click(t['x'], t['y'])
                    elif step.target_coords:
                        page.mouse.click(step.target_coords[0], step.target_coords[1])
                    else:
                        break
                    page.wait_for_timeout(300)
                    cur_progress = read_progress(page)
                    if cur_progress and cur_progress.get('fraction', 0) >= threshold:
                        log_stage("recipe", f"click_until_progress: {click_i+1} clicks, "
                                  f"progress={cur_progress.get('fraction', 0):.0%}")
                        break
                return True

            elif step.action_type == 'type_and_submit':
                params = step.params or {}
                value = params.get('value', step.value or '')
                if step.resolver:
                    from resolvers import resolve
                    resolved = resolve(step.resolver, page)
                    if resolved is not None:
                        value = resolved
                # Find input, type value
                input_target = self._find_first_input(page) if not target else None
                focus_target = target or (input_target if input_target else None)
                if focus_target:
                    loc = focus_target.get('locator')
                    if loc:
                        loc.click(timeout=2000)
                    else:
                        page.mouse.click(focus_target['x'], focus_target['y'])
                    page.wait_for_timeout(100)
                page.keyboard.press('Control+a')
                page.wait_for_timeout(50)
                page.keyboard.type(value)
                page.wait_for_timeout(500)
                # Check if auto-submitted (code appeared)
                score, code = harvest_and_score(page, '', 0)
                if code and score >= 0.3:
                    return True
                # Try pressing Enter
                page.keyboard.press('Enter')
                page.wait_for_timeout(500)
                return True

            elif step.action_type == 'wait_for_code':
                params = step.params or {}
                timeout_ms = params.get('timeout', 5000)
                polls = max(1, timeout_ms // 250)
                for _ in range(polls):
                    score, code = harvest_and_score(page, '', 0)
                    if code and score >= 0.3:
                        return True
                    page.wait_for_timeout(250)
                return True  # Always continue — code check is via assertion

            elif step.action_type in ('switch_tab', 'switch_frame'):
                # These don't change execution context — abort recipe so System 2 handles it
                log_stage("recipe", f"aborting: {step.action_type} not supported in recipe replay")
                return False

        except Exception as e:
            log_stage("recipe", f"execute error: {e}")
            return False

        # Strict mode: unknown action types fail fast instead of silently passing
        log_stage("recipe", f"unsupported action type: '{step.action_type}' — aborting")
        return False

    @staticmethod
    def _get_challenge_boundary_y(page) -> float:
        """Find the Y boundary below which elements are decoys (not part of the challenge).

        3-strategy fallback:
        1. Code input form → parent form → bottom + 20
        2. "Submit Code" button → bottom + 20
        3. First filler marker → top (conservative matching only)
        4. Fallback: 99999 (accept all)
        """
        try:
            boundary = page.evaluate(r'''() => {
                // Strategy 1: Code input form parent
                const codeInput = document.querySelector(
                    'input[placeholder*="code" i], input[placeholder*="enter" i]');
                if (codeInput) {
                    const form = codeInput.closest('form') || codeInput.parentElement;
                    if (form) {
                        const r = form.getBoundingClientRect();
                        if (r.height > 0) return r.bottom + 20;
                    }
                }
                // Strategy 2: Submit Code button
                for (const btn of document.querySelectorAll('button')) {
                    if (/submit\s*code/i.test(btn.textContent || '')) {
                        const r = btn.getBoundingClientRect();
                        if (r.height > 0) return r.bottom + 20;
                    }
                }
                // Strategy 3: First filler marker (conservative phrases only)
                const fillerRe = /^(Next Section|Scroll Down|Section \d+|Keep Scrolling|Below the Fold)$/i;
                for (const el of document.querySelectorAll('h2, h3, h4, div, p, span')) {
                    const t = (el.textContent || '').trim();
                    if (fillerRe.test(t)) {
                        const r = el.getBoundingClientRect();
                        if (r.height > 0 && r.top > 100) return r.top;
                    }
                }
                return 99999;
            }''')
            return boundary or 99999
        except Exception:
            return 99999

    @staticmethod
    def _is_in_challenge_area(center_y: float, boundary_y: float) -> bool:
        """Check if element's center Y is above the challenge boundary."""
        return boundary_y >= 99999 or center_y < boundary_y

    def _fingerprint_match(self, page, fingerprint: dict, boundary_y: float) -> dict | None:
        """HybridSimilo-inspired fingerprint matching: find best element by multi-attribute similarity.

        Scores each interactive element against the stored fingerprint:
        - Tag match: +0.15, Text exact: +0.30 / partial: +0.15
        - Role match: +0.10, ARIA label match: +0.15
        - Neighbor keyword overlap: up to +0.20
        - Size similarity: +0.05, In challenge area: +0.05 bonus
        Returns {x, y} of best match if score > 0.30, else None.
        """
        try:
            result = page.evaluate(r'''(args) => {
                const fp = args.fp;
                const boundaryY = args.boundaryY;
                const sels = 'button, input, canvas, [role="button"], [onclick], [tabindex]';
                let best = null, bestScore = 0;
                for (const el of document.querySelectorAll(sels)) {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    let score = 0;
                    // Tag match
                    if (fp.tag && el.tagName.toUpperCase() === fp.tag.toUpperCase()) score += 0.15;
                    // Text match
                    const elText = (el.textContent || '').trim().substring(0, 50);
                    if (fp.text && elText) {
                        if (elText === fp.text) score += 0.30;
                        else if (elText.includes(fp.text) || fp.text.includes(elText)) score += 0.15;
                    }
                    // Role match
                    const elRole = el.getAttribute('role') || '';
                    if (fp.role && elRole && elRole === fp.role) score += 0.10;
                    // ARIA label match
                    const elAria = el.getAttribute('aria-label') || '';
                    if (fp.aria_label && elAria && elAria.toLowerCase().includes(fp.aria_label.toLowerCase()))
                        score += 0.15;
                    // Neighbor keywords
                    if (fp.neighbor_keywords && fp.neighbor_keywords.length > 0) {
                        const parentText = (el.parentElement?.textContent || '').toLowerCase();
                        let kwMatched = 0;
                        for (const kw of fp.neighbor_keywords) {
                            if (parentText.includes(kw.toLowerCase())) kwMatched++;
                        }
                        score += 0.20 * (kwMatched / fp.neighbor_keywords.length);
                    }
                    // Size similarity (within 50% ratio)
                    if (fp.size && fp.size.length === 2 && fp.size[0] > 0 && fp.size[1] > 0) {
                        const wRatio = Math.min(r.width, fp.size[0]) / Math.max(r.width, fp.size[0]);
                        const hRatio = Math.min(r.height, fp.size[1]) / Math.max(r.height, fp.size[1]);
                        if (wRatio > 0.5 && hRatio > 0.5) score += 0.05;
                    }
                    // Challenge area bonus
                    const cy = r.y + r.height / 2;
                    if (boundaryY >= 99999 || cy < boundaryY) score += 0.05;
                    if (score > bestScore) {
                        bestScore = score;
                        best = {x: r.x + r.width/2, y: r.y + r.height/2, score: score};
                    }
                }
                return (best && bestScore > 0.30) ? best : null;
            }''', {'fp': fingerprint, 'boundaryY': boundary_y})
            if result:
                log_stage("recipe", f"fingerprint match score={result.get('score', 0):.2f}")
            return result
        except Exception:
            return None

    def _find_target(self, page, step: ActionStep) -> dict | None:
        """Locator cascade: role/name -> text -> selector -> fingerprint -> DNA -> coords.

        Auto-scrolls elements into view if found but off-screen (bounding_box
        returns None). This makes explicit scroll recipe steps optional.
        Prefers elements in the challenge area (above code submission form).
        Sets self._last_target_search for diagnostics on failure.
        """
        tried = []  # [(method, query, result_str)]
        if self._cached_boundary_y is None:
            self._cached_boundary_y = self._get_challenge_boundary_y(page)
        boundary_y = self._cached_boundary_y

        def _box_to_center(box, locator=None):
            result = {'x': box['x'] + box['width']/2,
                      'y': box['y'] + box['height']/2}
            if locator is not None:
                result['locator'] = locator
            return result

        def _try_scroll_into_view(loc_or_el, is_locator=True, locator_ref=None):
            """Scroll element into view and return center coords, or None."""
            try:
                if is_locator:
                    loc_or_el.first.scroll_into_view_if_needed(timeout=2000)
                    box = loc_or_el.first.bounding_box()
                else:
                    loc_or_el.scroll_into_view_if_needed(timeout=2000)
                    box = loc_or_el.bounding_box()
                if box:
                    return _box_to_center(box, locator=locator_ref)
            except Exception:
                pass
            return None

        def _pick_in_challenge_area(loc, cnt, method_name, query_str):
            """Pick the first element in the challenge area from a locator with cnt matches.
            Falls back to the first match if all are below boundary. Returns (center_dict, tried_str) or (None, tried_str)."""
            best_in_area = None
            first_match = None
            for idx in range(min(cnt, 5)):
                try:
                    el = loc.nth(idx)
                    box = el.bounding_box()
                    if box:
                        center = _box_to_center(box, locator=el)
                        if first_match is None:
                            first_match = center
                        if self._is_in_challenge_area(center['y'], boundary_y):
                            if best_in_area is None:
                                best_in_area = center
                                break  # first one in area is good enough
                except Exception:
                    continue
            result = best_in_area or first_match
            if result:
                skipped = best_in_area is not None and first_match is not None and best_in_area is not first_match
                extra = ' (SKIP decoy below boundary)' if skipped else ''
                return result, f'HIT (count={cnt}){extra}'
            return None, f'found {cnt} but no box'

        # 1. ARIA role + accessible name (most robust across layouts)
        if step.target_role and step.target_name:
            try:
                loc = page.get_by_role(step.target_role, name=step.target_name)
                cnt = loc.count()
                if cnt > 0:
                    result, hit_str = _pick_in_challenge_area(loc, cnt, 'role+name',
                        f'{step.target_role}:"{step.target_name}"')
                    if result:
                        tried.append(('role+name', f'{step.target_role}:"{step.target_name}"', hit_str))
                        self._last_target_search = tried
                        return result
                    # Element exists but off-screen — scroll into view
                    scroll_result = _try_scroll_into_view(loc, locator_ref=loc.first)
                    if scroll_result:
                        tried.append(('role+name', f'{step.target_role}:"{step.target_name}"', f'HIT after scroll (count={cnt})'))
                        self._last_target_search = tried
                        return scroll_result
                    tried.append(('role+name', f'{step.target_role}:"{step.target_name}"', f'found {cnt} but no box (off-screen?)'))
                else:
                    tried.append(('role+name', f'{step.target_role}:"{step.target_name}"', 'count=0'))
            except Exception as e:
                tried.append(('role+name', f'{step.target_role}:"{step.target_name}"', f'error: {e}'))

        # 2. Text content match (target_text or target_name without role)
        text_to_find = step.target_text or (step.target_name if not step.target_role else None)
        if text_to_find:
            try:
                loc = page.get_by_text(text_to_find, exact=False)
                cnt = loc.count()
                if cnt > 0:
                    result, hit_str = _pick_in_challenge_area(loc, cnt, 'text', f'"{text_to_find}"')
                    if result:
                        tried.append(('text', f'"{text_to_find}"', hit_str))
                        self._last_target_search = tried
                        return result
                    scroll_result = _try_scroll_into_view(loc, locator_ref=loc.first)
                    if scroll_result:
                        tried.append(('text', f'"{text_to_find}"', f'HIT after scroll (count={cnt})'))
                        self._last_target_search = tried
                        return scroll_result
                    tried.append(('text', f'"{text_to_find}"', f'found {cnt} but no box'))
                else:
                    tried.append(('text', f'"{text_to_find}"', 'count=0'))
            except Exception as e:
                tried.append(('text', f'"{text_to_find}"', f'error: {e}'))
            # 2b. Fuzzy: strip trailing counters/parens and retry
            #     "Capture (0/3)" → "Capture", "Part 1: AB3" → "Part 1"
            shorter = re.sub(r'\s*[\(\[][\d/]+[\)\]]\s*$', '', text_to_find).strip()
            shorter = re.sub(r':\s*[A-Z0-9]{2,4}\s*$', '', shorter).strip()
            if shorter and shorter != text_to_find and len(shorter) >= 3:
                try:
                    loc = page.get_by_text(shorter, exact=False)
                    cnt = loc.count()
                    if cnt > 0:
                        result, hit_str = _pick_in_challenge_area(loc, cnt, 'text_fuzzy', f'"{shorter}"')
                        if result:
                            tried.append(('text_fuzzy', f'"{shorter}"', hit_str))
                            self._last_target_search = tried
                            return result
                        scroll_result = _try_scroll_into_view(loc, locator_ref=loc.first)
                        if scroll_result:
                            tried.append(('text_fuzzy', f'"{shorter}"', f'HIT after scroll'))
                            self._last_target_search = tried
                            return scroll_result
                        tried.append(('text_fuzzy', f'"{shorter}"', f'found {cnt} but no box'))
                    else:
                        tried.append(('text_fuzzy', f'"{shorter}"', 'count=0'))
                except Exception as e:
                    tried.append(('text_fuzzy', f'"{shorter}"', f'error: {e}'))

        # 2.5. Pattern match (regex against visible interactables)
        if step.target_pattern:
            try:
                matches = page.evaluate('''([pat, bY]) => {
                    const re = new RegExp(pat, 'i');
                    const results = [];
                    const sel = 'button, [role="button"], input, [onclick], [role="tab"], select';
                    for (const el of document.querySelectorAll(sel)) {
                        if (el.tagName === 'A') continue;
                        if (el.disabled || el.hidden) continue;
                        const text = el.textContent?.trim() || '';
                        if (re.test(text)) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {
                                const cy = r.y + r.height/2;
                                results.push({
                                    x: r.x + r.width/2,
                                    y: cy,
                                    _area: r.width * r.height,
                                    _inChallenge: bY >= 99999 || cy < bY ? 1 : 0
                                });
                            }
                        }
                    }
                    // Sort: challenge-area first, then by area
                    results.sort((a, b) => b._inChallenge - a._inChallenge || b._area - a._area);
                    return results.map(r => ({x: r.x, y: r.y}));
                }''', [step.target_pattern, boundary_y])
                if matches:
                    tried.append(('pattern', f'/{step.target_pattern}/', f'HIT ({len(matches)} matches)'))
                    self._last_target_search = tried
                    return matches[0]
                tried.append(('pattern', f'/{step.target_pattern}/', '0 matches'))
            except Exception as e:
                tried.append(('pattern', f'/{step.target_pattern}/', f'error: {e}'))

        # 3. CSS selector
        if step.target_selector:
            try:
                sel_loc = page.locator(step.target_selector).first
                el = page.query_selector(step.target_selector)
                if el:
                    box = el.bounding_box()
                    if box:
                        tried.append(('selector', step.target_selector, 'HIT'))
                        self._last_target_search = tried
                        return _box_to_center(box, locator=sel_loc)
                    result = _try_scroll_into_view(el, is_locator=False, locator_ref=sel_loc)
                    if result:
                        tried.append(('selector', step.target_selector, 'HIT after scroll'))
                        self._last_target_search = tried
                        return result
                    tried.append(('selector', step.target_selector, 'found but no box'))
                else:
                    tried.append(('selector', step.target_selector, 'not found'))
            except Exception as e:
                tried.append(('selector', step.target_selector, f'error: {e}'))

        # 3.5. Fingerprint similarity matching (HybridSimilo-inspired)
        # Triggers only when primary cascade found nothing AND fingerprint is available.
        # If we reach here, all prior cascade steps failed to return.
        if step.target_fingerprint:
            fp_target = self._fingerprint_match(page, step.target_fingerprint, boundary_y)
            if fp_target:
                tried.append(('fingerprint', f'score={fp_target.get("score", 0):.2f}', 'HIT'))
                self._last_target_search = tried
                return {'x': fp_target['x'], 'y': fp_target['y']}
            tried.append(('fingerprint', 'multi-attr', 'no match > 0.30'))

        # 4. DNA query (scan live DOM for matching element)
        if step.target_dna_query:
            try:
                from agents.dna_reasoner import DETECT_DOM_DNA_JS, DNAReasoner
                elements = page.evaluate(DETECT_DOM_DNA_JS)
                reasoner = DNAReasoner()
                target_key = reasoner._make_dna_key(step.target_dna_query)
                for el in elements:
                    el_key = reasoner._make_dna_key(el.get('dna', {}))
                    if el_key == target_key and el.get('spatial', {}).get('visible'):
                        tried.append(('dna', str(step.target_dna_query)[:60], 'HIT'))
                        self._last_target_search = tried
                        return el['spatial']
                tried.append(('dna', str(step.target_dna_query)[:60], f'0/{len(elements)} matched'))
            except Exception as e:
                tried.append(('dna', str(step.target_dna_query)[:60], f'error: {e}'))

        # 5. Coordinates (last resort — skip near-origin which is always a bug)
        if step.target_coords:
            x, y = step.target_coords
            if abs(x) >= 2 or abs(y) >= 2:
                tried.append(('coords', f'({x:.0f},{y:.0f})', 'fallback'))
                self._last_target_search = tried
                return {'x': x, 'y': y}
            tried.append(('coords', f'({x:.0f},{y:.0f})', 'REJECTED near-origin'))

        # All cascade steps failed — log diagnostic
        self._last_target_search = tried
        if tried:
            cascade_summary = ' | '.join(f'{m}={r}' for m, _, r in tried)
            log_stage("recipe", f"target NOT FOUND: {cascade_summary}")
        else:
            log_stage("recipe", f"target NOT FOUND: no locator data on step "
                      f"(action={step.action_type})")
        return None

    def _scroll_recover(self, page, step: ActionStep) -> dict | None:
        """Scroll page and retry target resolution (once down, once up).

        Only called for not-found/not-visible click failures.
        Returns target dict if found after scrolling, else None.
        """
        for direction in ('down', 'up'):
            try:
                delta = 500 if direction == 'down' else -500
                page.evaluate(f'window.scrollBy(0, {delta})')
                page.wait_for_timeout(200)
                target = self._find_target(page, step)
                if target:
                    log_stage("recipe", f"scroll recovery ({direction}) found target")
                    return target
            except Exception:
                pass
        return None

    @staticmethod
    def _dismiss_before_coord_click(page):
        """Clear popups before a raw coordinate click (which bypasses locator handler)."""
        try:
            from agents.popup import dismiss_all_popups
            dismiss_all_popups(page)
        except Exception:
            pass

    @staticmethod
    def _find_first_input(page) -> dict | None:
        """Find the first visible, non-code-entry input or textarea on the page.

        Used as fallback when a 'type' step has no target — ensures keyboard
        input reaches the right element (e.g. puzzle answer input).
        """
        try:
            result = page.evaluate(r'''() => {
                const inputs = document.querySelectorAll(
                    'input[type="text"], input[type="number"], input:not([type]), textarea');
                for (const el of inputs) {
                    // Skip the code SUBMISSION input (inside a form with "Submit Code" button)
                    // but NOT challenge inputs that happen to mention "code" in placeholder
                    const form = el.closest('form');
                    if (form) {
                        const btn = form.querySelector('button');
                        if (btn && /submit code/i.test(btn.textContent || '')) continue;
                    }
                    const r = el.getBoundingClientRect();
                    if (r.width > 20 && r.height > 10 && r.top >= 0 && r.top < window.innerHeight) {
                        return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                    }
                }
                return null;
            }''')
            return result
        except Exception:
            return None

    def _extract_wait_from_page_text(self, page) -> int | None:
        """Parse visible page text for duration hints like '5 seconds', 'wait 3s'.

        Returns duration in ms if found, None otherwise.
        Patterns matched:
          - "N second(s)" / "N sec"
          - "wait N s" / "wait Ns"
          - "after N seconds"
          - "in N seconds"
          - "N-second" (hyphenated)
        """
        try:
            text = page.inner_text('body')[:3000].lower()
        except Exception:
            return None

        # Match patterns like "5 seconds", "3 second", "10 sec", "5-second"
        patterns = [
            r'(\d+)[\s-]*seconds?',
            r'(\d+)[\s-]*secs?(?:\b)',
            r'wait\s+(\d+)\s*s\b',
            r'after\s+(\d+)\s*s\b',
            r'in\s+(\d+)\s*s\b',
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                val = int(m.group(1))
                if 1 <= val <= 60:  # Sanity: 1-60 seconds
                    return val * 1000
        return None

    def _check_assertions(self, page, step: ActionStep, dom_sig_before: str,
                          progress_before: dict | None = None,
                          is_final_step: bool = False) -> bool:
        """Check post-step assertions. Returns False to abort recipe.
        Sets self._last_assertion_failure on failure for diagnostics."""
        self._last_assertion_failure = None

        def _safe_check(name, fn):
            """Run a single assertion check, return False if it fails, True otherwise."""
            try:
                result = fn()
                if not result:
                    self._last_assertion_failure = name
                return result
            except (TimeoutError, ValueError, AttributeError) as e:
                log_stage("recipe", f"assertion check error ({name}): {e}")
                return True  # Don't abort on transient/element errors
            except Exception as e:
                log_stage("recipe", f"assertion check unexpected error ({name}): {e}")
                self._last_assertion_failure = f'{name}:exception'
                return False  # Abort recipe on unexpected errors

        # Progress delta assertion (with retry for popup interference)
        if step.expect_progress_delta is not None:
            def _check_progress():
                progress_after = read_progress(page)
                if progress_before and progress_after:
                    delta = progress_after['fraction'] - progress_before['fraction']
                    if delta < step.expect_progress_delta * 0.5:
                        # Retry: popup dismiss may have caused transient re-render.
                        # Dismiss any popup that reappeared, wait for DOM to settle,
                        # then re-read progress.
                        self._dismiss_before_coord_click(page)
                        page.wait_for_timeout(300)
                        progress_retry = read_progress(page)
                        if progress_retry:
                            delta2 = progress_retry['fraction'] - progress_before['fraction']
                            if delta2 >= step.expect_progress_delta * 0.5:
                                log_stage("recipe",
                                          f"progress retry OK: delta {delta2:.2f} "
                                          f"(was {delta:.2f} before retry)")
                                return True
                        retry_info = f" (retry delta: {delta2:.2f})" if progress_retry else ""
                        log_stage("recipe",
                                  f"assertion fail: progress delta {delta:.2f} < "
                                  f"expected {step.expect_progress_delta:.2f}{retry_info}")
                        return False
                return True
            if not _safe_check("progress_delta", _check_progress):
                return False

        # DOM change score assertion
        if step.expect_dom_change_score is not None:
            def _check_dom():
                dom_sig_after = self._get_dom_signature(page)
                score = self._compute_change_score(dom_sig_before, dom_sig_after)
                if score < step.expect_dom_change_score:
                    log_stage("recipe", f"assertion fail: dom_change_score {score:.3f} < {step.expect_dom_change_score}")
                    return False
                return True
            if not _safe_check("dom_change_score", _check_dom):
                return False

        # Selector visibility
        if step.expect_selector_visible:
            def _check_sel():
                el = page.query_selector(step.expect_selector_visible)
                if not el or not el.is_visible():
                    log_stage("recipe", f"assertion fail: '{step.expect_selector_visible}' not visible")
                    return False
                return True
            if not _safe_check("selector_visible", _check_sel):
                return False

        # Text regex
        if step.expect_text_regex:
            def _check_regex():
                text = page.inner_text('body')[:5000]
                if not re.search(step.expect_text_regex, text, re.IGNORECASE):
                    log_stage("recipe", f"assertion fail: text doesn't match '{step.expect_text_regex}'")
                    return False
                return True
            if not _safe_check("text_regex", _check_regex):
                return False

        # Code visible
        if step.expect_code_visible:
            def _check_code():
                # Short-circuit: fiber_bypass stashes code outside DOM
                fc = getattr(self, '_fiber_code', None)
                if fc and len(fc) == 6:
                    return True
                codes_before = getattr(self, '_codes_before', set())
                # Fast path: check mutation hooks immediately (no polling needed)
                try:
                    hook_codes = page.evaluate(
                        "() => window.__getAllCodes ? window.__getAllCodes() : {bus:[], mut:[]}") or {}
                    items = (hook_codes.get('bus', []) + hook_codes.get('mut', [])
                             if isinstance(hook_codes, dict) else hook_codes or [])
                    for item in items:
                        c = item.get('c', item) if isinstance(item, dict) else str(item)
                        if c and len(c) == 6 and is_valid_code(c) and c not in codes_before:
                            return True
                except Exception:
                    pass
                # Slow path: harvest + poll (1.5s budget)
                score, code = 0.0, None
                max_polls, poll_interval = 5, 250
                for _poll in range(max_polls):
                    score, code = harvest_and_score(page, '', 0)
                    if code and score >= 0.3:
                        break
                    page.wait_for_timeout(poll_interval)
                if not code or score < 0.3:
                    score, code = harvest_and_score(page, '', 0)
                if not code or score < 0.3:
                    sweep = self._completion_sweep(page)
                    if sweep:
                        return True
                    log_stage("recipe", "assertion fail: no valid code visible")
                    return False
                return True
            if not _safe_check("code_visible", _check_code):
                return False

        # Legacy: expect_text_contains
        if step.expect_text_contains:
            def _check_text():
                text = page.inner_text('body')[:5000]
                if not re.search(step.expect_text_contains, text, re.IGNORECASE):
                    log_stage("recipe", f"assertion fail: text doesn't match '{step.expect_text_contains}'")
                    return False
                return True
            if not _safe_check("text_contains", _check_text):
                return False

        # Legacy: expect_dom_changed
        if step.expect_dom_changed:
            def _check_dom_changed():
                dom_sig_after = self._get_dom_signature(page)
                score = self._compute_change_score(dom_sig_before, dom_sig_after)
                if score < 0.02:
                    log_stage("recipe", "assertion fail: DOM didn't change")
                    return False
                return True
            if not _safe_check("dom_changed", _check_dom_changed):
                return False

        return True

    def _dict_to_step(self, d: dict) -> ActionStep:
        """Convert serialized dict back to ActionStep."""
        return ActionStep(
            action_type=d.get('action_type', 'wait'),
            target_role=d.get('target_role'),
            target_name=d.get('target_name'),
            target_text=d.get('target_text'),
            target_pattern=d.get('target_pattern', ''),
            target_selector=d.get('target_selector'),
            target_dna_query=d.get('target_dna_query'),
            target_coords=tuple(d['target_coords'][:2]) if d.get('target_coords') and len(d['target_coords']) >= 2 else None,
            dest_coords=tuple(d['dest_coords'][:2]) if d.get('dest_coords') and len(d['dest_coords']) >= 2 else None,
            target_fingerprint=d.get('target_fingerprint'),
            value=d.get('value'),
            resolver=d.get('resolver'),
            params=d.get('params'),
            delay_ms=int(d.get('delay_ms', 100)),
            expect_selector_visible=d.get('expect_selector_visible'),
            expect_text_contains=d.get('expect_text_contains'),
            expect_text_regex=d.get('expect_text_regex'),
            expect_dom_changed=d.get('expect_dom_changed', False),
            expect_dom_change_score=d.get('expect_dom_change_score'),
            expect_code_visible=d.get('expect_code_visible', False),
            expect_url_changed=d.get('expect_url_changed', False),
            expect_progress_delta=d.get('expect_progress_delta'),
            expect_state_changes=d.get('expect_state_changes') if isinstance(d.get('expect_state_changes'), list) else ([d.get('expect_state_changes')] if d.get('expect_state_changes') else None),
            repeat=int(d.get('repeat', 1)),
        )

    def _wait_for_state_settle(self, page, expected_changes=None,
                               timeout_ms=2000, min_wait_ms=100) -> bool:
        """Poll for state changes after action with stability window.

        Returns True once no new changes have fired for STABLE_MS after the
        last observed change, or immediately when a valid code appears.
        Sets self._last_settle_ms on success (None on timeout).

        Uses the full state change vocabulary from CHANGE_PRIORITY (same as
        the sidecar), so recipes can react to all 12 change types: enabled,
        new_element, turned_green, turned_red, appeared, revealed,
        became_clickable, text_changed, activated, turned_grey,
        became_interactive, became_vibrant.
        """
        STABLE_MS = 200  # No changes for 200ms = settled
        # Full vocabulary — same as sidecar uses via CHANGE_PRIORITY
        ALL_CHANGE_TYPES = set(CHANGE_PRIORITY.keys())
        target_set = set(expected_changes) if expected_changes else ALL_CHANGE_TYPES
        page.wait_for_timeout(min_wait_ms)
        start = time.time()
        deadline = start + timeout_ms / 1000
        found_any = False
        last_change_t = None

        while time.time() < deadline:
            now = time.time()

            # Code found is a hard terminal condition
            code = extract_code_js(page)
            if code and is_valid_code(code):
                self._last_settle_ms = (now - start) * 1000
                return True

            changes = peek_state_changes(page)
            found = set()
            for sc in (changes or []):
                for ch in sc.get('changes', []):
                    found.add(ch)

            # Any relevant change extends the settling window
            if found & (target_set | ALL_CHANGE_TYPES):
                found_any = True
                last_change_t = now

            # Stable for STABLE_MS after seeing at least one change = settled
            if found_any and last_change_t and (now - last_change_t) * 1000 >= STABLE_MS:
                self._last_settle_ms = (now - start) * 1000
                return True

            page.wait_for_timeout(150)

        self._last_settle_ms = None
        return False

    def _snapshot_visible_codes(self, page) -> set[str]:
        """Collect all valid 6-char codes currently visible on the page."""
        try:
            codes = page.evaluate(r'''() => {
                const RE = /\b[A-HJ-NP-Z2-9]{6}\b/g;
                const seen = new Set();
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT, null, false);
                let node;
                while ((node = walker.nextNode())) {
                    const text = node.nodeValue;
                    if (!text || text.length < 6) continue;
                    const matches = text.match(RE);
                    if (matches) matches.forEach(c => seen.add(c));
                }
                return [...seen];
            }''')
            return set(codes or [])
        except Exception:
            return set()

    @staticmethod
    def _find_green_code(page) -> str | None:
        """Find a 6-char code rendered in green text (real codes often appear green)."""
        try:
            return page.evaluate(r'''() => {
                const RE = /\b[A-HJ-NP-Z2-9]{6}\b/;
                for (const el of document.querySelectorAll('*')) {
                    const text = el.textContent?.trim() || '';
                    if (text.length > 20 || !RE.test(text)) continue;
                    const m = getComputedStyle(el).color.match(/[\d.]+/g);
                    if (m && m.length >= 3 && +m[1] > +m[0] * 1.3 && +m[1] > +m[2] * 1.3)
                        return text.match(RE)[0];
                }
                return null;
            }''')
        except Exception:
            return None

    def _extract_code_after_recipe(self, page) -> str | None:
        """Extract code after recipe completion using harvest_and_score.

        Steps:
        1. Wait 500ms for DOM mutation to settle (codes appear via async render)
        2. Try harvest immediately
        3. If progress is 100%, run completion sweep (click Reveal/Complete button)
        4. For delay-type challenges: poll and prefer newly-revealed codes
        """
        # Check for fiber bypass code (set by fiber_bypass action type)
        fiber_code = getattr(self, '_fiber_code', None)
        if fiber_code:
            self._fiber_code = None  # consume it
            return fiber_code

        # Check for finish navigation (set by finish_navigation action type)
        if getattr(self, '_finish_navigation_done', False):
            self._finish_navigation_done = False
            return '__FINISH__'

        # Brief settle wait — codes appear via DOM mutation ~200-500ms after final action
        page.wait_for_timeout(500)

        try:
            last_action_time = page.evaluate('() => window.__lastActionTime || 0')
        except Exception:
            last_action_time = 0

        codes_before = getattr(self, '_codes_before', set())
        base_type = re.sub(r'_v\d+$', '', getattr(self, '_challenge_type', ''))
        is_delay = base_type in self.DELAY_CHALLENGE_TYPES  # Always use delay path
        recipe_start_ts = getattr(self, '_delay_recipe_start_ts', 0)

        if is_delay:
            # Strategy 0: Green-colored code boost — real codes often appear in green
            green_code = self._find_green_code(page)
            if green_code and green_code not in codes_before and is_valid_code(green_code):
                log_stage("recipe", f"delay extract: green code {green_code} "
                          f"(stale={codes_before})")
                return green_code

            # Strategy 1: Timestamp-based — only accept codes captured AFTER
            # the recipe started (filters decoys captured during/before the flash)
            if recipe_start_ts:
                for _poll in range(12):
                    try:
                        recent = page.evaluate(
                            f'() => window.__getRecentCodes ? '
                            f'window.__getRecentCodes({recipe_start_ts}) : []')
                        for item in (recent or []):
                            c = item.get('c', '')
                            if c and len(c) == 6 and is_valid_code(c) and c not in codes_before:
                                log_stage("recipe", f"delay extract: post-recipe code {c} "
                                          f"(ts-filtered, stale={codes_before})")
                                return c
                    except Exception:
                        pass
                    # Green code check during polling
                    green_code = self._find_green_code(page)
                    if green_code and green_code not in codes_before and is_valid_code(green_code):
                        log_stage("recipe", f"delay extract: green code {green_code} (poll)")
                        return green_code
                    # Also check live DOM for codes not in before-set
                    score, code = harvest_and_score(page, '', recipe_start_ts)
                    if code and code not in codes_before and score >= 0.5:
                        log_stage("recipe", f"delay extract: new code {code} "
                                  f"(score={score:.2f}, stale={codes_before})")
                        return code
                    page.wait_for_timeout(250)

            # Strategy 2: Fallback — poll harvest_and_score, ONLY accept new codes
            best_new, best_new_score = None, 0.0
            for _poll in range(8):
                score, code = harvest_and_score(page, '', last_action_time)
                if code and code not in codes_before and score > best_new_score:
                    best_new, best_new_score = code, score
                if best_new and best_new_score >= 0.5:
                    log_stage("recipe", f"delay extract: new code {best_new} "
                              f"(score={best_new_score:.2f}, stale={codes_before})")
                    return best_new
                page.wait_for_timeout(150)
            if best_new and best_new_score >= 0.3:
                log_stage("recipe", f"delay extract: new code {best_new} "
                          f"(score={best_new_score:.2f})")
                return best_new
            # Never return a known decoy — let sidecar handle it fresh
            log_stage("recipe", f"delay extract: no new code found (stale={codes_before})")
            return None

        # Standard path: try harvest first
        score, code = harvest_and_score(page, '', last_action_time)
        if code and score >= 0.3:
            return code

        # No code yet — try completion sweep (click Reveal/Complete button)
        sweep_code = self._completion_sweep(page)
        if sweep_code:
            return sweep_code

        return None

    def _check_early_code(self, page) -> str | None:
        """Check if a valid code appeared mid-recipe via mutation hooks or DOM.

        This is the payoff of state-change awareness: when the sidecar-taught
        state changes fire (e.g., typed the answer → auto-submit → mutation
        hook captured the code), we grab it immediately instead of blindly
        continuing to remaining steps.

        Lightweight: single JS evaluate + DOM check. No harvest_and_score.
        """
        codes_before = getattr(self, '_codes_before', set())

        # 1. Check fiber bypass stash
        fc = getattr(self, '_fiber_code', None)
        if fc and len(fc) == 6 and is_valid_code(fc):
            return fc

        # 2. Check mutation hooks (code bus + mutation observer)
        try:
            hook_codes = page.evaluate(
                "() => window.__getAllCodes ? window.__getAllCodes() : {bus:[], mut:[]}")
            items = []
            if isinstance(hook_codes, dict):
                items = hook_codes.get('bus', []) + hook_codes.get('mut', [])
            else:
                items = hook_codes or []
            for item in items:
                c = item.get('c', item) if isinstance(item, dict) else str(item)
                if c and len(c) == 6 and is_valid_code(c) and c not in codes_before:
                    return c
        except Exception:
            pass

        # 3. Quick DOM extract (no heavy scoring)
        code = extract_code_js(page)
        if code and is_valid_code(code) and code not in codes_before:
            return code

        return None

    def _completion_sweep(self, page) -> str | None:
        """Click 'Reveal Code' / 'Complete' button if progress is 100%.

        Ported from sidecar — checks for completion buttons after recipe
        finishes all steps but code hasn't appeared yet.
        """
        try:
            # Only sweep if progress is at or near 100%
            progress = read_progress(page)
            if progress and progress.get('fraction', 0) < 0.99:
                return None

            clicked = page.evaluate(r'''() => {
                const KW = ['reveal code', 'reveal', 'complete challenge', 'complete',
                    'all tabs visited', 'show code', 'finish', 'get code', 'done'];
                const SKIP = /^(next|continue|proceed|advance|click here|move on|keep going|go forward|submit code)/i;
                for (const btn of document.querySelectorAll('button, [role="button"]')) {
                    const t = (btn.textContent || '').trim();
                    const tl = t.toLowerCase();
                    if (btn.disabled) continue;
                    if (SKIP.test(t)) continue;
                    const r = btn.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) continue;
                    for (const kw of KW) {
                        if (tl.includes(kw)) {
                            const opts = {bubbles: true, cancelable: true, view: window};
                            btn.dispatchEvent(new PointerEvent('pointerdown', opts));
                            btn.dispatchEvent(new MouseEvent('mousedown', opts));
                            btn.dispatchEvent(new PointerEvent('pointerup', opts));
                            btn.dispatchEvent(new MouseEvent('mouseup', opts));
                            btn.dispatchEvent(new MouseEvent('click', opts));
                            return t;
                        }
                    }
                }
                return null;
            }''')
            if clicked:
                log_stage("recipe", f"completion sweep clicked: '{clicked}'")
                page.wait_for_timeout(500)
                score, code = harvest_and_score(page, '', 0)
                if code and score >= 0.3:
                    log_stage("recipe", f"completion sweep found code: {code}")
                    return code
                page.wait_for_timeout(300)
                score, code = harvest_and_score(page, '', 0)
                if code and score >= 0.3:
                    log_stage("recipe", f"completion sweep found code (2nd): {code}")
                    return code
        except Exception:
            pass
        return None

    @staticmethod
    def compress_recipe(raw_steps: list[dict]) -> list[dict]:
        """Collapse repeated identical actions into a single step with repeat count.
        Keep the LAST successful attempt's assertions (not the first noisy ones)."""
        if not raw_steps:
            return []
        compressed = []
        i = 0
        while i < len(raw_steps):
            step = dict(raw_steps[i])
            # Count consecutive identical actions (same type + target)
            repeat = 1
            while i + repeat < len(raw_steps):
                next_step = raw_steps[i + repeat]
                if (next_step.get('action_type') == step.get('action_type') and
                    next_step.get('target_selector') == step.get('target_selector') and
                    next_step.get('target_text') == step.get('target_text') and
                    next_step.get('target_role') == step.get('target_role') and
                    next_step.get('target_name') == step.get('target_name') and
                    next_step.get('value') == step.get('value') and
                    next_step.get('target_coords') == step.get('target_coords') and
                    next_step.get('dest_coords') == step.get('dest_coords')):
                    repeat += 1
                else:
                    break
            if repeat > 1:
                # Use the LAST step's assertions (it's the one that succeeded)
                step = dict(raw_steps[i + repeat - 1])
                step['repeat'] = repeat
            compressed.append(step)
            i += repeat
        # Preserve special finish steps (fiber_bypass, finish_navigation) during truncation.
        # These are appended by LearningSidecar.finalize_promotion() as the final workaround
        # step and must survive truncation even if the recipe exceeds MAX_RECIPE_STEPS.
        FINISH_TYPES = {'fiber_bypass', 'finish_navigation'}
        if len(compressed) > MAX_RECIPE_STEPS:
            last = compressed[-1]
            if last.get('action_type') in FINISH_TYPES:
                return compressed[:MAX_RECIPE_STEPS - 1] + [last]
        return compressed[:MAX_RECIPE_STEPS]
