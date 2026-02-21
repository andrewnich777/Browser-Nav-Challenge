"""Drag-and-drop challenge: drag pieces from selection row into drop slots.

Any piece fits any slot — the only constraint is uniqueness (duplicates in the
selection row get greyed out when one is placed). So the algorithm is O(slots):
grab first available piece, drop on first empty slot, rescan, repeat.

Uses Playwright's locator.drag_to() for reliable HTML5 drag events.
Falls back to coordinate-based mouse drag if slot CSS selector can't be found.

All interactions use Playwright drag APIs — no synthetic DragEvent dispatch
(MISSION compliant).
"""

import re
from agents.v4.context import StepCtx
from agents.v4.helpers import (
    wait_for_code_mutation, extract_code, get_progress_fraction,
)
from log import log


def _check_fill_status(page) -> tuple[int, int]:
    """Read "N / M filled" from page text. Returns (current, total) or (-1, -1)."""
    try:
        text = page.evaluate('() => (document.body?.innerText || "").substring(0, 500)')
        m = re.search(r'(\d+)\s*/\s*(\d+)\s*filled', text)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return -1, -1


def _is_complete(page) -> bool:
    """Check if all pieces are placed (progress >= 1.0 or fill == total)."""
    frac = get_progress_fraction(page)
    if frac >= 1.0:
        return True
    fill, total = _check_fill_status(page)
    return fill >= total > 0


def _find_slot_selector(page) -> dict | None:
    """Find a CSS selector matching slot elements and indices of empty slots.

    Tries class-based and data-attribute selectors, returns the first one that
    matches 2-12 slot-sized elements with at least one empty slot.
    """
    return page.evaluate(r'''() => {
        const classPatterns = ['slot', 'drop', 'target', 'zone'];
        const candidateSelectors = [
            '[data-slot]', '[data-drop]', '[data-index]', '[data-position]',
            '[data-accept]', '[data-type]', '.slot', '.drop-zone', '.drop-target',
            '[ondrop]', '[ondragover]',
        ];

        // Find class-based selectors from actual page elements
        const seenClasses = new Set();
        for (const el of document.querySelectorAll('*')) {
            const cls = el.className;
            if (typeof cls !== 'string') continue;
            for (const word of cls.split(/\s+/)) {
                if (!word || seenClasses.has(word)) continue;
                seenClasses.add(word);
                const lower = word.toLowerCase();
                for (const pat of classPatterns) {
                    if (lower.includes(pat)) {
                        candidateSelectors.push('.' + CSS.escape(word));
                        break;
                    }
                }
            }
        }

        // Test each selector — find one with 2-12 slot-sized elements
        for (const sel of [...new Set(candidateSelectors)]) {
            try {
                const els = [...document.querySelectorAll(sel)];
                if (els.length < 2 || els.length > 12) continue;

                // Filter to slot-sized, visible, non-draggable elements
                const slots = els.filter(el => {
                    if (el.hasAttribute('draggable')) return false;
                    const r = el.getBoundingClientRect();
                    return r.width >= 20 && r.width <= 200
                        && r.height >= 20 && r.height <= 200
                        && r.top >= 0 && r.top <= 800;
                });
                if (slots.length < 2) continue;

                // Find empty slot indices (within original querySelectorAll order)
                const emptyIndices = [];
                els.forEach((el, i) => {
                    if (el.hasAttribute('draggable')) return;
                    const r = el.getBoundingClientRect();
                    if (r.width < 20 || r.height < 20 || r.top < 0 || r.top > 800) return;

                    const text = (el.innerText || '').trim();
                    const hasDraggable = el.querySelector('[draggable="true"]') !== null;
                    // Check for visible child that ISN'T a slot label (dropped piece
                    // may lose draggable attr but its text won't match slot patterns)
                    const hasVisibleChild = [...el.children].some(child => {
                        if (child.tagName === 'BR') return false;
                        const cr = child.getBoundingClientRect();
                        if (cr.width <= 10 || cr.height <= 10) return false;
                        const ct = (child.innerText || '').trim();
                        // Exclude slot label text — these are placeholders, not pieces
                        if (ct === '' || /^slot\s*\d*$/i.test(ct) ||
                            /^drop\s*(here|zone)?\s*\d*$/i.test(ct) ||
                            /^\d{1,2}$/.test(ct) ||
                            /^(target|zone)\s*\d*$/i.test(ct)) return false;
                        return true;
                    });
                    const isEmpty = !hasDraggable && !hasVisibleChild && (
                        text === '' ||
                        /^slot\s*\d*$/i.test(text) ||
                        /^drop\s*(here|zone)?\s*\d*$/i.test(text) ||
                        /^\d{1,2}$/.test(text) ||
                        /^(target|zone)\s*\d*$/i.test(text)
                    );
                    if (isEmpty) emptyIndices.push(i);
                });

                if (emptyIndices.length > 0) {
                    return {
                        selector: sel,
                        total: slots.length,
                        emptyIndices: emptyIndices,
                    };
                }
            } catch (e) {}
        }
        return null;
    }''')


