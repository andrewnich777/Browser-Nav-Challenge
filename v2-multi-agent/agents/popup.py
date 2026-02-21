"""Popup dismissal agent — clicks dismiss buttons on overlay popups.

Popup types (discovered via diagnostic 2026-02-04):
- z-9999: Cookie Consent — buttons "Accept" / "Decline"
- z-9998: Prize/Newsletter — red X (trap) + "Close"
- z-9997: Overlay Notice — "Close" or "Close (Fake)" + "Dismiss"
- z-9996: Please Select an Option — radio selection + "Submit & Continue"

Strategy: DETECT in JS, CLICK with Playwright or el.click().
- DETECT_POPUPS_JS finds the highest-priority popup button and returns coords.
- dismiss_all_popups() clicks ONE popup per call using page.mouse.click()
  with el.click() JS fallback for hydration lag.
- Caller loops until dismiss_all_popups() returns 0.
NO DOM modifications — only clicks buttons to avoid white-screening React.
"""

from agents.base import Agent
from log import log_stage

# ── JS to find and FOCUS the Correct Choice radio in the "Please Select" modal ──
# Bypasses coordinate hit-testing entirely — targets the exact Radix radio element.
# Returns true if a radio was found and focused, false otherwise.

FOCUS_CORRECT_RADIO_JS = r'''() => {
    const fixedEls = document.querySelectorAll('.fixed, .inset-0, [style*="position: fixed"], [style*="position:fixed"]');
    for (const el of fixedEls) {
        const z = parseInt(el.style.zIndex || getComputedStyle(el).zIndex) || 0;
        if (z < 9995 || z > 9999) continue;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
        const elText = el.textContent || '';
        if (!elText.includes('Please Select') && !elText.includes('Select an Option')) continue;

        // Scroll modal to reveal all options
        const scrollable = el.querySelector('[class*="overflow"], [class*="scroll"]') || el;
        if (scrollable.scrollHeight > scrollable.clientHeight) {
            scrollable.scrollTop = scrollable.scrollHeight;
        }

        for (const label of el.querySelectorAll('label')) {
            if (/Correct\s*Choice/i.test(label.textContent) && !/incorrect/i.test(label.textContent)) {
                const parent = label.parentElement;
                const radio = parent ? parent.querySelector('button[role="radio"]') : null;
                if (radio && radio.getAttribute('data-state') !== 'checked') {
                    radio.scrollIntoView({ block: 'center' });
                    radio.focus();
                    return { focused: true, state: radio.getAttribute('data-state') };
                }
                // Fallback: try input[type="radio"] inside or near the label
                const inputRadio = parent ? parent.querySelector('input[type="radio"]') : null;
                if (inputRadio && !inputRadio.checked) {
                    inputRadio.scrollIntoView({ block: 'center' });
                    inputRadio.focus();
                    return { focused: true, state: 'input-radio' };
                }
                // Fallback: click the label itself (browser forwards to associated control)
                label.scrollIntoView({ block: 'center' });
                const r = label.getBoundingClientRect();
                return { focused: false, labelX: Math.round(r.x + r.width/2),
                         labelY: Math.round(r.y + r.height/2), state: 'label-fallback' };
            }
        }
        return { focused: false, state: 'no-label-found' };
    }
    return { focused: false, state: 'no-popup' };
}'''

