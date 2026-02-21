# Current Session State

Last updated: 2026-02-11 (Session 23b: Slow-Step Fixes + Detection Hardening)

## Quick Start for New Session

```bash
cd "C:\Dev\Browser Nav Challenge\v2-multi-agent"
python solve.py --headed
```

See `../CLAUDE.md` for full session orientation.
See `docs/COVERAGE_ENGINEER.md` for the dev-time iteration role.
See `docs/RECIPE_DIAGNOSTIC.md` for the recipe system failure analysis.

## Current Performance

- **Best result:** **30/30** (v=2, $0.56, 645s) — Session 17b
- **Session 23b:** 29/30 on v=2 ($0.46, 613.2s) | 29/30 on v=1 ($0.66, 771.7s)
- **V4 hit rate:** 20/30 (up from 16 pre-Session 23)
- **Remaining failure:** recursive_iframe (fiber bypass version-dependent)
- **v=3:** 29/30 (step 30 service_worker occasionally flaky)
- **Time per step:** ~20s median (V4 agent <1s, sidecar ~15-30s)
- **Vision calls:** ~50-60 per run (sidecar rounds)
- **Vision model:** Claude Sonnet 4.5 (`claude-sonnet-4-5-20250929`)
- **System 2 engine:** LearningSidecar with BID grounding + strategy bandit + reflection
- **V4 agents:** 27 challenge agents + 5 universal agents (deterministic, $0 cost)
- **Canonical learnings:** 67 entries
- **Learning storage:** `knowledge/learnings.json` (atomic JSON)
- **Detection scoring:** 3-layer cascade: DOM structural → semantic structure → text matching → 6-channel knowledge
- **Detection penalties:** action-signature (DOM + instruction) up to -0.30, confusion history up to -0.30
- **Rejection loop:** MAX_SIDECAR_CALLS=2, MAX_SUBMISSIONS=3 per step

## Architecture (Session 21+)

```
Pipeline per step:
  Phase 1:   V4 Agents (27 challenge + 5 universal, deterministic, $0)
  Phase 2:   Passive Checks (observers, DNA clustering, harvest_and_score)
  Phase 3:   System 2 Sidecar (VisionLearning + LearningSidecar, Claude API)
  Phase 4:   Fiber Bypass (recursive_iframe ONLY) + Step 30 /finish
```

### Key Subsystems
| Subsystem | Files | Purpose |
|-----------|-------|---------|
| **V4 Agents** | `agents/v4/challenges/*.py` | One agent per challenge type, deterministic solving |
| **V4 Helpers** | `agents/v4/helpers.py` (932 LOC) | 20+ shared utilities (JS click, shadow root, frame traversal) |
| **CDP Helpers** | `agents/v4/cdp_helpers.py` | Event listener detection, pierced DOM search |
| **Detection** | orchestrator `_detect_type_for_v4()` | 3-layer: DOM → semantic → text matching → knowledge |
| **System 2** | `agents/learning_sidecar.py` + `agents/vision_learning.py` | Vision-powered closed-loop controller |
| **Knowledge** | `knowledge_reader.py` | Detection scoring, sidecar context |
| **Recipe Executor** | `agents/recipe_executor.py` | DOM signatures + sidecar promotion (no longer used for replay) |

### Special Agents
| Agent | Behavior |
|-------|----------|
| `recursive_iframe` | Click through levels → fiber bypass (broken Extract Code button) |
| `calculated` | Same as puzzle_solve — appears after puzzle_solve, needs stale state guard |
| `step30_*` | Returns None → orchestrator handles via `/finish` navigation |

## New Capabilities (2026-02-11 — Session 23b: Slow-Step Fixes + Detection Hardening)

### Fixes Applied
| Fix | File | Description |
|-----|------|-------------|
| **hover scoring** | helpers.py | Cap 8 candidates, 10s timeout, text_before bug fixed |
| **gesture shapes** | gesture.py | SHAPE_PATHS for triangle/square/rectangle/star |
| **delayed_reveal** | delayed_reveal.py | Minimum wait safety net before mutation polling |
| **decode hint** | decode.py | `break` after DECODE_ME instead of `continue` |
| **sequence scroll** | sequence.py | Complete rewrite: target actual scrollable container |
| **split_parts** | split_parts.py | Proactive scroll on progress stall |
| **calculated detection** | orchestrator.py | DOM→puzzle_solve, _prev_step_type guard for calculated |
| **video detection** | orchestrator.py | Full-page text check when canvas found |
| **recursive_iframe** | recursive_iframe.py, context.py | used_codes filter for stale fiber state |
| **calculated boundary** | calculated.py | boundary_y=99999 after page.goto refresh |
| **semantic detection** | helpers.py | Always returns puzzle_solve for math inputs |

### Previous (2026-02-11 — Session 23: PW 1.57 Migration + Research Upgrades)

