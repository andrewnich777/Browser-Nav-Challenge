"""Hover challenge: find target element, sustain hover, extract code.

Uses actual mouse hovering to find responsive elements. No CDP pseudo-state
forcing — works on any website.
"""

from agents.v4.context import StepCtx
from agents.v4.helpers import (
    extract_code, wait_for_code_mutation, do_hover_with_js_events,
    find_hover_targets_by_hovering,
)
from primitives import hover_reveal_extract
from log import log


def _find_hover_targets_by_text(page, boundary_y: int) -> list[dict]:
    """Find hover target elements by text patterns, filtered by dimensions.

    Returns candidates sorted by area (largest first — interactive targets
    are typically larger than text labels or instruction paragraphs).

    Filters out instruction text: wide paragraphs (>500px), very long text (>50 chars),
    or very short elements (<25px height).
    """
    candidates = []
    seen_positions = set()

    # Two passes: specific patterns first (exact hover targets),
    # then broader patterns with stricter filtering
    passes = [
        # Pass 1: Specific phrases — likely the actual hover target
        (['Hover here', 'Hover me', 'Hover zone', 'Hover target',
          'Mouse over me', 'Mouse over this'], 500, 60),
        # Pass 2: Broader phrases — stricter width/text filter
        (['Hover Over', 'Hover Area', 'Mouse Over'], 400, 40),
    ]

    for patterns, max_width, max_text_len in passes:
        for pattern in patterns:
            try:
                locs = page.get_by_text(pattern, exact=False).all()
                for loc in locs[:3]:  # max 3 matches per pattern
                    try:
                        bbox = loc.bounding_box(timeout=200)
                        if not bbox:
                            continue
                        cy = bbox['y'] + bbox['height'] / 2
                        if cy >= boundary_y or cy < 0:
                            continue
                        # Skip instruction text: wide paragraphs
                        if bbox['width'] > max_width:
                            continue
                        # Skip tiny elements (not interactive targets)
                        if bbox['height'] < 25:
                            continue
                        # Skip long instruction text
                        try:
                            text = loc.inner_text(timeout=200)
                            if len(text) > max_text_len:
                                continue
                        except Exception:
                            pass
                        cx = round(bbox['x'] + bbox['width'] / 2)
                        icy = round(cy)
                        # Deduplicate by position
                        pos_key = (cx // 20, icy // 20)
                        if pos_key in seen_positions:
                            continue
                        seen_positions.add(pos_key)
                        candidates.append({
                            'pattern': pattern,
                            'x': cx,
                            'y': icy,
                            'area': bbox['width'] * bbox['height'],
                        })
                    except Exception:
                        continue
            except Exception:
                continue

    # Sort by area — larger elements are more likely interactive hover targets
    candidates.sort(key=lambda t: -t['area'])
    return candidates


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999

    # Layer 0 (FAST): Direct text match for hover targets — filtered by dimensions
    # to avoid hovering instruction text paragraphs (which can trigger decoy states)
    targets = _find_hover_targets_by_text(page, boundary_y)
    for t in targets[:3]:
        try:
            log(f"step {ctx.step}: [hover] fast text match: '{t['pattern']}' at ({t['x']},{t['y']})")
            do_hover_with_js_events(page, t['x'], t['y'], hold_ms=2500)
            code = extract_code(page) or wait_for_code_mutation(page, 1000)
            if code:
                return code
            page.mouse.move(0, 0)
            page.wait_for_timeout(200)
        except Exception:
            continue

    # Layer 1: Actually hover elements and observe visual changes
    # Detection leaves mouse ON the last responsive target — check immediately
    hover_targets = find_hover_targets_by_hovering(page, boundary_y)
    if hover_targets:
        log(f"step {ctx.step}: [hover] found {len(hover_targets)} responsive targets")
        for i, target in enumerate(hover_targets[:4]):
            do_hover_with_js_events(page, target['x'], target['y'], hold_ms=2000)
            code = extract_code(page) or wait_for_code_mutation(page, 800)
            if code:
                log(f"step {ctx.step}: [hover] code from hover target: {code}")
                return code
            # Move away to reset before trying next target
            page.mouse.move(0, 0)
            page.wait_for_timeout(200)

    # Layer 2: Use the proven hover_reveal_extract primitive
    result = hover_reveal_extract(page, hold_ms=2000, max_hovers=6)
    code = result.get('code')
    if code:
        log(f"step {ctx.step}: [hover] code from hover_reveal: {code}")
        return code

    # Layer 3: Fallback — find hover-related elements by visible text/cursor
    elements = page.evaluate(f'''() => {{
        const els = document.querySelectorAll(
            'button, [role="button"], div, span, section, p, label');
        return [...els].filter(el => {{
            const r = el.getBoundingClientRect();
            if (r.width < 20 || r.height < 10 || r.width > 600 || r.height > 600) return false;
            if (r.top + r.height/2 > {boundary_y} || r.top <= 0) return false;
            const t = (el.innerText || '').trim().toLowerCase();
            const style = getComputedStyle(el);
            return (t.includes('hover') || t.includes('mouse over')
                || style.cursor === 'pointer');
        }}).map(el => {{
            const r = el.getBoundingClientRect();
            return {{
                x: Math.round(r.x + r.width / 2),
                y: Math.round(r.y + r.height / 2),
                w: Math.round(r.width),
                h: Math.round(r.height),
                text: (el.innerText || '').trim().substring(0, 40),
            }};
        }});
    }}''') or []

    for el in elements[:4]:
        do_hover_with_js_events(page, el['x'], el['y'], hold_ms=2000)
        code = extract_code(page) or wait_for_code_mutation(page, 1000)
        if code:
            return code
        page.mouse.move(0, 0)
        page.wait_for_timeout(200)

    return None
