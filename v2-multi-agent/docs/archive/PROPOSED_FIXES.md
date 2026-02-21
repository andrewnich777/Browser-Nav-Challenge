# Proposed Fixes — Specific Solutions with Rationale

## Overview

Based on the investigation, five root causes were identified. This document proposes specific code fixes for each, ordered by priority.

---

## Fix 1: Remove Fallback Click (Highest Priority)

### Problem
The fallback click at raw coordinates bypasses the anchor tag filter.

### Location
`orchestrator.py:302-306`

### Current Code
```python
if result and result.get('score', -1) >= 0:
    log(f"click_from_vision: found {result.get('tag')} at ({result['x']:.0f}, {result['y']:.0f}) score={result['score']}")
    page.mouse.click(result['x'], result['y'])
else:
    # Fallback to raw coordinates
    safe_y = max(50, min(viewport_y, metrics['viewportHeight'] - 50))
    log(f"click_from_vision: fallback click at ({doc_x:.0f}, {safe_y:.0f})")
    page.mouse.click(doc_x, safe_y)
```

### Proposed Fix
```python
if result and result.get('score', -1) >= 0:
    log(f"click_from_vision: found {result.get('tag')} at ({result['x']:.0f}, {result['y']:.0f}) score={result['score']}")
    page.mouse.click(result['x'], result['y'])
else:
    # NO FALLBACK - refusing to click without validated element
    log(f"click_from_vision: NO SAFE ELEMENT FOUND at ({doc_x:.0f}, {viewport_y:.0f}) - skipping click")
    # Return without clicking - let other agents try different approaches
```

### Rationale
- Clicking blindly at coordinates is never safe
- Better to skip a click than navigate to 404
- Other agents (click_reveal, scroll) can try alternative approaches

### Risk
- May miss some valid clicks
- Mitigation: Improve element search grid (wider radius, more samples)

---

## Fix 2: Add URL Guard Around All Clicks

### Problem
No detection when a click causes unintended navigation.

### Location
New function in `orchestrator.py`

### Proposed Code
```python
def safe_click(self, page, x: int, y: int, description: str = "click") -> bool:
    """
    Click with URL change detection. Returns True if click was safe.
    If URL changed unexpectedly, attempts recovery.
    """
    url_before = page.url

    page.mouse.click(x, y)
    page.wait_for_timeout(200)

    url_after = page.url

    if url_after != url_before:
        # Check if this is expected navigation (step advance)
        if '/step' in url_after and 'version=' in url_after:
            # Might be legitimate step advance - let caller decide
            log(f"safe_click: URL changed to {url_after}")
            return True

        # Unexpected navigation!
        log(f"safe_click: UNEXPECTED NAVIGATION from {url_before} to {url_after}")

        # Check if we're on 404
        if self._is_error_page(page):
            log(f"safe_click: ON ERROR PAGE - attempting recovery")
            self._recover_from_navigation(page, url_before)
            return False

        return True

    return True

def _is_error_page(self, page) -> bool:
    """Check if current page is a 404 or error page."""
    try:
        title = page.title().lower()
        if 'not found' in title or '404' in title or 'error' in title:
            return True

        body = page.inner_text('body')[:500].lower()
        if 'page not found' in body or 'doesn\'t exist' in body:
            return True
    except:
        pass
    return False

def _recover_from_navigation(self, page, target_url: str) -> bool:
    """Recover from accidental navigation by returning to challenge."""
    log(f"_recover_from_navigation: restoring {target_url}")

    # Parse step and version from target URL
    import re
    match = re.search(r'/step(\d+)\?version=(\d+)', target_url)
    if not match:
        log(f"_recover_from_navigation: couldn't parse URL")
        return False

    step = int(match.group(1))
    version = int(match.group(2))

    # Go to home first (only valid entry point)
    page.goto(BASE_URL)
    page.wait_for_load_state('networkidle')

    # Use pushState to route to target step
    target_path = f'/step{step}?version={version}'
    page.evaluate(f'''() => {{
        window.history.pushState({{}}, '', '{target_path}');
        window.dispatchEvent(new PopStateEvent('popstate', {{state: {{}}}}));
    }}''')
    page.wait_for_timeout(500)

    # Verify recovery
    if f'/step{step}' in page.url:
        log(f"_recover_from_navigation: SUCCESS")
        return True

    log(f"_recover_from_navigation: FAILED")
    return False
```

