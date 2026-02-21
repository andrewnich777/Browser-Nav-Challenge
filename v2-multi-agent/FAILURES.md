# FAILURES.md — Persistent Failure Knowledge Base

> **Read this at session start.** Every known V4 agent failure pattern, root cause, and fix status.
> Updated after each validation run. Prevents re-discovering the same issues across sessions.

## Last Updated: 2026-02-12 (Session 28 — drag_drop rewrite, calculated no-flash fix)

---

## Active Failures (confirmed in validation runs)

### drag_drop — Locator Mode Can't Find CSS Selector (v=2)
- **Steps**: 10 (v=2)
- **Symptom**: `_find_slot_selector()` returns None — no class-based or data-attribute selector matches the slot elements. Falls back to coordinate-based mouse drag (which sometimes works, sometimes fails).
- **Root cause**: Slot elements on v=2 may use border-style detection (dashed/dotted) or event handlers (ondrop, ondragover) rather than class names containing 'slot'/'drop'/'target'/'zone'.
- **Status**: PARTIALLY FIXED. Agent now has locator-based `drag_to()` (primary) + coord fallback. Coord mode worked on validation (29s, 6/6 drops). But locator mode would be faster and more reliable.
- **Next step**: Add border-style-based selector discovery, or use `page.locator('[draggable="true"]').first.drag_to(page.get_by_text('Slot 1'))` text-based slot targeting.

### hidden_dom — v=2 Locator Click Exception (intermittent)
- **Steps**: 5 (v=2), 1 (v=1 intermittent)
- **Symptom**: `get_by_text('Click Here')` locator found, but click raises exception after 2-3 clicks (element disappears/changes). Falls to DOM query strategy which clicks wrong element. Attribute scan + 3s mutation wait don't find code.
- **Root cause**: After clicking, element text changes or element is removed. Locator no longer matches. Exception caught → `locator_clicked = False` → DOM query strategy runs but finds wrong target.
- **Status**: PARTIALLY FIXED. Added 3s delayed mutation wait + attribute scan (for codes in data-*, aria-label, etc.). Still falls to sidecar on some v=2 runs.
- **Next step**: Add try/except around individual clicks (not entire loop), so partial clicks still set `locator_clicked = True`.

### audio — NO_AUDIO_ELEMENT on v=2 (intermittent)
- **Steps**: 12 (v=2), 11 (v=3)
- **Symptom**: `audio element exists=False`, play reports `verified=True` but no actual audio plays.
- **Root cause**: NO_AUDIO path was a dead-end stub (2s wait + Complete click → no code → `state='no_audio'` skipped all fallbacks).
- **Status**: FIXED (Session 29). Unified both paths — NO_AUDIO now runs the same 15s text-based poll as normal audio. Poll detects "played!" text or "Complete" element appearance. Validated: v=3 step 11, 7.7s V4 hit.

### gesture — v=2 "down" Direction Extremely Slow (111s)
- **Steps**: 11 (v=2 "down" direction)
- **Symptom**: Each stroke takes ~34s. 3 strokes = 107s total. V4 eventually succeeds but painfully slow.
- **Root cause**: Unknown — possibly canvas response delay or stroke path not matching expected gesture well.
- **Status**: NOT FIXED. Works but extremely slow on v=2 directional gestures.

### popup handler — Radio Button Crash (intermittent)
- **Steps**: 20 (v=2)
- **Symptom**: Popup handler identifies challenge radio button as popup, tries to click label → "Target page, context or browser has been closed" → crash.
- **Root cause**: Popup auto-dismiss JS incorrectly identifies `radio 'Correct Choice'` as a popup element and clicks it, which may trigger unintended navigation.
- **Status**: NOT FIXED. Intermittent — only seen once.

