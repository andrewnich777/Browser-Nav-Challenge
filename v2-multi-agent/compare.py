"""compare.py — Side-by-side: what the V4 agent did vs what the sidecar did.

Runs a single step twice:
  1. V4 agent path (captures action trace via monkey-patched click/type/hover)
  2. Sidecar path (uses its built-in action_log)

Then aligns and diffs the action sequences so Claude Code can see exactly
where the agent diverged from the sidecar's successful approach.

Usage:
    python compare.py --step 18 --headed
    python compare.py --step 5 --headed --skip-to 5
"""
import sys
import io
import re
import time
import json
import argparse

from dotenv import load_dotenv
load_dotenv()

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)

from playwright.sync_api import sync_playwright
from orchestrator import Orchestrator
from config import get_challenge_type, generate_code
from verify import extract_step_from_url, extract_version_from_url, is_finish_page
from page_state import get_challenge_text
from primitives import extract_code_js, read_progress
from agents.v4 import CHALLENGE_AGENTS, StepCtx
from agents.v4.helpers import compute_challenge_scope, SUBMIT_EXCLUDE


class ActionTracer:
    """Monkey-patches page.mouse and page.keyboard to capture agent actions.

    Also hooks into helpers.py JS-level interactions (js_click, fill) via
    the _action_tracer module-level variable.
    """

    def __init__(self, page, dom_diffs: bool = False):
        self.page = page
        self.dom_diffs = dom_diffs
        self.actions = []
        self._t0 = time.time()
        self._install()
        self._install_helpers_hook()

    def _install(self):
        """Wrap mouse.click/move/down/up/wheel, keyboard.type/press."""
        original_click = self.page.mouse.click
        original_move = self.page.mouse.move
        original_down = self.page.mouse.down
        original_up = self.page.mouse.up
        original_wheel = self.page.mouse.wheel
        original_type = self.page.keyboard.type
        original_press = self.page.keyboard.press

        def traced_click(x, y, **kwargs):
            snap_before = self._snapshot() if self.dom_diffs else None
            el_text = self._element_at(x, y)
            self.actions.append({
                'type': 'click',
                'x': round(x), 'y': round(y),
                'element': el_text,
                't': round((time.time() - self._t0) * 1000),
            })
            result = original_click(x, y, **kwargs)
            if self.dom_diffs:
                snap_after = self._snapshot()
                self.actions[-1]['dom_diff'] = self._diff(snap_before, snap_after)
            return result

        def traced_move(x, y, **kwargs):
            el_text = self._element_at(x, y)
            self.actions.append({
                'type': 'hover',
                'x': round(x), 'y': round(y),
                'element': el_text,
                't': round((time.time() - self._t0) * 1000),
            })
            return original_move(x, y, **kwargs)

        def traced_down(**kwargs):
            self.actions.append({
                'type': 'down',
                't': round((time.time() - self._t0) * 1000),
            })
            return original_down(**kwargs)

        def traced_up(**kwargs):
            snap_before = self._snapshot() if self.dom_diffs else None
            self.actions.append({
                'type': 'up',
                't': round((time.time() - self._t0) * 1000),
            })
            result = original_up(**kwargs)
            if self.dom_diffs:
                snap_after = self._snapshot()
                self.actions[-1]['dom_diff'] = self._diff(snap_before, snap_after)
            return result

        def traced_wheel(delta_x, delta_y, **kwargs):
            self.actions.append({
                'type': 'wheel',
                'dx': round(delta_x), 'dy': round(delta_y),
                't': round((time.time() - self._t0) * 1000),
            })
            return original_wheel(delta_x, delta_y, **kwargs)

        def traced_type(text, **kwargs):
            self.actions.append({
                'type': 'type',
                'value': text[:50],
                't': round((time.time() - self._t0) * 1000),
            })
            return original_type(text, **kwargs)

        def traced_press(key, **kwargs):
            self.actions.append({
                'type': 'press',
                'key': key,
                't': round((time.time() - self._t0) * 1000),
            })
            return original_press(key, **kwargs)

        self.page.mouse.click = traced_click
        self.page.mouse.move = traced_move
        self.page.mouse.down = traced_down
        self.page.mouse.up = traced_up
        self.page.mouse.wheel = traced_wheel
        self.page.keyboard.type = traced_type
        self.page.keyboard.press = traced_press

    def _element_at(self, x, y):
        """Get text description of element at given coordinates."""
        try:
            return self.page.evaluate(f'''() => {{
                const el = document.elementFromPoint({x}, {y});
                if (!el) return '(nothing)';
                const tag = el.tagName.toLowerCase();
                const text = (el.textContent || '').trim().substring(0, 40);
                return tag + ': ' + text;
            }}''')
        except Exception:
            return '(unknown)'

    def _snapshot(self):
        """Lightweight snapshot: visible buttons/draggables with text+position."""
        try:
            return self.page.evaluate(r'''() => {
                const els = [
                    ...document.querySelectorAll(
                        'button, [role="button"], [draggable="true"]')
                ];
                return els.map(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return null;
                    return {
                        text: (el.textContent || '').trim().substring(0, 40),
                        x: Math.round(r.x + r.width/2),
                        y: Math.round(r.y + r.height/2),
                        tag: el.tagName.toLowerCase(),
                        draggable: el.draggable || false,
                    };
                }).filter(Boolean);
            }''') or []
        except Exception:
            return []

    def _diff(self, before, after):
        """Diff two snapshots: return {added, removed, progress_before, progress_after}."""
        def _key(el):
            return (el.get('text', ''), el.get('x', 0), el.get('y', 0))
        before_set = {_key(e) for e in (before or [])}
        after_set = {_key(e) for e in (after or [])}
        added = [f"{t}" for t, x, y in (after_set - before_set)]
        removed = [f"{t}" for t, x, y in (before_set - after_set)]
        # Read progress
        try:
            from primitives import read_progress
            prog = read_progress(self.page)
            frac = prog.get('fraction', 0) if prog else 0
        except Exception:
            frac = 0
        return {
            'added': added[:5],
            'removed': removed[:5],
            'count_before': len(before or []),
            'count_after': len(after or []),
            'progress': frac,
        }

    def _install_helpers_hook(self):
        """Hook into helpers.py to capture JS-level clicks and fills."""
        from agents.v4 import helpers
        helpers._action_tracer = self.actions

    def unhook(self):
        """Remove the helpers hook (call when done tracing)."""
        from agents.v4 import helpers
        helpers._action_tracer = None


