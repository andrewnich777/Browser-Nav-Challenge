"""Orchestrator V4 — Agent-first pipeline.

Phase 1: V4 deterministic agents (27 challenge types + universal agents)
Phase 2: Passive checks (observers, DNA, harvest)
Phase 3: System 2 sidecar (VisionLearning)
"""

import base64
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from config import BASE_URL, STEP_TIMEOUT, FINAL_STEP, get_challenge_type, CHARSET, generate_code
from page_state import close_extra_tabs, get_body_text, get_challenge_text
from verify import is_finish_page
from log import log, log_stage, start_popup_batch, flush_popup_batch
from agents import ALL_AGENTS
from init_hooks import get_init_script
from code_scorer import (
    harvest_and_score, is_valid_code, DECOY_CODES,
    reset_code_tracker, reset_state_change_tracker,
)
from knowledge_reader import (
    get_knowledge_reader, KnowledgeReader, detect_challenge_type,
)
from agent_tracker import get_agent_tracker
from agents.dna_reasoner import DNAReasoner
from agents.recipe_executor import RecipeExecutor
from agents.vision_learning import VisionLearningAgent
from agents.learning_sidecar import LearningSidecar
from primitives import (
    extract_code_js, read_progress,
    reset_state_watch,
)
from agents.v4 import UNIVERSAL_AGENTS, CHALLENGE_AGENTS, StepCtx
from agents.v4.final_step_hook import wrap_final_step


@dataclass
class StepContextSnapshot:
    """Immutable snapshot of page state captured at step start, before any actions.

    Threaded through System 1 and System 2 so that fingerprints, screenshots,
    and instruction text always reflect the pristine challenge state — never
    the post-solve or post-advance state.
    """
    fingerprint: dict = field(default_factory=dict)   # DOM_FINGERPRINT_JS result
    instruction_text: str = ''                         # get_challenge_text() normalized
    screenshot_b64: str = ''                           # PNG screenshot for AI review
    text_ctx: dict = field(default_factory=dict)       # _extract_text_context() result
    dom_signals: list = field(default_factory=list)    # Structural DOM features for detection
    step: int = 0
    timestamp: float = 0.0


# Fast-path routing for steps 1-15 (instruction text → challenge type)
# Checked in order — more specific patterns first to avoid false positives.
FAST_ROUTES_ORDERED = [
    ('drag_drop', ['drag and drop', 'drag the', 'drop zone']),
    ('hover', ['hover over', 'hover on', 'hover the', 'mouse over']),
    ('audio', ['play audio', 'listen to', 'play the audio']),
    ('keyboard_sequence', ['press ctrl', 'press shift', 'key combination',
                          'keyboard sequence', 'required sequence',
                          'control+', 'keys in sequence']),
    ('gesture', ['draw a', 'swipe', 'gesture', 'draw on the canvas']),
    ('decode', ['decode:', 'base64', 'rot13', 'cipher']),
    ('timing', ['capture while', 'click before it disappears', 'click while',
                'rotating code', 'click "capture"', "click 'capture'"]),
    ('scroll', ['scroll down at least', 'scroll to reveal', 'px to reveal']),
    ('hidden_dom', ['hidden dom challenge', 'hidden somewhere', 'find the hidden',
                    'inspect the dom', 'click here 3', 'click here 5',
                    'times to reveal']),
    ('delay_memory', ['memorize', 'remember the code', 'did you see', 'memory challenge',
                      'code will flash', 'i remember']),
    ('delayed_reveal', ['will appear in', 'will be revealed', 'after.*seconds',
                        'timer', 'countdown']),
    ('split_parts', ['collect the parts', 'gather', 'find all parts', 'scattered',
                     'click all', 'parts scattered', 'split parts']),
    ('calculated', ['calculate', 'what is', 'compute', 'evaluate the expression']),
    ('puzzle_solve', ['puzzle challenge', 'solve this puzzle', 'jigsaw', 'arrange the pieces']),
    ('video', ['play the video', 'seek through', 'video player']),
    ('sequence', ['complete all', 'sub-task', 'series of tasks']),
    ('mutation', ['trigger.*mutation', 'observe.*mutation', 'mutation challenge']),
    ('service_worker', ['service worker', 'register.*service', 'retrieve from cache']),
    ('websocket', ['websocket', 'web socket']),
    ('shadow_dom', ['shadow dom', 'shadow level', 'nested layers']),
    ('recursive_iframe', ['recursive iframe', 'nested levels', 'deepest level']),
    ('multi_tab', ['multi-tab', 'multi tab', 'visit all', 'tabs to visit']),
    ('click_reveal', ['click to reveal', 'click the button to', 'reveal the code']),
]

# Time budgets (ms) per step category
BUDGET_SIMPLE = 1500      # steps 1-15
BUDGET_STANDARD = 2500    # steps 16-29
BUDGET_COMPLEX = 5000     # drag_drop, video, shadow_dom, websocket, service_worker
BUDGET_STEP30 = 8000      # step 30 special handlers

COMPLEX_TYPES = {'drag_drop', 'video', 'shadow_dom', 'websocket',
                 'service_worker', 'recursive_iframe'}


