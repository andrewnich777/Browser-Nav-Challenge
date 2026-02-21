"""diagnose.py — Single-step runner with full telemetry.

Runs one step and dumps everything Claude Code needs to debug a failure:
  - Which agent was dispatched and detection scores
  - Instruction text (raw)
  - boundary_y and scope analysis
  - All buttons visible (with coordinates, text, in-scope status)
  - All inputs visible (with coordinates, placeholder, in-scope status)
  - DOM structural signals (canvas, audio, video, draggable, iframe, shadow)
  - Hook codes captured (with timestamps relative to step start)
  - Progress state (N/M)
  - Code candidates from all sources
  - What the agent returned (code or None)
  - What CompletionSweep/CodeExtractor would have found

Diagnostic tools (4 layers, use in order):
  --sidecar-only   Tool 1: Sidecar Action Transcript — "What SHOULD happen?"
                   Shows numbered action timeline from sidecar's action_log.
  (default)        Tool 2: Agent Action Trace — "What ACTUALLY happens?"
                   Shows numbered agent action timeline with timestamps.
  (with Tool 2)    Tool 3: DOM Snapshot Diff — "Did each action land?"
                   Before/after DOM diff per click/drag, inline with trace.
  --trace          Tool 4: Playwright Trace — "WHY at the frame level?"
                   Records trace ZIP viewable with `playwright show-trace`.

Workflow:
  1. --sidecar-only --screenshots  → Get the answer key (sidecar transcript)
  2. --screenshots                 → See what agent did + DOM diffs
  3. Compare transcript vs trace   → Find exact divergence point
  4. --trace                       → Deep dive (frame/network timing)

Usage:
    python diagnose.py --step 5 --headed
    python diagnose.py --step 18 --headed --skip-to 18
    python diagnose.py --step 5 --headed --no-agent
    python diagnose.py --step 9 --headed --screenshots
    python diagnose.py --step 9 --headed --sidecar-only --screenshots
    python diagnose.py --step 9 --headed --trace
"""
import sys
import io
import os
import re
import time
import json
import argparse

from dotenv import load_dotenv
load_dotenv()

try:
    sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
except Exception:
    pass

from playwright.sync_api import sync_playwright
from orchestrator import Orchestrator
from config import get_challenge_type, generate_code, CHARSET
from verify import extract_step_from_url, extract_version_from_url, is_finish_page
from page_state import get_challenge_text
from primitives import extract_code_js, read_progress
from code_scorer import harvest_and_score, is_valid_code
from agents.v4 import CHALLENGE_AGENTS, StepCtx
from agents.v4.helpers import (
    compute_challenge_scope, query_buttons_in_scope, query_inputs_in_scope,
    SUBMIT_EXCLUDE, extract_code, wait_for_code_mutation, get_all_hook_codes,
)
from compare import ActionTracer, _format_sidecar_action, _format_agent_action


# ── Screenshot Capture ──────────────────────────────────────────────────

SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "screenshots")

# Screenshot budget by challenge complexity (informed by sidecar action counts):
# drag_drop: sidecar does ~8-12 actions (6 drags + verifications)
# sequence: 4 sub-tasks + completion
# recursive_iframe: 5 levels deep
# timing: 3+ captures + completion
# Most others: 1-2 actions + completion
SCREENSHOT_BUDGET = {
    'drag_drop': 10,       # before + up to 6 drops + retries + after
    'sequence': 8,         # before + 4 sub-tasks + after
    'recursive_iframe': 8, # before + 5 levels + after
    'timing': 6,           # before + captures + after
    'gesture': 5,          # before + strokes + complete + after
    'split_parts': 5,      # before + parts + after
}
DEFAULT_SCREENSHOT_BUDGET = 4  # before + action + complete + after


