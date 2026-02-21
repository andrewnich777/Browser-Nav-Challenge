"""Shared primitives — extract_code, read_progress, locator cascade,
enumerate_code_candidates, enumerate_interactives."""

import re
import time
from config import CHARSET
from log import log_stage


def extract_code_js(page) -> str | None:
    """Extract a valid 6-char code from the page via JS (charset + __isValidCode)."""
    try:
        return page.evaluate(f'''() => {{
            const pattern = new RegExp('[{CHARSET}]{{6}}', 'g');
            const text = document.body.innerText;
            const matches = text.match(pattern) || [];
            for (const m of matches) {{
                if (m !== m.toUpperCase()) continue;
                if (!window.__isValidCode || window.__isValidCode(m)) return m;
            }}
            return null;
        }}''')
    except Exception:
        return None


def read_progress(page) -> dict | None:
    """Parse N/M progress indicators from the page.

    Returns {'fraction': 0.6, 'current': 3, 'total': 5, 'text': '3/5 mutations'}
    or None if ambiguous or not found.

    Filters out "Section N" filler content. If multiple candidates with different
    denominators exist, returns None (ambiguous). Prefers element nearest top-left
    with smallest bounding box (HUD-like).
    """
    try:
        result = page.evaluate(r'''() => {
            const vh = window.innerHeight;
            const vw = window.innerWidth;
            const candidates = [];

            // Context keywords shared across patterns
            const CTX = 'mutations?|task|step|complete|done|click|interaction|found|filled|required|strokes?|seek|operations?|slots?|pieces?|parts?|zones?|remaining|attempts?|more|capture|visit|visited|tab|tabs|actions?|challenges?|triggered|depth|levels?';
            const CTX_BEFORE = 'mutations?|task|step|click|progress|interaction|strokes?|seek|operations?|slots?|pieces?|parts?|filled|found|drop\\s*zones?|capture|visit|visited|tab|tabs|actions?|challenges?|complete|triggered|depth|levels?';

            const patterns = [
                new RegExp('(\\d+)\\s*\\/\\s*(\\d+)\\s*(?:' + CTX + ')', 'i'),
                new RegExp('(?:' + CTX_BEFORE + ')[:\\s(]*' + '(\\d+)\\s*\\/\\s*(\\d+)', 'i'),
                new RegExp('(\\d+)\\s+of\\s+(\\d+)\\s*(?:' + CTX + ')', 'i'),
            ];

            function _addCandidate(m, el) {
                if (!el) return;
                const rect = el.getBoundingClientRect();
                candidates.push({
                    current: parseInt(m[1]),
                    total: parseInt(m[2]),
                    text: m[0],
                    dist: Math.sqrt(
                        Math.max(0, rect.left) ** 2 +
                        Math.max(0, rect.top) ** 2
                    ),
                    area: rect.width * rect.height,
                });
            }

            // Pass 1: Walk individual text nodes (most precise)
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            while (walker.nextNode()) {
                const node = walker.currentNode;
                const text = node.textContent.trim();
                if (!text) continue;
                if (/^Section \d+$/.test(text)) continue;

                for (const pat of patterns) {
                    const m = text.match(pat);
                    if (m) {
                        _addCandidate(m, node.parentElement);
                        break;
                    }
                }
            }

            // Pass 2: If no candidates from text nodes, check parent element textContent.
            // Handles split text nodes (e.g., <p>Progress: <span>3/4</span></p>).
            if (candidates.length === 0) {
                const containers = document.querySelectorAll('p, div, span, label, h1, h2, h3, h4, h5, h6, li, td, th, button');
                for (const el of containers) {
                    const text = (el.textContent || '').trim();
                    if (!text || text.length > 200) continue;
                    if (/^Section \d+$/.test(text)) continue;
                    // Skip if this element contains too many children (likely a large container)
                    if (el.children.length > 10) continue;
                    for (const pat of patterns) {
                        const m = text.match(pat);
                        if (m) {
                            _addCandidate(m, el);
                            break;
                        }
                    }
                }
            }

            // Pass 3: <progress> elements and role="progressbar"
            if (candidates.length === 0) {
                const progressEls = document.querySelectorAll('progress, [role="progressbar"]');
                for (const el of progressEls) {
                    const val = el.value ?? parseFloat(el.getAttribute('aria-valuenow'));
                    const max = el.max ?? parseFloat(el.getAttribute('aria-valuemax')) ?? 100;
                    if (!isNaN(val) && !isNaN(max) && max > 0) {
                        const rect = el.getBoundingClientRect();
                        candidates.push({current: Math.round(val), total: Math.round(max),
                            text: Math.round(val) + '/' + Math.round(max),
                            dist: Math.sqrt(Math.max(0,rect.left)**2 + Math.max(0,rect.top)**2),
                            area: rect.width * rect.height});
                    }
                }
            }

            // Pass 4: Percentage text ("50%", "66% complete")
            if (candidates.length === 0) {
                const pctRe = /(\d+)\s*%\s*(?:complete|done|progress|filled)?/i;
                const pctMatch = (document.body?.innerText || '').match(pctRe);
                if (pctMatch) {
                    const pct = parseInt(pctMatch[1]);
                    if (pct > 0 && pct <= 100)
                        candidates.push({current: pct, total: 100, text: pctMatch[0], dist: 0, area: 0});
                }
            }

            // Pass 5: Stepper dots / aria-current
            if (candidates.length === 0) {
                const active = document.querySelectorAll(
                    '[aria-current="step"], .step.active, .dot.active, .step.completed');
                const allSteps = document.querySelectorAll('.step, .dot, [role="tab"]');
                if (active.length > 0 && allSteps.length > active.length) {
                    candidates.push({current: active.length, total: allSteps.length,
                        text: active.length + '/' + allSteps.length + ' steps', dist: 0, area: 0});
                }
            }

            if (candidates.length === 0) return null;

            // Check for ambiguity: different denominators
            const denoms = new Set(candidates.map(c => c.total));
            if (denoms.size > 1) return null;

            // Pick best: smallest dist + area
            candidates.sort((a, b) => (a.dist + a.area * 0.01) - (b.dist + b.area * 0.01));
            const best = candidates[0];
            return {
                current: best.current,
                total: best.total,
                text: best.text,
                fraction: best.total > 0 ? best.current / best.total : 0,
            };
        }''')
        return result
    except Exception:
        return None


def get_locator_cascade(page, x: float, y: float) -> dict | None:
    """Get locator info for both the leaf element and its interactive ancestor at (x, y).

    Returns {leaf: {...}, ancestor: {...} | null, isAnchor: bool, coords: [x, y]}
    """
    try:
        return page.evaluate(f'''() => {{
            const el = document.elementFromPoint({x}, {y});
            if (!el) return null;
            const ancestor = el.closest(
                '[data-testid],[data-test],[data-cy],button,[role="button"],'
                + 'a[href],input,select,textarea,[tabindex]'
            );
            const info = (node) => ({{
                data_testid: node.dataset?.testid || node.dataset?.test || node.dataset?.cy || null,
                aria_label: node.getAttribute('aria-label'),
                role: node.getAttribute('role'),
                name: node.getAttribute('name'),
                text: (node.textContent || '').trim().substring(0, 40),
                id: node.id || null,
                tag: node.tagName,
                classes: (node.className?.toString() || '').substring(0, 60),
            }});
            return {{
                leaf: info(el),
                ancestor: ancestor ? info(ancestor) : null,
                isAnchor: !!el.closest('a[href]'),
                coords: [{x}, {y}]
            }};
        }}''')
    except Exception:
        return None


# ── Context-aware candidate enumeration ──────────────────────────────────

_ENUMERATE_CANDIDATES_JS = r'''() => {
    const RE = /\b[A-HJ-NP-Z2-9]{6}\b/g;
    const candidates = [];
    const seen = new Set();

    function buildSelector(el) {
        if (!el) return null;
        if (el.id) return '#' + el.id;
        if (el.dataset && el.dataset.testid)
            return '[data-testid="' + el.dataset.testid + '"]';
        const role = el.getAttribute('role');
        if (role) return '[role="' + role + '"]';
        return el.tagName.toLowerCase();
    }

    const INSTR_RE = /instructions?|step\s+\d+\s+of|scroll\s+down|challenge|submit\s+(?:the|your)\b|code\s+is\b|enter\s+(?:the\s+)?code|you\s+(?:need|must|should)|your\s+task/i;
    const POS_RE = /\bcode[:\s]|\byour code\b|\bresult[:\s]|\boutput[:\s]|\brevealed?\b|\bunlock/i;

    // 1. Observer / code-bus codes
    if (window.__getAllCodes) {
        try {
            const all = window.__getAllCodes();
            for (const item of [...(all.bus || []), ...(all.mut || [])]) {
                const c = item.c;
                if (!c || seen.has(c)) continue;
                if (window.__isValidCode && !window.__isValidCode(c)) continue;
                seen.add(c);
                candidates.push({
                    code: c,
                    source: item.src ? 'codebus' : 'mutation',
                    selector: null, bbox: null, text_context: '',
                    is_in_instruction_zone: false,
                    appeared_after_baseline:
                        !(window.__baselineCodes && window.__baselineCodes.has(c)),
                    has_positive_label: false, is_small_badge: false,
                    has_testid_or_role: false,
                    container_tag: null, parent_text_len: 0,
                });
            }
        } catch (e) {}
    }

    // 2. DOM text-node scan
    if (!document.body) return candidates;
    const walker = document.createTreeWalker(
        document.body, NodeFilter.SHOW_TEXT
    );
    while (walker.nextNode()) {
        const node = walker.currentNode;
        const text = node.nodeValue || '';
        if (text.length < 6) continue;

        RE.lastIndex = 0;
        let match;
        while ((match = RE.exec(text)) !== null) {
            const c = match[0];
            if (seen.has(c)) continue;
            if (window.__isValidCode && !window.__isValidCode(c)) continue;
            seen.add(c);

            const el = node.parentElement;
            if (!el) continue;

            // Precise bbox via Range
            let bbox = null;
            try {
                const range = document.createRange();
                range.setStart(node, match.index);
                range.setEnd(node, match.index + 6);
                const r = range.getBoundingClientRect();
                if (r.width > 0)
                    bbox = [Math.round(r.x), Math.round(r.y),
                            Math.round(r.width), Math.round(r.height)];
            } catch (e) {}

            // Off-screen filter
            const elRect = el.getBoundingClientRect();
            if (!bbox && (elRect.bottom < -200 ||
                          elRect.top > window.innerHeight + 200)) continue;
            if (bbox && (bbox[1] < -200 ||
                         bbox[1] > window.innerHeight + 200)) continue;

            // Text context (~40 chars around match)
            const ctxS = Math.max(0, match.index - 40);
            const ctxE = Math.min(text.length, match.index + 46);
            const textCtx = text.substring(ctxS, ctxE).trim();

            // Instruction zone: check parent chain (up to 3 levels)
            let inInstr = false;
            let n = el;
            for (let i = 0; i < 3 && n; i++) {
                if (INSTR_RE.test((n.textContent || '').substring(0, 500))) {
                    inInstr = true; break;
                }
                n = n.parentElement;
            }

            const parentText = (el.textContent || '').trim();
            const hasPos = parentText.length < 120 && POS_RE.test(parentText);
            const isSmall = elRect.width > 0 && elRect.width < 250 &&
                            elRect.height < 80 && parentText.length < 40;
            const hasTR = !!(
                el.dataset?.testid ||
                el.getAttribute('role') === 'button' ||
                el.closest('[data-testid]') ||
                el.closest('[role="button"]') ||
                el.closest('[aria-label]')
            );
            const afterBL = !(
                window.__baselineCodes && window.__baselineCodes.has(c)
            );

            candidates.push({
                code: c, source: 'dom_text',
                selector: buildSelector(el), bbox: bbox,
                text_context: textCtx,
                is_in_instruction_zone: inInstr,
                appeared_after_baseline: afterBL,
                has_positive_label: hasPos,
                is_small_badge: isSmall,
                has_testid_or_role: hasTR,
                container_tag: el.tagName,
                parent_text_len: parentText.length,
            });
        }
    }
    return candidates;
}'''


