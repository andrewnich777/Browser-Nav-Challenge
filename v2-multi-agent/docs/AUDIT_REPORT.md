# Full Codebase Audit Report — 2026-02-06

**Scope:** All Python files in `v2-multi-agent/` (orchestrator, sidecar, primitives, knowledge_reader, code_scorer, init_hooks, config, agents/, utilities)

**Total issues found:** 127 (deduplicated)
**Breakdown:** 12 CRITICAL, 19 HIGH, 38 MEDIUM, 58 LOW

---

## TIER 0 — CRITICAL BUGS ON ACTIVE CODE PATH

These bugs fire during normal solver execution and directly cause step failures or SPA crashes.

### C1. Element-targeted scroll anti-loop is completely broken
**Files:** `learning_sidecar.py:1098-1136, 559-561`
**Impact:** Scrollable container challenges (step 16 v3 sequence, step 14 parts) fail after first scroll attempt

Element-targeted scrolls never set `scroll_moved` in `hit_info` because `_sy_before` is only captured for page-level scrolls (`scroll_eid is None`). The anti-loop exemption at line 560 (`if hit_info.get('scroll_moved')`) never fires. After one scroll, anti-loop blocks all further scrolls on that element.

### C2. `_action_sig` scroll signature collapses all directions on same element
**File:** `learning_sidecar.py:44-58`
**Impact:** "scroll up" then "scroll down" on same BID are treated as duplicate, blocked by anti-loop

For any action with `element_id`, the function returns `(atype, f'bid:{eid}')` at line 47, before reaching the scroll-specific branch. All scroll directions on the same element share one signature.

### C3. Iframe coordinate offset math is wrong
**File:** `learning_sidecar.py:319-325`
**Impact:** All clicks on iframe elements miss their targets

```python
fel['x'] = fx - fw // 2 + fel['x']   # WRONG: shifts left by half iframe width
fel['y'] = fy - fh // 2 + fel['y']   # WRONG: shifts up by half iframe height
```
Should be `fx + fel['x']` and `fy + fel['y']` since `fx`/`fy` are already the iframe's top-left corner.

### C4. `progress_guided_exploration` uses `page.go_back()` — risks 404
**File:** `primitives.py:737-742`
**Impact:** Can crash the SPA during Phase 2.5 exploration

Also has a logic error: uses `or` instead of `and` in the URL check (`'/step' not in page.url or 'version=' not in page.url`).

