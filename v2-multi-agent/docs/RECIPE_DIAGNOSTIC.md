# Recipe System Diagnostic Report

**Date**: 2026-02-08
**Runs analyzed**: Run 1 (v=1, 10/30) + Run 2 (v=2, 30/30)
**Verdict**: Recipes are NOT majorly progressing. 11% hit rate on Run 2. Testing stopped per user instruction.

---

## Executive Summary

The recipe system has **6 reliable recipes** (tier 3, 94% replay success) that save time on ~3-5 steps per run. The other **20 tier-1 recipes have a 9.1% replay success rate** and actively waste 5-10 seconds each on failed attempts before falling through to the sidecar. The promotion pipeline produces recipes that are too fragile to replay.

| Metric | Run 2 (v=2) |
|--------|-------------|
| Total steps | 30/30 |
| Recipe attempts | 10 |
| Recipe successes | 1 (keyboard_sequence) |
| Recipe hit rate | 11% |
| Time wasted on failed recipes | ~55s |
| Sidecar solves | 26 |
| /finish workaround | 1 (step 30) |

---

## What Works (Tier 3 — Do Not Touch)

These 6 recipes have 94% replay success and should be preserved:

| Recipe | Steps | Replay Rate | Why It Works |
|--------|-------|-------------|--------------|
| `keyboard_sequence_v2` | press Ctrl+V | 8/9 (89%) | Single keypress, no targeting needed |
| `keyboard_sequence_v3` | press Ctrl+Shift+K | 5/7 (71%) | Same — single keypress |
| `delay_memory_v2` | click "I Remember" | 8/8 (100%) | Text-targeted click, stable UI |
| `delay_memory_v3` | click "I Remember" x2 | 5/5 (100%) | Same pattern |
| `scroll_v1` | scroll down 500px | 4/4 (100%) | No targeting — just scroll |
| `timing_v3` | click "Capture" x3 | 5/5 (100%) | Text-targeted clicks, stable UI |

**Pattern**: Simple actions (1-2 steps), text-based targeting (not coordinates), no session-specific values, no complex interactions.

---

## Failure Categories (10 Broken Recipe Types)

### Category A: Drag Actions Don't Work (3 recipes)

**Affected**: `drag_drop_v1`, `drag_drop_v2`, `drag_drop_v3`

**Symptom**: Every drag shows 0 progress. Assertion fails with `progress delta 0.00 < expected 0.17`.

**Root Cause**: The recipe executor dispatches synthetic HTML5 `DragEvent`s via JavaScript (`dragstart→dragenter→dragover→drop→dragend`). But React uses its own **SyntheticEvent system** — it doesn't listen on native DragEvents dispatched programmatically. React attaches a single delegated listener at the root that intercepts native browser drag events, but events created with `new DragEvent()` in JS and dispatched via `element.dispatchEvent()` are marked as "untrusted" (`event.isTrusted === false`). React's event delegation ignores untrusted events.

**Why sidecar works**: The sidecar uses Playwright's `locator.drag_to()`, which operates at the Chrome DevTools Protocol (CDP) level via `Input.dispatchDragEvent`. CDP-dispatched events are trusted by the browser.

**Fix required**: Replace `page.evaluate(DragEvent JS)` with `page.drag_and_drop(source_selector, target_selector)` or find/create locators from coordinates at replay time. Alternatively, use CDP `Input.dispatchDragEvent` directly via `page.evaluate` is NOT possible (CDP is a protocol, not JS API) — must use Playwright's built-in drag methods.

**Difficulty**: Medium-hard. Recipe steps store coordinates, not selectors. Need to either:
- (a) Store selectors during promotion (changes promotion pipeline), or
- (b) Find elements at coordinates and build locators at replay time, or
- (c) Use `page.mouse` down/move/up sequence BUT simulate the CDP-level "drag interceptor" pattern

### Category B: Recipe Clicks "Solve" But No Code Appears (4 recipes)

**Affected**: `puzzle_solve_v2`, `calculated_v2`, `calculated_v3`, `websocket_v2`

**Symptom**: Recipe types the answer, clicks Solve, then assertion says "no valid code visible".

