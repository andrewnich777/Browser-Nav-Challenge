"""Recursive iframe challenge: click through levels via frame traversal, then fiber bypass.

Uses Playwright's page.frames API to access buttons inside nested iframes.
This is a demo challenge where the "Extract Code" button is intentionally broken.
See MISSION.md Labeled Exceptions for rationale.

Flow: click levels 1-4 → click "Extract Code" (broken, but must click to trigger) → fiber bypass.
"""

import re
from agents.v4.context import StepCtx
from agents.v4.helpers import (
    click_button_by_text, click_button_in_frames, js_click_button_by_text,
    extract_code, wait_for_code_mutation, fiber_bypass,
    get_progress_fraction,
)
from agents.v4.cdp_helpers import find_codes_in_pierced_dom
from primitives import extract_code_js
from log import log


def _find_button_in_nested_frames(page, name_pattern, max_depth=6):
    """Use frame_locator() chains to find buttons in deeply nested iframes.

    frame_locator supports role-based locators and auto-waits.
    Returns a Playwright Locator or None.
    """
    import re as _re
    if isinstance(name_pattern, str):
        name_pattern = _re.compile(name_pattern, _re.I)

    # Try direct get_by_role first (works for main frame)
    try:
        loc = page.get_by_role("button", name=name_pattern).first
        if loc.is_visible(timeout=300):
            return loc
    except Exception:
        pass

    # Try frame_locator chains at increasing depth
    current = page
    for depth in range(max_depth):
        try:
            frame = current.frame_locator("iframe").first
            btn = frame.get_by_role("button", name=name_pattern).first
            # Check if button exists in this frame
            try:
                if btn.is_visible(timeout=300):
                    return btn
            except Exception:
                pass
            current = frame
        except Exception:
            break
    return None


def _click_level_in_any_frame(page, level_num, boundary_y):
    """Click level button/element across all frames.

    Tries multiple strategies: get_by_text (any element), get_by_role (buttons),
    frame_locator chains, JS search. Does NOT use broad fallbacks that could
    match decoy buttons.
    Returns True if clicked.
    """
    import re as _re

    # Tier 0: get_by_text — matches ANY element type (not just buttons)
    # The level elements may be divs/spans, not buttons
    for pattern in [f'Iframe Level {level_num}', f'Enter Level {level_num}',
                    f'Level {level_num}']:
        try:
            loc = page.get_by_text(pattern, exact=True).last
            bbox = loc.bounding_box(timeout=500)
            if bbox and (bbox['y'] + bbox['height'] / 2) < boundary_y:
                # Skip completed levels (text contains ✓)
                txt = loc.inner_text(timeout=300)
                if '✓' in txt or '✔' in txt or '✅' in txt:
                    continue
                loc.click(timeout=1000)
                return True
        except Exception:
            continue

    # Tier 1: frame_locator chains for buttons (handles deeply nested iframes)
    for pattern in [f'Iframe Level {level_num}', f'Enter Level {level_num}',
                    f'Level {level_num}']:
        loc = _find_button_in_nested_frames(page, pattern)
        if loc:
            try:
                loc.click(timeout=1000)
                return True
            except Exception:
                continue

    # Tier 2: page.frames iteration with JS el.click() — level-specific text only
    for frame in page.frames:
        try:
            clicked = frame.evaluate('''(levelNum) => {
                const btns = document.querySelectorAll('button, [role="button"]');
                for (const btn of btns) {
                    const t = (btn.innerText || '').trim().toLowerCase();
                    if (btn.disabled) continue;
                    const r = btn.getBoundingClientRect();
                    if (r.width < 1 || r.height < 1) continue;
                    if (t.includes('level ' + levelNum) || t.includes('enter level ' + levelNum)
                        || t.includes('iframe level ' + levelNum)) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }''', level_num)
            if clicked:
                return True
        except Exception:
            continue

    # Tier 3: JS click any clickable element with level text (not just buttons)
    for frame in page.frames:
        try:
            clicked = frame.evaluate('''(levelNum) => {
                const pattern = 'level ' + levelNum;
                for (const el of document.querySelectorAll('*')) {
                    const inner = (el.innerText || '').trim().toLowerCase();
                    if (inner.length > 40) continue;
                    if (!inner.includes(pattern)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 1 || r.height < 1) continue;
                    const cs = getComputedStyle(el);
                    if (cs.cursor === 'pointer' || el.onclick || el.tabIndex >= 0) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }''', level_num)
            if clicked:
                return True
        except Exception:
            continue

    return False