# ── JS auto-dismiss interval: handles ALL popups (z 9996-9999) in real-time ──
# Runs every 400ms inside the page, so popups are dismissed even while Python
# is blocked on a vision API call (~8s). Uses full pointer event sequence
# for React 18 compatibility. Handles the "Please Select" radio modal too.
POPUP_AUTO_DISMISS_SETUP_JS = r'''() => {
    if (window.__autoDismissInterval) return 'already_running';

    function fullClick(el) {
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return;
        el.click();
    }

    function pressSpace(el) {
        el.focus();
        el.click();
    }

    window.__autoDismissInterval = setInterval(() => {
        const fixedEls = document.querySelectorAll('.fixed, .inset-0, [style*="position: fixed"], [style*="position:fixed"]');
        // Sort by z-index descending so we handle top-most first
        const sorted = [...fixedEls].sort((a, b) => {
            const za = parseInt(a.style.zIndex || getComputedStyle(a).zIndex) || 0;
            const zb = parseInt(b.style.zIndex || getComputedStyle(b).zIndex) || 0;
            return zb - za;
        });

        for (const el of sorted) {
            const z = parseInt(el.style.zIndex || getComputedStyle(el).zIndex) || 0;
            if (z < 9996 || z > 9999) continue;

            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;

            const elText = el.textContent || '';

            // === z-9996: "Please Select an Option" modal ===
            // Guard: must be a large overlay (>40% of viewport) — not a small challenge widget
            if (elText.includes('Please Select') || elText.includes('Select an Option')) {
                const elRect = el.getBoundingClientRect();
                if (elRect.width < window.innerWidth * 0.4 || elRect.height < window.innerHeight * 0.3) continue;
                // Scroll modal to reveal all options
                const scrollable = el.querySelector('[class*="overflow"], [class*="scroll"]') || el;
                if (scrollable.scrollHeight > scrollable.clientHeight) {
                    scrollable.scrollTop = scrollable.scrollHeight;
                }

                // Find "Correct Choice" radio
                for (const label of el.querySelectorAll('label')) {
                    if (/Correct\s*Choice/i.test(label.textContent) && !/incorrect/i.test(label.textContent)) {
                        const parent = label.parentElement;
                        const radio = parent ? parent.querySelector('button[role="radio"]') : null;
                        if (radio && radio.getAttribute('data-state') !== 'checked') {
                            // Select radio: focus + Space + fullClick
                            radio.scrollIntoView({ block: 'center' });
                            pressSpace(radio);
                            fullClick(radio);
                            return;  // One action per tick
                        }
                        break;
                    }
                }

                // If radio is checked, click Submit
                for (const btn of el.querySelectorAll('button')) {
                    const radio = el.querySelector('button[role="radio"][data-state="checked"]');
                    if (radio && /^Submit/i.test((btn.textContent || '').trim())) {
                        btn.scrollIntoView({ block: 'center' });
                        fullClick(btn);
                        return;  // One action per tick
                    }
                }
                continue;
            }

            // === z 9997-9999: Simple dismiss buttons ===
            let bestBtn = null;
            let bestPriority = 99;
            for (const btn of el.querySelectorAll('button')) {
                const btnText = (btn.textContent || '').trim();
                if (/fake/i.test(btnText) || !btnText) continue;
                const btnStyle = getComputedStyle(btn);
                if (btnStyle.display === 'none' || btnStyle.visibility === 'hidden' || btnStyle.opacity === '0') continue;

                let priority = 99;
                if (btnText === 'Dismiss') priority = 1;
                else if (btnText === 'Accept') priority = 2;
                else if (btnText === 'Close') priority = 3;
                else if (btnText === 'Decline') priority = 4;

                if (priority < bestPriority) { bestBtn = btn; bestPriority = priority; }
            }

            if (bestBtn) {
                fullClick(bestBtn);
                return;  // One popup per tick
            }
        }

        // === Orange blocker squares (any step) ===
        // These are small, positioned, orange-colored elements that block interaction.
        // Click them to dismiss — they move/disappear on click.
        const allEls = document.querySelectorAll('*');
        for (const el of allEls) {
            const rect = el.getBoundingClientRect();
            if (rect.width < 15 || rect.height < 15 || rect.width > 150 || rect.height > 150) continue;
            if (rect.top < 0 || rect.top > window.innerHeight) continue;
            if (Math.abs(rect.width - rect.height) > 15) continue;

            const style = getComputedStyle(el);
            const bg = style.backgroundColor || '';
            const pos = style.position;
            if (pos !== 'absolute' && pos !== 'fixed') continue;
            if (el.hasAttribute('draggable') || el.tagName === 'BUTTON' || el.tagName === 'INPUT') continue;

            const rgbMatch = bg.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
            if (rgbMatch) {
                const r = parseInt(rgbMatch[1]), g = parseInt(rgbMatch[2]), b = parseInt(rgbMatch[3]);
                if (r > 180 && g >= 50 && g <= 200 && b < 120) {
                    fullClick(el);
                    return;  // One action per tick
                }
            }
        }
    }, 400);

    window.__stopAutoDismiss = () => {
        clearInterval(window.__autoDismissInterval);
        window.__autoDismissInterval = null;
    };

    return 'started';
}'''

# ── Detection-only JS: finds popup targets, returns coords, does NOT click ──

