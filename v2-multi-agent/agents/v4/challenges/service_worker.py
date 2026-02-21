"""Service worker challenge: register, wait for cache, retrieve from cache.

Uses JS el.click() for React compatibility. Progress-gated with retry on
retrieve step. Broader keyword matching for button text variants.
"""

from agents.v4.context import StepCtx
from agents.v4.helpers import (
    click_button_by_text, js_click_button_by_text,
    query_buttons_in_scope, wait_for_code_mutation,
    extract_code, get_all_hook_codes, SUBMIT_EXCLUDE,
    get_progress_fraction, click_and_verify_progress,
)
from log import log


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999

    # Step 1: Click register/install button with progress verification
    register_kw = ['register', 'install', 'start', 'enable', 'activate']
    clicked, p_after = click_and_verify_progress(page, register_kw, boundary_y, settle_ms=500)
    log(f"step {ctx.step}: [service_worker] clicked register (clicked={clicked}, progress={p_after:.2f})")
    p_before = p_after

    # Wait for registration + cache population
    # Poll for the "Retrieve" button to become enabled OR progress change
    retrieve_found = False
    for i in range(12):  # Up to 6s
        page.wait_for_timeout(500)

        # Check if retrieve button appeared
        btns = query_buttons_in_scope(page, boundary_y, exclude=SUBMIT_EXCLUDE)
        for b in btns:
            tl = b['text'].lower()
            if ('retrieve' in tl or 'cache' in tl or 'fetch' in tl
                    or 'get code' in tl or 'get from' in tl):
                retrieve_found = True
                break
        if retrieve_found:
            log(f"step {ctx.step}: [service_worker] retrieve button ready after {(i+1)*500}ms")
            break

        # Also check if progress changed (registration completed)
        p = get_progress_fraction(page)
        if p > p_before:
            log(f"step {ctx.step}: [service_worker] progress after register: "
                f"{p_before:.2f} → {p:.2f}")

    # Step 2: Click activate if it exists as a separate button
    js_click_button_by_text(page, ['activate', 'enable'], boundary_y)
    page.wait_for_timeout(500)

    # Step 3: Click retrieve from cache (2 attempts max)
    for attempt in range(2):
        p_before = get_progress_fraction(page)

        # JS click first (React compat)
        clicked = js_click_button_by_text(
            page,
            ['retrieve from cache', 'retrieve', 'from cache', 'cache',
             'fetch', 'get code', 'get from cache'],
            boundary_y,
        )
        if not clicked:
            # Playwright mouse click fallback
            clicked = click_button_by_text(
                page,
                ['retrieve from cache', 'retrieve', 'from cache', 'cache',
                 'fetch', 'get code', 'get from cache'],
                boundary_y,
            )

        if clicked:
            log(f"step {ctx.step}: [service_worker] clicked retrieve (attempt {attempt+1})")
            page.wait_for_timeout(1000)

            # Check for code from hooks
            codes = get_all_hook_codes(page)
            if codes:
                log(f"step {ctx.step}: [service_worker] hook code: {codes[-1]}")
                return codes[-1]

            # Check DOM
            code = extract_code(page) or wait_for_code_mutation(page, 1500)
            if code:
                return code

            # Progress-gate
            p_after = get_progress_fraction(page)
            if p_after > p_before:
                log(f"step {ctx.step}: [service_worker] retrieve advanced progress")
        else:
            page.wait_for_timeout(1000)

    # Final check
    codes = get_all_hook_codes(page)
    if codes:
        return codes[-1]
    return extract_code(page) or wait_for_code_mutation(page, 2000)