def _get_available_piece_indices(page) -> list[int]:
    """Get indices of available (visible, non-greyed) draggable pieces.

    Returns indices within page.locator('[draggable="true"]') ordering.
    """
    return page.evaluate('''() => {
        const all = document.querySelectorAll('[draggable="true"]');
        const indices = [];
        [...all].forEach((el, i) => {
            const r = el.getBoundingClientRect();
            if (r.width < 5 || r.height < 5 || r.top < 0 || r.top > 800) return;
            const style = getComputedStyle(el);
            if (parseFloat(style.opacity) < 0.4) return;
            if (style.pointerEvents === 'none') return;
            if (el.getAttribute('aria-disabled') === 'true' || el.disabled) return;
            indices.push(i);
        });
        return indices;
    }''') or []


def _mouse_drag(page, sx, sy, dx, dy):
    """Fallback: coordinate-based mouse drag for when CSS selectors fail."""
    page.mouse.move(sx, sy)
    page.wait_for_timeout(20)
    page.mouse.down()
    page.wait_for_timeout(30)
    page.mouse.move(dx, dy, steps=12)
    page.mouse.move(dx + 1, dy)  # double hover for dragover
    page.wait_for_timeout(20)
    page.mouse.up()
    page.wait_for_timeout(30)


def _scan_coords(page) -> dict:
    """Fallback: find pieces and slots by coordinates (for when CSS selector fails)."""
    return page.evaluate('''() => {
        const emptySlots = [];
        const filledSlots = [];

        for (const el of document.querySelectorAll('*')) {
            const rect = el.getBoundingClientRect();
            if (rect.width < 20 || rect.height < 20 || rect.width > 150 || rect.height > 150) continue;
            if (rect.top < 0 || rect.top > 800) continue;
            if (el.hasAttribute('draggable')) continue;

            const classStr = (el.className || '').toString().toLowerCase();
            const style = getComputedStyle(el);
            const slotText = (el.innerText || '').trim();
            const isDropRelated =
                classStr.includes('slot') || classStr.includes('drop') ||
                classStr.includes('target') || classStr.includes('zone') ||
                el.ondrop !== null || el.ondragover !== null ||
                style.borderStyle.includes('dashed') || style.borderStyle.includes('dotted') ||
                el.hasAttribute('data-slot') || el.hasAttribute('data-drop') ||
                el.hasAttribute('data-index') || el.hasAttribute('data-position') ||
                el.hasAttribute('data-accept') || el.hasAttribute('data-type') ||
                // Text-based slot detection: "Slot 1", "Drop zone 2", etc.
                /^slot\\s*\\d+$/i.test(slotText) ||
                /^drop\\s*(here|zone)?\\s*\\d*$/i.test(slotText);
            if (!isDropRelated) continue;

            const cx = Math.round(rect.x + rect.width/2);
            const cy = Math.round(rect.y + rect.height/2);
            const hasDraggable = el.querySelector('[draggable="true"]') !== null;
            // Check for visible child that ISN'T a slot label (dropped piece
            // may lose draggable attr but its text won't match slot patterns)
            const hasVisibleChild = [...el.children].some(child => {
                if (child.tagName === 'BR') return false;
                const cr = child.getBoundingClientRect();
                if (cr.width <= 10 || cr.height <= 10) return false;
                const ct = (child.innerText || '').trim();
                if (ct === '' || /^slot\\s*\\d*$/i.test(ct) ||
                    /^drop\\s*(here|zone)?\\s*\\d*$/i.test(ct) ||
                    /^\\d{1,2}$/.test(ct) ||
                    /^(target|zone)\\s*\\d*$/i.test(ct)) return false;
                return true;
            });
            const isPlaceholder =
                slotText === '' ||
                /^slot\\s*\\d*$/i.test(slotText) ||
                /^drop\\s*(here|zone)?\\s*\\d*$/i.test(slotText) ||
                /^\\d{1,2}$/.test(slotText) ||
                /^(target|zone)\\s*\\d*$/i.test(slotText);

            if (!hasDraggable && !hasVisibleChild && isPlaceholder) emptySlots.push({x: cx, y: cy});
            else filledSlots.push({x: cx, y: cy});
        }

        const draggables = Array.from(document.querySelectorAll('[draggable="true"]'));
        const pieces = draggables.filter(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 5 || r.height < 5 || r.top < 0 || r.top >= 800) return false;
            const cx = r.x + r.width/2, cy = r.y + r.height/2;
            for (const fs of filledSlots) {
                if (Math.abs(cx - fs.x) < 30 && Math.abs(cy - fs.y) < 30) return false;
            }
            const style = getComputedStyle(el);
            if (parseFloat(style.opacity) < 0.4) return false;
            if (style.pointerEvents === 'none') return false;
            return true;
        }).map(el => {
            const r = el.getBoundingClientRect();
            return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)};
        });

        const unique = [];
        for (const s of emptySlots) {
            if (!unique.some(u => Math.abs(u.x - s.x) < 20 && Math.abs(u.y - s.y) < 20))
                unique.push(s);
        }
        return { pieces: pieces.slice(0, 12), emptySlots: unique.slice(0, 6) };
    }''') or {'pieces': [], 'emptySlots': []}


