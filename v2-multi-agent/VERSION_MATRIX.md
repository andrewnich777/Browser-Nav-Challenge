# VERSION_MATRIX.md — Cross-Version Agent Performance Tracker

> Updated after each validation run. Tracks V4 agent success/failure across all 3 site versions with timing.
> Steps 1-15 are "simple" (runtime-detected) — challenge types rotate within that range per session.

## Step-Type Mappings (Steps 16-30)

| Step | v=1               | v=2               | v=3               |
|------|-------------------|-------------------|-------------------|
| 16   | multi_tab         | gesture           | sequence          |
| 17   | gesture           | sequence          | puzzle_solve      |
| 18   | sequence          | puzzle_solve      | calculated        |
| 19   | puzzle_solve      | calculated        | multi_tab         |
| 20   | calculated        | multi_tab         | gesture           |
| 21   | shadow_dom        | websocket         | service_worker    |
| 22   | websocket         | service_worker    | mutation          |
| 23   | service_worker    | mutation          | recursive_iframe  |
| 24   | mutation          | recursive_iframe  | conditional_reveal|
| 25   | recursive_iframe  | conditional_reveal| multi_tab         |
| 26   | conditional_reveal| multi_tab         | sequence          |
| 27   | multi_tab         | sequence          | calculated        |
| 28   | sequence          | calculated        | shadow_dom        |
| 29   | calculated        | shadow_dom        | websocket         |
| 30   | shadow_dom        | websocket         | service_worker    |

## Steps 1-15: Simple Challenge Types (runtime detected)

These rotate within the session. Common types seen in steps 1-15:
- click_reveal, hidden_dom, code_entry, toggle, scroll_reveal
- color_match, decode, slider, split_parts, audio
- drag_drop, hover, timing, delayed_reveal, video
- delay_memory, conditional_reveal, keyboard_sequence

---

## Run Log

### Run R1 — v=2, 2026-02-12 (baseline, pre-drag_drop-fix)

| Step | Type (detected)     | Solver  | Time (s) | Result | Notes |
|------|---------------------|---------|----------|--------|-------|
| 1    | click_reveal        | V4      | 1.3      | OK     |       |
| 2    | hidden_dom          | V4      | 1.5      | OK     |       |
| 3    | delayed_reveal      | Sidecar | ~25      | OK     | V4 regression |
| 4    | code_entry          | V4      | 1.2      | OK     |       |
| 5    | toggle              | V4      | 1.4      | OK     |       |
| 6    | scroll_reveal       | V4      | 0.9      | OK     |       |
| 7    | color_match         | V4      | 2.1      | OK     |       |
| 8    | decode              | V4      | 1.6      | OK     |       |
| 9    | slider              | V4      | 1.8      | OK     |       |
| 10   | drag_drop           | Sidecar | ~30      | OK     | V4 got 2/6 |
| 11   | gesture             | Sidecar | ~45      | OK     | V4 got 1/3 strokes |
| 12   | split_parts         | V4      | 2.4      | OK     |       |
| 13   | audio               | V4      | 3.1      | OK     |       |
| 14   | video               | V4      | 4.2      | OK     |       |
| 15   | delay_memory        | V4      | 8.3      | OK     |       |
| 16   | gesture             | Sidecar | ~40      | OK     | V4 stroke fail |
| 17   | sequence            | Sidecar | ~35      | OK     | Hover sub-task fails |
| 18   | puzzle_solve        | V4      | 1.4      | OK     |       |
| 19   | calculated          | V4      | 2.8      | OK     |       |
| 20   | multi_tab           | V4      | 3.5      | OK     |       |
| 21   | websocket           | V4      | 5.2      | OK     |       |
| 22   | service_worker      | V4      | 7.1      | OK     |       |
| 23   | mutation            | V4      | 2.3      | OK     |       |
| 24   | recursive_iframe    | V4      | 4.8      | OK     |       |
| 25   | conditional_reveal  | Sidecar | ~30      | OK     | Hover sub-task fails |
| 26   | multi_tab           | V4      | 3.2      | OK     |       |
| 27   | sequence            | V4      | 4.5      | OK     | Non-hover sub-tasks only |
| 28   | calculated          | V4      | 2.1      | OK     |       |
| 29   | shadow_dom          | V4      | 3.5      | OK     |       |
| 30   | websocket           | V4      | 5.8      | OK     | Step 30 hook |