class Orchestrator:
    """V4 orchestrator: agent-first pipeline.

    Phase 1: V4 deterministic agents →
    Phase 2: Passive checks → Phase 3: System 2 sidecar
    """

    # Challenge types that are cumulative (don't minimize)
    CUMULATIVE_TYPES = {'mutation', 'sequence', 'sequence_challenge'}

    def __init__(self):
        self.a = ALL_AGENTS
        self.dna_reasoner = DNAReasoner()
        self.recipe_executor = RecipeExecutor()
        self._vision_learner = VisionLearningAgent()

        # V2 learning system: canonical learnings + agent tracker
        self.knowledge_reader = get_knowledge_reader()
        self.agent_tracker = get_agent_tracker()
        self.knowledge_reader.set_agent_tracker(self.agent_tracker)
        self.knowledge_reader.reset_disabled()

        knowledge_stats = self.knowledge_reader.get_stats()
        if knowledge_stats['total_learnings'] > 0:
            log(f"Knowledge: {knowledge_stats['total_learnings']} learnings, "
                f"{knowledge_stats['total_variants']} variants, "
                f"{knowledge_stats['verified']} verified")

        self._sidecar = LearningSidecar(
            vision_learner=self._vision_learner,
            recipe_executor=self.recipe_executor,
            dna_reasoner=self.dna_reasoner,
            knowledge_reader=self.knowledge_reader,
        )
        self._last_sidecar_result = None

        self.previously_used_codes = set()

        self.metrics = {
            'steps_attempted': 0,
            'steps_succeeded': 0,
            'vision_calls': 0,
            'v4_hits': 0,
            'passive_hits': 0,
            'system2_successes': 0,
            'promotions_created': 0,
            'raf_waits': 0,
            'codes_from_observers': 0,
            'codes_from_extraction': 0,
        }
        self._step_times = []  # Per-step timing for median calc
        self._failed_codes = set()  # Codes rejected this step (init for safety)
        self._step_start_time = time.time()  # Init for safety
        self._cached_challenge_type = None
        self._prev_step_type = None  # Track previous step's type for calculated detection

        # Failure diagnostics
        self._run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._failures_dir = Path("failures") / self._run_id
        self._step_failures = []  # Structured failure records for post-run report

    def get_full_metrics(self) -> dict:
        """Combine all metrics."""
        all_metrics = dict(self.metrics)
        all_metrics['success_rate'] = (
            self.metrics['steps_succeeded'] / max(1, self.metrics['steps_attempted'])
        )
        if self._step_times:
            sorted_times = sorted(self._step_times)
            mid = len(sorted_times) // 2
            if len(sorted_times) % 2 == 0 and mid > 0:
                all_metrics['median_step_time'] = (sorted_times[mid - 1] + sorted_times[mid]) / 2
            else:
                all_metrics['median_step_time'] = sorted_times[mid]
        else:
            all_metrics['median_step_time'] = 0.0
        all_metrics['knowledge_stats'] = self.knowledge_reader.get_stats()
        all_metrics['agent_performance'] = self.agent_tracker.get_performance_summary()

        # Token usage from vision learner (safe getattr with 0 defaults)
        all_metrics['total_input_tokens'] = getattr(
            self._vision_learner, '_total_input_tokens', 0)
        all_metrics['total_output_tokens'] = getattr(
            self._vision_learner, '_total_output_tokens', 0)

        return all_metrics

    # ── Page setup ────────────────────────────────────────────────────────────

    def start_fresh(self, browser, version_hint=None):
        """Create fresh page, click START, return (page, version).

        Configures Playwright 1.57 features: emulate_media, add_locator_handler,
        add_init_script (WebSocket/Fetch/XHR/mutation observers).
        """
        from config import VIEWPORT
        from agents.popup import dismiss_all_popups, CLEAR_BLOCKERS_JS
        log("start_fresh: launching new page")
        page = browser.new_page(viewport=VIEWPORT)

        page.emulate_media(reduced_motion="reduce")

        def auto_dismiss_overlays(overlay):
            for _ in range(8):
                if not dismiss_all_popups(page):
                    break
            try:
                page.evaluate(CLEAR_BLOCKERS_JS)
            except Exception:
                pass

        # Trigger locator: match fixed-position overlays (popups) but NOT the
        # permanent header bar (z-[10000], class top-0).  Using .first avoids
        # strict-mode violations when multiple popups are present simultaneously.
        page.add_locator_handler(
            page.locator(
                ":is(.fixed, [style*='position: fixed'])"
                ":not(.top-0):not([class*='bg-black/80'])"
            ).first,
            auto_dismiss_overlays,
            no_wait_after=True,
        )

        page.add_init_script(get_init_script())

        # Network/console scanning removed (Session 22b) — non-human-like.
        # Labeled exceptions (websocket, service_worker) use JS-side hooks
        # in exception_hooks.py which feed into the codeBus.

        page.goto(BASE_URL)
        page.wait_for_load_state('networkidle')

        hooks_ok = page.evaluate('() => window.__hooksInstalled === true')
        if hooks_ok:
            log("start_fresh: hooks installed successfully")
        else:
            hook_error = page.evaluate('() => window.__hookError || "unknown"')
            log(f"start_fresh: WARNING - hooks failed: {hook_error}")

        page.evaluate("c => { window.__CHARSET = c; }", CHARSET)
        page.evaluate("d => { window.__DECOY_CODES = new Set(d); }", sorted(DECOY_CODES))

        page.click('button:has-text("START")')
        try:
            page.wait_for_url(lambda url: url != BASE_URL, timeout=3000)
        except Exception:
            page.wait_for_timeout(500)

        from agents.popup import POPUP_AUTO_DISMISS_SETUP_JS
        auto_status = page.evaluate(POPUP_AUTO_DISMISS_SETUP_JS)
        log(f"start_fresh: popup auto-dismiss: {auto_status}")

        m = re.search(r'version=(\d+)', page.url)
        version = int(m.group(1)) if m else 1
        log(f"start_fresh: got version={version}")
        return page, version

    # ── Utility methods (kept from V2) ───────────────────────────────────────

    def wait_for_raf_stable(self, page, timeout_ms: int = 2000) -> bool:
        """Wait for React to finish rendering via RAF stability check."""
        self.metrics['raf_waits'] += 1
        try:
            return page.evaluate(f'''() => new Promise(resolve => {{
                let last = 0, stable = 0;
                const deadline = Date.now() + {timeout_ms};
                function tick() {{
                    const h = document.body?.innerText?.length || 0;
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
            return False

    def _dismiss_popups(self, page):
        """Dismiss popups using Playwright mouse clicks."""
        from agents.popup import dismiss_all_popups, CLEAR_BLOCKERS_JS
        try:
            for _ in range(8):
                if not dismiss_all_popups(page):
                    break
            page.evaluate(CLEAR_BLOCKERS_JS)
        except Exception:
            pass

    def _is_error_page(self, page) -> bool:
        """Check if current page is a 404 or error page."""
        try:
            title = page.title().lower()
            if 'not found' in title or '404' in title or 'error' in title:
                return True
            body = page.inner_text('body')[:500].lower()
            if any(x in body for x in ['page not found', "doesn't exist", '404', 'not found']):
                return True
        except Exception:
            pass
        return False

    def _reinject_validation(self, page):
        """Re-inject CHARSET, DECOY_CODES, used codes after page.goto()."""
        try:
            page.evaluate("c => { window.__CHARSET = c; }", CHARSET)
            page.evaluate("d => { window.__DECOY_CODES = new Set(d); }", sorted(DECOY_CODES))
            if self.previously_used_codes:
                page.evaluate("u => { window.__usedCodes = new Set(u); }",
                              sorted(self.previously_used_codes))
        except Exception:
            pass

    def _recover_from_navigation(self, page, step, version):
        """Attempt to recover from accidental navigation away from step URL."""
        target_url = f'{BASE_URL}/step{step}?version={version}'
        current = page.url
        if current == target_url or (f'/step{step}' in current and f'version={version}' in current):
            return  # Already on correct URL
        log(f"step {step}: navigation recovery needed — current URL: {current}")
        # Cannot safely navigate in SPA without risking white screen.
        # Let the step fail and orchestrator will handle it.
        log(f"step {step}: cannot recover from {current}, step will fail")

    def _submit_and_wait(self, page, code, step, version, current_url, timeout=1500) -> bool:
        """Submit code and wait for URL change."""
        start_popup_batch()
        self._dismiss_popups(page)
        flush_popup_batch()
        agent = self.a.get("code_entry")
        if agent:
            agent.run(page, step, version, code=code)
        try:
            page.wait_for_url(lambda url: url != current_url, timeout=timeout)
            return True
        except Exception:
            return False

    def _check_code_observers(self, page, step: int) -> str | None:
        """Check always-on observers (WebSocket/mutation) for captured codes."""
        try:
            all_codes = page.evaluate(
                '() => window.__getAllCodes ? window.__getAllCodes() : {bus: [], mut: []}'
            )
            for item in all_codes.get('bus', []) + all_codes.get('mut', []):
                code = item.get('c', '')
                if is_valid_code(code) and code not in self._failed_codes:
                    log(f"step {step}: observer captured code {code}")
                    self.metrics['codes_from_observers'] += 1
                    return code
        except Exception:
            pass
        return None

    def _extract_page_info(self, page) -> dict:
        """Extract structured page info (instruction, buttons, interactives)."""
        try:
            return page.evaluate(r'''() => {
                const instruction = (document.querySelector('h1, h2, [class*="instruction"], [class*="challenge"]')
                                     || document.querySelector('p'))?.innerText?.trim() || '';
                const buttons = Array.from(document.querySelectorAll('button')).map(b => ({
                    text: (b.innerText || '').trim().substring(0, 30),
                    tag: 'button'
                }));
                const interactives = Array.from(document.querySelectorAll(
                    'input, select, textarea, canvas, audio, [role="slider"], [draggable="true"]'
                )).map(el => ({
                    tag: el.tagName.toLowerCase(),
                    type: el.type || '',
                }));
                return { instruction, buttons, interactives };
            }''')
        except Exception:
            return {"instruction": "", "buttons": [], "interactives": []}

    # ── The V3 pipeline: run_step ────────────────────────────────────────────

    def run_step(self, page, step: int, version: int) -> tuple[bool, list[str]]:
        """Solve one step via lean pipeline:
        StepSetup → Passive → Sidecar (1 call, bounded) → Submit → Promote.

        Returns (success: bool, agents_used: list[str]).
        """
        self.metrics['steps_attempted'] += 1
        agents_used = []
        self._step_start_time = time.time()

        try:
            return self._run_step_inner(page, step, version, agents_used)
        finally:
            self._step_times.append(time.time() - self._step_start_time)

    def _run_step_inner(self, page, step: int, version: int,
                        agents_used: list[str]) -> tuple[bool, list[str]]:
        """Inner step logic (wrapped by run_step's try/finally for timing)."""

        # Phase 0: Setup
        # Reset scoring trackers so codes/state from previous step don't bleed
        reset_code_tracker()
        reset_state_change_tracker()

        current_url = self._step_setup(page, step, version)
        if current_url is None:
            return False, agents_used

        # Cache challenge type for this step (avoid 6 redundant lookups)
        self._cached_challenge_type = get_challenge_type(step, version)

        # Phase 1: V4 Agents (deterministic, no API cost)
        code = self._system1_v4(page, step, version)
        if code == "__FINISH__":
            # Final step hook reached /finish — challenge complete, no code to submit
            agents_used.append('v4_agent')
            agents_used.append('finish_hook')
            self.metrics['v4_hits'] += 1
            self.metrics['steps_succeeded'] = self.metrics.get('steps_succeeded', 0) + 1
            return True, agents_used
        if code:
            agents_used.append('v4_agent')
            success, _reason = self._submit_and_record(page, code, step, version,
                                                       current_url, 'v4_agent')
            if success:
                self.metrics['v4_hits'] += 1
                return True, agents_used

        # Phase 2: Passive Checks (observers, DNA, harvest)
        code = self._passive_checks(page, step, version)
        if code:
            agents_used.append('passive')
            success, _reason = self._submit_and_record(page, code, step, version,
                                                       current_url, 'passive')
            if success:
                self.metrics['passive_hits'] += 1
                return True, agents_used

        config_ctype = self._cached_challenge_type

        # Phase 3: Sidecar (with rejection re-invocation loop)
        # - Final step + recursive_iframe: known broken-button challenges,
        #   limit sidecar effort (V4 agents handle the workarounds)
        is_broken_challenge = (step == FINAL_STEP or config_ctype == 'recursive_iframe')
        MAX_SUBMISSIONS = 1 if is_broken_challenge else 3
        MAX_SIDECAR_INVOCATIONS = 1 if is_broken_challenge else 3
        submission_count = 0
        agents_used.append('vision_learning')

        for sidecar_invocation in range(MAX_SIDECAR_INVOCATIONS):
            if submission_count >= MAX_SUBMISSIONS:
                break

            result = self._invoke_sidecar(
                page, step, version, agents_used,
            )
            self._last_sidecar_result = result
            candidates = result.get('candidates', [])

            # Fallback: if no candidates list but a code exists, wrap it
            if not candidates and result.get('code'):
                candidates = [{
                    'code': result['code'], 'source': 'sidecar',
                    'score': 0.5, 'evidence': {},
                    'seen_after_baseline': True,
                    'dom_change_score': 0.0, 'progress_delta': None,
                }]

            if not candidates:
                # Sidecar couldn't find any code — no point retrying
                break

            log(f"step {step}: [System 2] {len(candidates)} candidates:")
            for i, c in enumerate(candidates[:5]):
                log(f"  [{i}] {c['code']} score={c.get('score',0):.2f} "
                    f"src={c.get('source','?')}")

            for candidate in candidates:
                if submission_count >= MAX_SUBMISSIONS:
                    break
                code = candidate['code']
                success, reason = self._submit_and_record(
                    page, code, step, version, current_url, 'vision_learning'
                )
                submission_count += 1
                if success:
                    self.metrics['system2_successes'] += 1
                    # Promote sidecar result
                    promo = result.get('promotion_candidate')

                    # Don't promote for types that need hardcoded fallbacks
                    NO_PROMOTE_TYPES = {'recursive_iframe'}
                    promo_ctype = promo.get('challenge_type', '') if promo else ''
                    promo_base = re.sub(r'_v\d+$', '', promo_ctype)
                    is_broken_type = promo_base in NO_PROMOTE_TYPES
                    is_final_step = (step == FINAL_STEP)
                    should_promote = promo and not is_broken_type and not is_final_step

                    if should_promote:
                        snapshot = getattr(self, '_step_snapshot', None)
                        promoted = self._sidecar.finalize_promotion(
                            page, step, version, promo,
                            step_snapshot=snapshot,
                        )
                        if promoted:
                            self.metrics['promotions_created'] += 1
                        else:
                            log(f"step {step}: promotion REJECTED by finalize_promotion "
                                f"(type={promo.get('challenge_type', '?')})")
                    elif not should_promote and promo:
                        reason = 'broken_type' if is_broken_type else 'final_step'
                        log(f"step {step}: skipping promotion ({reason}) for {promo_ctype}")
                    elif not promo:
                        log(f"step {step}: no promotion candidate from sidecar "
                            f"(termination={result.get('termination_reason', '?')})")
                    return True, agents_used
                else:
                    self._sidecar.note_rejection(code, candidate)

            # All candidates from this invocation rejected — re-invoke
            if submission_count < MAX_SUBMISSIONS:
                log(f"step {step}: [System 2] re-invoking sidecar "
                    f"(attempt {sidecar_invocation + 2}/{MAX_SIDECAR_INVOCATIONS})")

        # Phase 3.5: Fast Twitch Recovery
        # For timing challenges where elements appear briefly and the sidecar
        # is too slow (15-20s API round-trip vs 3s window). Uses Playwright's
        # native wait_for + click for ~50ms reaction time.
        ft_code = self._fast_twitch_recovery(page, step, version, agents_used)
        if ft_code:
            success, _reason = self._submit_and_record(
                page, ft_code, step, version, current_url, 'fast_twitch')
            if success:
                agents_used.append('fast_twitch')
                # Promote: merge sidecar action log + fast twitch step
                self._promote_fast_twitch(page, step, version, ft_code)
                return True, agents_used

        # Phase 4: Step 30 /finish safety net (labeled exception)
        # Normally the final_step_hook in Phase 1 handles this. This is a backup
        # in case the hook fails or the agent wasn't dispatched.
        # See MISSION.md Labeled Exceptions for rationale.
        if step == FINAL_STEP:
            log(f"step {step}: [Phase 4] trying /finish navigation (labeled exception)")
            try:
                page.evaluate('''() => {
                    window.history.pushState({}, '', '/finish');
                    window.dispatchEvent(new PopStateEvent('popstate'));
                }''')
                page.wait_for_timeout(1000)
                from verify import is_finish_page
                if is_finish_page(page):
                    log(f"step {step}: [Phase 4] reached /finish page")
                    agents_used.append('finish_navigation')
                    self.metrics['steps_succeeded'] = self.metrics.get('steps_succeeded', 0) + 1
                    return True, agents_used
                else:
                    log(f"step {step}: [Phase 4] /finish navigation didn't reach finish page")
            except Exception as e:
                log(f"step {step}: [Phase 4] /finish navigation error: {e}")

        # All phases failed
        self._record_failure(page, step, version, agents_used)
        return False, agents_used

    # ── Phase 0: Step Setup ──────────────────────────────────────────────────

    def _step_setup(self, page, step: int, version: int) -> str | None:
        """Prepare page for new step. Returns current_url or None on failure."""
        log(f"step {step}: begin")

        # Error page recovery
        if self._is_error_page(page):
            log(f"step {step}: STARTING ON ERROR PAGE")
            self._recover_from_navigation(page, step, version)
            if self._is_error_page(page):
                return None

        # Tab normalization (Fix F)
        try:
            close_extra_tabs(page)
        except Exception:
            pass

        # Re-install popup auto-dismiss if page.goto() cleared it
        # (calculated agent does page.goto(BASE_URL) which wipes page JS context)
        try:
            has_autodismiss = page.evaluate('() => !!window.__autoDismissInterval')
            if not has_autodismiss:
                from agents.popup import POPUP_AUTO_DISMISS_SETUP_JS
                page.evaluate(POPUP_AUTO_DISMISS_SETUP_JS)
                log(f"step {step}: re-installed popup auto-dismiss")
        except Exception:
            pass

        # Step boundary: clear codes + snapshot baseline
        page.evaluate('() => { if (window.__clearCodes) window.__clearCodes(); }')

        # Reset state watcher for fresh step baseline
        reset_state_watch(page)

        # Inject exception hooks for labeled exceptions only
        config_type = get_challenge_type(step, version)
        from agents.v4.exception_hooks import get_exception_hook_js
        exc_js = get_exception_hook_js(config_type)
        if exc_js:
            try:
                page.evaluate(exc_js)
                log(f"step {step}: injected exception hooks for {config_type}")
            except Exception as e:
                log(f"step {step}: exception hook injection failed: {e}")

        # Reset scroll and wait for render
        page.evaluate('window.scrollTo(0, 0)')
        self.wait_for_raf_stable(page, timeout_ms=500)

        # Mark action time for observer recency
        page.evaluate('() => { if (window.__markAction) window.__markAction(); }')

        # Dismiss popups (batched logging to reduce noise)
        start_popup_batch()
        self._dismiss_popups(page)
        flush_popup_batch()

        # Scroll to top AGAIN after popup dismissal — popup clicks can scroll page down
        page.evaluate('window.scrollTo(0, 0)')

        # NOTE: popup auto-dismiss stays active for all steps including FINAL_STEP.
        # The final_step_hook wraps the agent and pushes to /finish after the agent
        # completes — popups must be dismissed normally for the agent to work.

        # NOTE: Previously had React fiber state reset here to clear stale
        # "solved" green box from previous step. REMOVED — dispatching into
        # React hooks causes white screen (violates read-only JS invariant).
        # Stale green boxes are harmless; popup dismissal handles them.

        # Initialize step state
        self._failed_codes = set(self.previously_used_codes)
        self._step_start_dom_sig = self.recipe_executor._get_dom_signature(page)

        # Capture immutable snapshot of pristine challenge state BEFORE any actions.
        # This is used for fingerprinting, AI review, and locator verification —
        # all of which must see step N's page, never step N+1's.
        from knowledge_reader import DOM_FINGERPRINT_JS
        snapshot_fp = {}
        try:
            snapshot_fp = page.evaluate(DOM_FINGERPRINT_JS) or {}
        except Exception:
            pass
        snapshot_instr = ''
        try:
            snapshot_instr = get_challenge_text(page, limit=500)
        except Exception:
            pass
        snapshot_screenshot = ''
        try:
            screenshot_bytes = page.screenshot(type="png")
            snapshot_screenshot = base64.b64encode(screenshot_bytes).decode()
        except Exception:
            pass
        # Compute text context from snapshot instruction (feeds 15% text_ctx weight)
        snapshot_text_ctx = {}
        try:
            page_info = self._extract_page_info(page)
            snapshot_text_ctx = self.knowledge_reader._extract_text_context(page_info)
        except Exception:
            pass

        # Capture structural DOM features for detection (dom_signals channel)
        snapshot_dom_signals = []
        try:
            snapshot_dom_signals = page.evaluate('''() => {
                const r = document.querySelector(
                    '[class*="challenge"], [class*="step"], main, #root > div > div');
                if (!r) return [];
                const signals = [];
                if (r.querySelector('canvas')) signals.push({type: "element_exists", selector: "canvas"});
                if (r.querySelector('video')) signals.push({type: "element_exists", selector: "video"});
                if (r.querySelector('audio')) signals.push({type: "element_exists", selector: "audio"});
                if (r.querySelector('[draggable="true"]'))
                    signals.push({type: "element_exists", selector: "[draggable=true]"});
                if (r.querySelector('iframe')) signals.push({type: "element_exists", selector: "iframe"});
                if (r.querySelector('[contenteditable]'))
                    signals.push({type: "element_exists", selector: "[contenteditable]"});
                if (r.querySelector('input[type="range"]'))
                    signals.push({type: "element_exists", selector: "input[type=range]"});
                if (r.querySelector('select')) signals.push({type: "element_exists", selector: "select"});
                if (r.querySelector('textarea'))
                    signals.push({type: "element_exists", selector: "textarea"});
                return signals;
            }''') or []
        except Exception:
            pass

        self._step_snapshot = StepContextSnapshot(
            fingerprint=snapshot_fp,
            instruction_text=snapshot_instr,
            screenshot_b64=snapshot_screenshot,
            text_ctx=snapshot_text_ctx,
            dom_signals=snapshot_dom_signals,
            step=step,
            timestamp=time.time(),
        )

        return page.url

    # ── V4 Agent System ─────────────────────────────────────────────────────

    def _get_budget(self, step: int) -> int:
        """Get time budget for this step."""
        if step == FINAL_STEP:
            return BUDGET_STEP30
        config_type = self._cached_challenge_type or 'simple'
        if config_type in COMPLEX_TYPES:
            return BUDGET_COMPLEX
        if step <= 15:
            return BUDGET_SIMPLE
        return BUDGET_STANDARD

    def _detect_type_for_v4(self, page, step: int, version: int,
                             instruction: str) -> str | None:
        """Detect challenge type for V4 agent dispatch.

        For steps 16+: use config directly.
        For steps 1-15: fast-path text matching, then 6-channel detection.
        """
        config_type = self._cached_challenge_type or get_challenge_type(step, version)

        # Steps 16+: config type is ground truth
        if config_type != 'simple':
            return config_type

        # Steps 1-15: DOM detection FIRST (unambiguous signals), text matching SECOND
        # DOM signals are structural and avoid misrouting from filler text
        try:
            dom_type = page.evaluate(r'''() => {
                const r = document.querySelector(
                    '[class*="challenge"], [class*="step"], main, #root > div > div');
                if (!r) return null;
                if (r.querySelector('video')) return 'video';
                const text0 = (r.innerText || '').toLowerCase();
                // Video keywords take priority — check before canvas→gesture
                if (/\b(seek|video|frame\s*\d|fast.?forward|rewind)\b/.test(text0))
                    return 'video';
                if (r.querySelector('canvas')) {
                    // Double-check full page text for video keywords — container
                    // may not include instruction header on some layouts
                    const fullText = (document.body?.innerText || '').substring(0, 3000).toLowerCase();
                    if (/\b(seek|video challenge|video frames?|fast.?forward|rewind)\b/.test(fullText))
                        return 'video';
                    return 'gesture';
                }
                if (r.querySelector('[draggable="true"]')) return 'drag_drop';
                if (r.querySelector('audio')) return 'audio';
                if (r.querySelector('iframe:not([style*="display:none"])')) return 'recursive_iframe';
                const btns = [...r.querySelectorAll('button')].map(b => (b.innerText || '').trim());
                if (btns.some(t => /^Shadow\s*Level/i.test(t))) return 'shadow_dom';
                if (btns.some(t => /^Tab\s*\d/i.test(t))) return 'multi_tab';
                const text = r.innerText || '';
                if (/Part\s*\d+\s*:/i.test(text)) return 'split_parts';
                if (/\d+\s*[+\-*/]\s*\d+\s*=\s*\?/.test(text)) return 'puzzle_solve';
                if (/required\s+sequence|keys?\s+in\s+sequence|keyboard\s+sequence/i.test(text)) return 'keyboard_sequence';
                if (/click\s+here.*times?\s+to\s+reveal/i.test(text)) return 'hidden_dom';
                if (/click\s+all\s*\d*\s*parts?\s+scattered/i.test(text)) return 'split_parts';
                if (/memory\s+challenge|code\s+will\s+flash/i.test(text)) return 'delay_memory';
                if (/capture.*times|rotating\s+code/i.test(text)) return 'timing';
                if (/service\s+worker/i.test(text)) return 'service_worker';
                if (/websocket/i.test(text)) return 'websocket';
                return null;
            }''')
            if dom_type and dom_type in CHALLENGE_AGENTS:
                return dom_type
        except Exception:
            pass

        # Semantic structure detection (roles + labels, no filler text noise)
        try:
            from agents.v4.helpers import detect_type_from_semantics, compute_challenge_scope
            _, quick_boundary = compute_challenge_scope(page)
            semantic_type = detect_type_from_semantics(page, quick_boundary)
            if semantic_type and semantic_type in CHALLENGE_AGENTS:
                return semantic_type
        except Exception:
            pass

        # Fast-path instruction text matching (fallback after DOM + semantic detection)
        instr_lower = instruction.lower()
        for ctype, phrases in FAST_ROUTES_ORDERED:
            for phrase in phrases:
                if phrase in instr_lower:
                    return ctype

        # Fallback: 6-channel detection from knowledge system
        try:
            challenge_text = get_challenge_text(page, limit=500)
            page_info = self._extract_page_info(page)
            learning, _variant = self.knowledge_reader.detect_and_get(
                challenge_text, page, page_info=page_info, version=version,
            )
            if learning:
                base_type = re.sub(r'_v\d+$', '', learning.challenge_type)
                if base_type in CHALLENGE_AGENTS:
                    return base_type
        except Exception:
            pass

        # Default: click_reveal (most common simple challenge)
        return 'click_reveal'

    def _system1_v4(self, page, step: int, version: int) -> str | None:
        """V4 agent dispatch — deterministic challenge agents.

        Returns code or None (falls through to passive checks / sidecar).
        """
        # Stale state guard: if progress > 5% at step start, the previous step's
        # React state hasn't cleared yet. Wait up to 3s for transition, then proceed
        # anyway (V4 agents use used_codes filter to avoid stale results).
        try:
            p = read_progress(page)
            initial_frac = p.get('fraction', 0) if p else 0
        except Exception:
            initial_frac = 0

        if initial_frac > 0.05:
            log(f"step {step}: [V4] stale state detected (progress={initial_frac:.2f}), "
                f"waiting for transition...")
            for _ in range(12):  # 12 × 250ms = 3s max wait
                page.wait_for_timeout(250)
                try:
                    p = read_progress(page)
                    frac = p.get('fraction', 0) if p else 0
                except Exception:
                    frac = 0
                if frac <= 0.05:
                    log(f"step {step}: [V4] stale state cleared after wait")
                    break
            else:
                log(f"step {step}: [V4] progress still elevated after 3s wait, proceeding anyway")

        instruction = get_challenge_text(page, limit=500)

        ctx = StepCtx(
            page=page, step=step, version=version,
            t0=time.time(), boundary_y=None,
            instruction=instruction.lower(),
            scope_selector=None,
            budget_ms=self._get_budget(step),
            debug={},
            used_codes=set(self.previously_used_codes),
        )

        # Universal BEFORE agents
        for agent in UNIVERSAL_AGENTS:
            result = agent.before(ctx)
            if result:
                log(f"step {step}: [V4] early code from {agent.__class__.__name__}: {result}")
                return result

        # Detect + dispatch
        challenge_type = self._detect_type_for_v4(page, step, version, instruction)
        base_type = re.sub(r'_v\d+$', '', challenge_type) if challenge_type else None

        # Override: only use 'calculated' (which does page.goto refresh) when the
        # previous step was puzzle_solve — that's the only case where state is stale.
        # Otherwise route math expressions to puzzle_solve (safe, no goto).
        if base_type == 'calculated' and self._prev_step_type != 'puzzle_solve':
            log(f"step {step}: [V4] overriding calculated → puzzle_solve "
                f"(prev_type={self._prev_step_type}, not stale)")
            challenge_type = 'puzzle_solve'
            base_type = 'puzzle_solve'

        # Record this step's type for next step's calculated override
        self._prev_step_type = base_type

        agent_fn = CHALLENGE_AGENTS.get(challenge_type) or CHALLENGE_AGENTS.get(base_type)
        code = None
        if agent_fn:
            t_agent = time.time()
            start_popup_batch()  # Suppress popup logs during agent execution
            try:
                log(f"step {step}: [V4] dispatching agent '{challenge_type}'")
                if step == FINAL_STEP:
                    log(f"step {step}: [V4] final step — wrapping with finish hook")
                    code = wrap_final_step(agent_fn, ctx)
                else:
                    code = agent_fn(ctx)
            except Exception as e:
                log(f"step {step}: [V4] agent '{challenge_type}' error: {e}")
            flush_popup_batch()
            elapsed_ms = (time.time() - t_agent) * 1000
            ctx.debug['agent_type'] = challenge_type
            ctx.debug['agent_ms'] = round(elapsed_ms)

            if code:
                log(f"step {step}: [V4] agent '{challenge_type}' returned code "
                    f"{code} ({elapsed_ms:.0f}ms)")
                return code

        # Universal AFTER agents (completion sweep + code extraction)
        for agent in UNIVERSAL_AGENTS:
            result = agent.after(ctx)
            if result:
                log(f"step {step}: [V4] post-agent code from "
                    f"{agent.__class__.__name__}: {result}")
                return result

        log(f"step {step}: [V4] no code from agents "
            f"(type={challenge_type}, elapsed={ctx.debug.get('agent_ms', 0)}ms)")
        return None

    # ── Phase 2: Passive Checks ──────────────────────────────────────────────

    def _passive_checks(self, page, step: int, version: int) -> str | None:
        """Check for codes already visible (no actions taken)."""

        # 1. Code observers (WebSocket/mutation hooks)
        code = self._check_code_observers(page, step)
        if code and code not in self._failed_codes:
            return code

        # 2. DNA clustering
        try:
            elements = self.dna_reasoner.scan(page)
            if elements:
                code = self.dna_reasoner.find_code_cluster(elements)
                if code and is_valid_code(code) and code not in self._failed_codes:
                    log(f"step {step}: DNA cluster found code {code}")
                    return code
        except Exception:
            pass

        # 3. harvest_and_score
        try:
            last_action_time = page.evaluate('() => window.__lastActionTime || 0')
            score, code = harvest_and_score(page, '', last_action_time)
            if code and score >= 0.6 and code not in self._failed_codes:
                log(f"step {step}: harvest found code {code} (score={score:.2f})")
                self.metrics['codes_from_extraction'] += 1
                return code
        except Exception:
            pass

        return None

    # ── Phase 3.5: Fast Twitch Recovery ─────────────────────────────────────

    # Phrase patterns that indicate timing-sensitive challenges.
    # Only these trigger fast twitch — everything else stays with sidecar.
    _FAST_TWITCH_PHRASES = [
        re.compile(r'click.*capture.*while.*window.*active', re.I),
        re.compile(r'click.*before.*disappears', re.I),
        re.compile(r'click.*while.*visible', re.I),
        re.compile(r'capture.*while.*window', re.I),
    ]

    def _fast_twitch_recovery(self, page, step: int, version: int,
                              agents_used: list) -> str | None:
        """Post-sidecar recovery for timing challenges.

        Gate: challenge text must match timing phrases.
        Action: click Capture N times (parsed from instruction), then extract code.
        The real code only appears AFTER all required captures complete.
        Returns code if found, None otherwise.
        """
        try:
            body_text = page.evaluate(
                '() => (document.body?.innerText || "").substring(0, 1000)')
        except Exception:
            return None

        # Gate: phrase match only
        if not any(p.search(body_text) for p in self._FAST_TWITCH_PHRASES):
            return None

        log(f"step {step}: [Phase 3.5] fast twitch triggered — timing challenge detected")

        from code_scorer import harvest_and_score, is_valid_code

        # Parse required captures from instruction
        m_req = re.search(r'at\s+least\s+(\d+)\s+times?', body_text, re.I)
        required = int(m_req.group(1)) if m_req else 3

        # Parse timing interval
        m_int = re.search(r'every\s+(\d+)\s*seconds?', body_text, re.I)
        interval_s = int(m_int.group(1)) if m_int else 3

        log(f"step {step}: [Phase 3.5] need {required} captures, interval={interval_s}s")

        # Phase A: Click Capture N+1 times with proper timing
        # Don't look for code until ALL captures are done (displayed codes are decoys)
        for i in range(required + 1):
            try:
                page.evaluate('''() => {
                    const buttons = document.querySelectorAll('button');
                    for (const b of buttons) {
                        if (/capture|click|grab|catch/i.test(b.innerText)) {
                            b.click(); return true;
                        }
                    }
                    return false;
                }''')
                log(f"step {step}: [Phase 3.5] capture click {i+1}")
            except Exception:
                pass

            page.wait_for_timeout(300)

            # Wait interval between captures (not after last)
            if i < required - 1:
                page.wait_for_timeout(interval_s * 1000)

        # Phase B: All captures done — NOW extract the real code
        page.wait_for_timeout(1000)

        # Check for "real code" text pattern
        try:
            real_code = page.evaluate(r'''() => {
                const text = document.body.innerText;
                const m = text.match(/(?:real|actual|final)\s+code\s+(?:is|:)\s*([A-Z0-9]{6})/i);
                return m ? m[1] : null;
            }''')
            if real_code and is_valid_code(real_code) and real_code not in self._failed_codes:
                log(f"step {step}: [Phase 3.5] found 'real code' pattern: {real_code}")
                return real_code
        except Exception:
            pass

        # Poll for code via hooks/DOM/harvest
        for _poll in range(20):
            # 1. Mutation hooks
            try:
                all_codes_raw = page.evaluate(
                    "() => window.__getAllCodes ? window.__getAllCodes() : []"
                ) or []
                if isinstance(all_codes_raw, dict):
                    items = all_codes_raw.get('bus', []) + all_codes_raw.get('mut', [])
                else:
                    items = all_codes_raw
                for item in items:
                    c = item.get('c', item) if isinstance(item, dict) else str(item)
                    if (c and len(c) == 6 and is_valid_code(c)
                            and c not in self._failed_codes):
                        log(f"step {step}: [Phase 3.5] found code {c} (hooks)")
                        return c
            except Exception:
                pass

            # 2. DOM scan
            try:
                all_matches = page.evaluate(f'''() => {{
                    const RE = /\\b[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{{6}}\\b/g;
                    return (document.body.innerText.match(RE) || []);
                }}''') or []
                for m in all_matches:
                    if is_valid_code(m) and m not in self._failed_codes:
                        log(f"step {step}: [Phase 3.5] found code {m} (DOM)")
                        return m
            except Exception:
                pass

            # 3. harvest_and_score
            score, code = harvest_and_score(page, '', 0)
            if code and score >= 0.3 and code not in self._failed_codes:
                log(f"step {step}: [Phase 3.5] harvest code {code} "
                    f"(score={score:.2f})")
                return code
            page.wait_for_timeout(300)

        log(f"step {step}: [Phase 3.5] fast twitch exhausted")
        return None

    def _promote_fast_twitch(self, page, step: int, version: int, code: str):
        """Promote a recipe that merges sidecar actions + fast twitch reactive_click."""
        import hashlib

        try:
            # Build recipe: sidecar's actions (setup) + reactive_click (finish)
            sidecar_result = self._last_sidecar_result or {}
            sidecar_recipe = []
            promo = sidecar_result.get('promotion_candidate', {})
            if promo:
                sidecar_recipe = promo.get('recipe', [])

            # Append the fast twitch step
            ft_step = {
                'action_type': 'reactive_click',
                'target_text': 'Capture',
                'delay_ms': 15000,
                'expect_code_visible': True,
            }
            full_recipe = sidecar_recipe + [ft_step]

            # Fast twitch only fires for timing challenges — hardcode type
            # (body text is unreliable here: page already advanced to next step)
            ctype = f"timing_v{version}"

            variant_id = f"fast_twitch_{step}_{hashlib.md5(code.encode()).hexdigest()[:8]}"
            log(f"step {step}: [Phase 3.5] promoting {ctype} with {len(full_recipe)} steps "
                f"(sidecar_recipe={len(sidecar_recipe)}, promo={'yes' if promo else 'no'})")

            snapshot = getattr(self, '_step_snapshot', None)
            promoted = self._sidecar.finalize_promotion(page, step, version, {
                'challenge_type': ctype,
                'variant_id': variant_id,
                'recipe': full_recipe,
                'semantic_steps': full_recipe,
                'assertions_present': True,
                'dna_signature': None,
                'code_source': 'fast_twitch',
            }, step_snapshot=snapshot)
            if promoted:
                self.metrics['promotions_created'] += 1
            else:
                log(f"step {step}: [Phase 3.5] promotion REJECTED for {ctype}")
        except Exception as e:
            log(f"step {step}: [Phase 3.5] promotion ERROR: {e}")

    # ── Phase 3: System 2 Reasoning ──────────────────────────────────────────

    def _invoke_sidecar(self, page, step: int, version: int,
                        agents_used: list) -> dict:
        """Run LearningSidecar once, return full result dict."""
        config_type = getattr(self, '_cached_challenge_type', None) or get_challenge_type(step, version)
        context = {
            'config_challenge_type': config_type,
            'agents_tried': list(agents_used),
        }

        # Content-based challenge detection (use clean text without decoys)
        challenge_text = get_challenge_text(page, limit=500)
        # One-line instruction log for diagnosis
        instr_snip = challenge_text.strip()[:300]
        log_stage("step", f"instructions_snip={instr_snip}")
        detected_type = detect_challenge_type(challenge_text)
        if detected_type:
            # Pass as hint only — sidecar uses config_type for promotion
            context['detected_challenge_type'] = detected_type

        # Pass codes already rejected by prior phases (recipe, passive)
        prior_rejections = self._failed_codes - self.previously_used_codes
        if prior_rejections:
            context['prior_rejected_codes'] = list(prior_rejections)

        # Thread step snapshot so sidecar/promotion uses pristine challenge state
        snapshot = getattr(self, '_step_snapshot', None)
        if snapshot:
            context['step_snapshot'] = snapshot

        log(f"step {step}: [System 2] starting sidecar (type={config_type})")

        result = self._sidecar.run(page, step, version, context)
        self.metrics['vision_calls'] += result.get('rounds', 0)

        candidates = result.get('candidates', [])
        code = result.get('code')
        if candidates:
            top3 = candidates[:3]
            log(f"step {step}: [System 2] sidecar found {len(candidates)} candidates "
                f"(rounds={result['rounds']}): " +
                ", ".join(f"{c['code']}({c['source']},{c['score']:.2f})" for c in top3))
        elif code:
            log(f"step {step}: [System 2] sidecar found code {code} "
                f"(rounds={result['rounds']}, actions={result['actions_executed']})")
        else:
            log(f"step {step}: [System 2] sidecar failed "
                f"(rounds={result['rounds']}, reason={result['termination_reason']})")

        return result

    # ── Submit + Record ──────────────────────────────────────────────────────

    def _submit_and_record(self, page, code: str, step: int, version: int,
                           current_url: str, source: str) -> tuple[bool, str]:
        """Submit code and detect step advancement via multiple signals (Fix J).

        Returns (success: bool, reason: str).
        """
        if code in self._failed_codes:
            return False, 'already_failed'

        start_popup_batch()
        self._dismiss_popups(page)
        flush_popup_batch()

        # Submit
        agent = self.a.get("code_entry")
        if agent:
            agent.run(page, step, version, code=code)

        # Multi-signal solved detection
        solved = False

        # Signal 1: URL changed (primary, fastest)
        try:
            page.wait_for_url(lambda url: url != current_url, timeout=2000)
            solved = True
        except Exception:
            pass

        # Signal 2: Step counter text changed
        if not solved:
            try:
                body = page.inner_text('body')[:500]
                if f'Step {step + 1} of {FINAL_STEP}' in body:
                    solved = True
            except Exception:
                pass

        # Signal 3: DOM changed significantly — give URL a bit more time
        if not solved:
            try:
                new_dom_sig = self.recipe_executor._get_dom_signature(page)
                score = self.recipe_executor._compute_change_score(
                    self._step_start_dom_sig, new_dom_sig
                )
                if score > 0.3:
                    try:
                        page.wait_for_url(lambda url: url != current_url, timeout=1000)
                        solved = True
                    except Exception:
                        pass
            except Exception:
                pass

        if solved:
            self.metrics['steps_succeeded'] += 1
            self.previously_used_codes.add(code)
            try:
                page.evaluate(
                    "c => { if (window.__addUsedCode) window.__addUsedCode(c); }", code
                )
            except Exception:
                pass
            log(f"step {step}: SOLVED by {source} with code {code}")
            return True, 'solved'
        else:
            self._failed_codes.add(code)
            log(f"step {step}: code {code} from {source} REJECTED")
            return False, 'rejected'

    # (Dead methods _convert_vl_actions_to_recipe, _minimize_recipe,
    #  _promote_to_system1 removed — promotion now handled by LearningSidecar.)

    # ── Failure recording ────────────────────────────────────────────────────

    def _record_failure(self, page, step: int, version: int, agents_used: list):
        """Log failure and capture diagnostics (screenshot + JSON)."""
        elapsed = time.time() - self._step_start_time
        log(f"step {step}: FAILED after {elapsed:.1f}s "
            f"(agents: {', '.join(agents_used) or 'none'}, "
            f"codes_tried: {len(self._failed_codes)})")
        self._capture_failure_snapshot(page, step, version, agents_used)

    def _capture_failure_snapshot(self, page, step: int, version: int,
                                  agents_used: list) -> dict | None:
        """Capture screenshot + structured diagnostics for a failed step."""
        try:
            self._failures_dir.mkdir(parents=True, exist_ok=True)
            elapsed = time.time() - self._step_start_time
            challenge_type = getattr(self, '_cached_challenge_type', None) or get_challenge_type(step, version)
            tag = f"step{step:02d}_v{version}_{challenge_type}"

            # 1. Screenshot
            screenshot_path = self._failures_dir / f"{tag}.png"
            try:
                png_bytes = page.screenshot(type="png", full_page=False)
                screenshot_path.write_bytes(png_bytes)
            except Exception as e:
                log(f"step {step}: screenshot failed: {e}")
                screenshot_path = None

            # 2. Page info
            try:
                page_info = self._extract_page_info(page)
            except Exception:
                page_info = {"instruction": "", "buttons": [], "interactives": []}
            try:
                body_text = get_body_text(page, limit=500)
            except Exception:
                body_text = ""

            # 3. Sidecar diagnostics (sanitized for JSON)
            sc = self._last_sidecar_result or {}
            sidecar_summary = {
                "rounds": sc.get("rounds", 0),
                "actions_executed": sc.get("actions_executed", 0),
                "termination_reason": sc.get("termination_reason", "n/a"),
                "iframe_count": sc.get("iframe_count", 0),
                "num_candidates": len(sc.get("candidates", [])),
                "top_candidates": [
                    {"code": c.get("code"), "score": c.get("score", 0),
                     "source": c.get("source", "?")}
                    for c in sc.get("candidates", [])[:5]
                ],
                "action_log": [
                    {"round": a.get("round"),
                     "action_type": a.get("action", {}).get("type", "?"),
                     "hit_tag": a.get("hit_tag"),
                     "dom_change": a.get("dom_change_score", 0),
                     "progress_before": a.get("progress_before"),
                     "progress_after": a.get("progress_after")}
                    for a in sc.get("action_log", [])
                ],
            }

            # 4. Build failure record
            failure = {
                "step": step, "version": version,
                "challenge_type": challenge_type,
                "elapsed_sec": round(elapsed, 1),
                "agents_used": agents_used,
                "codes_tried": sorted(self._failed_codes - self.previously_used_codes),
                "url": page.url,
                "instruction": page_info.get("instruction", "")[:200],
                "buttons": [b.get("text", "") for b in page_info.get("buttons", [])[:10]],
                "interactives": page_info.get("interactives", [])[:10],
                "body_snippet": re.sub(r'Section \d+\s*', '', body_text).strip()[:300],
                "sidecar": sidecar_summary,
                "screenshot": str(screenshot_path) if screenshot_path else None,
            }

            # 5. Write JSON
            json_path = self._failures_dir / f"{tag}.json"
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(failure, f, indent=2, default=str)

            self._step_failures.append(failure)
            log(f"step {step}: failure snapshot saved to {self._failures_dir}")
            return failure
        except Exception as e:
            log(f"step {step}: failure capture error: {e}")
            return None

    def get_failure_report(self) -> str:
        """Generate concise failure summary for post-run display."""
        if not self._step_failures:
            return ""
        lines = [
            f"\n--- Failure Report ({len(self._step_failures)} failed steps) ---",
            f"Run ID: {self._run_id}",
            f"Diagnostics: {self._failures_dir}/",
            "",
        ]
        for f in self._step_failures:
            sc = f.get("sidecar", {})
            reason = sc.get("termination_reason", "n/a")
            n_cand = sc.get("num_candidates", 0)
            codes = f.get("codes_tried", [])
            instr = f.get("instruction", "")[:60]
            ver = f.get('version', '?')
            ctype = f.get('challenge_type', 'unknown')[:12]
            elapsed = f.get('elapsed_sec', 0)
            lines.append(
                f"  Step {f.get('step', '?'):>2} (v{ver}) "
                f"[{ctype:12s}] "
                f"{elapsed:5.1f}s | {reason} | "
                f"cand={n_cand} tried={len(codes)} | {instr}"
            )
        lines.append(f"\nScreenshots: {self._failures_dir}/")

        # ── Coverage Engineer: recipe health audit ──
        # Scan all learnings for systematically broken recipes
        coverage_warnings = []
        all_learnings = self.knowledge_reader.get_all_learnings()
        for ctype, learning in all_learnings.items():
            variant = learning.get_active_variant()
            if not variant or not variant.action_recipe:
                continue
            # Repeated replay failure: recipe exists but never works
            if variant.replay_attempts >= 3 and variant.replay_successes == 0:
                coverage_warnings.append(
                    f"  ⚠ {ctype}: {variant.replay_attempts} replay attempts, "
                    f"0 successes (tier={variant.tier}, ttl={variant.recipe_ttl}) "
                    f"— recipe is systematically broken")
            # Struggling to build: keeps getting promoted but keeps failing
            elif (variant.consecutive_failures >= 2
                  and variant.replay_attempts >= 2
                  and variant.replay_successes == 0):
                coverage_warnings.append(
                    f"  ⚠ {ctype}: {variant.consecutive_failures} consecutive failures, "
                    f"never replayed successfully — promotion pipeline producing bad recipes")
            # Tier demotion: was hardened but regressed
            elif (variant.tier < 2 and variant.replay_successes >= 2
                  and variant.consecutive_failures >= 2):
                coverage_warnings.append(
                    f"  ⚠ {ctype}: demoted from hardened, now tier={variant.tier} "
                    f"with {variant.consecutive_failures} consecutive failures "
                    f"— challenge may have changed")

        if coverage_warnings:
            lines.append(f"\n--- Coverage Engineer Warnings ({len(coverage_warnings)}) ---")
            lines.extend(coverage_warnings)

        return "\n".join(lines)

    # ── Legacy compatibility ─────────────────────────────────────────────────

    def speed_run_to(self, page, target_step: int, version: int):
        """Replay steps 1..target_step using deterministic codes (for testing)."""
        for s in range(1, target_step):
            code = generate_code(s, version)
            self._submit_and_wait(page, code, s, version, page.url)
            page.wait_for_timeout(300)
