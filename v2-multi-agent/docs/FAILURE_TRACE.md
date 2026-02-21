# Failure Trace — Exact Sequence Leading to 404

## Summary

Based on analysis of failure screenshots, learnings.jsonl, and code review, here's the reconstructed failure sequence.

---

## Documented Failures from learnings.jsonl

### Failure 1: Step 9, Version 3 (timestamp: 11:43:41)

```
Challenge type detected: 404_error
Agents tried: popup, vision, scroll, click_reveal, radio, checkbox, decode, code_entry
Extracted code: "SBYYBJ" (ROT13 decodes to "FOLLOW")
Expected code: Unknown (page was 404)

ANALYSIS: "The page shows a Netlify 404 'Page not found' error.
This is not a challenge page at all - the challenge URL is broken
or the site has moved/been deleted."
```

### Failure 2: Step 2, Version 3 (timestamp: 11:46:32)

```
Challenge type detected: 404_error/navigation_failure
Agents tried: popup, vision, scroll, click_reveal, radio, checkbox, decode, code_entry
Extracted code: "SBYYBJ" (ROT13 decodes to "FOLLOW")
Expected code: "7WNZ7C"

ANALYSIS: "The 404 indicates we either:
(1) clicked a link that was meant to be decoded/transformed before following,
(2) navigated away from the challenge incorrectly"
```

---

## Reconstructed Failure Sequence

### Step-by-Step Trace

```
1. STEP START
   - URL: /step2?version=3 (or similar)
   - Page loads correctly
   - Hooks installed, observers active

2. POPUP DISMISSAL
   - popup agent runs
   - Clicks green buttons in z-index 9995-9999
   - Popups dismissed (probably OK)

3. VISION ANALYSIS
   - Screenshot taken
   - Vision API analyzes page
   - Returns: action=scroll, click, or unknown
   - May return coordinates for clicking

4. ACTION EXECUTION (THE PROBLEM)
   - Agent needs to scroll or click
   - Vision gives coordinates, e.g., (200, 350)

   OPTION A: click_reveal agent runs
   - Searches for "Reveal" buttons
   - May find decoy button or <a> tag
   - Clicks it

   OPTION B: Vision click at coordinates
   - click_from_vision_coords() called
   - Scrolls to target area
   - Uses elementFromPoint to find clickable
   - SHOULD skip <a> tags... but something slips through

5. UNINTENDED NAVIGATION OCCURS
   - Something with "FOLLOW" text is clicked
   - Browser navigates to /follow or similar
   - Netlify returns 404

6. 404 PAGE DETECTED
   - Page now shows "Page not found"
   - extract_code runs, finds "SBYYBJ" in page text
   - SBYYBJ = ROT13("FOLLOW") which appears somewhere
   - Agent thinks this is a code, tries to submit
   - Everything fails from here
```

---

## Evidence Analysis

### The "SBYYBJ" Connection

The code "SBYYBJ" appears repeatedly in failures:
- It's ROT13-encoded "FOLLOW"
- It appears in the 404 page content OR
- It was visible on the challenge page before navigation
- The presence of "FOLLOW" as extractable text suggests there's a visible element

**Hypothesis**: There's a link or button labeled "FOLLOW" (or containing this text) that:
1. The vision API sees as a target
2. The click_reveal agent finds
3. When clicked, navigates away

### Screenshot Evidence

From `failure_step9_v3_114329.png` and `failure_step2_v3_114619.png`:
- Both show Netlify's standard 404 page
- "Looks like you've followed a broken link"
- The word "followed" is ironic — they literally followed a link

From `failure_step1_v1_073234.png`:
- Challenge page shows many clickable elements
- Buttons like "Continue", "Next", "Next Section"
- These could be `<a>` tags, not `<button>` elements
- Clicking any of these could cause navigation

From `after_click_1.png`:
- Page after scrolling shows extensive "distraction zone"
- Many sections with "Keep scrolling to find navigation button"
- The word "navigation" is a decoy concept

---

## Click Source Analysis

Where clicks originate in the code:

### 1. orchestrator.py:301 — Vision Click