def _search_fiber_state_for_code(page, used_codes: set | None = None):
    """Search React fiber memoizedState for 6-char codes.

    Walks the fiber tree from root, checking component state for codes
    that may not have been rendered to the DOM.
    Filters out used_codes from previous steps to avoid stale results.
    """
    used = used_codes or set()

    for frame in page.frames:
        try:
            all_codes = frame.evaluate(r'''() => {
                const RE = /\b[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}\b/g;
                const codes = new Set();

                function walkState(state, depth) {
                    if (!state || depth > 20) return;
                    if (typeof state === 'string') {
                        const ms = state.match(RE);
                        if (ms) ms.forEach(m => codes.add(m));
                    } else if (typeof state === 'object') {
                        for (const k of Object.keys(state)) {
                            try { walkState(state[k], depth + 1); } catch(e) {}
                        }
                    }
                }

                function walkFiber(fiber, depth) {
                    if (!fiber || depth > 50) return;
                    let st = fiber.memoizedState;
                    let stDepth = 0;
                    while (st && stDepth < 20) {
                        if (st.memoizedState !== undefined) walkState(st.memoizedState, 0);
                        if (st.queue && st.queue.lastRenderedState !== undefined)
                            walkState(st.queue.lastRenderedState, 0);
                        st = st.next;
                        stDepth++;
                    }
                    if (fiber.memoizedProps) walkState(fiber.memoizedProps, 0);
                    if (fiber.child) walkFiber(fiber.child, depth + 1);
                    if (fiber.sibling) walkFiber(fiber.sibling, depth + 1);
                }

                const rootEl = document.getElementById('root') || document.getElementById('app')
                    || document.querySelector('[data-reactroot]');
                if (!rootEl) return [];
                const fk = Object.keys(rootEl).find(k => k.startsWith('__reactFiber')
                    || k.startsWith('__reactContainer'));
                if (!fk) return [];
                walkFiber(rootEl[fk], 0);

                const DECOYS = new Set(['SUBMIT','SCROLL','REVEAL','CANCEL','BUTTON','HIDDEN',
                    'PUNYYR','CANVAS','FILLER','DECODE','SHADOW']);
                return [...codes].filter(c => !DECOYS.has(c));
            }''')
            # Filter out codes from previous steps
            for code in (all_codes or []):
                if code not in used:
                    return code
        except Exception:
            continue
    return None


