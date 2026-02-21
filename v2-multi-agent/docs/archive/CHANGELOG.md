# Changelog - Session Fixes

## Session 23b: 2026-02-11 — Slow-Step Fixes + Detection Hardening (Latest)

### Overview
Two validation runs: 29/30 on v=2 ($0.46, 613.2s) and 29/30 on v=1 ($0.66, 771.7s). V4 hit rate: 20 (up from 16). Remaining failure: recursive_iframe (stale fiber state). Fixed 6 slow-step agents, hardened calculated/video detection, rewrote sequence scroll targeting.

### Validation Results
| Run | Version | Score | Cost | Time | V4 Hits | Failure |
|-----|---------|-------|------|------|---------|---------|
| Run 3 (v=2) | v=2 | 29/30 | $0.46 | 613.2s | 20 | Step 24 recursive_iframe (stale fiber code) |
| Run 4 (v=1) | v=1 | 29/30 | $0.66 | 771.7s | 20 | Step 25 recursive_iframe (fiber bypass no code) |

### 6 Slow-Step Fixes (>20s V4 agent time)

| Fix | File | Problem | Solution |
|-----|------|---------|----------|
| A: hover scoring | helpers.py | `find_hover_targets_by_hovering()` tested 12 candidates with no timeout, logic bug read text_before AFTER hover | Cap 8 candidates, 10s hard timeout, prioritize hover-text/cssom targets, read text_len_before BEFORE hover |
| B: gesture shapes | gesture.py | No shape path data for triangle/square/rectangle/star | Added SHAPE_PATHS dict with normalized coordinates, shape detection runs BEFORE direction matching |
| C: delayed_reveal wait | delayed_reveal.py | `wait_for_code_mutation` returned early (~1.6s) due to page state exceptions | Added explicit `wait_for_timeout((wait_s-1)*1000)` safety net before mutation polling |
| D: decode hint loop | decode.py | After DECODE_ME hint detection, `continue` tried 10+ noise candidates | Changed `continue` → `break` to exit b64_candidates loop |
| E: sequence scroll | sequence.py | Scroll event didn't bubble, React didn't detect it | Added `{bubbles: true}` to dispatched scroll Event |
| J: split_parts stall | split_parts.py | Parts below viewport not found | Proactive `page.mouse.wheel(0, 400)` when progress stalls |

### Detection Hardening

| Fix | File | Problem | Solution |
|-----|------|---------|----------|
| calculated misdetection | orchestrator.py | Step 28 (puzzle_solve) routed to calculated agent (destructive page.goto) | DOM regex returns 'puzzle_solve' for math expressions; added `_prev_step_type` tracking — only route to 'calculated' when prev was 'puzzle_solve' |
| video misdetection | orchestrator.py | v=1 step 14 "Video Challenge" with canvas misrouted to gesture | Full-page text check for video keywords when canvas found (container may exclude instruction header) |
| semantic detection | helpers.py | `detect_type_from_semantics()` returned 'calculated' for any math input | Changed to always return 'puzzle_solve' for math inputs (safe default, no page.goto) |

### Infrastructure Fixes

| Fix | File | Problem | Solution |
|-----|------|---------|----------|
| recursive_iframe stale codes | recursive_iframe.py, context.py | Fiber state search returned codes from previous steps | Added `used_codes` field to StepCtx, filter in `_search_fiber_state_for_code()` |
| calculated boundary_y | calculated.py | After page.goto refresh, old boundary_y is stale | Use `boundary_y=99999` after refresh |
| sequence scroll targeting | sequence.py | Mouse positioned over text label, scrolled page instead of element | Complete rewrite: find actual scrollable container (overflow:scroll/auto), position mouse inside, then wheel + JS fallback |

### Files Modified (8)

| File | Changes |
|------|---------|
| `agents/v4/helpers.py` | hover scoring fix (cap 8, timeout, text_before bug), semantic detection always returns puzzle_solve |
| `agents/v4/challenges/gesture.py` | SHAPE_PATHS dict, shape detection before direction matching |
| `agents/v4/challenges/delayed_reveal.py` | Minimum wait safety net before mutation polling |
| `agents/v4/challenges/decode.py` | `continue` → `break` after DECODE_ME hint |
| `agents/v4/challenges/sequence.py` | scroll Event bubbles:true, complete _do_scroll rewrite |
| `agents/v4/challenges/split_parts.py` | Proactive scroll on progress stall |
| `agents/v4/challenges/recursive_iframe.py` | used_codes filter for stale fiber state |
| `agents/v4/challenges/calculated.py` | boundary_y=99999 after refresh |
| `agents/v4/context.py` | Added `used_codes` field to StepCtx |
| `orchestrator.py` | calculated→puzzle_solve DOM regex, _prev_step_type tracking, video full-page text check |

### Known Remaining Issues
- **recursive_iframe**: Fiber bypass still fails on some versions — fiber state may not contain code
- **shadow_dom**: V4 always fails, sidecar handles reliably — V4 should learn from sidecar approach
- **Sidecar learning gap**: V4 agents don't learn from sidecar's successful strategies (user feedback)
- **Needs clean validation run**: Session ended before clean run could complete

---

## Session 23: 2026-02-11 — PW 1.57 Migration + Research-Driven Upgrades

### Overview
Run 2 scored 29/30 on v=3 ($0.66, 986.5s). V4 solved 16 steps, sidecar 12, Phase 4 /finish saved step 30. One failure: step 23 (recursive_iframe). Critical discovery: `page.accessibility.snapshot()` was removed in PW 1.57 — `get_accessible_elements()` silently returned empty results. Two-stage fix: Stage 1 fixes 8 specific failures, Stage 2 applies research-driven infrastructure upgrades.

### Stage 1: 8 Failure Fixes

| Fix | File | Root Cause | Solution |
|-----|------|-----------|----------|
| 1.1 `get_accessible_elements()` | helpers.py | PW 1.57 removed `page.accessibility.snapshot()` | `aria_snapshot()` + YAML parsing |
| 1.2 shadow_dom text matching | shadow_dom.py | `textContent` matched 5000-char parent div | `get_by_role("button")` auto-pierces shadow DOM |
| 1.3 calculated typing | calculated.py | JS typing didn't trigger React onChange | `locator.fill()` on spinbutton/textbox |
| 1.4 recursive_iframe depth | recursive_iframe.py | `page.frames` missed depth 5+ iframes | `frame_locator()` chains (max depth 6) |
| 1.5 sequence hover | sequence.py, helpers.py | Empty candidates from broken API | Tier 0 text locator + CSSOM :hover scanning |
| 1.6 video detection | orchestrator.py | Canvas→gesture before video keywords | Regex word boundary check before canvas test |
| 1.7 split_parts stale coords | split_parts.py | Cached coordinates stale after clicks | Lazy Playwright locators re-evaluate each use |
| 1.8 decode hint loop | decode.py | DECODE_ME_* treated as code attempt | Pattern recognition → click reveal button |

### Stage 2: Infrastructure Upgrades

| Item | Files | Description |
|------|-------|-------------|
| 2.1 aria_snapshot | helpers.py, sidecar | `get_aria_snapshot()` as primary page representation, added to sidecar context |
| 2.2 Visibility filter | helpers.py | `query_buttons_in_scope()` filters display:none, visibility:hidden, opacity:0 |
| 2.3 locator.fill() | helpers.py, puzzle_solve, calculated, sequence | Primary typing strategy via `verified_type()` |
| 2.4 frame_locator() | helpers.py, recursive_iframe | `find_in_nested_frames()` helper + upgraded `click_button_in_frames()` |
| 2.5 Shadow auto-pierce | shadow_dom.py | `get_by_role()` replaces manual shadow traversal |
| 2.6 Lazy locators | split_parts, mutation | Re-evaluate on use, survive DOM changes |
| 2.7 Scroll upgrade | sequence.py | Bottom verification + bubbling scroll event |
| 2.9 Mutation polling | helpers.py | `wait_for_code_mutation()` polling="mutation" (~16ms vs ~1000ms) |
| 2.10 Dynamic task order | sequence.py | `_detect_task_order()` from instruction text |
| 2.13 innerText audit | 10+ files | textContent→innerText for visible text reading |
| 2.14 CSSOM safety | helpers.py | Per-sheet try/catch for cross-origin stylesheets |

### Bug Fixes (Post-Implementation)

| Bug | Severity | Fix |
|-----|----------|-----|
| `screenshot_extract_code()` wrong API args | HIGH | `(screenshot_bytes, 0, context_string)` |
| `get_aria_snapshot()` unused boundary_y | LOW | Removed parameter |
| `_do_scroll()` pre-scroll scrollTop mutation | LOW | Removed JS assignment before mouse.wheel |
| `_detect_task_order()` broad "text" match | INFO | Specific "type text"/"type here" matching |
| `detect_type_from_semantics()` misroute | INFO | Split calculated vs puzzle_solve detection |
| Redundant `import re as _re` | INFO | Removed from sequence.py |

### Skipped Items
- **2.8 Clock API**: Can't install mid-page; mutation polling (2.9) gives most benefit
- **2.12 page.console_messages()**: Mission violation — humans can't see console output
- **2.11 Set-of-Mark validation**: Already working correctly per MEMORY.md

### Files Modified (26)

| File | Changes |
|------|---------|
| `agents/v4/helpers.py` | get_aria_snapshot, get_accessible_elements rewrite, find_in_nested_frames, verified_type rewrite, type_into_challenge_input rewrite, wait_for_code_mutation rewrite, visibility filter, CSSOM scanning, innerText audit, frame_locator chains, semantic detection fix |
| `agents/v4/challenges/shadow_dom.py` | get_by_role auto-pierce, removed SHADOW_DOM_SEARCH_JS |
| `agents/v4/challenges/calculated.py` | locator.fill() 3-tier typing |
| `agents/v4/challenges/recursive_iframe.py` | frame_locator chains, innerText |
| `agents/v4/challenges/sequence.py` | Tier 0 text hover, locator.fill(), scroll upgrade, dynamic task order |
| `agents/v4/challenges/split_parts.py` | Lazy locator re-query, innerText |
| `agents/v4/challenges/decode.py` | DECODE_ME hint recognition |
| `agents/v4/challenges/mutation.py` | get_by_role lazy locators |
| `agents/v4/challenges/puzzle_solve.py` | locator.fill() for answer typing |
| `agents/v4/challenges/hover.py` | innerText audit |
| `agents/v4/challenges/hidden_dom.py` | innerText audit |
| `orchestrator.py` | Video detection regex fix, innerText |
| `agents/learning_sidecar.py` | aria_snapshot in planner context |

---

## Session 22: 2026-02-11 — Validation + Stale State Fix

### Overview
Session 21 validation + targeted fixes. Removed fiber_bypass from non-iframe agents, added stale state guard for V4 agent dispatch.

### Changes

#### Fix 1: Stale state guard for V4 agents (`orchestrator.py`)
- **Problem:** Back-to-back puzzle challenges (puzzle_solve → calculated) leave stale React state — progress > 5% at step start confuses V4 agents
- **Fix:** `_system1_v4()` checks `read_progress()` before dispatch; waits up to 2s (8×250ms) for transition; skips V4 if stuck
- **Pattern:** Same guard as `recipe_executor.py` line 142, extended with retry loop

#### Fix 2: Removed fiber_bypass from shadow_dom agents (`shadow_dom.py`, `step30_shadow_dom.py`)
- **Problem:** fiber_bypass should ONLY be used for recursive_iframe (the one demo challenge with intentionally broken buttons)
- **Fix:** Removed fiber_bypass import+call from `shadow_dom.py` and `step30_shadow_dom.py`
- **step30_shadow_dom** now returns None, falling through to orchestrator's `/finish` handler

#### Fix 3: Removed React fiber state reset from `_step_setup()` (`orchestrator.py`)
- **Problem:** Dispatching into React hooks (`hook.queue.dispatch("")`) caused white screens (violates read-only JS invariant)
- **Fix:** Entire fiber dispatch block (lines 795-820) removed in Session 21 validation run 1

### Files Modified
| File | Changes |
|------|---------|
| `orchestrator.py` | Stale state guard in `_system1_v4()`, fiber reset removal in `_step_setup()` |
| `agents/v4/challenges/shadow_dom.py` | Removed fiber_bypass import + fallback |
| `agents/v4/challenges/step30_shadow_dom.py` | Removed fiber_bypass import + fallback, returns None for /finish |

---

## Session 21: 2026-02-10 — V4 Agent Overhaul

### Overview
Major rewrite of 10 V4 challenge agents + 20 new helper functions + CDP helpers module + semantic detection layer. Informed by research into React internals (nativeInputValueSetter, fiber tree), shadow DOM traversal, CDP DOMDebugger API, and Playwright frame API.

### Agent Rewrites (10)

| Agent | Key Fix | Technique |
|-------|---------|-----------|
| `shadow_dom` | Levels 2+ inside shadow roots | `js_click_in_shadow_roots()` recursive traversal |
| `step30_shadow_dom` | Generic keywords → specific "Shadow Level N" | Shadow-root-aware clicking |
| `multi_tab` | `page.mouse.click` misses React handlers | `_js_click_tab()` with JS `el.click()`, 3-tier fallback |
| `hidden_dom` | Codes in DOM attributes, not visible text | `scan_dom_attributes_for_code()` (data-*, title, aria-label, CSS ::before/::after) |
| `split_parts` | Parts are non-button clickable elements | `_find_all_clickable_parts()` with cursor:pointer + React fiber onClick detection |
| `recursive_iframe` | Button clicks only search main page | `click_button_in_frames()` via Playwright `page.frames` API |
| `websocket` | Connect button click unreliable | JS click + Playwright fallback, hook code polling |
| `service_worker` | Retrieve button keyword too narrow | Broadened keywords, 12-poll (6s) wait, JS click for both buttons |
| `sequence` | Hover sub-task picked wrong target | Interactivity-scored hover (cursor:pointer +5, background +2, transition +3), ✓ status detection |
| `puzzle_solve` | Input below boundary, first type gets "Wrong" | React nativeInputValueSetter + Ctrl+A retry + triple-click for number inputs |

### New Helper Functions (20+)

| Function | File | Purpose |
|----------|------|---------|
| `js_click_button_by_text()` | helpers.py | JS `el.click()` for React handler compat |
| `js_click_in_shadow_roots()` | helpers.py | Recursive shadow root button search + click |
| `click_button_in_frames()` | helpers.py | Playwright `page.frames` iframe traversal |
| `scan_dom_attributes_for_code()` | helpers.py | data-*, title, aria-label, CSS pseudo-elements |
| `get_semantic_structure()` | helpers.py | Accessibility-tree-like roles + labels |
| `detect_type_from_semantics()` | helpers.py | Challenge type from semantic structure |
| `wait_for_animation_end()` | helpers.py | CSS transitionend/animationend events |
| `is_interactable_at()` | helpers.py | Full interactability check (visibility + pointer-events + opacity + disabled + hit test) |
| `find_real_target_at()` | helpers.py | elementsFromPoint overlay pierce |
| `click_and_verify_progress()` | helpers.py | Playwright click → JS click retry if no progress |
| `complete_challenge_sweep()` | helpers.py | Click all completion buttons, check for code |
| `find_interactive_elements()` | helpers.py | React fiber + CSS + ARIA multi-signal detection |

