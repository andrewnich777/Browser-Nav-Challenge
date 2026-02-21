"""Dynamic value resolvers for recipe replay.

When a recipe contains a 'type' action with a resolver tag instead of a
hardcoded value, the resolver computes the correct value from the live page
(e.g., reading a math expression and evaluating it, or decoding ROT13 text).
"""

import re
from log import log


def eval_expression(page):
    """Read math expression from page, evaluate, return string result."""
    text = page.evaluate('''() => {
        const ch = document.querySelector('.bg-pink-100')
                || document.querySelector('[class*="challenge"]');
        return (ch ? ch.innerText : document.body?.innerText) || "";
    }''')
    # Try multiple expression patterns
    m = re.search(r'(\d+)\s*([+\-*/])\s*(\d+)\s*=\s*\?', text)
    if not m:
        m = re.search(r'[Ww]hat is\s+(\d+)\s*([+\-*/])\s*(\d+)', text)
    if not m:
        # Broader: "Calculate: 12 + 34" or "Solve: 5 * 7"
        m = re.search(r'(?:calculate|solve|compute|answer)[:\s]+(\d+)\s*([+\-*/])\s*(\d+)', text, re.I)
    if not m:
        # Last resort: any "N op N" near an input field
        m = re.search(r'(\d+)\s*([+\-*/])\s*(\d+)', text)
    if not m:
        log(f"[resolver] eval_expression: no math expression found in page text ({len(text)} chars)")
        return None
    a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
    ops = {'+': a + b, '-': a - b, '*': a * b}
    if op == '/':
        if b == 0 or a % b != 0:
            log(f"[resolver] eval_expression: division {a}/{b} not clean")
            return None
        ops['/'] = a // b
    result = ops.get(op)
    if result is not None:
        log(f"[resolver] eval_expression: {a} {op} {b} = {result}")
    return str(result) if result is not None else None


def rot13(page):
    """Read encoded text from page, apply ROT13, return decoded string."""
    text = page.evaluate('() => document.body?.innerText || ""')
    m = re.search(r'[Dd]ecode[:\s]+([A-Za-z]+)', text)
    if not m:
        log(f"[resolver] rot13: no encoded text found")
        return None
    encoded = m.group(1)
    decoded = encoded.translate(str.maketrans(
        'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz',
        'NOPQRSTUVWXYZABCDEFGHIJKLMnopqrstuvwxyzabcdefghijklm'))
    log(f"[resolver] rot13: '{encoded}' -> '{decoded}'")
    return decoded


RESOLVERS = {
    'eval_expression': eval_expression,
    'rot13': rot13,
}


def resolve(name, page):
    """Run a named resolver against the live page. Returns value or None."""
    fn = RESOLVERS.get(name)
    if not fn:
        log(f"[resolver] unknown resolver: '{name}'")
        return None
    try:
        return fn(page)
    except Exception as e:
        log(f"[resolver] {name} error: {e}")
        return None