### Stage 1: 8 Failure Fixes
| Fix | File | Description |
|-----|------|-------------|
| **get_accessible_elements()** | helpers.py | Replaced broken `page.accessibility.snapshot()` with `aria_snapshot()` + YAML parsing |
| **shadow_dom text matching** | shadow_dom.py | `get_by_role("button", name=...)` auto-pierces shadow DOM, replaced textContent matching |
| **calculated typing** | calculated.py | `locator.fill()` on spinbutton/textbox instead of JS typing |
| **recursive_iframe depth** | recursive_iframe.py | `frame_locator()` chains for deep iframe traversal (max depth 6) |
| **sequence hover** | sequence.py, helpers.py | Tier 0 text locator + CSSOM :hover scanning + fixed get_accessible_elements |
| **video detection** | orchestrator.py | Video keywords checked before canvas→gesture fallback |
| **split_parts stale coords** | split_parts.py | Lazy Playwright locators that re-evaluate after each click |
| **decode hint loop** | decode.py | DECODE_ME_* pattern recognition → click reveal instead of submit |

### Stage 2: Infrastructure Upgrades
| Feature | File | Description |
|---------|------|-------------|
| **aria_snapshot() as page repr** | helpers.py, sidecar | `get_aria_snapshot()` for semantic page structure, added to sidecar context |
| **Visibility filter** | helpers.py | `query_buttons_in_scope()` filters display:none, visibility:hidden, opacity:0 |
| **locator.fill() everywhere** | helpers.py, puzzle_solve, calculated, sequence | Primary typing strategy across all agents |
| **frame_locator() chains** | helpers.py | `find_in_nested_frames()` helper + `click_button_in_frames()` upgrade |
| **Shadow DOM auto-pierce** | shadow_dom.py | `get_by_role()` replaces manual shadow root traversal |
| **Lazy locator pattern** | split_parts, mutation | Locators re-evaluate on use, survive DOM changes |
| **Scroll verification** | sequence.py | Bottom-reached check + bubbling scroll event dispatch |
| **Mutation polling** | helpers.py | `wait_for_code_mutation()` uses `polling="mutation"` (~16ms vs ~1000ms) |
| **innerText audit** | 10+ files | textContent→innerText across all challenge agents for visible text |
| **CSSOM safety** | helpers.py | Cross-origin stylesheet access wrapped in per-sheet try/catch |

### Bug Fixes
| Bug | Fix |
|-----|-----|
| `screenshot_extract_code()` wrong API args | Corrected to `(screenshot_bytes, 0, context_string)` |
| `get_aria_snapshot()` unused param | Removed unused `boundary_y` parameter |
| `_do_scroll()` pre-scroll scrollTop | Removed non-human-like JS scroll before mouse.wheel |
| `_detect_task_order()` broad "text" match | Specific "type text"/"type here"/"type hello" matching |
| `detect_type_from_semantics()` misroute | Split calculated vs puzzle_solve detection |
| Redundant `import re as _re` | Removed from sequence.py (module-level re already imported) |

### Previous (2026-02-11 — Session 22: Validation + Stale State Fix)

| Feature | File | Description |
|---------|------|-------------|
| **V4 stale state guard** | orchestrator.py | `_system1_v4()` detects progress > 5% at step start, waits up to 2s for transition |
| **fiber_bypass scoped** | shadow_dom.py, step30_shadow_dom.py | Removed fiber_bypass from non-iframe agents (was incorrectly applied) |
| **Fiber reset removal** | orchestrator.py | Removed React fiber state reset from `_step_setup()` (caused white screens) |

### Previous (2026-02-10 — Session 21: V4 Agent Overhaul)

| Feature | File | Description |
|---------|------|-------------|
| **10 agent rewrites** | agents/v4/challenges/ | shadow_dom, multi_tab, hidden_dom, split_parts, recursive_iframe, websocket, service_worker, sequence, puzzle_solve, step30_shadow_dom |
| **20+ new helpers** | agents/v4/helpers.py | JS click, shadow root traversal, frame traversal, DOM attribute scanning, semantic detection, interactivity scoring, animation wait |
| **CDP helpers module** | agents/v4/cdp_helpers.py | DOMDebugger.getEventListeners, pierced DOM code search |
| **Semantic detection** | orchestrator.py, helpers.py | New layer between DOM and text matching, accessibility-tree-like roles+labels |

### Previous (2026-02-10 — Session 18: Recipe Executor Bottleneck Fixes)

| Feature | File | Description |
|---------|------|-------------|
| **Fast code polling** | recipe_executor.py | `expect_code_visible`: 8×300ms (was 15×400ms). Fiber_code short-circuit + final harvest before fallback. |
| **No auto-Solve click** | recipe_executor.py | Removed auto-click-Solve after resolver typing (caused 30s locator handler loop) |
| **Faster wait_for_code** | recipe_executor.py | 250ms poll interval (was 400ms) |
| **Recipe cleanup** | learnings.json | Deleted 5 structurally broken recipes (mutation_v1/v2/v3, service_worker_v1, multi_tab_v2) |

