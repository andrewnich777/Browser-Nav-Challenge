"""Delayed reveal challenge: click start, wait for timer, code appears."""

import re
from agents.v4.context import StepCtx
from agents.v4.helpers import (
    click_button_by_text, wait_for_code_mutation, extract_code,
    wait_for_animation_end,
)
from log import log


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999

    # Parse wait duration
    m = re.search(r'(\d+)\s*second', ctx.instruction, re.I)
    wait_s = int(m.group(1)) if m else 5

    # Click start/begin
    click_button_by_text(page, ['start', 'begin', 'reveal', 'show', 'go'], boundary_y)

    log(f"step {ctx.step}: [delayed_reveal] waiting {wait_s}s for timed reveal")

    # Use RAF-polled wait_for_function for near-instant code detection (~16ms).
    # The code appears after the timer expires — raf polling checks every frame.
    timeout_ms = wait_s * 1000 + 3000  # timer + 3s grace
    try:
        result = page.wait_for_function(
            r'''() => {
                // Check hooks first (MutationObserver captures)
                if (window.__getAllCodes) {
                    const codes = window.__getAllCodes();
                    const all = (codes.bus || []).concat(codes.mut || []);
                    for (const item of all) {
                        const c = item.c || '';
                        if (c.length === 6 && /^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}$/.test(c))
                            return c;
                    }
                }
                // Check DOM for visible code
                const RE = /\b[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}\b/g;
                const text = document.body?.innerText || '';
                const matches = text.match(RE) || [];
                const DECOYS = window.__DECOY_CODES || new Set();
                for (const m of matches) {
                    if (!DECOYS.has(m)) return m;
                }
                return null;
            }''',
            polling='raf',
            timeout=timeout_ms,
        )
        if result:
            code = result.json_value()
            if code:
                return code
    except Exception:
        pass

    # Fallback: explicit extract after wait
    code = extract_code(page)
    if code:
        return code

    # Fallback: animation end + extract
    if wait_for_animation_end(page, 2000):
        code = extract_code(page)
        if code:
            return code

    return extract_code(page)
