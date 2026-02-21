"""Sequence challenge: complete 4 sub-tasks (click, hover, type, scroll) in order.

Uses progress-gating: reads ✓ status to skip completed tasks, checks progress
after each action to detect success/failure, retries on failure.
"""

import re
from agents.v4.context import StepCtx
from agents.v4.helpers import (
    click_button_by_text, click_all_matching_buttons,
    query_buttons_in_scope, type_into_challenge_input,
    wait_for_code_mutation, extract_code, SUBMIT_EXCLUDE,
    get_progress_fraction, read_task_status,
    find_hover_target_scored, do_hover_with_js_events,
    type_react_native, wait_for_animations_done,
    find_hover_targets_by_hovering,
)
from primitives import read_progress
from log import log


# Buttons to skip during sub-task execution (completion buttons that end the challenge)
_SKIP_GENERIC = {'complete', 'reveal', 'show', 'done', 'finish', 'extract'}


def _do_click(page, boundary_y):
    """Sub-task: click a challenge button (JS click for React compatibility)."""
    # JS-level click for React guaranteed handler firing
    clicked = page.evaluate(f'''() => {{
        const btns = document.querySelectorAll('button, [role="button"]');
        for (const btn of btns) {{
            const t = (btn.innerText || '').trim();
            const tl = t.toLowerCase();
            if (tl === 'click me' || tl.includes('click me')) {{
                const r = btn.getBoundingClientRect();
                if (r.top + r.height/2 < {boundary_y}) {{
                    btn.click();
                    return t;
                }}
            }}
        }}
        return null;
    }}''')
    if clicked:
        return
    # Fallback: Playwright click on first non-completion button
    btns = query_buttons_in_scope(page, boundary_y, exclude=SUBMIT_EXCLUDE)
    for b in btns:
        if not any(kw in b['text'].lower() for kw in _SKIP_GENERIC):
            page.mouse.click(b['x'], b['y'])
            return
    if btns:
        page.mouse.click(btns[0]['x'], btns[0]['y'])