### Previous (2026-02-10 — Recipe Robustness Overhaul)

| Feature | File | Description |
|---------|------|-------------|
| **Challenge Boundary Filtering** | recipe_executor.py | Y-boundary detection: code form → submit button → filler markers. `_find_target` prefers challenge-area elements. |
| **HybridSimilo Fingerprinting** | recipe_executor.py, learning_sidecar.py | Multi-attribute element fingerprint (tag+text+role+aria+neighbors+size). New cascade step 3.5 with similarity scoring. |
| **Completion Sweep on Step-0** | recipe_executor.py | Allows sweep when progress >= 50% even at step 0 |
| **6-Channel Detection** | knowledge_reader.py, orchestrator.py, learning_sidecar.py | Re-enabled dom_signals + dna_signatures channels. Captured at step start, populated on promotion. |
| **Replay Success Context** | knowledge_reader.py, orchestrator.py | Successful replays reinforce text_context + fingerprint on the learning |
| **Parameterized Actions** | recipe_executor.py, learning_sidecar.py | `click_until_progress`, `type_and_submit`, `wait_for_code`. Auto-collapsed from N identical clicks. |
| **CSS Selector Fallback** | primitives.py | `classes` field in `get_locator_cascade` enables CSS selector construction |

### Previous (2026-02-09 — Self-Improving Detection)

| Feature | File | Description |
|---------|------|-------------|
| **Action-Signature Penalty** | knowledge_reader.py | DOM structure + instruction text mismatch penalty in detect_and_get() |
| **Confusion Recording** | knowledge_reader.py, orchestrator.py | Tracks misrouted types, records on System 2 success after routing mismatch |
| **Confusion Penalty** | knowledge_reader.py | Escalating penalty (0.05→0.25) for types with confusion history, 7-day decay |
| **AI Keywords** | learning_sidecar.py | KEYWORDS_JSON piggybacked on Layer C AI review, confusion-aware prompting |
| **Score Histogram** | knowledge_reader.py, orchestrator.py | `SCORE_HISTOGRAM` log for threshold calibration |
| **Failure Step Tracking** | recipe_executor.py | `_last_failure_step` distinguishes routing errors from execution flakiness |
| **Routing Mismatch Guard** | orchestrator.py | Only records confusion on structural impossibility or early failures (step 0-1) |

### Previous (2026-02-08 — Recipe Diagnostic + Fixes)

| Feature | File | Description |
|---------|------|-------------|
| **Stale State Guard** | recipe_executor.py | Skips recipe if progress > 5% at start (React state leak) |
| **Focus Before Type** | recipe_executor.py | Clicks target to focus before keyboard.type() |
| **Hover Coords Fallback** | recipe_executor.py | Falls back to target_coords like click handler |
| **Playwright Native Drag** | recipe_executor.py | `page.drag_and_drop()` with CSS selectors from elementFromPoint |
| **Longer Code Polling** | recipe_executor.py | 10x300ms = 3s for code visibility assertion (was 1.2s) |
| **Aggressive Auto-Demotion** | orchestrator.py | 2 consecutive failures → tier 0 (was 3 attempts) |
| **Cross-Contamination Guard** | orchestrator.py | Keyword detection only for simple steps; config-type lookup for steps 16-30 |
| **Diagnostic Report** | docs/RECIPE_DIAGNOSTIC.md | Comprehensive recipe failure analysis with root causes |

### Previous (2026-02-07 — Recipe Overhaul)

| Feature | File | Description |
|---------|------|-------------|
| **3-Tier Wait Filter** | learning_sidecar.py | Waits: own signals → targetability-only from next → drop. No more nonsensical wait assertions. |
| **2-Step Lookback** | learning_sidecar.py | Keeps i-1 and i-2 setup steps. Flailing guard caps at last 2. Max 1 wait in setup. |
| **Scroll Causal Gate** | learning_sidecar.py | Scrolls only kept if `scroll_moved==True`. No more dead scroll steps in recipes. |
| **Auto Scroll-to-Target** | recipe_executor.py | `_find_target()` auto-scrolls elements into view. Explicit scroll steps now optional. |
| **Adaptive Click Recovery** | recipe_executor.py | Scroll down/up 500px to find missing targets before aborting recipe. |
| **Recursive Iframe Probe** | _debug/probe_recursive_iframe.py | Systematic keyboard/click/dblclick/pointer/scroll/hover probe with depth tracking. |

### Previous (2026-02-07 Night — Reliability + Audit)