def _score_enumerated_candidate(c: dict) -> float:
    """Score a raw candidate from the JS enumerate scan. Returns 0.0-1.0."""
    score = 0.5

    # Source bonus: observer/mutation codes bypassed DOM → higher trust
    src = c.get('source', '')
    if src in ('codebus', 'mutation'):
        score += 0.15

    # Appeared after baseline snapshot (strong positive)
    if c.get('appeared_after_baseline'):
        score += 0.25
    else:
        score -= 0.25

    # Instruction zone penalty (key differentiator for Step 7 fix)
    if c.get('is_in_instruction_zone'):
        score -= 0.3

    # Positive label bonus ("Code:", "Result:", "revealed")
    if c.get('has_positive_label'):
        score += 0.15

    # Small badge / chip element
    if c.get('is_small_badge'):
        score += 0.1

    # Has data-testid or interactive role
    if c.get('has_testid_or_role'):
        score += 0.1

    # Large parent text → likely instruction paragraph
    ptl = c.get('parent_text_len', 0)
    if ptl > 500:
        score -= 0.25
    elif ptl > 200:
        score -= 0.15

    # H/P/LI inside instruction zone = double penalty
    tag = (c.get('container_tag') or '').upper()
    if tag in ('P', 'LI', 'H1', 'H2', 'H3') and c.get('is_in_instruction_zone'):
        score -= 0.1

    return max(0.0, min(1.0, score))


def enumerate_code_candidates(page, *, include_frames=True,
                              max_candidates=25) -> list[dict]:
    """Return ranked code candidates with provenance, context, and
    instruction-zone detection.

    Each candidate dict has: code, source, score, selector, bbox,
    text_context, is_in_instruction_zone, appeared_after_baseline, frame_id.
    """
    all_candidates = []

    # Main frame scan
    try:
        raw = page.evaluate(_ENUMERATE_CANDIDATES_JS)
        for c in (raw or []):
            c['frame_id'] = 'main'
            c['score'] = _score_enumerated_candidate(c)
        all_candidates.extend(raw or [])
    except Exception:
        pass

    # Iframe scan (bounded: at most 5 frames, 1 pass each)
    if include_frames:
        try:
            frames = page.frames[1:]  # skip main
            for idx, frame in enumerate(frames[:5]):
                try:
                    frame_raw = frame.evaluate(_ENUMERATE_CANDIDATES_JS)
                    for c in (frame_raw or []):
                        c['frame_id'] = f'index:{idx}'
                        c['score'] = _score_enumerated_candidate(c)
                    all_candidates.extend(frame_raw or [])
                except Exception:
                    pass
        except Exception:
            pass

    # Dedup by code string, keep highest score
    best = {}
    for c in all_candidates:
        code = c['code']
        if code not in best or c['score'] > best[code]['score']:
            best[code] = c

    result = sorted(best.values(), key=lambda x: x['score'], reverse=True)
    return result[:max_candidates]


# ── Interactive Element Enumeration (backbone for all exploration) ────────

_ENUMERATE_INTERACTIVES_JS = r'''() => {
    const results = [];
    const sels = [
        'button', '[role="button"]', 'input:not([type="hidden"])',
        'select', 'textarea', '[tabindex]:not(a)', '[data-testid]',
        '[draggable="true"]', 'canvas', 'audio', 'video',
        '[role="slider"]', '[role="checkbox"]', '[role="radio"]',
        '[role="switch"]', '[role="tab"]', '[role="menuitem"]',
    ].join(', ');
    const seen = new Set();

    for (const el of document.querySelectorAll(sels)) {
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) continue;
        if (rect.bottom < -100 || rect.top > window.innerHeight + 100) continue;

        const tag = el.tagName;
        const role = el.getAttribute('role') || '';
        const ariaLabel = el.getAttribute('aria-label') || '';
        const text = (el.textContent || '').trim().substring(0, 50);
        const testid = el.dataset?.testid || el.dataset?.test || '';
        const elId = el.id || '';
        const type = el.type || '';

        const isAnchor = !!el.closest('a[href]');
        const isDisabled = el.disabled ||
                           el.getAttribute('aria-disabled') === 'true';
        const cs = getComputedStyle(el);
        const isHidden = cs.display === 'none' || cs.visibility === 'hidden';

        const noveltyKey = tag + ':' + (testid || elId || role || '') +
                           ':' + text.substring(0, 20).replace(/\s+/g, '_');
        if (seen.has(noveltyKey)) continue;
        seen.add(noveltyKey);

        results.push({
            tag, role, type, text, ariaLabel, testid, elId,
            x: Math.round(rect.x + rect.width / 2),
            y: Math.round(rect.y + rect.height / 2),
            w: Math.round(rect.width),
            h: Math.round(rect.height),
            top: Math.round(rect.top),
            danger_anchor: isAnchor,
            danger_disabled: isDisabled,
            danger_hidden: isHidden,
            novelty_key: noveltyKey,
        });
    }
    return results;
}'''


def enumerate_interactives(page) -> list[dict]:
    """Return all interactive elements with metadata, danger flags, and novelty keys.

    Each element dict has: tag, role, type, text, ariaLabel, testid, elId,
    x, y, w, h, top, danger_anchor, danger_disabled, danger_hidden, novelty_key.
    """
    try:
        return page.evaluate(_ENUMERATE_INTERACTIVES_JS) or []
    except Exception:
        return []




# ── Split Parts Solver ────────────────────────────────────────────────

_SPLIT_PARTS_RE = re.compile(
    r'Part\s*(\d+)\s*:\s*([A-Z0-9]{2})', re.IGNORECASE
)

def split_parts_solver(page, body_text: str,
                       skip_codes: set | None = None,
                       ) -> tuple[str | None, list[dict]]:
    """Find and click scattered parts by searching DOM for part values.

    Extracts part values from the header (e.g., "Part 2:XY"), searches the
    full-page DOM for elements containing those 2-char strings, scrolls to
    each, and clicks. Returns (code, action_log) on success.
    """
    from code_scorer import is_valid_code

    skip = skip_codes or set()
    action_log = []

    def _is_valid_new_code(c):
        return (c and len(c) == 6 and is_valid_code(c)
                and c.upper() not in skip)

    # Extract part values via JS (targeted DOM query, avoids noisy body_text)
    parts = page.evaluate(r'''() => {
        const results = [];
        const re = /Part\s*(\d+)\s*:\s*([A-Z0-9]{2})/gi;
        // Look in the challenge header area (first 2000 chars of visible text)
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        const seen = new Set();
        while (walker.nextNode()) {
            const text = walker.currentNode.textContent;
            re.lastIndex = 0;
            let m;
            while ((m = re.exec(text)) !== null) {
                const partNum = m[1];
                const partVal = m[2].toUpperCase();
                // Skip already-found parts (marked with checkmark)
                const ctx = text.substring(m.index, m.index + m[0].length + 5);
                if (ctx.includes('\u2713')) continue;  // ✓
                const key = partNum + ':' + partVal;
                if (!seen.has(key)) {
                    seen.add(key);
                    results.push({num: parseInt(partNum), val: partVal});
                }
            }
        }
        // Sort by part number and deduplicate
        results.sort((a, b) => a.num - b.num);
        return results;
    }''')
    if not parts:
        log_stage("split_parts", "no part values found in DOM")
        return None, action_log

    part_values = [p['val'] for p in parts]
    log_stage("split_parts", f"searching for parts: {part_values}")

    # Search DOM for elements containing each part value and click them
    found_count = 0
    for val in part_values:
        try:
            result = page.evaluate(r'''(partVal) => {
                // Search all elements for the 2-char part value
                const allEls = document.querySelectorAll('*');
                const matches = [];
                for (const el of allEls) {
                    const text = el.textContent?.trim() || '';
                    if (text !== partVal) continue;
                    // Skip already-found parts (✓ marker)
                    const parentText = el.parentElement?.textContent || '';
                    if (parentText.includes('✓') && parentText.includes(partVal)) continue;
                    // Skip the header display (Part N:XX in the instruction area)
                    if (/Part\s*\d+\s*:/i.test(parentText)) continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) continue;
                    const cs = getComputedStyle(el);
                    const clickable = cs.cursor === 'pointer' || el.onclick ||
                        el.closest('button, [role="button"], [tabindex]');
                    matches.push({
                        x: rect.x + rect.width / 2,
                        y: rect.y + rect.height / 2,
                        absY: rect.y + window.scrollY,
                        w: rect.width,
                        h: rect.height,
                        text: text,
                        clickable: !!clickable,
                    });
                }
                if (matches.length === 0) return null;
                // Prefer clickable elements, then smallest
                matches.sort((a, b) => {
                    if (a.clickable !== b.clickable) return b.clickable - a.clickable;
                    return (a.w * a.h) - (b.w * b.h);
                });
                return matches[0];
            }''', val)

            if not result:
                # Part might be below fold — scroll down and try again
                for scroll_y in [800, 1600, 2400, 3200, 4000, 5000]:
                    page.evaluate(f'window.scrollTo(0, {scroll_y})')
                    page.wait_for_timeout(200)
                    result = page.evaluate(r'''(partVal) => {
                        const allEls = document.querySelectorAll('*');
                        const matches = [];
                        for (const el of allEls) {
                            const text = el.textContent?.trim() || '';
                            if (text !== partVal) continue;
                            const parentText = el.parentElement?.textContent || '';
                            if (/Part\s*\d+\s*:/i.test(parentText)) continue;
                            const rect = el.getBoundingClientRect();
                            if (rect.width <= 0 || rect.height <= 0) continue;
                            if (rect.top < -50 || rect.top > window.innerHeight + 50) continue;
                            const cs = getComputedStyle(el);
                            const clickable = cs.cursor === 'pointer' || el.onclick ||
                                el.closest('button, [role="button"], [tabindex]');
                            matches.push({
                                x: rect.x + rect.width / 2,
                                y: rect.y + rect.height / 2,
                                w: rect.width, h: rect.height,
                                text: text,
                                clickable: !!clickable,
                            });
                        }
                        if (matches.length === 0) return null;
                        matches.sort((a, b) => {
                            if (a.clickable !== b.clickable) return b.clickable - a.clickable;
                            return (a.w * a.h) - (b.w * b.h);
                        });
                        return matches[0];
                    }''', val)
                    if result:
                        break

            if result:
                x, y = result['x'], result['y']
                log_stage("split_parts",
                          f"click part '{val}' at ({x:.0f},{y:.0f})")
                action_log.append({
                    'action_type': 'click',
                    'target_coords': [x, y],
                    'target_text': val,
                })
                page.mouse.click(x, y)
                page.wait_for_timeout(400)
                found_count += 1
            else:
                log_stage("split_parts", f"part '{val}' not found in DOM")

        except Exception as e:
            log_stage("split_parts", f"error finding part '{val}': {e}")

    if found_count == 0:
        log_stage("split_parts", "no parts found")
        return None, action_log

    log_stage("split_parts", f"clicked {found_count}/{len(part_values)} parts")

    # Scroll back to top to find the code
    page.evaluate('window.scrollTo(0, 0)')
    page.wait_for_timeout(500)

    # Check for code
    code = extract_code_js(page)
    if code and _is_valid_new_code(code):
        log_stage("split_parts", f"found code: {code}")
        if action_log:
            action_log[-1]['expect_code_visible'] = True
        return code, action_log

    # Try mutation codes
    try:
        all_codes = page.evaluate(
            "() => window.__getAllCodes ? window.__getAllCodes() : {bus:[], mut:[]}"
        )
        for item in (all_codes.get('bus', []) + all_codes.get('mut', [])):
            c = item.get('c', '')
            if _is_valid_new_code(c):
                log_stage("split_parts", f"found code from mutation: {c}")
                if action_log:
                    action_log[-1]['expect_code_visible'] = True
                return c, action_log
    except Exception:
        pass

    return None, action_log




