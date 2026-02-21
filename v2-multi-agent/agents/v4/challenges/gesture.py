"""Gesture/draw challenge: parse direction or stroke count, draw on canvas.

Reliability: Click canvas first for focus. Use more interpolation steps (10)
for smoother strokes that canvas apps detect better. More varied stroke paths
to avoid the app rejecting near-duplicate angles.
"""

import re
from agents.v4.context import StepCtx
from agents.v4.helpers import (
    wait_for_code_mutation, extract_code, js_click_button_by_text, SUBMIT_EXCLUDE,
    get_progress_fraction,
)
from primitives import draw_stroke_on_canvas, read_progress
from log import log

# Direction → normalized path (0.0-1.0 coordinates)
DIRECTION_PATHS = {
    'right':      [[0.1, 0.5], [0.9, 0.5]],
    'left':       [[0.9, 0.5], [0.1, 0.5]],
    'down':       [[0.5, 0.1], [0.5, 0.9]],
    'up':         [[0.5, 0.9], [0.5, 0.1]],
    'diagonal':   [[0.1, 0.1], [0.9, 0.9]],
    'circle':     [[0.5, 0.2], [0.8, 0.5], [0.5, 0.8], [0.2, 0.5], [0.5, 0.2]],
    'zigzag':     [[0.1, 0.2], [0.5, 0.8], [0.9, 0.2]],
}

# Shape → closed path coordinates (0.0-1.0)
SHAPE_PATHS = {
    'triangle':  [[0.5, 0.15], [0.15, 0.85], [0.85, 0.85], [0.5, 0.15]],
    'square':    [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8], [0.2, 0.2]],
    'rectangle': [[0.15, 0.25], [0.85, 0.25], [0.85, 0.75], [0.15, 0.75], [0.15, 0.25]],
    'star':      [[0.5, 0.1], [0.6, 0.4], [0.9, 0.4], [0.65, 0.6],
                  [0.75, 0.9], [0.5, 0.7], [0.25, 0.9], [0.35, 0.6],
                  [0.1, 0.4], [0.4, 0.4], [0.5, 0.1]],
}

# 12 varied strokes — different angles and positions to avoid app-level dedup.
# Mix of 2-point (straight) and 3-point (curved) paths for detection variety.
# Some canvas apps reject short/fast strokes — 3-point paths create slower, longer arcs.
VARIED_STROKES = [
    [[0.1, 0.3], [0.5, 0.3], [0.9, 0.3]],      # horizontal top (3-point)
    [[0.3, 0.1], [0.3, 0.5], [0.3, 0.9]],      # vertical left (3-point)
    [[0.1, 0.7], [0.5, 0.7], [0.9, 0.7]],      # horizontal bottom (3-point)
    [[0.7, 0.1], [0.7, 0.5], [0.7, 0.9]],      # vertical right (3-point)
    [[0.1, 0.1], [0.5, 0.5], [0.9, 0.9]],      # diagonal down-right (3-point)
    [[0.9, 0.1], [0.5, 0.5], [0.1, 0.9]],      # diagonal down-left (3-point)
    [[0.2, 0.5], [0.8, 0.5]],                    # horizontal center (2-point)
    [[0.5, 0.2], [0.5, 0.8]],                    # vertical center (2-point)
    [[0.1, 0.2], [0.3, 0.5], [0.5, 0.8]],      # angled left-down (3-point)
    [[0.5, 0.2], [0.7, 0.5], [0.9, 0.8]],      # angled right-down (3-point)
    [[0.2, 0.1], [0.5, 0.3], [0.8, 0.5]],      # angled shallow right (3-point)
    [[0.8, 0.1], [0.5, 0.3], [0.2, 0.5]],      # angled shallow left (3-point)
]

# Higher interpolation steps = smoother, slower strokes that canvas apps detect better.
# v=2 canvas is particularly picky — needs 15+ steps per segment.
STROKE_STEPS_DIRECTION = 10   # 2-point directional strokes (fast, doesn't need smoothness)
STROKE_STEPS_SHAPE = 15       # Multi-segment closed paths (needs smoothness)


def _focus_canvas(page):
    """Click the center of the canvas to ensure it has focus."""
    try:
        canvas = page.query_selector('canvas')
        if canvas:
            box = canvas.bounding_box()
            if box:
                cx = box['x'] + box['width'] / 2
                cy = box['y'] + box['height'] / 2
                page.mouse.click(cx, cy)
                page.wait_for_timeout(100)
    except Exception:
        pass


