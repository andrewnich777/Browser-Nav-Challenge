"""CDP-based capabilities for V4 agents.

Only `find_codes_in_pierced_dom()` is used in production — it serves the
shadow_dom and recursive_iframe labeled exceptions (see MISSION.md).

DEPRECATED (no production callers after Session 22b):
- get_elements_with_listeners() — replaced by get_accessible_elements()
- find_hover_targets_via_css() — replaced by find_hover_targets_by_hovering()

All functions fail gracefully — return None/empty on CDP errors.
"""

from log import log

_CDP_SESSIONS = {}  # page id -> CDPSession


def _get_cdp(page):
    """Get or create a CDP session. Returns None on failure."""
    pid = id(page)
    if pid in _CDP_SESSIONS:
        try:
            # Quick health check — send a simple command
            _CDP_SESSIONS[pid].send("Runtime.evaluate",
                                    {"expression": "1", "returnByValue": True})
            return _CDP_SESSIONS[pid]
        except Exception:
            # Session stale, recreate
            try:
                _CDP_SESSIONS[pid].detach()
            except Exception:
                pass
            del _CDP_SESSIONS[pid]

    try:
        session = page.context.new_cdp_session(page)
        session.send("DOM.enable")
        _CDP_SESSIONS[pid] = session
        return session
    except Exception as e:
        log(f"[cdp] session creation failed: {e}")
        return None


def cleanup_cdp(page):
    """Clean up CDP session for a page."""
    pid = id(page)
    if pid in _CDP_SESSIONS:
        try:
            _CDP_SESSIONS[pid].detach()
        except Exception:
            pass
        del _CDP_SESSIONS[pid]


def get_elements_with_listeners(page, boundary_y: int = 99999,
                                event_types: list[str] | None = None
                                ) -> list[dict]:
    """DEPRECATED: Use get_accessible_elements() + cursor:pointer instead.

    Find elements with specific event listeners via CDP.
    Site-specific: CDP listener detection doesn't transfer to arbitrary websites.

    Returns list of {x, y, w, h, text, tag, listeners: [type, ...]}
    for elements that have any of the specified event types attached.
    """
    if event_types is None:
        event_types = ['click', 'mousedown', 'mouseup', 'mouseenter',
                       'mouseover', 'dragstart', 'pointerdown']
    target_set = set(event_types)

    cdp = _get_cdp(page)
    if not cdp:
        return []

    try:
        # Step 1: Get all visible elements as RemoteObject array
        result = cdp.send("Runtime.evaluate", {
            "expression": f"""(() => {{
                return [...document.querySelectorAll('*')].filter(el => {{
                    const r = el.getBoundingClientRect();
                    return r.width > 10 && r.height > 10
                        && r.top + r.height/2 < {boundary_y};
                }});
            }})()""",
            "returnByValue": False,
        })

        if result.get("exceptionDetails"):
            return []

        array_id = result.get("result", {}).get("objectId")
        if not array_id:
            return []

        # Step 2: Get array properties (each element reference)
        props = cdp.send("Runtime.getProperties", {
            "objectId": array_id,
            "ownProperties": True,
        })

        elements = []
        for prop in props.get("result", []):
            val = prop.get("value", {})
            obj_id = val.get("objectId")
            if not obj_id or val.get("type") != "object":
                continue

            try:
                # Step 3: Get event listeners for this element
                listeners_result = cdp.send("DOMDebugger.getEventListeners", {
                    "objectId": obj_id,
                })
                listener_types = set()
                for listener in listeners_result.get("listeners", []):
                    lt = listener.get("type", "")
                    if lt in target_set:
                        listener_types.add(lt)

                if listener_types:
                    # Get element details
                    details = cdp.send("Runtime.callFunctionOn", {
                        "objectId": obj_id,
                        "functionDeclaration": """function() {
                            const r = this.getBoundingClientRect();
                            return {
                                x: Math.round(r.x + r.width/2),
                                y: Math.round(r.y + r.height/2),
                                w: Math.round(r.width),
                                h: Math.round(r.height),
                                text: (this.textContent || '').trim().substring(0, 80),
                                tag: this.tagName,
                            };
                        }""",
                        "returnByValue": True,
                    })
                    info = details.get("result", {}).get("value", {})
                    if info:
                        info["listeners"] = sorted(listener_types)
                        elements.append(info)
            except Exception:
                continue

        # Release the array reference
        try:
            cdp.send("Runtime.releaseObject", {"objectId": array_id})
        except Exception:
            pass

        return elements

    except Exception as e:
        log(f"[cdp] get_elements_with_listeners error: {e}")
        return []