### C5. `<a>` elements get BIDs without anchor danger flag
**File:** `primitives.py:1435`
**Impact:** Planner can click `<a>` links, causing 404 navigation (invariant #2 violation)

Pass 2 of `annotate_elements` scans `'div, span, p, li, label, td, a'` for cursor:pointer elements. `<a>` tags get BIDs and appear as `'clickable'` type without any anchor warning. The sidecar will happily click them.

### C6. `_recover_from_navigation` dispatches PopStateEvent
**File:** `orchestrator.py:261-264`
**Impact:** Violates invariant #6 — can cause white screen during error recovery

```javascript
window.history.pushState({}, '', '{target_path}');
window.dispatchEvent(new PopStateEvent('popstate', {state: {}}));
```
CLAUDE.md explicitly lists this as forbidden.

### C7. `popup.py` CLEAR_BLOCKERS_JS mutates DOM styles
**File:** `agents/popup.py:622-623`
**Impact:** Sets `el.style.opacity = '0'` and `el.style.pointerEvents = 'none'` — invariant #6 violation

Called on **every step** from orchestrator and sidecar. Technically should cause white screens per the invariant, but may be tolerated because it only targets overlay elements, not React-managed content.

### C8. `code_entry.py` force-enables disabled buttons
**File:** `agents/code_entry.py:128-132`
**Impact:** Sets `btn.disabled = false`, removes attributes — invariant #6 violation

Called on **every code submission**. Forces React-controlled button state.

### C9. `record_failure` crashes with NameError on empty variants list
**File:** `knowledge_reader.py:1240`
**Impact:** If `learning.variants` is empty, `v` is unbound → NameError crash

Same loop-variable-after-loop antipattern in `record_success` (line 1192) and `refine_variant` (line 1361). All three methods log the wrong variant's data when variant_id doesn't match.

### C10. `read_progress` fraction can be Infinity when total=0
**File:** `primitives.py:132`
**Impact:** `Infinity >= 1.0` is True → false "progress complete" detection

`fraction: best.current / best.total` — no zero-denominator guard.

### C11. `_colorCat()` alpha channel parsing bug
**File:** `init_hooks.py:258-262`
**Impact:** Semi-transparent elements (rgba 0.5 alpha) treated as fully transparent

`/\d+/g` splits `0.5` into `["0", "5"]`. `m[3]` becomes `"0"`, alpha = 0 → returns `'none'`.

### C12. `__snapshotBaseline()` regex lacks word boundaries
**File:** `init_hooks.py:52-53`
**Impact:** Real codes silently filtered as "stale baseline" if they appear as substrings of longer text

Uses `[A-HJ-NP-Z2-9]{6}` without `\b`, while the main scanner uses `\b[...]{6}\b`.

---

## TIER 1 — HIGH BUGS (Reliability Risks)

### H1. Tier demotion system is a no-op — replay_successes never reset
**File:** `orchestrator.py:698, 725-735`
**Impact:** Failing recipes oscillate: fail 5x → demote → succeed 1x → re-promote immediately

`replay_successes` retains old count through demotion. Since tier-1→2 requires `>=1` and tier-2→3 requires `>=3`, one success after demotion re-promotes instantly.

### H2. Tier-1 promotion threshold of 1 makes tier-1 stage meaningless
**File:** `orchestrator.py:701`
**Impact:** Single lucky replay → permanent tier-2 status

### H3. `wait_for_state` drains non-matching changes permanently
**File:** `primitives.py:1816-1832`
**Impact:** State changes not matching the caller's filter are lost forever

`drain_state_changes()` clears the JS buffer. Non-matching changes are iterated but never returned or re-queued.

### H4. Signal 3 in `_submit_and_record` is unreachable
**File:** `orchestrator.py:890-903`
**Impact:** DOM-based advancement detection never fires (gates on URL change that Signal 1 already caught)

### H5. Progress appearing from nothing flagged as "no effect"
**File:** `learning_sidecar.py:553-558`
**Impact:** Action that CREATES a progress indicator is blocked by anti-loop

If `progress_before is None` and `progress_after is not None`, the condition evaluates as "no effect" because `progress_before is None` is True.

### H6. Iframe elements invisible to planner + BIDs don't resolve in DOM
**File:** `learning_sidecar.py:264-279, 322`
**Impact:** Iframe-based challenges completely non-functional via BID system

`element_catalog_for_planner` and `interactable_bids` computed before iframe scan. Iframe BIDs are Python-only (no `data-bid` DOM attribute set).

### H7. `_seed_defaults` defined but never called
**File:** `knowledge_reader.py:810-861`
**Impact:** Fresh installs start with zero learnings — no bootstrap knowledge

### H8. Scroll direction only handles up/down
**File:** `learning_sidecar.py:1098-1131`
**Impact:** Horizontal scrolls silently become vertical scrolls

### H9. `_stale_instr_candidates` not reset on step change
**File:** `learning_sidecar.py:152, 165-168`
**Impact:** Codes from step N get -0.15 penalty on step N+1

### H10. Completion sweep uses JS `.click()` which may fail with React 18
**File:** `learning_sidecar.py:1431`
**Impact:** "Reveal Code" button click may silently fail after progress=100%

### H11. Large-container filter drops video/audio elements
**File:** `primitives.py:1387`
**Impact:** Video/audio elements larger than 800x400 filtered out of BID catalog

### H12. Drag fallback uses `page.locator().bounding_box()` violating project rule
**File:** `learning_sidecar.py:1191-1203`
**Impact:** Popup handler eats timeout during drag resolution

### H13. JS ternary precedence bug in DOM_SIGNATURE_JS
**File:** `knowledge_reader.py:417`
**Impact:** Checked checkboxes produce `"true"` instead of `"1"` in DOM signatures

### H14. `classify_state_changes` crashes on empty changes list
**File:** `primitives.py:1712`
**Impact:** `max()` on empty sequence → ValueError crash

### H15. `hidden_dom_click` detection pattern is unreachable
**File:** `knowledge_reader.py:346-350`
**Impact:** Detection type can never win over `hidden_dom` (shared patterns, lower total weight)

### H16. `_compute_flag_score` returns 0.3 when page is None, 0.0 when page exists
**File:** `knowledge_reader.py:1048-1066`
**Impact:** Detection results vary depending on whether page object is passed

### H17. `record_success` and `record_failure` log wrong variant data on miss
**File:** `knowledge_reader.py:1151-1195, 1207-1240`
**Impact:** Wilson scores logged for wrong variant. Timestamps updated even when no match found.

### H18. `harvest_and_score` frequency counting is always 1
**File:** `code_scorer.py:260-270`
**Impact:** JS-side deduplication makes frequency-based scoring dead code

### H19. Stale strategy hint persists across rounds
**File:** `learning_sidecar.py:338, 347-352, 417`
**Impact:** Planner follows stale strategy from a previous stall round

---

## TIER 2 — MEDIUM (Tech Debt / Potential Issues)

### M1. ~250 lines dead code in orchestrator.py
`_promote_to_system1` (115 lines), `_minimize_recipe` (62 lines), `_convert_vl_actions_to_recipe` (79 lines), `_safe_click` (13 lines), `verify_extraction` (4 lines). Several have bugs that would fire if revived.

### M2. 28 of 37 agent classes are DEAD CODE
Only 8 agents are on the active code path: popup, code_entry, dna_reasoner, recipe_executor, vision_learning, learning_sidecar, calculated, and (registered but unused) extract_code.

### M3. `_extract_code()` duplicated across 13+ agents
Copy-pasted with minor variations. `primitives.extract_code_js()` already exists but agents don't use it.

### M4. `_step_times.append(...)` duplicated in 10+ return paths
Fragile — any new return path silently misses timing. Should use try/finally.

### M5. `_promote_to_system1` discards constructed variant
Lines 1131-1147: builds `new_variant` with all data, then `create_variant()` ignores it and creates a blank one. Dead code but dangerous if revived.

### M6. Unbounded `__codeBus` and `__mutCodes` arrays
init_hooks.py: grow without cap. Linear dedup scan degrades over long sessions.

### M7. `_isDropTarget` Stage C: data-index check is dead code
primitives.py:1329-1332: Always returns true regardless of data-index attribute.

### M8. VisionClient ignores `config.VISION_MODEL` env var
vision_client.py:182: Hardcoded default instead of reading from config.

### M9. solve.py step counting bugs
Failed step increments `step` but URL doesn't change → next iteration breaks. Finish page detection math is fragile.

### M10. `get_challenge_type()` called redundantly 6 times per step
orchestrator.py: Same (step, version) lookup repeated across methods.

### M11. Inconsistent return types across agents
Base class declares `run() -> bool` but 17 agents return `str | None`.

### M12. Bare `except:` clauses in 10+ agent files
Swallows KeyboardInterrupt, SystemExit. Should be `except Exception:`.

### M13. Module-level docstring in knowledge_reader has stale scoring weights
Says "0.5*keyword + 0.4*dom + 0.1*flags" — actual is "0.35*keyword + 0.25*dom + 0.1*flags + 0.15*text_ctx + 0.15*dna".

### M14. `_compute_keyword_score` mixes regex with substring matching for stored keywords
knowledge_reader.py: Stored keywords matched via `in` (substring) vs DETECTION_PATTERNS via `re.search`.

### M15. Double `_save()` calls in multiple code paths
orchestrator.py and knowledge_reader.py: record_success/record_failure call _save(), then caller saves again.

### M16. Redundant imports inside method bodies
`import re as _re` at orchestrator.py:436, redundant `from code_scorer import harvest_and_score` at line 463.

### M17. `annotate_elements` context count budget wasted
primitives.py:1477-1487: `contextCount++` runs even when `addEl` returns early without adding element.

### M18. DOM signature hashing provides false precision
knowledge_reader.py:1122-1136: MD5 avalanche means any change → ~0.5 score. Cannot distinguish minor from major changes.

### M19-M38. (Additional medium issues — see individual audit sections for details)

---

## TIER 3 — LOW (Style / Minor)

58 LOW issues spanning: dead data entries in DECOY_CODES, redundant checks, naming inconsistencies, stale comments, duplicate utility functions (page_state.py vs verify.py), 14 diagnostic scripts mixed into source directory, archived learning system still present, etc.

---

## TOP 10 FIXES BY IMPACT ON SOLVER SCORE

Prioritized by likelihood of solving more steps:

| Priority | Issue | Fix Effort | Expected Impact |
|----------|-------|------------|-----------------|
| 1 | C1+C2: Scroll anti-loop broken | Small | Unblocks scroll-dependent steps (14, 16) |
| 2 | C3: Iframe coord math wrong | Tiny | Unblocks iframe challenges (step 24+) |
| 3 | C5: `<a>` tags get clickable BIDs | Small | Prevents 404 crashes from planner clicking links |
| 4 | H1+H2: Tier system is no-op | Small | Prevents bad recipes from being sticky |
| 5 | H5: Progress-from-nothing = no effect | Tiny | Unblocks challenges where progress indicator appears |
| 6 | H6: Iframe elements invisible to planner | Medium | Enables iframe interaction via BID system |
| 7 | C4: page.go_back() in exploration | Small | Prevents SPA crashes during Phase 2.5 |
| 8 | C11: Alpha parsing bug | Tiny | Fixes color transition detection for rgba elements |
| 9 | C12: Baseline regex no word boundaries | Tiny | Prevents real codes being filtered as stale |
| 10 | H9: Stale instruction candidates | Tiny | Prevents valid codes from getting penalized |

---

## DEAD CODE SUMMARY

| Category | Lines | Files |
|----------|-------|-------|
| Dead orchestrator methods | ~250 | orchestrator.py |
| Dead agent classes (28 of 37) | ~3500+ | agents/*.py |
| Archived learning system | ~800+ | learning_archived/*.py |
| Diagnostic scripts (14) | ~1500+ | v2-multi-agent root |
| Dead detection patterns | ~20 | knowledge_reader.py |
| Dead DECOY_CODES entries | 18 | code_scorer.py |
| **Total dead code** | **~6000+** | |

The active codebase is approximately 4000 lines across 8 files. Dead code exceeds active code by ~50%.