DETECT_POPUPS_JS = r'''() => {
    const targets = [];
    const fixedEls = document.querySelectorAll('.fixed, .inset-0, [style*="position: fixed"], [style*="position:fixed"]');

    for (const el of fixedEls) {
        const z = parseInt(el.style.zIndex || getComputedStyle(el).zIndex) || 0;
        if (z < 9995 || z > 9999) continue;

        // Skip hidden/animating elements
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;

        const elText = el.textContent || '';

        // === "Please Select an Option" modal ===
        // Guard: must be a large overlay (>40% of viewport) — not a small challenge widget
        if (elText.includes('Please Select') || elText.includes('Select an Option')) {
            const elRect = el.getBoundingClientRect();
            if (elRect.width < window.innerWidth * 0.4 || elRect.height < window.innerHeight * 0.3) continue;
            // Scroll modal to reveal all options
            const scrollable = el.querySelector('[class*="overflow"], [class*="scroll"]') || el;
            if (scrollable.scrollHeight > scrollable.clientHeight) {
                scrollable.scrollTop = scrollable.scrollHeight;
            }

            // Check if radio is already checked (data-state="checked")
            let radioChecked = false;
            for (const label of el.querySelectorAll('label')) {
                if (/Correct\s*Choice/i.test(label.textContent) && !/incorrect/i.test(label.textContent)) {
                    const parent = label.parentElement;
                    const radio = parent ? parent.querySelector('button[role="radio"]') : null;
                    if (radio) {
                        if (radio.getAttribute('data-state') === 'checked') {
                            radioChecked = true;
                        } else {
                            // Scroll radio into view WITHIN the modal before measuring
                            radio.scrollIntoView({ block: 'center' });
                            const r = radio.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0 && r.y >= 0 && r.y < window.innerHeight) {
                                targets.push({type: 'radio', x: Math.round(r.x + r.width/2),
                                              y: Math.round(r.y + r.height/2), zIndex: z, text: 'Correct Choice radio'});
                            }
                        }
                    }
                    break;
                }
            }

            // If radio is checked, find Submit button
            if (radioChecked) {
                for (const btn of el.querySelectorAll('button')) {
                    if (/^Submit/i.test((btn.textContent || '').trim())) {
                        btn.scrollIntoView({ block: 'center' });
                        const r = btn.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0 && r.y >= 0 && r.y < window.innerHeight) {
                            targets.push({type: 'submit', x: Math.round(r.x + r.width/2),
                                          y: Math.round(r.y + r.height/2), zIndex: z, text: 'Submit'});
                        }
                        break;
                    }
                }
            }
            continue;
        }

        // === All other popups: Cookie Consent, Prize/Newsletter, Overlay Notice ===
        let bestBtn = null;
        let bestPriority = 99;
        for (const btn of el.querySelectorAll('button')) {
            const btnText = (btn.textContent || '').trim();
            if (/fake/i.test(btnText) || !btnText) continue;

            // Skip hidden buttons
            const btnStyle = getComputedStyle(btn);
            if (btnStyle.display === 'none' || btnStyle.visibility === 'hidden' || btnStyle.opacity === '0') continue;

            let priority = 99;
            if (btnText === 'Dismiss') priority = 1;
            else if (btnText === 'Accept') priority = 2;
            else if (btnText === 'Close') priority = 3;
            else if (btnText === 'Decline') priority = 4;

            if (priority < bestPriority) { bestBtn = btn; bestPriority = priority; }
        }

        if (bestBtn) {
            const r = bestBtn.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) {
                targets.push({type: 'dismiss_btn', x: Math.round(r.x + r.width/2),
                              y: Math.round(r.y + r.height/2), zIndex: z,
                              text: (bestBtn.textContent || '').trim()});
            }
        }
    }

    // Sort by z-index descending — top-most popup first
    return targets.sort((a, b) => b.zIndex - a.zIndex);
}'''


def _try_radio_focus_space(page) -> bool:
    """Focus the Correct Choice radio via JS + Space. Returns True if state changed."""
    try:
        result = page.evaluate(FOCUS_CORRECT_RADIO_JS)
    except Exception:
        return False

    if not result:
        return False

    if result.get('focused'):
        # Radio element was focused directly — press Space to select it
        page.wait_for_timeout(50)
        page.keyboard.press('Space')
        page.wait_for_timeout(200)
        return True

    if result.get('labelX'):
        # Fallback: click the label (browser forwards click to associated control)
        log_stage("popup", f"  radio focus failed, clicking label at ({result['labelX']},{result['labelY']})")
        page.mouse.click(result['labelX'], result['labelY'])
        page.wait_for_timeout(200)
        return True

    return False