| Feature | File | Description |
|---------|------|-------------|
| **Aggressive Tier Demotion** | orchestrator.py | T3→T2 at 3 failures, T2→T1 at 2 failures. Persisted to disk. |
| **Startup Migration** | knowledge_reader.py | Clones strong unversioned recipes to versioned at startup (safe: tracks local_seeded) |
| **scroll_container_js** | primitives.py | React-compatible container scroll via el.scrollTop + scroll event dispatch |
| **Anti-Loop State Awareness** | learning_sidecar.py | drain-before/drain-after. Color/targetability changes count as meaningful effects |
| **State-Aware Recipe Settle** | recipe_executor.py | `_wait_for_state_settle()` polls for changes instead of fixed wait |
| **expect_state_changes** | recipe steps | Captured at promotion, used during replay for synchronization |
| **Style-Aware Code Scoring** | code_scorer.py | Color bucket (green/red), temporal stability, causality scoring |
| **19 Bug Fixes** | 6 files | 3 CRITICAL + 4 HIGH + 12 MEDIUM from coverage audit |

## New Capabilities (2026-02-07 Late — Primitives Enhancement Plan)

| Feature | File | Description |
|---------|------|-------------|
| **Transport Layer Capture** | init_hooks.py, orchestrator.py | postMessage/SW listeners + page.on("response"/"console") scan for codes in network traffic and console output |
| **Canvas Stroke Verification** | primitives.py | Pixel hash before/after each stroke, retry with jitter, wiggle after mousedown, cross-origin fallback |
| **Broadened read_progress** | primitives.py | `<progress>` elements, percentage text, stepper dots/aria-current |
| **scroll_container_until** | primitives.py | Composite: scrolls inside container until code/progress/end, detects lazy-loading |
| **hover_reveal_extract** | primitives.py | Adaptive hover: 200ms poll intervals, early break on reveal, 800ms→2000ms adaptive hold |
| **interact_in_container** | primitives.py | Scoped click/scroll/type in styled-div containers. Two-attempt: mouse click → JS dispatch fallback |
| **classify_challenge_dom** | primitives.py, sidecar, VL | Lightweight DOM archetype hint (~5ms). Soft hint only, never hard-gates. |
| **Message Trimming** | vision_learning.py | `_trim_messages_for_api()`: last 2 rounds full, older collapsed. -40% API latency. |
| **Minimal Page Info** | vision_learning.py | `_extract_page_info_minimal()`: skips full scans when catalog exists. -50-100ms/round. |
| **Early Code Check** | learning_sidecar.py | Fast extract_code_js after each action, before heavy harvest. Short-circuits on valid code. |
| **Wait Reductions** | learning_sidecar.py, vision_learning.py | click 200→100ms, scroll 300→200ms, press 300→150ms, sweep 800→400ms. ~1.5s/step saved. |
| **Phase 2.5c Container Fallback** | orchestrator.py | After seq_click fails, tries interact_in_container on discovered containers |
| **Phase 2.5d Hover-Reveal** | orchestrator.py | When instruction hints include "hover", runs hover_reveal_extract before exploration |

## Capabilities (2026-02-07 Earlier — Recipe System Overhaul)

| Feature | File | Description |
|---------|------|-------------|
| **Action Validity Gate** | primitives.py, sidecar, VL | Pre-execution validation: rejects near-origin drags, empty draws/paths, out-of-region clicks |
| **Canvas Drawing** | primitives.py, all executors | `draw_stroke_on_canvas()` with normalized 0-1 coords. `draw`/`canvas_draw` action type in all 3 executors |
| **Container Discovery** | primitives.py, sidecar, VL | `discover_interactive_containers()` — finds styled fake-frames with interactive children. Wired into sidecar round loop → planner context. Planner sees `containers` field with element details. |
| **double_click** | all executors | New action type: fast double-click |
| **focus** | all executors | New action type: explicit element focus for input prep |
| **element_scroll** | all executors | New action type: scroll inside a specific container element |
| **Recipe Health Check** | recipe_executor, orchestrator | `validate_recipe()` catches corrupted recipes before replay; auto-disables with tier=0 |
| **dest_coords Schema** | recipe_executor, sidecar | Explicit drag destination field eliminates drag-to-self/drag-to-origin bugs |
| **Near-Origin Guard** | all executors | Rejects coordinates within 2px of origin (common default-value corruption) |
| **Planner Prompt v2** | vision_learning.py | SYSTEM_PROMPT includes draw, double_click, focus, element_scroll examples + canvas/container docs |

## Capabilities (2026-02-06)