### Integration Points
Replace direct `page.mouse.click()` calls with `self.safe_click()`:
- `orchestrator.py:301` — vision click
- `orchestrator.py:1027` — learning action click
- `orchestrator.py:1037` — fallback click (if kept)
- `orchestrator.py:1148` — stored action click

### Rationale
- Catches navigation problems immediately
- Automatic recovery means step can continue
- Logging helps debug future issues

---

## Fix 3: Integrate PageValidatorAgent

### Problem
System doesn't detect when it's on an error page.

### Location
`orchestrator.py:407-450` (start of `run_step`)

### Proposed Code
```python
def run_step(self, page, step: int, version: int) -> tuple[bool, list[str]]:
    """Solve a step like a human: detect challenge, do action, extract code, submit."""
    self.metrics['steps_attempted'] += 1
    agents_used = []
    current_url = page.url

    log(f"step {step}: begin")

    # === NEW: Check if we're on an error page before starting ===
    if self._is_error_page(page):
        log(f"step {step}: STARTING ON ERROR PAGE - attempting recovery")
        if not self._recover_from_navigation(page, f"{BASE_URL}/step{step}?version={version}"):
            log(f"step {step}: recovery failed, cannot proceed")
            return False, agents_used
        current_url = page.url  # Update after recovery

    # Rest of run_step continues...
```

### Also Add Check After Every Agent
```python
def _run_agent_safe(self, name, page, step, version, **kwargs):
    """Run agent with URL safety check."""
    url_before = page.url
    result = self._run_agent(name, page, step, version, **kwargs)

    # Check for accidental navigation
    if page.url != url_before and self._is_error_page(page):
        log(f"step {step}: agent {name} caused navigation to error page!")
        self._recover_from_navigation(page, url_before)
        return False

    return result
```

### Rationale
- Fail fast if already on error page
- Recover immediately when detected
- Prevents wasted retries on wrong page

---

## Fix 4: Strengthen Anchor Tag Filter in elementFromPoint

### Problem
The search grid might not find anchors if they're at the exact click point.

### Location
`orchestrator.py:265-297` (the JavaScript in `click_from_vision_coords`)

### Current Code
```javascript
// SKIP anchor tags - clicking them causes page navigation!
if (el.tagName === 'A') return -1;
```

### Proposed Enhancement
```javascript
function score(el) {
    if (!el) return -1;
    const cs = getComputedStyle(el);
    if (cs.pointerEvents === 'none' || cs.visibility === 'hidden') return -1;
    if (cs.display === 'none') return -1;

    // SKIP anchor tags - clicking them causes page navigation!
    if (el.tagName === 'A') return -1;

    // ALSO skip elements INSIDE anchor tags
    if (el.closest('a')) return -1;

    // Skip elements with href attribute (some non-anchor elements have it)
    if (el.hasAttribute('href')) return -1;

    // Skip elements with onclick that contains navigation keywords
    const onclick = el.getAttribute('onclick') || '';
    if (/location|href|navigate|window\.open/i.test(onclick)) return -1;

    let s = 0;
    if (el.tagName === 'BUTTON' || el.onclick) s += 5;
    if (cs.cursor === 'pointer') s += 2;
    if (el.getAttribute('role') === 'button') s += 3;
    return s;
}
```

### Rationale
- `el.closest('a')` catches elements nested inside anchor tags
- `hasAttribute('href')` catches `<area>`, `<base>`, `<link>` with href
- onclick check catches JavaScript-based navigation

---

## Fix 5: Add Click Boundary Enforcement

### Problem
Clicks in the "distraction zone" (below code input) are dangerous.

### Location
`orchestrator.py` — add to `click_from_vision_coords`

### Proposed Code
```python
def click_from_vision_coords(self, page, full_x: int, full_y: int, screenshot_height: int):
    """Convert full-page screenshot coords to viewport click with smart targeting."""

    # === NEW: Enforce click boundaries ===
    # Get the code input form position as the safe zone boundary
    safe_zone_bottom = page.evaluate('''() => {
        const input = document.querySelector('input[placeholder*="code" i]');
        if (input) {
            const rect = input.getBoundingClientRect();
            const scroll = window.scrollY;
            return rect.bottom + scroll + 100;  // 100px buffer below input
        }
        return 600;  // Default safe zone
    }''')

    # Scale vision coords to document coords
    metrics = page.evaluate('''() => ({
        scrollHeight: document.documentElement.scrollHeight,
        viewportHeight: window.innerHeight,
        scrollY: window.scrollY
    })''')

    scale = metrics['scrollHeight'] / max(1, screenshot_height) if screenshot_height > 0 else 1
    doc_y = full_y * scale
    doc_x = full_x * scale

    # === NEW: Refuse to click below safe zone ===
    if doc_y > safe_zone_bottom:
        log(f"click_from_vision: REFUSING click at y={doc_y:.0f} (below safe zone {safe_zone_bottom})")
        return  # Don't click at all

    # Rest of function continues...
```

