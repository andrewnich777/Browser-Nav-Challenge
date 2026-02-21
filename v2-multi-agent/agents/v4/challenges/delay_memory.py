"""Delay memory challenge: code flashes briefly, click 'I Remember' to reveal.

The flash shows a code for 1-2 seconds then hides it. We need to:
1. Note any codes visible BEFORE clicking Show (to filter them out)
2. Click Show and capture the NEW code during the flash
3. Click "I Remember" which reveals the real code
"""

from agents.v4.context import StepCtx
from agents.v4.helpers import (
    click_button_by_text, wait_for_code_mutation, extract_code,
)
from log import log


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999

    # Snapshot codes visible BEFORE the flash (to filter stale ones)
    pre_codes = set()
    try:
        pre = extract_code(page)
        if pre:
            pre_codes.add(pre)
    except Exception:
        pass

    # Click show/start — triggers the flash
    click_button_by_text(page, ['show', 'start', 'begin', 'flash', 'display'], boundary_y)

    # Wait for the flash to appear and capture it (1-3 seconds)
    # Use a polling loop instead of wait_for_code_mutation to filter stale codes
    flash_code = None
    for _ in range(15):  # 15 × 200ms = 3s
        page.wait_for_timeout(200)
        code = extract_code(page)
        if code and code not in pre_codes:
            flash_code = code
            log(f"step {ctx.step}: [delay_memory] captured flash code: {flash_code}")
            # Purge flash code from hook state so CodeExtractor doesn't return it
            page.evaluate(f'''() => {{
                if (window.__DECOY_CODES) window.__DECOY_CODES.add("{flash_code}");
                if (window.__codeBus) window.__codeBus = window.__codeBus.filter(x => x.c !== "{flash_code}");
                if (window.__mutCodes) window.__mutCodes = window.__mutCodes.filter(x => x.c !== "{flash_code}");
            }}''')
            break

    # Wait for flash to finish (code disappears)
    page.wait_for_timeout(1000)

    # Click "I Remember" to reveal the real code
    clicked = click_button_by_text(
        page,
        ['i remember', 'remember', 'recall', 'seen', 'got it', 'ready'],
        boundary_y,
    )
    if clicked:
        log(f"step {ctx.step}: [delay_memory] clicked 'I Remember'")
        page.wait_for_timeout(800)
        # The real code appears after clicking — flash code is always a decoy
        stale = pre_codes | ({flash_code} if flash_code else set())
        post_code = extract_code(page)
        if post_code and post_code not in stale:
            return post_code
        # Wait longer for mutation (code may animate in)
        post_code = wait_for_code_mutation(page, 3000)
        if post_code and post_code not in stale:
            return post_code

    # Final fallback — wait for any new code (never return the flash code)
    stale = pre_codes | ({flash_code} if flash_code else set())
    code = wait_for_code_mutation(page, 2000)
    if code and code not in stale:
        return code
    return None