| Feature | File | Description |
|---------|------|-------------|
| **BID Grounding** | primitives.py, sidecar, VL | Set-of-Marks: elements labeled [N] on screenshot, vision model uses `element_id` instead of pixel coords |
| **Structured Reflection** | learning_sidecar.py | After no-progress round, detailed action-by-action reflection injected into history |
| **Anti-Loop Gate** | learning_sidecar.py | Failed action signatures blocked from re-proposal (type + 50px-grid bucket) |
| **Strategy Bandit** | learning_sidecar.py | 8 exploration strategies with epsilon-greedy selection when stuck |
| **Challenge Region** | primitives.py | Detects challenge container bounds, filters out distraction zone |
| **Decoy Filter** | primitives.py | Structural pattern matching (text + position) marks known decoy elements |
| **Frame/Shadow Surfacing** | primitives.py | Enumerates iframes and shadow DOM roots with interactive counts |
| **Causal Action Storage** | learning_sidecar.py | Recipes store semantic locators (role/text/aria) for coord-independent replay |
| **Iframe Element Annotation** | learning_sidecar.py | BID annotation extended into visible iframes |
| **Element Classification** | primitives.py | classify() assigns semantic type (button, drag_source, drop_target, etc.) to each element |
| **Interactable/Context Split** | primitives.py, sidecar, VL | Interactable elements get BID+overlay; context elements get eid-only (no DOM attr) |
| **Drop Target Detection** | primitives.py | Pass 3 scans for slot/zone divs with 3-stage confidence (high/medium/low) |
| **Sidecar Enforcement** | learning_sidecar.py | Rejects actions on context-only BIDs with closed-loop feedback to planner |
| **Mouse-Only Drag** | primitives.py | smart_drag() uses mouse events + bbox outcome verification; no synthetic DragEvent |
| **Hardened BID Drag** | learning_sidecar.py | locator.drag_to() first, smart_drag fallback; pre-waits + scroll + force=True |
| **State Change Watcher** | init_hooks.py | Always-on JS MutationObserver + 300ms sweep. Tracks disabled→enabled, hidden→visible, opacity, pointer-events, cursor, aria-hidden, vibrancy. 5 hardening measures. |
| **State Change Pipeline** | primitives.py, sidecar, VL | drain → classify (priority scoring) → BID match → inject into planner as `state_changes` field. High-priority changes count as progress. |
| **wait_for_state** | primitives.py, sidecar, VL | Blocking poll primitive: waits until UI signals readiness (element enables, appears, etc.) then returns matched BID. Replaces time-based waits with condition-based sync. |
| **State Change Reflection** | learning_sidecar.py | When stuck, reflection highlights detected state changes as "try these first!" to guide planner. |
| **State Change Hit Rate** | learning_sidecar.py | Logs `triggered_by_state_change` effectiveness at step termination for Coverage Engineer tracking. |
| **Semantic Color Transitions** | init_hooks.py, primitives.py | `_colorCat()` categorizes colors → `turned_green/red/grey` events. Verified: `turned_green` fires reliably. |
| **ACTIONABLE_CHANGES Filter** | learning_sidecar.py | Only positive transitions + color changes sent to planner. No more confusing "disabled" events. |
| **Contextual Planner Header** | vision_learning.py | "UI STATE CHANGES since last round:" + color hints. NOT imperative "act on these FIRST". |
| **Scrollable Container Detection** | primitives.py | Pass 3.5 finds overflow:scroll/auto divs. Gets BIDs for element-targeted scroll. |
| **Recipe Phase 2.5 Capture** | orchestrator.py, sidecar | Pre-sidecar actions threaded into recipe promotion. Full causal chain preserved. |
| **Tiling Scroll Fix** | primitives.py | Restores scroll to top after tiling scan so sidecar starts at correct position. |

## System 2 Architecture: LearningSidecar + Rejection Loop (Updated 2026-02-05 Late Night)

```
System 2 Pipeline:
  VisionLearningAgent.propose_actions()   ← planner (Claude API, no execution)
         │
         ▼
  LearningSidecar.run()                   ← controller (execute, measure, iterate)
         │  Up to 6 rounds, 3 actions/round
         │  Measures: DOM change, progress delta, clickable hash, new codes
         │  Detects stalls: 2 consecutive no-progress → frame sweep → terminate
         │  Returns ranked candidates list with provenance metadata
         │
         ▼
  Returns {code, candidates, promotion_candidate}  ← never submits
         │
         ▼
  Orchestrator: Rejection Loop (MAX_SIDECAR_CALLS=2, MAX_SUBMISSIONS=3)
         │  For each candidate in ranked list:
         │    submit → if rejected → sidecar.note_rejection(code, meta)
         │    → try next candidate
         │  If all candidates rejected → call sidecar again (2nd call)
         │  Sidecar filters rejected codes on 2nd call
         │
         ▼ on success
  finalize_promotion() with 3 guards
         │  Guards: causal actions + strong assertions + stable locators
         ▼
  Knowledge: recipe stored for System 1 replay
```

### Rejection-Feedback Loop Details (2026-02-05 Late Night)

- **Sidecar**: Maintains per-step `_rejected_codes` (exact blacklist) and `_rejected_signatures`
  (source+evidence tuples). Reset on step boundary. Never persisted to disk.
- **Candidate scoring**: `_score_candidate()` returns 0.0-1.0 based on:
  - `seen_after_baseline` (+0.3 / -0.3)
  - `harvest_score` scaling (*0.15)
  - `dom_change_score` bonus (+0.1 if >= 0.05)
  - `progress_delta` bonus (+0.1)
  - Source preference (`harvest_score` +0.05)
  - Signature rejection penalty (-0.3)