### CDP Helpers Module (NEW: `agents/v4/cdp_helpers.py`)

| Function | Purpose |
|----------|---------|
| `_get_cdp(page)` | CDP session management with health checks |
| `get_elements_with_listeners()` | CDP DOMDebugger.getEventListeners for handler detection |
| `find_codes_in_pierced_dom()` | Search codes across shadow roots + all iframes |

### Detection Enhancement
- **Semantic structure detection** added as new layer between DOM detection and text matching in `_detect_type_for_v4()`
- Extracts interactive element roles + labels (zero filler text noise)
- Scoped to challenge area via `compute_challenge_scope()` boundary_y

### Bug Fixes
- `type_react_native`: pass dict `{sel, val}` to `page.evaluate` (was broken `arguments[0]`)
- `detect_type_from_semantics`: missing `boundary_y` parameter
- `sequence.py`: local import fixed

---

## Session 20: 2026-02-10 — Agent Reliability Fixes

### Overview
Fixed 6 specific V4 agent failures identified during Session 19 validation runs.

### Fixes
| Issue | Fix |
|-------|-----|
| `delay_memory` agent failure | Fixed timing/capture logic |
| `keyboard_sequence` agent failure | Fixed key sequence detection |
| `service_worker` agent failure | Fixed button keyword matching |
| `sequence` agent failure | Fixed hover target selection |
| `EarlyCodeProbe` decoy false positives | Added challenge type exclusion list |
| `video` agent failure | Fixed video playback detection |

---

## Session 19: 2026-02-10 — Validation Runs

### Overview
Validation runs for Session 18 changes. Confirmed 30/30 on v=1 and v=2.

### Results
| Run | Version | Score | Recipe Rate | Cost | Time |
|-----|---------|-------|-------------|------|------|
| Run 1 | v=2 | **30/30** | 74% | $0.80 | 1190s |
| Run 2 | v=1 | **30/30** | 57% | $0.58 | 657s |

---

## Session 18: 2026-02-10 — Recipe Executor Bottleneck Fixes

### Overview
Fixed 3 systemic timing bottlenecks in `recipe_executor.py` that caused recipes taking 10-33s even when working correctly. Deleted 5 structurally broken recipes.

### Changes

#### Fix 1: `expect_code_visible` polling reduced (`recipe_executor.py`)
- **Was:** 15×400ms = 6s polling budget
- **Now:** 8×300ms = 2.4s polling budget
- Added fiber_code short-circuit: if `_fiber_code` is set and valid (6 chars), return immediately
- Added final harvest before giving up: one last `harvest_and_score` call after polling loop, before falling to completion sweep
- Added `self._fiber_code = None` at top of each step iteration to prevent stale code leaks between steps

#### Fix 2: Removed auto-click-Solve from type handler (`recipe_executor.py`)
- **Deleted:** Lines that searched for Solve/Calculate/Submit/Check button after resolver typing
- **Problem:** `solve_btn.first.click(timeout=2000)` triggered locator handler loop → 30s wall clock
- **Reason safe:** puzzle_solve and calculated auto-submit on keystroke across all 3 versions

#### Fix 3: `wait_for_code` poll interval reduced (`recipe_executor.py`)
- **Was:** 400ms poll interval → ~12.5 polls in 5s budget
- **Now:** 250ms poll interval → 20 polls in 5s budget (faster detection, same total budget)

#### Fix 4: Deleted 5 broken recipes (`knowledge/learnings.json`)
- `mutation_v1` (0/2): 10 steps, only gets 3-4/5 mutations — structurally impossible
- `mutation_v2` (0/3): 10 steps, only gets 3-4/5 mutations — structurally impossible
- `mutation_v3` (0/1): 7 steps, only gets 3-4/5 mutations — structurally impossible
- `service_worker_v1` (0/1): Step 0 clicks wrong target
- `multi_tab_v2` (0/2): 7 steps, structurally broken

### Expected Impact
- Calculated/puzzle_solve recipes: ~5s → <3s
- Any recipe with `expect_code_visible`: saves 3.6s per assertion
- Eliminated 30s stall on resolver-type steps
- 67 learnings remaining (was 72)

---

## Session 17b: 2026-02-10 — Popup-Recipe Interference Fixes

### Overview
Fixed popup-recipe interference causing strict mode errors and false assertion failures.

### Changes
- Pre-step popup drain before every recipe step baseline capture
- Progress assertion retry on failure: dismiss popups + wait 300ms + re-read
- Locator handler trigger excludes header + uses `.first` for strict mode safety
- **VALIDATED**: 30/30 on v1 and v2, zero strict mode errors

---

## Session 17: 2026-02-10 — Adaptive Timeouts + Bug Sweep

### Overview
Adaptive action-dependent timeouts, stability window improvements, and 6 bug fixes.

### Changes
- Adaptive timeouts: click/type 800ms, hover 2500ms, default 2000ms
- Stability window: 200ms quiet after last change
- Delay learning: EMA-updated settle times
- CRITICAL: replay_successes deferred to post-verification
- Bug fixes: stale _fiber_code, __getRecentCodes timestamp, _last_detection_gap, delete_learning save, should_sweep condition
- Recipe sweep: 8 broken recipes deleted

---

## Session 16: 2026-02-10 — Recipe Robustness Overhaul

### Overview
Implemented 6 complementary techniques to reduce recipe replay failures (target: step-0 failures from ~25% to <5%). Informed by HybridSimilo (98.8% element relocalization), Agent-E DOM distillation, and AgentRR check functions research. Plus bug fixes from comprehensive 4-agent review.

### Changes

#### Fix 1: Challenge Boundary Filtering (`recipe_executor.py`)
- `_get_challenge_boundary_y(page)`: 3-strategy fallback (code input form → Submit button → filler marker) to find Y boundary below which elements are decoys
- `_is_in_challenge_area(center_y, boundary_y)`: simple center-Y check
- `_pick_in_challenge_area()`: inner function in `_find_target` that iterates up to 5 candidates from each cascade, preferring challenge-area elements. "Prefer not require" — falls back to first match if all below boundary.
- Pattern cascade JS updated to accept `[pat, bY]` array and sort results by `_inChallenge` flag

#### Fix 2: HybridSimilo Element Fingerprinting (`recipe_executor.py`, `learning_sidecar.py`)
- `target_fingerprint` field on `ActionStep` dataclass: `{tag, text, role, aria_label, neighbor_keywords, size}`
- `_fingerprint_match(page, fingerprint, boundary_y)`: single JS eval scores all interactive elements against fingerprint (tag +0.15, text +0.30, role +0.10, ARIA +0.15, neighbors +0.20, size +0.05, challenge area +0.05). Returns best match if score > 0.30.
- New cascade step 3.5 between CSS selector and DNA query in `_find_target`
- Fingerprint capture in `_action_to_recipe_step()` from BID catalog / locator data

#### Fix 3: Completion Sweep on Step-0 (`recipe_executor.py`)
- Both failure paths now allow sweep when `progress >= 0.5` even on step 0 (was `i > 0` only)
- Safe: `_step_setup` already skips recipes if progress > 0.05 at start

#### Fix 4: Populate dom_signals + dna_signatures (`orchestrator.py`, `learning_sidecar.py`, `knowledge_reader.py`)
- `StepContextSnapshot.dom_signals` field: captured at step start via JS eval detecting canvas/video/audio/draggable/iframe/contenteditable/range/select/textarea
- `finalize_promotion()` populates `dom_signals` and aggregates `dna_signatures` from snapshot
- Re-enabled `_compute_dom_score()` and `_compute_dna_score()` in detection scoring
- 6-channel scoring: kw=0.25, flags=0.10, text_ctx=0.15, dom=0.10, dna=0.10, fp=0.20

#### Fix 5: Update Context on Replay Success (`knowledge_reader.py`, `orchestrator.py`)
- `record_replay_success()`: lightweight context update (text_context + fingerprint merge) after successful replay
- `_merge_fingerprint()`: union-based fingerprint merging (keeps higher element counts)
- Called from orchestrator replay-success path after code submission

#### Fix 6: Parameterized High-Level Actions (`recipe_executor.py`, `learning_sidecar.py`)
- `params` field on `ActionStep` dataclass
- 3 new action types: `click_until_progress` (repeated clicks until threshold), `type_and_submit` (type + auto-submit/Enter), `wait_for_code` (poll for code appearance)
- Recipe collapsing in `finalize_promotion()`: N identical clicks → single `click_until_progress` step

#### Bug Fixes (from 4-agent comprehensive review)
- **Unused `boundary_y_cup`** in `click_until_progress` handler (redundant `page.evaluate()`)
- **`hit_width`/`hit_height` never populated**: BID resolution now stores `box['width']`/`box['height']` on `hit_info` for fingerprint size data
- **`classes` missing from `get_locator_cascade`**: Added `classes` field to `info()` helper in `primitives.py`, fixing dead CSS selector fallback in `_action_to_recipe_step()`

### Files Modified
| File | Changes | LOC |
|------|---------|-----|
| `recipe_executor.py` | Boundary filtering, fingerprint matching, completion sweep, parameterized actions | ~95 |
| `learning_sidecar.py` | Fingerprint capture, action collapsing, dom_signals/DNA population, hit_width/height | ~40 |
| `knowledge_reader.py` | 6-channel scoring, record_replay_success, merge_fingerprint | ~30 |
| `orchestrator.py` | dom_signals on snapshot, record_replay_success call | ~10 |
| `primitives.py` | classes field in get_locator_cascade | ~1 |

**Total: ~176 LOC across 5 files. NEEDS VALIDATION RUN.**

---

## Session: 2026-02-09 — Self-Improving Detection

### Overview
Implemented 5-part self-improving detection system to reduce recipe misrouting (keyboard_sequence matched as hidden_dom, etc.). Addresses the ~38% recipe miss rate from wrong type matching. Zero-cost at runtime (all deterministic), compounding learning across runs.

### Changes

#### 1. Action-Signature Penalty (`knowledge_reader.py`)
- New `_action_signature_penalty()` method: hard penalty when recipe needs DOM features (canvas, audio, draggables, tabs) absent from live page
- Instruction text matching: soft penalty when page instruction keywords contradict recipe type (e.g., page says "press Ctrl+V" but recipe is hidden_dom)
- Asymmetric: -0.10 for missing own keywords, -0.20 when page matches a *different* type's keywords
- Normalized instruction (collapsed whitespace, stripped punctuation, length gate < 8 chars)

#### 2. Confusion Recording (`knowledge_reader.py`, `orchestrator.py`)
- New `confused_with` field on CanonicalLearning: tracks which types get misrouted to each other
- `record_confusion()` stores at base-type level (transfers across v1/v2/v3)
- Routing-error-only guard: only records confusion when `sig_penalty > 0` (structural impossibility) or early failure (step 0-1) with low score/gap
- Capped at 5 confusion entries per learning, sorted by count

#### 3. Confusion Penalty in Detection (`knowledge_reader.py`)
- Before sorting candidates, applies escalating penalty for types with confusion history
- Scaling: 1→0.05, 2→0.12, 3+→0.25 per confusion pair, cap 0.30 total
- Age decay: confusions older than 7 days have effective count reduced by 1

#### 4. AI Keywords via Layer C (`agents/learning_sidecar.py`)
- `_ai_review_recipe()` now returns `tuple[list[dict] | None, list[str]]` — recipe + keywords
- KEYWORDS_JSON sentinel in AI prompt: deterministic parsing of 3-8 discriminative keywords
- Confusion context: if type has confusion history, prompt instructs distinguishing keywords
- Keywords merged with heuristic-generated detection keywords at promotion

#### 5. Score Histogram Logging (`knowledge_reader.py`, `orchestrator.py`)
- Detection scoring logs top1/top2 type, scores, and gap on every detection call
- `SCORE_HISTOGRAM` log lines on recipe hits and System 2 fallbacks
- Enables threshold calibration: grep `SCORE_HISTOGRAM` after a run to validate thresholds
- `_last_detection_gap` cached on KnowledgeReader for routing guard

#### 6. Recipe Failure Step Tracking (`agents/recipe_executor.py`)
- `_last_failure_step` and `_last_failure_total` set on failure for routing mismatch detection
- Orchestrator reads these to distinguish routing errors (step 0-1) from execution flakiness (step 2+)

### Files Modified
| File | Changes | LOC |
|------|---------|-----|
| `knowledge_reader.py` | `confused_with` field + persist/load, `record_confusion()`, `_action_signature_penalty()`, confusion penalty with decay, `_last_sig_penalty` + `_last_detection_gap` caching, score histogram | ~90 |
| `orchestrator.py` | `_system1_attempted_type/score/gap/routing_mismatch`, routing guard, confusion recording, SCORE_HISTOGRAM | ~30 |
| `agents/recipe_executor.py` | `_last_failure_step` + `_last_failure_total` tracking | ~10 |
| `agents/learning_sidecar.py` | `_ai_review_recipe()` return type + KEYWORDS_JSON + confusion context, keyword merging | ~25 |

### Verification
- Import check: `python -c "from orchestrator import Orchestrator; print('OK')"` — PASS
- Compile check: all 4 files pass `py_compile` — PASS
- Bug check: 3 independent reviews found 0 bugs across all modified sections

---

## Session: 2026-02-08 — Recipe Diagnostic + 7 Targeted Fixes

### Overview
Comprehensive recipe system diagnostic after benchmark runs showed 11% recipe hit rate (1/9 on v=2 30/30 run). Identified 5 root causes of tier-1 recipe failures. Implemented 7 targeted fixes. Full report in `docs/RECIPE_DIAGNOSTIC.md`.

### Benchmark Results
| Run | Version | Score | Recipe Hits | Recipe Rate | Cost |
|-----|---------|-------|-------------|-------------|------|
| Run 1 | v=1 | 10/30 | 2 | 50% | $0.30 |
| Run 2 | v=2 | **30/30** | 1 | 11% | $1.20 |

### Root Causes Identified
1. **Drag: React ignores untrusted DragEvents** — JS-dispatched events have `isTrusted=false`
2. **Assertion timing too short** — 6x200ms (1.2s) misses codes needing 2-3s to appear
3. **Stale React state** — Progress > 0 at step start causes all delta assertions to fail
4. **Detection cross-contamination** — puzzle_solve keywords match calculated challenges
5. **Hover missing coord fallback** — Unlike click handler, hover had no target_coords path

### Fixes Applied (7)

#### Fix 1: Aggressive Auto-Demotion (orchestrator.py)
- Changed from `replay_attempts >= 3` to `consecutive_failures >= 2` for tier 0 demotion
- Both startup cleanup and runtime demotion updated

#### Fix 2: Code Visibility Polling (recipe_executor.py)
- `expect_code_visible` assertion: 10x300ms = 3s (was 6x200ms = 1.2s)
- Gives puzzle_solve/calculated codes time to appear after Solve click

