"""WebSocket challenge: connect via button (JS click), poll for code with longer wait.

Uses JS el.click() for React handler compatibility. Polls both hook codes
and DOM for up to 8s after connecting.
"""

from agents.v4.context import StepCtx
from agents.v4.helpers import (
    click_button_by_text, js_click_button_by_text,
    extract_code, wait_for_code_mutation,
    get_all_hook_codes, get_progress_fraction,
)
from log import log


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999

    # Click connect/start button — JS click for React compat
    clicked = js_click_button_by_text(
        page,
        ['connect', 'start', 'open', 'establish', 'websocket', 'ws'],
        boundary_y,
    )
    if not clicked:
        # Fallback: Playwright mouse click
        click_button_by_text(
            page,
            ['connect', 'start', 'open', 'establish', 'websocket', 'ws'],
            boundary_y,
        )

    log(f"step {ctx.step}: [websocket] clicked connect (js={bool(clicked)})")

    # Poll with retries (total ~5s) — WS messages typically arrive in 1-3s
    for i in range(10):
        page.wait_for_timeout(500)

        # Check hook codes (captures WS messages via init_hooks)
        codes = get_all_hook_codes(page)
        if codes:
            log(f"step {ctx.step}: [websocket] hook code after {(i+1)*0.5:.1f}s: {codes[-1]}")
            return codes[-1]

        # Check DOM
        code = extract_code(page)
        if code:
            return code

        # Check if progress suggests completion
        p = get_progress_fraction(page)
        if p >= 1.0:
            log(f"step {ctx.step}: [websocket] progress complete, checking for code")
            break

    # Try reveal button (JS click, then Playwright fallback)
    clicked = js_click_button_by_text(
        page, ['reveal', 'show', 'extract', 'code', 'display', 'get code'],
        boundary_y)
    if not clicked:
        click_button_by_text(
            page, ['reveal', 'show', 'extract', 'code', 'display'],
            boundary_y)
    page.wait_for_timeout(500)
    return extract_code(page) or wait_for_code_mutation(page, 2000)
