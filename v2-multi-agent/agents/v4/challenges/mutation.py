"""Mutation challenge: click trigger button repeatedly, then complete.

Uses JS el.click() for React handler compatibility. Progress-gated:
clicks until progress reaches 100%, then clicks completion button.
"""

from agents.v4.context import StepCtx
from agents.v4.helpers import (
    click_all_matching_buttons, extract_code, wait_for_code_mutation,
    query_buttons_in_scope, js_click_button_by_text, SUBMIT_EXCLUDE,
    get_progress_fraction,
)
from log import log


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999

    # Click mutation trigger buttons — role locator first, JS click fallback
    import re as _re
    _trigger_re = _re.compile(r"trigger|mutate|click|action", _re.I)
    for i in range(12):
        p = get_progress_fraction(page)
        if p >= 1.0:
            log(f"step {ctx.step}: [mutation] progress complete at iteration {i}")
            break

        # Tier 1: Playwright get_by_role (lazy locator, re-evaluates each call)
        clicked = False
        try:
            loc = page.get_by_role("button", name=_trigger_re).first
            bbox = loc.bounding_box(timeout=300)
            if bbox and (bbox['y'] + bbox['height'] / 2) < boundary_y:
                loc.click(timeout=500)
                clicked = True
        except Exception:
            pass

        # Tier 2: JS click (React handler compatibility)
        if not clicked:
            clicked = bool(js_click_button_by_text(
                page,
                ['trigger mutation', 'trigger', 'mutate', 'click', 'action', 'change'],
                boundary_y,
            ))

        if not clicked:
            buttons = query_buttons_in_scope(page, boundary_y, exclude=SUBMIT_EXCLUDE)
            if not buttons:
                break
            page.evaluate('({x, y}) => { const el = document.elementFromPoint(x, y); if (el) el.click(); }',
                          {'x': buttons[0]['x'], 'y': buttons[0]['y']})

        page.wait_for_timeout(300)

        code = extract_code(page)
        if code:
            return code

    # Click completion button after all mutations triggered
    js_click_button_by_text(
        page, ['complete', 'reveal', 'show code', 'done'], boundary_y)
    page.wait_for_timeout(500)

    code = extract_code(page) or wait_for_code_mutation(page, 2000)
    if code:
        return code

    # Playwright fallback for completion
    click_all_matching_buttons(
        page, ['complete', 'reveal', 'show', 'done'], boundary_y,
        settle_ms=500, max_clicks=1)

    return extract_code(page) or wait_for_code_mutation(page, 2000)
