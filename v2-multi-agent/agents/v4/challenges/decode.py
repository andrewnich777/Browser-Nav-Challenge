"""Decode challenge: decode base64/hex/rot13/reverse/caesar text, type answer if interactive.

Key insight: detect the encoding type from the instruction text FIRST, then
only try that specific decoder. Trying wrong decoders (e.g. ROT13 on a base64
challenge) types garbage into the input and corrupts challenge state.
"""

import re
import base64
from agents.v4.context import StepCtx
from agents.v4.helpers import (
    type_into_challenge_input, click_button_by_text,
    wait_for_code_mutation, extract_code,
)
from resolvers import rot13
from log import log


def _try_submit(page, value: str, boundary_y: int, step: int, label: str) -> str | None:
    """Type a decoded value, click submit, check for code."""
    if type_into_challenge_input(page, value, boundary_y):
        code = wait_for_code_mutation(page, 800) or extract_code(page)
        if code:
            log(f"step {step}: [decode] code from {label} (post-type): {code}")
            return code
        click_button_by_text(page, ['complete', 'decode', 'submit', 'reveal', 'check', 'verify', 'go'], boundary_y)
        page.wait_for_timeout(300)
    code = wait_for_code_mutation(page, 1500) or extract_code(page)
    if code:
        log(f"step {step}: [decode] code from {label}: {code}")
    return code


def _caesar_shift(text: str, shift: int) -> str:
    """Apply Caesar cipher shift to text."""
    result = []
    for c in text:
        if c.isalpha():
            base = ord('A') if c.isupper() else ord('a')
            result.append(chr((ord(c) - base + shift) % 26 + base))
        else:
            result.append(c)
    return ''.join(result)


def _detect_encoding(text_lower: str) -> str:
    """Detect encoding type from instruction text."""
    if 'base64' in text_lower or 'base 64' in text_lower:
        return 'base64'
    if 'rot13' in text_lower or 'rot-13' in text_lower:
        return 'rot13'
    if any(kw in text_lower for kw in ['reverse', 'backwards', 'reversed']):
        return 'reverse'
    if 'caesar' in text_lower or 'cipher' in text_lower:
        return 'caesar'
    if 'hex' in text_lower or 'hexadecimal' in text_lower:
        return 'hex'
    return 'unknown'


def _solve_base64(page, raw_text: str, boundary_y: int, step: int) -> str | None:
    """Handle base64 encoded challenges."""
    b64_candidates = re.findall(r'\b([A-Za-z0-9+/]{8,}={0,2})\b', raw_text)
    for candidate in b64_candidates:
        try:
            decoded = base64.b64decode(candidate).decode('utf-8', errors='ignore')
            if not decoded or len(decoded) < 2:
                continue
            log(f"step {step}: [decode] base64 '{candidate}' -> '{decoded}'")

            # DECODE_ME hint pattern — type 'DECODE' into input, click Reveal
            if re.match(r'DECODE_ME_\d+', decoded):
                log(f"step {step}: [decode] hint pattern: {decoded}")
                # Type directly into challenge textbox (exclude code submission input)
                for val in ['DECODE', decoded]:
                    typed = False
                    for role in ['textbox', 'spinbutton']:
                        try:
                            locs = page.get_by_role(role).all()
                            for loc in locs:
                                try:
                                    ph = loc.get_attribute('placeholder') or ''
                                    if any(kw in ph.lower() for kw in
                                           ['enter code', 'character', 'submit code', '6-char']):
                                        continue
                                    loc.fill(val)
                                    log(f"step {step}: [decode] typed '{val}' into {role}")
                                    typed = True
                                    break
                                except Exception:
                                    continue
                            if typed:
                                break
                        except Exception:
                            continue
                    if not typed:
                        type_into_challenge_input(page, val, 99999)
                        log(f"step {step}: [decode] typed '{val}' via challenge input")
                    # Click Reveal/Submit — text-based FIRST (handles <div>, <p>, etc.)
                    # then role='button' fallback. Text-based is faster for non-button elements.
                    clicked_btn = False
                    for btn_text in ['Reveal Next Section', 'Reveal', 'Complete',
                                     'Decode', 'Submit', 'Check', 'Continue']:
                        try:
                            loc = page.get_by_text(btn_text, exact=False).first
                            bbox = loc.bounding_box(timeout=500)
                            if bbox and bbox['width'] > 10:
                                loc.click(timeout=800)
                                log(f"step {step}: [decode] clicked '{btn_text}' via text")
                                clicked_btn = True
                                break
                        except Exception:
                            continue
                    if not clicked_btn:
                        for btn_name in ['Reveal', 'Complete', 'Submit', 'Check']:
                            try:
                                page.get_by_role('button', name=btn_name, exact=False).first.click(timeout=500)
                                log(f"step {step}: [decode] clicked '{btn_name}' button")
                                clicked_btn = True
                                break
                            except Exception:
                                continue
                    page.wait_for_timeout(500)
                    code = extract_code(page) or wait_for_code_mutation(page, 3000)
                    if code:
                        log(f"step {step}: [decode] code from hint ({val}): {code}")
                        return code
                # Final wait — content blocks may load with delay after Reveal
                page.wait_for_timeout(2000)
                code = extract_code(page) or wait_for_code_mutation(page, 3000)
                if code:
                    log(f"step {step}: [decode] code from delayed reveal: {code}")
                    return code
                return None  # Don't try other b64 candidates

            # Check if decoded is a valid 6-char code
            from config import CHARSET
            if len(decoded) == 6 and all(c in CHARSET for c in decoded):
                code = _try_submit(page, decoded, boundary_y, step, 'base64')
                if code:
                    return code
            else:
                code = _try_submit(page, decoded, boundary_y, step, 'base64')
                if code:
                    return code
        except Exception:
            pass
    return None