# ── Smart Drag (generalized drag primitive) ──────────────────────────

def smart_drag(page, x1: float, y1: float, x2: float, y2: float) -> dict:
    """Mouse-based drag with outcome verification.

    Returns success=True only if the source element's bounding box actually moved
    or disappeared (proving the drop registered). Never reports false success.
    """
    # Snapshot source element bbox BEFORE drag
    before = page.evaluate("""([x,y]) => {
        const el = document.elementFromPoint(x,y);
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return {x:r.x, y:r.y, w:r.width, h:r.height, tag:el.tagName};
    }""", [x1, y1])

    # Execute mouse drag with nudge to reliably initiate drag system
    try:
        page.mouse.move(x1, y1)
        page.wait_for_timeout(80)
        page.mouse.down()
        page.wait_for_timeout(120)
        page.mouse.move(x1 + 3, y1 + 3, steps=3)  # Small nudge to trigger drag
        page.wait_for_timeout(50)
        page.mouse.move(x2, y2, steps=18)  # Smooth interpolation to target
        page.wait_for_timeout(80)
        page.mouse.up()
        page.wait_for_timeout(150)
    except Exception as e:
        log_stage("smart_drag", f"mouse drag error: {e}")
        return {'method': 'mouse', 'success': False, 'error': str(e)}

    # Snapshot source element bbox AFTER drag — check if it actually moved
    after = page.evaluate("""([x,y]) => {
        const el = document.elementFromPoint(x,y);
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return {x:r.x, y:r.y, w:r.width, h:r.height, tag:el.tagName};
    }""", [x1, y1])

    # Outcome check: did the element at the source position change?
    moved = 0
    if before and after:
        moved = abs(before['x'] - after['x']) + abs(before['y'] - after['y'])
    elif before and not after:
        moved = 999  # Element disappeared — strong success signal

    ok = moved > 3
    log_stage("smart_drag",
              f"mouse drag ({x1:.0f},{y1:.0f})->({x2:.0f},{y2:.0f}) "
              f"moved={moved:.0f} {'OK' if ok else 'NO_CHANGE'}")
    return {'method': 'mouse', 'success': ok, 'moved': moved}


# ── Canvas Drawing ────────────────────────────────────────────────────

def draw_stroke_on_canvas(page, canvas_bid_or_selector: str | int,
                          path: list[list[float]],
                          steps_per_segment: int = 3) -> dict:
    """Draw a stroke on a canvas element using normalized coordinates.

    Args:
        canvas_bid_or_selector: BID (int) or CSS selector (str) for the canvas
        path: List of [x, y] points where coords are normalized 0.0-1.0
              relative to canvas bounding box. E.g. [[0.2, 0.3], [0.8, 0.7]]
        steps_per_segment: Playwright mouse.move interpolation steps per segment

    Returns dict with success, progress_before, progress_after, strokes info.
    """
    if not path or len(path) < 2:
        return {'success': False, 'error': 'path must have >= 2 points'}

    # Resolve canvas bounding box
    try:
        if isinstance(canvas_bid_or_selector, int):
            selector = f'[data-bid="{canvas_bid_or_selector}"]'
        else:
            selector = canvas_bid_or_selector
        el = page.query_selector(selector)
        if not el:
            return {'success': False, 'error': f'canvas not found: {selector}'}
        box = el.bounding_box()
        if not box or box['width'] < 4 or box['height'] < 4:
            return {'success': False, 'error': 'canvas has no size'}
    except Exception as e:
        return {'success': False, 'error': f'canvas resolution: {e}'}

    # Convert normalized coords to absolute viewport coords
    left, top = box['x'], box['y']
    w, h = box['width'], box['height']
    # Clamp normalized coords to [0.01, 0.99] to avoid edge issues
    def to_abs(nx, ny):
        cx = max(0.01, min(float(nx), 0.99))
        cy = max(0.01, min(float(ny), 0.99))
        return left + cx * w, top + cy * h

    progress_before = read_progress(page)

    # Canvas pixel hash for stroke verification
    def _canvas_hash():
        try:
            return page.evaluate(f'''(sel) => {{
                try {{
                    const c = document.querySelector(sel);
                    if (!c) return null;
                    const ctx = c.getContext('2d');
                    const d = ctx.getImageData(0, 0, c.width, c.height).data;
                    let h = 0; for (let i = 0; i < d.length; i += 97) h = (h * 31 + d[i]) | 0;
                    return h;
                }} catch(e) {{ return null; }}
            }}''', selector)
        except Exception:
            return None

    MAX_RETRIES = 1  # was 2; reduced to limit retry overhead (~6s per extra retry)
    stroke_verified = False

    for attempt in range(1 + MAX_RETRIES):
        hash_before = _canvas_hash()

        # Execute the stroke
        try:
            jitter = attempt * 3  # ±3px jitter on retry
            ax, ay = to_abs(path[0][0], path[0][1])
            ax += jitter
            page.mouse.move(ax, ay)
            page.wait_for_timeout(30)
            page.mouse.down()
            # rAF sync: ensure mousedown is processed before first mousemove.
            # Chrome coalesces input events per animation frame — without this,
            # mousedown+mousemove can land in the same frame and canvas apps
            # using rAF-based rendering miss the stroke start.
            page.evaluate("() => new Promise(r => requestAnimationFrame(r))")

            # Small wiggle after mousedown to trigger drawing (some canvas apps
            # only start drawing after movement) — only on retry
            if attempt > 0:
                page.mouse.move(ax + 2, ay, steps=1)
                page.mouse.move(ax, ay + 2, steps=1)
                page.mouse.move(ax, ay, steps=1)

            for point in path[1:]:
                bx, by = to_abs(point[0], point[1])
                page.mouse.move(bx, by, steps=steps_per_segment)

            page.mouse.up()
            # Wait for canvas render (rAF + small buffer) before hash check
            try:
                page.evaluate("() => new Promise(r => requestAnimationFrame(r))")
            except Exception:
                pass
            page.wait_for_timeout(30)
        except Exception as e:
            log_stage("draw_canvas", f"stroke error: {e}")
            return {'success': False, 'error': str(e)}

        # Verify stroke registered
        hash_after = _canvas_hash()
        if hash_before is None or hash_after is None:
            # Cross-origin taint or no canvas — fall back to progress check
            progress_check = read_progress(page)
            if progress_check and progress_before:
                if progress_check.get('fraction', 0) > progress_before.get('fraction', 0):
                    stroke_verified = True
                    break
            # Can't verify, assume success
            stroke_verified = True
            break
        elif hash_before != hash_after:
            stroke_verified = True
            break
        else:
            # Pixel hash unchanged — but check progress before retrying.
            # The app may register the stroke without visible canvas pixels
            # (e.g. tracking mousedown/mouseup events internally).
            progress_check = read_progress(page)
            if progress_check and progress_before:
                if progress_check.get('fraction', 0) > progress_before.get('fraction', 0):
                    stroke_verified = True
                    break
            log_stage("draw_canvas",
                      f"stroke attempt {attempt+1} had no pixel change, "
                      f"{'retrying with jitter' if attempt < MAX_RETRIES else 'giving up'}")

    progress_after = read_progress(page)
    progress_changed = (
        progress_before is not None and progress_after is not None and
        progress_after.get('fraction', 0) > progress_before.get('fraction', 0)
    )

    log_stage("draw_canvas",
              f"stroke {len(path)} points on {selector} "
              f"{'progress+' if progress_changed else 'no_progress'} "
              f"verified={stroke_verified}")

    return {
        'success': stroke_verified or progress_changed,
        'points': len(path),
        'progress_before': progress_before,
        'progress_after': progress_after,
        'progress_changed': progress_changed,
        'stroke_verified': stroke_verified,
    }