def _click_complete(page, boundary_y: int) -> str | None:
    """Click Complete/Submit/Reveal button after drawing, then check for code.

    Also checks for any enabled button near the canvas element.
    Uses el.click() (standard DOM API) for React compatibility.
    """
    clicked = js_click_button_by_text(
        page,
        ['complete', 'finish', 'done', 'reveal', 'show code', 'verify',
         'submit drawing', 'submit', 'check'],
        boundary_y, exclude=SUBMIT_EXCLUDE)
    if clicked:
        log(f"  [gesture] clicked complete: '{clicked}'")
        page.wait_for_timeout(500)
        return extract_code(page) or wait_for_code_mutation(page, 1500)

    # Fallback: click any enabled button near the canvas
    try:
        near = page.evaluate('''() => {
            const canvas = document.querySelector('canvas');
            if (!canvas) return null;
            const cRect = canvas.getBoundingClientRect();
            for (const btn of document.querySelectorAll('button')) {
                const bRect = btn.getBoundingClientRect();
                if (Math.abs(bRect.top - cRect.bottom) < 200 && !btn.disabled) {
                    const t = (btn.textContent || '').trim();
                    if (t.length > 1 && t.length < 30) {
                        btn.click();
                        return t;
                    }
                }
            }
            return null;
        }''')
        if near:
            log(f"  [gesture] clicked near-canvas button: '{near}'")
            page.wait_for_timeout(500)
            return extract_code(page) or wait_for_code_mutation(page, 1500)
    except Exception:
        pass
    return None


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999
    instr = ctx.instruction.lower()

    # Find canvas
    canvas = page.query_selector('canvas')
    if not canvas:
        return None

    # Focus canvas before any drawing
    _focus_canvas(page)

    # Parse stroke count from instruction ("draw at least 3 strokes")
    m = re.search(r'(\d+)\s*stroke', instr)
    num_strokes = int(m.group(1)) if m else 1

    # Parse shape from instruction
    shape_path = None
    for shape, coords in SHAPE_PATHS.items():
        if shape in instr:
            shape_path = coords
            log(f"step {ctx.step}: [gesture] shape: {shape}")
            break

    if shape_path:
        # Draw shape as separate strokes (one per side).
        # Canvas app needs distinct mousedown/mouseup per side to register each stroke.
        # Lines may briefly disappear between strokes (canvas clears on mousedown
        # then redraws committed strokes) — this is inherent canvas behavior.
        for i in range(len(shape_path) - 1):
            segment = [shape_path[i], shape_path[i + 1]]
            draw_stroke_on_canvas(page, 'canvas', segment, steps_per_segment=STROKE_STEPS_SHAPE)
            page.wait_for_timeout(100)

        page.wait_for_timeout(300)
        code = extract_code(page) or wait_for_code_mutation(page, 1500)
        if code:
            return code
        code = _click_complete(page, boundary_y)
        if code:
            return code
        # Shape didn't produce code — allow fallthrough to direction/multi-stroke
        shape_path = None

    # Parse direction from instruction
    direction_path = None
    for direction, coords in DIRECTION_PATHS.items():
        if direction in instr:
            direction_path = coords
            log(f"step {ctx.step}: [gesture] direction: {direction}")
            break

    if direction_path and num_strokes <= 1 and not shape_path:
        # Single directional stroke
        result = draw_stroke_on_canvas(page, 'canvas', direction_path, steps_per_segment=STROKE_STEPS_DIRECTION)
        if result.get('success'):
            page.wait_for_timeout(300)
            code = extract_code(page) or wait_for_code_mutation(page, 1500)
            if code:
                return code
            return _click_complete(page, boundary_y)
    elif not shape_path:
        # Multiple strokes needed
        log(f"step {ctx.step}: [gesture] drawing {num_strokes} strokes")
        progress_strokes = 0
        max_attempts = num_strokes + 4  # trimmed retry budget (still generous)
        for i in range(max_attempts):
            # Check early completion BEFORE each stroke
            frac = get_progress_fraction(page)
            if frac >= 1.0:
                log(f"step {ctx.step}: [gesture] progress complete after {progress_strokes} strokes")
                break

            # Move mouse off canvas between strokes to force "new stroke" detection.
            # Many canvas apps distinguish new strokes by mouseenter after mouseleave.
            # Move to just outside canvas edge (less visually jarring than screen corner).
            try:
                canvas = page.query_selector('canvas')
                if canvas:
                    box = canvas.bounding_box()
                    if box:
                        page.mouse.move(box['x'] - 10, box['y'] + box['height'] / 2)
                    else:
                        page.mouse.move(10, 10)
                else:
                    page.mouse.move(10, 10)
                page.wait_for_timeout(80)
            except Exception:
                pass

            path = VARIED_STROKES[i % len(VARIED_STROKES)]
            result = draw_stroke_on_canvas(page, 'canvas', path, steps_per_segment=STROKE_STEPS_DIRECTION)

            # Only count strokes where progress actually changed (not just pixel-verified)
            if result.get('progress_changed'):
                progress_strokes += 1

            # Wait for canvas to process stroke before next one
            page.wait_for_timeout(400)

        page.wait_for_timeout(300)
        code = extract_code(page) or wait_for_code_mutation(page, 1500)
        if code:
            return code
        return _click_complete(page, boundary_y)

    return None