**Detailed Run 2 analysis**:
- Step 18 (`puzzle_solve`): Recipe has 3 steps: `click`, `type` (resolver=eval_expression), `click` Solve. The type+click executes, but code doesn't appear in DOM within the 6×200ms assertion polling window (1.2s total).
- Step 19 (`calculated`): **Misdetected** — `puzzle_solve_v2` recipe matched a `calculated` challenge. Cross-contamination from overlapping keywords.
- Step 28 (`calculated`): `calculated_v2` recipe types "36" via resolver (18+18=36, correct!), clicks Solve. Same assertion failure — code not visible in time.
- Step 30 (`websocket`): Connect + wait + Reveal Code. Completion sweep clicked "Reveal Code" but no code appeared (websocket timing issue).

**Root Causes**:
1. **Assertion polling window too short**: 6 polls × 200ms = 1.2s. Some challenges need 2-3s for the code to appear after the Solve click. The sidecar uses longer observation windows.
2. **Detection cross-contamination**: puzzle_solve and calculated have overlapping keywords (both use "puzzle", "solve", "input", "type"), causing the wrong recipe to match.
3. **websocket timing inherently variable**: The simulated WebSocket server delivers the code at unpredictable times.

### Category C: Recipe Steps Execute But Focus/Hover Doesn't Register (3 recipes)

**Affected**: `conditional_reveal_v2`, `sequence_v2`, `gesture_v2`

**Symptom**: First 1-2 steps execute (click, hover), then focus/hover step shows `progress delta 0.00 < expected 0.25`.

**Detailed Run 2 analysis**:
- Step 25 (`conditional_reveal_v2`): Recipe does click→hover→focus. First two complete (progress goes to 2/4), but the `focus` step doesn't register. The sidecar then does `focus BID 1`, `type 'test'`, `element_scroll` and completes in 1 round.
- Step 27 (`sequence_v2`): Recipe does click→hover. Hover doesn't register progress (delta 0.00 < 0.25). But the page shows "Progress: 2/4" with "click button ✓" and "hover area ✓" already checked — meaning the **recipe's first two steps DID work**, but the recipe thinks they didn't.

**Root Causes**:
1. **Stale React state from previous recipe run**: The recipe ran on a PREVIOUS step with similar UI. When the same component renders on the new step, React reuses state (the "state leak" bug). The `click` and `hover` from the recipe are already "checked" because of leaked state, not because the recipe did them. So progress was ALREADY at 2/4 before the recipe ran. The recipe's hover assertion expects 0.25 progress delta (from 0.5 to 0.75), but actual delta is 0.00 (already at 0.5, hover didn't advance because hover target is already satisfied from stale state).
2. **Focus action targeting**: The recipe's `focus` step uses coordinate-based targeting that may miss the input element. The sidecar uses BID-based targeting which is more precise.
3. **gesture_v2**: Recipe's first step is a `click` that fails immediately — the click target doesn't exist or is at wrong coordinates.

### Category D: Soft Failure — Code Not Extracted After Successful Steps

**Affected**: `delay_memory_v2` (on this specific run)

**Symptom**: All recipe steps execute successfully (no assertion failure), but `_extract_code_after_recipe()` returns None. Recipe "completes" but no code found.

**Root Cause**: The code may take slightly longer to appear in DOM after the final click. The `_extract_code_after_recipe()` function calls `harvest_and_score()` which scans the DOM. If the delay_memory challenge's code reveal has a CSS animation delay (the "flash" pattern), the code might not be in DOM at the exact moment of extraction.

---

## Recipe Detection Cross-Contamination

Run 2 revealed that `puzzle_solve_v2` matched a `calculated` challenge at step 19. This is because:

- puzzle_solve_v2 keywords: `input, puzzle, scroll, solve, type`
- calculated_v2 keywords: `calculated, input, puzzle, solve, type`

They share 4/5 keywords. The detection system picks the highest-scoring match, and puzzle_solve might score higher due to Wilson confidence. This means step 19 gets the wrong recipe, wastes time, and then sidecar has to clean up.

**Other likely cross-contaminations**:
- `sequence_v2` vs `conditional_reveal_v2` (both have: area, box, container, hover, input, scroll, sequence, type)
- `delayed_reveal_v*` vs generic timing challenges

---

## Structural Problems in the Promotion Pipeline

### Problem 1: One-Success Promotion
Recipes are promoted to tier 1 after a SINGLE successful sidecar solve. This is insufficient evidence that the recipe is replayable — the sidecar's actions may have worked due to vision-guided precise targeting that the recipe executor can't replicate.

