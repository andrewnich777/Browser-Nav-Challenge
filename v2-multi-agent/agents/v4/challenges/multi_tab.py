"""Multi-tab challenge: virtual tab buttons first (JS click), popup fallback.

Uses JS el.click() for React handler compatibility. Progress-gated: verifies
each tab click actually registered before moving to the next.
"""

import re
from agents.v4.context import StepCtx
from agents.v4.helpers import (
    click_button_by_text, js_click_button_by_text,
    query_buttons_in_scope, extract_code,
    wait_for_code_mutation, SUBMIT_EXCLUDE,
    get_progress_fraction, click_and_verify_progress,
)
from primitives import extract_code_js
from log import log


def _js_click_tab(page, tab_num: int, boundary_y: int) -> bool:
    """Click a specific tab button using JS el.click() for React compat.

    Searches for "Tab N" text patterns. Returns True if clicked.
    """
    result = page.evaluate('''({tabNum, boundaryY}) => {
        const btns = document.querySelectorAll('button, [role="button"], [role="tab"]');
        const patterns = [
            new RegExp('^tab\\\\s*' + tabNum + '$', 'i'),
            new RegExp('tab\\\\s*' + tabNum + '\\\\b', 'i'),
        ];
        for (const btn of btns) {
            const t = (btn.innerText || '').trim();
            if (btn.disabled) continue;
            const r = btn.getBoundingClientRect();
            if (r.width < 1 || r.height < 1) continue;
            if (r.top + r.height/2 > boundaryY) continue;
            for (const pat of patterns) {
                if (pat.test(t)) {
                    btn.click();
                    return t;
                }
            }
        }
        return null;
    }''', {'tabNum': tab_num, 'boundaryY': boundary_y})
    return bool(result)


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999
    context = page.context

    # Check for virtual tab buttons first (Tab 1, Tab 2, ...)
    buttons = query_buttons_in_scope(page, boundary_y, exclude=SUBMIT_EXCLUDE)
    tab_buttons = [b for b in buttons if re.match(r'tab\s*\d', b['text'].lower())]

    if tab_buttons:
        # Find the max tab number
        max_tab = 0
        for b in tab_buttons:
            m = re.search(r'\d+', b['text'])
            if m:
                max_tab = max(max_tab, int(m.group()))
        num_tabs = max(max_tab, len(tab_buttons))

        log(f"step {ctx.step}: [multi_tab] found {len(tab_buttons)} virtual tabs, max={max_tab}")

        # Click tabs using JS for React compatibility, with progress-gating
        for i in range(1, num_tabs + 1):
            p_before = get_progress_fraction(page)

            # Tier 1: JS click (most reliable for React)
            clicked = _js_click_tab(page, i, boundary_y)
            if not clicked:
                # Tier 2: Playwright locator
                try:
                    loc = page.locator(f'button:has-text("Tab {i}"), [role="tab"]:has-text("Tab {i}")').first
                    if loc.is_visible(timeout=500):
                        loc.click(timeout=1000)
                        clicked = True
                except Exception:
                    pass
            if not clicked:
                # Tier 3: Re-query and mouse click
                fresh = query_buttons_in_scope(page, boundary_y, exclude=SUBMIT_EXCLUDE)
                for b in fresh:
                    if re.search(rf'tab\s*{i}\b', b['text'], re.I):
                        page.mouse.click(b['x'], b['y'])
                        clicked = True
                        break

            page.wait_for_timeout(400)

            # Progress-gate: check if tab click registered
            p_after = get_progress_fraction(page)
            if p_after > p_before:
                log(f"step {ctx.step}: [multi_tab] tab {i} registered "
                    f"({p_before:.2f} → {p_after:.2f})")
            elif clicked:
                # No progress — retry with Playwright locator (different method)
                log(f"step {ctx.step}: [multi_tab] tab {i} no progress, retrying")
                try:
                    loc = page.locator(f'button:has-text("Tab {i}"), [role="tab"]:has-text("Tab {i}")').first
                    loc.click(timeout=1000)
                except Exception:
                    _js_click_tab(page, i, boundary_y)
                page.wait_for_timeout(400)

            # Check for code after each tab
            code = extract_code(page) or wait_for_code_mutation(page, 300)
            if code:
                return code

        # All tabs visited — check for code (short wait, code appears quickly)
        code = extract_code(page) or wait_for_code_mutation(page, 500)
        if code:
            return code

        # Click reveal-type buttons with progress verification
        reveal_kw = ['all tabs visited', 'all tabs', 'reveal code', 'reveal', 'complete', 'show']
        clicked, p = click_and_verify_progress(page, reveal_kw, boundary_y)
        if clicked:
            code = extract_code(page) or wait_for_code_mutation(page, 1500)
            if code:
                return code

        # Fallback: regular Playwright click on reveal
        click_button_by_text(
            page, ['all tabs', 'visited', 'reveal', 'complete', 'show'],
            boundary_y,
        )
        page.wait_for_timeout(300)
        return extract_code(page) or wait_for_code_mutation(page, 1500)

    # Fallback: real popup path
    click_button_by_text(
        page, ['open', 'launch', 'new tab', 'new window', 'trigger', 'click'],
        boundary_y,
    )

    try:
        new_page = context.wait_for_event('page', timeout=5000)
        new_page.wait_for_load_state('domcontentloaded', timeout=3000)
        page.wait_for_timeout(500)

        code = extract_code_js(new_page)
        if code:
            log(f"step {ctx.step}: [multi_tab] code from popup: {code}")
            new_page.close()
            return code

        try:
            body = new_page.inner_text('body')
            for m in re.findall(r'\b[A-HJ-NP-Z2-9]{6}\b', body):
                if m == m.upper():
                    log(f"step {ctx.step}: [multi_tab] code from popup body: {m}")
                    new_page.close()
                    return m
        except Exception:
            pass

        new_page.close()
    except Exception as e:
        log(f"step {ctx.step}: [multi_tab] no popup detected: {e}")

    return wait_for_code_mutation(page, 2000)