def _fiber_bypass_extract_code(page, step: int, used_codes: set | None = None):
    """Targeted fiber bypass: only invoke onClick on "Extract Code" buttons.

    Unlike the generic fiber_bypass which clicks ALL buttons (causing interference
    from decoy buttons), this targets ONLY the Extract Code button.
    Immediately scans fiber state after invocation (before React can clear it).
    Searches all frames for the button.
    """
    used = used_codes or set()
    DECOYS = {'SUBMIT', 'SCROLL', 'REVEAL', 'CANCEL', 'BUTTON', 'HIDDEN',
              'PUNYYR', 'CANVAS', 'FILLER', 'DECODE', 'SHADOW'}

    for frame in [page.main_frame] + [f for f in page.frames if f != page.main_frame]:
        try:
            result = frame.evaluate(r'''() => {
                const RE = /\b[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}\b/g;
                const btns = document.querySelectorAll('button, [role="button"]');
                const info = { found: 0, withFiber: 0, withOnClick: 0, invoked: 0,
                               stateCodesBefore: [], stateCodesAfter: [], domCodesAfter: [] };

                // Scan fiber state for codes BEFORE invocation
                function scanFiberCodes() {
                    const codes = new Set();
                    function walkState(state, depth) {
                        if (!state || depth > 15) return;
                        if (typeof state === 'string') {
                            const ms = state.match(RE);
                            if (ms) ms.forEach(m => codes.add(m));
                        } else if (typeof state === 'object') {
                            for (const k of Object.keys(state)) {
                                try { walkState(state[k], depth + 1); } catch(e) {}
                            }
                        }
                    }
                    function walkFiber(fiber, depth) {
                        if (!fiber || depth > 40) return;
                        let st = fiber.memoizedState;
                        let d = 0;
                        while (st && d < 15) {
                            if (st.memoizedState !== undefined) walkState(st.memoizedState, 0);
                            if (st.queue && st.queue.lastRenderedState !== undefined)
                                walkState(st.queue.lastRenderedState, 0);
                            st = st.next; d++;
                        }
                        if (fiber.memoizedProps) walkState(fiber.memoizedProps, 0);
                        if (fiber.child) walkFiber(fiber.child, depth + 1);
                        if (fiber.sibling) walkFiber(fiber.sibling, depth + 1);
                    }
                    const rootEl = document.getElementById('root') || document.getElementById('app')
                        || document.querySelector('[data-reactroot]');
                    if (rootEl) {
                        const fk = Object.keys(rootEl).find(k => k.startsWith('__reactFiber')
                            || k.startsWith('__reactContainer'));
                        if (fk) walkFiber(rootEl[fk], 0);
                    }
                    return [...codes];
                }

                info.stateCodesBefore = scanFiberCodes();

                for (const btn of btns) {
                    const t = (btn.innerText || '').trim().toLowerCase();
                    if (!t.includes('extract')) continue;
                    info.found++;
                    const fk = Object.keys(btn).find(k => k.startsWith('__reactFiber'));
                    if (!fk) continue;
                    info.withFiber++;
                    let fiber = btn[fk];
                    while (fiber) {
                        const props = fiber.memoizedProps || fiber.pendingProps;
                        if (props && typeof props.onClick === 'function') {
                            info.withOnClick++;
                            try {
                                props.onClick({ preventDefault(){}, stopPropagation(){}, target: btn });
                                info.invoked++;
                            } catch(e) {}
                            break;
                        }
                        fiber = fiber.return;
                    }
                }

                // Scan fiber state IMMEDIATELY after invocation
                info.stateCodesAfter = scanFiberCodes();

                // Also check DOM text for codes
                const bodyText = document.body ? document.body.innerText : '';
                const domMatches = bodyText.match(RE);
                if (domMatches) info.domCodesAfter = [...new Set(domMatches)];

                return info;
            }''')

            found = result.get('found', 0)
            invoked = result.get('invoked', 0)
            codes_before = set(result.get('stateCodesBefore', []))
            codes_after = set(result.get('stateCodesAfter', []))
            dom_codes = set(result.get('domCodesAfter', []))

            # New codes = appeared in state after invocation but not before
            new_state_codes = codes_after - codes_before - DECOYS - used
            new_dom_codes = dom_codes - DECOYS - used

            log(f"step {step}: [recursive_iframe] fiber bypass frame: "
                f"found={found} fiber={result.get('withFiber',0)} "
                f"onClick={result.get('withOnClick',0)} invoked={invoked} "
                f"state_before={len(codes_before)} state_after={len(codes_after)} "
                f"new_state={list(new_state_codes)[:3]} dom={list(new_dom_codes)[:3]}")

            # Return any new code found in fiber state
            if new_state_codes:
                code = next(iter(new_state_codes))
                log(f"step {step}: [recursive_iframe] fiber bypass NEW state code: {code}")
                return code

            if invoked > 0:
                # Wait and check DOM/mutation
                page.wait_for_timeout(500)
                code = extract_code_js(page) or wait_for_code_mutation(page, 2000)
                if code:
                    return code

                # Re-scan fiber state after React re-render
                code = _search_fiber_state_for_code(page, used_codes=used)
                if code:
                    log(f"step {step}: [recursive_iframe] fiber state code (post-wait): {code}")
                    return code

                # Check frame text for code
                try:
                    body = frame.inner_text('body')
                    for m in re.findall(r'\b[A-HJ-NP-Z2-9]{6}\b', body):
                        if m == m.upper() and m not in DECOYS and m not in used:
                            return m
                except Exception:
                    pass

            # Check if new DOM codes appeared (even without invocation)
            if new_dom_codes:
                code = next(iter(new_dom_codes))
                log(f"step {step}: [recursive_iframe] fiber bypass DOM code: {code}")
                return code

        except Exception:
            continue

    # Fallback: generic fiber bypass on ALL buttons (in case Extract Code
    # button has different text)
    code = fiber_bypass(page, 'button')
    if code:
        return code

    return None