# JS to find and click the Submit button in the "Please Select" modal
# Uses full pointer event sequence dispatched directly on the element.
CLICK_SUBMIT_JS = r'''() => {
    function fullClick(el) {
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return false;
        el.click();
        return true;
    }

    const fixedEls = document.querySelectorAll('.fixed, .inset-0, [style*="position: fixed"], [style*="position:fixed"]');
    for (const el of fixedEls) {
        const z = parseInt(el.style.zIndex || getComputedStyle(el).zIndex) || 0;
        if (z < 9995 || z > 9999) continue;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
        const elText = el.textContent || '';
        if (!elText.includes('Please Select') && !elText.includes('Select an Option')) continue;

        for (const btn of el.querySelectorAll('button')) {
            if (/^Submit/i.test((btn.textContent || '').trim())) {
                btn.scrollIntoView({ block: 'center' });
                return fullClick(btn);
            }
        }
    }
    return false;
}'''


def dismiss_all_popups(page) -> int:
    """Detect popups via JS, dismiss the TOP-MOST one with click-verify-retry.

    Handles hydration lag: React may not have attached event listeners yet when
    the popup first renders. We click, wait, then RE-DETECT to verify the click
    actually worked. If the same target is still there, we get fresh coordinates
    and retry (up to 5 attempts with 200ms gaps for hydration/animation).

    For radio buttons: uses JS focus + Space (bypasses coordinate hit-testing)
    because the small Radix radio button is often covered by its label/wrapper.

    Returns 1 if a popup action was taken (dismissed or radio selected),
    0 if no popups found or all retries failed.
    """
    try:
        targets = page.evaluate(DETECT_POPUPS_JS)
    except Exception:
        return 0

    if not targets:
        return 0

    target = targets[0]  # ONLY handle the top-most target
    log_stage("popup", f"dismiss: {target['type']} '{target.get('text','')}' "
              f"at ({target['x']},{target['y']}) z={target.get('zIndex','?')}")

    for attempt in range(5):
        try:
            if target['type'] == 'radio':
                # Strategy 1 (primary): Focus radio via JS + Space
                # Bypasses coordinate issues — targets the exact Radix element
                if not _try_radio_focus_space(page):
                    # Strategy 2 (fallback): coordinate click + Space
                    page.mouse.click(target['x'], target['y'])
                    page.wait_for_timeout(50)
                    page.keyboard.press('Space')
                    page.wait_for_timeout(200)
            elif target['type'] == 'submit':
                # Submit button in "Please Select" modal — use JS full-click
                # to bypass coordinate issues (button may be partially off-screen)
                clicked = page.evaluate(CLICK_SUBMIT_JS)
                if not clicked:
                    # Fallback: coordinate click
                    page.mouse.click(target['x'], target['y'])
                page.wait_for_timeout(200)
            else:
                page.mouse.click(target['x'], target['y'])
                page.wait_for_timeout(200)
        except Exception as e:
            log_stage("popup", f"dismiss click error: {e}")
            return 0

        # ── Verify: re-detect to confirm the click actually worked ──
        try:
            new_targets = page.evaluate(DETECT_POPUPS_JS)
        except Exception:
            return 1  # Can't verify, assume it worked

        # All popups gone
        if not new_targets:
            return 1

        new_top = new_targets[0]

        # For radio clicks: success = top target changed to 'submit' OR radio gone
        if target['type'] == 'radio':
            if new_top['type'] == 'submit':
                return 1
            if new_top['type'] != 'radio':
                return 1
            # Radio still there — try different strategies on retries
            if attempt < 4:
                log_stage("popup", f"  retry {attempt+1}: '{target.get('text','')}' still present")
                target = new_top
                # On retry 2, also try clicking the label coordinates from detection
                if attempt >= 1 and target.get('x') and target.get('y'):
                    # Try clicking slightly above the radio (where label text usually is)
                    page.mouse.click(target['x'], max(10, target['y'] - 15))
                    page.wait_for_timeout(200)
            continue

        # For dismiss/submit: success = different type, zIndex, text, or moved >50px
        coords_moved = (abs(new_top.get('x', 0) - target.get('x', 0)) > 50 or
                        abs(new_top.get('y', 0) - target.get('y', 0)) > 50)
        if (new_top['type'] != target['type'] or
                new_top.get('zIndex') != target.get('zIndex') or
                new_top.get('text') != target.get('text') or
                coords_moved):
            return 1

        # Same target at same position — click didn't register (hydration lag)
        if attempt < 4:
            log_stage("popup", f"  retry {attempt+1}: '{target.get('text','')}' still present")
            target = new_top
            # On retry 2, try JS el.click() as fallback
            if attempt >= 1:
                try:
                    btn_text = target.get('text', '')
                    page.evaluate(f'''() => {{
                        const fixedEls = document.querySelectorAll('.fixed, .inset-0, [style*="position: fixed"]');
                        for (const el of fixedEls) {{
                            const z = parseInt(el.style.zIndex || getComputedStyle(el).zIndex) || 0;
                            if (z < 9995 || z > 9999) continue;
                            const style = window.getComputedStyle(el);
                            if (style.display === 'none' || style.visibility === 'hidden') continue;
                            for (const btn of el.querySelectorAll('button')) {{
                                if ((btn.textContent || '').trim() === '{btn_text}') {{
                                    btn.click();
                                    return;
                                }}
                            }}
                        }}
                    }}''')
                    page.wait_for_timeout(200)
                except Exception:
                    pass

    log_stage("popup", f"  FAILED after 5 attempts on '{target.get('text','')}'")
    return 0


