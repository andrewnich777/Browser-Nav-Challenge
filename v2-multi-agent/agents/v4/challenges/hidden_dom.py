"""Hidden DOM challenge: click elements to reveal hidden code.

After clicking, checks for visible codes. Falls back to Vision API
screenshot extraction for codes in pseudo-elements, canvas, etc.
"""

import re
from agents.v4.context import StepCtx
from agents.v4.helpers import (
    click_all_matching_buttons, wait_for_code_mutation, extract_code,
    screenshot_extract_code, get_progress_fraction, SUBMIT_EXCLUDE,
    get_accessible_elements,
)
from log import log


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999
    instr = ctx.instruction

    # Parse "click here N more times" pattern
    m = re.search(r'click\s+(?:here\s+)?(\d+)\s+(?:more\s+)?times?', instr, re.I)
    click_count = int(m.group(1)) + 1 if m else 5  # +1 for safety margin

    # Strategy 1: Use Playwright locator for "Click Here" text (handles overlays)
    locator_clicked = False
    for text in ['Click Here', 'Click Me']:
        try:
            locator = page.get_by_text(text, exact=False).first
            if locator.is_visible(timeout=500):
                log(f"step {ctx.step}: [hidden_dom] clicking '{text}' "
                    f"{click_count}x via locator")
                for i in range(click_count):
                    try:
                        locator.click(timeout=2000)
                    except Exception:
                        log(f"  [hidden_dom] locator click failed at iteration {i}, switching to fallback")
                        locator_clicked = False
                        break
                    page.wait_for_timeout(250)
                    code = extract_code(page) or wait_for_code_mutation(page, 500)
                    if code:
                        return code
                    if get_progress_fraction(page) >= 1.0:
                        break
                else:
                    locator_clicked = True
                break
        except Exception:
            continue

    # Strategy 2: Find clickable elements by DOM query (coordinate-based fallback)
    if not locator_clicked:
        try:
            targets = page.evaluate(f'''() => {{
                const all = document.querySelectorAll('div, span, p, button, [role="button"]');
                return [...all].filter(el => {{
                    const box = el.getBoundingClientRect();
                    if (box.width < 20 || box.height < 10) return false;
                    if (box.top + box.height/2 > {boundary_y}) return false;
                    const t = (el.innerText || '').toLowerCase();
                    const st = (el.getAttribute('style') || '').toLowerCase();
                    return (t.includes('click here') || t.includes('click me')
                        || t.includes('hidden') || t.includes('reveal')
                        || st.includes('cursor: pointer') || st.includes('cursor:pointer')
                        || el.tagName === 'BUTTON');
                }}).map(el => {{
                    const box = el.getBoundingClientRect();
                    return {{
                        text: (el.innerText || '').trim().substring(0, 60),
                        x: Math.round(box.left + box.width/2),
                        y: Math.round(box.top + box.height/2),
                        tag: el.tagName,
                    }};
                }});
            }}''') or []
        except Exception:
            targets = []

        # Filter out submit-related elements
        excl = {e.lower() for e in SUBMIT_EXCLUDE}
        targets = [t for t in targets if t['text'].lower() not in excl]

        # Accessibility tree fallback: find clickable elements by role
        if not targets:
            a11y = get_accessible_elements(page, boundary_y)
            a11y_buttons = a11y.get('buttons', [])
            targets = [
                {'text': b.get('name', ''), 'x': b['x'], 'y': b['y'], 'tag': 'BUTTON'}
                for b in a11y_buttons
                if b.get('name', '').lower() not in excl
            ]
            if targets:
                log(f"step {ctx.step}: [hidden_dom] a11y found {len(targets)} button targets")

        if targets:
            # Prefer element with "click here" text
            target = next(
                (t for t in targets if 'click here' in t['text'].lower()),
                targets[0],
            )
            log(f"step {ctx.step}: [hidden_dom] clicking '{target['text'][:30]}' "
                f"{click_count}x")
            for i in range(click_count):
                page.mouse.click(target['x'], target['y'])
                page.wait_for_timeout(250)

                # Check visible code after each click
                code = extract_code(page) or wait_for_code_mutation(page, 500)
                if code:
                    return code

                # Progress-gate: if progress advanced, keep going
                if get_progress_fraction(page) >= 1.0:
                    break

    # Strategy 2: Try standard button keywords
    keywords = ['reveal', 'show', 'find', 'search', 'look', 'check',
                'inspect', 'examine', 'hidden', 'secret', 'click']
    for _ in range(click_count):
        clicked = click_all_matching_buttons(
            page, keywords, boundary_y, settle_ms=400, max_clicks=1,
        )
        if not clicked:
            break
        code = extract_code(page) or wait_for_code_mutation(page, 500)
        if code:
            return code

    # Final wait — code may appear after a delay from clicks
    code = wait_for_code_mutation(page, 3000)
    if code:
        log(f"step {ctx.step}: [hidden_dom] code from delayed mutation: {code}")
        return code

    # Check element attributes (hint says: "Check attributes, aria labels, meta tags")
    attr_code = page.evaluate('''() => {
        const re = /\\b[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}\\b/;
        for (const el of document.querySelectorAll('*')) {
            for (const attr of el.attributes) {
                if (attr.name === 'class' || attr.name === 'style') continue;
                const m = attr.value.match(re);
                if (m) return m[0];
            }
        }
        return null;
    }''')
    if attr_code:
        from config import CHARSET
        if len(attr_code) == 6 and all(c in CHARSET for c in attr_code):
            log(f"step {ctx.step}: [hidden_dom] attribute code: {attr_code}")
            return attr_code

    # Screenshot scan only if progress suggests completion (costs API money)
    if get_progress_fraction(page) >= 0.8:
        code = screenshot_extract_code(page, boundary_y)
        if code:
            log(f"step {ctx.step}: [hidden_dom] final screenshot code: {code}")
            return code

    return None
