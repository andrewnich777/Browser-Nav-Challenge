"""Keyboard sequence challenge: parse key combo from instruction, press it."""

import re
from agents.v4.context import StepCtx
from agents.v4.helpers import (
    wait_for_code_mutation, click_button_by_text, query_inputs_in_scope,
)
from log import log

# Map instruction text to Playwright key names
KEY_MAP = {
    'ctrl': 'Control', 'control': 'Control',
    'shift': 'Shift', 'alt': 'Alt', 'meta': 'Meta',
    'enter': 'Enter', 'return': 'Enter',
    'tab': 'Tab', 'escape': 'Escape', 'esc': 'Escape',
    'space': 'Space', 'backspace': 'Backspace',
    'delete': 'Delete', 'home': 'Home', 'end': 'End',
    'arrowup': 'ArrowUp', 'arrowdown': 'ArrowDown',
    'arrowleft': 'ArrowLeft', 'arrowright': 'ArrowRight',
    'up': 'ArrowUp', 'down': 'ArrowDown',
    'left': 'ArrowLeft', 'right': 'ArrowRight',
}

# Keys safe to press as single keys (won't cause navigation or data loss)
SAFE_SINGLE_KEYS = {
    'enter', 'space', 'tab', 'escape', 'arrowup', 'arrowdown',
    'arrowleft', 'arrowright', 'backspace', 'delete', 'home', 'end',
    'pageup', 'pagedown',
}


def _normalize_combo(combo_str: str) -> str:
    """Normalize a combo like 'ctrl+v' to 'Control+v'."""
    keys = [k.strip().lower() for k in combo_str.split('+')]
    pw_keys = []
    for k in keys:
        mapped = KEY_MAP.get(k, k.upper() if len(k) == 1 else k.capitalize())
        pw_keys.append(mapped)
    return '+'.join(pw_keys)


def _parse_key_token(token: str) -> str | None:
    """Parse a single key token like 'Control+Shift+K' or 'Tab' to Playwright key."""
    token = token.strip().rstrip('.,;:!?')
    if not token:
        return None
    if '+' in token:
        return _normalize_combo(token)
    mapped = KEY_MAP.get(token.lower())
    if mapped:
        return mapped
    if len(token) == 1 and token.isalpha():
        return token.upper()
    if token.lower() in SAFE_SINGLE_KEYS:
        return token.capitalize()
    return None


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999
    instr = ctx.instruction

    # Focus an input if available (some keyboard challenges need focus)
    inputs = query_inputs_in_scope(page, boundary_y)
    if inputs:
        page.mouse.click(inputs[0]['x'], inputs[0]['y'])
        page.wait_for_timeout(100)

    # Parse multi-key sequence: "Required sequence: Control+Shift+K Tab Enter Escape"
    seq_match = re.search(
        r'(?:required\s*sequence|sequence)[:\s]+(.+?)(?:\.|$)',
        instr, re.I,
    )
    if seq_match:
        tokens = seq_match.group(1).strip().split()
        keys = [_parse_key_token(t) for t in tokens]
        keys = [k for k in keys if k]
        if keys:
            log(f"step {ctx.step}: [keyboard] pressing sequence: {keys}")
            for key in keys:
                page.keyboard.press(key)
                # Deselect after Ctrl+A to avoid blue highlight flash
                if 'Control+A' in key or 'Control+a' in key:
                    page.mouse.click(1, 1)
                page.wait_for_timeout(200)
            page.wait_for_timeout(300)
            code = wait_for_code_mutation(page, 1500)
            if code:
                return code

    # Single combo: "press Ctrl+Shift+K"
    combo_patterns = [
        r'((?:ctrl|control|shift|alt|meta)\s*\+\s*\w+(?:\s*\+\s*\w+)*)',
        r'(?:press|type|combination)[:\s]+((?:\w+\+)+\w+)',
    ]
    for pat in combo_patterns:
        m = re.search(pat, instr, re.I)
        if m:
            combo = _normalize_combo(m.group(1))
            log(f"step {ctx.step}: [keyboard] pressing combo: {combo}")
            page.keyboard.press(combo)
            # Deselect after Ctrl+A to avoid blue highlight flash
            if 'Control+A' in combo or 'Control+a' in combo:
                page.mouse.click(1, 1)
            page.wait_for_timeout(300)
            code = wait_for_code_mutation(page, 1500)
            if code:
                return code

    # Parse quoted strings to type: "type 'hello'"
    m = re.search(r'(?:type|enter|input)\s*[:\s]*["\']([^"\']+)["\']', instr, re.I)
    if m:
        value = m.group(1)
        log(f"step {ctx.step}: [keyboard] typing: {value}")
        page.keyboard.type(value)
        page.wait_for_timeout(300)
        code = wait_for_code_mutation(page, 1500)
        if code:
            return code

    # Single key fallback — only from safe allowlist
    for word in re.findall(r'\b\w+\b', instr):
        if word.lower() in SAFE_SINGLE_KEYS:
            mapped = KEY_MAP.get(word.lower(), word.capitalize())
            log(f"step {ctx.step}: [keyboard] pressing safe key: {mapped}")
            page.keyboard.press(mapped)
            page.wait_for_timeout(300)
            code = wait_for_code_mutation(page, 1500)
            if code:
                return code
            break  # Only try first matching safe key

    # Fallback: click keyboard-related buttons
    click_button_by_text(page, ['press', 'type', 'key', 'input'], boundary_y)
    page.wait_for_timeout(300)
    return wait_for_code_mutation(page, 1500)