#### Fix 3: Drag Uses Playwright Native (recipe_executor.py)
- Replaced HTML5 DragEvent JS dispatch with `page.drag_and_drop()`
- Builds CSS selectors from `elementFromPoint` at runtime (nth-of-type path)
- Playwright uses CDP `Input.dispatchDragEvent` = trusted events

#### Fix 4: Stale State Guard (recipe_executor.py)
- Before recipe execution, checks `read_progress(page)`
- If progress > 5%, skips recipe entirely (React state leak from previous step)
- Prevents wasting 5-10s on doomed assertion checks

#### Fix 5: Focus Before Type (recipe_executor.py)
- `type` action now clicks target to establish focus before `keyboard.type()`
- Falls back to target_coords if no locator match
- Prevents typed text going to wrong element

#### Fix 6: Hover Coords Fallback (recipe_executor.py)
- Hover action now falls back to `target_coords` when locator cascade finds nothing
- Matches existing click handler behavior

#### Fix 7: Detection Cross-Contamination Guard (orchestrator.py)
- Keyword-based detection (`detect_and_get`) now only runs for generic/simple steps (1-15)
- Steps 16-30 with specific config types use direct lookup only
- Prevents puzzle_solve recipe matching calculated challenge

### Previous Session Fixes (carried forward)
- Recipe overwrite guard (skip finalize_promotion when recipe_executor already ran)
- Drag_drop exempted from non_transferable detection
- Target_text stripped from drag_drop recipes during promotion
- 80 noise keywords stripped from all recipes + generator
- decode_v2/v3, sequence_v3 demoted to tier 0

---

## Session: 2026-02-07 — Recipe Overhaul + Recursive Iframe Probe

### Overview
Recipe promotion overhaul (A1-A4) to produce leaner, faster recipes. Added recursive iframe diagnostic probe script (B1). Fixed 3 bugs found during review.

### A1: Refined Causal Filter (learning_sidecar.py)
- Wait handling now uses 3-tier logic: (1) own positive signals, (2) targetability-only from next entry (`became_clickable`, `enabled`, `appeared`, `revealed`), (3) drop
- Scroll actions kept only if `scroll_moved == True` (content actually repositioned)
- Added `_TARGETABILITY_SIGNALS` set and `_get_targetability_changes()` helper

### A2: Two-Step Lookback with Flailing Guard (learning_sidecar.py)
- Pass 2 now keeps i-1 and i-2 setup steps before each causal step
- i-2 only kept if it's click/hover/focus/scroll (not another wait)
- Flailing guard: if 3+ consecutive non-causal before causal, caps at last 2
- At most ONE neutral wait per causal step in setup

### A3: Auto Scroll-to-Target (recipe_executor.py)
- `_find_target()` calls `scroll_into_view_if_needed(timeout=2000)` when element found by locator but `bounding_box()` returns None
- Makes explicit scroll recipe steps optional — elements auto-scroll into view

### A4: Adaptive Click Recovery (recipe_executor.py)
- New `_scroll_recover()` method: scrolls down 500px then up 500px, re-resolving target after each
- Only for not-found click failures, once per step, before aborting recipe

### B1: Recursive Iframe Probe (`_debug/probe_recursive_iframe.py`)
- Systematic probe: keyboard, click, dblclick, pointer sequence, container scroll, hover
- Reports depth changes + state changes after each interaction
- Element stack inspection at container centers (overlay detection)
- Browser stays open for manual testing

### Bug Fixes (3)
- **MEDIUM**: Wait-dedup in Pass 2 was cross-contaminated — scanned ALL setup indices against ALL causal steps. Fixed: only scan setups within the gap between previous and current causal step.
- **LOW**: Probe script `clickables` undefined if JS evaluate throws. Fixed: initialize before try block.
- **LOW**: Probe script crashes on error dict from `get_containers()`. Fixed: filter error dicts.

---

## Session: 2026-02-07 (Night) — Reliability Plan + Coverage Engineer Audit

### Overview
Implemented 8-fix Reliability & State-Awareness Plan, then performed comprehensive coverage engineer audit (data flow verification + exhaustive bug review across 6 files). Fixed 19 bugs total (3 CRITICAL, 4 HIGH, 12 MEDIUM).

### Reliability Plan (8 Fixes)
1. **Aggressive Tier Demotion**: T3→T2 at 3 failures (was 7), T2→T1 at 2 failures (was 5). Retroactive cleanup: 16 demotions applied.
2. **Startup Migration**: Unversioned recipes (tier≥2, 3+ successes) cloned to versioned entries at startup. 4 entries qualified.
3. **scroll_container_js**: React-compatible scrolling via `el.scrollTop` + scroll event dispatch. Replaces broken `mouse.wheel()` for element_scroll.
4. **Anti-Loop State Awareness**: drain-before/drain-after pattern. `turned_green`, `became_clickable` etc. now count as meaningful effects in anti-loop gate.
5. **Promotion Lint Whitelist**: Added `element_scroll`, `draw`, `canvas_draw`, `double_click`, `focus` to SUPPORTED_TYPES.
6. **State-Aware Recipe Execution**: `expect_state_changes` field in recipe steps. `_wait_for_state_settle()` polls for changes instead of fixed wait.
7. **Recipe Cleanup**: `_detect_non_transferable()` marks coord-only recipes as non_replayable. 9 cleanups applied.
8. **Recursive Iframe Diagnostic**: JS diagnostic dump for recursive_iframe challenges.

### Style-Aware Code Scoring (Fix 9)
- Temporal stability tracker, state change causality tracker
- Color bucket scoring: green +0.10, red -0.15, vibrancy +0.05
- Stability bonus (up to +0.20), causality bonus (up to +0.15)
- JS harvest enhanced with `colorCat()`/`isVibrant()` functions

### Coverage Engineer Audit — Data Flows (Task #13)
All 11 data flows verified end-to-end across sidecar, orchestrator, recipe executor, code scorer.

### Coverage Engineer Audit — Bug Fixes (Task #14)

**CRITICAL (3 fixed):**
- knowledge_reader.py: Seed migration archived unversioned recipe even when no replacement was created. Fixed: only archive if `local_seeded > 0`.
- knowledge_reader.py: Seed migration replaced ALL variants of existing entry. Fixed: only replace active variant, preserve others.
- orchestrator.py: Tier demotion changes never persisted to disk (happened after `record_failure._save()`). Fixed: added `_save()` after demotion.

**HIGH (4 fixed):**
- primitives.py: `draw_stroke_on_canvas` returned `success=True` even when verification failed. Fixed: `success = stroke_verified or progress_changed`.
- learning_sidecar.py: element_scroll fallback added `w/2, h/2` to already-centered coords. Fixed: use `el['x'], el['y']` directly.
- learning_sidecar.py: Locator cascade computed BEFORE BID resolution (x/y not yet set). Fixed: moved to AFTER `_execute_action`.
- recipe_executor.py: `run_agent` passed `step=0, version=0` to sub-agents. Fixed: store/pass actual values.

**MEDIUM (12 fixed):**
- orchestrator.py: Tier-2 demotion didn't reset `consecutive_failures`. Fixed.
- code_scorer.py: `colorBucket` preferred bg over fg, missing green text on neutral bg. Fixed: prefer non-grey color.
- recipe_executor.py: Unbound `score, code` in `expect_code_visible` loop. Fixed: initialize before loop.
- recipe_executor.py: `_extract_code_after_recipe` missing try/except on page.evaluate. Fixed.
- recipe_executor.py: Score threshold mismatch (0.3 in assertion vs 0.4 in extraction). Aligned to 0.3.
- primitives.py: `repeat_action_until_signal` swallowed first progress increment (missed 1-step completion). Fixed.
- learning_sidecar.py: element_scroll fallback used falsy check on coords (0 is valid). Fixed to `is not None`.
- learning_sidecar.py: Strategy bandit double-credited same strategy across rounds. Fixed: reset after reward.
- knowledge_reader.py: `record_success` silently dropped when variant not found. Added logging.
- knowledge_reader.py: `record_failure` silently dropped when variant not found. Added logging.
- knowledge_reader.py: `record_success` silently ignored unknown challenge_type. Added logging.
- knowledge_reader.py: `record_failure` silently ignored unknown challenge_type. Added logging.

### Architectural Fix-ups (4 additional)
1. **nearInstruction**: Changed from `el.innerText` (all descendants, always true) to scoped parent text (200 chars, childNodes < 20 guard). Scoring now discriminates.
2. **Frequency/uniqueness scoring**: Removed inert +0.15 "unique" boost (frequency always 1 due to JS dedup). Added comment explaining why.
3. **switch_tab/switch_frame**: Added WARNING logs that these are no-ops for execution context. Added try/except around index parsing.
4. **knowledge_reader dom_change_score**: Replaced MD5 hash + hex diff (binary: ~0 or ~0.5) with structural comparison matching base.py approach (tag counts 70% + other features 30%). Now proportional.

### Files Modified
- `orchestrator.py`: Tier demotion save + consecutive_failures reset
- `primitives.py`: draw_stroke success, repeat_action first progress
- `agents/recipe_executor.py`: run_agent args, unbound vars, try/except, threshold alignment, switch_tab/frame warnings
- `agents/learning_sidecar.py`: locator timing, element_scroll coords/falsy, strategy bandit
- `code_scorer.py`: colorBucket preference, nearInstruction scope, frequency scoring
- `knowledge_reader.py`: Seed migration safety (3 fixes), silent drop logging (4 additions), dom_change_score proportional

---

## Session: 2026-02-07 (Late) — Primitives Enhancement Plan (11 Iterations) + Bug Fixes

### Overview
Implemented the full 11-iteration Primitives Enhancement Plan (designed by 5-model consensus + ChatGPT review). 6 functionality iterations + 5 efficiency iterations across 5 files (~410 LOC). Then fixed 3 discovered bugs and implemented 2 additional suggestions.

**Goal:** 23/30 → 26-28/30, -30% time (350s → ~250s), -25% API cost.

### Part A: Functionality (6 iterations)

#### Iteration 1: Transport Layer Code Capture (~65 LOC)
- **init_hooks.py**: postMessage + ServiceWorker message listeners in INIT_SCRIPT (500-char length cap, noise filter)
- **orchestrator.py**: `page.on("response")` scans fetch/xhr/document responses for 6-char codes (content-length pre-check, URL filter, dedup set per step)
- **orchestrator.py**: `page.on("console")` scans console messages for codes (500-char cap)
- **Wiring**: Transport codes checked in `_passive_checks()` as steps 3+4. Buffers cleared per-step in `_step_setup()`.

#### Iteration 2: Canvas Stroke Verification + Retry (~40 LOC)
- **primitives.py**: Rewrote `draw_stroke_on_canvas()` with pixel hash verification (sampled `getImageData` every 97th pixel)
- 2px wiggle after mousedown to trigger drawing initiation
- After stroke: compare hash. If identical → retry with ±3px jitter (max 2 retries)
- Cross-origin taint fallback to `read_progress()` verification
- Returns `strokes_verified` count

#### Iteration 3: Broadened `read_progress()` (~45 LOC)
- **primitives.py**: Added 3 new fallback passes after existing Pass 2:
  - Pass 3: `<progress>` elements and `[role="progressbar"]` (aria-valuenow/aria-valuemax)
  - Pass 4: Percentage text patterns (`(\d+)\s*%\s*(?:complete|done|progress|filled)?`)
  - Pass 5: Stepper dots / `[aria-current="step"]` / `.step.active` / `.step.completed`

#### Iteration 4: `scroll_container_until()` (~50 LOC)
- **primitives.py**: New composite primitive. Scrolls inside container (by selector or BID) with adaptive behavior.
- Stops on: code found, progress detected, scroll-end (scrollTop+scrollHeight unchanged), or budget (max_scrolls=8)
- Detects lazy-loading via scrollHeight changes
- Returns `{scrolled, code, progress_delta, new_elements, stopped_by}`

#### Iteration 5: `hover_reveal_extract()` (~55 LOC, expanded with Suggestion 5)
- **primitives.py**: Systematic hover with adaptive timing.
- Auto-shortlists from `annotate_elements()` + `find_challenge_region()`, filters by interactable + non-decoy
- **Adaptive hold**: Starts at 800ms, polls state_changes every 200ms during hold, breaks early on reveal
- After 2 consecutive no-reveals, extends to full hold_ms (2000ms) for remaining elements
- Checks `extract_code_js()` mid-hold for early exit

#### Iteration 6: Scoped Container Interaction (~60 LOC, enhanced with Suggestion 2)
- **primitives.py**: `interact_in_container()` for styled-div fake iframes (recursive_iframe blocker)
- Resolves target child by BID or text match, handles scrollable containers
- **Two-attempt click strategy**: mouse.click first → check for effect via state_changes → JS pointer event dispatch fallback (full React 18 compatible sequence)
- **orchestrator.py**: Phase 2.5c container fallback + Phase 2.5d hover-reveal wired in

### Part B: Efficiency (5 iterations)

#### Iteration 7: Truncate Multi-Turn Message History (~25 LOC)
- **vision_learning.py**: `_trim_messages_for_api()` keeps last 2 full rounds, collapses older rounds (drop images, truncate text to 200 chars)
- **Expected impact**: -40% API latency on rounds 3+, -30% per-call cost

#### Iteration 8: Skip Redundant Page Info (~20 LOC)
- **vision_learning.py**: `_extract_page_info_minimal()` — only gets text, progress, codes, scroll_y, viewport_h when `element_catalog` already exists
- Skips full button/clickable/input/interactive scans (~50-100ms savings per round)

#### Iteration 9: DOM Challenge Classifier (~40 LOC)
- **primitives.py**: `classify_challenge_dom()` — lightweight JS archetype hint (~5ms)
- Archetypes: canvas_draw, drag_drop, scroll_box, timer_reveal, nested_levels, form_fill, hover_reveal, multi_click, unknown
- **learning_sidecar.py**: Injected into sidecar context on round 0 (confidence >0.5)
- **vision_learning.py**: Passed through to planner as soft hint (never hard-gates)

#### Iteration 10: Early Code Check (~15 LOC)
- **learning_sidecar.py**: Fast `extract_code_js()` immediately after each action, before heavy `_harvest_code()`
- Short-circuits if valid code found and progress complete

#### Iteration 11: Wait Time Reductions (~15 LOC)
- **learning_sidecar.py**: click 200→100ms, scroll 300→200ms, press 300→150ms, sweep 800→400ms/500→250ms, sweep click 350→200ms
- **vision_learning.py**: post-action 250→150ms
- **Expected impact**: ~500ms/round × 3 rounds = ~1.5s per unsolved step

### Bug Fixes (3)

1. **Double-read of `extra` in propose_actions** (`vision_learning.py`): `extra = context.get('_page_info_extra', {})` read twice redundantly. Removed duplicate.
2. **Missing post-type wait** (`learning_sidecar.py`): `type` action handler had no `wait_for_timeout()` after `keyboard.type()`. Added 150ms settle wait.
3. **content-length ValueError** (`orchestrator.py`): `int(cl)` could throw on non-numeric content-length headers. Wrapped in targeted try/except instead of relying on outer catch-all.