# ── Repeat Action Until Signal ────────────────────────────────────────

def repeat_action_until_signal(
    page,
    action_fn,
    *,
    progress_reader=None,
    code_checker=None,
    dom_sig_fn=None,
    max_tries: int = 10,
    timeout_s: float = 15.0,
    delay_ms: int = 400,
) -> dict:
    """Repeat an action until a stop signal fires. Returns result dict.

    Parameters:
        action_fn:       callable(page) — executes the action (e.g., click)
        progress_reader: callable(page) → dict|None with 'fraction' key
        code_checker:    callable(page) → str|None — returns code if found
        dom_sig_fn:      callable(page) → str — returns DOM signature for change detection
        max_tries:       max number of action repetitions
        timeout_s:       overall wall-clock timeout
        delay_ms:        pause between repetitions (ms)

    Returns:
        {'stopped_by': str, 'tries': int, 'progress': dict|None,
         'code': str|None, 'dom_changed': bool}
    """
    from code_scorer import harvest_and_score
    start = time.time()
    result = {'stopped_by': 'exhausted', 'tries': 0, 'progress': None,
              'code': None, 'dom_changed': False}

    prev_progress = progress_reader(page) if progress_reader else None
    prev_dom = dom_sig_fn(page) if dom_sig_fn else None

    for i in range(max_tries):
        if time.time() - start > timeout_s:
            result['stopped_by'] = 'timeout'
            break

        try:
            action_fn(page)
        except Exception as e:
            log_stage("repeat_action", f"action error on try {i+1}: {e}")
            break

        result['tries'] = i + 1
        page.wait_for_timeout(delay_ms)

        # Check code appearance
        if code_checker:
            code = code_checker(page)
            if code:
                result['code'] = code
                result['stopped_by'] = 'code_found'
                break
        else:
            score, code = harvest_and_score(page, '', int(time.time() * 1000))
            if code and score >= 0.5:
                result['code'] = code
                result['stopped_by'] = 'code_found'
                break

        # Check progress change
        if progress_reader:
            cur = progress_reader(page)
            if cur and prev_progress:
                if cur.get('fraction', 0) > prev_progress.get('fraction', 0):
                    result['progress'] = cur
                    prev_progress = cur
                    log_stage("repeat_action", f"progress: {cur}")
                    # Check if progress is complete (100%)
                    if cur.get('fraction', 0) >= 1.0:
                        result['stopped_by'] = 'progress_complete'
                        break
            elif cur:
                prev_progress = cur
                # Check completion on first appearance (handles 1-step challenges)
                if cur.get('fraction', 0) >= 1.0:
                    result['stopped_by'] = 'progress_complete'
                    result['progress'] = cur
                    break

        # Check DOM change
        if dom_sig_fn:
            cur_dom = dom_sig_fn(page)
            if cur_dom != prev_dom:
                result['dom_changed'] = True
                prev_dom = cur_dom

    log_stage("repeat_action",
              f"done: {result['tries']} tries, stopped_by={result['stopped_by']}")
    return result


# ── Drag-Drop Auto-Discovery ──────────────────────────────────────────

def discover_drag_drop_puzzle(page) -> dict | None:
    """Discover draggable elements and drop targets, build CSS selector pairs.

    Returns {'pairs': [{'src_sel': str, 'dst_sel': str}], 'count': int} or None.
    """
    result = page.evaluate('''() => {
        function buildSelector(el) {
            if (!el) return null;
            if (el.id) return '#' + CSS.escape(el.id);
            if (el.dataset && el.dataset.testid)
                return '[data-testid="' + el.dataset.testid + '"]';
            const parts = [];
            let cur = el;
            while (cur && cur !== document.body && cur !== document.documentElement) {
                let tag = cur.tagName.toLowerCase();
                if (cur.parentElement) {
                    const siblings = Array.from(cur.parentElement.children)
                        .filter(c => c.tagName === cur.tagName);
                    if (siblings.length > 1) {
                        const idx = siblings.indexOf(cur) + 1;
                        tag += ':nth-of-type(' + idx + ')';
                    }
                }
                parts.unshift(tag);
                cur = cur.parentElement;
            }
            return parts.join(' > ');
        }

        const draggables = Array.from(document.querySelectorAll('[draggable="true"]'));
        if (draggables.length === 0) return null;

        // Find drop targets
        const allElements = document.querySelectorAll('*');
        let dropTargets = [];
        for (const el of allElements) {
            const rect = el.getBoundingClientRect();
            if (rect.width < 20 || rect.height < 20 || rect.width > 150 || rect.height > 150) continue;
            if (rect.top < 0 || rect.top > 800) continue;
            const classStr = (el.className || '').toString().toLowerCase();
            const style = getComputedStyle(el);
            const isDropTarget =
                classStr.includes('slot') || classStr.includes('drop') ||
                classStr.includes('target') || classStr.includes('zone') ||
                el.ondrop !== null || el.ondragover !== null ||
                style.borderStyle === 'dashed' || style.borderStyle === 'dotted' ||
                el.hasAttribute('data-slot') || el.hasAttribute('data-drop') ||
                el.hasAttribute('data-index') || el.hasAttribute('data-position');
            if (isDropTarget && !el.hasAttribute('draggable') &&
                el.querySelector('[draggable="true"]') === null) {
                dropTargets.push(el);
            }
        }

        // Deduplicate by position
        const unique = [];
        for (const el of dropTargets) {
            const r = el.getBoundingClientRect();
            if (!unique.some(u => {
                const ur = u.getBoundingClientRect();
                return Math.abs(ur.x - r.x) < 10 && Math.abs(ur.y - r.y) < 10;
            })) {
                unique.push(el);
            }
        }
        dropTargets = unique;

        // Inferred fallback: if few explicit drops, find squarish bordered/bg elements
        if (dropTargets.length < draggables.length) {
            const draggableParents = new Set(draggables.map(d => d.parentElement));
            for (const el of allElements) {
                const rect = el.getBoundingClientRect();
                if (rect.width < 25 || rect.height < 25 || rect.width > 120 || rect.height > 120) continue;
                if (rect.top < 0 || rect.top > 800) continue;
                if (draggableParents.has(el) || el.hasAttribute('draggable')) continue;
                if (el.querySelector('[draggable="true"]') !== null) continue;
                const st = getComputedStyle(el);
                const hasBorder = st.borderWidth && st.borderWidth !== '0px' && st.borderStyle !== 'none';
                const bg = st.backgroundColor;
                const hasBg = bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent';
                const isSquarish = Math.abs(rect.width - rect.height) < 30;
                const directText = Array.from(el.childNodes)
                    .filter(n => n.nodeType === Node.TEXT_NODE)
                    .map(n => n.textContent?.trim()).join('');
                const isEmpty = directText === '' || /^\\d$/.test(directText) || /^slot/i.test(directText);
                if (isSquarish && (hasBorder || hasBg) && isEmpty) {
                    if (!dropTargets.some(u => {
                        const ur = u.getBoundingClientRect();
                        return Math.abs(ur.x - rect.x) < 10 && Math.abs(ur.y - rect.y) < 10;
                    })) {
                        dropTargets.push(el);
                    }
                }
            }
        }

        // Sort both arrays positionally (left-to-right, top-to-bottom)
        const posSort = (a, b) => {
            const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
            if (Math.abs(ra.y - rb.y) < 15) return ra.x - rb.x;
            return ra.y - rb.y;
        };
        draggables.sort(posSort);
        dropTargets.sort(posSort);

        if (dropTargets.length === 0) return null;

        const count = Math.min(draggables.length, dropTargets.length, 6);
        const pairs = [];
        for (let i = 0; i < count; i++) {
            pairs.push({
                src_sel: buildSelector(draggables[i]),
                dst_sel: buildSelector(dropTargets[i])
            });
        }
        return { pairs, count };
    }''')
    if not result or not result.get('pairs'):
        return None
    return result


def execute_drag_sequence(page, pairs: list[dict]) -> bool:
    """Execute a sequence of drag-and-drop operations using Playwright's CDP-level drag.

    Args:
        page: Playwright page
        pairs: list of {'src_sel': str, 'dst_sel': str}

    Returns True if at least one drag succeeded.
    """
    succeeded = 0
    for pair in pairs:
        src_sel = pair.get('src_sel')
        dst_sel = pair.get('dst_sel')
        if not src_sel or not dst_sel:
            continue

        # Get bounding boxes for both elements (needed for mouse fallback)
        try:
            src_box = page.locator(src_sel).first.bounding_box(timeout=1000)
            dst_box = page.locator(dst_sel).first.bounding_box(timeout=1000)
        except Exception:
            src_box = dst_box = None

        # Strategy 1: locator.drag_to (fast, handles scroll)
        dragged = False
        try:
            src_loc = page.locator(src_sel).first
            dst_loc = page.locator(dst_sel).first
            src_loc.drag_to(dst_loc, timeout=2000, force=True, steps=10)
            dragged = True
        except Exception:
            pass

        # Strategy 2: mouse-based drag (reliable fallback)
        if not dragged and src_box and dst_box:
            sx = src_box['x'] + src_box['width'] / 2
            sy = src_box['y'] + src_box['height'] / 2
            dx = dst_box['x'] + dst_box['width'] / 2
            dy = dst_box['y'] + dst_box['height'] / 2
            try:
                page.mouse.move(sx, sy)
                page.mouse.down()
                page.wait_for_timeout(100)
                page.mouse.move(dx, dy, steps=10)
                page.mouse.move(dx + 1, dy)  # double hover: 2nd mousemove triggers dragover
                page.wait_for_timeout(50)
                page.mouse.up()
                dragged = True
            except Exception as e:
                log_stage("drag_auto", f"mouse drag {src_sel} -> {dst_sel} failed: {e}")

        if dragged:
            succeeded += 1
            page.wait_for_timeout(150)

    log_stage("drag_auto", f"completed {succeeded}/{len(pairs)} drags")
    return succeeded > 0


# ── BID Element Annotation (Set-of-Marks for Vision Grounding) ────────