class ScreenshotCapture:
    """Captures PNG screenshots at progress milestones.

    Budget is set per challenge type based on typical sidecar action counts.
    """

    def __init__(self, step: int, challenge_type: str = '', enabled: bool = False):
        self.step = step
        self.enabled = enabled
        self.count = 0
        self.max = SCREENSHOT_BUDGET.get(challenge_type, DEFAULT_SCREENSHOT_BUDGET)
        if enabled:
            os.makedirs(SCREENSHOT_DIR, exist_ok=True)
            # Clean previous screenshots for this step
            for f in os.listdir(SCREENSHOT_DIR):
                if f.startswith(f"step{step}_"):
                    os.remove(os.path.join(SCREENSHOT_DIR, f))
            print(f"  [screenshots] budget={self.max} for type='{challenge_type}'",
                  flush=True)

    def capture(self, page, label: str) -> str | None:
        """Take a screenshot if under the budget. Returns the file path or None."""
        if not self.enabled or self.count >= self.max:
            return None
        self.count += 1
        fname = f"step{self.step}_{self.count}_{label}.png"
        fpath = os.path.join(SCREENSHOT_DIR, fname)
        try:
            page.screenshot(path=fpath, full_page=False)
            print(f"  [screenshot {self.count}/{self.max}] {fname}", flush=True)
            return fpath
        except Exception as e:
            print(f"  [screenshot] FAILED: {e}", flush=True)
            return None


def _fmt_boundary(y):
    if y >= 99999:
        return "99999 (no boundary found)"
    return str(y)


def _dump_dom_signals(page):
    """Capture structural DOM signals (what detection JS sees)."""
    try:
        return page.evaluate(r'''() => {
            const r = document.querySelector(
                '[class*="challenge"], [class*="step"], main, #root > div > div');
            if (!r) return {container: null, signals: []};
            const signals = [];
            if (r.querySelector('canvas')) signals.push('canvas');
            if (r.querySelector('[draggable="true"]')) signals.push('draggable');
            if (r.querySelector('audio')) signals.push('audio');
            if (r.querySelector('video')) signals.push('video');
            if (r.querySelector('iframe:not([style*="display:none"])')) signals.push('iframe');
            if (r.querySelector('[contenteditable]')) signals.push('contenteditable');

            const btns = [...r.querySelectorAll('button')].map(b => b.textContent.trim());
            if (btns.some(t => /^Shadow\s*Level/i.test(t))) signals.push('shadow_level_btn');
            if (btns.some(t => /^Tab\s*\d/i.test(t))) signals.push('tab_N_btn');

            const text = r.innerText || '';
            if (/Part\s*\d+\s*:/i.test(text)) signals.push('part_N_label');
            if (/\d+\s*[+\-*/]\s*\d+\s*=\s*\?/.test(text)) signals.push('math_expression');
            if (/required\s+sequence|keyboard\s+sequence/i.test(text)) signals.push('keyboard_seq_text');
            if (/click\s+here.*times?\s+to\s+reveal/i.test(text)) signals.push('click_N_times');
            if (/memory\s+challenge|code\s+will\s+flash/i.test(text)) signals.push('memory_flash');
            if (/capture.*times|rotating\s+code/i.test(text)) signals.push('timing_capture');
            if (/service\s+worker/i.test(text)) signals.push('service_worker');
            if (/websocket/i.test(text)) signals.push('websocket');

            return {
                container: r.tagName + (r.className ? '.' + r.className.split(' ')[0] : ''),
                signals: signals,
            };
        }''')
    except Exception as e:
        return {'container': f'ERROR: {e}', 'signals': []}


def _dump_all_buttons(page):
    """Get ALL visible buttons with full info (not filtered by scope)."""
    try:
        return page.evaluate(r'''() => {
            return [...document.querySelectorAll('button, [role="button"]')].map(b => {
                const r = b.getBoundingClientRect();
                return {
                    text: (b.textContent || '').trim().substring(0, 80),
                    x: Math.round(r.x + r.width / 2),
                    y: Math.round(r.y + r.height / 2),
                    w: Math.round(r.width),
                    h: Math.round(r.height),
                    tag: b.tagName,
                    disabled: b.disabled,
                    classes: (b.className || '').substring(0, 60),
                };
            }).filter(b => b.w > 0 && b.h > 0);
        }''') or []
    except Exception:
        return []