def _fast_forward(orch, page, target_step, version):
    """Solve steps 1..(target-1) using deterministic V1 codes."""
    step = extract_step_from_url(page.url)
    if step is None:
        return version

    while step < target_step:
        code = generate_code(step, version)
        current_url = page.url
        orch._step_setup(page, step, version)
        success, _reason = orch._submit_and_record(page, code, step, version,
                                                    current_url, 'fast_forward')
        if not success:
            orch.run_step(page, step, version)

        page.wait_for_timeout(200)
        step_now = extract_step_from_url(page.url)
        ver_now = extract_version_from_url(page.url)
        if ver_now:
            version = ver_now
        if step_now is None or step_now <= step:
            break
        step = step_now
    return version


def _format_agent_action(a):
    """Format an agent action for display."""
    if a['type'] == 'click':
        return f"CLICK ({a['x']},{a['y']}) '{a.get('element', '')[:40]}'"
    elif a['type'] in ('js_click', 'js_click_shadow'):
        tag = 'JS_CLICK' if a['type'] == 'js_click' else 'JS_CLICK_SHADOW'
        return f"{tag} ({a.get('x','?')},{a.get('y','?')}) '{a.get('element', '')[:40]}'"
    elif a['type'] == 'fill':
        return f"FILL  '{a.get('value', '')}' -> {a.get('element', '')[:30]}"
    elif a['type'] == 'hover':
        return f"HOVER ({a['x']},{a['y']}) '{a.get('element', '')[:40]}'"
    elif a['type'] == 'down':
        return "DOWN"
    elif a['type'] == 'up':
        return "UP"
    elif a['type'] == 'wheel':
        return f"WHEEL dx={a.get('dx', 0)} dy={a.get('dy', 0)}"
    elif a['type'] == 'type':
        return f"TYPE  '{a.get('value', '')}'"
    elif a['type'] == 'press':
        return f"PRESS {a.get('key', '')}"
    return f"{a['type'].upper()} {json.dumps(a)[:50]}"


