"""Shadow DOM challenge: click "Shadow Level N" in order, with shadow root traversal.

Uses Playwright get_by_text() which auto-pierces open shadow DOM for any element type.
Falls back to JS el.click() inside shadow roots for closed or edge cases.
Progress-gated with retry.
"""

import re
from agents.v4.context import StepCtx
from agents.v4.helpers import (
    click_button_by_text, js_click_button_by_text, js_click_in_shadow_roots,
    extract_code, wait_for_code_mutation,
    get_progress_fraction, wait_for_animation_end,
)
from agents.v4.cdp_helpers import find_codes_in_pierced_dom
from log import log


def _click_in_shadow_roots_any_element(page, keywords: list[str],
                                        boundary_y: int) -> str | None:
    """Click ANY element by text inside shadow roots (not just buttons).

    Shadow DOM challenges may use <div>, <span>, or custom elements as clickable
    targets. This searches all elements recursively through shadow roots.
    """
    try:
        return page.evaluate('''({keywords, boundaryY}) => {
            function collectMatches(root, kwl, matches) {
                // Recurse into shadow roots FIRST (prefer deeper/more specific)
                for (const el of root.querySelectorAll('*')) {
                    if (el.shadowRoot) {
                        collectMatches(el.shadowRoot, kwl, matches);
                    }
                }
                // Then search elements in this root
                for (const el of root.querySelectorAll('*')) {
                    const t = (el.textContent || '').trim();
                    const tl = t.toLowerCase();
                    if (tl.length > 200) continue;
                    if (!tl.includes(kwl)) continue;
                    const inner = (el.innerText || '').trim().toLowerCase();
                    if (inner.length > 100) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 1 || r.height < 1) continue;
                    if (r.top + r.height/2 > boundaryY) continue;
                    const cs = getComputedStyle(el);
                    const clickable = cs.cursor === 'pointer' || el.onclick ||
                        el.tagName === 'BUTTON' || el.getAttribute('role') === 'button' ||
                        el.tagName === 'A' || el.tabIndex >= 0;
                    if (clickable || inner === kwl || inner.startsWith(kwl)) {
                        matches.push({ el, inner, t: t.substring(0, 40) });
                    }
                }
            }
            for (const kw of keywords) {
                const kwl = kw.toLowerCase();
                const matches = [];
                collectMatches(document, kwl, matches);
                if (matches.length === 0) continue;
                // Pick the match with shortest innerText (most specific element)
                matches.sort((a, b) => a.inner.length - b.inner.length);
                matches[0].el.click();
                return matches[0].t;
            }
            return null;
        }''', {'keywords': keywords, 'boundaryY': boundary_y})
    except Exception:
        return None


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999

    # Phase 1: Click "Shadow Level N" elements using multiple strategies
    levels_clicked = 0
    for i in range(1, 6):
        p_before = get_progress_fraction(page)
        clicked_text = None

        # Tier 0: Playwright get_by_text — auto-pierces shadow DOM, any element type
        # Use .last to prefer the innermost/most-specific match (avoids parent containers
        # whose text includes both "Shadow Level 1 ✓" and "Shadow Level 2")
        for pattern in [f'Shadow Level {i}', f'Level {i}', f'Layer {i}']:
            try:
                loc = page.get_by_text(pattern, exact=True).last
                bbox = loc.bounding_box(timeout=300)
                if bbox and (bbox['y'] + bbox['height'] / 2) < boundary_y:
                    loc.click(timeout=1000)
                    clicked_text = pattern
                    break
            except Exception:
                continue

        # Tier 1: Playwright get_by_role("button") — standard button matching
        if not clicked_text:
            for pattern in [f'Shadow Level {i}', f'Level {i}', f'Layer {i}']:
                try:
                    loc = page.get_by_role("button", name=pattern).first
                    bbox = loc.bounding_box(timeout=300)
                    if bbox and (bbox['y'] + bbox['height'] / 2) < boundary_y:
                        loc.click(timeout=1000)
                        clicked_text = pattern
                        break
                except Exception:
                    continue

        # Tier 2: JS click any element in shadow roots (catches closed shadow roots)
        if not clicked_text:
            clicked_text = _click_in_shadow_roots_any_element(
                page,
                [f'shadow level {i}', f'level {i}', f'layer {i}'],
                boundary_y,
            )

        # Tier 3: Original shadow-root button-only search
        if not clicked_text:
            clicked_text = js_click_in_shadow_roots(
                page,
                [f'shadow level {i}', f'level {i}', f'layer {i}'],
                boundary_y,
            )

        if not clicked_text:
            break

        levels_clicked += 1
        log(f"step {ctx.step}: [shadow_dom] clicked level {i}: {clicked_text}")
        # Wait for animation completion instead of fixed timeout
        if not wait_for_animation_end(page, 800):
            page.wait_for_timeout(300)

        # Progress-gate: verify the click registered
        p_after = get_progress_fraction(page)
        if p_after > p_before:
            log(f"step {ctx.step}: [shadow_dom] progress {p_before:.2f} → {p_after:.2f}")
        else:
            log(f"step {ctx.step}: [shadow_dom] level {i} no progress, retrying")
            _click_in_shadow_roots_any_element(
                page,
                [f'shadow level {i}', f'level {i}', f'layer {i}'],
                boundary_y,
            )
            page.wait_for_timeout(400)
            p_retry = get_progress_fraction(page)
            if p_retry <= p_before:
                log(f"step {ctx.step}: [shadow_dom] level {i} still no progress after retry")
                break

        # Quick code check (no mutation wait — save time per level)
        code = extract_code(page)
        if code:
            return code

        # If progress complete, skip to code extraction immediately
        if p_after >= 1.0:
            break

    # Phase 2: Generic level keywords as fallback (if no specific levels found)
    if levels_clicked == 0:
        for _ in range(3):
            clicked = js_click_button_by_text(
                page,
                ['enter', 'level', 'open', 'expand', 'next', 'layer'],
                boundary_y,
            )
            if not clicked:
                clicked = click_button_by_text(
                    page,
                    ['enter', 'level', 'open', 'expand', 'next', 'layer'],
                    boundary_y,
                )
            if not clicked:
                break
            levels_clicked += 1
            page.wait_for_timeout(300)

    # Check for code — pierced DOM first (codes live in shadow roots)
    code = extract_code(page)
    if code:
        return code

    code = find_codes_in_pierced_dom(page)
    if code:
        log(f"step {ctx.step}: [shadow_dom] pierced DOM code: {code}")
        return code

    # Click reveal/complete button (text-based — pierces shadow DOM)
    for label in ['Reveal Code', 'Reveal', 'Show Code', 'Complete']:
        try:
            loc = page.get_by_text(re.compile(label, re.I)).last
            bbox = loc.bounding_box(timeout=300)
            if bbox and (bbox['y'] + bbox['height'] / 2) < boundary_y:
                loc.click(timeout=500)
                page.wait_for_timeout(300)
                code = extract_code(page) or find_codes_in_pierced_dom(page)
                if code:
                    return code
                break
        except Exception:
            continue

    # JS click fallback for reveal
    js_click_button_by_text(
        page, ['reveal code', 'reveal', 'show code', 'complete'], boundary_y)
    page.wait_for_timeout(300)

    return (extract_code(page)
            or find_codes_in_pierced_dom(page)
            or wait_for_code_mutation(page, 2000))
