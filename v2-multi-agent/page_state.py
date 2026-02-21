"""Page state inspection utilities."""

import re


def get_current_step(page) -> int | None:
    m = re.search(r'step(\d+)', page.url)
    return int(m.group(1)) if m else None


def get_version(page) -> int | None:
    m = re.search(r'version=(\d+)', page.url)
    return int(m.group(1)) if m else None


def get_body_text(page, limit: int = 500) -> str:
    try:
        return page.inner_text('body')[:limit]
    except Exception:
        return ""


def get_challenge_text(page, limit: int = 500) -> str:
    """Get page text excluding popups, decoy buttons, and filler sections.

    Used for challenge-type detection where decoy text causes false matches.
    """
    try:
        return page.evaluate('''(limit) => {
            // Collect elements to exclude: fixed overlays (popups) and decoy buttons
            const exclude = new Set();
            document.querySelectorAll('*').forEach(el => {
                const s = window.getComputedStyle(el);
                // Fixed-position overlays with high z-index = popups
                if (s.position === 'fixed') {
                    const z = parseInt(s.zIndex) || 0;
                    if (z >= 9990) {
                        exclude.add(el);
                        el.querySelectorAll('*').forEach(c => exclude.add(c));
                    }
                }
            });
            // Known decoy button labels
            const decoyRe = /^(Next|Continue|Proceed|Advance|Click Here|Move On|Keep Going|Go Forward|Next Step|Next Page|Continue Journey|Continue Reading|Load|Try This|New Button|Proceed Forward)$/i;
            document.querySelectorAll('button, [role="button"]').forEach(el => {
                const t = (el.textContent || '').trim();
                if (decoyRe.test(t)) {
                    exclude.add(el);
                    el.querySelectorAll('*').forEach(c => exclude.add(c));
                }
            });
            // Walk text nodes, skip excluded subtrees and filler sections
            let text = '';
            const walker = document.createTreeWalker(
                document.body, NodeFilter.SHOW_TEXT, {
                    acceptNode: n => {
                        if (exclude.has(n.parentElement)) return NodeFilter.FILTER_REJECT;
                        let p = n.parentElement;
                        while (p && p !== document.body) {
                            if (exclude.has(p)) return NodeFilter.FILTER_REJECT;
                            p = p.parentElement;
                        }
                        return NodeFilter.FILTER_ACCEPT;
                    }
                }
            );
            while (walker.nextNode()) {
                const val = walker.currentNode.textContent.trim();
                if (val) {
                    text += val + ' ';
                    if (text.length > limit) break;
                }
            }
            // Strip filler sections (Section 1, Section 2, etc.)
            text = text.split(/\\bSection\\s+\\d+\\b/i)[0];
            return text.substring(0, limit).trim();
        }''', limit)
    except Exception:
        return get_body_text(page, limit)


def has_popups(page) -> bool:
    return page.evaluate(r'''() => {
        let count = 0;
        document.querySelectorAll('*').forEach(el => {
            const s = window.getComputedStyle(el);
            if (s.position === 'fixed') {
                const z = parseInt(s.zIndex) || 0;
                if (z >= 9995 && z <= 9999) count++;
            }
        });
        return count > 0;
    }''')


def has_radio_buttons(page) -> bool:
    return page.evaluate(r'''() => document.querySelectorAll('input[type="radio"]').length > 0''')


def has_checkboxes(page) -> bool:
    return page.evaluate(r'''() => {
        const cbs = document.querySelectorAll('input[type="checkbox"]');
        for (const cb of cbs) { if (!cb.checked) return true; }
        return false;
    }''')


def close_extra_tabs(page):
    for extra in page.context.pages:
        if extra != page:
            try:
                extra.close()
            except Exception:
                pass
    try:
        page.evaluate(r'''() => {
            (window.__openedWindows || []).forEach(w => { try { w.close(); } catch(e) {} });
            window.__openedWindows = [];
        }''')
    except Exception:
        pass