### Suggestion Implementations (2)

4. **Suggestion 2 — Container click reliability** (`primitives.py`): `interact_in_container()` now tries mouse.click first, checks for effect via state_changes, falls back to full JS pointer event dispatch (pointerdown→mousedown→pointerup→mouseup→click) directly on resolved element. Targets recursive_iframe "Extract Code" button.
5. **Suggestion 5 — Adaptive hover hold times** (`primitives.py`): `hover_reveal_extract()` rewritten with polling loop (200ms intervals), early break on state_change detection, code check mid-hold. Adaptive hold starts at 800ms, extends to 2000ms after 2 no-reveal elements. Saves ~8-12s when hovering many elements with fast reveals.

### Files Modified

| File | Changes |
|------|---------|
| `init_hooks.py` | postMessage + SW message listeners (~20 LOC JS) |
| `orchestrator.py` | page.on("response"/"console"), transport code scanning in _passive_checks, Phase 2.5c/2.5d, content-length bug fix (~45 LOC) |
| `primitives.py` | Canvas verification, read_progress broadening, scroll_container_until, hover_reveal_extract (adaptive), interact_in_container (JS fallback), classify_challenge_dom (~290 LOC) |
| `agents/vision_learning.py` | Message trimming, minimal page info, archetype passthrough, wait reduction, duplicate bug fix (~60 LOC) |
| `agents/learning_sidecar.py` | Early code check, wait reductions, classifier wiring, type wait bug fix (~30 LOC) |
| **Total** | **~445 LOC across 5 files** |

---

## Session: 2026-02-07 — Recipe System Overhaul + New Primitives

### Recipe System Bug Fixes (11 bugs)
1. **dest_coords schema** (`recipe_executor.py`, `learning_sidecar.py`): Added explicit `dest_coords` field for drags. Source in `target_coords`, destination in `dest_coords`. Eliminates drag-to-self and drag-to-(0,0) bugs.
2. **(0,0) default elimination**: Changed missing coord default from `(0,0)` to `None` in `_action_to_recipe_step`. Added near-origin guard (abs < 2px) in `_find_target`, drag execution, and all executors.
3. **Dead enrichment removal** (`learning_sidecar.py`): Removed 23-line dead block in `finalize_promotion` that read `locator`/`hit` fields that never exist.
4. **Dedup tightening** (`recipe_executor.py`): `compress_recipe` now compares 8 fields (was 3): action_type, target_selector, target_text, target_role, target_name, value, target_coords, dest_coords.
5. **Recipe health check** (`recipe_executor.py`, `orchestrator.py`): `validate_recipe()` catches (0,0) coords, drags without destination, press/type without value. Runs before every replay. Corrupted recipes get tier=0.
6. **BID drag resolution** (`vision_learning.py`): Drag now resolves `source_id`/`target_id` BIDs to coords (was ignored, fell through to (0,0)).
7. **Partial BID validation** (`learning_sidecar.py`): Drag fallback validates both endpoints non-None and non-near-origin. Logs which side is missing.
8. **Progress None preservation** (`learning_sidecar.py`): `progress_before`/`progress_after` preserved as None (was defaulting to 0), preventing false "causal action" detection.
9. **Harvest threshold alignment** (`learning_sidecar.py`): Lowered from 0.5 to 0.4 to match orchestrator/code_scorer thresholds.
10. **Frame click logging** (`learning_sidecar.py`): Frame click exceptions now logged with error details instead of silently swallowed.
11. **Canvas clamping** (`learning_sidecar.py`): Selects nearest canvas to drag source (was always first canvas).
12. **3 corrupted recipes reset** (`learnings.json`): drag_drop_v1, v2, v3 set to tier=0 (had target_coords=[0,0], value="0,0", repeat=6-7).

### Action Validity Gate (NEW)
- `validate_action()` in `primitives.py` — shared pre-execution gate for sidecar + vision executor
- Checks: drag endpoints resolved + not near-origin, draw has canvas + path, click/hover not outside challenge region, press/type has value
- Wired into both `LearningSidecar._execute_action()` and `VisionLearningAgent._execute_action()`

### New Primitives
1. **`draw_stroke_on_canvas()`** (`primitives.py`): Canvas drawing with normalized coordinates (0.0-1.0). Resolves canvas by BID or selector, converts to absolute, draws with mouse.move interpolation, verifies progress.
2. **`discover_interactive_containers()`** (`primitives.py`): Finds scrollable/nested styled containers with interactive children. For "fake iframe" patterns (step 24 blocker). Returns container info + interactable list.

### New Action Types (all 3 executors)
- **`double_click`**: Fast double-click on target coords or BID
- **`focus`**: Click or focus element for text input prep
- **`element_scroll`**: Scroll inside specific container element (not page)
- **`draw` / `canvas_draw`**: Canvas stroke using normalized path coords

### Planner Prompt + Container Pipeline
- **Planner SYSTEM_PROMPT updated** (`vision_learning.py`): Added examples for `draw`, `double_click`, `focus`, `element_scroll` in OUTPUT FORMAT actions array. Added documentation for normalized canvas coords, double-click use cases, focus before type, element scroll vs page scroll, and interactive containers data.
- **Container discovery wired into sidecar** (`learning_sidecar.py`): `discover_interactive_containers()` called each round, results injected into `_page_info_extra['containers']`. Logged with count + total children.
- **Container passthrough to planner** (`vision_learning.py`): `propose_actions()` now merges `containers` from `_page_info_extra` into `page_info`, making container data visible to the vision model.

### Files Modified
| File | Changes |
|------|---------|
| `agents/recipe_executor.py` | dest_coords field, drag fix, (0,0) guard, dedup, health check, 4 new action types |
| `agents/learning_sidecar.py` | dest_coords, None defaults, dead code removal, near-origin, progress None, threshold, frame logging, canvas clamping, validity gate, 3 new action types, container discovery wiring |
| `agents/vision_learning.py` | BID drag resolution, near-origin guard, validity gate, 4 new action types, planner prompt update (new actions + containers), container passthrough |
| `orchestrator.py` | validate_recipe health check before replay |
| `primitives.py` | validate_action gate, draw_stroke_on_canvas, discover_interactive_containers |
| `knowledge/learnings.json` | 3 corrupted drag recipes reset to tier=0 |

---

## Session: 2026-02-06 — V7.4: Semantic Color Transitions + Recipe Fix

### Changes
1. **Semantic color transitions** (`init_hooks.py`): Added `_colorCat()` function that categorizes
   CSS colors into green/red/grey/blue/yellow/other/none. Added `bgColor`, `fgColor`, `borderColor`
   to `_snapState()`. `_checkTransitions()` now emits `turned_green`, `turned_red`, `turned_grey`.
2. **Color priority weights** (`primitives.py`): `turned_green: 0.85`, `turned_red: 0.80`,
   `turned_grey: 0.60`. `turned_green` and `turned_red` count as progress in PROGRESS_CHANGE_TYPES.
3. **ACTIONABLE_CHANGES filter** (`learning_sidecar.py`): Only positive transitions + color changes
   sent to planner. Removed "disabled" negative events that confused planner.
4. **Contextual planner header** (`vision_learning.py`): Replaced imperative "act on these FIRST"
   with "UI STATE CHANGES since last round:" + per-change color hints. Planner reasons about meaning.
5. **Planner system prompt** (`vision_learning.py`): Added color context semantics: turned_green
   could mean "done" or "click me", turned_red could mean "error" or "urgent", turned_grey = skip.
6. **Recipe Phase 2.5 capture** (`orchestrator.py`, `learning_sidecar.py`): Phase 2.5 action_log
   passed as `pre_actions` to sidecar. Recipes now capture full causal chain. Confirmed: audio_v3
   recipe went from 2 steps to 8 steps (7 Phase 2.5 + 1 sidecar).
7. **Scrollable container detection** (`primitives.py`): Added Pass 3.5 in `annotate_elements()`
   for overflow:scroll/auto divs with scrollable content. Added `scroll_container` to `classify()`.
8. **Tiling scroll fix** (`primitives.py`): `progress_guided_exploration()` now scrolls to top
   after tiling scan completes, preventing sidecar from starting at wrong scroll position.
9. **Removed negative state emission** (`init_hooks.py`): Removed "disabled" event from
   `_checkTransitions()` — negative signals confused the planner.

