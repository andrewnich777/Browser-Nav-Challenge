"""Scroll challenge: scroll down aggressively, check for code after each pass."""

import re
from agents.v4.context import StepCtx
from agents.v4.helpers import wait_for_code_mutation, extract_code
from primitives import scroll_container_js, read_progress


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999
    # Parse pixel target from instruction
    m = re.search(r'(\d{2,4})\s*(?:px|pixel)', ctx.instruction, re.I)
    if not m:
        amount = 600
    else:
        amount = int(m.group(1))

    # Try 1: scroll container (for scrollable divs)
    step_amount = max(amount // 3, 100)
    for i in range(3):
        result = scroll_container_js(page, direction="down", amount=step_amount)
        page.wait_for_timeout(300)
        code = extract_code(page)
        if code:
            return code

    code = wait_for_code_mutation(page, 1000)
    if code:
        return code

    # Try 2: window-level scroll (for full-page scroll challenges)
    page.evaluate('window.scrollTo(0, 0)')  # Reset
    page.wait_for_timeout(200)
    for i in range(3):
        page.evaluate(f'window.scrollBy(0, {step_amount})')
        page.wait_for_timeout(300)
        code = extract_code(page)
        if code:
            return code

    # Try 3: aggressive multi-pass mouse wheel scroll
    for _ in range(4):
        page.mouse.wheel(0, 600)
        page.wait_for_timeout(300)

        progress = read_progress(page)
        if progress and progress.get('fraction', 0) >= 1.0:
            break

        code = extract_code(page)
        if code:
            return code

    # CompletionSweep handles reveal buttons
    return wait_for_code_mutation(page, 1500)