def find_codes_in_pierced_dom(page) -> str | None:
    """Search for 6-char codes across shadow roots and all iframes.

    Uses JS shadow root traversal + Playwright frame API.
    Returns first valid code found, or None.
    """
    skip_words = ('SUBMIT', 'SCROLL', 'REVEAL', 'CANCEL', 'BUTTON',
                  'HIDDEN', 'CANVAS', 'SHADOW', 'RANDOM', 'PUNYYR',
                  'SEARCH', 'ENABLE', 'DELETE', 'UPDATE', 'SELECT',
                  'RETURN', 'STRING')

    # Method 1: Search main page + shadow roots (JS)
    try:
        code = page.evaluate('''() => {
            const re = /\\b[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}\\b/g;
            const skip = new Set(''' + repr(list(skip_words)) + ''');

            function searchNode(root) {
                // Check text nodes
                const walker = document.createTreeWalker(
                    root, NodeFilter.SHOW_TEXT, null);
                let node;
                while (node = walker.nextNode()) {
                    const matches = node.textContent.match(re);
                    if (matches) {
                        for (const m of matches) {
                            if (!skip.has(m)) return m;
                        }
                    }
                }
                // Check attributes
                for (const el of root.querySelectorAll('*')) {
                    for (const attr of el.attributes || []) {
                        if (attr.name.startsWith('data-') || attr.name === 'title') {
                            const matches = attr.value.match(re);
                            if (matches) {
                                for (const m of matches) {
                                    if (!skip.has(m)) return m;
                                }
                            }
                        }
                    }
                    // Recurse into shadow roots
                    if (el.shadowRoot) {
                        const code = searchNode(el.shadowRoot);
                        if (code) return code;
                    }
                }
                return null;
            }
            return searchNode(document);
        }''')
        if code:
            return code
    except Exception:
        pass

    # Method 2: Search all child frames
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            code = frame.evaluate('''() => {
                const re = /\\b[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}\\b/g;
                const skip = new Set(''' + repr(list(skip_words)) + ''');
                const text = document.body?.innerText || '';
                const matches = text.match(re);
                if (matches) {
                    for (const m of matches) {
                        if (!skip.has(m)) return m;
                    }
                }
                return null;
            }''')
            if code:
                return code
        except Exception:
            continue

    return None


