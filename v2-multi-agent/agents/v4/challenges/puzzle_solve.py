"""Puzzle solve challenge: evaluate math expression, type answer, click Solve.

Key insight: some puzzles auto-submit on typing, others require clicking "Solve".
Click Solve IMMEDIATELY after typing — don't waste time waiting first.
"""

import re
from agents.v4.context import StepCtx
from agents.v4.helpers import (
    type_into_challenge_input, wait_for_code_mutation, extract_code,
    type_react_native, click_button_by_text, js_click_button_by_text,
    get_progress_fraction, SUBMIT_EXCLUDE,
)
from resolvers import eval_expression
from log import log


def _find_puzzle_input(page, boundary_y: int) -> dict | None:
    """Find the puzzle answer input (not the code submission input)."""
    return page.evaluate(f'''() => {{
        const all = document.querySelectorAll(
            'input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"]), textarea');
        for (const el of [...all]) {{
            const box = el.getBoundingClientRect();
            if (box.width < 20 || box.height < 5) continue;
            const ph = (el.placeholder || '').toLowerCase();
            if (ph.includes('enter code') || ph.includes('character code')
                || ph.includes('submit code') || ph.includes('6-character')) continue;
            const label = el.labels && el.labels[0] ? (el.labels[0].innerText || '').toLowerCase() : '';
            if (label.includes('enter code') || label.includes('submit code')) continue;
            return {{
                x: Math.round(box.left + box.width/2),
                y: Math.round(box.top + box.height/2),
                type: el.type || 'text',
            }};
        }}
        return null;
    }}''')


def _type_answer(page, result: str, boundary_y: int) -> bool:
    """Type the answer into the puzzle input. Returns True if typed."""
    # Method 1: scoped input via helper (has boundary_y + code-input exclusion)
    if type_into_challenge_input(page, result, boundary_y):
        return True

    # Method 2: role-based locator with code-input exclusion
    # spinbutton (number input) first — almost always the puzzle input, not code input
    for role in ['spinbutton', 'textbox']:
        try:
            # Get all matching elements and find one that's NOT the code submission input
            locs = page.get_by_role(role).all()
            for loc in locs:
                try:
                    bbox = loc.bounding_box(timeout=300)
                    if not bbox:
                        continue
                    # Skip if clearly below reasonable challenge area
                    if bbox['y'] > 800:
                        continue
                    # Check placeholder to exclude code submission input
                    ph = loc.get_attribute('placeholder') or ''
                    if any(kw in ph.lower() for kw in ['enter code', 'character', 'submit code', '6-char']):
                        continue
                    loc.fill(str(result), timeout=3000)
                    return True
                except Exception:
                    continue
        except Exception:
            continue

    # Method 3: find puzzle input directly (no boundary constraint)
    input_info = _find_puzzle_input(page, boundary_y=99999)
    if input_info:
        log(f"  [puzzle_solve] found input at ({input_info['x']},{input_info['y']}) type={input_info['type']}")
        page.mouse.click(input_info['x'], input_info['y'])
        page.wait_for_timeout(100)
        type_react_native(page, None, str(result))
        return True
    return False


def _click_solve(page, boundary_y: int):
    """Click the Solve/Check/Verify button if present.

    The Solve element may be a <button>, [role=button], or just a <div> with text.
    Tries JS button click → role locator → text locator (catches non-button elements).
    """
    # JS click first — instant, no timeout overhead
    js_clicked = js_click_button_by_text(
        page, ['solve', 'check', 'verify', 'calculate'], boundary_y)
    if js_clicked:
        log(f"  [puzzle_solve] JS-clicked '{js_clicked}' button")
        return

    # Role-based locator (shorter timeout — 500ms instead of 1000ms)
    for name in ['Solve', 'Check', 'Verify', 'Calculate']:
        try:
            btn = page.get_by_role('button', name=re.compile(name, re.I)).first
            btn.click(timeout=500)
            log(f"  [puzzle_solve] clicked '{name}' button")
            return
        except Exception:
            continue

    # Text-based locator — catches <div>, <span>, etc. with "Solve" text
    for name in ['Solve', 'Check', 'Verify', 'Calculate']:
        try:
            loc = page.get_by_text(name, exact=False).first
            bbox = loc.bounding_box(timeout=300)
            if bbox and (bbox['y'] + bbox['height'] / 2) < boundary_y:
                loc.click(timeout=500)
                log(f"  [puzzle_solve] text-clicked '{name}'")
                return
        except Exception:
            continue


