"""Calculated challenge: second sequential math puzzle (labeled exception).

The calculated challenge arrives with stale React state from the previous
puzzle_solve step — the expression appears already "solved" with the OLD code
displayed. The component needs a fresh React render to show the new expression.

Fix: client-side navigation via pushState + popstate to '/' (unmounts step
component) then back to '/stepN' (mounts fresh). No full page load needed —
React Router handles the unmount/remount cycle internally.

See MISSION.md Labeled Exceptions for rationale.
"""

from agents.v4.context import StepCtx
from agents.v4.helpers import (
    type_into_challenge_input, wait_for_code_mutation, extract_code,
    type_react_native, get_progress_fraction, click_button_by_text,
)
from agents.v4.challenges.puzzle_solve import _find_puzzle_input, _click_solve
from resolvers import eval_expression
from log import log


def _refresh_react_state(ctx: StepCtx, dwell_ms: int = 16) -> bool:
    """Refresh React state via client-side navigation (no full page load).

    Uses pushState + popstate to navigate to '/' (different route pattern that
    unmounts the step component, clearing stale state) then back to '/stepN'
    (mounts fresh component). React Router handles the routing internally.

    Args:
        dwell_ms: How long to stay on '/' before navigating back. 16ms (one
            frame) is the fast path; 200ms is the proven fallback.

    Returns True if successfully navigated back to the step.
    """
    page = ctx.page
    step = ctx.step
    version = ctx.version
    target_path = f'/step{step}?version={version}'

    log(f"step {step}: [calculated] refreshing React state (dwell={dwell_ms}ms)")

    try:
        # Navigate to '/' — a DIFFERENT route pattern (not /stepN).
        # React Router only fully unmounts when the route pattern changes.
        page.evaluate('''() => {
            window.history.pushState({}, '', '/');
            window.dispatchEvent(new PopStateEvent('popstate'));
        }''')
        page.wait_for_timeout(dwell_ms)

        # Navigate back to the real step → mounts FRESH component with clean state
        page.evaluate(f'''() => {{
            window.history.pushState({{}}, '', '{target_path}');
            window.dispatchEvent(new PopStateEvent('popstate'));
        }}''')
        page.wait_for_timeout(800)

        page.evaluate('() => window.scrollTo(0, 0)')
        page.wait_for_timeout(200)

        if f'/step{step}' in page.url:
            log(f"step {step}: [calculated] React state refreshed, now at {page.url}")
            return True
        else:
            log(f"step {step}: [calculated] refresh failed, URL is {page.url}")
            return False
    except Exception as e:
        log(f"step {step}: [calculated] refresh error: {e}")
        return False


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999

    # Refresh React state to clear stale puzzle_solve result.
    # Fast path (16ms dwell) → if no expression found, retry with proven 200ms dwell.
    if not _refresh_react_state(ctx, dwell_ms=16):
        return None

    result = eval_expression(page)
    if not result:
        log(f"step {ctx.step}: [calculated] no expression after fast refresh, retrying with 200ms dwell")
        if not _refresh_react_state(ctx, dwell_ms=200):
            return None
        result = eval_expression(page)
        if not result:
            log(f"step {ctx.step}: [calculated] no math expression found after retry")
            return None

    log(f"step {ctx.step}: [calculated] answer: {result}")

    # Strategy 1: locator.fill() on spinbutton (number input) — most reliable
    for role in ['spinbutton', 'textbox']:
        try:
            loc = page.get_by_role(role).first
            bbox = loc.bounding_box(timeout=500)
            if bbox and (bbox['y'] + bbox['height'] / 2) < 99999:  # ignore boundary — layout changes after refresh
                loc.fill(str(result))
                page.wait_for_timeout(300)
                # Click Solve/Check button
                import re as _re
                try:
                    solve_btn = page.get_by_role("button", name=_re.compile(r"Solve|Check|Verify|Calculate", _re.I)).first
                    solve_btn.click(timeout=1000)
                except Exception:
                    click_button_by_text(page, ['solve', 'check', 'verify', 'calculate'], boundary_y=99999)
                page.wait_for_timeout(500)
                code = extract_code(page) or wait_for_code_mutation(page, 2000)
                if code:
                    return code
        except Exception:
            pass

    # Strategy 2: CSS selector fill fallback
    for selector in ['input[type="number"]', 'input:not([type="hidden"])', 'textarea']:
        try:
            loc = page.locator(selector).first
            bbox = loc.bounding_box(timeout=300)
            if bbox:
                loc.fill(str(result))
                page.wait_for_timeout(300)
                click_button_by_text(page, ['solve', 'check', 'verify', 'calculate'], boundary_y=99999)
                page.wait_for_timeout(500)
                code = extract_code(page) or wait_for_code_mutation(page, 2000)
                if code:
                    return code
        except Exception:
            continue

    # Strategy 3: Scoped input with keyboard typing
    if type_into_challenge_input(page, result, boundary_y):
        page.wait_for_timeout(500)
        code = wait_for_code_mutation(page, 2000)
        if code:
            return code

    # Find the puzzle input (ignoring boundary — input may be below)
    input_info = _find_puzzle_input(page, boundary_y=99999)
    if input_info:
        log(f"step {ctx.step}: [calculated] found input at "
            f"({input_info['x']},{input_info['y']}) type={input_info['type']}")
        page.mouse.click(input_info['x'], input_info['y'])
        page.wait_for_timeout(100)
        type_react_native(page, None, str(result))
        _click_solve(page, boundary_y=99999)
        page.wait_for_timeout(500)
        code = extract_code(page) or wait_for_code_mutation(page, 2000)
        if code:
            return code

    # Click Solve as last resort (answer may have been typed earlier)
    _click_solve(page, boundary_y=99999)
    page.wait_for_timeout(500)
    return extract_code(page) or wait_for_code_mutation(page, 2000)