### Results
- **v=1: 16/30** ($0.34, 269.9s, 17 vision calls) — tied best ever
- **v=3: 15/30** ($0.30, 237.6s, 15 vision calls) — best v=3 ever
- **v=2: 13/30** ($0.26) — baseline maintained
- **`turned_green` verified**: 7/9 instances on solved steps across all versions
- **`turned_red`/`turned_grey`**: Never fired (site doesn't use those patterns)
- **Recipe fix confirmed**: audio recipe captures Phase 2.5 "Play Audio" click

### Blockers Remaining
- Step 17 (gesture/canvas): planner draws off-canvas. Canvas BID constraint needed.
- Step 16 v3 (sequence scroll box): scroll_container fix untested (no v3 run after fix).
- Step 14 v2 (split_parts 4/4): anti-loop blocks repeated scroll to find 4th part.

---

## Session: 2026-02-06 — V7.2: State Change Pipeline Overhaul

### Problem
V7.1 fixes still had deep architectural issues. 10 root causes identified via exhaustive investigation:
1. Post-round `drain_state_changes()` cleared JS buffer — next round's pre-round drain got nothing
2. WeakMap baseline race — baselines snapped at arbitrary 300ms tick, missing transitions already settled
3. `:hover` filter suppressed changes during Playwright clicks (element is `:hover` during click)
4. MutationObserver can't detect CSS computed style changes (only DOM attribute mutations)
5. `element_catalog` not passed to `wait_for_state`, causing NameError
6. 500ms debounce too aggressive for rapid UI transitions
7. Challenge region filter in JS too aggressive (no Python context)
8. Decoy filter in JS had false positives (no Python context)
9. Selector missed non-interactive elements that gain interactivity via state change
10. `read_progress` regex didn't match "Capture (0/3)" format

### Fixes
- **init_hooks.py**: Complete rewrite of JS state watcher:
  - Removed hover filter, decoy filter, challenge region filter from JS (moved to Python)
  - Reduced debounce 500→200ms
  - Added `__peekStateChanges()` (read WITHOUT clearing buffer)
  - `__resetStateWatch()` now calls `_checkTransitions()` immediately (force baseline snap)
  - Added delayed 50ms re-check after MutationObserver attribute callbacks
  - Increased element cap to 200, viewport ±500px, buffer cap 100
  - Added `disabled` change type (reverse direction)
  - Broadened `new_element` selector to include `[data-bid]`, `[onclick]`, `canvas`
- **primitives.py**:
  - Added `peek_state_changes(page)` — read without clearing
  - Added `match_state_changes_to_catalog()` — 2-strategy matching (BID first, proximity+text fallback)
  - Updated `classify_state_changes()` — position+text as stable identity (not ephemeral BID)
  - Updated `wait_for_state()` to use centralized matching function with `element_catalog`
  - Fixed `read_progress` pattern 2: `[:\s]*` → `[:\s(]*` to match "Capture (0/3)"
- **learning_sidecar.py**:
  - Replaced post-round `drain` with `peek` (critical: stops stealing from next round)
  - Replaced inline matching with centralized `match_state_changes_to_catalog()`
  - Threaded `element_catalog` through `_execute_action` to `wait_for_state`
  - Removed duplicate `reset_state_watch()` (orchestrator handles it)

### Results
- **15/30 (v=1)**, $0.26, 202.9s — **new best score**
- State changes now actively firing and matched:
  - Step 12 (Canvas): `enabled,became_clickable,became_interactive:Reveal Code` — button enabled after 3/3 strokes
  - Step 13 (Audio): `disabled:Playing...` → `enabled,became_clickable,became_interactive:Play Again`
- Failed at step 16 (Multi-Tab Challenge) — not state-change related
- State change hit rate improved from 1/8 steps to 3/8 steps

---

## Session: 2026-02-06 — V7.1: State Change Detection Bug Fixes

### Problem
Investigation found state changes only fired on 1/8 sidecar steps (step 15 only). Root causes:
1. JS `_checkTransitions` selector missed cursor:pointer divs and drop targets (no `[data-bid]`)
2. `drain_state_changes()` only called at round START — within-round changes lost
3. MutationObserver `new_element` detection used narrow selector, missing annotated elements
4. Double `reset_state_watch()` (orchestrator + sidecar) wiped Phase 1/2 accumulated changes
5. 80-element cap too low, ±100px viewport filter too narrow for below-fold elements

### Fixes
- **init_hooks.py**: Added `[data-bid]` to both `_checkTransitions` selector and `new_element`
  MutationObserver selector. Increased element cap 80→150. Expanded viewport ±100→±300px.
- **learning_sidecar.py**: Removed duplicate `reset_state_watch()` (keep orchestrator's only).
  Added post-round drain after actions + settle to capture within-round state changes for
  progress check and next-round injection.

### Results
- 14/30 (v=3), $0.22, 198.7s, 11 vision calls, 100% recipe hit rate
- Steps 1-14 all solved consecutively (best streak on v3)
- State changes detected on step 15 (Submit Code enabled)
- Step 15 failure: Rotating Code Challenge — timing issue, not state-change related

---

## Session: 2026-02-06 — V7: UI State Change Detection + wait_for_state

### Problem
The system treats every element catalog snapshot as a fresh view with no memory of what changed.
When a button goes from disabled→enabled, or a new element appears after completing a sub-task,
the planner has no signal that THIS is the next required action. It wastes actions clicking random
elements or proposes "wait" when stuck, instead of reacting to UI transitions.

### Changes

#### Layer 1: Always-On JS MutationObserver (`init_hooks.py`)
- MutationObserver watches childList + attributes (disabled, aria-disabled, aria-hidden, class, style, hidden)
- 300ms periodic sweep catches CSS-only transitions (opacity, cursor, pointer-events)
- Tracks: disabled→enabled, hidden→visible, opacity activation, pointer-events becoming clickable,
  cursor→pointer, aria-hidden removal, color vibrancy increases, new interactive elements
- Five hardening measures: WeakMap identity (not string sigs), hover filter, decoy text filter,
  challenge region filter, 500ms per-element debounce
- API: `window.__drainStateChanges()`, `window.__resetStateWatch()`

#### Layer 2: Python drain + classify (`primitives.py`)
- `drain_state_changes(page)`: read + clear JS buffer
- `classify_state_changes(changes)`: priority scoring (0.45-0.95) + dedup by (sig, change_type)
- `CHANGE_PRIORITY` dict, `PROGRESS_CHANGE_TYPES` frozenset
- `reset_state_watch(page)`: clear baselines at step boundary

#### Layer 3: wait_for_state primitive (`primitives.py`)
- `wait_for_state(page, change_types, timeout_ms, match_text, element_catalog)`: blocking poll
  loop that drains state changes until a matching transition is detected or timeout
- Returns matched element with BID for immediate click — replaces time-based waits with
  condition-based synchronization
- Available as action type in both sidecar and VL executor

#### Layer 4: Sidecar integration (`learning_sidecar.py`)
- Step start: `reset_state_watch()` after baseline
- Each round: drain → classify → BID-match → inject as `state_changes` in planner context
- Reflection: state changes highlighted as "try these first!" when planner is stuck
- Progress: high-confidence state changes (enabled/appeared/new_element/clickable/activated)
  count as progress to prevent premature stall termination
- Action log: `triggered_by_state_change` flag for recipe awareness
- Hit rate metric: logs state-change effectiveness at step termination

#### Layer 5: Planner integration (`vision_learning.py`)
- System prompt: 2-line addition about state changes as strongest signal
- Message: `state_changes` field injected into planner data
- New action type: `wait_for_state` available in output format

#### Layer 6: Step boundary (`orchestrator.py`)
- `reset_state_watch()` called in `_step_setup()` after clearing codes

### Files Modified
| File | Changes | ~LOC |
|------|---------|------|
| `init_hooks.py` | JS MutationObserver + periodic sweep (5 hardening measures) | ~120 |
| `primitives.py` | drain/reset/classify + wait_for_state + constants | ~85 |
| `learning_sidecar.py` | drain+inject, reflection, progress, recipe tag, hit rate, wait_for_state exec | ~75 |
| `vision_learning.py` | System prompt + message injection + wait_for_state exec | ~25 |
| `orchestrator.py` | Step boundary reset_state_watch() | ~3 |
| **Total** | | **~308** |

---

## Session: 2026-02-06 — Fix Drag-and-Drop: Mouse-Only with Outcome Verification

### Problem
`smart_drag()` used JavaScript `dispatchEvent(new DragEvent(...))` as primary strategy. This is
fundamentally broken for React apps: React 18 ignores programmatic DragEvents (event delegation
filtering), `new DataTransfer()` enters "protected mode" (getData returns empty), and the HTML5
path always reports success (masking failures). Result: alternating success/failure pattern,
only ~3/6 fills per round, running out of rounds at 5/6.

### Changes

#### C11: Rewrite `smart_drag()` — mouse-only with outcome verification (`primitives.py`)
- **Removed**: Entire HTML5 DragEvent dispatch block (~80 lines of JS)
- **Added**: Mouse drag with 3px nudge step to reliably trigger drag initiation
- **Added**: Before/after bbox snapshot — `success=True` only when element moved >3px or disappeared
- **Key**: No more false `html5 OK` reports. Outcome-based verification eliminates ghost successes.

#### C12: Hardened BID-based drag in sidecar (`learning_sidecar.py`)
- **Added**: `locator.drag_to()` as first-choice for BID-based drags (when both source_id and target_id present)
- Pre-waits: `wait_for(state="visible")` + `scroll_into_view_if_needed()` on both elements
- `force=True` bypasses actionability checks (BID overlay interference)
- Falls back to BID→coord resolution + `smart_drag()` only if `drag_to()` fails

### Synthetic Event Audit
Audited all `dispatchEvent()` calls in the codebase. Results:
- **BROKEN (fixed)**: DragEvent dispatch in `primitives.py`, `drag_drop.py`, `human_solver.py`
- **Safe**: nativeInputValueSetter + input/change (canonical React pattern, used by testing libs)
- **Safe**: PointerEvent/MouseEvent/click sequence in popup.py (bubbles to React root)
- **Safe**: KeyboardEvent dispatch in popup.py (bubbles to React root)
- **Low risk**: mouseenter/mouseover in sequence_challenge.py (backed by page.mouse.move())
- **Medium risk**: scroll event dispatch in sequence_challenge.py (backed by scrollTop mutation)

### Recommendations
1. Always prefer Playwright native APIs (page.mouse.*, locator.drag_to()) over JS dispatchEvent()
2. DragEvent is uniquely broken — only event where React delegation + DataTransfer protection combine
3. mouseenter/scroll dispatches are fragile but have Playwright backups — monitor if sequence challenges fail
4. The input/change pattern is stable — React testing libraries depend on same mechanism

### Net LOC
~50 lines changed, ~80 lines removed (HTML5 DragEvent block)

---

## Session: 2026-02-06 Late — Interactable vs Context Element Classification

### Overview
Three-file change (~120 LOC) that classifies every annotated element as interactable or context,
adds drop target detection, and enforces interactability at the sidecar level with closed-loop
rejection feedback.

### Changes

#### C8: Element Classification + Drop Target Detection (`primitives.py`)
- `annotate_elements()` now classifies every element via `classify()` function:
  button, text_input, drag_source, drop_target, canvas, clickable, checkbox, radio,
  select, slider, focusable, submit_button, or context.
- **Interactable elements** get `data-bid` attribute + `bid` field + overlay label.
- **Context elements** get `eid` field only (no DOM mutation, no overlay label).
- New `_isDropTarget()` with 3-stage confidence: high (ondrop/data-slot), medium (class/text),
  low (dashed border). `drop_confidence` field in catalog.
- **Pass 3**: Dedicated drop target scan for slot/zone divs missed by Pass 1-2.
- **Pass 4**: Context text elements (p, h1-h4, span, label) capped at 15, viewport-only.
- `render_bid_overlay()` now skips `interactable: false` elements.

#### C9: System Prompt + Message Split (`vision_learning.py`)
- System prompt now describes `interactable_elements` vs `context_elements` with types.
- Drag workflow documented: drag_source → drop_target by BID, prefer high/medium confidence.
- Raw x,y restricted to scroll, canvas, typing.
- Message format splits `element_catalog` into `interactable_elements` + `context_elements`.

#### C10: Sidecar Enforcement Gate (`learning_sidecar.py`)
- Builds `interactable_bids` set from planner catalog each round.
- **Enforcement gate**: click/hover on context-only BIDs rejected with reason string.
  Drag source/target must both be interactable. Raw coord clicks emit warning log.
- **Rejection feedback loop**: Rejected actions recorded as `round_entries` with
  `rejected_by_controller: True` + reason. Appears in reflection so planner adjusts.
- `bid_to_elsig` now filters out context elements (no `None` key).
- Iframe elements get `interactable: True` and `interactable_type` fields.
- Catalog stats logging includes interactable/context/drop_target counts.

---

## Session: 2026-02-06 — Vision Grounding + Exploration Upgrade

### Overview
Research-driven upgrade targeting the 12-14/30 ceiling. Implements 7 core changes + 5 follow-up
fixes across 3 files (~300 LOC). Key capabilities added: BID-based element grounding for the
vision model (Set-of-Marks), structured reflection with anti-loop enforcement, exploration
strategy bandit, challenge region detection, frame/shadow DOM surfacing, structural decoy
filtering, and causal action storage for recipe replay.

### Core Changes (C1-C7)

#### C1: Structured Reflection + Hard Anti-Loop Gate (`learning_sidecar.py`)
**Problem:** Sidecar proposed "wait/scroll" when stuck — no reflection on WHY actions failed.
**Fix:**
- After a round with `outcome: 'no_change'`, build a structured reflection string from action
  results (action type, coordinates/BID, hit element, DOM change score). Injected into history
  entry so the planner sees it on the next call.
- **Hard anti-loop enforcement:** Track action signatures `(type, element_id|xy_bucket)`. Block
  re-proposals that match failed actions. `_xy_bucket()` rounds coordinates to nearest 50px grid
  to catch near-duplicates.
- New functions: `_xy_bucket()`, `_action_sig()`.

#### C2: Element Annotation with Visual Overlay — BID / Set-of-Marks (`primitives.py`, `vision_learning.py`, `learning_sidecar.py`)
**Problem:** Vision model guessed pixel coordinates, often missing targets. No element grounding.
**Fix:**
- **`annotate_elements(page)`**: Assigns `data-bid` attribute to all interactive elements
  (buttons, inputs, selects, canvas, role=button/checkbox/slider/radio, tabindex, draggable,
  onclick). Returns catalog with bid, tag, type, text, role, ariaLabel, x, y, w, h, draggable,
  in_viewport.
- **`render_bid_overlay(page, catalog)`**: Injects a cosmetic overlay (`pointer-events:none`)
  with red numbered labels at each element's position. Removed after screenshot via
  `remove_bid_overlay(page)`.
- **Vision prompt updated**: System prompt now instructs model to use `"element_id"` in actions.
  Example: `{"type": "click", "element_id": 7}`. Falls back to x,y for canvas/empty space.
- **BID resolution in sidecar**: `_execute_action()` resolves `element_id` → bounding box
  coordinates via `page.locator('[data-bid="N"]').bounding_box()`.
- **BID resolution in VisionLearningAgent**: Same resolution added to standalone `_execute_action()`
  for when VL runs without sidecar.
- **Safety**: data-bid is a cosmetic attribute, overlay is pointer-events:none, removed
  immediately after screenshot. No React state or DOM structure mutation.

#### C3: Exploration Strategy Bandit (`learning_sidecar.py`)
**Problem:** Agent repeated same interaction types when stuck instead of trying alternatives.
**Fix:**
- 8 strategy types: click_buttons, hover_reveal, drag_drop, iframe_switch, keyboard,
  scroll_explore, form_fill, canvas_draw.
- `StrategyBandit` class with epsilon-greedy selection: untried strategies first, then highest
  reward per visit.
- Reward signal: `progress_delta * 2 + dom_change + new_elements * 0.5`.
- Strategy suggestion injected into reflection string on stall rounds.
- Strategy hint also injected directly into planner context dict for structured visibility.

#### C4: Challenge Region Detection (`primitives.py`)
**Problem:** Agent wasted budget clicking page chrome / distraction zone / decoy buttons.
**Fix:**
- **`find_challenge_region(page)`**: Finds the code input form, walks up DOM to find the
  challenge container. Returns `{x, y, w, h, selector}`.
- Used by sidecar to mark elements as `in_challenge_region: true/false`.
- Passed to planner via page_info so vision model knows the active zone.

#### C5: Frame + Shadow DOM Surfacing (`primitives.py`)
**Problem:** Sidecar had no way to discover or interact with iframe/shadow DOM content.
**Fix:**
- **`enumerate_frames(page)`**: Returns list of iframes with index, src, position, visibility,
  and first 200 chars of inner text.
- **`enumerate_shadow_roots(page)`**: Recursively walks DOM (max depth 5) finding open shadow
  roots, counting their interactive elements, extracting text.
- Results passed to planner on first round. Strategy bandit boosts `iframe_switch` when
  iframes with interactives are found.

#### C6: Structural Decoy Filtering (`primitives.py`)
**Problem:** Agent clicked known decoy buttons (Next, Continue, Proceed, etc.) and elements in
the distraction zone below the challenge area.
**Fix:**
- **`GLOBAL_DECOY_TEXT_RE`**: Regex matching 15 known decoy button labels.
- **`is_decoy_element(el_info, challenge_region)`**: Returns True if element text matches decoy
  pattern OR element is below challenge region + 100px margin.
- Applied to BID catalog: elements marked `decoy: true`. Vision prompt says "Elements marked
  `decoy: true` are known traps. NEVER interact with them."

#### C7: Causal Action Storage for Recipes (`learning_sidecar.py`)
**Problem:** Recipes stored ALL actions including ineffective ones. Coordinate-based recipes
failed when step layouts changed.
**Fix:**
- In `_build_promotion_candidate()`, build `causal_actions` list containing only actions that
  caused progress (progress_delta > 0), significant DOM change (> 0.05), or revealed a code.
- Each causal action stores semantic locator: `target_role`, `target_text`, `target_aria`,
  `target_testid`, `effect` (progress/dom_change/code_found).
- Stored as `semantic_steps` in promotion candidate alongside existing recipe. Future recipe
  replay can fall back to semantic locator when coord-based replay fails.

### Follow-Up Fixes (#1-#5)

#### Fix #1: BID Resolution in VisionLearningAgent Standalone Mode
Added same `element_id` → bounding box resolution to `VisionLearningAgent._execute_action()`
so BID-based actions work when VL runs without sidecar.

#### Fix #2: Strategy Hint Injection into Planner Context
When `consecutive_stalls >= 1`, inject `suggested_strategy` dict (name + description) directly
into context. Planner sees it in the structured JSON data block alongside screenshot.

#### Fix #3: BID Catalog Trimming for Prompt Efficiency
Sort elements by priority: in_viewport (4pts) > in_challenge_region (2pts) > non-decoy (1pt).
Cap at 30 elements sent to planner. Prevents prompt bloat on pages with 50+ elements.

#### Fix #4: Strip Ephemeral Page Info from Context
Remove `_page_info_extra` key from context dict after planner call. Prevents full element
catalog from leaking into recipe storage or promotion logs.

#### Fix #5: Annotate Elements Inside Iframes
On first round, for each visible iframe, use Playwright's frame API to run element annotation
inside the iframe. Results merged into main catalog with `frame: N` tag and coordinates offset
to main-page space.

### Files Modified

| File | Changes | ~LOC |
|------|---------|------|
| `agents/learning_sidecar.py` | C1 (reflection+anti-loop), C2D (BID resolution), C3 (strategy bandit), C7 (causal storage), Fix #2-5 | ~170 |
| `primitives.py` | C2A/B (annotate+overlay), C4 (challenge region), C5 (frames+shadow), C6 (decoy filter) | ~130 |
| `agents/vision_learning.py` | C2C (BID prompt+catalog), Fix #1 (BID resolution) | ~30 |

**Total: ~330 LOC across 3 files**

### Invariants Preserved
1. BID overlay is pointer-events:none, removed immediately after screenshot — no DOM mutation
2. data-bid attribute is cosmetic only — does not affect React state
3. All new JS in primitives.py is read-only (querySelectorAll, getBoundingClientRect)
4. No page.goto() / page.reload()
5. Anchor clicks still blocked in sidecar's _execute_action
6. Sidecar still never submits codes — returns candidates to orchestrator

---

## Session: 2026-02-05 (Late Night) — Rejection-Feedback Loop

### Overview
Implemented rejection-feedback loop for System 2 sidecar pipeline. When a submitted code is
rejected by the site, the orchestrator informs the sidecar (ephemeral per-step blacklist),
tries remaining candidates, and re-invokes the sidecar up to 2 times with up to 3 total
submissions. Sidecar now returns ranked candidate lists with provenance metadata and scoring.

### Architecture Change: Rejection Loop

**Before:** Sidecar returned a single `{code, promotion_candidate}`. Orchestrator submitted once.
If rejected, the step failed immediately.

**After:** Sidecar returns `{code, candidates, promotion_candidate}` where `candidates` is a
ranked list with provenance (source, score, seen_after_baseline, etc.). Orchestrator iterates
candidates, informs sidecar of rejections, and can re-invoke sidecar with rejection context.

```
Orchestrator Phase 3 (new):
  for sidecar_call in range(MAX_SIDECAR_CALLS=2):
    result = _invoke_sidecar(page, step, version)
    for candidate in result.candidates:
      if submission_count >= MAX_SUBMISSIONS=3: break
      submit → if rejected → sidecar.note_rejection(code, meta)
    if all rejected → loop continues (sidecar called again with rejection context)
```

### Key Changes

#### LearningSidecar (`agents/learning_sidecar.py`)
- **Rejection state**: `_rejected_codes` (exact blacklist set), `_rejected_signatures` (source+evidence tuples)
- **Step-boundary reset**: Rejection state clears on step change, never persisted
- **Rejection context**: Passes `rejected_codes` + `last_rejection_reason` to planner via context
- **`_harvest_code()`**: Filters out rejected codes at all 3 validation points
- **`_harvest_all_candidates()`**: Returns ALL valid non-rejected codes with metadata (source, score, seen_after_baseline, harvest_score, dom_change_score, progress_delta)
- **`_score_candidate()`**: Scoring function 0.0-1.0:
  - `seen_after_baseline` (+0.3 / -0.3)
  - `harvest_score` (*0.15)
  - `dom_change_score` (+0.1 if >= 0.05)
  - `progress_delta` (+0.1)
  - Source preference (`harvest_score` source +0.05)
  - Signature rejection penalty (-0.3)
- **`_finalize_code_return()`**: Helper that collects all candidates, ensures found code is first, logs top 3, builds promotion, returns via `_make_result`
- **`note_rejection(code, candidate_meta)`**: Adds to blacklist + optionally signatures
- **`clear_soft_rejections_on_progress()`**: Clears signature rejections on meaningful progress, keeps exact code blacklist
- **`_make_result()`**: Now includes `candidates` field

#### Orchestrator (`orchestrator.py`)
- **`_submit_and_record()`**: Returns `(bool, str)` tuple (`'solved'`, `'rejected'`, `'already_failed'`)
- **Phase 1 + Phase 2**: Updated to unpack tuple from `_submit_and_record()`
- **Phase 3 rewritten**: Rejection loop with `MAX_SIDECAR_CALLS=2`, `MAX_SUBMISSIONS=3`
- **`_invoke_sidecar()`**: Renamed from `_system2_reasoning()`, returns full result dict with candidates
- **Logging**: Logs candidate count, top 3 with scores, rejection events

### Files Modified

| File | Action | ~Lines |
|------|--------|--------|
| `agents/learning_sidecar.py` | Modified | ~+180 (rejection state, candidates, scoring, helpers) |
| `orchestrator.py` | Modified | ~+25, ~-12 (tuple returns, rejection loop, rename) |

### Acceptance Tests (Verified)

| Test | Status |
|------|--------|
| Rejected code never re-submitted in same step | ✓ |
| Try next candidate after rejection | ✓ (logic verified) |
| Progress clears soft rejections | ✓ (implemented, not triggered in test run) |
| No persistence across steps/sessions | ✓ |

### Performance Results

| Run | Version | Result | Notes |
|-----|---------|--------|-------|
| v3 steps 1-10 | v3 | **5/10** | Steps 1-5 pass; step 5 via System 1 recipe replay (click_reveal promoted from prior run) |
| Step 6 rejection | v3 | FAIL | 3C93WW rejected → 2nd sidecar call → stalled (planner proposed "wait" actions) |

### Invariants Preserved
1. Sidecar never submits codes — returns candidates to orchestrator
2. Rejection state is ephemeral (per-step, per-session, never persisted)
3. All JS is read-only (no DOM/React mutation)
4. No page.goto() / page.reload()
5. Promotion requires 3 guards (causal + assertions + locators)

---

## Session: 2026-02-05 (Night) — LearningSidecar v1 + Coverage Engineer Role

### Overview
Split System 2 into **planner** (VisionLearningAgent.propose_actions) and **controller**
(LearningSidecar). Sidecar owns the closed loop: propose → execute → observe → iterate → return
code. Sidecar never submits codes — orchestrator owns submission and triggers promotion only after
confirmed advancement. Established **Coverage Engineer** role for future dev-time iterations.

### Architecture Change: VisionLearning + LearningSidecar

**Before:** `_system2_reasoning()` called `VisionLearningAgent.run()` which both planned AND
executed actions in a single monolithic loop.

**After:** Two components with clean separation of concerns:
- **VisionLearningAgent** (planner): `propose_actions()` captures state, calls Claude, returns
  `{actions, notes, stop, extracted_codes}` without executing anything.
- **LearningSidecar** (controller): Owns the closed loop — calls planner, executes actions itself
  (with anchor blocking, frame-aware coord translation, hit testing), measures progress via multiple
  signals (DOM change, progress delta, clickable hash, new codes), detects stalls, builds promotion
  candidates with recipe steps + assertions + locator cascades.

**Promotion is two-phase:**
1. Sidecar builds a `promotion_candidate` dict during `run()` (recipe steps, assertions, DNA sig)
2. Orchestrator calls `finalize_promotion()` ONLY after confirmed URL advancement
3. Finalization checks three guards: causal actions exist, strong assertions present, at least one
   stable locator (data-testid, role+name, aria-label, or text — not coords-only)

### Key Changes

#### VisionLearningAgent (`agents/vision_learning.py`)
- `reset_conversation()`: Clears multi-turn state between steps
- `propose_actions()`: Plan-only mode with optional timeout (Windows-safe via ThreadPoolExecutor)
- `_build_user_message()`: New `history=` kwarg for sidecar round-by-round feedback
- `import concurrent.futures` added

#### LearningSidecar (`agents/learning_sidecar.py`) — NEW (~500 lines)
- `run()`: Up to 6 rounds, 3 actions/round, broad stall detection (2 consecutive no-progress)
- `_execute_action()`: Sidecar-owned execution (no VL state mutation) with anchor blocking
- `_harvest_code()`: Three-layer harvest (observers → JS extraction → harvest_and_score) with
  unified charset regex + JS oracle validation
- `_get_dom_signature()`: Lightweight 2-level DOM signature (faster than base.py's `querySelectorAll('*')`)
- `_get_clickable_hash()`: Visible interactive element inventory for cheap progress detection
- `_wait_for_settle()`: RAF-based settle (600ms cap) instead of fixed delay
- `_read_bound_progress()`: Progress binding (first read anchors total, subsequent must match)
- `_build_promotion_candidate()`: Recipe steps with locator cascade priority
- `finalize_promotion()`: Three-guard promotion (causal actions, strong assertions, stable locators)
- `_frame_sweep()`: Iframe code search on stall
- `_dismiss_popups()`: Full popup dismiss + CLEAR_BLOCKERS_JS (matches orchestrator behavior)

#### Orchestrator (`orchestrator.py`)
- Imports and instantiates `LearningSidecar` in `__init__()`
- `_system2_reasoning()` now calls `sidecar.run()` instead of `VisionLearningAgent.run()`
- Promotion uses two-phase sidecar pattern instead of `_promote_to_system1()`
- `_promote_to_system1()` preserved intact as revert path
- ~30 lines changed total

#### Smoke Harness (`solve.py`)
- `--sidecar-smoke` flag: Force sidecar path for 1-3 steps with detailed SIDECAR output
- `run_sidecar_smoke()`: Standalone test function

#### Coverage Engineer Role (`docs/COVERAGE_ENGINEER.md`) — NEW
- Formal role definition for dev-time iteration on primitives
- Non-negotiable rules, iteration loop (Reproduce → Diagnose → Propose → Implement → Validate → Report)
- Escape hatch for out-of-scope failures
- Example primitives catalog

### Files Modified

| File | Action | ~Lines |
|------|--------|--------|
| `agents/vision_learning.py` | Modified | +90 (propose_actions, reset_conversation, history kwarg) |
| `agents/learning_sidecar.py` | **NEW** | ~500 (LearningSidecar class) |
| `orchestrator.py` | Modified | ~+30, ~-10 (sidecar integration) |
| `solve.py` | Modified | +45 (--sidecar-smoke flag) |
| `docs/COVERAGE_ENGINEER.md` | **NEW** | ~120 (role definition) |

### Invariants Preserved
1. Sidecar never submits codes — returns `code` to orchestrator
2. Sidecar never calls code_entry — orchestrator owns submission
3. All JS is read-only (no DOM/React mutation)
4. No page.goto() / page.reload()
5. Anchor clicks blocked in sidecar's _execute_action
6. VisionLearningAgent.run() preserved as revert path
7. Popup auto-dismiss JS interval + Playwright handler unaffected

---

## Session: 2026-02-05 (Late) — Scroll Blacklist, Memory Agent, Model Upgrade

### Overview
Multiple targeted fixes: scroll false positive elimination via blacklist, new memory agent for
flash-code challenges, sequence challenge hover scoring, puzzle back-to-back workaround,
click_reveal popup fix, vision model upgrade to Sonnet 4.5, broadened early check detection.
Result: **10/30 steps pass on v2** (steps 1-10), blocked by draw challenge at step 11.

### Key Changes

#### Scroll Blacklist (`RECIPE_BLACKLISTED_TYPES`)
**Problem:** "Scroll down" text appears as decoy on ~99% of pages (99 filler content sections).
Recipe/verified learning/stored actions all tried to scroll on non-scroll pages.
**Fix:** `RECIPE_BLACKLISTED_TYPES = {"scroll"}` — scroll blocked from recipe replay, verified
learning path, and `_execute_stored_action`. Only vision dispatch can trigger scroll agent.

#### Memory Agent (`agents/memory.py`) — NEW
**Problem:** Step 7 (code flash + recall) was being misidentified as delay challenge, causing
cross-contamination of learnings/recipes.
**Fix:** New `MemoryAgent` with separate learning track. Clicks start/show/flash buttons, uses
`wait_for_function(polling="mutation")` to catch briefly-flashed code. Added to early checks
with keywords: "watch carefully", "briefly", "will appear for", "memory challenge", etc.

#### Sequence Challenge Hover Fix (`agents/sequence_challenge.py`)
**Problem:** Hover target selection picked oversized page containers (1152x2988px) instead of
the actual hover zone (~173x48px).
**Fix:** Viewport-relative max size cap (`min(600, viewport * 0.9)`), interactivity scoring
(mid-size preference ~200x80, cursor:pointer bonus), steps=10 (was 5), 2s hold (was 1.5s).
Applied to both `run()` and `_retry_all_tasks()`.

#### Puzzle Back-to-Back Workaround (`agents/puzzle_solve.py`)
**Problem:** Site reuses same code for consecutive puzzle challenges. Second puzzle shows
"Code revealed: XXXXXX" with the same code from previous step.
**Fix:** Workaround extracts code from "Code revealed" text and accepts it even if previously
used (removed `__isValidCode` check from workaround only).

#### Click Reveal Popup Fix (`agents/click_reveal.py`)
**Problem:** Second occurrence of click_reveal in a session struggled due to accumulated popups.
**Fix:** Full `self.dismiss_popups(page)` at start + retry-after-popup-dismissal logic.

#### Vision Model Upgrade
Updated `config.py`, `vision_client.py`, `agents/vision_learning.py` from
`claude-sonnet-4-20250514` to `claude-sonnet-4-5-20250929`.

### Files Modified

| File | Change |
|------|--------|
| `orchestrator.py` | `RECIPE_BLACKLISTED_TYPES`, memory early check, broadened memory/delay keywords, scroll gate in `_execute_stored_action` |
| `agents/memory.py` | **New file** (flash code challenge agent) |
| `agents/__init__.py` | Registered memory agent (32 agents total) |
| `agents/sequence_challenge.py` | Hover: max size cap, interactivity scoring, steps=10, 2s hold |
| `agents/puzzle_solve.py` | Back-to-back puzzle workaround (accept reused code) |
| `agents/click_reveal.py` | Popup dismissal + retry logic |
| `config.py` | Vision model → `claude-sonnet-4-5-20250929` |
| `vision_client.py` | Default model → `claude-sonnet-4-5-20250929` |
| `agents/vision_learning.py` | Vision model → `claude-sonnet-4-5-20250929` |

### Performance Results

| Run | Version | Result | Notes |
|-----|---------|--------|-------|
| v2 steps 1-10 | v2 | **10/10** | All pass, scroll blacklist working, memory agent working |
| v2 step 11 | v2 | FAIL | Draw: canvas shows 2/3 strokes, 3rd stroke not registering |

---

## Session: 2026-02-05 — Pre-Vision Early Checks + 6 New Agents

### Overview
Major architectural shift: **10 specialist agents now run BEFORE the Vision API call**,
saving ~8s per matched challenge. Added 4 new agents (timing, split_parts, sequence_challenge,
multi_tab) and rewrote/fixed 4 existing agents (audio, video, draw, hover). Result: **15/15
steps pass on v1** (steps 1-15), up from ~8/30.

### New Architecture: Pre-Vision Early Checks

Before the expensive Vision API screenshot + analysis (~8s), the orchestrator reads body text
and checks DOM elements to dispatch directly to specialist agents:

```python
early_checks = [
    ('sequence_challenge', 'sequence challenge' + 'progress' + task keywords),
    ('timing',            'capture' + timing/countdown keywords),
    ('hover',             'hover over/here/to reveal'),
    ('draw',              canvas element + 'draw/stroke' keywords),
    ('audio',             audio element or 'play audio' keywords),
    ('video',             'seek/video' + N/M progress pattern),
    ('keyboard',          'keyboard sequence' / 'press keys' keywords),
    ('hidden_dom',        'hidden' + 'dom/element' or click-to-reveal),
    ('split_parts',       'split' + 'part' keywords),
    ('multi_tab',         'multi-tab' or 'tab' + 'visit/click each'),
]
```

Additionally, a **rotating code handler** detects "rotating"/"changes every" keywords, clicks
the challenge button 3x rapidly, then extracts the code from "the real code is" text pattern.

### New Agents

| File | Agent | What It Does |
|------|-------|--------------|
| `agents/timing.py` | `TimingAgent` | Polls up to 5s (10×500ms) for "Capture Now!" button during countdown window. Clicks it, waits for code, tries "Complete Challenge" button if needed. |
| `agents/split_parts.py` | `SplitPartsAgent` | Finds "Part N: XX" text patterns via regex, clicks part elements, combines code fragments, clicks Complete/Combine button. |
| `agents/sequence_challenge.py` | `SequenceChallengeAgent` | Completes 4 mini-tasks in a composite challenge: (1) click "Click Me" button, (2) hover over designated area (widest matching element + 1.5s hold + JS mouseenter), (3) type text in input field, (4) scroll inside scroll box. Then clicks "Complete (4/4)". Has retry logic for missed tasks. |
| `agents/multi_tab.py` | `MultiTabAgent` | Clicks all "Tab N" buttons to visit each tab, then clicks "Reveal Code"/"Complete" button. Polls for unvisited tabs across 3 rounds. |

### Agent Fixes

| File | Change |
|------|--------|
| `agents/audio.py` | **Complete rewrite.** Removed OpenAI Whisper dependency. New flow: click "Play Audio" → poll up to 8s (16×500ms) for "played" state → extra 2s wait if Complete button not yet visible → click "Complete Challenge" → extract code. Detects already-played state ("Audio played!", "Play Again") and skips to Complete. |
| `agents/video.py` | Added "Complete Challenge" button click after seek clicks reach target count. Previously found 3/3 on counter but never clicked Complete to reveal the code. Added harvest_and_score fallback. |
| `agents/draw.py` | Fixed pen timing: 50ms pause after move-to-start, 30ms after mouse.down, 30ms after mouse.move, 100ms after mouse.up. 300ms between strokes (was 150ms). Added `_click_complete()` with expanded keyword search (complete/submit/check/reveal). Added near-canvas button search fallback. |
| `agents/hover.py` | Limited strategy 2 to max 3 tries per selector (was 25+ decoy iterations). Raised minimum element size to 60×40px. Added Fallback 2 (class_match: `[class*="hover"]` with lower threshold). Added Fallback 3 (text_match: elements with "hover here"/"hover to reveal" text). Increased DOM walk from 5→8 levels up. |

### Orchestrator Changes (`orchestrator.py`)

| Change | Description |
|--------|-------------|
| Pre-vision early checks | Unified `early_checks` list runs 10 agents BEFORE vision API call |
| Rotating code handler | Detects rotating/changing codes, clicks button 3x rapidly, extracts "real code is" pattern |
| Removed duplicate checks | Old post-vision early checks removed (replaced by pre-vision) |
| `__init__.py` updates | Registered timing, split_parts, sequence_challenge, multi_tab (31 agents total) |

### Performance Results

| Run | Version | Result | Notes |
|-----|---------|--------|-------|
| v1 steps 1-15 | v1 | **15/15** | All pre-vision agents working |
| v1 step 16 | v1 | FAIL | Multi-tab: vision_learning clicked all 4 tabs but ran out of rounds before "Reveal Code" (multi_tab agent added to fix) |
| v3 steps 1-14 | v3 | **14/15** | Split parts (2.6s), rotating code handler working |
| v3 step 15 | v3 | FAIL | Rotating code: extracted wrong code (display code vs real code). Fixed with "the real code is" pattern. |

### Key Timing Improvements (Pre-Vision vs Vision Path)

| Challenge | Pre-Vision Time | Vision Path Time | Savings |
|-----------|----------------|------------------|---------|
| Timing | ~3.7s | ~12s+ | ~8s |
| Hover | ~2.6s | ~10s+ | ~7s |
| Audio | ~12s | ~20s+ | ~8s |
| Video | ~4.8s | ~15s+ | ~10s |
| Keyboard | ~3.8s | ~12s+ | ~8s |
| Hidden DOM | ~3.2s | ~10s+ | ~7s |
| Split Parts | ~2.7s | N/A (new) | N/A |

### Files Modified

| File | Change |
|------|--------|
| `orchestrator.py` | Pre-vision early checks architecture, rotating code handler with "real code is" extraction, removed duplicate post-vision checks |
| `agents/audio.py` | Complete rewrite (no Whisper, click-flow with 8s polling + 2s extra wait) |
| `agents/video.py` | Added Complete Challenge button click after seek clicks |
| `agents/draw.py` | Pen timing fixes, Complete button search, near-canvas button fallback |
| `agents/hover.py` | Limited decoy iterations, text/class fallbacks, wider DOM walk |
| `agents/timing.py` | **New file** (Capture button polling) |
| `agents/split_parts.py` | **New file** (find/combine split code parts) |
| `agents/sequence_challenge.py` | **New file** (4 mini-tasks composite challenge) |
| `agents/multi_tab.py` | **New file** (click all tabs then reveal) |
| `agents/__init__.py` | Registered all new agents (31 total) |

---

## Session: 2026-02-04 — System 1/System 2 Tiered Solver

### Overview
Added a two-tier solving architecture: **System 1 (Reflex)** replays proven action recipes
with assertion-based verification, and **System 2 (Reasoning)** uses DOM DNA clustering to
find codes when agents fail. Successes record DNA signatures and recipes for future System 1
use, creating a feedback loop that accelerates solving over multiple runs.

### New Files

| File | Purpose |
|------|---------|
| `agents/dna_reasoner.py` | **System 2:** `DETECT_DOM_DNA_JS` extracts computed styles from all visible elements (including Shadow DOM). `DNAReasoner` normalizes colors/fonts, clusters by DNA key, assembles codes from scattered fragments using Y-bucket row sort + X-column sort. Scores clusters by monospace, small size, known DNA match, highlighted background, and code/solution class labels. |
| `agents/recipe_executor.py` | **System 1:** `ActionStep` dataclass with locator cascade (ARIA role → text → CSS → DNA query → coords) and verification assertions (`expect_selector_visible`, `expect_text_contains`, `expect_dom_changed`). `RecipeExecutor` replays up to 10 steps, aborting on any assertion failure. |

### Modified Files

| File | Changes |
|------|---------|
| `knowledge_reader.py` | **StrategyVariant:** Added `action_recipe` (System 1 steps), `successful_dna_signature` (winning DNA), `page_context` (text patterns). **CanonicalLearning:** Added `dna_signatures` (type-level, with occurrence counts), `page_text_context` (aggregated keywords/buttons/interactives). **`detect_and_get()`:** New 5-factor scoring: `0.35*keyword + 0.25*dom + 0.1*flags + 0.15*text_ctx + 0.15*dna`. New helpers: `_aggregate_dna_signature()`, `_extract_text_context()`, `_compute_dna_score()`, `_compute_text_context_score()`. **`record_success()`:** Now accepts `page_info`, `recipe`, `dna_signature`. **`record_failure()`:** Now accepts `page_info`, stores instruction snippets + button labels. All JSON persistence updated for new fields (backward-compatible defaults). |
| `orchestrator.py` | **Imports:** Added `DNAReasoner`, `RecipeExecutor`, `DETECT_DOM_DNA_JS`. **Init:** Creates `self.dna_reasoner` and `self.recipe_executor`. **New utilities:** `_extract_page_info()` (single JS call for instruction/buttons/interactives), `_wait_for_dom_stable()` (element count stabilization), `_should_dna_scan()` (gates scanning by recipe existence, confidence, small text count). **System 1 path:** Before vision/agents, checks for stored recipe → replay → submit. **System 2 path:** After all agents fail, DNA scan → cluster → assemble → submit → record success with DNA. **Learning callbacks:** `_on_learning_succeeded()` and `_on_learning_failed()` now pass `page_info` through to `record_success()`/`record_failure()`. |
| `agents/__init__.py` | Registered `DNAReasoner` and `RecipeExecutor` in `ALL_AGENTS` (25 agents total). |

### Guard Rails
- **Performance gating:** DNA scan only when recipe missing AND confidence < 0.7 AND >10 small text fragments
- **DOM stability:** Wait for element count to stabilize before scanning (up to 2s)
- **Recipe cap:** Max 10 steps per recipe
- **Assertion abort:** Failing `expect_*` assertions abort recipe → System 2
- **DNA signature cap:** Max 5 per learning type, sorted by occurrences
- **Backward compatible:** All new JSON fields use `.get()` with `{}` or `[]` defaults

---

## Session: 2026-02-04 — Popup Dismissal Rewrite

### Overview
Complete rewrite of the popup dismissal system. The old approach (JS `.click()` inside
`page.evaluate()`) only fired a bare `click` event — React 18's event delegation expects
the full pointer event sequence (`pointerdown→mousedown→pointerup→mouseup→click`),
causing ~50% dismissal reliability. Additionally, popups spawning during the ~8s vision
API call were invisible to the Python-side code until the API returned.

### Architecture: Two-Layer Auto-Dismiss

#### Layer 1: JS Auto-Dismiss Interval (`POPUP_AUTO_DISMISS_SETUP_JS`)
- Runs every **400ms inside the page** via `setInterval`
- Handles ALL popup z-indexes (9996–9999) instantly, even during vision API calls
- `fullClick()`: dispatches full pointer event sequence on element (React 18 compatible)
- `pressSpace()`: `focus()` + `KeyboardEvent('keydown/keyup', {key: ' '})` for Radix radio
- Injected once in `start_fresh()` after navigation
- Handles the full "Please Select" modal flow: radio select → submit click

#### Layer 2: Playwright-Based Backup (`dismiss_all_popups()`)
- `_try_radio_focus_space()`: JS `focus()` + `page.keyboard.press('Space')`
- `CLICK_SUBMIT_JS`: JS `fullClick()` directly on Submit button element
- Click-verify-retry loop (5 attempts, 200ms gaps) as safety net
- Falls back to JS full pointer event dispatch on retries

#### Key Discoveries
- **JS `.click()` alone fails with React 18** — must dispatch full pointer event sequence
- **Synthetic events work**: React doesn't check `isTrusted`, responds to any events
  that bubble to the delegation root with correct `event.target`
- **Radix radio buttons**: coordinate click misses because label covers the small button.
  Fix: `radio.focus()` + Space keyboard event targets the exact element
- **Off-screen radio**: Modal scrolls to bottom to render all options → radio at y=-197.
  Fix: `scrollIntoView({ block: 'center' })` before `getBoundingClientRect()`
- **Submit button miss**: Same coordinate issue. Fix: JS `fullClick()` directly on element

### Files Modified

| File | Changes |
|------|---------|
| `agents/popup.py` | Added `POPUP_AUTO_DISMISS_SETUP_JS`, `FOCUS_CORRECT_RADIO_JS`, `CLICK_SUBMIT_JS`, `_try_radio_focus_space()`. Rewrote `dismiss_all_popups()` with radio focus strategy + submit JS click. Renamed `DISMISS_JS` → `DISMISS_JS_LEGACY`. |
| `agents/base.py` | Added `dismiss_popups(page)` method for all future agents |
| `agents/select_popup.py` | Updated to use `_try_radio_focus_space()` for radio selection |
| `orchestrator.py` | Rewrote `_dismiss_popups()` to loop `dismiss_all_popups()`. Updated `add_locator_handler`. Inject `POPUP_AUTO_DISMISS_SETUP_JS` in `start_fresh()`. |
| `agents/audio.py` | Swapped `page.evaluate(DISMISS_JS)` → `dismiss_all_popups(page)` |
| `agents/draw.py` | Swapped DISMISS_JS calls → `dismiss_all_popups(page)` loop |
| `agents/extract_code.py` | Swapped import |
| `agents/hidden_dom.py` | Swapped `_dismiss_blocking_modals()` to use `dismiss_all_popups()` |
| `agents/hover.py` | Swapped import |
| `agents/vision_learning.py` | Swapped all DISMISS_JS calls to `dismiss_all_popups()` loops |

### Results
- **Before**: Popups took 5–14s to dismiss (waited for vision API to return)
- **After**: Popups dismissed within 400ms of appearing (JS interval handles them)
- **Radio modal**: Was failing 100% (5 retries then FAILED). Now succeeds on first attempt.
- **5/5 steps pass** with zero popup retries or failures in test runs

---

## Session: 2026-02-04 — Learning V2 Refinements

### Overview
Eight targeted fixes to the Learning V2 system: seeding, routing, confidence tuning,
and failure handling improvements.

### Fixes Applied

#### Fix #1: V1 Seed Strategies
- Added `V1_SEED_STRATEGIES` dict with 15 challenge types
- `_seed_defaults()` called on first load and after JSON migration to fill missing types
- Bootstrap prior: `successes=2, attempts=3` (Wilson ≈ 0.386)
- Result: 16 total learnings (8 migrated + 8 seeded)

#### Fix #2: Track Winning Agent
- Added `last_winning_agent` variable set at every code-finding point in `run_step()`
- Passed to `bootstrap_learning()` for accurate first-time learning creation
- Covers: hidden_dom, click_reveal, hover, delay, websocket, draw, drag_drop, decode, audio

#### Fix #3: Failure Deduplication
- Before LLM refinement, compare last two `failure_history` entries
- If sanitized `why` fields match → skip refinement (saves API cost)

#### Fix #5: Less Conservative Wilson Score
- Changed from `z=1.96` (95% CI) to `z=1.0` (~84% CI)
- New learnings not crushed: 1/1→0.500, 2/3→0.386, 3/3→0.750, 5/5→0.833
- Bootstrap prior changed to `successes=2, attempts=3` (was `1, 1`)

#### Fix #6: Smarter Variant Eviction
- Minimum 3 attempts before a variant can be evicted
- Uses success ratio (`successes/attempts`) instead of raw Wilson score
- Falls back to evicting oldest variant if all are undertested

#### Fix #7: Agent Health Routing
- `AgentTracker` wired into `KnowledgeReader` via `set_agent_tracker()`
- `_pick_best_variant()` now factors `agent_health = max(0.3, 1.0 - failure_rate * 0.5)`
- Added `get_failure_rate()` to `AgentTracker` (returns 0.0 if <3 calls)

#### Fix #9: Rollback Failure Logging
- Logs error_info + dom_change_score before triggering rollback
- Provides context for debugging why rollbacks happen

#### Fix #10: Sanitize on Write
- `record_failure()` now sanitizes `why` and `what_tried` via `_sanitize_text()`
- Prevents session codes from leaking into persisted failure_history

### Files Modified

| File | Changes |
|------|---------|
| `knowledge_reader.py` | V1_SEED_STRATEGIES, _seed_defaults(), Wilson z=1.0, create_variant eviction, set_agent_tracker, _pick_best_variant agent_health, record_failure sanitization |
| `orchestrator.py` | last_winning_agent tracking, failure dedup in _on_learning_failed, rollback logging, bootstrap prior (2/3) |
| `agents/learning.py` | winning_agent param in bootstrap_learning() |
| `agent_tracker.py` | get_failure_rate() method |

---

## Session: 2026-02-04 — Learning Agent V2

### Overview
Replaced the accumulate-many-learnings JSONL system with **one canonical learning per
challenge type** (bounded variants, max 3) that **refines itself through failure analysis**.
Stripped useful ideas from the unused `learning/` infrastructure (~2500 lines) and integrated
them into a streamlined system. Deleted `learning_db.py` and moved `learning/` to
`learning_archived/`.

### What Changed

#### 1. `knowledge_reader.py` — Complete Rewrite
- **New dataclasses:** `CanonicalLearning` (1 per challenge type) + `StrategyVariant` (max 3 per type)
- **Wilson score** as sole confidence metric (no manual decay)
- **Scored detection router:** `match_score = 0.5*keyword + 0.4*dom + 0.1*flags`
- **DOM change score** (float 0.0–1.0) replaces simple boolean for failure diagnosis
- **Atomic JSON writes** via `os.replace()` + `fsync` (crash-safe on POSIX & Windows)
- **Bounded variants** with preconditions to prevent subtype thrashing
- **Multi-level rollback** (`previous_versions` snapshots, keep last 2–3)
- **TTL-based disable** (circuit breaker: 5+ consecutive failures → disable for rest of run, reset on next run)
- **One-time JSONL → JSON migration** with scoped sanitization (strips session codes from natural language fields only)
- Extracted: `wilson_score_lower()` from `feedback.py`, `extract_failure_pattern()` from `agent_improver.py`, detection patterns from `retrieval.py`
- Storage: `knowledge/learnings.json` (was `learnings.jsonl`)

#### 2. `agents/learning.py` — Two New Methods
- **`refine_learning()`**: Constrained LLM refinement with candidate elements, `ACTION_SCHEMAS` validation, dom_change_score diagnosis. Can produce `parameter_tweak`, `logic_rewrite`, or `new_variant`.
- **`bootstrap_learning()`**: LLM-bootstrapped first-time learning from a successful episode. Extracts action_type, action_params, detection_keywords, dom_signals.
- Deprecated `get_known_patterns()` (returns None).

#### 3. `agent_tracker.py` — New File
- Lightweight per-agent success/failure tracking (extracted from `agent_improver.py`)
- `AgentPerformance` dataclass: total_calls, successes, failures, recent_failures, failure_patterns
- Persisted to `knowledge/agent_performance.json`
- Singleton via `get_agent_tracker()`

#### 4. `orchestrator.py` — Moderate Changes
- **Removed:** All `learning_db.py` imports, all `learning/*.py` imports, `LEARNING_SYSTEM_AVAILABLE` flag
- **Removed:** Dead fields (observer, pattern_retriever, strategy_executor, self_play, feedback_tracker, working_memory, episode_store, agent_improver)
- **Added:** DOM signature capture around agent dispatch (`dom_sig_before` / `dom_sig_after`)
- **Added:** `_on_learning_succeeded()` — Wilson score update + agent tracking
- **Added:** `_on_learning_failed()` — rollback-first logic (3+ consecutive → rollback, 5+ → disable), then LLM refinement (max 1 per step)
- **Added:** `_on_step_succeeded_new()` — LLM-bootstrapped learning for first-time types
- **Added:** `reset_disabled()` call at run start

#### 5. Cleanup
- **Deleted:** `learning_db.py`, `knowledge/strategies.json`
- **Moved:** `learning/` → `learning_archived/` (7 files, ~2500 lines preserved in git)
- **Migrated:** `knowledge/learnings.jsonl` → `learnings.json` (26 JSONL entries → 8 canonical learnings)

### Migration Results
| Metric | Before | After |
|--------|--------|-------|
| Storage entries | 26 JSONL (11 hover, 6 drag_drop dupes) | 8 canonical types |
| Storage systems | 3 (JSONL + JSON LRU + SQLite) | 1 (atomic JSON) |
| Dead code | ~2500 lines in `learning/` | 0 (moved to `learning_archived/`) |
| Confidence metric | Manual decay + Wilson (double-penalizing) | Wilson only |
| Failure feedback | None (retry same strategy forever) | Rollback + LLM refinement + disable |

### Files Modified

| File | Change |
|------|--------|
| `knowledge_reader.py` | Complete rewrite (dataclasses, JSON storage, Wilson score, scored detection, DOM change score, migration) |
| `agents/learning.py` | Added `refine_learning()`, `bootstrap_learning()` |
| `agent_tracker.py` | New file (agent performance tracking) |
| `orchestrator.py` | Removed dead imports/code, added V2 integration methods |
| `learning_db.py` | Deleted |
| `knowledge/strategies.json` | Deleted |
| `learning/` (7 files) | Moved to `learning_archived/` |
| `knowledge/learnings.jsonl` | Migrated to `learnings.json`, backed up as `.jsonl.migrated_bak` |

---

## Session: 2026-02-04 — Playwright Power-Ups

### Overview
Replaced polling loops and manual workarounds with native Playwright 1.57 APIs.
Reduced `wait_for_timeout` calls in orchestrator.py from **38 → 4**. Created
comprehensive documentation (CLAUDE.md, ARCHITECTURE.md, updated README.md).

### Playwright 1.57 Optimizations

#### 1. Auto-Dismiss Popups via `add_locator_handler` (orchestrator.py)
**What:** Registered a locator handler in `start_fresh()` that fires automatically
before every locator-based action (click, fill, hover, drag). Runs `DISMISS_JS` +
`CLEAR_BLOCKERS_JS` from popup.py.
**Impact:** Eliminated ~5 explicit `_run_agent("popup", ...)` calls. Removed
`_check_popup_timing()` method entirely. Kept 1 manual popup call at step start
as safety net.

#### 2. Mutation-Driven Code Detection via `wait_for_function` (delay.py, draw.py)
**What:** Replaced polling loops (`for i in range(N): wait_for_timeout(X); check()`)
with `page.wait_for_function(..., polling="mutation")` which re-evaluates only when
the DOM changes.
**Impact:** Code detection within ~16ms of DOM change (was up to 1000ms). Applies to
delay agent timed-reveal challenges and draw agent post-gesture code detection.

#### 3. Instant URL Change Detection via `wait_for_url` (orchestrator.py)
**What:** Replaced 20-iteration `wait_for_timeout(200) + if page.url != current_url`
loop with `page.wait_for_url(lambda url: url != current_url, timeout=N)`.
**Impact:** Detects navigation instantly instead of up to 4s late. Also replaced
the 800ms sleep after clicking START in `start_fresh()`.

#### 4. Built-in Mouse Interpolation via `mouse.move(steps=N)` (draw.py)
**What:** Replaced manual `for i in range(steps): mouse.move(...); wait_for_timeout(20)`
loops in `_draw_line` and `_draw_circle` with `page.mouse.move(x, y, steps=10)`.
**Impact:** Cleaner code, eliminates per-point 20ms waits (Playwright interpolates
internally).

#### 5. Skip CSS Animations via `emulate_media` (orchestrator.py)
**What:** Added `page.emulate_media(reduced_motion="reduce")` in `start_fresh()`.
**Impact:** Pages respecting `prefers-reduced-motion` skip CSS transitions (~1-2s
saved on animated challenges).

#### 6. Native Drag-and-Drop via `locator.drag_to` (drag_drop.py)
**What:** Rewrote `_strategy_playwright_drag` to use `locator.nth(i).drag_to(
target.nth(i), source_position={"x": 25, "y": 25})` instead of manual mouse
event sequences. Also simplified `_mouse_drag_one` with `mouse.move(steps=10)`.
**Impact:** More reliable drag operations, eliminated manual interpolation loops.

#### 7. Iframe Traversal via `frame_locator` (shadow_dom.py)
**What:** Added `_search_iframes()` method that chains `page.frame_locator("iframe")`
up to 3 levels deep. Clicks reveal buttons inside iframes and extracts codes.
**Impact:** Can now handle nested iframe challenges that were previously missed.

### `_submit_and_wait` Helper (orchestrator.py)

**Problem:** 8+ identical "submit + sleep 300ms + check URL" patterns scattered
throughout `run_step()`.

**Solution:** Created `_submit_and_wait(page, code, step, version, current_url)` that
combines `code_entry` agent + `wait_for_url()` in one call. Returns `True` if URL
changed (step advanced), `False` otherwise.

**Replaced patterns in:**
- Vision-extracted code submission
- Observer hook code submission
- `harvest_and_score()` code submission
- `extract_code` agent code submission
- Hidden DOM / Shadow DOM / Decode fallback submissions
- Audio detection submission
- Gesture/canvas detection submission
- Radio detection submission
- Vision Learning Agent submission
- `_execute_learning_action` submit branches
- `_execute_stored_action` submit branches

### Other `wait_for_timeout` Eliminations

- `fix_stale_content()` — replaced with `wait_for_raf_stable`
- `handle_stale()` — replaced with `wait_for_raf_stable`
- `speed_run_to()` — replaced with `wait_for_raf_stable`
- `_execute_learning_action` 'wait' action — delegates to delay agent
- `_execute_stored_action` 'wait' action — delegates to delay agent

### Remaining `wait_for_timeout` Calls (4 total, all justified)

1. `start_fresh()` — 500ms fallback if `wait_for_url` times out after START click
2. `_safe_click()` — 100ms post-click check (safety margin for URL guard)
3. `_recover_from_navigation()` — 300ms after pushState (React needs time to process)
4. Hover animation wait — 800ms (some hover challenges need sustained hover)

### Documentation Created

| File | Type | Content |
|------|------|---------|
| `../CLAUDE.md` | NEW | Top-level session orientation for Claude Code |
| `README.md` | REWRITTEN | Full agent registry, Playwright features, design decisions |
| `docs/ARCHITECTURE.md` | NEW | System design, data flow, per-step pipeline, invariants |
| `docs/CHANGELOG.md` | UPDATED | This entry |
| `docs/SESSION_STATE.md` | UPDATED | Post-optimization status |

### Files Modified

| File | Changes |
|------|---------|
| `orchestrator.py` | `add_locator_handler`, `emulate_media`, `wait_for_url`, `_submit_and_wait` helper, removed `_check_popup_timing`, 38→4 `wait_for_timeout` |
| `agents/delay.py` | `wait_for_function(polling="mutation")` replaces polling loop |
| `agents/draw.py` | `mouse.move(steps=N)`, `_wait_for_code` helper with `wait_for_function` |
| `agents/drag_drop.py` | `locator.drag_to()`, `mouse.move(steps=10)` in `_mouse_drag_one` |
| `agents/shadow_dom.py` | Added `_search_iframes()` with `frame_locator()` chaining |

### Estimated Performance Impact
- **Popup handling:** ~5-15s saved per run (no explicit popup calls needed)
- **Code detection:** ~5-10s saved per run (mutation-driven vs polling)
- **URL detection:** ~2-4s saved per run (instant vs up to 4s polling)
- **Total estimated:** ~15-30s per full run
- **Avg time per step:** ~8s (down from ~12s pre-optimizations)

---

## Session: 2026-02-03 (Latest)

### Critical Fixes - Double-Advance Issue

#### 5. Double-Advance Fix (orchestrator.py)
**Problem:** Steps were being skipped (URL shows step 8, expected 7). The solver was accidentally advancing two steps at once.
**Root Cause:** When a code was submitted (e.g., via early hidden_dom detection), the rest of `run_step()` continued executing and submitted the same or another code again, causing double-advance.
**Fix:** Added `code_already_submitted` flag to track when a code has been submitted:
- Set flag after early hidden_dom submission
- Check flag before main code submission
- Check flag before retry submissions
- Check flag before Learning Agent code submission
- Check flag before Self-Play code submission

#### 6. Improved URL Change Detection (orchestrator.py)
**Problem:** After submitting a code during retry, we only waited 400ms and checked once. Sometimes the URL change took longer.
**Fix:** Changed to check URL change multiple times (5x200ms = 1 second total) after submissions in retry sections.

#### 7. Verified Learning Tracking (knowledge_reader.py)
**Problem:** When Learning Agent or Self-Play successfully solved a step, the strategy wasn't being saved as "verified".
**Fix:**
- Added `verified` field to `StoredLearning` dataclass
- Added `mark_verified(step, version, code)` method to persist success
- Updated sorting to prioritize verified learnings: `(verified, confidence, timestamp)`
- Learning Agent and Self-Play now call `mark_verified()` on success

Files changed:
- `orchestrator.py` - `code_already_submitted` flag, improved URL checks, mark_verified calls
- `knowledge_reader.py` - `verified` field, `mark_verified()` method

### Performance Results
- **Before fixes:** Steps skipped (6/30 but with skips)
- **After fixes:** 8/30 steps completed without skips
- Learning Agent calls reduced (only 1-4 per run)
- Knowledge usage increased (50+ learnings loaded)

### Remaining Issues
1. **Drag-drop detection** - Still showing "0 free pieces, 0 empty slots"
2. **Hover challenges** - Not maintaining hover long enough
3. **Click reveal** - Sometimes code not extracted after click

---

## Session: 2026-02-03 (Earlier)

### Critical Fixes

#### 1. Blank Page Fix (popup.py, click_reveal.py)
**Problem:** Pages were going blank between steps.
**Root Cause:** `el.remove()` calls in popup agent's Strategy 5 and click_reveal agent were removing `.fixed.inset-0` elements - which in Tailwind CSS can be main content containers, not just overlays.
**Fix:** Removed all `el.remove()` calls. Now only click dismiss buttons instead of removing DOM elements.

Files changed:
- `agents/popup.py` - Removed Strategy 5's `el.remove()`
- `agents/click_reveal.py` - Removed `el.remove()` in modal clearing code

#### 2. Modal Not Dismissed (popup.py)
**Problem:** "Please Select an Option" modal was blocking challenges (especially hidden_dom clicks).
**Root Cause:** Existing strategies weren't catching all Cancel buttons.
**Fix:** Added multiple new dismissal strategies:
- Strategy 0: Aggressive Cancel button clicking (exact text match)
- Strategy 7: Escape key press
- Strategy 8: Click on backdrop/overlay
- Strategy 9: Direct Playwright click on Cancel buttons

#### 3. Hidden DOM Agent Blocked by Modals (hidden_dom.py)
**Problem:** Hidden DOM agent was clicking 3 times but modal was intercepting clicks.
**Fix:** Added `_dismiss_blocking_modals()` method that runs before any click attempts.

#### 4. Learning Agent API Architecture (agents/learning.py)
**Problem:** Learning agent was creating its own Anthropic client, making redundant API calls.
**Root Cause:** Duplicate API connections - vision had one, learning had another.
**Fix:**
- Learning agent now uses SHARED vision client
- Removed `_ensure_client()` and separate `_client`
- Added `set_vision_agent()` to link to vision agent
- Increased `max_tokens` to 5000 for thorough analysis

Files changed:
- `agents/learning.py` - Refactored to use shared client
- `agents/__init__.py` - Wires learning agent to vision agent

### New Features

#### Agent Improver System (learning/agent_improver.py)
New system for tracking agent performance and generating improvements:
- Tracks success/failure rates per agent
- Identifies failure patterns
- Generates patches with goal-aware reasoning
- Validates patches before applying (syntax check, safety check)
- Can rollback patches if needed
- Only applies HIGH confidence patches

Integration points:
- `orchestrator.py` - `_track_agent_failure()`, `_track_agent_success()`, `_check_for_blocking_modal()`

### Architecture Notes

**API Usage:**
- Vision agent creates the ONE Anthropic client
- Learning agent shares that client (no separate connection)
- All screenshot analysis goes through vision client
- Text/code analysis done locally without API

**Agent Wiring (agents/__init__.py):**
```python
_vision_agent = VisionAgent()
_learning_agent = LearningAgent()
_learning_agent.set_vision_agent(_vision_agent)
```

### Files Modified This Session
- `agents/popup.py` - Modal dismissal improvements
- `agents/click_reveal.py` - Removed dangerous el.remove()
- `agents/hidden_dom.py` - Added modal dismissal before clicks
- `agents/learning.py` - Shared vision client, higher token limit
- `agents/__init__.py` - Agent wiring
- `orchestrator.py` - Agent tracking, modal detection, learning agent re-enabled
- `learning/agent_improver.py` - NEW FILE

### Known Issues
- Self-play may still make external API calls (not yet refactored)
- Some challenges still fail (step 4/5 hidden DOM with persistent modals)

### Testing Notes
Run with: `python solve.py --max-steps=5 --headed`
Watch for:
- No blank pages between steps
- Modals should be dismissed before challenge actions
- Learning agent should use shared vision client (check logs for "linked to vision agent")