def _click_complete(page, boundary_y: int):
    """Click Complete/Reveal/Show Code button that appears after solving the puzzle.

    Many puzzles show a second button after the answer is accepted.
    Uses el.click() (standard DOM API) for React compatibility.
    """
    js_clicked = js_click_button_by_text(
        page, ['complete', 'reveal', 'show code', 'done', 'finish'],
        boundary_y, exclude=SUBMIT_EXCLUDE)
    if js_clicked:
        log(f"  [puzzle_solve] JS-clicked complete: '{js_clicked}'")
        return

    for name in ['Complete', 'Reveal', 'Show Code', 'Done', 'Finish']:
        try:
            btn = page.get_by_role('button', name=re.compile(name, re.I)).first
            btn.click(timeout=500)
            log(f"  [puzzle_solve] clicked '{name}' button")
            return
        except Exception:
            continue


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999

    result = eval_expression(page)
    if not result:
        return None

    log(f"step {ctx.step}: [puzzle_solve] answer: {result}")

    # Type the answer
    if not _type_answer(page, str(result), boundary_y):
        return None

    # Some puzzles auto-solve on fill — check immediately
    page.wait_for_timeout(300)
    code = extract_code(page)
    if code:
        return code

    # Click Solve (many puzzles require this)
    _click_solve(page, boundary_y)
    page.wait_for_timeout(300)

    # Quick check: code may appear instantly after a successful Solve click
    code = extract_code(page)
    if code:
        return code

    # Short mutation wait — if Solve registered, code appears within ~1s
    code = wait_for_code_mutation(page, 1200)
    if code:
        return code

    # Check if Solve actually registered (progress should change)
    p = get_progress_fraction(page)
    if p < 0.5:
        # Solve click likely didn't register — retry immediately before long wait
        _click_solve(page, boundary_y)
        page.wait_for_timeout(300)
        code = extract_code(page) or wait_for_code_mutation(page, 2000)
        if code:
            return code
    else:
        # Answer accepted — wait a bit more for code to appear
        code = wait_for_code_mutation(page, 2000)
        if code:
            return code

    # Click "Complete Challenge"/"Reveal"/"Show Code" button (appears after correct answer)
    _click_complete(page, boundary_y)
    page.wait_for_timeout(300)

    code = extract_code(page) or wait_for_code_mutation(page, 2000)
    if code:
        return code

    # Scroll down slightly — code might appear below viewport
    try:
        page.mouse.wheel(0, 200)
        page.wait_for_timeout(300)
        code = extract_code(page)
        if code:
            return code
        page.mouse.wheel(0, -200)  # scroll back up
    except Exception:
        pass

    # Retry: clear and retype via fill (more reliable than keyboard)
    for role in ['spinbutton', 'textbox']:
        try:
            loc = page.get_by_role(role).first
            bbox = loc.bounding_box(timeout=300)
            if not bbox:
                continue
            ph = loc.get_attribute('placeholder') or ''
            if any(kw in ph.lower() for kw in ['enter code', 'character', 'submit code', '6-char']):
                continue
            loc.fill(str(result), timeout=3000)
            break
        except Exception:
            continue

    _click_solve(page, boundary_y)
    page.wait_for_timeout(500)

    # Click Complete after retry Solve
    _click_complete(page, boundary_y)
    page.wait_for_timeout(300)

    code = extract_code(page) or wait_for_code_mutation(page, 3000)
    if code:
        return code

    # Long wait — some versions have delayed content blocks that take 30+s
    # Only wait if answer was accepted (progress > 0)
    p = get_progress_fraction(page)
    if p > 0:
        log(f"  [puzzle_solve] answer accepted (progress={p:.2f}), long wait for code")
        code = wait_for_code_mutation(page, 10000)
        if code:
            return code
        # Try Complete one more time
        _click_complete(page, boundary_y)
        page.wait_for_timeout(300)
        return extract_code(page) or wait_for_code_mutation(page, 3000)

    return None