def _do_hover(page, boundary_y):
    """Sub-task: hover over a target by actually hovering and observing.

    Priority: fast text/DOM match → slow hover detection → CSS heuristic → fallback.
    """
    # Reset cursor to neutral position so mouseenter fires when we reach the target.
    # Per MDN spec, mouseenter only fires when cursor crosses from outside to inside.
    page.mouse.move(10, 10)
    page.wait_for_timeout(100)

    # Tier 0: Direct text match via JS evaluate + mouse.move
    # Uses page.evaluate() for coord discovery and page.mouse.move() for hover.
    # Neither triggers Playwright's locator handler, avoiding popup interference.
    # Two-pass search: first look for specific phrases (only in actual target),
    # then broader patterns. Skips short text (<15 chars) to avoid matching
    # tab labels like "hover area" instead of the actual "Hover over this area" box.
    try:
        hover_match = page.evaluate(f'''() => {{
            // Pass 1: specific phrases (only appear in actual hover targets)
            const specificPatterns = ['hover over', 'hover here', 'mouse over'];
            // Pass 2: broader patterns (may also match tab labels)
            const broadPatterns = ['hover area', 'hover me', 'hover zone',
                                   'hover target', 'hover box'];

            function findMatch(patterns, minLen) {{
                let best = null;
                for (const el of document.querySelectorAll('div, span, section, p, button, [role="button"], label')) {{
                    const r = el.getBoundingClientRect();
                    if (r.width < 10 || r.height < 5 || r.width > 1200 || r.height > 300) continue;
                    if (r.top + r.height/2 > {boundary_y} || r.top <= 0) continue;
                    const t = (el.innerText || '').trim();
                    if (t.length > 80 || t.length < minLen) continue;
                    const tl = t.toLowerCase();
                    for (const p of patterns) {{
                        if (tl.includes(p)) {{
                            const candidate = {{x: Math.round(r.x + r.width/2),
                                               y: Math.round(r.y + r.height/2), text: t,
                                               area: r.width * r.height}};
                            // Prefer larger elements (interactive targets > tab labels)
                            if (!best || candidate.area > best.area) best = candidate;
                        }}
                    }}
                }}
                return best;
            }}

            // Pass 1: specific phrases, no min length needed
            let match = findMatch(specificPatterns, 0);
            if (match) return match;
            // Pass 2: broader patterns, skip short tab labels (<15 chars)
            return findMatch(broadPatterns, 15);
        }}''')
        if hover_match:
            log(f"  [sequence._do_hover] Tier 0 text match: '{hover_match['text'][:30]}' "
                f"at ({hover_match['x']},{hover_match['y']})")
            page.mouse.move(hover_match['x'], hover_match['y'], steps=5)
            page.wait_for_timeout(2000)
            wait_for_animations_done(page, timeout_ms=500)
            return
    except Exception:
        pass

    # Tier 0.5: Any element with "hover" in text (catches variants)
    # Prefers larger elements to avoid matching tab labels vs actual interactive areas.
    try:
        hover_el = page.evaluate(f'''() => {{
            let best = null;
            for (const el of document.querySelectorAll('div, span, section, p, button, [role="button"], label')) {{
                const r = el.getBoundingClientRect();
                if (r.width < 20 || r.height < 10 || r.width > 1200 || r.height > 200) continue;
                if (r.top + r.height/2 > {boundary_y} || r.top <= 0) continue;
                const t = (el.innerText || '').trim();
                if (t.length > 80 || t.length < 15) continue;
                const tl = t.toLowerCase();
                if (tl.includes('hover') || tl.includes('mouse over')) {{
                    const candidate = {{x: Math.round(r.x + r.width/2),
                                       y: Math.round(r.y + r.height/2), text: t,
                                       area: r.width * r.height}};
                    if (!best || candidate.area > best.area) best = candidate;
                }}
            }}
            return best;
        }}''')
        if hover_el:
            log(f"  [sequence._do_hover] DOM hover element: '{hover_el['text'][:30]}'")
            page.mouse.move(hover_el['x'], hover_el['y'], steps=3)
            page.wait_for_timeout(2500)
            wait_for_animations_done(page, timeout_ms=1000)
            return
    except Exception:
        pass

    # Tier 1: CSSOM :hover rule scanning (fast, no actual hovering)
    target = find_hover_target_scored(page, boundary_y)
    if target:
        log(f"  [sequence._do_hover] scored target at ({target['x']},{target['y']}) "
            f"score={target.get('score', 0)}")
        do_hover_with_js_events(page, target['x'], target['y'], hold_ms=1500)
        wait_for_animations_done(page, timeout_ms=500)
        return

    # Tier 2: Actually hover elements and observe visual changes (SLOW — last resort)
    hover_targets = find_hover_targets_by_hovering(page, boundary_y)
    if hover_targets:
        t = hover_targets[0]
        log(f"  [sequence._do_hover] responsive target at ({t['x']},{t['y']}) "
            f"response={t.get('response_type', '?')}")
        do_hover_with_js_events(page, t['x'], t['y'], hold_ms=1500)
        wait_for_animations_done(page, timeout_ms=500)
        return

    # Fallback: hover over first non-completion button
    btns = query_buttons_in_scope(page, boundary_y, exclude=SUBMIT_EXCLUDE)
    for b in btns:
        if not any(kw in b['text'].lower() for kw in _SKIP_GENERIC):
            do_hover_with_js_events(page, b['x'], b['y'], hold_ms=1500)
            return
    if btns:
        do_hover_with_js_events(page, btns[0]['x'], btns[0]['y'], hold_ms=2500)


def _do_type(page, boundary_y):
    """Sub-task: type 'hello' into the challenge input."""
    # Tier 1: Role-based locator + fill (most reliable)
    try:
        loc = page.get_by_role("textbox").first
        bbox = loc.bounding_box(timeout=500)
        if bbox and (bbox['y'] + bbox['height'] / 2) < boundary_y:
            loc.fill("hello")
            return
    except Exception:
        pass

    # Tier 2: Scoped input + fill (via helper)
    if type_into_challenge_input(page, 'hello', boundary_y):
        return
    # Tier 3: React native value setter (handles inputs outside boundary)
    type_react_native(page, None, 'hello')


