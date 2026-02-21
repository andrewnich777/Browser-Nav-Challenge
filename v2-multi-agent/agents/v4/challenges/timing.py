"""Timing challenge: click Capture multiple times while code rotates.

Key insight: the displayed code during captures is a DECOY that changes every N
seconds. The REAL code only appears AFTER all required captures complete.
Do NOT extract code during the capture loop — only after progress reaches 100%.

The real code appears near "The real code is:" text but may be in a child element,
not inline. Must search for 6-char codes near that text, not just regex on innerText.
"""

import re
from agents.v4.context import StepCtx
from agents.v4.helpers import (
    click_button_by_text, wait_for_code_mutation,
    js_click_button_by_text, get_progress_fraction,
)
from config import CHARSET
from log import log


def _click_capture(page, boundary_y: int) -> bool:
    """Click a Capture-like button. JS first (instant), role-based fallback."""
    # JS click first — instant, no timeout overhead
    js_clicked = js_click_button_by_text(
        page, ['capture'], boundary_y)
    if js_clicked:
        return True

    # Role-based locator for "Capture" only (not "Click" which matches everything)
    try:
        btn = page.get_by_role("button", name=re.compile(r"Capture", re.I)).first
        btn.click(timeout=1500)
        return True
    except Exception:
        pass

    # Broader JS fallback
    js_clicked = js_click_button_by_text(
        page, ['click me', 'grab', 'catch'], boundary_y)
    if js_clicked:
        return True

    # Playwright mouse last resort
    click_button_by_text(
        page, ['capture', 'click', 'grab', 'catch'], boundary_y)
    return True


def _find_real_code(page) -> str | None:
    """Find the real code that appears after 'Challenge completed!' or 'real code is'.

    The real code may be:
    1. Inline after "The real code is: ABCDEF"
    2. In a child element: <p>The real code is: <span>ABCDEF</span></p>
    3. In a sibling element after the "completed" text
    4. A green/success-colored 6-char string
    """
    charset = CHARSET
    try:
        code = page.evaluate(r'''(charset) => {
            const charsetSet = new Set(charset);
            function isValidCode(s) {
                if (!s || s.length !== 6) return false;
                for (const c of s) { if (!charsetSet.has(c)) return false; }
                return true;
            }

            // Strategy 1: Find element containing "real code" and extract code AFTER the phrase.
            // Important: the current rotating display code is also in the same text,
            // so we must only extract codes that appear AFTER "real code is:" not before.
            const allEls = document.querySelectorAll('*');
            for (const el of allEls) {
                const t = (el.innerText || '').trim();
                if (t.length > 500) continue;
                const tl = t.toLowerCase();
                if (tl.includes('real code') || tl.includes('actual code') || tl.includes('final code')) {
                    // Check inline regex: code immediately after "real code is:"
                    const m = t.match(/(?:real|actual|final)\s+code\s+(?:is|:)\s*([A-Z0-9]{6})/i);
                    if (m && isValidCode(m[1].toUpperCase())) return m[1].toUpperCase();

                    // Find the position of "real code" and only look for codes AFTER it
                    const phraseIdx = tl.search(/(?:real|actual|final)\s+code/);
                    if (phraseIdx >= 0) {
                        const afterPhrase = t.substring(phraseIdx);
                        // Check children that appear after the phrase
                        for (const child of el.querySelectorAll('*')) {
                            const ct = (child.innerText || '').trim();
                            if (!isValidCode(ct)) continue;
                            // Verify this child's text appears after the "real code" phrase
                            const childIdx = t.indexOf(ct, phraseIdx);
                            if (childIdx > phraseIdx) return ct;
                        }
                        // Check 6-char sequences after the phrase
                        const codesAfter = afterPhrase.match(/[A-Z0-9]{6}/g) || [];
                        // Skip the first match if it's part of "real code" keyword itself
                        for (const c of codesAfter) {
                            if (isValidCode(c)) return c;
                        }
                    }
                }
            }

            // Strategy 2: Find green/success-colored text with 6 chars
            for (const el of allEls) {
                const t = (el.innerText || '').trim();
                if (!isValidCode(t)) continue;
                const style = getComputedStyle(el);
                const color = style.color || '';
                // Green text (success indicator)
                const rgb = color.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
                if (rgb) {
                    const r = parseInt(rgb[1]), g = parseInt(rgb[2]), b = parseInt(rgb[3]);
                    if (g > r + 30 && g > b + 30) return t; // distinctly green
                }
            }

            // Strategy 3: Find "completed" text and look for code in nearby siblings
            for (const el of allEls) {
                const t = (el.innerText || '').trim().toLowerCase();
                if (t.includes('completed') || t.includes('challenge complete')) {
                    let sibling = el.nextElementSibling;
                    for (let i = 0; i < 5 && sibling; i++) {
                        const st = (sibling.innerText || '').trim();
                        const codes = st.match(/[A-Z0-9]{6}/g) || [];
                        for (const c of codes) { if (isValidCode(c)) return c; }
                        sibling = sibling.nextElementSibling;
                    }
                }
            }

            return null;
        }''', charset)
        return code
    except Exception:
        return None


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999
    instr = ctx.instruction

    # Parse required captures: "click 'Capture' at least N times"
    m = re.search(r'at\s+least\s+(\d+)\s+times?', instr, re.I)
    required = int(m.group(1)) if m else 3

    # Parse timing interval: "changes every N seconds"
    m = re.search(r'every\s+(\d+)\s*seconds?', instr, re.I)
    interval_s = int(m.group(1)) if m else 3

    # Short initial wait for challenge to render
    page.wait_for_timeout(500)

    # Click start/begin button if present
    click_button_by_text(page, ['start', 'begin', 'go'], boundary_y)

    log(f"step {ctx.step}: [timing] need {required} captures, interval={interval_s}s")

    # Interval-based capture loop. The timing challenge does NOT use a standard
    # progress bar (get_progress_fraction returns 0). Instead, we trust the
    # interval timing: click once per window, spaced by the rotation interval.
    # Double-click within each window for reliability (catches misses).
    for window in range(required + 1):  # +1 safety window
        _click_capture(page, boundary_y)
        page.wait_for_timeout(200)
        _click_capture(page, boundary_y)  # double-click for reliability
        log(f"step {ctx.step}: [timing] capture window {window + 1}/{required}")

        # Check if the real code appeared early (challenge may complete before all windows)
        if window >= required - 1:
            page.wait_for_timeout(300)
            real_code = _find_real_code(page)
            if real_code:
                log(f"step {ctx.step}: [timing] found real code early: {real_code}")
                return real_code

        if window < required:
            # Wait for the code to rotate to next window
            page.wait_for_timeout(interval_s * 1000 - 200)

    # Wait for the real code to appear
    page.wait_for_timeout(1000)

    # Strategy 1: Find the real code specifically (not the rotating decoy)
    real_code = _find_real_code(page)
    if real_code:
        log(f"step {ctx.step}: [timing] found real code: {real_code}")
        return real_code

    # Strategy 2: Click completion button, then look again
    js_click_button_by_text(page, ['complete', 'reveal', 'show', 'done', 'finish'], boundary_y)
    page.wait_for_timeout(500)

    real_code = _find_real_code(page)
    if real_code:
        log(f"step {ctx.step}: [timing] found real code after button: {real_code}")
        return real_code

    # Strategy 3: Wait for mutation (real code may render with delay)
    return wait_for_code_mutation(page, 5000)