def _dump_all_inputs(page):
    """Get ALL visible inputs with full info (not filtered by scope)."""
    try:
        return page.evaluate(r'''() => {
            return [...document.querySelectorAll(
                'input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"]), '
                + 'textarea, [contenteditable="true"]'
            )].map(el => {
                const r = el.getBoundingClientRect();
                return {
                    x: Math.round(r.x + r.width / 2),
                    y: Math.round(r.y + r.height / 2),
                    w: Math.round(r.width),
                    h: Math.round(r.height),
                    type: el.type || el.tagName.toLowerCase(),
                    placeholder: el.placeholder || '',
                    value: el.value || '',
                    maxLength: el.maxLength >= 0 ? el.maxLength : null,
                    id: el.id || '',
                    classes: (el.className || '').substring(0, 60),
                };
            }).filter(i => i.w > 0 && i.h > 0);
        }''') or []
    except Exception:
        return []


def _dump_hook_codes(page, t0_ms):
    """Get all codes from init hooks with timestamps relative to step start."""
    try:
        result = page.evaluate(
            "() => window.__getAllCodes ? window.__getAllCodes() : {bus:[], mut:[]}"
        ) or {}
        codes = []
        for src_name, items in [('bus', result.get('bus', [])), ('mut', result.get('mut', []))]:
            for item in items:
                c = item.get('c', '')
                t = item.get('t', 0)
                src = item.get('src', src_name)
                codes.append({
                    'code': c,
                    'source': src,
                    'timestamp_ms': t,
                    'relative_ms': t - t0_ms if t0_ms else t,
                    'valid': is_valid_code(c),
                })
        return codes
    except Exception:
        return []


def _fast_forward(orch, page, target_step, version):
    """Solve steps 1..(target-1) using deterministic V1 codes to fast-forward."""
    from config import generate_code
    step = extract_step_from_url(page.url)
    if step is None:
        return version

    while step < target_step:
        code = generate_code(step, version)
        current_url = page.url
        # Use orchestrator's step setup + submit (handles popups, hooks, React state)
        orch._step_setup(page, step, version)
        success, _reason = orch._submit_and_record(page, code, step, version,
                                                    current_url, 'fast_forward')
        if not success:
            print(f"  Fast-forward failed at step {step}, trying full solve...", flush=True)
            orch.run_step(page, step, version)

        page.wait_for_timeout(200)
        step_now = extract_step_from_url(page.url)
        ver_now = extract_version_from_url(page.url)
        if ver_now:
            version = ver_now
        if step_now is None or step_now <= step:
            print(f"  Fast-forward stuck at step {step}", flush=True)
            break
        step = step_now
        print(f"  Step {step-1} → {step}", flush=True)
    return version


TRACE_DIR = os.path.join(os.path.dirname(__file__), "traces")


