"""Video challenge: seek through video using buttons, extract final code."""

from agents.v4.context import StepCtx
from agents.v4.helpers import (
    click_button_by_text, click_all_matching_buttons,
    extract_code, wait_for_code_mutation, get_all_hook_codes,
    js_click_button_by_text, complete_challenge_sweep,
)
from code_scorer import harvest_and_score
from primitives import read_progress
from log import log

SEEK_KEYWORDS = ['+10', '+5', '+30', '+1', 'seek', 'forward', 'skip', 'next', '>>']


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999

    # Click play first
    click_button_by_text(page, ['play', 'start'], boundary_y)
    page.wait_for_timeout(500)

    # Click seek/forward buttons repeatedly until progress is full
    last_code = None

    for i in range(25):
        clicked = click_all_matching_buttons(
            page, SEEK_KEYWORDS,
            boundary_y, settle_ms=200, max_clicks=1,
        )
        if not clicked:
            break

        # Check for codes after each seek — keep the LAST one
        codes = get_all_hook_codes(page)
        if codes:
            last_code = codes[-1]
        dom_code = extract_code(page)
        if dom_code:
            last_code = dom_code

        # Check progress
        progress = read_progress(page)
        if progress and progress.get('fraction', 0) >= 1.0:
            break

    # Return the last code seen (video challenges show decoys early)
    if last_code:
        log(f"step {ctx.step}: [video] final code: {last_code}")
        return last_code

    # Click Complete/Reveal button (code may only appear after completion)
    js_click_button_by_text(
        page, ['complete', 'complete challenge', 'reveal', 'done', 'finish'],
        boundary_y)
    page.wait_for_timeout(500)
    code = extract_code(page) or wait_for_code_mutation(page, 1500)
    if code:
        log(f"step {ctx.step}: [video] code after complete: {code}")
        return code

    # Completion sweep (tries multiple button keywords)
    code = complete_challenge_sweep(page, boundary_y)
    if code:
        log(f"step {ctx.step}: [video] code from sweep: {code}")
        return code

    # Try harvest (no timestamp filter — just get best code)
    try:
        score, code = harvest_and_score(page, ctx.instruction, 0)
        if code and score >= 0.5:
            log(f"step {ctx.step}: [video] harvest code: {code} (score={score:.2f})")
            return code
    except Exception:
        pass

    return wait_for_code_mutation(page, 2000)