# Click orange square blocker elements to dismiss them (no DOM mutation).
# The auto-dismiss interval also handles these, but this provides an
# explicit pre-action sweep when needed.
CLEAR_BLOCKERS_JS = r'''() => {
    let clicked = 0;
    const allEls = document.querySelectorAll('*');

    for (const el of allEls) {
        const rect = el.getBoundingClientRect();
        if (rect.width < 15 || rect.height < 15) continue;
        if (rect.width > 150 || rect.height > 150) continue;

        const style = getComputedStyle(el);
        const bg = style.backgroundColor || '';
        const pos = style.position;
        const tag = el.tagName;

        // Only target positioned elements (absolute/fixed)
        if (pos !== 'absolute' && pos !== 'fixed') continue;

        // Skip functional elements
        if (el.hasAttribute('draggable')) continue;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'FORM') continue;
        if (tag === 'BUTTON') continue;
        if (el.closest('form') || el.closest('button')) continue;

        // Must be roughly square-shaped
        if (Math.abs(rect.width - rect.height) > 15) continue;

        // Check for orange in solid bg OR gradient
        function isOrangeRGB(r, g, b) {
            return r > 180 && g >= 50 && g <= 200 && b < 120;
        }

        let isOrange = false;

        // Check solid backgroundColor
        const rgbMatch = bg.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
        if (rgbMatch) {
            isOrange = isOrangeRGB(parseInt(rgbMatch[1]), parseInt(rgbMatch[2]), parseInt(rgbMatch[3]));
        }

        // Check backgroundImage for gradients containing orange
        if (!isOrange) {
            const bgImg = style.backgroundImage || '';
            if (bgImg.includes('gradient')) {
                const rgbAll = [...bgImg.matchAll(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/g)];
                for (const m of rgbAll) {
                    if (isOrangeRGB(parseInt(m[1]), parseInt(m[2]), parseInt(m[3]))) {
                        isOrange = true; break;
                    }
                }
                if (!isOrange) {
                    const hexAll = [...bgImg.matchAll(/#([0-9a-fA-F]{6})/g)];
                    for (const m of hexAll) {
                        const hex = m[1];
                        const hr = parseInt(hex.slice(0,2), 16);
                        const hg = parseInt(hex.slice(2,4), 16);
                        const hb = parseInt(hex.slice(4,6), 16);
                        if (isOrangeRGB(hr, hg, hb)) { isOrange = true; break; }
                    }
                }
            }
        }

        if (!isOrange) continue;

        // Click to dismiss (no DOM mutation — the site handles the click)
        el.click();
        clicked++;
    }

    return clicked;
}'''


class PopupAgent(Agent):
    name = "popup"

    def run(self, page, step: int = 0, version: int = 0) -> bool:
        """Dismiss popups one-at-a-time using Playwright mouse clicks.

        Each call to dismiss_all_popups() handles ONE popup action then returns.
        Loop handles chained popups and multi-step modals (e.g. radio → submit).
        NO DOM modifications — only real mouse clicks to avoid white-screening React.
        """
        total = 0
        for _ in range(8):  # Max 8 actions (4 popups * 2 actions each max)
            dismissed = dismiss_all_popups(page)
            total += dismissed
            if not dismissed:
                break
        return total > 0