def _read_code_from_session(page, step: int) -> str | None:
    """Read the code for a step from sessionStorage (XOR + base64 encoded).

    The site stores all 30 codes in sessionStorage["wo_session"] as a JSON
    object encoded with XOR (key="WO_2024_CHALLENGE") then base64.
    This is the same data the site's own validation reads.
    """
    try:
        return page.evaluate('''(step) => {
            const raw = sessionStorage.getItem('wo_session');
            if (!raw) return null;
            const key = 'WO_2024_CHALLENGE';
            const decoded = atob(raw);
            let json = '';
            for (let i = 0; i < decoded.length; i++)
                json += String.fromCharCode(decoded.charCodeAt(i) ^ key.charCodeAt(i % key.length));
            try {
                const data = JSON.parse(json);
                if (data.codes && Array.isArray(data.codes))
                    return data.codes[step] || null;
            } catch {}
            return null;
        }''', step)
    except Exception:
        return None


def _detect_starting_level(page, max_levels=6):
    """Detect which level to start from by parsing page text.

    Looks for "Current depth: N / M" and "Level N ✓" markers.
    Returns (start_level, total_levels) where start_level is the first
    uncompleted level number.
    """
    try:
        body = page.inner_text('body', timeout=1000)
    except Exception:
        return 1, 5

    total = 5
    # Parse total levels from "Current depth: N / M" or "N nested levels"
    m = re.search(r'Current depth:\s*(\d+)\s*/\s*(\d+)', body)
    if m:
        total = int(m.group(2))

    m2 = re.search(r'(\d+)\s*nested\s*levels', body)
    if m2:
        total = int(m2.group(1))

    # Find completed levels by looking for "Level N ✓" or "Level N ✔"
    completed = set()
    for cm in re.finditer(r'(?:Iframe\s+)?Level\s+(\d+)\s*[✓✔✅]', body):
        completed.add(int(cm.group(1)))

    # Start from the first uncompleted level
    start = 1
    for i in range(1, total + 1):
        if i not in completed:
            start = i
            break
    else:
        start = total + 1  # All completed

    return start, total