def annotate_elements(page) -> list[dict]:
    """Assign data-bid to interactive elements, return catalog with classification.

    Elements are classified as interactable or context:
    - Interactable: get data-bid DOM attribute, bid field, overlay label
    - Context: get eid field only (no DOM attribute, no overlay)

    Each entry includes interactable_type (button, drag_source, drop_target,
    canvas, text_input, clickable, context, etc.) and interactable flag.

    Four passes: (1) standard interactive selectors, (2) cursor:pointer,
    (3) drop targets, (4) context text elements.
    """
    try:
        result = page.evaluate(r'''() => {
            // Clear stale data-bid from previous rounds to prevent duplicates
            document.querySelectorAll('[data-bid]').forEach(el => el.removeAttribute('data-bid'));

            const seen = new Set();
            const results = [];
            const vh = window.innerHeight;
            let bid = 0;
            let eid = 0;

            // ── Drop target detection ──────────────────────────────
            function _isDropTarget(el) {
                const cls = (el.className || '').toString().toLowerCase();
                const style = getComputedStyle(el);
                const text = (el.textContent || '').trim().toLowerCase();
                // Stage A: Strong signals (event handlers or explicit data attributes)
                if (el.ondrop !== null || el.ondragover !== null) return true;
                if (el.hasAttribute('data-slot') || el.hasAttribute('data-drop')) return true;
                // Stage B: Medium signals (semantic class names or text)
                const hasSlotClass = cls.includes('slot') || cls.includes('drop') ||
                    cls.includes('target') || cls.includes('zone');
                const hasSlotText = /^(slot|drop|place|target)\s*\d*/i.test(text) && text.length < 30;
                if (hasSlotClass || hasSlotText) return true;
                // Stage C: Weak signal (dashed/dotted border)
                if (style.borderStyle === 'dashed' || style.borderStyle === 'dotted') {
                    if (el.hasAttribute('data-index')) return true;
                    return false;  // Only with data-index
                }
                return false;
            }

            function _dropConfidence(el) {
                if (el.ondrop !== null || el.ondragover !== null) return 'high';
                if (el.hasAttribute('data-slot') || el.hasAttribute('data-drop')) return 'high';
                const cls = (el.className || '').toString().toLowerCase();
                const text = (el.textContent || '').trim().toLowerCase();
                const hasSlotClass = cls.includes('slot') || cls.includes('drop') ||
                    cls.includes('target') || cls.includes('zone');
                const hasSlotText = /^(slot|drop|place|target)\s*\d*/i.test(text) && text.length < 30;
                if (hasSlotClass || hasSlotText) return 'medium';
                return 'low';
            }

            // ── Element classification ─────────────────────────────
            function classify(el) {
                const tag = el.tagName.toLowerCase();
                if (tag === 'button' || el.getAttribute('role') === 'button') return 'button';
                if (tag === 'canvas') return 'canvas';
                if (tag === 'select') return 'select';
                if (tag === 'textarea') return 'text_input';
                if (tag === 'input') {
                    const t = (el.type || 'text').toLowerCase();
                    if (t === 'checkbox') return 'checkbox';
                    if (t === 'radio') return 'radio';
                    if (t === 'submit') return 'submit_button';
                    return 'text_input';
                }
                if (el.getAttribute('role') === 'checkbox') return 'checkbox';
                if (el.getAttribute('role') === 'radio') return 'radio';
                if (el.getAttribute('role') === 'slider') return 'slider';
                if (el.draggable) return 'drag_source';
                if (el.onclick || el.hasAttribute('onclick')) return 'clickable';
                if (_isDropTarget(el)) return 'drop_target';
                const style = getComputedStyle(el);
                if (style.cursor === 'pointer') return 'clickable';
                // Scrollable containers (overflow:scroll/auto with actual scrollable content)
                const ov = style.overflow + ' ' + style.overflowY + ' ' + style.overflowX;
                if ((ov.includes('scroll') || ov.includes('auto')) &&
                    (el.scrollHeight > el.clientHeight + 10 || el.scrollWidth > el.clientWidth + 10))
                    return 'scroll_container';
                if (el.hasAttribute('tabindex')) return 'focusable';
                return 'context';
            }

            // ── Core addEl with interactable/context split ─────────
            function addEl(el) {
                if (seen.has(el)) return;
                seen.add(el);
                const r = el.getBoundingClientRect();
                if (r.width === 0 && r.height === 0) return;
                // Skip very large containers (likely page sections, not interactive)
                // Exception: canvas elements need to be included regardless of size
                if (r.width > 800 && r.height > 400 && !['CANVAS','VIDEO','AUDIO'].includes(el.tagName)) return;
                const itype = classify(el);
                const isInteractable = itype !== 'context';
                const entry = {
                    tag: el.tagName.toLowerCase(),
                    type: el.type || el.getAttribute('role') || '',
                    text: (el.textContent || '').trim().substring(0, 40),
                    role: el.getAttribute('role') || '',
                    ariaLabel: el.getAttribute('aria-label') || '',
                    x: Math.round(r.x + r.width/2),
                    y: Math.round(r.y + r.height/2),
                    w: Math.round(r.width), h: Math.round(r.height),
                    draggable: el.draggable || false,
                    in_viewport: r.bottom > 0 && r.top < vh,
                    interactable_type: itype,
                    interactable: isInteractable,
                };
                if (isInteractable) {
                    el.setAttribute('data-bid', String(bid));
                    entry.bid = bid;
                    bid++;
                } else {
                    entry.eid = eid;
                    eid++;
                }
                if (itype === 'drop_target') {
                    entry.drop_confidence = _dropConfidence(el);
                }
                if (itype === 'canvas') {
                    entry.canvas_bounds = {
                        left: Math.round(r.left), top: Math.round(r.top),
                        right: Math.round(r.right), bottom: Math.round(r.bottom),
                        width: Math.round(r.width), height: Math.round(r.height),
                    };
                }
                // Anchor safety: <a> tags with href must not be clickable (causes 404)
                if (el.tagName === 'A' && el.href) {
                    entry.is_anchor = true;
                    entry.interactable = false;
                    entry.interactable_type = 'context';
                }
                results.push(entry);
            }

            // Pass 1: Standard interactive selectors
            const sels = 'button, input, select, textarea, canvas, ' +
                '[role="button"], [role="checkbox"], [role="slider"], [role="radio"], ' +
                '[tabindex], [draggable="true"], [onclick]';
            for (const el of document.querySelectorAll(sels)) {
                addEl(el);
            }

            // Pass 2: cursor:pointer elements (catches React click handlers)
            // Only check elements in viewport to avoid scanning entire page
            for (const el of document.querySelectorAll('div, span, p, li, label, td, a')) {
                if (seen.has(el)) continue;
                const r = el.getBoundingClientRect();
                if (r.width <= 10 || r.height <= 10) continue;
                if (r.width > 800 && r.height > 400) continue;
                if (r.bottom < -50 || r.top > vh + 50) continue;
                const cs = getComputedStyle(el);
                if (cs.cursor === 'pointer' && cs.display !== 'none' && cs.visibility !== 'hidden') {
                    addEl(el);
                }
            }

            // Pass 3: Drop targets — elements with drop-zone signals
            // not already captured by previous passes
            for (const el of document.querySelectorAll('div, span, li, td')) {
                if (seen.has(el)) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 20 || r.height < 20 || r.width > 300 || r.height > 300) continue;
                if (r.bottom < -50 || r.top > vh + 50) continue;
                if (_isDropTarget(el)) addEl(el);
            }

            // Pass 3.5: Scrollable containers (overflow:scroll/auto with content)
            // e.g., "scroll box" tasks in sequence challenges
            for (const el of document.querySelectorAll('div, section, aside, nav, ul, ol')) {
                if (seen.has(el)) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 30 || r.height < 30) continue;
                if (r.bottom < -50 || r.top > vh + 50) continue;
                const cs = getComputedStyle(el);
                const ov = cs.overflow + ' ' + cs.overflowY + ' ' + cs.overflowX;
                if ((ov.includes('scroll') || ov.includes('auto')) &&
                    (el.scrollHeight > el.clientHeight + 10 || el.scrollWidth > el.clientWidth + 10)) {
                    addEl(el);
                }
            }

            // Pass 4: Context elements — meaningful text in viewport
            // Capped to prevent catalog bloat. No data-bid, no overlay.
            let contextCount = 0;
            const MAX_CONTEXT = 15;
            const challengeContainer = document.querySelector('.max-w-6xl') || document.body;
            for (const el of challengeContainer.querySelectorAll('p, h1, h2, h3, h4, span, label, li')) {
                if (contextCount >= MAX_CONTEXT) break;
                if (seen.has(el)) continue;
                const text = (el.textContent || '').trim();
                if (text.length < 5 || text.length > 150) continue;
                if (el.parentElement && seen.has(el.parentElement)) continue;
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                if (r.bottom < 0 || r.top > vh) continue;
                addEl(el);
                contextCount++;
            }

            return results;
        }''') or []
        return result
    except Exception as e:
        log_stage("annotate", f"error: {e}")
        return []


def render_bid_overlay(page, catalog: list[dict]):
    """Render numbered labels on interactive elements for vision grounding.

    Injects a pointer-events:none overlay div. Call remove_bid_overlay() after screenshot.
    """
    try:
        page.evaluate(r'''(catalog) => {
            let c = document.getElementById('__bid_overlay');
            if (c) c.remove();
            c = document.createElement('div');
            c.id = '__bid_overlay';
            c.style.cssText = 'position:fixed;top:0;left:0;width:100vw;height:100vh;z-index:99999;pointer-events:none;overflow:visible';
            for (const el of catalog) {
                if (!el.in_viewport || !el.interactable) continue;
                const lbl = document.createElement('span');
                lbl.textContent = el.bid;
                lbl.style.cssText = `position:fixed;left:${el.x - el.w/2}px;top:${Math.max(0, el.y - el.h/2 - 14)}px;` +
                    'background:#f00;color:#fff;font:bold 11px monospace;padding:1px 4px;border-radius:3px;z-index:100000;pointer-events:none';
                c.appendChild(lbl);
            }
            document.body.appendChild(c);
        }''', catalog)
    except Exception as e:
        log_stage("annotate", f"render overlay error: {e}")


def remove_bid_overlay(page):
    """Remove BID overlay after screenshot."""
    try:
        page.evaluate("() => document.getElementById('__bid_overlay')?.remove()")
    except Exception:
        pass


# ── Challenge Region Detection ────────────────────────────────────────