def _format_sidecar_action(entry):
    """Format a sidecar action_log entry for display."""
    action = entry.get('action', {})
    hit = entry.get('hit', {})
    atype = action.get('type', '?')
    text = hit.get('hit_text', '') or action.get('text', '')
    x = hit.get('x', '?')
    y = hit.get('y', '?')

    prog_before = entry.get('progress_before')
    prog_after = entry.get('progress_after')
    prog_delta = ''
    if prog_before is not None and prog_after is not None:
        if prog_after != prog_before:
            prog_delta = f" progress: {prog_before:.2f}→{prog_after:.2f}"

    if atype == 'click':
        return f"CLICK ({x},{y}) '{text[:40]}'{prog_delta}"
    elif atype == 'hover':
        return f"HOVER ({x},{y}) '{text[:40]}'{prog_delta}"
    elif atype == 'type':
        val = action.get('text', action.get('value', ''))
        return f"TYPE  '{val[:30]}'{prog_delta}"
    elif atype == 'press' or atype == 'key':
        return f"PRESS {action.get('key', action.get('text', ''))}{prog_delta}"
    elif atype == 'scroll':
        return f"SCROLL{prog_delta}"
    return f"{atype.upper()} {json.dumps(action)[:50]}{prog_delta}"


def compare_step(target_step: int, headed: bool = True, skip_to: int = 0):
    """Run both V4 agent and sidecar on the same step, compare actions."""

    # ── Run 1: V4 Agent ──
    print(f"{'='*70}")
    print(f"RUN 1: V4 Agent on step {target_step}")
    print(f"{'='*70}")

    orch1 = Orchestrator()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page, version = orch1.start_fresh(browser)
        if skip_to and skip_to > 1:
            version = _fast_forward(orch1, page, skip_to, version)
        current_step = extract_step_from_url(page.url)
        if current_step < target_step:
            version = _fast_forward(orch1, page, target_step, version)
        ver = extract_version_from_url(page.url)
        if ver:
            version = ver

        # Setup
        orch1._step_setup(page, target_step, version)
        t0 = time.time()

        config_type = get_challenge_type(target_step, version)
        instruction = get_challenge_text(page, limit=500)
        _sel, boundary_y = compute_challenge_scope(page)
        detected_type = orch1._detect_type_for_v4(page, target_step, version, instruction)

        # Install tracer and run agent
        tracer = ActionTracer(page)

        ctx = StepCtx(
            page=page, step=target_step, version=version,
            t0=t0, boundary_y=boundary_y,
            instruction=instruction.lower(),
            scope_selector=_sel,
            budget_ms=2500, debug={},
        )
        agent_fn = CHALLENGE_AGENTS.get(detected_type)
        agent_code = None
        if agent_fn:
            try:
                agent_code = agent_fn(ctx)
            except Exception as e:
                print(f"Agent error: {e}", flush=True)
        agent_elapsed = (time.time() - t0) * 1000
        agent_actions = tracer.actions
        agent_progress = read_progress(page)

        print(f"  Detected type: {detected_type}")
        print(f"  Config type:   {config_type}")
        print(f"  Agent code:    {agent_code or 'None'}")
        print(f"  Elapsed:       {agent_elapsed:.0f}ms")
        print(f"  Actions:       {len(agent_actions)}")
        print(f"  Progress:      {agent_progress}", flush=True)

        browser.close()

    # ── Run 2: Sidecar ──
    print(f"\n{'='*70}")
    print(f"RUN 2: Sidecar on step {target_step}")
    print(f"{'='*70}")

    orch2 = Orchestrator()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page, version2 = orch2.start_fresh(browser)
        if skip_to and skip_to > 1:
            version2 = _fast_forward(orch2, page, skip_to, version2)
        current_step = extract_step_from_url(page.url)
        if current_step < target_step:
            version2 = _fast_forward(orch2, page, target_step, version2)
        ver = extract_version_from_url(page.url)
        if ver:
            version2 = ver

        orch2._step_setup(page, target_step, version2)

        result = orch2._sidecar.run(page, target_step, version2, context={})
        sidecar_code = result.get('code')
        sidecar_actions = result.get('action_log', [])
        sidecar_rounds = result.get('rounds', 0)
        sidecar_reason = result.get('termination_reason', '?')

        print(f"  Sidecar code:  {sidecar_code or 'None'}")
        print(f"  Rounds:        {sidecar_rounds}")
        print(f"  Actions:       {len(sidecar_actions)}")
        print(f"  Termination:   {sidecar_reason}", flush=True)

        browser.close()

    # ── Diff ──
    print(f"\n{'='*70}")
    print(f"ACTION DIFF: step {target_step} (v={version})")
    print(f"Config type: {config_type} | Detected: {detected_type}")
    print(f"Agent: {'OK' if agent_code else 'FAILED'} | "
          f"Sidecar: {'OK' if sidecar_code else 'FAILED'}")
    print(f"{'='*70}")

    max_len = max(len(agent_actions), len(sidecar_actions))
    if max_len == 0:
        print("  (no actions from either side)")
        return

    print(f"\n{'#':>3s}  {'AGENT ACTION':<45s}  {'SIDECAR ACTION':<45s}")
    print(f"{'─'*3}  {'─'*45}  {'─'*45}")

    for i in range(max_len):
        agent_str = ''
        sidecar_str = ''

        if i < len(agent_actions):
            a = agent_actions[i]
            agent_str = _format_agent_action(a)
            agent_str = f"{agent_str[:43]}"

        if i < len(sidecar_actions):
            s = sidecar_actions[i]
            sidecar_str = _format_sidecar_action(s)
            sidecar_str = f"{sidecar_str[:43]}"

        # Mark differences
        marker = '  '
        if agent_str and sidecar_str:
            # Check if same action type at same-ish location
            a_type = agent_actions[i]['type'] if i < len(agent_actions) else ''
            s_type = sidecar_actions[i].get('action', {}).get('type', '') if i < len(sidecar_actions) else ''
            if a_type != s_type:
                marker = '!!'
        elif agent_str and not sidecar_str:
            marker = 'A>'
        elif sidecar_str and not agent_str:
            marker = 'S>'

        print(f"{i+1:3d} {marker} {agent_str:<45s}  {sidecar_str:<45s}")

    # Summary
    print(f"\n--- KEY DIFFERENCES ---")
    if not agent_code and sidecar_code:
        # Find what sidecar did that agent didn't
        sidecar_clicks = [(e.get('hit', {}).get('hit_text', ''),
                          e.get('hit', {}).get('x'), e.get('hit', {}).get('y'))
                         for e in sidecar_actions
                         if e.get('action', {}).get('type') == 'click']
        agent_clicks = [(a.get('element', ''), a.get('x'), a.get('y'))
                       for a in agent_actions if a['type'] == 'click']

        sidecar_texts = {t for t, x, y in sidecar_clicks if t}
        agent_texts = {t for t, x, y in agent_clicks if t}

        only_sidecar = sidecar_texts - agent_texts
        if only_sidecar:
            print(f"  Sidecar clicked but agent didn't:")
            for t in only_sidecar:
                print(f"    - '{t[:50]}'")

        # Check for progress-advancing actions
        for e in sidecar_actions:
            pb = e.get('progress_before')
            pa = e.get('progress_after')
            if pb is not None and pa is not None and pa > pb:
                action = e.get('action', {})
                hit = e.get('hit', {})
                print(f"  Progress-advancing action: {action.get('type', '?')} "
                      f"'{hit.get('hit_text', '')[:40]}' "
                      f"({pb:.2f}→{pa:.2f})")

    elif agent_code and not sidecar_code:
        print(f"  Agent succeeded, sidecar failed (unusual)")
    elif agent_code and sidecar_code:
        print(f"  Both succeeded")
    else:
        print(f"  Both failed")


def main():
    parser = argparse.ArgumentParser(
        description="Compare V4 agent vs sidecar actions on a single step")
    parser.add_argument("--step", type=int, required=True, help="Step number (1-30)")
    parser.add_argument("--headed", action="store_true", help="Show browser")
    parser.add_argument("--skip-to", type=int, default=0,
                        help="Fast-forward to this step")
    args = parser.parse_args()

    skip_to = args.skip_to or args.step
    compare_step(args.step, headed=args.headed, skip_to=skip_to)


if __name__ == "__main__":
    main()