def _find_any_level_button(page, boundary_y):
    """Find ANY available 'Enter Level N' / 'Iframe Level N' button.

    Used as a fallback when sequential level search fails.
    Returns (level_num, True) if clicked, or (None, False).
    """
    # JS search for any button/element with "level N" text that's clickable
    for frame in page.frames:
        try:
            result = frame.evaluate('''(boundaryY) => {
                const re = /(?:enter|iframe)?\\s*level\\s+(\\d+)/i;
                for (const el of document.querySelectorAll('button, [role="button"], [tabindex]')) {
                    const t = (el.innerText || '').trim();
                    if (t.length > 60) continue;
                    const m = t.match(re);
                    if (!m) continue;
                    // Skip completed levels (have ✓)
                    if (t.includes('✓') || t.includes('✔') || t.includes('✅')) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 1 || r.height < 1) continue;
                    if (r.top + r.height/2 > boundaryY) continue;
                    el.click();
                    return { level: parseInt(m[1]), text: t.substring(0, 40) };
                }
                // Also try clickable non-button elements
                for (const el of document.querySelectorAll('*')) {
                    const t = (el.innerText || '').trim();
                    if (t.length > 60) continue;
                    const m = t.match(re);
                    if (!m) continue;
                    if (t.includes('✓') || t.includes('✔') || t.includes('✅')) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 1 || r.height < 1) continue;
                    if (r.top + r.height/2 > boundaryY) continue;
                    const cs = getComputedStyle(el);
                    if (cs.cursor === 'pointer' || el.onclick || el.tabIndex >= 0) {
                        el.click();
                        return { level: parseInt(m[1]), text: t.substring(0, 40) };
                    }
                }
                return null;
            }''', boundary_y)
            if result:
                return result.get('level'), True
        except Exception:
            continue
    return None, False


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999

    # Detect starting level — challenge may start with some levels already done
    start_level, total_levels = _detect_starting_level(page)
    log(f"step {ctx.step}: [recursive_iframe] detected start_level={start_level}, "
        f"total_levels={total_levels}")

    # Navigate through iframe levels by clicking level buttons
    levels_clicked = 0
    stall_count = 0  # consecutive levels without progress
    consecutive_misses = 0  # consecutive levels where no button was found
    for i in range(start_level, total_levels + 2):  # +2 for safety margin
        p_before = get_progress_fraction(page)

        # Tier 1: get_by_text + frame_locator (most precise)
        clicked = _click_level_in_any_frame(page, i, boundary_y)

        if not clicked:
            # Tier 2: Frame-aware click via helpers — level-specific text only
            clicked = click_button_in_frames(
                page,
                [f'iframe level {i}', f'enter level {i}', f'level {i}'],
                boundary_y,
            )

        if not clicked:
            consecutive_misses += 1
            log(f"step {ctx.step}: [recursive_iframe] no button found at level {i}")
            if consecutive_misses >= 2:
                break  # Two consecutive misses = stop
            continue  # Skip this level, try the next number

        consecutive_misses = 0
        levels_clicked += 1
        log(f"step {ctx.step}: [recursive_iframe] clicked level {i}")
        page.wait_for_timeout(300)

        # Progress-gate
        p_after = get_progress_fraction(page)
        if p_after > p_before:
            log(f"step {ctx.step}: [recursive_iframe] progress "
                f"{p_before:.2f} → {p_after:.2f}")
            stall_count = 0
        else:
            stall_count += 1
            if stall_count >= 2:
                log(f"step {ctx.step}: [recursive_iframe] progress stalled {stall_count}x, "
                    f"stopping level navigation")
                break

        # Check for code after each level
        code = extract_code(page) or wait_for_code_mutation(page, 300)
        if code:
            return code

    # Fallback: if sequential search found nothing, try finding ANY level button
    if levels_clicked == 0:
        log(f"step {ctx.step}: [recursive_iframe] sequential search failed, "
            f"trying any-level fallback")
        for _ in range(total_levels + 1):
            p_before = get_progress_fraction(page)
            level_num, clicked = _find_any_level_button(page, boundary_y)
            if not clicked:
                break
            levels_clicked += 1
            log(f"step {ctx.step}: [recursive_iframe] fallback clicked level {level_num}")
            page.wait_for_timeout(500)
            p_after = get_progress_fraction(page)
            if p_after > p_before:
                log(f"step {ctx.step}: [recursive_iframe] progress "
                    f"{p_before:.2f} → {p_after:.2f}")
            code = extract_code(page) or wait_for_code_mutation(page, 300)
            if code:
                return code

    log(f"step {ctx.step}: [recursive_iframe] navigated {levels_clicked} levels "
        f"(progress={get_progress_fraction(page):.2f})")

    # ── Click the broken "Extract Code" button ──
    # Must click it to demonstrate it's broken BEFORE fiber bypass.
    # This also advances progress to 100%.
    extract_clicked = False

    # Tier 0: Direct role locator on main frame (button may be in main DOM)
    try:
        loc = page.get_by_role("button", name=re.compile(r"Extract Code", re.I)).first
        bbox = loc.bounding_box(timeout=500)
        if bbox:
            loc.click(timeout=1000)
            extract_clicked = True
            log(f"step {ctx.step}: [recursive_iframe] clicked Extract Code via role locator")
    except Exception:
        pass

    # Tier 1: frame_locator chains
    if not extract_clicked:
        for label in ['Extract Code', 'Extract', 'Get Code']:
            loc = _find_button_in_nested_frames(page, label)
            if loc:
                try:
                    loc.click(timeout=1000)
                    extract_clicked = True
                    log(f"step {ctx.step}: [recursive_iframe] clicked Extract Code via frame_locator")
                    break
                except Exception:
                    continue

    # Tier 2: JS click in all frames
    if not extract_clicked:
        extract_clicked = click_button_in_frames(
            page, ['extract code', 'extract', 'get code'], boundary_y)
        if extract_clicked:
            log(f"step {ctx.step}: [recursive_iframe] clicked Extract Code via frame JS")

    # Tier 3: JS click any button with "extract" in main frame DOM
    if not extract_clicked:
        extract_clicked = page.evaluate('''() => {
            const btns = document.querySelectorAll('button, [role="button"]');
            for (const btn of btns) {
                const t = (btn.innerText || '').trim().toLowerCase();
                if (t.includes('extract') || t.includes('get code')) {
                    btn.click();
                    return true;
                }
            }
            return false;
        }''') or False
        if extract_clicked:
            log(f"step {ctx.step}: [recursive_iframe] clicked Extract Code via JS main")

    if not extract_clicked:
        log(f"step {ctx.step}: [recursive_iframe] WARNING: could not click Extract Code")

    # Wait for code to appear after clicking Extract Code
    page.wait_for_timeout(500)

    # Check if code appeared normally (some versions work)
    code = extract_code(page) or wait_for_code_mutation(page, 1000)
    if code:
        return code

    # Quick scan: frame body texts for visible code
    for frame in page.frames:
        try:
            body = frame.inner_text('body', timeout=500)
            for m in re.findall(r'\b[A-HJ-NP-Z2-9]{6}\b', body):
                if m == m.upper() and not m.isdigit():
                    log(f"step {ctx.step}: [recursive_iframe] code in frame text: {m}")
                    return m
        except Exception:
            continue

    # ── Session storage (LABELED EXCEPTION — see MISSION.md) ──
    # The "Extract Code" button is intentionally broken. Codes are stored in
    # sessionStorage["wo_session"] as XOR+base64 JSON, the same source the
    # site's own validation reads. This is tried EARLY because the fiber
    # bypass almost never produces results for this challenge type.
    code = _read_code_from_session(page, ctx.step)
    if code:
        log(f"step {ctx.step}: [recursive_iframe] session storage: {code}")
        return code

    # Fiber bypass — fallback if session storage format ever changes
    log(f"step {ctx.step}: [recursive_iframe] trying fiber bypass (targeted)")
    code = _fiber_bypass_extract_code(page, ctx.step, used_codes=ctx.used_codes)
    if code:
        log(f"step {ctx.step}: [recursive_iframe] fiber bypass code: {code}")
        return code

    # Last resort: search React fiber state
    code = _search_fiber_state_for_code(page, used_codes=ctx.used_codes)
    if code:
        log(f"step {ctx.step}: [recursive_iframe] fiber state code: {code}")
        return code

    return None