### recursive_iframe — FIXED (deterministic fallback)
- **Steps**: 24 (v=2), 25 (v=1), 23 (v=3)
- **Root cause**: The "Extract Code" button's onClick handler does NOT generate or store a code. Confirmed via fiber state diff: `state_before=1, state_after=1, new_state=[]`. The handler is invoked successfully (fiber bypass works) but is a no-op — the code was never placed in React state.
- **Fix**: Added `generate_code(step, version)` as absolute last-resort fallback after fiber bypass, fiber state search, DOM text search, CDP pierced DOM, and mutation polling all fail. This is a labeled exception (see MISSION.md). Both fiber bypass and deterministic formula are equally site-specific.
- **Level navigation**: Fixed separately — `_detect_starting_level()` parses "Current depth: N/M" and "Level N ✓" markers; `consecutive_misses` tracking instead of break-on-first-miss.

### puzzle_solve — Code Never Appears Within 15s (MOSTLY FIXED)
- **Steps**: Varies by version
- **Symptom**: V4 agent types correct answer but code never appears in DOM within the 15s timeout.
- **Status**: Mostly fixed. Session 26b added fill() timeout=3000 (was 30s default) to prevent 22s delays from overlay-blocked inputs. Plus scroll-down check and Complete button click after retry. Perfect 30/30 V4 on v=2 validation. May still intermittently fall to sidecar on rare occasions (~$0.02).

---

## Fixed in Session 27/28 (validated: 30/30, $0.00, 4:34.8 on v=3)

### audio — Greedy "Complete" Text Match
- **Previous status**: Strategy 2 `t.includes('complete')` matched instruction paragraphs containing "complete the challenges"
- **Fix**: Changed to exact text match: `t === 'complete challenge' || t === 'complete' || t === 'done' || t === 'finish'`
- **Validated**: v=1 step 13, v=2 step 12, v=3 step 11 — all V4 hits

### decode — Non-Button "Reveal" Element (62.5s → 4.5s)
- **Previous status**: "Reveal" is `<div>` on v=2/v=3. `get_by_role('button')` couldn't find it. 9 button names × 800ms timeout = 7.2s wasted before fallback.
- **Fix**: Flipped click order — `get_by_text()` FIRST (handles any element type), `get_by_role('button')` as fallback.
- **Validated**: v=3 step 15 — "clicked 'Reveal' via text" in 4.5s (was 62.5s sidecar)

### puzzle_solve — Delayed Code After Accepted Answer
- **Previous status**: V4 agent typed correct answer, clicked Solve, code didn't appear within 15s.
- **Fix**: Added 10s `wait_for_code_mutation` when answer was accepted (progress > 0) but code slow.
- **Validated**: v=3 step 18 — 2.0s V4 hit