### Rationale
- Distraction zone is always below the code input
- Legitimate challenge elements are above the input
- Refusing clicks below input eliminates most decoy targets

---

## Fix 6: Pre-Click Element Logging

### Problem
Hard to debug what element was clicked when failures occur.

### Location
Add to `orchestrator.py`

### Proposed Code
```python
def log_element_at_coords(self, page, x: int, y: int) -> dict:
    """Log details about element at coordinates before clicking."""
    info = page.evaluate('''([x, y]) => {
        const el = document.elementFromPoint(x, y);
        if (!el) return {found: false};

        return {
            found: true,
            tagName: el.tagName,
            id: el.id || null,
            className: el.className || null,
            textContent: (el.textContent || '').substring(0, 50),
            href: el.getAttribute('href') || el.closest('a')?.getAttribute('href') || null,
            isAnchor: el.tagName === 'A' || !!el.closest('a'),
            boundingRect: el.getBoundingClientRect()
        };
    }''', [x, y])

    log(f"Element at ({x}, {y}): {info}")
    return info
```

### Use Before Every Click
```python
# Before clicking
element_info = self.log_element_at_coords(page, click_x, click_y)
if element_info.get('isAnchor'):
    log(f"REFUSING to click anchor: href={element_info.get('href')}")
    return
```

### Rationale
- Creates audit trail for debugging
- Last chance to catch anchor tags
- Helps identify new decoy patterns

---

## Implementation Priority

| Fix | Priority | Effort | Impact |
|-----|----------|--------|--------|
| Fix 1: Remove fallback click | P0 | 5 min | Eliminates primary bug |
| Fix 2: URL guard + recovery | P0 | 30 min | Catches and recovers from any navigation |
| Fix 3: PageValidator integration | P1 | 15 min | Early detection of error state |
| Fix 4: Strengthen anchor filter | P1 | 10 min | Defense in depth |
| Fix 5: Click boundary | P2 | 15 min | Eliminates distraction zone clicks |
| Fix 6: Pre-click logging | P2 | 10 min | Debugging aid |

---

## Testing Plan

After implementing fixes:

1. **Unit test: anchor tag rejection**
   - Create page with anchor at known coordinates
   - Verify click is refused

2. **Unit test: URL recovery**
   - Navigate to 404 manually
   - Verify recovery restores correct step

3. **Integration test: full run**
   - Run 30 steps with `--headed`
   - Verify no 404 pages encountered
   - Count recovery attempts (should be 0 ideally)

4. **Regression test**
   - Re-run the exact scenarios from learnings.jsonl
   - Step 2 v3, Step 9 v3 should now succeed

---

## Success Criteria

- [ ] No "Page not found" screenshots in knowledge/ folder
- [ ] No "SBYYBJ" in extracted codes
- [ ] 404 recovery code never triggered (no navigations to recover from)
- [ ] 30/30 steps completed without navigation errors

---

## Implementation Status (Updated 2026-02-03)

| Fix | Status | Notes |
|-----|--------|-------|
| Fix 1: Remove fallback click | ✅ DONE | Fallback click removed |
| Fix 2: URL guard + recovery | ✅ DONE | `_safe_click()`, `_recover_from_navigation()` added |
| Fix 3: PageValidator integration | ✅ DONE | `_is_error_page()`, `_check_for_blocking_modal()` added |
| Fix 4: Strengthen anchor filter | ✅ DONE | `el.closest('a')` check added |
| Fix 5: Click boundary | ✅ DONE | Safe zone enforcement added |
| Fix 6: Pre-click logging | ✅ DONE | `_log_element_at_coords()` added |

### Additional Fixes Applied:
- **Blank Page Fix**: Removed `el.remove()` from popup.py and click_reveal.py
- **Modal Dismissal**: Added Escape key, backdrop click, direct Playwright click
- **Hidden DOM**: Added `_dismiss_blocking_modals()` method
- **Learning Agent**: Refactored to use shared vision client (no duplicate API)