def find_challenge_region(page) -> dict | None:
    """Find the container with highest density of instruction text + interactives.

    Returns {x, y, w, h, selector} or None.
    """
    try:
        return page.evaluate(r'''() => {
            // Strategy: find the code input form, walk up to find challenge container
            const codeInput = document.querySelector(
                'input[placeholder*="code" i], input[placeholder*="character" i]'
            );
            if (!codeInput) return null;

            // Walk up from code input to find the challenge wrapper
            let container = codeInput.parentElement;
            for (let i = 0; i < 5 && container && container !== document.body; i++) {
                const text = container.textContent || '';
                if (text.includes('Challenge Step') || text.includes('Complete the challenge')) {
                    break;
                }
                container = container.parentElement;
            }
            if (!container || container === document.body) return null;

            const r = container.getBoundingClientRect();
            return {
                x: Math.round(r.x), y: Math.round(r.y),
                w: Math.round(r.width), h: Math.round(r.height),
                selector: container.tagName +
                    (container.className ? '.' + container.className.split(' ')[0] : ''),
            };
        }''')
    except Exception as e:
        log_stage("region", f"challenge region detection error: {e}")
        return None


# ── Frame + Shadow DOM Surfacing ──────────────────────────────────────

def enumerate_frames(page) -> list[dict]:
    """Discover all iframes and their basic content info.

    Returns list of {index, src, x, y, w, h, visible, innerText}.
    """
    try:
        return page.evaluate(r'''() => {
            const frames = document.querySelectorAll('iframe');
            return Array.from(frames).map((f, i) => {
                const r = f.getBoundingClientRect();
                let innerText = '';
                try { innerText = f.contentDocument?.body?.innerText?.substring(0, 200) || ''; }
                catch(e) { innerText = '[cross-origin]'; }
                return {
                    index: i, src: f.src || '',
                    x: Math.round(r.x), y: Math.round(r.y),
                    w: Math.round(r.width), h: Math.round(r.height),
                    visible: r.width > 0 && r.height > 0,
                    innerText: innerText,
                };
            });
        }''') or []
    except Exception:
        return []


def enumerate_all_frames(page) -> list[dict]:
    """Walk full frame tree including nested iframes.

    Uses Playwright's page.frames (flat list of ALL frames) plus
    frame.parent_frame to build hierarchy. Bounding boxes are already
    in page viewport coords (Playwright handles offset math).

    Returns list of {frame_id, depth, url, visible, bounding_box, parent_id, pw_frame}.
    pw_frame is the Playwright Frame object — NOT serializable, keep out of JSON logs.
    """
    results = []
    try:
        all_frames = page.frames[1:]  # skip main frame
        # Build parent → id mapping for hierarchy
        frame_id_map = {page.main_frame: 'main'}
        for i, frame in enumerate(all_frames):
            frame_id_map[frame] = f'frame_{i}'

        for i, frame in enumerate(all_frames):
            fid = f'frame_{i}'
            try:
                el_handle = frame.frame_element()
                box = el_handle.bounding_box()
            except Exception:
                box = None

            # Compute depth by walking parent chain
            depth = 0
            pf = frame.parent_frame
            while pf and pf != page.main_frame:
                depth += 1
                pf = pf.parent_frame
            depth += 1  # at least 1 (direct child of main)

            parent_id = frame_id_map.get(frame.parent_frame, 'main')

            results.append({
                'frame_id': fid,
                'depth': depth,
                'url': frame.url,
                'visible': box is not None and box['width'] > 0 and box['height'] > 0,
                'bounding_box': box,  # {x, y, width, height} in page coords, or None
                'parent_id': parent_id,
                'pw_frame': frame,
            })
    except Exception:
        pass
    return results


def enumerate_shadow_roots(page) -> list[dict]:
    """Find open shadow DOM roots and their interactive contents.

    Returns list of {host, depth, x, y, interactiveCount, text}.
    """
    try:
        return page.evaluate(r'''() => {
            const results = [];
            function walk(root, depth) {
                if (depth > 5) return;
                const els = root.querySelectorAll('*');
                for (const el of els) {
                    if (el.shadowRoot) {
                        const r = el.getBoundingClientRect();
                        const interactives = el.shadowRoot.querySelectorAll(
                            'button, input, [role="button"], [onclick]');
                        results.push({
                            host: el.tagName, depth: depth,
                            x: Math.round(r.x), y: Math.round(r.y),
                            interactiveCount: interactives.length,
                            text: el.shadowRoot.textContent?.substring(0, 100) || '',
                        });
                        walk(el.shadowRoot, depth + 1);
                    }
                }
            }
            walk(document, 0);
            return results;
        }''') or []
    except Exception:
        return []


# ── Structural Decoy Filtering ────────────────────────────────────────

# Decoy pattern for is_decoy_element() — general label set without "submit".
GLOBAL_DECOY_TEXT_RE = re.compile(
    r'^(Next|Continue|Proceed|Advance|Click Here|Move On|Keep Going|'
    r'Go Forward|Next Step|Next Page|Continue Journey|Continue Reading|'
    r'Load|Try This|New Button)$', re.IGNORECASE)


# ── UI State Change Detection ──────────────────────────────────────────

def drain_state_changes(page) -> list[dict]:
    """Read and clear accumulated UI state changes from the JS watcher.

    Returns list of {sig, changes, t, tag, text, bid, x, y, w, h}.
    Each 'changes' is a list like ['enabled'], ['appeared', 'became_clickable'], ['new_element'].
    """
    try:
        return page.evaluate("() => window.__drainStateChanges ? window.__drainStateChanges() : []") or []
    except Exception:
        return []


def reset_state_watch(page):
    """Reset the state watcher's element snapshots and snap fresh baselines.

    Call at step boundary. The JS side clears WeakMaps and immediately runs
    _checkTransitions() to snap baselines from the current DOM state, avoiding
    the race where the 300ms interval snaps baselines AFTER elements have
    already changed.
    """
    try:
        page.evaluate("() => window.__resetStateWatch && window.__resetStateWatch()")
    except Exception:
        pass


def peek_state_changes(page) -> list[dict]:
    """Read accumulated UI state changes WITHOUT clearing the buffer.

    Use for post-round progress checks. The buffer stays intact so the
    next pre-round drain_state_changes() gets all accumulated changes.
    """
    try:
        return page.evaluate(
            "() => window.__peekStateChanges ? window.__peekStateChanges() : []"
        ) or []
    except Exception:
        return []


# Priority scores — tuned to reduce style noise
# Higher = stronger signal worth reporting to planner
CHANGE_PRIORITY = {
    'enabled': 0.95,           # disabled attr removed — strong unlock signal
    'new_element': 0.9,        # brand new interactive in DOM
    'turned_green': 0.85,      # color → green: could mean "done" or "ready/click me"
    'turned_red': 0.8,         # color → red: could mean "error" or "important/urgent"
    'appeared': 0.85,          # display:none → visible
    'revealed': 0.85,          # aria-hidden removed
    'became_clickable': 0.8,   # pointer-events: none → auto
    'text_changed': 0.75,      # textContent changed (counter, status, label update)
    'activated': 0.75,         # opacity < 0.5 → >= 0.8
    'turned_grey': 0.6,        # color → grey: likely "consumed/done", move on
    'became_interactive': 0.5, # cursor changed to pointer
    'became_vibrant': 0.45,    # vibrancy increased (noisy)
}

# Only these change types count as "progress" for stall detection
PROGRESS_CHANGE_TYPES = frozenset([
    'enabled', 'appeared', 'new_element', 'became_clickable', 'activated',
    'turned_green', 'turned_red',  # color shifts indicate challenge reacted
    'text_changed',                # text content update (counter, status)
])


def classify_state_changes(changes: list[dict]) -> list[dict]:
    """Enrich raw state changes with priority scores and deduplicate.

    Deduplicates by (position_bucket, top_change_type), keeping highest priority.
    Uses position+text as stable identity since BIDs are ephemeral.
    """
    for c in changes:
        c['priority'] = max((CHANGE_PRIORITY.get(ch, 0.1) for ch in c.get('changes', [])), default=0.0)

    seen = {}
    for c in changes:
        # Stable identity: position bucket + text prefix (BIDs can change between rounds)
        x_bucket = round(c.get('x', 0) / 30) * 30
        y_bucket = round(c.get('y', 0) / 30) * 30
        text_prefix = (c.get('text', '') or '')[:15]
        change_key = c['changes'][0] if c.get('changes') else 'unknown'
        key = (x_bucket, y_bucket, text_prefix, change_key)
        if key not in seen or c['priority'] > seen[key]['priority']:
            seen[key] = c
    deduped = list(seen.values())

    deduped.sort(key=lambda c: (-c['priority'], -c.get('t', 0)))
    return deduped


def match_state_changes_to_catalog(
    state_changes: list[dict],
    element_catalog: list[dict],
) -> list[dict]:
    """Match state changes to catalog elements by BID, then proximity+text fallback.

    BIDs are ephemeral (reassigned each round), so we use multi-signal matching:
    1. BID match (if state change captured a data-bid and it matches current catalog)
    2. Proximity + text match (position within 40px AND text substring overlap)
    3. Proximity-only match (position within 25px)

    Returns the state_changes list with matched_bid and matched_type fields set.
    """
    if not element_catalog:
        return state_changes

    for sc in state_changes:
        if sc.get('matched_bid') is not None:
            continue  # already matched

        # Strategy 1: BID match (most reliable when BIDs haven't been reassigned)
        bid_str = sc.get('bid')
        if bid_str is not None:
            try:
                bid_int = int(bid_str)
                for el in element_catalog:
                    if el.get('bid') == bid_int:
                        sc['matched_bid'] = bid_int
                        sc['matched_type'] = el.get('interactable_type')
                        break
            except (ValueError, TypeError):
                pass

        # Strategy 2: Proximity + text (handles BID reassignment)
        if 'matched_bid' not in sc:
            sc_text = (sc.get('text', '') or '').lower()[:20]
            best_dist = 999
            best_el = None
            for el in element_catalog:
                dx = abs(el.get('x', 0) - sc.get('x', -999))
                dy = abs(el.get('y', 0) - sc.get('y', -999))
                dist = dx + dy
                if dist > 40:
                    continue
                # Text overlap bonus
                el_text = (el.get('text', '') or '').lower()[:20]
                if sc_text and el_text and (sc_text in el_text or el_text in sc_text):
                    dist -= 15  # strong bonus for text match
                if dist < best_dist:
                    best_dist = dist
                    best_el = el
            if best_el and 'bid' in best_el:
                sc['matched_bid'] = best_el['bid']
                sc['matched_type'] = best_el.get('interactable_type')

    return state_changes


