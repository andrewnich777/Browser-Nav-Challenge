# Page Anatomy — Challenge Site Structure Analysis

## Overview

The challenge site at `https://serene-frangipane-7fd25b.netlify.app` is a React SPA with deliberately confusing UI elements designed to trap automated tools.

---

## Page Layout (per step)

```
┌─────────────────────────────────────────────────────────────┐
│ HEADER (z-index: 10000+)                                    │
│ "Step N of 30 - Browser Navigation Challenge"               │
│ [May contain decoy links like "Section 4"]                  │
├─────────────────────────────────────────────────────────────┤
│ POPUP OVERLAYS (z-index: 9995-9999) — BLOCKING              │
│ "Please Select an Option", "Warning!", "Amazing Deals"      │
│ [Green button = dismiss, Pink/Orange = DECOY]               │
├─────────────────────────────────────────────────────────────┤
│ CHALLENGE AREA                                              │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Challenge Step N                                        │ │
│ │ [Real instruction in colored box]                       │ │
│ │ [Action elements: buttons, hover areas, canvases]       │ │
│ └─────────────────────────────────────────────────────────┘ │
├─────────────────────────────────────────────────────────────┤
│ CODE INPUT FORM                                             │
│ [Input: "Enter 6-character code"]                           │
│ [Submit button - GREEN]                                     │
├─────────────────────────────────────────────────────────────┤
│ DISTRACTION ZONE (SCROLLABLE - VERY LONG)                   │
│ - Section 1, 2, 3... 20+                                    │
│ - "Keep scrolling to find the navigation button"            │
│ - Decoy buttons: "Load", "Try This", "New Button"           │
│ - Content Blocks that "load" after scroll                   │
│ - ANCHOR LINKS that navigate away!                          │
│ - Fake "Continue", "Next", "Next Section" buttons           │
└─────────────────────────────────────────────────────────────┘
```

---

## Element Categories

### SAFE Elements (OK to click)

| Element | Selector | Purpose |
|---------|----------|---------|
| Green Submit | `button.green:has-text("Submit")` | Submit code |
| Reveal Code buttons | `button:has-text("Reveal"):not(:has-text("Cancel"))` | Reveal hidden codes |
| Green popup dismiss | `button.green` in z-index 9995-9999 | Dismiss blocking popups |
| Radio "Correct Choice" | `input[type="radio"]` with label containing "Correct" | Select correct answer |
| Checkboxes | `input[type="checkbox"]` | Check all required |

### DANGEROUS Elements (Cause 404)

| Element | Why Dangerous | Evidence |
|---------|---------------|----------|
| `<a>` tags anywhere | Navigate to non-existent URLs | learnings.jsonl entry 8, 9 |
| "Section N" links in header | Navigate to `/sectionN` (doesn't exist) | Screenshot analysis |
| "Continue", "Next", "Next Section" | May be `<a>` tags, not `<button>` | Decoy pattern |
| "FOLLOW" / "SBYYBJ" (ROT13) elements | Decoy links designed to trap | learnings.jsonl |
| External links in page content | Navigate away from challenge | Page structure |

### DECOY Elements (Waste time, don't navigate)

| Element | Behavior |
|---------|----------|
| Pink/Orange popup buttons | Spawn more popups, reset progress |
| "Load", "Try This", "New Button" | Do nothing useful |
| Fake "Reveal Code" buttons | Reveal decoy codes (PUNYYR, etc.) |
| Multiple identical-looking buttons | Confuse coordinate-based clicking |

---

## Z-Index Layers

```
z-index 10000+  : Step header bar (persistent)
z-index 9995-9999: Popup overlays (MUST DISMISS)
z-index 1000-9994: Challenge content (interact here)
z-index < 1000  : Distractions/filler content
```

---

## Clickable Element Audit

### Elements with `href` attributes (DANGEROUS)

The page contains anchor tags that can cause navigation:
- Header links (e.g., "Section 4" in pink)
- "Continue Reading" links disguised as buttons
- Help/support links in footer areas
- Decoy navigation in distraction sections

### Elements with `onclick` handlers (MIXED)

- Buttons in challenge area → Usually safe
- Buttons in distraction area → Usually decoys
- Elements in popup overlays → Green = safe, others = trap

### Elements that LOOK clickable but shouldn't be clicked

- Underlined text that isn't in challenge area
- "Navigation button" references (they're decoys)
- Any text containing "follow", "next page", "continue"

---

## Observed Decoy Patterns

### Decoy Codes (from code_scorer.py)
```
PUNYYR (ROT13 of "CHALYR")
SBYYBJ (ROT13 of "FOLLOW")  ← THIS IS KEY!
SUBMIT, SCROLL, REVEAL, HIDDEN
BUTTON, CANCEL, SEARCH, SELECT
VERIFY, EXPAND, ENABLE, TOGGLE, CHANGE
```

### Decoy Instructions
- "This is filler content"
- "Scroll Down to Find Navigation"
- "Keep scrolling to find the navigation button"
- Generic "You are on step X" text

### Visual Decoys
- Multiple similar-colored buttons
- Buttons at various scroll positions
- "Content Block X Loaded!" sections

---

## Critical Finding: The 404 Trigger

From learnings.jsonl analysis:
1. The code "SBYYBJ" appears frequently in failed extractions
2. SBYYBJ decodes (ROT13) to "FOLLOW"
3. There's likely a clickable "FOLLOW" element or link on the page
4. When clicked, it navigates to a URL that returns Netlify 404

**Hypothesis**: The agent clicks something containing "FOLLOW" or a similar navigation element, causing unintended page navigation.

---

## Safe Interaction Zones

```
SAFE ZONE (interact freely):
┌─────────────────────────────────────────┐
│ Challenge instruction box               │
│ Code input form                         │
│ Green Submit button                     │
│ Explicit "Reveal Code" buttons in       │
│ challenge area only                     │
└─────────────────────────────────────────┘

DANGER ZONE (avoid clicks):
┌─────────────────────────────────────────┐
│ Anything below the code input form      │
│ Anything in the "distraction" sections  │
│ Any <a> tags                            │
│ Pink/orange popup buttons               │
│ Header navigation elements              │
└─────────────────────────────────────────┘
```

---

## Recommendations for Click Safety

1. **Never click `<a>` tags** — Already implemented in `click_from_vision_coords` but may be bypassed
2. **Bound clicks to challenge area** — Only click within the first 400-500px vertically
3. **Validate element before click** — Check `tagName !== 'A'` before every click
4. **Use JavaScript clicks** — `element.click()` doesn't follow `<a>` href
5. **Pre-check URL** — Save URL before click, check after, recover if changed