def _solve_rot13(page, boundary_y: int, step: int) -> str | None:
    """Handle ROT13 encoded challenges."""
    decoded = rot13(page)
    if decoded:
        log(f"step {step}: [decode] rot13 result: {decoded}")
        return _try_submit(page, decoded, boundary_y, step, 'rot13')
    return None


def _solve_reverse(page, raw_text: str, boundary_y: int, step: int) -> str | None:
    """Handle reverse string challenges."""
    quoted = re.findall(r'"([^"]+)"|\'([^\']+)\'', raw_text)
    for groups in quoted:
        token = groups[0] or groups[1]
        if len(token) >= 2:
            reversed_token = token[::-1]
            log(f"step {step}: [decode] reverse '{token}' -> '{reversed_token}'")
            code = _try_submit(page, reversed_token, boundary_y, step, 'reverse')
            if code:
                return code
    return None


def _solve_caesar(page, raw_text: str, text_lower: str, boundary_y: int, step: int) -> str | None:
    """Handle Caesar cipher challenges."""
    caesar_match = re.search(r'shift(?:ed)?\s*(?:by|of)?\s*(\d+)', text_lower)
    shift = int(caesar_match.group(1)) if caesar_match else 3
    quoted = re.findall(r'"([^"]+)"|\'([^\']+)\'', raw_text)
    for groups in quoted:
        token = groups[0] or groups[1]
        if len(token) >= 2:
            for s in [shift, -shift, 26 - shift]:
                shifted = _caesar_shift(token, s)
                log(f"step {step}: [decode] caesar shift {s}: '{token}' -> '{shifted}'")
                code = _try_submit(page, shifted, boundary_y, step, f'caesar:{s}')
                if code:
                    return code
    return None


def _solve_hex(page, raw_text: str, boundary_y: int, step: int) -> str | None:
    """Handle hex encoded challenges."""
    hex_candidates = re.findall(r'\b([0-9A-Fa-f]{8,})\b', raw_text)
    for candidate in hex_candidates:
        if len(candidate) % 2 != 0:
            continue
        try:
            decoded = bytes.fromhex(candidate).decode('utf-8', errors='ignore')
            if not decoded or len(decoded) < 2:
                continue
            log(f"step {step}: [decode] hex '{candidate}' -> '{decoded}'")
            code = _try_submit(page, decoded, boundary_y, step, 'hex')
            if code:
                return code
        except Exception:
            pass
    return None


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999

    # Read ORIGINAL-CASE text from page (ctx.instruction is lowercased)
    try:
        raw_text = page.evaluate('() => (document.body?.innerText || "").substring(0, 2000)')
    except Exception:
        raw_text = ctx.instruction
    text_lower = ctx.instruction

    # Detect encoding type from instruction — avoids corrupting state with wrong decoder
    encoding = _detect_encoding(text_lower)
    log(f"step {ctx.step}: [decode] detected encoding: {encoding}")

    if encoding == 'base64':
        return _solve_base64(page, raw_text, boundary_y, ctx.step)
    elif encoding == 'rot13':
        return _solve_rot13(page, boundary_y, ctx.step)
    elif encoding == 'reverse':
        return _solve_reverse(page, raw_text, boundary_y, ctx.step)
    elif encoding == 'caesar':
        return _solve_caesar(page, raw_text, text_lower, boundary_y, ctx.step)
    elif encoding == 'hex':
        return _solve_hex(page, raw_text, boundary_y, ctx.step)

    # Unknown encoding — try all decoders in order, but skip ROT13 first
    # (ROT13 resolver's regex often matches "Decode this" → garbage)

    # Try quoted strings first
    quoted = re.findall(r'"([^"]+)"|\'([^\']+)\'', raw_text)
    for groups in quoted:
        token = groups[0] or groups[1]
        if len(token) < 2 or len(token) > 100:
            continue
        log(f"step {ctx.step}: [decode] trying quoted string: {token}")
        code = _try_submit(page, token, boundary_y, ctx.step, f'quoted:{token}')
        if code:
            return code

    # Try base64 first (most common)
    code = _solve_base64(page, raw_text, boundary_y, ctx.step)
    if code:
        return code

    # Then ROT13
    code = _solve_rot13(page, boundary_y, ctx.step)
    if code:
        return code

    # Then hex
    code = _solve_hex(page, raw_text, boundary_y, ctx.step)
    if code:
        return code

    # Interactive fallback — try typing the decoded keyword and clicking Complete
    log(f"step {ctx.step}: [decode] trying interactive DECODE keyword")
    code = _try_submit(page, 'DECODE', boundary_y, ctx.step, 'keyword-DECODE')
    if code:
        return code

    # Last resort: click Complete/Reveal in case the answer was already accepted
    click_button_by_text(page, ['complete', 'decode', 'reveal', 'done', 'finish'], boundary_y)
    page.wait_for_timeout(300)
    return extract_code(page) or wait_for_code_mutation(page, 1500)