def wait_for_state(page, change_types: set | None = None,
                   timeout_ms: int = 3000, poll_ms: int = 150,
                   match_text: list[str] | None = None,
                   element_catalog: list[dict] | None = None,
                   ) -> dict | None:
    """Block until a UI state change matching criteria is detected, or timeout.

    Polls drain_state_changes() in a tight loop. When a change matching
    change_types (and optionally match_text) is found, it's matched to the
    element_catalog by BID or proximity, and returned with matched_bid set.

    Returns the best matching state change dict, or None on timeout.

    Args:
        change_types: Set of change type strings to match. Defaults to
            high-confidence types (enabled, appeared, new_element, became_clickable, activated).
        timeout_ms: Max wait time in milliseconds.
        poll_ms: Sleep between drain polls.
        match_text: Optional list of substrings to match against element text (case-insensitive).
        element_catalog: Current BID catalog for matching. If None, returns raw change without BID.
    """
    if change_types is None:
        change_types = {'enabled', 'appeared', 'new_element', 'became_clickable', 'activated'}

    deadline = time.time() + timeout_ms / 1000.0
    best = None

    while time.time() < deadline:
        raw = drain_state_changes(page)
        if raw:
            classified = classify_state_changes(raw)
            if element_catalog:
                match_state_changes_to_catalog(classified, element_catalog)
            for sc in classified:
                # Must match at least one requested change type
                if not any(ch in change_types for ch in sc.get('changes', [])):
                    # Note: this change is lost (drained but not matched)
                    continue
                # Optional text filter
                if match_text:
                    text = (sc.get('text', '') or '').lower()
                    if not any(mt.lower() in text for mt in match_text):
                        continue
                # Return first matching change (highest priority due to classify sort)
                return sc
        try:
            page.wait_for_timeout(poll_ms)
        except Exception:
            break

    return None  # timed out


# ── Container Discovery ──────────────────────────────────────────────

def discover_interactive_containers(page) -> list[dict]:
    """Find scrollable/nested styled containers that may hold interactive elements.

    Targets "fake iframe" patterns: divs with overflow:auto/scroll, borders,
    or transform that visually resemble embedded windows but are not actual iframes.
    Returns a list of containers, each with interactable children info.
    """
    try:
        return page.evaluate(r'''() => {
            const containers = [];
            const seen = new Set();

            // Heuristic: elements with scrollable overflow or significant nested content
            const candidates = document.querySelectorAll(
                '[class*="frame"], [class*="panel"], [class*="window"], ' +
                '[class*="container"], [class*="embed"], [class*="viewport"], ' +
                '[style*="overflow"]'
            );

            // Also check all elements with scrollable overflow
            const allEls = document.querySelectorAll('*');
            for (const el of allEls) {
                const cs = getComputedStyle(el);
                const overflow = cs.overflow + cs.overflowX + cs.overflowY;
                if ((overflow.includes('auto') || overflow.includes('scroll')) &&
                    el.scrollHeight > el.clientHeight + 20) {
                    if (!seen.has(el)) seen.add(el);
                }
            }
            for (const el of candidates) {
                if (!seen.has(el)) seen.add(el);
            }

            for (const el of seen) {
                const rect = el.getBoundingClientRect();
                if (rect.width < 50 || rect.height < 50) continue;
                if (rect.width > window.innerWidth * 0.95 &&
                    rect.height > window.innerHeight * 0.95) continue;

                // Find interactive children inside this container
                const interactives = [];
                const children = el.querySelectorAll(
                    'button, [role="button"], input, select, textarea, ' +
                    'a[href], [tabindex], [onclick], canvas, ' +
                    '[draggable="true"], [contenteditable="true"]'
                );
                for (const child of children) {
                    const cr = child.getBoundingClientRect();
                    if (cr.width === 0 || cr.height === 0) continue;
                    const ccs = getComputedStyle(child);
                    if (ccs.display === 'none' || ccs.visibility === 'hidden') continue;
                    interactives.push({
                        tag: child.tagName.toLowerCase(),
                        text: (child.textContent || '').trim().substring(0, 40),
                        role: child.getAttribute('role') || '',
                        x: Math.round(cr.x + cr.width / 2),
                        y: Math.round(cr.y + cr.height / 2),
                        w: Math.round(cr.width),
                        h: Math.round(cr.height),
                    });
                }

                if (interactives.length === 0) continue;

                const cs = getComputedStyle(el);
                containers.push({
                    tag: el.tagName.toLowerCase(),
                    className: (el.className || '').toString().substring(0, 60),
                    x: Math.round(rect.x),
                    y: Math.round(rect.y),
                    w: Math.round(rect.width),
                    h: Math.round(rect.height),
                    scrollable: el.scrollHeight > el.clientHeight + 20,
                    scrollHeight: el.scrollHeight,
                    clientHeight: el.clientHeight,
                    overflow: cs.overflow,
                    border: cs.border.substring(0, 40),
                    interactives: interactives.slice(0, 20),
                    interactive_count: interactives.length,
                });
            }

            // Sort: most interactives first, prefer smaller (more focused) containers
            containers.sort((a, b) => {
                if (b.interactive_count !== a.interactive_count)
                    return b.interactive_count - a.interactive_count;
                return (a.w * a.h) - (b.w * b.h);
            });

            return containers.slice(0, 10);
        }''')
    except Exception as e:
        log_stage("containers", f"discovery error: {e}")
        return []


# ── Lightweight Challenge Classifier ──────────────────────────────────

def classify_challenge_dom(page) -> dict:
    """Lightweight DOM archetype hint — ~5ms, no vision needed.

    Returns: {archetype: str, confidence: float, hints: dict}
    Archetypes: canvas_draw, drag_drop, scroll_box, timer_reveal,
                multi_click, form_fill, hover_reveal, nested_levels, unknown.
    Used as soft hint for planner, NOT a hard gate.
    """
    try:
        return page.evaluate(r'''() => {
            const body = document.body;
            if (!body) return {archetype: 'unknown', confidence: 0, hints: {}};

            const text = (body.innerText || '').substring(0, 2000).toLowerCase();
            const hints = {};

            // Canvas detection
            const canvases = document.querySelectorAll('canvas');
            const hasCanvas = canvases.length > 0;
            if (hasCanvas) {
                const c = canvases[0];
                const r = c.getBoundingClientRect();
                hints.canvas = {w: r.width, h: r.height};
            }

            // Draggable elements
            const draggables = document.querySelectorAll('[draggable="true"]');
            hints.draggable_count = draggables.length;

            // Scrollable containers (not page-level)
            let scrollContainers = 0;
            for (const el of document.querySelectorAll('div, section')) {
                const cs = getComputedStyle(el);
                const ov = cs.overflow + cs.overflowY;
                if ((ov.includes('scroll') || ov.includes('auto')) &&
                    el.scrollHeight > el.clientHeight + 50) {
                    const r = el.getBoundingClientRect();
                    if (r.width < window.innerWidth * 0.9) scrollContainers++;
                }
            }
            hints.scroll_containers = scrollContainers;

            // Form elements
            const inputs = document.querySelectorAll('input, select, textarea, [role="slider"]');
            hints.input_count = inputs.length;

            // Checkboxes/radios/selects (conditional_reveal signal)
            const formControls = document.querySelectorAll(
                'input[type="checkbox"], input[type="radio"], select, [role="slider"]');
            hints.form_control_count = formControls.length;

            // Buttons count (in viewport)
            let btnCount = 0;
            for (const b of document.querySelectorAll('button, [role="button"]')) {
                const r = b.getBoundingClientRect();
                if (r.width > 0 && r.bottom > 0 && r.top < window.innerHeight) btnCount++;
            }
            hints.button_count = btnCount;

            // Text signals
            hints.has_timer = /wait|after\s+\d+\s*s|timer|countdown/i.test(text);
            hints.has_hover = /hover/i.test(text);
            hints.has_nested = /nested|level|depth|enter\s+level/i.test(text);

            // Classify
            if (hasCanvas && /draw|stroke|sketch|paint/i.test(text))
                return {archetype: 'canvas_draw', confidence: 0.85, hints};
            if (draggables.length >= 2)
                return {archetype: 'drag_drop', confidence: 0.8, hints};
            if (scrollContainers > 0 && /scroll/i.test(text))
                return {archetype: 'scroll_box', confidence: 0.75, hints};
            if (hints.has_timer)
                return {archetype: 'timer_reveal', confidence: 0.7, hints};
            if (hints.has_nested)
                return {archetype: 'nested_levels', confidence: 0.75, hints};
            if (formControls.length >= 2)
                return {archetype: 'form_fill', confidence: 0.7, hints};
            if (hints.has_hover)
                return {archetype: 'hover_reveal', confidence: 0.7, hints};
            if (btnCount >= 3 && /click|trigger|mutation/i.test(text))
                return {archetype: 'multi_click', confidence: 0.65, hints};

            return {archetype: 'unknown', confidence: 0, hints};
        }''')
    except Exception:
        return {'archetype': 'unknown', 'confidence': 0, 'hints': {}}


# ── Action Validity Gate ──────────────────────────────────────────────

def validate_action(action: dict, element_catalog: list[dict] = None,
                    challenge_region: dict = None) -> tuple[bool, str]:
    """Pre-execution validation for an action dict. Returns (valid, reason).

    Catches common corruption patterns before wasting execution time:
    - Drag with missing or near-origin endpoints
    - Draw without a canvas target or with empty path
    - Click/hover with raw coords outside challenge region (when region known)
    """
    atype = action.get('type', '').lower()

    if atype == 'drag':
        # Must have either BID pair or coord pair or single BID (for resolution)
        has_bids = action.get('source_id') is not None and action.get('target_id') is not None
        x1, y1 = action.get('x1'), action.get('y1')
        x2, y2 = action.get('x2'), action.get('y2')
        has_coords = x1 is not None and y1 is not None and x2 is not None and y2 is not None
        has_single_bid = action.get('source_id') is not None or action.get('target_id') is not None
        if not has_bids and not has_coords and not has_single_bid:
            return False, "drag: no source/target IDs or coordinates"
        if has_coords:
            if abs(float(x1)) < 2 and abs(float(y1)) < 2:
                return False, f"drag: source near-origin ({x1},{y1})"
            if abs(float(x2)) < 2 and abs(float(y2)) < 2:
                return False, f"drag: destination near-origin ({x2},{y2})"

    elif atype in ('draw', 'canvas_draw'):
        path = action.get('path', [])
        if not path or len(path) < 2:
            return False, f"{atype}: empty or single-point path"

    elif atype in ('click', 'hover'):
        if action.get('element_id') is None:
            x, y = action.get('x'), action.get('y')
            if x is not None and y is not None:
                if abs(float(x)) < 2 and abs(float(y)) < 2:
                    return False, f"{atype}: coords near-origin ({x},{y})"
                if challenge_region:
                    rx = challenge_region['x']
                    ry = challenge_region['y']
                    rw = challenge_region['w']
                    rh = challenge_region['h']
                    margin = 80
                    if (float(x) < rx - margin or float(x) > rx + rw + margin or
                            float(y) < ry - margin or float(y) > ry + rh + margin):
                        return False, (f"{atype}: coords ({x},{y}) outside challenge "
                                       f"region ({rx},{ry},{rw},{rh})")

    elif atype in ('press', 'type'):
        val = action.get('keys') or action.get('text') or ''
        if not val.strip():
            return False, f"{atype}: empty value"

    return True, ""