### Problem 2: Coordinate-Only Targeting Passes Promotion
The `_detect_non_transferable()` check allows recipes where up to 40% of steps are coordinate-only. For multi-step recipes (5-10 steps), having even 2-3 coordinate-only steps is enough to break the recipe when layout shifts.

### Problem 3: No Focus-Before-Type Guarantee
The recipe executor's `type` action calls `page.keyboard.type(value)` which types into whatever element has focus. But focus depends on prior steps executing correctly AND the browser maintaining focus between steps. If a prior click step targeted by coordinates misses, the wrong element has focus and typed text goes nowhere.

### Problem 4: Assertion Timing Mismatch
The 6×200ms polling window (1.2s) for `expect_code_visible` is too short for many challenges. The sidecar typically takes 5-10s to observe results, giving the DOM much more time to settle.

---

## Time Cost Analysis

Recipe failures in Run 2 cost **~55 seconds** total:

| Step | Recipe | Time Wasted |
|------|--------|-------------|
| 7 | delay_memory_v2 | ~3s (soft fail) |
| 10 | drag_drop_v2 | 10.3s |
| 16 | gesture_v2 | 0.4s |
| 18 | puzzle_solve_v2 | 7.3s |
| 19 | puzzle_solve_v2 | 6.0s |
| 25 | conditional_reveal_v2 | 6.8s |
| 27 | sequence_v2 | 4.4s |
| 28 | calculated_v2 | 6.0s |
| 30 | websocket_v2 | 7.6s |
| **Total** | | **~52s** |

Meanwhile, tier 3 recipes saved ~30-40s (steps 6+7 if delay_memory had worked). Net recipe system value: **negative** in this run.

---

## Recommended Fixes (Priority Order)

### Fix 1: Aggressive Auto-Demotion (HIGH IMPACT, LOW EFFORT)
Demote any tier-1 recipe to tier 0 after **2** consecutive failures (currently 3). This prevents 4-5 wasted attempts per run. Also demote recipes that match challenge types they weren't promoted for (detection mismatch guard).

### Fix 2: Increase Code Visibility Polling (HIGH IMPACT, LOW EFFORT)
Change `expect_code_visible` assertion from 6×200ms (1.2s) to 10×300ms (3.0s). This would likely fix puzzle_solve and calculated failures where the code simply needs more time to appear.

### Fix 3: Fix Drag to Use Playwright's Built-In (HIGH IMPACT, MEDIUM EFFORT)
Replace the HTML5 DragEvent JS dispatch with `page.drag_and_drop(source_selector, target_selector)`. Challenge: recipe stores coordinates, not selectors. Options:
- At replay time, find element at coordinates via `page.evaluate('document.elementFromPoint(x,y)')`, extract a selector, then use `page.drag_and_drop()`
- Or store both coords AND a selector during promotion

### Fix 4: Deduplicate puzzle_solve/calculated Detection (MEDIUM IMPACT, LOW EFFORT)
Add discriminating keywords: puzzle_solve should have "puzzle" as PRIMARY, calculated should have "calculated" as PRIMARY. Ensure they don't both match the same challenges. Or use config-type matching (step→type map) instead of keyword detection for recipe selection.

### Fix 5: Skip Recipe When Stale State Detected (MEDIUM IMPACT, LOW EFFORT)
Before replaying a recipe, check if progress is already > 0. If so, the challenge has stale React state from a previous step — skip recipe replay entirely as the recipe's assertions will fail (they expect delta from 0).

### Fix 6: Focus Target Before Typing (MEDIUM IMPACT, LOW EFFORT)
In the `type` action handler, if a target is found via `_find_target()`, click it first to establish focus before typing. This prevents typed text going to the wrong element.

### Fix 7: Require 2 Successful Replays for Tier 2+ (LOW IMPACT, LOW EFFORT)
Don't promote from tier 1 to tier 2+ until the recipe has 2+ successful replays. This prevents bad recipes from accumulating replay attempts.

---

## Bottom Line

The recipe system is **net negative** for tier-1 recipes. The 6 tier-3 recipes are valuable and save real time. The fix priority should be:
1. Stop wasting time on known-bad recipes (Fix 1 + Fix 5)
2. Give working recipes more time to succeed (Fix 2)
3. Fix drag actions (Fix 3) — this would add 3 more reliable recipes
4. Fix detection (Fix 4) — prevents wrong recipe from running

If these 4 fixes are implemented, estimated recipe hit rate would improve from 11% to ~40-50%.