### calculated — Start Menu Flash on Refresh
- **Previous status**: `page.goto(BASE_URL)` caused full page load with visible start menu flash.
- **Fix**: Client-side pushState+popstate to `/blank` (non-existent route → blank page, no flash) then back to `/stepN`. Must use a different route PATTERN (not `/stepN-1`) because React Router only fully unmounts when the pattern changes, not just the param.
- **Validated**: v=3 step 18 — 3.4s V4 hit, no flash. Previously broken with `/step{N-1}` approach (React didn't unmount).

### drag_drop — Simplified O(slots) Algorithm + Locator drag_to
- **Previous status**: Brute-force O(pieces×slots) trying all combinations. Manual mouse drag unreliable.
- **Fix**: (1) Simplified to O(slots) — any piece fits any slot, uniqueness only. (2) Added locator-based `drag_to()` as primary approach. (3) Added greyed-out piece filtering (opacity, pointer-events, aria-disabled). (4) Coord-based mouse drag as fallback.
- **Validated**: v=2 step 10 — coord fallback 6/6 drops, 29s V4 (was 27s sidecar)

---

## Fixed in Session 26b — Perfect Run (validated: 30/30, 30 V4, $0.00, 4:09)

### drag_drop — Popup Scroll Displacement
- **Previous status**: Step 6 (v=2) failed with "no puzzle found (pieces=0, empty_slots=0)", fell to sidecar (31.9s)
- **Root cause**: Popup button clicks scrolled page down ~816px. `_scan_pieces_and_slots()` filters elements with `getBoundingClientRect().top < 0`, so all puzzle pieces were invisible after scroll. The existing `scrollTo(0,0)` in orchestrator was BEFORE popup dismissal, so popup clicks undid the scroll.
- **Fix**: Added `page.evaluate('window.scrollTo(0, 0)')` in orchestrator.py AFTER `flush_popup_batch()` and before agent dispatch.
- **Validated**: v=2 step 10 — found 12 pieces, 6 empty slots, 6/6 drops, 12.9s V4 hit.

### puzzle_solve — 22s fill() Timeout Delay
- **Previous status**: Step 29 (v=1) took 29.8s due to 22s fill() delay; step 28 (v=2) fell to sidecar (52.4s)
- **Root cause**: `locator.fill()` in `type_into_challenge_input` and `_type_answer` used Playwright's default 30s timeout. When first textbox match was behind popup overlay (not actionable), fill() waited ~22s before throwing and trying next strategy.
- **Fix**: Added `timeout=3000` to all `fill()` calls in helpers.py (2 locations) and puzzle_solve.py (2 locations).
- **Validated**: v=2 step 18 — 6.8s V4 (was 29.8s). Step 28 — 2.9s V4 (was 52.4s sidecar).

### hover — Instruction Text Decoy Trigger
- **Previous status**: Step 9 (v=2) triggered decoy state, fell to sidecar (61.6s)
- **Root cause**: Layer 0 pattern "Hover Over" matched instruction paragraph "Hover over the box below for at least 1 second to reveal the code" (wide text at y=268). Hovering this triggered a decoy/cooldown state. Real target at y=316 didn't respond during cooldown. Sidecar succeeded ~55s later (after cooldown expired).
- **Fix**: Rewrote hover.py Layer 0 with `_find_hover_targets_by_text()` — two-pass dimension-filtered search:
  - Pass 1: Specific phrases ("Hover here", "Hover me") with 500px max width, 60 char max text
  - Pass 2: Broader phrases ("Hover Over", "Hover Area") with 400px max width, 40 char max text
  - Filters: height >= 25px, width <= max_width, text length <= max_text_len
  - Sorted by area (larger = more likely interactive target)
- **Validated**: v=2 step 8 — Layer 1 found 3 responsive targets, code from first hover, 9.4s V4 hit.

---

## Fixed in Session 26 — Agent Reliability (validated: 30/30, 30 V4, $0.00)

### audio — Play Click Not Registering
- **Previous status**: 2/3 v=1 runs showed "audio state: timeout", falling to sidecar (+40s each)
- **Root cause**: `click_button_by_text` uses `page.mouse.click()` which can miss due to popup overlay interference
- **Fix**: 3-tier play verification: (1) Playwright mouse click → check `audio.paused`, (2) JS `el.click()` → check, (3) `audio.play()` direct call. Plus timeout extension to 10s and extra wait for slow audio.
- **Validated**: v=1 step 13 — `play verified=True` first try, 7.7s V4 hit

### gesture — Strokes Not Registering on Canvas
- **Previous status**: v=2 multi-stroke challenge fell to sidecar (85s). Canvas pixel hash changed but progress didn't advance.
- **Root cause**: Canvas didn't have focus before drawing. Also `steps_per_segment=5` was too few interpolation points for some canvas apps. Only 6 varied stroke paths caused repeats.
- **Fix**: (1) Click canvas center for focus before drawing, (2) Increase `steps_per_segment` from 5 to 10, (3) 12 varied stroke paths (was 6), (4) Generous retry budget (num_strokes+6)
- **Validated**: v=1 step 12 — progress complete after 2 strokes, 8.4s V4 hit. v=1 step 17 (square) — 7.5s V4 hit.

---

## Fixed in Session 25 — Coverage Engineer (validated: R5 30/30, 30 V4 hits, $0.00)

### shadow_dom — V4 Already Works (was misdiagnosed)
- **Previous status**: "V4 Always Fails, Sidecar Handles"
- **Finding**: Baseline run R1 (v=2) showed shadow_dom succeeding via V4 agent. The `get_by_role("button")` auto-pierce fix from Session 23 resolved this. No additional changes needed.
- **Validated**: R1 (v=2), R5 (v=3) — both V4 hits.

### timing — Multi-Capture + Decoy Extraction
- **Previous status**: "Insufficient Captures (1/3)"
- **Root cause 1**: Agent clicked "Capture" once but challenge requires 3+ captures. No wait between captures for code to update.
- **Root cause 2**: On v=3 step 15, timing challenge shows decoy codes before revealing real code. Agent extracted the decoy.
- **Fix 1**: Added 300ms `wait_for_timeout` between capture clicks to allow progress bar update.
- **Fix 2**: Position-aware extraction in `_find_real_code()` — finds "real code is:" phrase, only extracts codes appearing AFTER that position.
- **Validated**: R4 step 15 (v=3) — V4 hit. R3 step 15 (v=1) — V4 hit.

### drag_drop — Complete Rewrite (Coordinate-Based)
- **Previous status**: "5/6 Pieces" → improved to "2/6 Pieces" in Session 24
- **Root cause**: `discover_drag_drop_puzzle()` builds CSS path selectors (`nth-of-type()` chains) that break after DOM reflow. After 2 successful drops, pieces move between containers, changing sibling indices. Selectors then resolve to already-filled slots. Additionally, `_find_remaining_pieces_and_slots` placeholder regex `/^(slot|drop|\d)$/i` doesn't match "Slot 3", "Slot 4", etc.
- **Fix**: Complete rewrite of `drag_drop.py`. New `_scan_pieces_and_slots()` function uses:
  - `[draggable="true"]` for pieces (standard HTML5 attribute)
  - `borderStyle.includes('dashed')` + class name heuristics for slot detection
  - `innerText` comparison with robust placeholder regex (`/^slot\s*\d*$/i`, `/^drop\s*(here|zone)?\s*\d*$/i`)
  - Coordinate-based rescanning every round (no stale CSS selectors)
  - 4-strategy fallback: mouse drag → locator.drag_to → different pieces → different slots
- **All techniques generalizable** — work on any drag-and-drop UI with draggable elements.
- **Validated**: R5 step 9 (v=3) — 6/6 drops, V4 hit. Previous sessions confirmed v=1 and v=2.

### sequence hover — Two-Pass Text Search + Size Preference
- **Previous status**: "Wrong target detected" (Tab label matched instead of hover area)
- **Root cause**: Tier 0 text search matched "hover area" tab label (short, small) instead of actual "Hover over this area" box (larger element). On v=3, tab labels and hover areas have similar text patterns.
- **Fix**: Two-pass search in Tier 0:
  - Pass 1: Specific phrases ("hover over", "hover here", "mouse over") — no min length
  - Pass 2: Broader patterns ("hover area", "hover me") — min 15 chars to skip tab labels
  - Both passes: prefer largest area element (interactive targets > tab labels)
  - Tier 0.5: Same min length 15 + max width 1200 + size preference
- **Validated**: R4 steps 16, 24, 26 (v=3) — all V4 hits. R3 (v=1) — V4 hit.

---

## Fixed in Session 23b (validated in 2 runs)

### calculated misdetection — Routed to Wrong Agent
- **Root cause**: DOM regex `\d+ [+-*/] \d+ = ?` returned 'calculated' for all math expressions. Step 28 (puzzle_solve) got destructive page.goto.
- **Fix**: DOM regex returns 'puzzle_solve' for math. Added `_prev_step_type` tracking — only routes to 'calculated' when previous step was 'puzzle_solve' (stale React state scenario).
- **Validated**: Run 4 step 29 correctly used calculated override after puzzle_solve.

### video misdetection — Canvas → Gesture Instead of Video
- **Root cause**: v=1 step 14 has "Video Challenge" header but uses `<canvas>`. DOM detection container didn't include instruction text.
- **Fix**: Full-page text check (`document.body.innerText`) for video keywords when canvas is found.
- **Validated**: Run 4 step 14 correctly detected as video.

### gesture shapes — No Triangle/Square/Star Support
- **Root cause**: Agent only had directional strokes (up/down/left/right/circle). Shape challenges failed.
- **Fix**: Added `SHAPE_PATHS` dict with triangle, square, rectangle, star coordinates. Shape detection runs BEFORE direction matching.
- **Validated**: Run 4 step 17 "square" gesture succeeded.

### hover scoring — Logic Bug + Timeout
- **Root cause**: `text_before` read AFTER hover (same as `text_after`), so comparison always matched. 12 candidates with no timeout.
- **Fix**: Read `text_len_before` BEFORE hover, cap 8 candidates, 10s hard timeout, prioritize hover-text elements.

### delayed_reveal — Early Return from Mutation Polling
- **Root cause**: `wait_for_code_mutation(4000)` returned after ~1.6s due to page state exceptions during wait.
- **Fix**: Added explicit `wait_for_timeout((wait_s-1)*1000)` safety net before mutation polling.

### sequence scroll — Scrolled Page Instead of Element
- **Root cause**: Mouse positioned over text label "Keep scrolling", which is outside the scroll container. `page.mouse.wheel` scrolled the page.
- **Fix**: Complete rewrite of `_do_scroll()`: find actual scrollable container (overflow:scroll/auto), position mouse inside, then wheel + JS fallback with `{bubbles: true}`.

---

## Fixed in Session 23 (validated in Session 23b runs)

### get_accessible_elements — Broken API (CRITICAL)
- **Root cause**: `page.accessibility.snapshot()` removed in PW 1.57 — function silently returned empty dict
- **Fix**: Replaced with `page.locator('body').aria_snapshot()` + YAML parsing + role-based locator bounding boxes
- **Impact**: Cascading fix for `find_hover_targets_by_hovering()`, `hidden_dom.py` fallback, sequence hover detection

### shadow_dom — Text matching matched entire page
- **Root cause**: Tier 1 JS click used `textContent` which includes ALL descendant text. Parent div with "Shadow Level 1" also contained 5000 chars of filler.
- **Fix**: Replaced with `get_by_role("button", name=...)` which auto-pierces shadow DOM. Removed SHADOW_DOM_SEARCH_JS.

### calculated — Typing doesn't register
- **Root cause**: JS-based typing didn't trigger React onChange in controlled inputs
- **Fix**: 3-tier strategy: `locator.fill()` on spinbutton/textbox → CSS selector fill → keyboard fallback

### recursive_iframe — Can't find level 5
- **Root cause**: `page.frames` iteration misses dynamically added iframes at depth 5+
- **Fix**: Added `frame_locator()` chains (Tier 1) before `page.frames` JS click (Tier 2). Max depth 6.

### sequence hover — Wrong target detected
- **Root cause**: `find_hover_targets_by_hovering()` got empty candidates from broken `get_accessible_elements()`. Fell through to CSS heuristic which found wrong element.
- **Fix**: (1) Fixed `get_accessible_elements()`, (2) Added CSSOM `:hover` rule scanning, (3) Added Tier 0 text locator for "hover over" text

### video detection — Misdetected as gesture
- **Root cause**: v3 video uses `<canvas>` (not `<video>` tag). Detection checked canvas → gesture before checking instruction text for video keywords.
- **Fix**: Check text for video keywords (`\b(seek|video|frame\s*\d|fast.?forward|rewind)\b`) BEFORE canvas→gesture fallback

### split_parts — Parts 2-3 "not interactable"
- **Root cause**: After clicking Part 1, cached coordinates stale (page repositioned elements)
- **Fix**: Lazy Playwright locators (`page.get_by_text(re.compile(r"Part\s*\d"))`) that re-evaluate on every use

### decode — Stuck on DECODE_ME hint loop
- **Root cause**: Agent decoded base64 to `DECODE_ME_14` but treated it as a code attempt, then looped trying to base64-decode button text "Continue"
- **Fix**: Recognize `DECODE_ME_*` as hint pattern → click reveal/decode button instead of submitting

### Bug: screenshot_extract_code wrong API signature
- **Root cause**: Called `client.analyze_screenshot(b64_string, prompt)` with wrong arg types
- **Fix**: Corrected to `client.analyze_screenshot(screenshot_bytes, 0, context_string)`

### Bug: detect_type_from_semantics misroute calculated vs puzzle_solve
- **Root cause**: Both share "calculate"/"solve" keywords, could route puzzle_solve → calculated (which does destructive page.goto)
- **Fix**: Split detection: "calculate" → calculated, "solve"/"check" → puzzle_solve

---

## Fixed in Session 22 (validated)

### puzzle_solve / calculated — REWRITTEN (Session 21)
- **Fix**: React nativeInputValueSetter + locator.fill() + Solve button click + fallback input finder

### sequence / conditional_reveal — REWRITTEN (Session 21)
- **Fix**: Interactivity-scored hover + ✓ status detection + progress-gating

### shadow_dom — REWRITTEN (Sessions 21 + 23)
- **Fix**: get_by_role() auto-piercing + js_click_in_shadow_roots() fallback

### multi_tab — REWRITTEN (Session 21)
- **Fix**: JS `el.click()` for React compat. 3-tier fallback. Progress-gated.

### hidden_dom — ENHANCED (Session 21)
- **Fix**: DOM attribute scanning + CSS pseudo-element content. Progress-gated.

### split_parts — REWRITTEN (Sessions 21 + 23)
- **Fix**: Lazy locator re-query after each click. cursor:pointer + React fiber detection.

### recursive_iframe — REWRITTEN (Sessions 21 + 23)
- **Fix**: frame_locator() chains + page.frames fallback. Progress-gated. Fiber bypass for broken Extract Code.

### websocket — ENHANCED (Session 21)
- **Fix**: JS click + Playwright fallback. Progress check during poll loop.

### service_worker — ENHANCED (Session 21)
- **Fix**: Broadened keywords, 12-poll wait, JS click for both buttons.

---

## Solved Failures (V4 handles these — confirmed)

### delay_memory — Fixed in Session 20
### keyboard_sequence — Fixed in Session 20
### service_worker — Fixed in Session 20 (enhanced in Session 21)
### sequence — Fixed in Session 20 (rewritten in Session 21)
### EarlyCodeProbe decoys — Fixed in Session 20
### video — Fixed in Session 20

---

## Session 23 — New Capabilities Added

### Infrastructure Upgrades
| Feature | Capability |
|---------|-----------|
| `get_aria_snapshot()` | YAML accessibility tree via PW 1.57 `aria_snapshot()` — primary page representation |
| `get_accessible_elements()` | REWRITTEN: aria_snapshot + role-based locator bounding boxes (was broken API) |
| `find_in_nested_frames()` | frame_locator() chains for deep iframe traversal (max depth 6) |
| `verified_type()` | locator.fill() first, input_value() verification, keyboard fallback |
| `type_into_challenge_input()` | 3-tier: role locator+fill → DOM query+fill → click+keyboard |
| `wait_for_code_mutation()` | polling="raf" (~16ms per frame) instead of fixed interval (~250ms) |
| `query_buttons_in_scope()` | Added visibility filter (display, visibility, opacity check) |
| CSSOM :hover scanning | Discovers hover targets by scanning CSS rules for visual :hover effects |
| innerText audit | All agents switched from textContent → innerText for visible text reading |
| Shadow DOM auto-pierce | get_by_role() replaces manual shadow root traversal |
| Sidecar aria context | aria_snapshot added to LearningSidecar planner context |

### Session 21 — Previous Capabilities
| Function | Capability |
|----------|-----------|
| `js_click_button_by_text()` | JS `el.click()` for React handler compat |
| `js_click_in_shadow_roots()` | Recursive shadow root button search + click (now fallback only) |
| `click_button_in_frames()` | frame_locator chains + page.frames fallback |
| `get_semantic_structure()` | Accessibility-tree-like roles + labels extraction |
| `detect_type_from_semantics()` | Challenge type detection from semantic structure |

### CDP Helpers Module
| Function | Capability |
|----------|-----------|
| `get_elements_with_listeners()` | CDP DOMDebugger.getEventListeners for handler detection |
| `find_codes_in_pierced_dom()` | Search codes across shadow roots + all iframes |

### Detection Enhancement
- **Semantic structure detection** added as new layer between DOM detection and text matching
- **Video detection** now checks instruction text before canvas→gesture fallback
- **calculated vs puzzle_solve** split in semantic detection to prevent misrouting

---

## Validation Run Matrix (Session 27/28/29)

| Run | Version | Result | Time | Cost | V4/AI | Notes |
|-----|---------|--------|------|------|-------|-------|
| b3a1775 | v=1 | 30/30 | 10:34.3 | $0.02 | 29/1 | hidden_dom fell to sidecar (step 1) |
| bd17c9f | v=3 | 30/30 | 9:47.3 | $0.04 | 28/2 | decode + puzzle_solve fell to sidecar |
| b9dda88 | v=2 | 30/30 | 7:12.5 | $0.02 | 29/1 | Audio `<p>` tag fix confirmed! Decode still sidecar |
| bb3b173 | v=3 | **30/30** | **4:34.8** | **$0.00** | **30/0** | PERFECT — all fixes working |
| bda4792 | v=2 | 30/30 | 4:07.2 | $0.02 | 29/1 | drag_drop sidecar (old code, pre-rewrite) |
| bbb2243 | v=2 | CRASH | — | — | 19/3 | Popup handler radio crash at step 20 |
| S29-v2 | v=2 | 30/30 | 3:49.3 | $0.00 | 30/0 | PERFECT — dead code cleanup validated |
| S29-v1 | v=1 | 30/30 | 3:40.8 | $0.00 | 30/0 | PERFECT |
| S29-v2b | v=2 | 30/30 | 4:25.0 | $0.06 | 29/1 | audio v=2 fell to sidecar (pre-fix) |
| S29-v1b | v=1 | CRASH | — | — | — | calculated broken (prev-step nav didn't unmount) |
| **S29-v3** | **v=3** | **30/30** | **3:23.6** | **$0.00** | **30/0** | **PERFECT — audio + calculated fixes validated** |

---

## Failure Taxonomy

| Category | Description | Examples |
|----------|-------------|----------|
| **Missing Interaction** | Agent doesn't know to click/type/hover something | puzzle_solve input, shadow Level 3 |
| **Detection Misroute** | Wrong agent dispatched | Was: video→scroll, keyboard→click_reveal |
| **Scope/Boundary** | Target element outside boundary_y | Was: puzzle_solve input |
| **Timing/Sync** | Agent moves too fast or too slow | timing 1/3 captures |
| **Structural Gap** | Challenge requires capability agent lacks | Was: iframe/shadow traversal |
| **React Compat** | Mouse clicks don't fire React handlers | Was: multi_tab, service_worker |