def find_hover_targets_via_css(page, boundary_y: int = 99999) -> list[dict]:
    """DEPRECATED: Use find_hover_targets_by_hovering() instead.

    Find elements that change on :hover using CSS.forcePseudoState.
    Site-specific: forcing CSS pseudo-state isn't how humans interact with pages.

    Returns list of {x, y, w, h, text, tag, changed_props: [str, ...]}
    sorted by number of changed properties (most changes first).
    """
    cdp = _get_cdp(page)
    if not cdp:
        return []

    try:
        # Enable CSS domain for forcing pseudo states
        cdp.send("CSS.enable")

        # Get candidate elements (visible, within boundary, reasonable size)
        result = cdp.send("Runtime.evaluate", {
            "expression": f"""(() => {{
                const candidates = [];
                const all = document.querySelectorAll('div, span, section, p, label, [role="button"]');
                for (const el of all) {{
                    const r = el.getBoundingClientRect();
                    if (r.width < 20 || r.height < 10) continue;
                    if (r.width > 600 || r.height > 600) continue;
                    if (r.top + r.height/2 > {boundary_y}) continue;
                    if (r.top <= 0) continue;
                    const t = (el.textContent || '').trim().toLowerCase();
                    if (t.includes('hover') || t.includes('mouse over')
                        || getComputedStyle(el).cursor === 'pointer'
                        || getComputedStyle(el).transition !== 'all 0s ease 0s') {{
                        candidates.push(el);
                    }}
                }}
                return candidates;
            }})()""",
            "returnByValue": False,
        })

        if result.get("exceptionDetails"):
            return []

        array_id = result.get("result", {}).get("objectId")
        if not array_id:
            return []

        props = cdp.send("Runtime.getProperties", {
            "objectId": array_id,
            "ownProperties": True,
        })

        hover_targets = []
        check_props = ['backgroundColor', 'color', 'opacity', 'transform',
                        'visibility', 'borderColor', 'boxShadow']

        for prop in props.get("result", []):
            val = prop.get("value", {})
            obj_id = val.get("objectId")
            if not obj_id or val.get("type") != "object":
                continue

            try:
                # Get DOM node for this element
                node_result = cdp.send("DOM.describeNode", {
                    "objectId": obj_id,
                })
                node_id = node_result.get("node", {}).get("nodeId")
                if not node_id:
                    continue

                # Read styles BEFORE hover
                before_styles = cdp.send("Runtime.callFunctionOn", {
                    "objectId": obj_id,
                    "functionDeclaration": """function() {
                        const s = getComputedStyle(this);
                        return {
                            backgroundColor: s.backgroundColor,
                            color: s.color,
                            opacity: s.opacity,
                            transform: s.transform,
                            visibility: s.visibility,
                            borderColor: s.borderColor,
                            boxShadow: s.boxShadow,
                        };
                    }""",
                    "returnByValue": True,
                })
                before = before_styles.get("result", {}).get("value", {})

                # Force :hover pseudo-state
                cdp.send("CSS.forcePseudoState", {
                    "nodeId": node_id,
                    "forcedPseudoClasses": ["hover"],
                })

                # Read styles AFTER hover
                after_styles = cdp.send("Runtime.callFunctionOn", {
                    "objectId": obj_id,
                    "functionDeclaration": """function() {
                        const s = getComputedStyle(this);
                        return {
                            backgroundColor: s.backgroundColor,
                            color: s.color,
                            opacity: s.opacity,
                            transform: s.transform,
                            visibility: s.visibility,
                            borderColor: s.borderColor,
                            boxShadow: s.boxShadow,
                        };
                    }""",
                    "returnByValue": True,
                })
                after = after_styles.get("result", {}).get("value", {})

                # Remove forced hover
                cdp.send("CSS.forcePseudoState", {
                    "nodeId": node_id,
                    "forcedPseudoClasses": [],
                })

                # Compare
                changed = [p for p in check_props
                           if before.get(p) != after.get(p)]

                if changed:
                    details = cdp.send("Runtime.callFunctionOn", {
                        "objectId": obj_id,
                        "functionDeclaration": """function() {
                            const r = this.getBoundingClientRect();
                            return {
                                x: Math.round(r.x + r.width/2),
                                y: Math.round(r.y + r.height/2),
                                w: Math.round(r.width),
                                h: Math.round(r.height),
                                text: (this.textContent || '').trim().substring(0, 60),
                                tag: this.tagName,
                            };
                        }""",
                        "returnByValue": True,
                    })
                    info = details.get("result", {}).get("value", {})
                    if info:
                        info["changed_props"] = changed
                        hover_targets.append(info)
            except Exception:
                continue

        try:
            cdp.send("Runtime.releaseObject", {"objectId": array_id})
        except Exception:
            pass

        # Sort by number of changed props (most responsive first)
        hover_targets.sort(key=lambda t: len(t.get("changed_props", [])), reverse=True)
        return hover_targets

    except Exception as e:
        log(f"[cdp] find_hover_targets_via_css error: {e}")
        return []