# ── Structural Decoy Filtering ────────────────────────────────────────

def is_decoy_element(el_info: dict, challenge_region: dict = None) -> bool:
    """Structural decoy detection — text + position relative to challenge region.

    Args:
        el_info: dict with at least 'text' and 'y' keys (from annotate_elements).
        challenge_region: optional dict from find_challenge_region().

    Returns True if the element is a known decoy.
    """
    text = (el_info.get('text') or '').strip()
    # Text match against known decoy button labels
    if GLOBAL_DECOY_TEXT_RE.match(text):
        return True
    # Below challenge region (distraction zone)
    if challenge_region:
        region_bottom = challenge_region['y'] + challenge_region['h']
        if el_info.get('y', 0) > region_bottom + 100:
            return True
    return False


# ── Scroll Container JS (React-compatible) ─────────────────────────

def scroll_container_js(page, bid=None, direction="down", amount=600) -> dict:
    """React-compatible container scroll via scrollTop + scroll event dispatch.

    Resolution order: BID → BID ancestors → text-hint containers → first scrollable.
    Returns: {success, moved, scrollTop, scrollHeight, clientHeight, tag} or
             {success: False, reason: str}.
    """
    try:
        return page.evaluate("""(args) => {
            const {bid, direction, amount} = args;
            function isScrollable(el) {
                if (!el) return false;
                const s = window.getComputedStyle(el);
                const oy = s.overflowY || s.overflow;
                return (oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 10;
            }
            let el = null;
            // 1. Try BID element directly
            if (bid != null) el = document.querySelector(`[data-bid="${bid}"]`);
            // 2. If BID isn't scrollable, walk up ancestors
            if (el && !isScrollable(el)) {
                let cur = el.parentElement;
                while (cur && cur !== document.body) {
                    if (isScrollable(cur)) { el = cur; break; }
                    cur = cur.parentElement;
                }
                if (!isScrollable(el)) el = null;
            }
            // 3. Fallback: text-hint scroll box
            if (!el) {
                for (const d of document.querySelectorAll('div, section, main, article')) {
                    const t = (d.textContent || '').trim().toLowerCase();
                    if ((t.includes('scroll inside') || t.includes('scroll box') ||
                         t.includes('scroll here')) && isScrollable(d)) { el = d; break; }
                }
            }
            // 4. Fallback: first scrollable container
            if (!el) {
                for (const d of document.querySelectorAll('div, section, main, article')) {
                    if (d === document.body || d === document.documentElement) continue;
                    if (isScrollable(d)) { el = d; break; }
                }
            }
            if (!el) return {success: false, reason: 'no_scrollable_element'};
            const before = el.scrollTop;
            const delta = Math.max(100, Math.min(amount || 600, el.clientHeight * 0.9));
            if (direction === 'down') el.scrollTop = Math.min(el.scrollTop + delta, el.scrollHeight);
            else el.scrollTop = Math.max(el.scrollTop - delta, 0);
            // Browser fires native scroll event on scrollTop change
            return {success: true, moved: el.scrollTop !== before,
                    scrollTop: el.scrollTop, scrollHeight: el.scrollHeight,
                    clientHeight: el.clientHeight, tag: el.tagName};
        }""", {"bid": bid, "direction": direction, "amount": amount})
    except Exception as e:
        return {"success": False, "reason": str(e)}


# ── Hover Reveal Extract ────────────────────────────────────────────

def hover_reveal_extract(page, elements: list[dict] = None,
                         hold_ms=2000, max_hovers=10) -> dict:
    """Systematically hover shortlisted elements, waiting for reveals.

    If elements=None, auto-shortlist from page:
      - cursor:pointer elements with mystery affordances
      - Elements in challenge region, not decoys
      - Sorted by proximity to instruction text > element novelty > element size

    Uses adaptive timing: polls state_changes every ~200ms during hover hold,
    breaks early if a reveal is detected. Starts at 800ms default, extends to
    hold_ms only if no changes detected on first few elements.

    Returns: {code: str|None, reveals: int, action_log: list}
    """
    from code_scorer import is_valid_code

    action_log = []
    reveals = 0
    poll_interval_ms = 200
    # Start with shorter hold, extend if no early reveals
    adaptive_hold = min(800, hold_ms)
    no_reveal_streak = 0

    if elements is None:
        catalog = annotate_elements(page)
        region = find_challenge_region(page)
        elements = []
        for el in catalog:
            if not el.get('interactable'):
                continue
            if el.get('is_anchor') or is_decoy_element(el, region):
                continue
            if region:
                rx, ry = region['x'], region['y']
                rw, rh = region['w'], region['h']
                margin = 50
                if not (rx - margin <= el['x'] <= rx + rw + margin and
                        ry - margin <= el['y'] <= ry + rh + margin):
                    continue
            elements.append(el)
        # Sort: smaller elements first (more likely to be icons/buttons)
        elements.sort(key=lambda e: e.get('w', 999) * e.get('h', 999))
        elements = elements[:max_hovers]

    for el in elements:
        x = el.get('x', 0)
        y = el.get('y', 0)
        text = (el.get('text') or '')[:25]
        actual_hold = 0
        found_reveal = False

        try:
            page.mouse.move(x, y)
            # Adaptive polling: check state_changes every poll_interval_ms
            elapsed = 0
            while elapsed < adaptive_hold:
                page.wait_for_timeout(poll_interval_ms)
                elapsed += poll_interval_ms
                # Check for early reveal signal
                changes = peek_state_changes(page)
                reveal_signals = [c for c in changes if any(
                    ch in ('appeared', 'revealed', 'activated', 'new_element')
                    for ch in c.get('changes', [])
                )]
                if reveal_signals:
                    found_reveal = True
                    break
                # Check for code appearing mid-hold
                code = extract_code_js(page)
                if code and is_valid_code(code):
                    action_log.append({
                        'action_type': 'hover', 'target_coords': [x, y],
                        'target_text': text, 'hold_ms': elapsed,
                        'expect_code_visible': True,
                    })
                    return {'code': code, 'reveals': reveals, 'action_log': action_log}
            actual_hold = elapsed
        except Exception:
            continue

        if found_reveal:
            reveals += 1
            no_reveal_streak = 0
            # Shorten adaptive hold since reveals are quick on this page
            adaptive_hold = min(800, hold_ms)
        else:
            no_reveal_streak += 1
            # After 2 non-reveals, extend to full hold_ms for remaining
            if no_reveal_streak >= 2:
                adaptive_hold = hold_ms

        action_log.append({
            'action_type': 'hover', 'target_coords': [x, y],
            'target_text': text, 'hold_ms': actual_hold,
        })

        # Final code check after hold completes
        code = extract_code_js(page)
        if code and is_valid_code(code):
            action_log[-1]['expect_code_visible'] = True
            return {'code': code, 'reveals': reveals, 'action_log': action_log}

    return {'code': None, 'reveals': reveals, 'action_log': action_log}



def resolve_click_target(page, x: float, y: float) -> dict:
    """Use elementsFromPoint to find the topmost truly-interactable element.

    Returns a dict with:
      - elements: top-5 element stack at (x, y) with tag/class/role/pointer-events/cursor/disabled
      - chosen: the first interactable element in the stack (or None)
      - chosen_center: {x, y} center of the chosen element's bbox
      - dossier: compact debug string for logging
    """
    return page.evaluate("""(args) => {
        const {x, y} = args;
        const stack = document.elementsFromPoint(x, y);
        const top5 = stack.slice(0, 8).map(el => {
            const cs = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return {
                tag: el.tagName,
                id: el.id || '',
                cls: (el.className || '').toString().substring(0, 60),
                role: el.getAttribute('role') || '',
                pointerEvents: cs.pointerEvents,
                cursor: cs.cursor,
                disabled: el.disabled || el.getAttribute('aria-disabled') === 'true',
                w: Math.round(rect.width),
                h: Math.round(rect.height),
                cx: Math.round(rect.left + rect.width / 2),
                cy: Math.round(rect.top + rect.height / 2),
                text: (el.textContent || '').trim().substring(0, 40),
            };
        });

        // Find first truly interactable element in the stack
        let chosen = null;
        for (const info of top5) {
            if (info.pointerEvents === 'none') continue;
            if (info.disabled) continue;
            if (info.w < 2 || info.h < 2) continue;
            const isClickable = (
                info.cursor === 'pointer' ||
                ['BUTTON', 'A', 'INPUT', 'SELECT', 'TEXTAREA'].includes(info.tag) ||
                ['button', 'link', 'tab', 'menuitem', 'checkbox', 'radio'].includes(info.role)
            );
            if (isClickable) {
                chosen = info;
                break;
            }
        }

        // If no explicitly clickable element, pick first non-blocked element
        if (!chosen) {
            for (const info of top5) {
                if (info.pointerEvents !== 'none' && !info.disabled && info.w >= 2 && info.h >= 2) {
                    chosen = info;
                    break;
                }
            }
        }

        const dossier = top5.map((e, i) =>
            `${i}: <${e.tag}> ptr=${e.pointerEvents} cur=${e.cursor} dis=${e.disabled} ` +
            `${e.w}x${e.h} "${e.text.substring(0, 20)}"`
        ).join(' | ');

        return {
            elements: top5,
            chosen: chosen,
            chosen_center: chosen ? {x: chosen.cx, y: chosen.cy} : null,
            dossier: dossier,
        };
    }""", {"x": x, "y": y})