**Summary R1**: 30/30, 25 V4, 5 sidecar, ~371s, ~$0.20

---

### Run R2 — v=3, 2026-02-12 (post-drag_drop-fix, post-timing-fix)

| Step | Type (detected)     | Solver  | Time (s) | Result | Notes |
|------|---------------------|---------|----------|--------|-------|
| 1    | scroll              | V4      | 5.5      | OK     |       |
| 2    | delayed_reveal      | V4      | 7.9      | OK     |       |
| 3    | (EarlyCodeProbe)    | V4      | 1.4      | OK     |       |
| 4    | hidden_dom          | V4      | 5.5      | OK     |       |
| 5    | click_reveal        | V4      | 1.8      | OK     |       |
| 6    | delay_memory        | V4      | 6.6      | OK     |       |
| 7    | hover               | V4      | 3.9      | OK     |       |
| 8    | click_reveal        | V4      | 1.9      | OK     |       |
| 9    | drag_drop           | Sidecar | 38.0     | OK     | V4 got 2/6, Phase 2: 0 empty slots |
| 10   | keyboard_sequence   | V4      | 5.1      | OK     |       |
| 11   | audio               | V4      | 7.8      | OK     |       |
| 12   | video               | V4      | 7.8      | OK     |       |
| 13   | split_parts         | V4      | 5.6      | OK     |       |
| 14   | decode              | V4      | 6.3      | OK     |       |
| 15   | timing              | Sidecar | 44.0     | OK     | V4 found code but REJECTED (decoy) |
| 16   | sequence            | Sidecar | 33.8     | OK     | Hover sub-task fails (3/4 V4) |
| 17   | puzzle_solve        | V4      | 2.0      | OK     |       |
| 18   | calculated          | V4      | 5.8      | OK     |       |
| 19   | multi_tab           | V4      | 10.4     | OK     |       |
| 20   | gesture             | V4      | 7.1      | OK     | Triangle shape worked! |
| 21   | service_worker      | V4      | 4.3      | OK     |       |
| 22   | mutation            | V4      | 4.0      | OK     |       |
| 23   | recursive_iframe    | V4      | 14.9     | OK     |       |
| 24   | conditional_reveal  | V4      | 8.9      | OK     | Hover was pre-completed (✓) |
| 25   | multi_tab           | V4      | 10.2     | OK     |       |
| 26   | sequence            | Sidecar | 34.5     | OK     | Hover sub-task fails (3/4 V4) |
| 27   | calculated          | V4      | 2.0      | OK     | Correctly overridden to puzzle_solve |
| 28   | shadow_dom          | V4      | 4.9      | OK     |       |
| 29   | websocket           | V4      | 9.9      | OK     |       |
| 30   | service_worker      | V4      | 15.5     | OK     | Step 30 hook → /finish |

**Summary R2**: 30/30, 26 V4, 4 sidecar, 326.4s, $0.08

**V4 Failures in R2**:
- drag_drop (step 9): 2/6 pieces, Phase 2 found 0 empty slots → sidecar
- timing (step 15): Found "real code" but was REJECTED (decoy extraction bug) → sidecar
- sequence (steps 16, 26): Hover sub-task fails both times → sidecar

---

### Run R3 — v=1, 2026-02-12 (post-drag_drop-fix, post-timing-fix, pre-hover-fix)

