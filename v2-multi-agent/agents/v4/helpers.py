"""Shared utilities for V4 challenge agents.

All button/input helpers use boundary_y filtering by default to prevent decoy clicks.
"""

import re
import time
from config import CHARSET
from log import log
from primitives import extract_code_js, read_progress
from code_scorer import harvest_and_score, is_valid_code

# ── Action Tracer Hook ──────────────────────────────────────────────────────
# Set by ActionTracer (compare.py) to capture JS-level interactions that
# bypass page.mouse.*. None when no tracer is active.
_action_tracer = None


def _trace_action(action_dict: dict):
    """Log an action to the active tracer, if any."""
    if _action_tracer is not None:
        _action_tracer.append(action_dict)

# ── Generalizable Capabilities ──────────────────────────────────────────────
# These helpers work on ANY website. They use:
# - Playwright's aria_snapshot() (PW 1.57+)
# - Playwright's role-based locators (get_by_role, get_by_label)
# - Standard DOM APIs (getAnimations, getComputedStyle, innerText)
# - Vision API as universal fallback
# See MISSION.md for the decision framework.


def get_aria_snapshot(page) -> str:
    """Get YAML aria snapshot of the page.

    Returns the raw YAML string from aria_snapshot(), or '' on failure.
    Much smaller than full DOM (~93% fewer tokens).
    """
    try:
        return page.locator('body').aria_snapshot() or ''
    except Exception:
        return ''


def get_accessible_elements(page, boundary_y: int = 99999) -> dict:
    """Snapshot the accessibility tree, filtered by boundary_y.

    Returns {buttons: [...], inputs: [...], headings: [...], links: [...], other: [...]}
    where each item is {name, role, x, y, w, h}.
    Works on any website — uses Playwright's aria_snapshot() (PW 1.57+).
    """
    result = {'buttons': [], 'inputs': [], 'headings': [], 'links': [], 'other': []}
    try:
        yaml_str = page.locator('body').aria_snapshot()
    except Exception:
        return result
    if not yaml_str:
        return result

    # Parse YAML lines: "- role \"name\" [attrs]" or "- role \"name\":"
    for line in yaml_str.split('\n'):
        stripped = line.strip()
        if not stripped.startswith('-'):
            continue
        m = re.match(r'-\s+(\w+)\s+"([^"]*)"\s*(\[.*\])?', stripped)
        if not m:
            # Also match "- role:" with no name
            m2 = re.match(r'-\s+(\w+)\s*:', stripped)
            if m2:
                role = m2.group(1).lower()
                name = ''
                attrs = ''
            else:
                continue
        else:
            role = m.group(1).lower()
            name = m.group(2).strip()
            attrs = m.group(3) or ''
        if 'disabled' in attrs:
            continue
        # Get bounding box via role-based locator
        bbox = None
        try:
            if role in ('button', 'link', 'textbox', 'heading', 'spinbutton',
                        'checkbox', 'radio', 'slider', 'tab', 'combobox',
                        'menuitem'):
                if name:
                    loc = page.get_by_role(role, name=name, exact=False).first
                else:
                    loc = page.get_by_role(role).first
                bbox = loc.bounding_box(timeout=200)
        except Exception:
            pass
        if not bbox:
            continue
        cy = bbox['y'] + bbox['height'] / 2
        if cy >= boundary_y:
            continue
        entry = {
            'name': name,
            'role': role,
            'x': round(bbox['x'] + bbox['width'] / 2),
            'y': round(cy),
            'w': round(bbox['width']),
            'h': round(bbox['height']),
        }
        bucket = {'button': 'buttons', 'link': 'links', 'heading': 'headings',
                  'textbox': 'inputs', 'spinbutton': 'inputs', 'combobox': 'inputs'}
        result[bucket.get(role, 'other')].append(entry)
    return result


def wait_for_animations_done(page, selector: str = 'body',
                              timeout_ms: int = 3000) -> bool:
    """Wait for all CSS animations on element subtree to complete.

    Uses el.getAnimations({subtree: true}) + Promise.all(a.finished).
    More reliable than transitionend events — catches already-started animations.
    Returns True if animations completed, False on timeout.
    """
    try:
        return page.evaluate('''({selector, timeout}) => new Promise(resolve => {
            const el = document.querySelector(selector) || document.body;
            const anims = el.getAnimations({subtree: true});
            if (!anims.length) { resolve(true); return; }
            const timer = setTimeout(() => resolve(false), timeout);
            Promise.all(anims.map(a => a.finished)).then(() => {
                clearTimeout(timer);
                resolve(true);
            }).catch(() => {
                clearTimeout(timer);
                resolve(false);
            });
        })''', {'selector': selector, 'timeout': timeout_ms})
    except Exception:
        return False