def diagnose_step(target_step: int, headed: bool = True, skip_to: int = 0,
                  run_agent: bool = True, screenshots: bool = False,
                  sidecar_only: bool = False, trace: bool = False):
    """Run a single step with full telemetry dump.

    Args:
        screenshots: Take up to 6 PNGs at progress milestones (saved to screenshots/).
        sidecar_only: Skip V4 agent, let orchestrator's sidecar handle the step.
                      Combined with screenshots, shows how sidecar approaches it.
        trace: Record a Playwright trace file (saved to traces/).
    """
    orch = Orchestrator()
    # ScreenshotCapture initialized after detection (needs challenge type for budget)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page, version = orch.start_fresh(browser)
        print(f"Started v={version}", flush=True)

        # Fast-forward if needed
        if skip_to and skip_to > 1:
            print(f"Fast-forwarding to step {skip_to}...", flush=True)
            version = _fast_forward(orch, page, skip_to, version)

        current_step = extract_step_from_url(page.url)
        if current_step != target_step:
            if current_step < target_step:
                print(f"Fast-forwarding from {current_step} to {target_step}...", flush=True)
                version = _fast_forward(orch, page, target_step, version)
                current_step = extract_step_from_url(page.url)

        if current_step != target_step:
            print(f"ERROR: Expected step {target_step}, got step {current_step}", flush=True)
            browser.close()
            return

        ver_now = extract_version_from_url(page.url)
        if ver_now:
            version = ver_now

        # ── Playwright Tracing (opt-in) ──
        if trace:
            os.makedirs(TRACE_DIR, exist_ok=True)
            page.context.tracing.start(screenshots=False, snapshots=False)
            print(f"  [trace] Recording started", flush=True)

        # ── Step setup (same as orchestrator) ──
        t0 = time.time()
        t0_ms = int(t0 * 1000)

        # Run orchestrator step setup for hooks/popups/state
        orch._step_setup(page, target_step, version)

        config_type = get_challenge_type(target_step, version)
        instruction = get_challenge_text(page, limit=500)

        print(f"\n{'='*70}")
        print(f"DIAGNOSE: Step {target_step} (v={version})")
        print(f"{'='*70}")

        # ── 1. Instruction text ──
        print(f"\n--- INSTRUCTION TEXT ---")
        print(f"  Config type: {config_type}")
        print(f"  Raw text:\n    {instruction[:300]}")

        # ── 2. Boundary / Scope ──
        scope_sel, boundary_y = compute_challenge_scope(page)
        print(f"\n--- SCOPE ---")
        print(f"  boundary_y: {_fmt_boundary(boundary_y)}")
        print(f"  scope_selector: {scope_sel or 'None'}")

        # ── 3. DOM signals ──
        dom = _dump_dom_signals(page)
        print(f"\n--- DOM SIGNALS ---")
        print(f"  Container: {dom.get('container', 'None')}")
        signals = dom.get('signals', [])
        if signals:
            for s in signals:
                print(f"    - {s}")
        else:
            print(f"    (none detected)")

        # ── 4. Detection result ──
        # Fix: update cached type for the target step (avoids stale cache from
        # the previous step that was solved during fast-forward)
        orch._cached_challenge_type = get_challenge_type(target_step, version)
        detected_type = orch._detect_type_for_v4(page, target_step, version, instruction)
        print(f"\n--- DETECTION ---")
        print(f"  Config type:   {config_type}")
        print(f"  Detected type: {detected_type}")
        if detected_type != config_type and config_type != 'simple':
            print(f"  *** MISMATCH: config says '{config_type}' but detected '{detected_type}'")

        # Now that we know the challenge type, init screenshots with proper budget
        sc = ScreenshotCapture(target_step, challenge_type=detected_type, enabled=screenshots)

        # ── 5. All buttons ──
        all_buttons = _dump_all_buttons(page)
        print(f"\n--- BUTTONS ({len(all_buttons)} total) ---")
        for b in all_buttons:
            in_scope = b['y'] < boundary_y
            excluded = b['text'].lower() in {e.lower() for e in SUBMIT_EXCLUDE}
            scope_tag = "IN-SCOPE" if in_scope else "OUT-OF-SCOPE"
            excl_tag = " [EXCLUDED]" if excluded else ""
            disabled_tag = " [DISABLED]" if b.get('disabled') else ""
            print(f"  [{scope_tag}]{excl_tag}{disabled_tag} "
                  f"({b['x']:4d},{b['y']:4d}) {b['w']:3d}x{b['h']:3d} "
                  f"'{b['text'][:50]}'")

        # ── 6. All inputs ──
        all_inputs = _dump_all_inputs(page)
        print(f"\n--- INPUTS ({len(all_inputs)} total) ---")
        for inp in all_inputs:
            in_scope = inp['y'] < boundary_y
            scope_tag = "IN-SCOPE" if in_scope else "OUT-OF-SCOPE"
            ml_tag = f" maxLen={inp['maxLength']}" if inp['maxLength'] else ""
            print(f"  [{scope_tag}] ({inp['x']:4d},{inp['y']:4d}) {inp['w']:3d}x{inp['h']:3d} "
                  f"type={inp['type']}{ml_tag} "
                  f"placeholder='{inp['placeholder'][:30]}' "
                  f"value='{inp['value'][:20]}' "
                  f"id='{inp['id']}'")

        # ── 7. Progress ──
        progress = read_progress(page)
        print(f"\n--- PROGRESS ---")
        if progress:
            print(f"  {progress.get('current', '?')}/{progress.get('total', '?')} "
                  f"(fraction={progress.get('fraction', 0):.2f}) '{progress.get('text', '')}'")
        else:
            print(f"  (no progress indicator found)")

        # ── 8. Hook codes (before agent) ──
        pre_codes = _dump_hook_codes(page, t0_ms)
        print(f"\n--- HOOK CODES (pre-agent) ---")
        if pre_codes:
            for hc in pre_codes:
                valid_tag = "VALID" if hc['valid'] else "INVALID"
                print(f"  [{valid_tag}] {hc['code']} src={hc['source']} "
                      f"t={hc['relative_ms']:+.0f}ms")
        else:
            print(f"  (none)")

        # ── 9. Screenshot: before agent ──
        sc.capture(page, "before_agent")

        # ── 10. Run agent or sidecar ──
        agent_code = None
        agent_elapsed_ms = 0

        if sidecar_only:
            # ── SIDECAR-ONLY MODE ──
            # Skip V4 agent, run the full orchestrator pipeline (which falls
            # through to sidecar when V4 returns None).
            print(f"\n--- SIDECAR-ONLY MODE ---")
            print(f"  Disabling V4 agent '{detected_type}', forcing sidecar...", flush=True)

            # Monkey-patch the agent to return None so sidecar takes over
            original_agents = dict(CHALLENGE_AGENTS)
            CHALLENGE_AGENTS[detected_type] = lambda ctx: None

            t_agent = time.time()
            try:
                # Run the full orchestrator step (includes sidecar + all phases)
                orch.run_step(page, target_step, version)
            except Exception as e:
                print(f"  SIDECAR ERROR: {e}", flush=True)
            agent_elapsed_ms = (time.time() - t_agent) * 1000

            # Restore original agents
            CHALLENGE_AGENTS.update(original_agents)

            sc.capture(page, "after_sidecar")

            # Check what happened
            new_step = extract_step_from_url(page.url)
            if new_step and new_step > target_step:
                print(f"  Sidecar SOLVED step {target_step} -> {new_step} "
                      f"in {agent_elapsed_ms:.0f}ms", flush=True)
            else:
                print(f"  Sidecar did NOT solve step {target_step} "
                      f"({agent_elapsed_ms:.0f}ms)", flush=True)

            # ── Sidecar Action Transcript ──
            sr = orch._last_sidecar_result or {}
            action_log = sr.get('action_log', [])
            sr_code = sr.get('code')
            if not sr_code and sr.get('candidates'):
                sr_code = sr['candidates'][0].get('code')
            sr_rounds = sr.get('rounds', 0)
            sr_reason = sr.get('termination_reason', '?')

            print(f"\n--- SIDECAR TRANSCRIPT ({len(action_log)} actions, "
                  f"{sr_rounds} rounds, code={sr_code or 'None'}) ---")
            for i, entry in enumerate(action_log):
                action = entry.get('action', {})
                rnd = entry.get('round', '?')
                atype = action.get('type', '?')
                formatted = _format_sidecar_action(entry)
                print(f"  [{i+1:2d}] R{rnd} {formatted}")
            print(f"  Result: code={sr_code or 'None'} | rounds={sr_rounds} | "
                  f"reason={sr_reason}")

            # Show candidates if available
            candidates = sr.get('candidates', [])
            if candidates:
                print(f"  Candidates:")
                for c in candidates[:5]:
                    print(f"    {c.get('code', '?')} score={c.get('score', 0):.2f} "
                          f"src={c.get('source', '?')}")

        elif run_agent:
            print(f"\n--- AGENT EXECUTION ---")
            ctx = StepCtx(
                page=page, step=target_step, version=version,
                t0=t0, boundary_y=boundary_y,
                instruction=instruction.lower(),
                scope_selector=scope_sel,
                budget_ms=2500,
                debug={},
            )

            # Install progress-aware screenshot callback
            if screenshots:
                _prev_frac = [0.0]
                def _check_progress_screenshot():
                    """Call after major actions to capture progress screenshots."""
                    try:
                        prog = read_progress(page)
                        frac = prog.get('fraction', 0) if prog else 0
                        if frac > _prev_frac[0]:
                            sc.capture(page, f"progress_{frac:.0%}".replace('%', 'pct'))
                            _prev_frac[0] = frac
                    except Exception:
                        pass
                ctx.debug['_progress_screenshot_fn'] = _check_progress_screenshot

            # Run the agent function directly (skip universals — we want to see raw agent)
            agent_fn = CHALLENGE_AGENTS.get(detected_type)
            tracer = ActionTracer(page, dom_diffs=True)
            if agent_fn:
                t_agent = time.time()
                try:
                    agent_code = agent_fn(ctx)
                except Exception as e:
                    print(f"  AGENT ERROR: {e}")
                agent_elapsed_ms = (time.time() - t_agent) * 1000
                print(f"  Agent: {detected_type}")
                print(f"  Elapsed: {agent_elapsed_ms:.0f}ms")
                print(f"  Returned: {agent_code or 'None'}")
                if ctx.debug:
                    # Don't dump the callback function
                    debug_clean = {k: v for k, v in ctx.debug.items()
                                   if not k.startswith('_')}
                    if debug_clean:
                        print(f"  Debug: {json.dumps(debug_clean, indent=2)}")

                # ── Agent Action Trace + DOM Diffs ──
                actions = tracer.actions
                tracer.unhook()
                if actions:
                    print(f"\n--- AGENT ACTION TRACE ({detected_type}, "
                          f"{len(actions)} actions, {agent_elapsed_ms:.0f}ms) ---")
                    t0_abs_ms = round(tracer._t0 * 1000)
                    prev_progress = 0.0
                    for i, a in enumerate(actions):
                        formatted = _format_agent_action(a)
                        # Normalize timestamps: helpers use absolute ms,
                        # Playwright wrappers use relative ms
                        t_ms = a['t']
                        if t_ms > 1e12:  # absolute epoch timestamp
                            t_ms = t_ms - t0_abs_ms
                        print(f"  [{i+1:2d}] {t_ms:+6d}ms  {formatted}")
                        # Show DOM diff for click/up actions
                        dd = a.get('dom_diff')
                        if dd:
                            removed = dd.get('removed', [])
                            added = dd.get('added', [])
                            prog = dd.get('progress', 0)
                            cnt_b = dd.get('count_before', 0)
                            cnt_a = dd.get('count_after', 0)
                            changes = []
                            if removed:
                                changes.append(f"removed: {removed}")
                            if added:
                                changes.append(f"added: {added}")
                            if cnt_b != cnt_a:
                                changes.append(f"elements: {cnt_b}->{cnt_a}")
                            if prog != prev_progress:
                                changes.append(
                                    f"progress: {prev_progress:.2f}->{prog:.2f}")
                                prev_progress = prog
                            if changes:
                                print(f"         >> {', '.join(changes)}")
                            else:
                                print(f"         >> NO CHANGE")
            else:
                print(f"  No agent found for type '{detected_type}'")

            # Screenshot after agent
            sc.capture(page, "after_agent")

            # ── 11. Post-agent state ──
            print(f"\n--- POST-AGENT STATE ---")

            # Progress after
            progress_after = read_progress(page)
            if progress_after:
                print(f"  Progress: {progress_after.get('current', '?')}/"
                      f"{progress_after.get('total', '?')} "
                      f"(fraction={progress_after.get('fraction', 0):.2f})")
            else:
                print(f"  Progress: (none)")

            # Buttons after
            post_buttons = _dump_all_buttons(page)
            new_buttons = [b for b in post_buttons
                           if not any(ob['text'] == b['text'] and ob['x'] == b['x']
                                      for ob in all_buttons)]
            if new_buttons:
                print(f"  New buttons appeared:")
                for b in new_buttons:
                    in_scope = b['y'] < boundary_y
                    print(f"    {'IN' if in_scope else 'OUT'} ({b['x']},{b['y']}) '{b['text'][:50]}'")

            # Hook codes after
            post_codes = _dump_hook_codes(page, t0_ms)
            new_codes = [c for c in post_codes
                         if not any(pc['code'] == c['code'] and pc['source'] == c['source']
                                    for pc in pre_codes)]
            if new_codes:
                print(f"  New hook codes:")
                for hc in new_codes:
                    valid_tag = "VALID" if hc['valid'] else "INVALID"
                    print(f"    [{valid_tag}] {hc['code']} src={hc['source']} "
                          f"t={hc['relative_ms']:+.0f}ms")

            # DOM code
            dom_code = extract_code_js(page)
            print(f"  DOM code: {dom_code or 'None'}")

            # harvest_and_score
            try:
                hscore, hcode = harvest_and_score(page, instruction, t0_ms)
                print(f"  harvest_and_score: code={hcode or 'None'} score={hscore:.2f}")
            except Exception as e:
                print(f"  harvest_and_score: ERROR {e}")

        else:
            sc.capture(page, "no_agent")
            print(f"\n--- AGENT SKIPPED (--no-agent) ---")

        # ── Stop Tracing ──
        trace_path = None
        if trace:
            trace_path = os.path.join(
                TRACE_DIR, f"step_{target_step}_{detected_type}.zip")
            page.context.tracing.stop(path=trace_path)
            print(f"\n  [trace] Saved to {trace_path}", flush=True)

        # ── Summary ──
        total_ms = (time.time() - t0) * 1000
        mode = "sidecar-only" if sidecar_only else ("agent" if run_agent else "no-agent")
        print(f"\n{'='*70}")
        print(f"SUMMARY: step={target_step} v={version} config={config_type} "
              f"detected={detected_type} mode={mode} "
              f"agent_code={agent_code or 'None'} total={total_ms:.0f}ms")
        if screenshots:
            print(f"  Screenshots: {sc.count} saved to {SCREENSHOT_DIR}/")
        if trace_path:
            print(f"  Trace: {trace_path}")
            print(f"  View:  playwright show-trace {trace_path}")
        print(f"{'='*70}")

        # Keep browser open for manual inspection
        if headed:
            print(f"\nBrowser open for inspection. Press Enter to close...")
            try:
                input()
            except EOFError:
                time.sleep(10)

        browser.close()


def main():
    parser = argparse.ArgumentParser(description="Diagnose a single challenge step")
    parser.add_argument("--step", type=int, required=True, help="Step number to diagnose (1-30)")
    parser.add_argument("--headed", action="store_true", help="Show browser")
    parser.add_argument("--skip-to", type=int, default=0,
                        help="Fast-forward to this step before diagnosing")
    parser.add_argument("--no-agent", action="store_true",
                        help="Just dump page state, don't run the agent")
    parser.add_argument("--screenshots", action="store_true",
                        help="Take screenshots at progress milestones (saved to screenshots/)")
    parser.add_argument("--sidecar-only", action="store_true",
                        help="Skip V4 agent, let sidecar solve it (shows sidecar approach)")
    parser.add_argument("--trace", action="store_true",
                        help="Record Playwright trace (saved to traces/)")
    args = parser.parse_args()

    skip_to = args.skip_to or args.step
    diagnose_step(args.step, headed=args.headed, skip_to=skip_to,
                  run_agent=not args.no_agent, screenshots=args.screenshots,
                  sidecar_only=args.sidecar_only, trace=args.trace)


if __name__ == "__main__":
    main()