| Step | Type (detected)     | Solver  | Time (s) | Result | Notes |
|------|---------------------|---------|----------|--------|-------|
| 1    | hidden_dom          | V4      | 5.4      | OK     |       |
| 2    | click_reveal        | V4      | 2.2      | OK     |       |
| 3    | scroll              | V4      | 6.5      | OK     |       |
| 4    | delayed_reveal      | V4      | 9.8      | OK     |       |
| 5    | (EarlyCodeProbe)    | V4      | 1.5      | OK     |       |
| 6    | drag_drop           | Sidecar | 39.7     | OK     | V4 got 2/6, Phase 2: 0 empty slots |
| 7    | keyboard_sequence   | V4      | 4.5      | OK     |       |
| 8    | color_match         | V4      | 5.5      | OK     |       |
| 9    | hover               | V4      | 4.1      | OK     | Standalone hover |
| 10   | click_reveal        | V4      | 2.3      | OK     |       |
| 11   | timing              | V4      | 27.5     | OK     | timing V4 SUCCESS! |
| 12   | gesture             | V4      | 9.0      | OK     | Direction "down" + shape worked |
| 13   | audio               | V4      | 8.0      | OK     |       |
| 14   | video               | V4      | 8.4      | OK     |       |
| 15   | split_parts         | V4      | 11.9     | OK     |       |
| 16   | multi_tab           | V4      | 10.1     | OK     |       |
| 17   | gesture             | V4      | 8.5      | OK     | Square shape worked |
| 18   | sequence            | Sidecar | 37.5     | OK     | Hover sub-task fails (Tab label match) |
| 19   | puzzle_solve        | V4      | 2.3      | OK     |       |
| 20   | calculated          | V4      | 10.4     | OK     |       |
| 21   | shadow_dom          | V4      | 4.0      | OK     |       |
| 22   | websocket           | V4      | 10.1     | OK     |       |
| 23   | service_worker      | V4      | 4.2      | OK     |       |
| 24   | mutation            | V4      | 4.4      | OK     |       |
| 25   | recursive_iframe    | V4      | 14.1     | OK     |       |
| 26   | conditional_reveal  | Sidecar | 38.1     | OK     | Hover sub-task fails |
| 27   | multi_tab           | V4      | 9.0      | OK     |       |
| 28   | sequence            | Sidecar | 37.2     | OK     | Hover sub-task fails |
| 29   | calculated          | Sidecar | 46.6     | OK     | puzzle_solve override; popup blocked input |
| 30   | shadow_dom          | V4      | 21.5     | OK     | Step 30 hook → /finish |

**Summary R3**: 30/30, 25 V4, 5 sidecar, 420.8s, $0.10

**V4 Failures in R3**:
- drag_drop (step 6): 2/6 pieces → sidecar
- sequence (steps 18, 28): Hover sub-task fails (tab label match) → sidecar
- conditional_reveal (step 26): Hover sub-task fails → sidecar
- calculated→puzzle_solve (step 29): Popup interference blocked input → sidecar

**V4 Successes in R3 (NEW)**:
- timing (step 11): V4 extracted real code correctly!
- gesture (steps 12, 17): Both down direction and square shape worked!
- delayed_reveal (step 4): V4 succeeded on v=1

---

### Run R4 — v=3, 2026-02-12 (post-sequence-hover-fix, post-timing-decoy-fix)

| Step | Type (detected)     | Solver  | Time (s) | Result | Notes |
|------|---------------------|---------|----------|--------|-------|
| 1    | scroll              | V4      | 6.0      | OK     |       |
| 2    | delayed_reveal      | V4      | 7.9      | OK     |       |
| 3    | (EarlyCodeProbe)    | V4      | 1.4      | OK     |       |
| 4    | hidden_dom          | V4      | 4.9      | OK     |       |
| 5    | click_reveal        | V4      | 2.1      | OK     |       |
| 6    | delay_memory        | V4      | 6.4      | OK     |       |
| 7    | hover               | V4      | 4.0      | OK     |       |
| 8    | click_reveal        | V4      | 1.6      | OK     |       |
| 9    | drag_drop           | Sidecar | 38.8     | OK     | V4 got 2/6, Phase 2: 8 pieces, 0 empty slots |
| 10   | keyboard_sequence   | V4      | 4.8      | OK     |       |
| 11   | audio               | V4      | 7.6      | OK     |       |
| 12   | video               | V4      | 6.5      | OK     |       |
| 13   | split_parts         | V4      | 5.5      | OK     |       |
| 14   | decode              | V4      | 6.5      | OK     |       |
| 15   | timing              | V4      | 24.8     | OK     | **V4 found real code! Decoy fix works** |
| 16   | sequence            | V4      | 10.9     | OK     | **Hover fix works: 'Hover over this area' at (632,444)** |
| 17   | puzzle_solve        | V4      | 2.1      | OK     |       |
| 18   | calculated          | V4      | 10.1     | OK     |       |
| 19   | multi_tab           | V4      | 10.5     | OK     |       |
| 20   | gesture             | V4      | 7.5      | OK     | Triangle, no pixel change but still worked |
| 21   | service_worker      | V4      | 4.3      | OK     |       |
| 22   | mutation            | V4      | 3.8      | OK     |       |
| 23   | recursive_iframe    | V4      | 15.1     | OK     |       |
| 24   | conditional_reveal  | V4      | 10.3     | OK     | Hover fix works here too! |
| 25   | multi_tab           | V4      | 10.5     | OK     |       |
| 26   | sequence            | V4      | 10.2     | OK     | **Hover fix works! Skipped hover (pre-completed ✓)** |
| 27   | calculated→puzzle   | V4      | 13.2     | OK     | Correctly overridden to puzzle_solve |
| 28   | shadow_dom          | V4      | 3.0      | OK     |       |
| 29   | websocket           | V4      | 10.4     | OK     |       |
| 30   | service_worker      | V4      | 15.8     | OK     | Step 30 hook → /finish |