def find_hover_targets_by_hovering(page, boundary_y: int = 99999) -> list[dict]:
    """Find hover-responsive elements by actually hovering and observing.

    For each interactive element: hover with locator.hover(), wait 300ms,
    check if any visible change occurred (new text, element appearance, progress).
    Returns list of {name, role, x, y, response_type} for elements that reacted.
    """
    targets = []
    a11y = get_accessible_elements(page, boundary_y)
    # Check buttons and other interactive elements
    candidates = a11y.get('buttons', []) + a11y.get('other', [])

    # Also find elements with hover-related text via DOM
    try:
        hover_els = page.evaluate(f'''() => {{
            const results = [];
            for (const el of document.querySelectorAll('div, span, section, p, label, [role="button"]')) {{
                const r = el.getBoundingClientRect();
                if (r.width < 20 || r.height < 10 || r.width > 600 || r.height > 600) continue;
                if (r.top + r.height/2 > {boundary_y} || r.top <= 0) continue;
                const t = (el.innerText || '').trim().toLowerCase();
                const style = getComputedStyle(el);
                if (t.includes('hover') || t.includes('mouse over')
                    || style.cursor === 'pointer') {{
                    results.push({{
                        name: (el.innerText || '').trim().substring(0, 60),
                        role: 'generic',
                        x: Math.round(r.x + r.width/2),
                        y: Math.round(r.y + r.height/2),
                        w: Math.round(r.width),
                        h: Math.round(r.height),
                    }});
                }}
            }}
            return results;
        }}''') or []
        candidates.extend(hover_els)
    except Exception:
        pass

    # CSSOM :hover rule scanning — find elements targeted by CSS hover rules
    try:
        cssom_targets = page.evaluate(f'''() => {{
            const results = [];
            const vizProps = ['display','visibility','opacity','transform','height','max-height'];
            for (const sheet of document.styleSheets) {{
                try {{
                    for (const rule of (sheet.cssRules || [])) {{
                        if (!rule.selectorText || !rule.selectorText.includes(':hover')) continue;
                        let hasViz = false;
                        for (const p of vizProps) if (rule.style.getPropertyValue(p)) hasViz = true;
                        if (!hasViz) continue;
                        const base = rule.selectorText.replace(/:hover.*/g, '').trim();
                        if (!base) continue;
                        const els = document.querySelectorAll(base);
                        for (const el of els) {{
                            const r = el.getBoundingClientRect();
                            if (r.width < 20 || r.height < 10) continue;
                            if (r.top + r.height/2 > {boundary_y}) continue;
                            results.push({{
                                name: (el.innerText||'').trim().substring(0,60),
                                role: 'cssom_hover',
                                x: Math.round(r.x+r.width/2),
                                y: Math.round(r.y+r.height/2),
                                w: Math.round(r.width),
                                h: Math.round(r.height),
                            }});
                        }}
                    }}
                }} catch(e) {{}}  // cross-origin sheets
            }}
            return results;
        }}''') or []
        candidates.extend(cssom_targets)
    except Exception:
        pass

    # Deduplicate by position
    seen = set()
    unique = []
    for c in candidates:
        key = (c.get('x', 0) // 10, c.get('y', 0) // 10)
        if key not in seen:
            seen.add(key)
            unique.append(c)

    # Prioritize hover-text and cssom_hover candidates over generic cursor:pointer
    def _sort_key(c):
        name = (c.get('name', '') or '').lower()
        role = c.get('role', '')
        if 'hover' in name or 'mouse' in name:
            return 0  # hover-text elements first
        if role == 'cssom_hover':
            return 1  # CSSOM :hover targets second
        return 2  # generic elements last
    unique.sort(key=_sort_key)

    import time as _time
    t0 = _time.monotonic()
    for el in unique[:8]:  # Cap at 8 to stay under 10s
        if _time.monotonic() - t0 > 10:
            break  # Hard 10s timeout
        x, y = el.get('x', 0), el.get('y', 0)
        if x == 0 and y == 0:
            continue
        # Snapshot progress + text length before hover
        try:
            p_before = get_progress_fraction(page)
            text_len_before = page.evaluate(
                '() => (document.body?.innerText || "").length')
        except Exception:
            continue

        # Actually hover
        try:
            page.mouse.move(x, y, steps=3)
            page.wait_for_timeout(300)
        except Exception:
            continue

        # Check for progress change (fast) then text length change
        try:
            p_after = get_progress_fraction(page)
            response_type = None
            if p_after > p_before:
                response_type = 'progress'
            else:
                text_len_after = page.evaluate(
                    '() => (document.body?.innerText || "").length')
                if text_len_after != text_len_before:
                    response_type = 'text_change'

            if response_type:
                targets.append({
                    'name': el.get('name', ''),
                    'role': el.get('role', ''),
                    'x': x, 'y': y,
                    'response_type': response_type,
                })
                # Stay hovered — don't pull away from a responsive element.
                # The caller (hover agent) will sustain hover from here.
                continue
        except Exception:
            continue

        # Only move away for NON-responsive elements (reset hover state)
        try:
            page.mouse.move(0, 0)
            page.wait_for_timeout(200)
        except Exception:
            pass

    return targets


def screenshot_extract_code(page, boundary_y: int = 99999) -> str | None:
    """Screenshot the challenge area and use Vision API to extract any visible code.

    Universal fallback for codes that are visually present but hard to read
    programmatically (pseudo-elements, canvas, images, styled text).
    Returns a valid 6-char code or None.
    """
    try:
        from vision_client import VisionClient

        # Screenshot just the challenge area (above boundary_y)
        clip = {'x': 0, 'y': 0,
                'width': page.viewport_size['width'],
                'height': min(boundary_y, page.viewport_size['height'])}
        screenshot_bytes = page.screenshot(type="png", clip=clip)

        client = VisionClient()
        context = (
            "Look at this screenshot of a browser challenge. "
            "Find any 6-character alphanumeric code visible in the image. "
            "The code uses characters A-H, J-N, P-Z, 2-9 (no I, O, 0, 1). "
            "Return ONLY the 6-character code, nothing else. "
            "If no code is visible, return 'NONE'."
        )
        response = client.analyze_screenshot(screenshot_bytes, 0, context)
        if response and response.code_value:
            code = response.code_value.strip().upper()
            if len(code) == 6 and is_valid_code(code):
                return code
    except Exception:
        pass
    return None


# ── Progress Gating ─────────────────────────────────────────────────────────


def get_progress_fraction(page) -> float:
    """Read current progress as a 0-1 fraction. Returns 0.0 on failure."""
    try:
        p = read_progress(page)
        return p.get('fraction', 0) if p else 0.0
    except Exception:
        return 0.0


def read_task_status(page) -> dict:
    """Read sequence-style task completion status (which sub-tasks have ✓).

    Returns dict like {'click': True, 'hover': False, 'type': True, 'scroll': True}.
    """
    try:
        return page.evaluate(r'''() => {
            const tasks = {};
            const els = document.querySelectorAll('span, div, button, label, li, p');
            for (const el of els) {
                const t = (el.innerText || '').trim().toLowerCase();
                if (t.length > 200) continue;
                if (t.includes('click button') || t.includes('click me'))
                    tasks.click = t.includes('✓') || t.includes('✔') || t.includes('done');
                if (t.includes('hover area') || t.includes('hover over'))
                    tasks.hover = t.includes('✓') || t.includes('✔') || t.includes('done');
                if (t.includes('type text') || t.includes('type here'))
                    tasks.type = t.includes('✓') || t.includes('✔') || t.includes('done');
                if (t.includes('scroll box') || t.includes('scroll inside'))
                    tasks.scroll = t.includes('✓') || t.includes('✔') || t.includes('done');
            }
            return tasks;
        }''') or {}
    except Exception:
        return {}


def find_hover_target_scored(page, boundary_y: int) -> dict | None:
    """Find the best hover target using interactivity scoring (from archived agent).

    Scores candidates by: cursor:pointer (+5), background color (+2),
    CSS transition (+3), reasonable size (+2). Returns {x, y, w, h, score} or None.
    """
    try:
        return page.evaluate(f'''() => {{
            const candidates = [];
            const maxW = Math.min(600, window.innerWidth * 0.9);
            const maxH = Math.min(600, window.innerHeight * 0.9);
            const allEls = document.querySelectorAll('div, span, p, section, label, [role="button"]');
            for (const el of allEls) {{
                const t = (el.innerText || '').trim().toLowerCase();
                if (!(t.includes('hover over') || t.includes('hover here')
                    || t.includes('hover area') || t.includes('hover me')
                    || t.includes('hover target'))) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 20 || rect.height < 10) continue;
                if (rect.width > maxW || rect.height > maxH) continue;
                if (rect.top + rect.height/2 > {boundary_y}) continue;
                if (rect.top <= 0) continue;
                candidates.push({{
                    x: Math.round(rect.x + rect.width / 2),
                    y: Math.round(rect.y + rect.height / 2),
                    w: Math.round(rect.width), h: Math.round(rect.height),
                    tag: el.tagName, text: t.substring(0, 40),
                }});
            }}
            if (!candidates.length) return null;
            // Score by interactivity signals
            for (const c of candidates) {{
                const el = document.elementFromPoint(c.x, c.y);
                const style = el ? getComputedStyle(el) : null;
                let score = 0;
                if (c.w >= 40 && c.h >= 20) score += 2;
                if (style && style.cursor === 'pointer') score += 5;
                if (style && style.pointerEvents !== 'none') score += 1;
                if (style && parseFloat(style.opacity) > 0) score += 1;
                const bg = style ? style.backgroundColor : '';
                if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') score += 2;
                const tr = style ? style.transition : '';
                if (tr && tr !== 'none' && tr !== 'all 0s ease 0s') score += 3;
                c.score = score;
            }}
            candidates.sort((a, b) => b.score - a.score);
            return candidates[0];
        }}''')
    except Exception:
        return None


def do_hover_with_js_events(page, x: int, y: int, hold_ms: int = 2500):
    """Hover at coordinates using Playwright mouse.move (generates real browser events).

    Playwright's mouse.move dispatches native mousemove/mouseenter/mouseover
    events through the browser's event system — no synthetic dispatch needed.
    """
    page.mouse.move(x, y, steps=5)
    page.wait_for_timeout(hold_ms)


def type_react_native(page, selector_or_coords, value: str):
    """Type into an input using React's nativeInputValueSetter for guaranteed state update.

    selector_or_coords: CSS selector string, or None to auto-find the input.
    Falls back to Playwright keyboard.type if native setter unavailable.
    """
    try:
        page.evaluate('''({sel, val}) => {
            let el;
            if (sel) {
                el = document.querySelector(sel);
            }
            if (!el) {
                // Find focused element or first visible non-code input
                el = document.activeElement;
                if (!el || el === document.body) {
                    const inputs = document.querySelectorAll('input:not([type="hidden"]), textarea');
                    for (const inp of inputs) {
                        const ph = (inp.placeholder || '').toLowerCase();
                        if (ph.includes('enter code') || ph.includes('character code')) continue;
                        if (inp.maxLength === 6) continue;
                        el = inp;
                        break;
                    }
                }
            }
            if (!el) return false;
            el.focus();
            const nativeSet = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value')?.set
                || Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value')?.set;
            if (nativeSet) {
                nativeSet.call(el, val);
            } else {
                el.value = val;
            }
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        }''', {'sel': selector_or_coords, 'val': value})
    except Exception:
        # Fallback to keyboard — only if an input/textarea is focused
        try:
            has_input_focus = page.evaluate('''() => {
                const el = document.activeElement;
                return el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA'
                    || el.isContentEditable);
            }''')
            if has_input_focus:
                page.keyboard.press('Control+a')
                page.keyboard.type(str(value))
        except Exception:
            pass

# ── Challenge Scope ──────────────────────────────────────────────────────────


def compute_challenge_scope(page) -> tuple[str | None, int]:
    """Compute challenge boundary: CSS selector and bottom Y coordinate.

    3-level fallback: code form → submit button → filler marker → (None, 99999).
    Returns (scope_selector, boundary_y).
    """
    try:
        result = page.evaluate(r'''() => {
            // Level 1: code input form (always below challenge)
            const codeInput = document.querySelector(
                'input[placeholder*="code" i], input[placeholder*="enter" i], '
                + 'input.code, [class*="code-input"], [data-testid*="code"]'
            );
            if (codeInput) {
                const r = codeInput.getBoundingClientRect();
                if (r.top > 50) return { selector: null, y: Math.round(r.top) };
            }

            // Level 2: submit button with "submit" or "enter code" text
            const buttons = document.querySelectorAll('button');
            for (const b of buttons) {
                const t = (b.innerText || '').trim().toLowerCase();
                if (t.includes('submit code') || t.includes('enter code')
                    || t === 'submit') {
                    const r = b.getBoundingClientRect();
                    if (r.top > 50) return { selector: null, y: Math.round(r.top) };
                }
            }

            // Level 3: filler / section markers
            const fillers = document.querySelectorAll(
                '[class*="filler"], [class*="section"]');
            let minY = 99999;
            for (const f of fillers) {
                const r = f.getBoundingClientRect();
                if (r.top > 100 && r.top < minY) minY = Math.round(r.top);
            }
            if (minY < 99999) return { selector: null, y: minY };

            return { selector: null, y: 99999 };
        }''')
        return result.get('selector'), result.get('y', 99999)
    except Exception:
        return None, 99999


def query_buttons_in_scope(page, boundary_y: int,
                           exclude: set[str] | None = None) -> list[dict]:
    """Get visible buttons with center Y < boundary_y.

    Returns list of {text, x, y, w, h, tag} dicts sorted by DOM order.
    Excludes buttons matching any text in `exclude` set (case-insensitive).
    """
    excl = {e.lower() for e in (exclude or set())}
    try:
        buttons = page.evaluate(r'''() => {
            return [...document.querySelectorAll(
                'button, [role="button"]'
            )].map(b => {
                const r = b.getBoundingClientRect();
                const style = getComputedStyle(b);
                return {
                    text: (b.innerText || '').trim().substring(0, 80),
                    x: Math.round(r.x + r.width / 2),
                    y: Math.round(r.y + r.height / 2),
                    w: Math.round(r.width),
                    h: Math.round(r.height),
                    tag: b.tagName,
                    disabled: b.disabled,
                    hidden: style.display === 'none' || style.visibility === 'hidden'
                        || parseFloat(style.opacity) < 0.01,
                };
            }).filter(b => b.w > 0 && b.h > 0 && !b.disabled && !b.hidden);
        }''') or []
        return [
            b for b in buttons
            if b['y'] < boundary_y
            and b['text'].lower() not in excl
        ]
    except Exception:
        return []


def query_inputs_in_scope(page, boundary_y: int) -> list[dict]:
    """Get visible inputs within challenge area (center Y < boundary_y)."""
    try:
        return page.evaluate(f'''() => {{
            return [...document.querySelectorAll(
                'input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"]), '
                + 'textarea, [contenteditable="true"]'
            )].map(el => {{
                const r = el.getBoundingClientRect();
                return {{
                    x: Math.round(r.x + r.width / 2),
                    y: Math.round(r.y + r.height / 2),
                    w: Math.round(r.width),
                    h: Math.round(r.height),
                    type: el.type || el.tagName.toLowerCase(),
                    placeholder: el.placeholder || '',
                    value: el.value || '',
                }};
            }}).filter(i => i.w > 0 && i.h > 0 && i.y < {boundary_y});
        }}''') or []
    except Exception:
        return []


# ── Button Interaction ───────────────────────────────────────────────────────

SUBMIT_EXCLUDE = {'submit code', 'enter code', 'submit', 'next step', 'continue'}


def click_button_by_text(page, keywords: list[str], boundary_y: int,
                         exclude: set[str] | None = None) -> bool:
    """Click the first visible button whose text matches any keyword.

    Returns True if a button was clicked.
    """
    excl = SUBMIT_EXCLUDE | (exclude or set())
    buttons = query_buttons_in_scope(page, boundary_y, exclude=excl)
    for kw in keywords:
        kw_lower = kw.lower()
        for btn in buttons:
            if kw_lower in btn['text'].lower():
                page.mouse.click(btn['x'], btn['y'])
                return True
    return False


def click_all_matching_buttons(page, keywords: list[str], boundary_y: int,
                               settle_ms: int = 400, max_clicks: int = 10,
                               exclude: set[str] | None = None) -> int:
    """Click all buttons matching keywords, re-querying DOM after each click.

    Returns number of clicks performed.
    """
    excl = SUBMIT_EXCLUDE | (exclude or set())
    clicked = set()
    total = 0
    for _ in range(max_clicks):
        buttons = query_buttons_in_scope(page, boundary_y, exclude=excl)
        found = False
        for kw in keywords:
            kw_lower = kw.lower()
            for btn in buttons:
                key = (btn['text'], btn['x'], btn['y'])
                if key in clicked:
                    continue
                if kw_lower in btn['text'].lower():
                    page.mouse.click(btn['x'], btn['y'])
                    clicked.add(key)
                    total += 1
                    found = True
                    page.wait_for_timeout(settle_ms)
                    break
            if found:
                break
        if not found:
            break
    return total


# ── Code Extraction ──────────────────────────────────────────────────────────


def extract_code(page) -> str | None:
    """Extract a valid 6-char code from the page (JS DOM scan)."""
    return extract_code_js(page)


def wait_for_code_mutation(page, timeout_ms: int = 3000) -> str | None:
    """Wait for a code to appear via RAF-polled wait_for_function.

    Uses page.wait_for_function(polling="raf") for ~16ms response time
    (every requestAnimationFrame), falling back to interval polling.
    """
    # Strategy 1: RAF-polled wait (~16ms per frame at 60fps)
    try:
        result = page.wait_for_function(
            r'''() => {
                // Check hooks first
                if (window.__getAllCodes) {
                    const codes = window.__getAllCodes();
                    const all = (codes.bus || []).concat(codes.mut || []);
                    for (const item of all) {
                        const c = item.c || '';
                        if (c.length === 6 && /^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}$/.test(c))
                            return c;
                    }
                }
                // Check DOM for visible code
                const RE = /\b[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}\b/g;
                const text = document.body?.innerText || '';
                const matches = text.match(RE) || [];
                const DECOYS = window.__DECOY_CODES || new Set();
                for (const m of matches) {
                    if (!DECOYS.has(m)) return m;
                }
                return null;
            }''',
            polling='raf',
            timeout=timeout_ms,
        )
        if result:
            code = result.json_value()
            if code and is_valid_code(code):
                return code
    except Exception:
        pass

    # Strategy 2: Interval polling fallback
    elapsed = 0
    poll = 250
    while elapsed < min(timeout_ms, 1500):
        try:
            codes = page.evaluate(
                "() => window.__getAllCodes ? window.__getAllCodes() : {bus:[], mut:[]}"
            ) or {}
            for item in codes.get('bus', []) + codes.get('mut', []):
                c = item.get('c', '')
                if len(c) == 6 and is_valid_code(c):
                    return c
        except Exception:
            pass
        code = extract_code_js(page)
        if code:
            return code
        page.wait_for_timeout(poll)
        elapsed += poll
    return None


def harvest_fallback(page, instruction: str = '') -> str | None:
    """harvest_and_score wrapper, threshold >= 0.3."""
    try:
        last_action = page.evaluate('() => window.__lastActionTime || 0')
        score, code = harvest_and_score(page, instruction, last_action)
        if code and score >= 0.3:
            return code
    except Exception:
        pass
    return None


def get_all_hook_codes(page) -> list[str]:
    """Get all codes captured by init_hooks (WebSocket, mutation, etc)."""
    try:
        result = page.evaluate(
            "() => window.__getAllCodes ? window.__getAllCodes() : {bus:[], mut:[]}"
        ) or {}
        codes = []
        seen = set()
        for item in result.get('bus', []) + result.get('mut', []):
            c = item.get('c', '')
            if len(c) == 6 and is_valid_code(c) and c not in seen:
                seen.add(c)
                codes.append(c)
        return codes
    except Exception:
        return []


# ── Input Interaction ────────────────────────────────────────────────────────


def type_into_challenge_input(page, value: str, boundary_y: int) -> bool:
    """Type a value into the first challenge input.

    Tries locator.fill() first (most reliable for React), falls back to keyboard.
    Returns True if typing succeeded.
    """
    # Strategy 1: Role-based locator + fill
    for role in ['textbox', 'spinbutton']:
        try:
            loc = page.get_by_role(role).first
            bbox = loc.bounding_box(timeout=300)
            if bbox and (bbox['y'] + bbox['height'] / 2) < boundary_y:
                loc.fill(str(value), timeout=3000)
                _trace_action({
                    'type': 'fill', 'value': str(value)[:50],
                    'element': f'{role} input',
                    'x': round(bbox['x'] + bbox['width'] / 2),
                    'y': round(bbox['y'] + bbox['height'] / 2),
                    't': round(time.time() * 1000),
                })
                return True
        except Exception:
            continue

    # Strategy 2: Scoped inputs via DOM query + fill
    inputs = query_inputs_in_scope(page, boundary_y)
    if inputs:
        inp = inputs[0]
        try:
            # Build a precise locator from the input type/placeholder
            ph = inp.get('placeholder', '')
            if ph:
                loc = page.get_by_placeholder(ph).first
            else:
                loc = page.locator(
                    f'input[type="{inp.get("type", "text")}"]'
                ).first
            loc.fill(str(value), timeout=3000)
            _trace_action({
                'type': 'fill', 'value': str(value)[:50],
                'element': f"input ph='{ph[:20]}'",
                'x': inp.get('x', 0), 'y': inp.get('y', 0),
                't': round(time.time() * 1000),
            })
            return True
        except Exception:
            pass

    # Strategy 3: Click + keyboard (fallback)
    if inputs:
        inp = inputs[0]
        page.mouse.click(inp['x'], inp['y'])
        page.wait_for_timeout(100)
        # Verify an input is actually focused before Ctrl+A (avoids selecting all page text)
        try:
            has_focus = page.evaluate('''() => {
                const el = document.activeElement;
                return el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA'
                    || el.isContentEditable);
            }''')
        except Exception:
            has_focus = False
        if has_focus:
            page.keyboard.press('Control+a')
        page.keyboard.type(str(value))
        return True
    return False


# ── Fiber Bypass ─────────────────────────────────────────────────────────────


def fiber_bypass(page, selector: str = 'button') -> str | None:
    """Invoke React onClick handler directly via fiber internals.

    Walks React fiber tree to find onClick props on matching elements
    and invokes them. Returns code if one appears after invocation.
    """
    try:
        result = page.evaluate(f'''() => {{
            const els = document.querySelectorAll('{selector}');
            let clicked = 0;
            for (const el of els) {{
                const fk = Object.keys(el).find(k => k.startsWith('__reactFiber'));
                if (!fk) continue;
                let fiber = el[fk];
                while (fiber) {{
                    const props = fiber.memoizedProps || fiber.pendingProps;
                    if (props && typeof props.onClick === 'function') {{
                        try {{ props.onClick({{ preventDefault(){{}}, stopPropagation(){{}}, target: el }}); clicked++; }} catch(e) {{}}
                        break;
                    }}
                    fiber = fiber.return;
                }}
            }}
            return clicked;
        }}''')
        if result and result > 0:
            page.wait_for_timeout(500)
            return extract_code_js(page) or wait_for_code_mutation(page, 2000)
    except Exception as e:
        log(f"fiber_bypass error: {e}")
    return None


# ── JS Click (React-Compatible) ─────────────────────────────────────────────


def js_click_button_by_text(page, keywords: list[str], boundary_y: int,
                            exclude: set[str] | None = None) -> str | None:
    """Click button via JS el.click() for reliable React handler firing.

    Unlike click_button_by_text which uses page.mouse.click (can miss due to
    overlays or coordinate issues), this uses direct DOM el.click() which goes
    straight to the element and fires React synthetic events.
    Returns clicked button text or None.
    """
    excl_list = list({e.lower() for e in (SUBMIT_EXCLUDE | (exclude or set()))})
    try:
        result = page.evaluate('''({keywords, boundaryY, excl}) => {
            const btns = document.querySelectorAll('button, [role="button"]');
            const exclSet = new Set(excl);
            for (const kw of keywords) {
                const kwl = kw.toLowerCase();
                for (const btn of btns) {
                    const t = (btn.innerText || '').trim();
                    const tl = t.toLowerCase();
                    if (exclSet.has(tl) || btn.disabled) continue;
                    const r = btn.getBoundingClientRect();
                    if (r.width < 1 || r.height < 1) continue;
                    if (r.top + r.height/2 > boundaryY) continue;
                    if (tl.includes(kwl)) {
                        btn.click();
                        return {text: t, x: Math.round(r.x + r.width/2),
                                y: Math.round(r.y + r.height/2)};
                    }
                }
            }
            return null;
        }''', {'keywords': keywords, 'boundaryY': boundary_y, 'excl': excl_list})
        if result:
            _trace_action({
                'type': 'js_click', 'element': f"button: {result['text'][:40]}",
                'x': result['x'], 'y': result['y'],
                't': round(time.time() * 1000),
            })
            return result['text']
        return None
    except Exception:
        return None


# ── Shadow Root Traversal ────────────────────────────────────────────────────


def js_click_in_shadow_roots(page, keywords: list[str],
                             boundary_y: int) -> str | None:
    """Click button by text, searching inside shadow roots recursively.

    Returns clicked button text or None.
    """
    excl_list = list({e.lower() for e in SUBMIT_EXCLUDE})
    try:
        result = page.evaluate('''({keywords, boundaryY, excl}) => {
            const exclSet = new Set(excl);
            function searchAndClick(root, kwl) {
                const btns = root.querySelectorAll('button, [role="button"]');
                for (const btn of btns) {
                    const t = (btn.textContent || '').trim();
                    const tl = t.toLowerCase();
                    if (exclSet.has(tl) || btn.disabled) continue;
                    const r = btn.getBoundingClientRect();
                    if (r.width < 1 || r.height < 1) continue;
                    if (r.top + r.height/2 > boundaryY) continue;
                    if (tl.includes(kwl)) {
                        btn.click();
                        return {text: t, x: Math.round(r.x + r.width/2),
                                y: Math.round(r.y + r.height/2)};
                    }
                }
                for (const el of root.querySelectorAll('*')) {
                    if (el.shadowRoot) {
                        const result = searchAndClick(el.shadowRoot, kwl);
                        if (result) return result;
                    }
                }
                return null;
            }
            for (const kw of keywords) {
                const result = searchAndClick(document, kw.toLowerCase());
                if (result) return result;
            }
            return null;
        }''', {'keywords': keywords, 'boundaryY': boundary_y, 'excl': excl_list})
        if result:
            _trace_action({
                'type': 'js_click_shadow', 'element': f"button: {result['text'][:40]}",
                'x': result['x'], 'y': result['y'],
                't': round(time.time() * 1000),
            })
            return result['text']
        return None
    except Exception:
        return None


# ── Frame Traversal ──────────────────────────────────────────────────────────


def find_in_nested_frames(page, role: str, name, max_depth: int = 6):
    """Find an element in nested iframes using frame_locator() chains.

    frame_locator supports role-based locators and auto-waits.
    Returns a Playwright Locator or None.
    """
    if isinstance(name, str):
        name = re.compile(re.escape(name), re.I)

    # Try direct get_by_role first (main frame)
    try:
        loc = page.get_by_role(role, name=name).first
        if loc.is_visible(timeout=300):
            return loc
    except Exception:
        pass

    # Try frame_locator chains at increasing depth
    current = page
    for depth in range(max_depth):
        try:
            frame = current.frame_locator("iframe").first
            btn = frame.get_by_role(role, name=name).first
            try:
                if btn.is_visible(timeout=300):
                    return btn
            except Exception:
                pass
            current = frame
        except Exception:
            break
    return None


def click_button_in_frames(page, keywords: list[str],
                           boundary_y: int = 99999) -> bool:
    """Click button by text, searching all page frames (nested iframes).

    Tries frame_locator chains first (handles deep nesting), then
    falls back to page.frames iteration.
    Returns True if a button was clicked.
    """
    # Tier 1: frame_locator chains
    for kw in keywords:
        loc = find_in_nested_frames(page, "button", kw)
        if loc:
            try:
                loc.click(timeout=1000)
                return True
            except Exception:
                continue

    # Tier 2: JS click on main page (React compat)
    if js_click_button_by_text(page, keywords, boundary_y):
        return True

    # Tier 3: Search all child frames
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            for kw in keywords:
                loc = frame.locator(
                    f'button:has-text("{kw}"), [role="button"]:has-text("{kw}")'
                ).first
                if loc.is_visible(timeout=300):
                    loc.click(timeout=500)
                    return True
        except Exception:
            continue
    return False



# ── Semantic Structure (Accessibility-Tree-Like) ─────────────────────────────


def get_semantic_structure(page, boundary_y: int = 99999) -> dict:
    """Extract a lightweight semantic structure from the page.

    Returns dict with roles mapping to lists of labels, similar to an
    accessibility tree but built from DOM. Used for challenge type detection
    without noise from filler text.

    Example: {'button': ['Shadow Level 1', 'Reveal Code'], 'textbox': ['Enter answer']}
    """
    try:
        return page.evaluate(f'''() => {{
            const roles = {{}};
            const selectors = 'button, input, select, textarea, [role], canvas, '
                + 'audio, video, iframe, [draggable="true"], [contenteditable="true"]';
            for (const el of document.querySelectorAll(selectors)) {{
                const r = el.getBoundingClientRect();
                if (r.width < 5 || r.height < 5) continue;
                if (r.top + r.height/2 > {boundary_y}) continue;
                const role = el.getAttribute('role')
                    || el.tagName.toLowerCase();
                const label = el.getAttribute('aria-label')
                    || el.getAttribute('placeholder')
                    || (el.innerText || '').trim().substring(0, 60)
                    || '';
                if (!roles[role]) roles[role] = [];
                roles[role].push(label);
            }}
            return roles;
        }}''') or {}
    except Exception:
        return {}


def detect_type_from_semantics(page, boundary_y: int = 99999) -> str | None:
    """Detect challenge type from semantic structure (roles + labels).

    Faster and more reliable than text matching for common challenge types.
    Returns challenge type string or None.
    """
    sem = get_semantic_structure(page, boundary_y)
    if not sem:
        return None

    # video → video challenge (check before canvas since video can render on canvas)
    if 'video' in sem:
        return 'video'

    # canvas → gesture or video (video challenges may use canvas for frame rendering)
    if 'canvas' in sem:
        try:
            full_text = page.inner_text('body', timeout=500)[:3000].lower()
            if re.search(r'\b(seek|video challenge|video frames?|fast.?forward|rewind)\b', full_text):
                return 'video'
        except Exception:
            pass
        return 'gesture'

    # audio → audio challenge
    if 'audio' in sem:
        return 'audio'

    # iframe → recursive_iframe
    if 'iframe' in sem:
        return 'recursive_iframe'

    # Buttons provide strong signal
    buttons = [b.lower() for b in sem.get('button', [])]

    # Shadow Level buttons → shadow_dom
    if any('shadow level' in b for b in buttons):
        return 'shadow_dom'

    # Tab buttons → multi_tab
    if any(re.match(r'tab\s*\d', b) for b in buttons):
        return 'multi_tab'

    # Connect/WebSocket buttons → websocket
    if any('connect' in b or 'websocket' in b for b in buttons):
        return 'websocket'

    # Register/Service Worker → service_worker
    if any('register' in b or 'service' in b for b in buttons):
        return 'service_worker'

    # Math puzzle with textbox + solve/calculate button → puzzle_solve (safe default, no page.goto)
    inputs = sem.get('input', []) + sem.get('textarea', []) + sem.get('textbox', [])
    if inputs and any('calculate' in b or 'solve' in b or 'check' in b for b in buttons):
        return 'puzzle_solve'

    # Capture button → timing
    if any('capture' in b for b in buttons):
        return 'timing'

    # Complete + multiple sub-task indicators → sequence
    if any('complete' in b for b in buttons) and len(buttons) >= 3:
        return 'sequence'

    return None


# ── Animation Completion ─────────────────────────────────────────────────────


def wait_for_animation_end(page, timeout_ms: int = 3000) -> bool:
    """Wait for CSS transition/animation to complete.

    Uses transitionend/animationend events instead of fixed polling.
    Returns True if animation ended, False on timeout.
    """
    try:
        return page.evaluate('''(timeout) => new Promise(resolve => {
            let resolved = false;
            const handler = () => {
                if (!resolved) { resolved = true; resolve(true); }
            };
            document.addEventListener('transitionend', handler, {once: true});
            document.addEventListener('animationend', handler, {once: true});
            setTimeout(() => {
                if (!resolved) { resolved = true; resolve(false); }
            }, timeout);
        })''', timeout_ms)
    except Exception:
        return False


# ── Compound Primitives ──────────────────────────────────────────────────────


def click_and_verify_progress(page, keywords: list[str], boundary_y: int,
                              settle_ms: int = 500) -> tuple[bool, float]:
    """Click a button and verify progress changed.

    Returns (clicked, progress_after). If Playwright click happened but
    progress didn't change, retries with JS click.
    """
    p_before = get_progress_fraction(page)

    # Tier 1: Playwright click
    clicked = click_button_by_text(page, keywords, boundary_y)
    if clicked:
        page.wait_for_timeout(settle_ms)
        p_after = get_progress_fraction(page)
        if p_after > p_before:
            return (True, p_after)

        # Tier 2: JS click (React compat)
        js_click_button_by_text(page, keywords, boundary_y)
        page.wait_for_timeout(settle_ms)
        p_after = get_progress_fraction(page)
        return (True, p_after)

    return (False, p_before)


def complete_challenge_sweep(page, boundary_y: int) -> str | None:
    """Click all completion buttons and check for code.

    Handles: Complete (4/4), Reveal Code, Show Code, Done, etc.
    Returns code if found, None otherwise.
    """
    completion_keywords = [
        'complete (', 'complete', 'reveal code', 'reveal',
        'show code', 'done', 'finish', 'all tabs visited',
    ]

    for kw in completion_keywords:
        clicked = js_click_button_by_text(page, [kw], boundary_y)
        if not clicked:
            clicked = click_button_by_text(page, [kw], boundary_y)
        if clicked:
            page.wait_for_timeout(500)
            code = extract_code(page) or wait_for_code_mutation(page, 1000)
            if code:
                return code

    return None