def _do_scroll(page, boundary_y):
    """Sub-task: scroll inside the first scrollable container.

    Finds the actual scrollable element (overflow:scroll/auto), positions mouse
    inside it, then wheels. JS fallback scrolls to bottom + dispatches event.
    """
    # Ensure page scroll is at top so scroll container is visible
    page.evaluate('window.scrollTo(0, 0)')
    page.wait_for_timeout(100)

    # Find the actual scrollable container (not just the text label)
    scrolled = page.evaluate(f'''() => {{
        function findScrollable() {{
            // Priority 1: scrollable container with scroll-related text
            for (const el of document.querySelectorAll('div, section')) {{
                const t = (el.innerText || '').trim().toLowerCase();
                const s = getComputedStyle(el);
                const isScrollable = (s.overflow === 'auto' || s.overflow === 'scroll' ||
                     s.overflowY === 'auto' || s.overflowY === 'scroll') &&
                    el.scrollHeight > el.clientHeight + 10;
                if (isScrollable && (t.includes('scroll') || t.includes('keep'))) {{
                    const r = el.getBoundingClientRect();
                    if (r.top + r.height/2 < {boundary_y}) return el;
                }}
            }}
            // Priority 2: any scrollable container in challenge area
            for (const el of document.querySelectorAll('div, section')) {{
                const s = getComputedStyle(el);
                if ((s.overflow === 'auto' || s.overflow === 'scroll' ||
                     s.overflowY === 'auto' || s.overflowY === 'scroll') &&
                    el.scrollHeight > el.clientHeight + 10 &&
                    el !== document.body && el !== document.documentElement) {{
                    const r = el.getBoundingClientRect();
                    if (r.top + r.height/2 < {boundary_y}) return el;
                }}
            }}
            return null;
        }}
        const el = findScrollable();
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return {{x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2),
                scrollHeight: el.scrollHeight, clientHeight: el.clientHeight}};
    }}''')
    if scrolled:
        # Mouse wheel inside the scroll container (human-like)
        page.mouse.move(scrolled['x'], scrolled['y'])
        scroll_needed = scrolled['scrollHeight'] - scrolled['clientHeight']
        # Wheel just enough to reach the bottom, avoid overflowing to page scroll
        chunks = max(1, min(5, scroll_needed // 200))
        for _ in range(chunks):
            page.mouse.wheel(0, 300)
            page.wait_for_timeout(150)


def _detect_task_order(page, boundary_y: int) -> list[str]:
    """Detect the order of sub-tasks from instruction text.

    Reads task list elements and parses which tasks exist and their order.
    Falls back to default order: click → hover → type → scroll.
    """
    try:
        tasks = page.evaluate(f'''() => {{
            const order = [];
            const els = document.querySelectorAll('li, div, span, p');
            for (const el of els) {{
                const t = (el.innerText || '').trim().toLowerCase();
                if (t.length > 100) continue;
                const r = el.getBoundingClientRect();
                if (r.top + r.height/2 > {boundary_y}) continue;
                if (t.includes('click') && !order.includes('click')) order.push('click');
                if (t.includes('hover') && !order.includes('hover')) order.push('hover');
                if ((t.includes('type text') || t.includes('type here') || t.includes('type hello')
                     || (t.includes('type ') && !t.includes('type='))) && !order.includes('type'))
                    order.push('type');
                if (t.includes('scroll') && !order.includes('scroll')) order.push('scroll');
            }}
            return order;
        }}''') or []
        if len(tasks) >= 2:
            return tasks
    except Exception:
        pass
    return ['click', 'hover', 'type', 'scroll']


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999

    # Read which tasks are already complete (✓ marks)
    status = read_task_status(page)
    prog_before = get_progress_fraction(page)

    # Detect task order dynamically from instruction text
    task_order = _detect_task_order(page, boundary_y)
    handlers = {
        'click': _do_click,
        'hover': _do_hover,
        'type': _do_type,
        'scroll': _do_scroll,
    }
    sub_tasks = [
        (name, handlers[name], status.get(name, False))
        for name in task_order
        if name in handlers
    ]

    log(f"step {ctx.step}: [sequence] status={status} progress={prog_before:.2f}")

    for name, handler, already_done in sub_tasks:
        if already_done:
            log(f"step {ctx.step}: [sequence] skipping {name} (already ✓)")
            continue

        log(f"step {ctx.step}: [sequence] executing sub-task: {name}")
        p_before = get_progress_fraction(page)
        handler(page, boundary_y)
        page.wait_for_timeout(400)

        # Check if this action produced a code
        code = extract_code(page) or wait_for_code_mutation(page, 500)
        if code:
            return code

        # Progress gate: check if action registered
        p_after = get_progress_fraction(page)
        if p_after > p_before:
            log(f"step {ctx.step}: [sequence] {name} advanced progress "
                f"{p_before:.2f} → {p_after:.2f}")
        else:
            log(f"step {ctx.step}: [sequence] {name} NO progress change, retrying")
            # Retry the same action once
            handler(page, boundary_y)
            page.wait_for_timeout(400)
            code = extract_code(page) or wait_for_code_mutation(page, 500)
            if code:
                return code

        if get_progress_fraction(page) >= 1.0:
            log(f"step {ctx.step}: [sequence] progress complete")
            break

    # Click Complete button (may need "Complete (4/4)")
    click_all_matching_buttons(
        page, ['complete (4/4)', 'complete (3/3)', 'complete'], boundary_y,
        settle_ms=300, max_clicks=2,
    )
    page.wait_for_timeout(500)

    # Final poll for code
    return extract_code(page) or wait_for_code_mutation(page, 2000)