def solve(ctx: StepCtx) -> str | None:
    page = ctx.page
    boundary_y = ctx.boundary_y or 99999

    # Ensure page is scrolled to top — popup dismissal can leave it scrolled down
    page.evaluate('window.scrollTo(0, 0)')
    page.wait_for_timeout(200)

    # Discover pieces and slots (with retry — draggable elements may load late)
    slot_info = _find_slot_selector(page)
    piece_indices = _get_available_piece_indices(page)

    if not piece_indices:
        # Retry after short wait — elements may not have rendered yet
        log(f"step {ctx.step}: [drag_drop] no pieces on first scan, retrying...")
        page.wait_for_timeout(500)
        slot_info = _find_slot_selector(page)
        piece_indices = _get_available_piece_indices(page)

    use_locators = slot_info is not None and len(piece_indices) > 0
    total_empty = len(slot_info['emptyIndices']) if slot_info else 0

    if use_locators:
        log(f"step {ctx.step}: [drag_drop] locator mode: {len(piece_indices)} pieces, "
            f"{total_empty} empty slots, selector='{slot_info['selector']}'")
    else:
        # Fallback: coordinate scan
        scan = _scan_coords(page)
        if not scan['pieces'] or not scan['emptySlots']:
            # One more retry with longer wait
            page.wait_for_timeout(500)
            scan = _scan_coords(page)
        if not scan.get('pieces') or not scan.get('emptySlots'):
            log(f"step {ctx.step}: [drag_drop] no puzzle found after retries")
            return None
        total_empty = len(scan['emptySlots'])
        log(f"step {ctx.step}: [drag_drop] coord mode: {len(scan['pieces'])} pieces, "
            f"{total_empty} empty slots")

    # Drag loop: first piece → first empty slot → rescan → repeat
    successful_drags = 0
    consecutive_failures = 0
    max_rounds = total_empty + 6
    filled_positions = set()  # Track slots we've successfully filled (avoid re-dropping)

    for round_num in range(max_rounds):
        if _is_complete(page):
            log(f"step {ctx.step}: [drag_drop] complete after {successful_drags} drops")
            break

        code = extract_code(page)
        if code:
            return code

        page.wait_for_timeout(150)
        frac_before = get_progress_fraction(page)
        fill_before, _ = _check_fill_status(page)
        advanced = False

        if use_locators:
            # Rescan for fresh slot info + piece indices
            slot_info = _find_slot_selector(page)
            piece_indices = _get_available_piece_indices(page)

            if not slot_info or not slot_info.get('emptyIndices') or not piece_indices:
                log(f"step {ctx.step}: [drag_drop] no pieces/slots left (locator)")
                break

            slot_idx = slot_info['emptyIndices'][0]
            slot_loc = page.locator(slot_info['selector']).nth(slot_idx)

            for attempt in range(min(3, len(piece_indices))):
                piece_loc = page.locator('[draggable="true"]').nth(piece_indices[attempt])
                try:
                    piece_loc.drag_to(slot_loc, timeout=3000)
                except Exception as e:
                    log(f"step {ctx.step}: [drag_drop] drag_to error: {e}")
                    continue
                page.wait_for_timeout(100)
                frac_after = get_progress_fraction(page)
                fill_after, _ = _check_fill_status(page)
                if frac_after > frac_before or (fill_after > fill_before >= 0):
                    advanced = True
                    break
        else:
            # Coordinate fallback
            scan = _scan_coords(page)
            pieces = scan.get('pieces', [])
            empty_slots = scan.get('emptySlots', [])
            # Filter out slots we've already filled (Python-side tracking)
            empty_slots = [s for s in empty_slots
                           if (s['x'], s['y']) not in filled_positions]
            if not pieces or not empty_slots:
                log(f"step {ctx.step}: [drag_drop] no pieces/slots left (coord)")
                break

            slot = empty_slots[0]
            for attempt in range(min(3, len(pieces))):
                p = pieces[attempt]
                _mouse_drag(page, p['x'], p['y'], slot['x'], slot['y'])
                page.wait_for_timeout(50)
                frac_after = get_progress_fraction(page)
                fill_after, _ = _check_fill_status(page)
                if frac_after > frac_before or (fill_after > fill_before >= 0):
                    advanced = True
                    # Record this slot as filled so we never re-drop into it
                    filled_positions.add((slot['x'], slot['y']))
                    break

        if advanced:
            successful_drags += 1
            consecutive_failures = 0
            frac_now = get_progress_fraction(page)
            fill_now, _ = _check_fill_status(page)
            log(f"step {ctx.step}: [drag_drop] drop {successful_drags} OK "
                f"(fill {fill_before}->{fill_now}, frac {frac_before:.2f}->{frac_now:.2f})")
        else:
            consecutive_failures += 1
            log(f"step {ctx.step}: [drag_drop] round {round_num+1} failed "
                f"(consecutive={consecutive_failures})")
            if consecutive_failures >= 3:
                # If locator mode failed, try switching to coord mode
                if use_locators:
                    log(f"step {ctx.step}: [drag_drop] switching to coord fallback")
                    use_locators = False
                    consecutive_failures = 0
                else:
                    log(f"step {ctx.step}: [drag_drop] 3 consecutive failures, giving up")
                    break

    page.wait_for_timeout(200)

    # Click any completion button
    from agents.v4.helpers import click_button_by_text
    click_button_by_text(page, ['complete', 'done', 'finish', 'check'], boundary_y)
    page.wait_for_timeout(200)

    return extract_code(page) or wait_for_code_mutation(page, 2000)
