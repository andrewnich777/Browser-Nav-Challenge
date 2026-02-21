"""Exception hooks — site-specific interception for labeled exceptions.

These hooks are ONLY injected for the 3 challenge types that have no visible
UI path. See MISSION.md for why these are exceptions, not the norm.

| Exception              | Challenge Type | Why No Visible Path              |
|------------------------|----------------|----------------------------------|
| WebSocket/SSE/Fetch/XHR| websocket      | Code delivered via WS, never DOM |
| postMessage            | service_worker | Code via SW message, not DOM     |
| Shadow DOM force-open  | shadow_dom     | Closed shadow roots block access |

Usage:
    from agents.v4.exception_hooks import get_exception_hook_js
    js = get_exception_hook_js(challenge_type)
    if js:
        page.evaluate(js)
"""

# Challenge types that need network interception hooks
NETWORK_EXCEPTION_TYPES = {'websocket', 'service_worker'}

# Challenge types that need shadow DOM force-open
SHADOW_EXCEPTION_TYPES = {'shadow_dom'}


NETWORK_HOOKS_JS = r'''
(() => {
    // Exception hook: WebSocket interception
    // Reason: websocket challenge delivers code ONLY via WS message, never rendered
    try {
        const OrigWS = window.WebSocket;
        window.WebSocket = function(...args) {
            const ws = new OrigWS(...args);
            ws.addEventListener('message', (e) => {
                if (window.__codeBus_push) window.__codeBus_push('ws', e.data);
            });
            return ws;
        };
        window.WebSocket.prototype = OrigWS.prototype;
        Object.defineProperty(window.WebSocket, 'name', {value: 'WebSocket'});
        Object.keys(OrigWS).forEach(k => {
            try { window.WebSocket[k] = OrigWS[k]; } catch {}
        });
    } catch (e) { (window.__hookErrors = window.__hookErrors || []).push({hook: 'ws', e: e.message}); }

    // Exception hook: SSE (EventSource) interception
    try {
        const OrigES = window.EventSource;
        if (OrigES) {
            window.EventSource = function(...args) {
                const es = new OrigES(...args);
                es.addEventListener('message', (e) => {
                    if (window.__codeBus_push) window.__codeBus_push('sse', e.data);
                });
                return es;
            };
            window.EventSource.prototype = OrigES.prototype;
        }
    } catch (e) { (window.__hookErrors = window.__hookErrors || []).push({hook: 'sse', e: e.message}); }

    // Exception hook: Fetch interception
    try {
        const ofetch = window.fetch;
        window.fetch = async (...args) => {
            const res = await ofetch(...args);
            try {
                const clone = res.clone();
                clone.text().then(txt => {
                    if (window.__codeBus_push) window.__codeBus_push('fetch', txt);
                }).catch(() => {});
            } catch {}
            return res;
        };
    } catch (e) { (window.__hookErrors = window.__hookErrors || []).push({hook: 'fetch', e: e.message}); }

    // Exception hook: XHR interception
    try {
        const OXHR = XMLHttpRequest.prototype;
        const osend = OXHR.send;
        OXHR.send = function(...args) {
            this.addEventListener('load', () => {
                try {
                    if (window.__codeBus_push) window.__codeBus_push('xhr', this.responseText);
                } catch {}
            });
            return osend.apply(this, args);
        };
    } catch (e) { (window.__hookErrors = window.__hookErrors || []).push({hook: 'xhr', e: e.message}); }

    // Exception hook: postMessage interception (SW → page, iframe → page)
    try {
        window.addEventListener('message', (event) => {
            const data = event.data;
            const text = typeof data === 'string' ? data
                : (typeof data === 'object' ? JSON.stringify(data) : String(data));
            if (text.length < 500 && window.__codeBus_push) window.__codeBus_push('postMessage', text);
        });
        if (navigator.serviceWorker) {
            navigator.serviceWorker.addEventListener('message', (event) => {
                const text = typeof event.data === 'string' ? event.data
                    : JSON.stringify(event.data);
                if (text.length < 500 && window.__codeBus_push) window.__codeBus_push('sw_message', text);
            });
        }
    } catch (e) { (window.__hookErrors = window.__hookErrors || []).push({hook: 'postMessage', e: e.message}); }
})();
'''


SHADOW_DOM_HOOK_JS = r'''
(() => {
    // Exception hook: Shadow DOM force-open
    // Reason: closed shadow roots block ALL visible access to content
    try {
        const origAttach = Element.prototype.attachShadow;
        window.__shadowRoots = window.__shadowRoots || [];
        Element.prototype.attachShadow = function(init) {
            try { init.mode = 'open'; } catch {}
            const root = origAttach.call(this, init);
            window.__shadowRoots.push(root);
            return root;
        };
    } catch (e) { (window.__hookErrors = window.__hookErrors || []).push({hook: 'shadow', e: e.message}); }
})();
'''


def get_exception_hook_js(challenge_type: str) -> str | None:
    """Return the exception hook JS for a challenge type, or None if not needed."""
    if challenge_type in NETWORK_EXCEPTION_TYPES:
        return NETWORK_HOOKS_JS
    if challenge_type in SHADOW_EXCEPTION_TYPES:
        return SHADOW_DOM_HOOK_JS
    return None


def get_all_exception_types() -> set[str]:
    """Return all challenge types that need exception hooks."""
    return NETWORK_EXCEPTION_TYPES | SHADOW_EXCEPTION_TYPES