**Summary R4**: 30/30, **29 V4**, 1 sidecar, 275.5s, $0.02

**V4 Failures in R4**:
- drag_drop (step 9): 2/6 pieces, Phase 2 found 8 pieces / 0 empty slots → sidecar

**Fixes Validated in R4**:
- sequence hover: FIXED on steps 16, 24, 26 (all 3 used sequence hover logic)
- timing decoy: FIXED on step 15 (found real code after 6 captures)
- timing still takes 6 captures (3 don't register) — room for improvement but works

---

## Agent Reliability Matrix (cross-version)

> Legend: V4=deterministic agent solved, SC=sidecar needed, FAIL=both failed
> Based on runs: R1(v=2), R2-R5(v=3), R3(v=1), R6(v=2)

### Steps 1-15 (simple types — vary per session)

| Type              | v=1    | v=2    | v=3    | Notes |
|-------------------|--------|--------|--------|-------|
| click_reveal      | V4     | V4     | V4     | Always works |
| hidden_dom        | V4     | V4     | V4     | Always works |
| code_entry        | —      | V4     | —      | Always works |
| toggle            | —      | V4     | —      | Always works |
| scroll_reveal     | V4     | V4     | V4     | Always works |
| color_match       | V4     | V4     | —      | Always works |
| decode            | —      | V4     | V4     | Fixed S23 |
| slider            | —      | V4     | —      | Always works |
| split_parts       | V4     | SC(R6) | V4     | V4 on v=1,v=3; v=2 R6 scrolled off screen |
| audio             | V4     | SC(R6) | V4     | V4 on v=1,v=3; v=2 R6 audio needs vision |
| video             | V4     | V4     | V4     | Fixed S23b |
| delay_memory      | —      | V4     | V4     | Fixed S24 |
| keyboard_sequence | V4     | V4     | V4     | Always works |
| drag_drop         | SC(R3) | **V4(R6)** | **V4(R5)** | **FIXED S25** — v=2,v=3 confirmed; v=1 needs retest |
| timing            | V4     | —      | V4     | v=1,v=3 OK; decoy fix validated R4 |
| gesture           | V4     | V4(R6) | V4     | v=2 works in steps 1-15 now! |
| hover             | V4     | V4     | V4     | Standalone hover works fine |
| delayed_reveal    | V4     | SC     | V4     | v=2 regression only |
| conditional_reveal| —      | —      | —      | (Not seen in 1-15 yet) |

### Steps 16-30 (fixed types per version)

| Type              | v=1          | v=2          | v=3          | Notes |
|-------------------|--------------|--------------|--------------|-------|
| multi_tab         | V4 (16,27)   | V4 (20,26)   | V4 (19,25)   | Always works |
| gesture           | V4 (17)      | V4 (16)      | V4 (20)      | **FIXED R6** — all versions V4 |
| sequence          | SC/SC (18,28)| **V4/V4(17,27)** | V4/V4 (16,26) | **FIXED R4+R6** — v=2,v=3 V4; v=1 needs retest |
| puzzle_solve      | V4 (19)      | SC (18,28)   | V4 (17)      | v=2: types answer but code never appears |
| calculated        | V4/SC (20,29)| V4 (19)      | V4 (18,27)   | v=1 step 29: popup blocked input |
| shadow_dom        | V4 (21,30)   | V4 (29)      | V4 (28)      | Fixed — works reliably |
| websocket         | V4 (22)      | V4 (21,30)   | V4 (29)      | Always works |
| service_worker    | V4 (23)      | V4 (22)      | V4 (21,30)   | All versions OK |
| mutation          | V4 (24)      | V4 (23)      | V4 (22)      | Always works |
| recursive_iframe  | V4 (25)      | V4 (24)      | V4 (23)      | Labeled exception, works |
| conditional_reveal| SC (26)      | V4 (25)      | V4 (24)      | **FIXED R6** — v=2 V4; v=1 needs retest |

---

### Run R5 — v=3, 2026-02-12 (post-drag_drop-rewrite)

| Step | Type (detected)     | Solver  | Time (s) | Result | Notes |
|------|---------------------|---------|----------|--------|-------|
| 1    | scroll              | V4      | 5.7      | OK     |       |
| 2    | delayed_reveal      | V4      | 7.9      | OK     |       |
| 3    | (EarlyCodeProbe)    | V4      | 1.0      | OK     |       |
| 4    | hidden_dom          | V4      | 6.5      | OK     |       |
| 5    | click_reveal        | V4      | 2.0      | OK     |       |
| 6    | delay_memory        | V4      | 7.2      | OK     |       |
| 7    | hover               | V4      | 3.5      | OK     |       |
| 8    | click_reveal        | V4      | 2.0      | OK     |       |
| 9    | drag_drop           | **V4**  | 21.9     | OK     | **6/6 drops! Coord-based scan works** |
| 10   | keyboard_sequence   | V4      | 4.6      | OK     |       |
| 11   | audio               | V4      | 8.0      | OK     |       |
| 12   | video               | V4      | 6.5      | OK     |       |
| 13   | split_parts         | V4      | 5.0      | OK     |       |
| 14   | decode              | V4      | 6.7      | OK     |       |
| 15   | timing              | V4      | 24.8     | OK     | Timing decoy fix works |
| 16   | sequence            | V4      | 11.5     | OK     | Hover fix works |
| 17   | puzzle_solve        | V4      | 2.5      | OK     |       |
| 18   | calculated          | V4      | 10.7     | OK     |       |
| 19   | multi_tab           | V4      | 10.4     | OK     |       |
| 20   | gesture             | V4      | 6.9      | OK     | Triangle |
| 21   | service_worker      | V4      | 4.2      | OK     |       |
| 22   | mutation            | V4      | 4.0      | OK     |       |
| 23   | recursive_iframe    | V4      | 15.3     | OK     |       |
| 24   | conditional_reveal  | V4      | 11.1     | OK     | Hover fix works |
| 25   | multi_tab           | V4      | 10.5     | OK     |       |
| 26   | sequence            | V4      | 11.3     | OK     | Hover fix works |
| 27   | calculated→puzzle   | V4      | 2.3      | OK     |       |
| 28   | shadow_dom          | V4      | 2.5      | OK     |       |
| 29   | websocket           | V4      | 10.1     | OK     |       |
| 30   | service_worker      | V4      | 15.5     | OK     | Step 30 hook → /finish |

**Summary R5**: 30/30, **30 V4**, 0 sidecar, **250.9s**, **$0.00** — PERFECT RUN

**All fixes validated**:
- drag_drop: 6/6 via coordinate-based scan (was 2/6)
- sequence hover: 3/3 steps (16, 24, 26 all V4)
- timing decoy: V4 finds real code correctly

---

### Run R6 — v=2, 2026-02-12 (post-all-fixes, cross-version validation)

| Step | Type (detected)     | Solver  | Time (s) | Result | Notes |
|------|---------------------|---------|----------|--------|-------|
| 1    | click_reveal        | V4      | 2.2      | OK     |       |
| 2    | scroll              | V4      | 10.8     | OK     |       |
| 3    | delayed_reveal      | Sidecar | 27.4     | OK     | v=2 regression persists |
| 4    | (EarlyCodeProbe)    | V4      | 1.5      | OK     |       |
| 5    | hidden_dom          | V4      | 6.1      | OK     |       |
| 6    | keyboard_sequence   | V4      | 4.7      | OK     |       |
| 7    | delay_memory        | V4      | 7.0      | OK     |       |
| 8    | hover               | V4      | 4.0      | OK     |       |
| 9    | click_reveal        | V4      | 1.8      | OK     |       |
| 10   | drag_drop           | **V4**  | 18.8     | OK     | **6/6 drops on v=2! Coord scan works** |
| 11   | gesture             | V4      | 8.9      | OK     | Down direction |
| 12   | audio               | Sidecar | 54.6     | OK     | Audio needs vision |
| 13   | video               | V4      | 7.5      | OK     |       |
| 14   | split_parts         | Sidecar | 65.2     | OK     | V4 failed, scrolled off screen |
| 15   | decode              | V4      | 6.8      | OK     |       |
| 16   | gesture             | V4      | 8.9      | OK     | v=2 gesture works in 16-30! |
| 17   | sequence            | V4      | 10.5     | OK     | **Hover fix works on v=2** |
| 18   | puzzle_solve        | V4      | 3.0      | OK     |       |
| 19   | calculated          | V4      | 10.6     | OK     |       |
| 20   | multi_tab           | V4      | 11.8     | OK     |       |
| 21   | websocket           | V4      | 9.6      | OK     |       |
| 22   | service_worker      | V4      | 4.0      | OK     |       |
| 23   | mutation            | V4      | 4.7      | OK     |       |
| 24   | recursive_iframe    | V4      | 26.2     | OK     |       |
| 25   | conditional_reveal  | V4      | 13.4     | OK     |       |
| 26   | multi_tab           | V4      | 15.1     | OK     |       |
| 27   | sequence            | V4      | 19.7     | OK     | Hover fix works |
| 28   | puzzle_solve→calc   | Sidecar | 45.9     | OK     | V4 typed 36, code never appeared |
| 29   | shadow_dom          | V4      | 10.0     | OK     |       |
| 30   | websocket           | V4      | 15.4     | OK     | Step 30 hook → /finish |

**Summary R6**: 30/30, **26 V4**, 4 sidecar, 444.7s, $0.18

**V4 Failures in R6**:
- delayed_reveal (step 3): v=2 regression persists → sidecar
- audio (step 12): audio hint needs vision → sidecar
- split_parts (step 14): V4 scrolled off screen → sidecar
- puzzle_solve (step 28): V4 typed correct answer but code never appeared → sidecar

**Fixes Validated on v=2**:
- drag_drop: **6/6 drops via coordinate scan** (was 2/6 on v=2 in R1)
- sequence hover: V4 on steps 17, 27 (was sidecar on step 17 in R1)
- gesture: V4 on step 16 (was sidecar on step 16 in R1)

---

## Priority Fixes (ordered by impact)

| # | Issue | Sidecar Cost | Affected | Status | Root Cause |
|---|-------|-------------|----------|--------|------------|
| 1 | ~~sequence hover~~ | ~34s x2 per run | ~~v=2(17,27), v=3(16,26)~~ | **FIXED R4+R6** | Two-pass search + size preference + min length filter |
| 2 | ~~drag_drop slot detection~~ | ~38s per run | ~~ALL versions~~ | **FIXED R5+R6** | Coordinate-based rescan, robust placeholder regex |
| 3 | ~~timing decoy extraction~~ | ~44s per run | ~~v=3(15)~~ | **FIXED R4** | Position-aware extraction: only codes AFTER "real code is:" phrase |
| 4 | ~~gesture strokes (v=2)~~ | ~40s per run | ~~v=2(11,16)~~ | **FIXED R6** | Works on v=2 steps 11 and 16 in R6 |
| 5 | **delayed_reveal (v=2)** | ~27s per run | v=2(3) | OPEN | v=2 regression; v=1,v=3 work fine |
| 6 | **puzzle_solve code appearance** | ~46s per run | v=2(18,28) | OPEN | V4 types correct answer but code never appears within timeout |
| 7 | **split_parts scroll (v=2)** | ~65s per run | v=2(14) | OPEN | V4 scrolled off screen; sidecar scrolled back up |

**Session 25 savings**: Fixed #1-#4, saving ~150s+ per run. v=3 now PERFECT (30 V4, $0.00).
**Remaining**: Only v=2-specific issues remain (delayed_reveal, puzzle_solve, split_parts).

---

## How to Add a Run

1. Run `python solve.py --headed 2>&1` and record output
2. For each step, note: detected type, solver (V4/Sidecar), time, result
3. Add a new "Run RN" section above with full step table
4. Update the Agent Reliability Matrix with new data points
5. Update "?" entries as versions are tested
6. Run all 3 versions to fill in the complete matrix
