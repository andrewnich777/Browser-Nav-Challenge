# Root Causes — Identified Issues with Evidence

## Root Cause 1: Fallback Click Bypasses Anchor Filter

### Location
`orchestrator.py:302-306` in `click_from_vision_coords()`

### The Bug

```python
if result and result.get('score', -1) >= 0:
    log(f"click_from_vision: found {result.get('tag')} at ...")
    page.mouse.click(result['x'], result['y'])
else:
    # Fallback to raw coordinates - NO ELEMENT VALIDATION
    safe_y = max(50, min(viewport_y, metrics['viewportHeight'] - 50))
    log(f"click_from_vision: fallback click at ({doc_x:.0f}, {safe_y:.0f})")
    page.mouse.click(doc_x, safe_y)  # <- CLICKS ANYTHING!
```

### Why It's a Problem

The `elementFromPoint` search has a filter for `<a>` tags:
```javascript
// SKIP anchor tags - clicking them causes page navigation!
if (el.tagName === 'A') return -1;
```

But when NO suitable element is found (all candidates score -1), the code falls back to clicking raw coordinates. This fallback:
- Has no element validation
- Could click directly on an `<a>` tag
- Causes browser navigation to invalid URL → 404

### Evidence

- learnings.jsonl shows "SBYYBJ" (FOLLOW) in extracted codes
- 404 pages reached after click actions
- The word "FOLLOW" suggests a link element was clicked

### Severity: HIGH

---

## Root Cause 2: No URL Change Detection Before/After Clicks

### Location
Throughout `orchestrator.py` — clicks happen without pre/post URL checks

### The Bug

Currently, URL is only checked AFTER code submission:
```python
# After code_entry (orchestrator.py:777-790)
for _ in range(5):
    page.wait_for_timeout(150)
    if page.url != current_url:
        advanced = True
        break
```

But intermediate clicks (vision clicks, click_reveal, popup) don't check:
```python
# Vision click - no URL check
self.click_from_vision_coords(page, x, y, height)
self.wait_for_raf_stable(page, timeout_ms=500)
# URL could be 404 here, we don't know!

# click_reveal - no URL check
self._run_agent("click_reveal", page, step, version)
# URL could be 404 here, we don't know!
```

### Why It's a Problem

If a click causes navigation to 404:
1. All subsequent actions operate on wrong page
2. Code extraction finds garbage ("SBYYBJ")
3. Submission fails
4. Retry logic can't help (still on 404)
5. No recovery attempted

### Evidence

- Failures show multiple agents tried AFTER 404 was reached
- Extract found "SBYYBJ" from 404 page text
- System kept trying to solve non-existent challenge

### Severity: HIGH

---

## Root Cause 3: No 404 Detection or Recovery

### Location
Missing from `orchestrator.py`

### The Bug

The system has no mechanism to:
1. Detect when it's on a 404 page
2. Recover from accidental navigation
3. Return to the challenge

A `PageValidatorAgent` was generated (`knowledge/generated_404_error.py`) but is NOT integrated into the orchestrator.

### Why It's a Problem

Once on 404, the system is stuck:
- All extraction fails
- All submissions fail
- Retry logic just repeats failures
- Step times out with no recovery

### Evidence

- `generated_404_error.py` exists — system identified the need
- But it's not used in orchestrator.py
- Failures show repeated attempts on 404 page

### Severity: HIGH

---

## Root Cause 4: Vision Coordinates May Target Wrong Elements

### Location
`vision_client.py` — coordinate extraction from screenshots

### The Bug

Vision API provides coordinates based on screenshot analysis:
```
ACTION_COORDS: [x, y coordinates for click/hover, or "none"]
```

Problems:
1. Coordinates are from full-page screenshot
2. Scaling between screenshot and actual DOM is imperfect
3. Visual similarity between decoys and real targets
4. No validation that coordinates point to safe elements

### Why It's a Problem

Vision might say "click at (200, 350)" but that coordinate could be:
- A decoy button
- An anchor link
- Overlapping with navigation element
- Shifted due to popups/dynamic content

### Evidence

- Vision suggests coordinates that lead to failures
- `click_from_vision_coords` has to search locally around target
- Fallback path exists because "good" element often not found

### Severity: MEDIUM

---

## Root Cause 5: Decoy "Navigation" Elements Exist on Page

### Location
Challenge page content (external)

### The Pattern

The challenge pages contain deliberate traps:
- Links labeled "FOLLOW", "Continue", "Next Section"
- Text saying "Keep scrolling to find the navigation button"
- Anchor tags styled to look like buttons
- Section links in headers

### Why It's a Problem

These elements:
1. Look like valid interaction targets
2. Have href attributes pointing to invalid URLs
3. When clicked, cause navigation to Netlify 404
4. Are scattered throughout the page, not just in "danger zone"

### Evidence

- "SBYYBJ" (FOLLOW) repeatedly extracted
- Screenshots show "Continue Reading", "Next Section" buttons
- Netlify 404 says "followed a broken link"

### Severity: CONTEXT (external factor, must work around)

---

## Root Cause Summary Table

| # | Root Cause | Severity | Fix Complexity |
|---|------------|----------|----------------|
| 1 | Fallback click bypasses anchor filter | HIGH | LOW |
| 2 | No URL change detection between clicks | HIGH | MEDIUM |
| 3 | No 404 detection/recovery | HIGH | MEDIUM |
| 4 | Vision coordinates target wrong elements | MEDIUM | HIGH |
| 5 | Decoy navigation elements on page | CONTEXT | N/A |

---

## Interaction Diagram

```
Vision gives coordinates
        │
        ▼
┌─────────────────────────────┐
│ elementFromPoint search     │
│ (Has anchor filter)         │
└─────────────┬───────────────┘
              │
        ┌─────┴─────┐
        │           │
   Found good    No good element
   element       (all scored -1)
        │           │
        ▼           ▼
┌───────────┐ ┌─────────────────┐
│ Click at  │ │ FALLBACK CLICK  │ ← BUG!
│ element   │ │ at raw coords   │
│ center    │ │ (no validation) │
└───────────┘ └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │ Clicks <a> tag  │
              │ by accident     │
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │ Browser follows │
              │ href → 404      │
              └─────────────────┘
```

---

## Why This Wasn't Caught Earlier

1. **Anchor filter appeared to work** — The JavaScript code correctly filters `<a>` tags
2. **Fallback path rarely discussed** — Focus was on "finding good elements"
3. **404 looks like challenge failure** — Easy to mistake for "wrong code" vs "wrong page"
4. **"SBYYBJ" looks like a code** — 6 chars, matches charset, gets submitted
5. **No explicit 404 handling** — System assumes it's always on challenge page

---

## Chain of Events Leading to Discovery

1. Multiple runs failed at various steps (2, 9, etc.)
2. Learning system captured screenshots showing 404
3. Learning system identified "404_error" challenge type
4. Generated `PageValidatorAgent` to detect error pages
5. But orchestrator never integrated the fix
6. This investigation connected the dots
