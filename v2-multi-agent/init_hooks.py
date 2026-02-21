"""
Human-Like Observation Hooks

These hooks are installed BEFORE page.goto() to observe the page like a human:
- MutationObserver: watches visible text changes ("watching the screen")
- State Change Watcher: tracks visible UI transitions ("observing the UI")
- Baseline snapshots: remembers what was already visible ("remembering what I saw")

Site-specific interception (WebSocket, Fetch, Shadow DOM) is in
agents/v4/exception_hooks.py and injected only for labeled exceptions.
See MISSION.md for the decision framework.
"""

INIT_SCRIPT = r'''
(() => {
    try {
        // === CODE BUS: Capture codes from visible text changes ===
        const RE = /\b[A-HJ-NP-Z2-9]{6}\b/g;
        window.__codeBus = [];
        window.__hookErrors = [];
        window.__lastActionTime = 0;

        // === Two-tier validation (populated from Python at session start) ===
        window.__CHARSET = '';
        window.__DECOY_CODES = new Set();
        window.__usedCodes = new Set();
        window.__baselineCodes = new Set();

        window.__isValidCodeHard = (code) => {
            if (!code || code.length !== 6 || code !== code.toUpperCase()) return false;
            if (window.__CHARSET) {
                for (const ch of code) { if (!window.__CHARSET.includes(ch)) return false; }
            }
            if (window.__usedCodes.has(code)) return false;
            return true;
        };

        window.__isValidCodeSoft = (code) => {
            if (!window.__isValidCodeHard(code)) return false;
            if (window.__DECOY_CODES.has(code)) return false;
            return true;
        };

        // Default alias — most agents use soft validation
        window.__isValidCode = window.__isValidCodeSoft;

        window.__addUsedCode = (code) => { window.__usedCodes.add(code); };

        // Scan visible text for all valid-looking codes at step start (baseline)
        window.__snapshotBaseline = () => {
            const re = new RegExp('\\b[A-HJ-NP-Z2-9]{6}\\b', 'g');
            const text = document.body?.innerText || '';
            const matches = text.match(re) || [];
            window.__baselineCodes = new Set(matches.filter(m => m === m.toUpperCase()));
        };

        // Code bus push function — shared with exception hooks
        const push = (src, txt, ts=Date.now()) => {
            try {
                if (!txt) return;
                const m = String(txt).match(RE);
                if (m) {
                    for (const c of m) {
                        if (window.__baselineCodes && window.__baselineCodes.has(c)) continue;
                        if (window.__isValidCodeSoft && !window.__isValidCodeSoft(c)) continue;
                        const isDupe = window.__codeBus.some(
                            x => x.c === c && x.src === src && Math.abs(x.t - ts) < 100
                        );
                        if (!isDupe) {
                            window.__codeBus.push({c, src, t: ts});
                            if (window.__codeBus.length > 200) window.__codeBus = window.__codeBus.slice(-120);
                        }
                    }
                }
            } catch (e) {
                window.__hookErrors.push({src, e: e.message});
            }
        };

        // Expose push for exception hooks to call
        window.__codeBus_push = push;

        // === MutationObserver: watch visible text for code appearance ===
        // This is "watching the screen" — human-like observation.
        try {
            window.__mutCodes = [];
            const scan = () => {
                try {
                    const txt = document.body?.innerText || '';
                    const m = txt.match(RE);
                    if (m) {
                        const ts = Date.now();
                        for (const c of m) {
                            if (window.__baselineCodes && window.__baselineCodes.has(c)) continue;
                            if (window.__isValidCodeSoft && !window.__isValidCodeSoft(c)) continue;
                            const isDupe = window.__mutCodes.some(
                                x => x.c === c && Math.abs(x.t - ts) < 500
                            );
                            if (!isDupe) {
                                window.__mutCodes.push({c, t: ts});
                                if (window.__mutCodes.length > 200) window.__mutCodes = window.__mutCodes.slice(-120);
                            }
                        }
                    }
                } catch {}
            };
            if (document.body) scan();
            const observer = new MutationObserver(scan);
            if (document.documentElement) {
                observer.observe(document.documentElement,
                    {subtree: true, childList: true, characterData: true});
            }
            document.addEventListener('DOMContentLoaded', () => {
                scan();
                observer.observe(document.documentElement,
                    {subtree: true, childList: true, characterData: true});
            });
        } catch (e) { window.__hookErrors.push({hook: 'mutation', e: e.message}); }

        // === Track opened windows ===
        try {
            window.__openedWindows = [];
            const origOpen = window.open.bind(window);
            window.open = function(...args) {
                const w = origOpen.apply(window, args);
                if (w) window.__openedWindows.push(w);
                return w;
            };
        } catch (e) { window.__hookErrors.push({hook: 'window', e: e.message}); }

        // === Mark action time (called from Python before clicking) ===
        window.__markAction = () => {
            window.__lastActionTime = Date.now();
        };

        // === Get codes that appeared after last action ===
        window.__getRecentCodes = (sinceTs) => {
            const threshold = sinceTs || window.__lastActionTime || 0;
            const busCodes = (window.__codeBus || []).filter(x => x.t > threshold);
            const mutCodes = (window.__mutCodes || []).filter(x => x.t > threshold);
            return [...busCodes, ...mutCodes];
        };

        // === Get all captured codes ===
        window.__getAllCodes = () => {
            return {
                bus: window.__codeBus || [],
                mut: window.__mutCodes || [],
                errors: window.__hookErrors || []
            };
        };

        // === Clear captured codes (call at start of each step) ===
        window.__clearCodes = () => {
            window.__codeBus = [];
            window.__mutCodes = [];
            window.__lastActionTime = Date.now();
            if (window.__snapshotBaseline) window.__snapshotBaseline();
        };

        // === Element State Change Watcher ===
        // Watches visible UI transitions: disabled→enabled, hidden→visible, etc.
        // This is "observing the UI" — human-like observation of visual changes.
        try {
            window.__stateChanges = [];
            let _prevByEl = new WeakMap();
            let _lastEventTime = new WeakMap();
            let _pendingRecheck = false;

            function _vibrancy(colorStr) {
                const m = (colorStr || '').match(/[\d.]+/g);
                if (!m || m.length < 3) return 0;
                const r = +m[0]/255, g = +m[1]/255, b = +m[2]/255;
                const max = Math.max(r,g,b), min = Math.min(r,g,b);
                const l = (max+min)/2;
                if (max === min) return 0;
                const d = max-min;
                const s = l > 0.5 ? d/(2-max-min) : d/(max+min);
                return s * (1 - Math.abs(2*l - 1));
            }

            function _colorCat(colorStr) {
                const m = (colorStr || '').match(/[\d.]+/g);
                if (!m || m.length < 3) return 'none';
                const r = +m[0], g = +m[1], b = +m[2];
                const a = m.length >= 4 ? (+m[3] > 1 ? +m[3]/255 : +m[3]) : 1;
                if (a < 0.1) return 'none';
                const max = Math.max(r,g,b), min = Math.min(r,g,b);
                const l = (max+min)/2;
                const d = max-min;
                if (d < 30 || l < 30 || l > 240) return 'grey';
                if (g > r * 1.3 && g > b * 1.3) return 'green';
                if (r > g * 1.3 && r > b * 1.3) return 'red';
                if (b > r * 1.3 && b > g * 1.3) return 'blue';
                if (r > 180 && g > 140 && b < 100) return 'yellow';
                return 'other';
            }

            function _snapState(el) {
                const cs = getComputedStyle(el);
                let txt = '';
                if (el.childElementCount === 0) {
                    txt = (el.textContent || '').trim().substring(0, 60);
                } else {
                    for (const n of el.childNodes) {
                        if (n.nodeType === 3) txt += n.textContent;
                    }
                    txt = txt.trim().substring(0, 60);
                    if (!txt && el.textContent.length < 80) {
                        txt = el.textContent.trim().substring(0, 60);
                    }
                }
                return {
                    disabled: el.disabled || el.getAttribute('aria-disabled') === 'true',
                    hidden: cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0',
                    opacity: parseFloat(cs.opacity),
                    pointerEvents: cs.pointerEvents,
                    cursor: cs.cursor,
                    bgVibrancy: _vibrancy(cs.backgroundColor),
                    fgVibrancy: _vibrancy(cs.color),
                    ariaHidden: el.getAttribute('aria-hidden') === 'true',
                    bgColor: _colorCat(cs.backgroundColor),
                    fgColor: _colorCat(cs.color),
                    borderColor: _colorCat(cs.borderColor),
                    text: txt,
                };
            }

            function _pushEvent(el, changes) {
                const now = Date.now();
                const lastTime = _lastEventTime.get(el) || 0;
                if (now - lastTime < 200) return;
                _lastEventTime.set(el, now);
                const r = el.getBoundingClientRect();
                window.__stateChanges.push({
                    changes: changes, t: now,
                    tag: el.tagName.toLowerCase(),
                    text: (el.textContent || '').trim().substring(0, 40),
                    bid: el.getAttribute('data-bid'),
                    x: Math.round(r.x + r.width/2),
                    y: Math.round(r.y + r.height/2),
                    w: Math.round(r.width), h: Math.round(r.height),
                });
                if (window.__stateChanges.length > 100)
                    window.__stateChanges = window.__stateChanges.slice(-60);
            }

            function _checkTransitions() {
                const sels = 'button, input, select, textarea, [role="button"], ' +
                    '[tabindex], [draggable="true"], [onclick], canvas, [data-bid]';
                const vh = window.innerHeight;

                let count = 0;
                for (const el of document.querySelectorAll(sels)) {
                    if (count >= 200) break;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) continue;
                    if (r.bottom < -500 || r.top > vh + 500) continue;
                    count++;

                    const curr = _snapState(el);
                    const prev = _prevByEl.get(el);

                    if (prev) {
                        const changes = [];
                        if (prev.disabled && !curr.disabled) changes.push('enabled');
                        if (prev.hidden && !curr.hidden) changes.push('appeared');
                        if (prev.opacity < 0.5 && curr.opacity >= 0.8) changes.push('activated');
                        if (prev.pointerEvents === 'none' && curr.pointerEvents !== 'none')
                            changes.push('became_clickable');
                        if (prev.cursor !== 'pointer' && curr.cursor === 'pointer')
                            changes.push('became_interactive');
                        if (prev.ariaHidden && !curr.ariaHidden) changes.push('revealed');
                        const bgDelta = curr.bgVibrancy - prev.bgVibrancy;
                        const fgDelta = curr.fgVibrancy - prev.fgVibrancy;
                        if (bgDelta > 0.15 || fgDelta > 0.15) changes.push('became_vibrant');

                        for (const [prop, label] of [['bgColor','bg'],['borderColor','border']]) {
                            const pc = prev[prop], cc = curr[prop];
                            if (pc !== cc && cc !== 'none' && pc !== 'none') {
                                if (cc === 'green' && !changes.includes('turned_green')) changes.push('turned_green');
                                else if (cc === 'red' && !changes.includes('turned_red')) changes.push('turned_red');
                                else if (cc === 'grey' && pc !== 'grey' && !changes.includes('turned_grey')) changes.push('turned_grey');
                            }
                        }

                        if (prev.text && curr.text && prev.text !== curr.text) {
                            changes.push('text_changed');
                        }

                        if (changes.length > 0) _pushEvent(el, changes);
                    }

                    _prevByEl.set(el, curr);
                }
            }

            const stateObserver = new MutationObserver((mutations) => {
                for (const m of mutations) {
                    if (m.type === 'childList' && m.addedNodes.length > 0) {
                        for (const node of m.addedNodes) {
                            if (node.nodeType !== 1) continue;
                            const sels2 = 'button, input, select, textarea, ' +
                                '[role="button"], [tabindex], [data-bid], [onclick], canvas';
                            const interactives = node.matches && node.matches(sels2)
                                ? [node]
                                : [...(node.querySelectorAll ? node.querySelectorAll(sels2) : [])];
                            for (const el of interactives) {
                                const r = el.getBoundingClientRect();
                                if (r.width === 0 && r.height === 0) continue;
                                _pushEvent(el, ['new_element']);
                            }
                        }
                    }
                    if (m.type === 'attributes') {
                        _checkTransitions();
                        if (!_pendingRecheck) {
                            _pendingRecheck = true;
                            setTimeout(() => {
                                _pendingRecheck = false;
                                _checkTransitions();
                            }, 50);
                        }
                    }
                }
            });

            if (document.body || document.documentElement) {
                stateObserver.observe(document.body || document.documentElement, {
                    childList: true, subtree: true,
                    attributes: true,
                    attributeFilter: ['disabled', 'aria-disabled', 'aria-hidden', 'class', 'style', 'hidden'],
                });
            }

            window.__stateWatchInterval = setInterval(_checkTransitions, 300);

            window.__drainStateChanges = () => {
                const changes = window.__stateChanges.slice();
                window.__stateChanges = [];
                return changes;
            };

            window.__peekStateChanges = () => {
                return window.__stateChanges.slice();
            };

            window.__resetStateWatch = () => {
                window.__stateChanges = [];
                _prevByEl = new WeakMap();
                _lastEventTime = new WeakMap();
                _checkTransitions();
            };

        } catch (e) { window.__hookErrors.push({hook: 'state_watch', e: e.message}); }

        window.__hooksInstalled = true;
    } catch (e) {
        window.__hooksInstalled = false;
        window.__hookError = e.message;
    }
})();
'''


def get_init_script() -> str:
    """Return the init script for installation via page.add_init_script()."""
    return INIT_SCRIPT
