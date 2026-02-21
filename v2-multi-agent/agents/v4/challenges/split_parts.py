"""Split parts challenge: find scattered code parts and combine them.

Two variants:
1. Click-to-find: "Find and click all N parts scattered on this page."
   Parts are small standalone clickable elements scattered across a tall page.
   Systematic scroll + click every small interactive element with progress-gating.
   Status labels ("Part 1: K7") are decorative — NOT search hints for targets.
2. Text-assembly: Parts visible as text, combine to form code.
"""

import re
from agents.v4.context import StepCtx
from agents.v4.helpers import (
    click_button_by_text, extract_code,
    wait_for_code_mutation, get_progress_fraction,
)
from primitives import split_parts_solver
from log import log


def _extract_part_count(instr: str) -> int:
    """Extract part count from '(0 / N found)' or 'all N parts' patterns."""
    m = re.search(r'\d+\s*/\s*(\d+)\s*found', instr)
    if m:
        return int(m.group(1))
    m = re.search(r'all\s+(\d+)\s+parts?', instr)
    if m:
        return int(m.group(1))
    return 4


# UI elements to skip (lowercase). These are buttons/labels, not scattered parts.
_SKIP_TEXTS = frozenset([
    'submit', 'next', 'enter', 'done', 'finish', 'reveal', 'complete',
    'submit code', 'enter code', 'next step', 'previous', 'back', 'close',
    'ok', 'accept', 'continue', 'start', 'begin', 'next section',
    'scroll down to find', 'navigation', 'scroll down', 'step',
])


def _scan_viewport_candidates(page, broad: bool = False) -> list[dict]:
    """Find small, clickable, standalone elements visible in the current viewport.

    Returns {text, x, y} for each candidate, sorted top-to-bottom.
    No value matching — parts have unpredictable text content.

    broad=True relaxes clickability check (cursor:pointer not required) and
    widens size limits to catch parts that strict mode misses.
    """
    broad_js = 'true' if broad else 'false'
    try:
        return page.evaluate(r'''(broad) => {
            const results = [];
            const vh = window.innerHeight;
            const seen = new Set();
            for (const el of document.querySelectorAll('*')) {
                const r = el.getBoundingClientRect();
                if (r.bottom < 0 || r.top > vh) continue;
                if (r.width < 5 || r.height < 5) continue;
                const maxW = broad ? 400 : 300;
                const maxH = broad ? 200 : 150;
                if (r.width > maxW || r.height > maxH) continue;

                const t = (el.innerText || '').trim();
                if (t.length < 1 || t.length > 8) continue;

                // Skip status labels ("Part 1: K7")
                if (/Part\s*\d/i.test(t)) continue;

                // Must be visible
                const style = getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden'
                    || parseFloat(style.opacity) === 0) continue;

                if (!broad) {
                    // Strict: must look clickable
                    const tag = el.tagName;
                    const clickable = style.cursor === 'pointer'
                        || tag === 'BUTTON'
                        || el.getAttribute('role') === 'button'
                        || el.onclick !== null
                        || (el.tabIndex >= 0 && tag !== 'BODY' && tag !== 'HTML');
                    if (!clickable) continue;
                }

                // Leaf-ish (not a large container)
                if (el.children.length > 3) continue;

                const key = Math.round(r.left) + ',' + Math.round(r.top);
                if (seen.has(key)) continue;
                seen.add(key);

                results.push({
                    text: t,
                    x: Math.round(r.left + r.width/2),
                    y: Math.round(r.top + r.height/2),
                });
            }
            results.sort((a, b) => a.y - b.y);
            return results;
        }''', broad_js) or []
    except Exception:
        return []


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999
    instr = ctx.instruction

    is_click_variant = ('click all' in instr or 'scattered' in instr
                        or 'find' in instr.lower())

    if is_click_variant:
        num_parts = _extract_part_count(instr)
        log(f"step {ctx.step}: [split_parts] click-to-find ({num_parts} parts)")

        clicked_texts = set()
        found_count = 0
        use_broad = False  # escalate to broad scan after first full pass

        # Start from top of page to systematically scan
        page.evaluate('window.scrollTo(0, 0)')
        page.wait_for_timeout(300)

        for scroll_round in range(40):  # ~16000px coverage at 400px/scroll
            p = get_progress_fraction(page)
            if p >= 1.0:
                break

            candidates = _scan_viewport_candidates(page, broad=use_broad)
            candidates = [c for c in candidates
                          if c['text'] not in clicked_texts
                          and c['text'].lower() not in _SKIP_TEXTS]

            found_in_round = False
            for c in candidates:
                p_before = get_progress_fraction(page)
                page.mouse.click(c['x'], c['y'])
                page.wait_for_timeout(200)
                p_after = get_progress_fraction(page)
                clicked_texts.add(c['text'])

                if p_after > p_before:
                    found_count += 1
                    found_in_round = True
                    log(f"step {ctx.step}: [split_parts] found part "
                        f"'{c['text']}' ({p_before:.0%} -> {p_after:.0%})")
                    code = extract_code(page)
                    if code:
                        return code
                    break  # Re-scan from same position after finding a part

            if found_in_round:
                continue  # Re-scan at current scroll position

            # No progress — scroll down (smaller increments for better coverage)
            page.mouse.wheel(0, 350)
            page.wait_for_timeout(250)

            # If at bottom, scroll back to top for another pass
            at_bottom = page.evaluate(
                '() => window.scrollY + window.innerHeight '
                '>= document.documentElement.scrollHeight - 50'
            )
            if at_bottom and scroll_round < 30:
                # Switch to broad scan on second pass (relaxed clickability check)
                if not use_broad:
                    use_broad = True
                    log(f"step {ctx.step}: [split_parts] switching to broad scan")
                page.evaluate('window.scrollTo(0, 0)')
                page.wait_for_timeout(250)

        # Scroll back to top before extracting code — parts loop may leave
        # page scrolled to bottom, hiding the code area above the fold
        page.evaluate('window.scrollTo(0, 0)')
        page.wait_for_timeout(300)

        # Completion sweep
        return extract_code(page) or wait_for_code_mutation(page, 2000)

    # Text-assembly variant
    click_button_by_text(page, ['part', 'show', 'reveal', 'find'], boundary_y)
    page.wait_for_timeout(500)
    body_text = page.evaluate('() => document.body?.innerText || ""')
    code, action_log = split_parts_solver(page, body_text)
    if code:
        log(f"step {ctx.step}: [split_parts] assembled code: {code}")
        return code
    return extract_code(page)