- **Progress-aware clear**: On meaningful progress, signature rejections clear but exact code
  blacklist stays. Prevents permanently blocking a source that may find the right code later.
- **Orchestrator**: `_invoke_sidecar()` (renamed from `_system2_reasoning()`) returns full
  result dict. `_submit_and_record()` returns `(bool, str)` tuple for rejection tracking.

## Learning Agent V2 (Implemented 2026-02-04)

| Component | Description |
|-----------|-------------|
| `CanonicalLearning` | 1 per challenge type, holds bounded variants (max 3) |
| `StrategyVariant` | Per-variant confidence (Wilson score), rollback, failure history, **recipe**, **DNA signature** |
| Scored detection | `match_score = 0.35*keyword + 0.25*dom + 0.1*flags + 0.15*text_ctx + 0.15*dna` |
| **System 1 (Reflex)** | `recipe_executor.py` replays stored ActionStep sequences with assertion verification |
| **System 2 (Reasoning)** | `dna_reasoner.py` clusters DOM elements by computed style DNA → assembles codes |
| **DNA signatures** | Type-level aggregated DNA (3+ occurrences = high confidence for detection scoring) |
| **Text context** | Instruction keywords, button labels, interactive types for detection scoring |
| DOM change score | Float 0.0–1.0 (not just boolean) for failure diagnosis |
| Rollback | 3+ consecutive failures → restore previous version |
| Circuit breaker | 5+ consecutive failures → disable for rest of run (TTL) |
| LLM refinement | Constrained prompt with ACTION_SCHEMAS validation |
| LLM bootstrap | First-time success → LLM extracts clean canonical |
| Agent tracking | Per-agent success/failure rates in `agent_tracker.py` |
| V1 seeding | 15 default strategies seeded on first load (fix #1) |
| Winning agent | Tracks which agent found the code for bootstrap (fix #2) |
| Failure dedup | Identical consecutive failures skip LLM refinement (fix #3) |
| Wilson z=1.0 | Less conservative ~84% CI (was z=1.96/95%) (fix #5) |
| Smart eviction | Min 3 attempts before variant eviction, success ratio (fix #6) |
| Agent health routing | Variant scores penalized by agent failure rate (fix #7) |
| Rollback logging | Logs error info + dom_change_score before rollback (fix #9) |
| Sanitize on write | Session codes stripped from failure_history on write (fix #10) |
| Cross-type guard | Won't overwrite recipe with different action_type on verified learnings |

## Playwright 1.57 Features (Implemented 2026-02-04)

| Feature | Where Used | Replaces |
|---------|-----------|----------|
| `add_locator_handler` | orchestrator.py `start_fresh()` | ~5 explicit popup agent calls |
| `wait_for_function(polling="mutation")` | delay.py, draw.py | Polling loops (up to 1000ms late) |
| `wait_for_url(lambda)` | orchestrator.py (all submissions) | 20-iteration URL polling loop |
| `mouse.move(steps=N)` | draw.py, drag_drop.py | Manual interpolation + wait_for_timeout(20) |
| `emulate_media(reduced_motion)` | orchestrator.py `start_fresh()` | Nothing (new optimization) |
| `locator.drag_to()` | drag_drop.py | Manual mouse event sequences |
| `frame_locator()` | shadow_dom.py | N/A (new iframe search capability) |

## Pre-Vision Early Check Architecture (Added 2026-02-05)

Before the expensive Vision API call (~8s), 13 specialist agents run using only body text + DOM checks:

| Agent | Detection | Avg Time |
|-------|-----------|----------|
| `sequence_challenge` | "sequence challenge" + "progress" + task keywords | ~4s |
| `timing` | "capture" + timing/countdown keywords | ~3-4s |
| `hover` | "hover over/here/to reveal" keywords | ~2-3s |
| `draw` | canvas element + "draw/stroke" keywords | ~4s |
| `audio` | audio element or "play audio" keywords | ~12s |
| `video` | "seek/video" + N/M progress pattern | ~4-5s |
| `keyboard` | "keyboard sequence" / "press keys" keywords | ~2-3s |
| `hidden_dom` | "hidden" + "dom/element" or click-to-reveal | ~2-3s |
| `split_parts` | "split" + "part" keywords | ~2-3s |
| `multi_tab` | "multi-tab" or "tab" + "visit/click each" | ~3-4s |
| `click_reveal` | compound phrases: "click to reveal", "click the button to reveal" | ~1-2s |
| `memory` | "watch carefully", "briefly", "will appear for", "memory challenge" | ~2-10s |
| `delay` | "wait for the code", "code will appear", "delayed reveal" | ~5s |
| `puzzle_solve` | math equation pattern: `N op M = ?` | ~2s |

Additionally: **Rotating code handler** (click button 3x rapidly, extract "the real code is" text)

**NOTE:** Scroll removed from early checks — "scroll down" appears as decoy text on many pages.

## What's Working

1. **BID-Grounded Vision** — Elements labeled [N] on screenshot; vision model uses element_id for precise targeting
2. **LearningSidecar + Rejection Loop** — System 2 closed-loop controller with rejection feedback
3. **Structured Reflection + Anti-Loop** — Failed rounds produce detailed action-by-action analysis; blocked_sigs prevents re-proposals
4. **Strategy Bandit** — 8 exploration strategies with epsilon-greedy selection when stuck
5. **Challenge Region Detection** — Filters clicks to challenge area, excludes distraction zone
6. **System 1/2 Tiered Solver** — Recipe replay (System 1) with assertion verification; Sidecar (System 2) as fallback
7. **Two-phase promotion with causal actions** — Semantic locators (role/text/aria) stored for coord-independent replay
8. **Instant popup dismissal** — JS auto-dismiss interval runs every 400ms + Playwright handler + CLEAR_BLOCKERS_JS
9. **All specialist agents** — audio, video, timing, split_parts, hover, draw, keyboard, hidden_dom, drag_drop, memory, puzzle_solve
10. **Frame/Shadow DOM surfacing** — Iframes and shadow roots enumerated with interactive counts
11. **Instant code/URL detection** — `wait_for_function(polling="mutation")` + `wait_for_url(lambda)`
12. **Canonical learnings** — 67 entries with 6-channel scored detection, DNA signatures, HybridSimilo fingerprints

## Known Issues to Fix

### High Priority
1. **recursive_iframe** — Fiber bypass version-dependent. Step 24/25 fails on v=1 and v=2. Fiber state may not contain code after used_codes filter.
2. **shadow_dom V4** — Always falls through to sidecar. V4 should learn from sidecar's approach. Low cost impact but adds ~15-20s per step.
3. **Sidecar learning gap** — V4 agents don't incorporate knowledge from sidecar successes. User feedback: "It feels like you aren't getting data from the sidecar solves at all."

### Medium Priority
4. **Drag recipes fragile** — drag_drop_v1/v2/v3 have low success rates. Locator cascade struggles with puzzle piece targets.
5. **timing agent** — Insufficient captures (1/3). Mutation polling may help.
6. **v=3 step 30** — service_worker challenge occasionally flaky.

## Architecture (V4 — Agent-First Pipeline)

```
┌──────────┐     ┌──────────────────────────────┐     ┌────────────────────────────────┐
│ solve.py │ ──> │ Orchestrator.start_fresh()   │ ──> │ run_step() × 30                │
│          │     │  • emulate_media(reduced_     │     │                                │
│ Creates  │     │    motion="reduce")           │     │ Phase 1: V4 agents (27+5)      │
│ browser  │     │  • add_locator_handler (auto  │     │ Phase 2: Passive checks        │
│ context  │     │    popup dismiss)             │     │   (observers, DNA, harvest)    │
│          │     │  • add_init_script (hooks)    │     │ Phase 3: Sidecar + rejection   │
│          │     │  • click START + wait_for_url │     │   loop (2 calls × 3 submits)  │
└──────────┘     └──────────────────────────────┘     │ Phase 4: Fiber bypass / finish │
                                                      └────────────────────────────────┘
```

## Agents Added/Fixed (2026-02-05 Sessions)

| Agent | Status | Change |
|-------|--------|--------|
| `memory.py` | **New** | Catches briefly-flashed codes via mutation polling. Clicks start/show buttons, uses `wait_for_function(polling="mutation")` to catch code the instant it appears. |
| `audio.py` | **Fixed** | Rewritten: click Play → poll 8s for played state → extra 2s wait → click Complete (no Whisper) |
| `video.py` | **Fixed** | Added Complete Challenge click after seek clicks reach target |
| `timing.py` | **New** | Polls up to 5s for Capture button during countdown window |
| `draw.py` | **Fixed** | Pen timing (50/30/30/100ms pauses), 300ms between strokes, Complete button search |
| `hover.py` | **Fixed** | Limited strategy 2 to 3 tries/selector, min 60x40px, text_match/class_match fallbacks |
| `split_parts.py` | **New** | Finds "Part N: XX" text, clicks parts, combines code |
| `sequence_challenge.py` | **Fixed** | Hover: viewport-relative max cap, interactivity scoring (mid-size + cursor:pointer), steps=10, 2s hold |
| `multi_tab.py` | **New** | Clicks all tab buttons then Reveal Code |
| `click_reveal.py` | **Fixed** | Full popup dismissal at start + retry-after-popup-dismissal logic |
| `puzzle_solve.py` | **Fixed** | Workaround for back-to-back puzzle site bug (accepts reused code from "Code revealed" text) |
| `orchestrator.py` | **Major** | Scroll blacklist, memory early check, broadened memory/delay keywords, vision model upgrade |
| `config.py` | **Updated** | Vision model → `claude-sonnet-4-5-20250929` |
| `vision_client.py` | **Updated** | Default model → `claude-sonnet-4-5-20250929` |

## Recently Modified Files

### 2026-02-06 (V7.4: Semantic Color Transitions + Recipe Fix)
- `init_hooks.py` — Added `_colorCat()`, bgColor/fgColor/borderColor to `_snapState()`, turned_green/red/grey to `_checkTransitions()`. Removed "disabled" negative event.
- `primitives.py` — Color transition weights in CHANGE_PRIORITY. Pass 3.5 scrollable container detection. `scroll_container` in classify(). Tiling scroll restoration.
- `agents/learning_sidecar.py` — ACTIONABLE_CHANGES filter (positive only). Pre-actions prepended to recipe in `_build_promotion_candidate()`.
- `agents/vision_learning.py` — Color context in system prompt. Contextual "UI STATE CHANGES" header replacing imperative "act on these FIRST".
- `orchestrator.py` — Phase 2.5 `pre_sidecar_actions` passed through to sidecar via `pre_actions` parameter.

### 2026-02-06 (Drag-and-Drop Fix)
- `primitives.py` — `smart_drag()` rewritten: removed HTML5 DragEvent dispatch, mouse-only with nudge + bbox outcome verification
- `agents/learning_sidecar.py` — BID-based drag uses `locator.drag_to()` first (visible+scroll+force), smart_drag fallback

### 2026-02-06 (Vision Grounding + Exploration Upgrade)
- `agents/learning_sidecar.py` — BID resolution, reflection+anti-loop, strategy bandit, causal action storage, iframe annotation, catalog trimming, context cleanup (~170 LOC added)
- `primitives.py` — `annotate_elements()`, `render_bid_overlay()`, `remove_bid_overlay()`, `find_challenge_region()`, `enumerate_frames()`, `enumerate_shadow_roots()`, `is_decoy_element()` (~130 LOC added)
- `agents/vision_learning.py` — BID system prompt, element catalog in messages, BID resolution in standalone `_execute_action()`, strategy hint passthrough (~30 LOC added)

### 2026-02-05 (Late Night — Rejection-Feedback Loop)
- `agents/learning_sidecar.py` — Rejection tracking, candidate scoring, `_harvest_all_candidates`, `_finalize_code_return`, `note_rejection()`, `clear_soft_rejections_on_progress()`
- `orchestrator.py` — `_submit_and_record()` returns tuple, Phase 3 rejection loop (MAX_SIDECAR_CALLS=2, MAX_SUBMISSIONS=3), renamed `_system2_reasoning` → `_invoke_sidecar`

### 2026-02-05 (Night — LearningSidecar v1 + Coverage Engineer)
- See `docs/CHANGELOG.md` for full details

### 2026-02-04 (System 1/System 2, Popup Rewrite, Learning V2)
- See `docs/CHANGELOG.md` for full details

## Commands

```bash
# Run full 30 steps with visible browser
python solve.py --headed

# Run limited steps for testing
python solve.py --max-steps=10 --headed

# Headless run
python solve.py

# Check if code compiles
python -c "from orchestrator import Orchestrator; print('OK')"
python -c "from agents.learning_sidecar import LearningSidecar; print('OK')"

# View canonical learnings
python -c "from knowledge_reader import get_knowledge_reader; r = get_knowledge_reader(); print(r.get_stats())"

# View agent performance
python -c "from agent_tracker import get_agent_tracker; t = get_agent_tracker(); print(t.get_performance_summary())"
```

## Next Steps

### Immediate: Fix recursive_iframe + shadow_dom
1. **recursive_iframe**: Run `diagnose.py --step 25 --headed` on v=1 to see fiber state. May need alternative code extraction (e.g., network intercept or visual code detection).
2. **shadow_dom**: Run `diagnose.py` to study sidecar's successful actions. Port strategy to V4 agent.
3. **Sidecar learning**: Implement mechanism for V4 agents to learn from sidecar successes.

### Compare metrics:
| Metric | Pre-S23 | S23b Run 3 (v=2) | S23b Run 4 (v=1) | S17b Best | Target |
|--------|---------|-------------------|-------------------|-----------|--------|
| Steps solved | 29/30 | 29/30 | 29/30 | 30/30 | 30/30 |
| V4 hits | 16 | 20 | 20 | N/A | >22 |
| Time | 986s | 613s | 772s | 645s | <600s |
| API cost | $0.66 | $0.46 | $0.66 | $0.56 | <$0.50 |

### After recursive_iframe fix:
1. **Drag recipe improvement**: drag_drop still flaky — may need smarter puzzle piece targeting.
2. **Timing agent**: Only captures 1/3 — investigate capture button timing.
3. **v=3 step 30**: service_worker occasionally flaky.