```python
page.mouse.click(result['x'], result['y'])
```
- Uses `elementFromPoint` to find target
- Has anchor tag filter (`el.tagName === 'A'` returns -1)
- BUT: Clicks raw coordinates if no good element found (line 306)

### 2. orchestrator.py:306 — Fallback Click

```python
page.mouse.click(doc_x, safe_y)  # Fallback to raw coordinates
```
- No element validation
- Could click anything at those coordinates
- DANGER: This bypasses the anchor filter

### 3. click_reveal.py:88 — Force Click

```python
reveal_btns.nth(i).click(force=True, timeout=2000)
```
- Uses Playwright locator `button:has-text("Reveal")`
- Should only find `<button>` elements
- BUT: force=True might click overlapping elements

### 4. click_reveal.py:167 — JavaScript Click

```python
btn.click()  # In page.evaluate
```
- Clicks any element matching reveal patterns
- Filter checks: not in overlay, not submit, not dismiss/close/cancel
- MISSING: No check for `<a>` tags

### 5. popup.py — Dismiss Clicks

```javascript
btn.click()  // Various buttons in popup areas
```
- Clicks buttons in popup overlays
- Should be safe (popups are above challenge)
- BUT: If popup has a link, clicking it navigates

---

## The Specific Bug

### Most Likely Culprit: click_reveal.py JavaScript Click

In `click_reveal.py:146-184`:

```javascript
// Get all buttons including those in shadow DOMs
const allButtons = [...document.querySelectorAll('button')];
// ...

// Filter out overlay buttons and submit buttons
const challengeButtons = allButtons.filter(btn => {
    if (isInOverlay(btn)) return false;
    if (btn.type === 'submit') return false;
    // ... other filters ...
    return true;
});
```

**The problem**: It only queries `button` elements. BUT...

On the page, there might be:
```html
<a class="button" href="/follow">FOLLOW</a>
```

This wouldn't be caught by `querySelectorAll('button')`, but the Playwright locator:
```python
reveal_btns = page.locator('button:has-text("Reveal"):not(:has-text("Cancel")):not(:has-text("Close"))')
```

This SHOULD be safe because it explicitly asks for `button` elements.

### Secondary Culprit: orchestrator.py Fallback Click

```python
# orchestrator.py:302-306
if result and result.get('score', -1) >= 0:
    page.mouse.click(result['x'], result['y'])
else:
    # Fallback to raw coordinates
    safe_y = max(50, min(viewport_y, metrics['viewportHeight'] - 50))
    page.mouse.click(doc_x, safe_y)  # <- DANGER!
```

The fallback click at raw coordinates could hit an anchor tag.

---

## Timeline of a Typical Failure

```
T+0ms     : run_step() starts for step N
T+200ms   : popup agent dismisses overlays
T+500ms   : vision analysis completes, suggests action=click
T+600ms   : click_from_vision_coords() called
T+650ms   : elementFromPoint finds element, but score < 0 (maybe <a> tag)
T+700ms   : Fallback click at raw coordinates
T+750ms   : Mouse click happens
T+800ms   : <a> tag receives click
T+850ms   : Browser navigates to href
T+1000ms  : Netlify returns 404
T+1100ms  : Page shows "Page not found"
T+1200ms  : extract_code scans page, finds "SBYYBJ" (from "followed")
T+1500ms  : Agent tries to submit "SBYYBJ" as code
T+2000ms  : Submission fails (obviously)
T+3000ms  : Retry logic kicks in, still on 404 page
T+10000ms : Step times out, marked as failed
```

---

## Key Evidence Points

1. **"SBYYBJ" = "FOLLOW"** — Not a random code, but a word related to navigation
2. **Multiple steps affected** — Steps 2 and 9 at minimum
3. **Same extracted code** — "SBYYBJ" appears in multiple failures
4. **404 page reached** — Proves actual navigation occurred
5. **Vision coordinates involved** — Failures happen during click actions

---

## Verification Needed

To confirm this hypothesis, run with `--headed` and watch:
1. What element is at the coordinates vision provides?
2. Is there a "FOLLOW" link visible on the page?
3. Does the fallback click path execute?
4. What is the URL immediately after the click?
