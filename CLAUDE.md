# Browser Nav Challenge — Session Orientation

> **Read this first.** This file orients every new Claude Code session on this project.
>
> **Read `v2-multi-agent/MISSION.md` second.** It is the single source of truth for
> all technical decisions. Every technique must work on an arbitrary website we've
> never seen. If a proposed change conflicts with MISSION.md, MISSION.md wins.

## What Is This?

An automated solver for a 30-step browser navigation challenge hosted at
`https://serene-frangipane-7fd25b.netlify.app`. Each step presents a UI challenge
(click a button, solve a puzzle, decode a string, draw a gesture, etc.), reveals a
6-character alphanumeric code, which must be entered to advance to the next step.

## Project Layout

```
Browser Nav Challenge/
├── CLAUDE.md              ← YOU ARE HERE
├── solve.py               ← Old v1 deterministic solver (30/30, ~75s, zero API cost)
├── APPROACH.md            ← Reverse-engineering notes for v1
│
└── v2-multi-agent/        ← **ACTIVE CODEBASE** — vision-powered multi-agent solver
    ├── solve.py           ← Entry point: `python solve.py [--headed] [--max-steps N]`
    ├── diagnose.py        ← Coverage tool: single-step telemetry dump
    ├── compare.py         ← Coverage tool: agent vs sidecar action diff
    ├── scope_check.py     ← Coverage tool: boundary validation all 30 steps
    ├── FAILURES.md        ← Persistent failure knowledge base (read at session start)
    ├── orchestrator.py    ← Core loop: detect challenge → run agent → extract code → submit
    ├── config.py          ← Constants, code generation formula, timing
    ├── agents/            ← One agent per challenge type (see README.md for full list)
    ├── primitives.py      ← Shared helpers: extract_code_js, read_progress, get_locator_cascade
    ├── code_scorer.py     ← Multi-factor code validation & decoy filtering
    ├── init_hooks.py      ← JS injection: human-like observation hooks (MutationObserver, state watcher)
    ├── knowledge_reader.py← Canonical learnings V2: scored detection, Wilson confidence, rollback
    ├── agent_tracker.py   ← Per-agent success/failure rates, feeds into routing
    ├── vision_client.py   ← Claude Vision API client
    ├── docs/              ← Reference docs + archive
    │   ├── COVERAGE_ENGINEER.md ← Dev-time iteration role for adding primitives
    │   ├── NAVIGATION_FLOW.md   ← SPA routing rules & recovery
    │   └── archive/             ← Historical docs (CHANGELOG, SESSION_STATE, etc.)
    └── knowledge/         ← Canonical learnings (JSON) and agent performance data
```

## The Active Code Lives In `v2-multi-agent/`

**Always work in `v2-multi-agent/`.** The root-level `solve.py` is a legacy v1 solver.

**Read `v2-multi-agent/FAILURES.md` at session start** — it lists every known V4 agent failure with root causes and diagnosis commands.

## How to Run

```bash
cd v2-multi-agent
python solve.py --headed                          # Watch in browser (recommended)
python solve.py                                   # Headless
python solve.py --max-steps 5                     # Stop after step 5
```

## Coverage Engineer's Toolbox

```bash
cd v2-multi-agent
python diagnose.py --step 18 --headed             # Full telemetry dump for one step
python compare.py --step 18 --headed              # Agent vs sidecar action diff
python scope_check.py --headed                    # Boundary validation for all 30 steps
```

These tools give you raw data about page state instead of guessing. **Always run `diagnose.py`
before fixing an agent failure** — it shows buttons, inputs, boundary_y, detection result,
and hook codes.

Requires: Python 3.11+, Playwright (`pip install playwright && playwright install`),
`.env` with `ANTHROPIC_API_KEY` (for Vision API).

## Architecture in 30 Seconds

1. **`solve.py`** creates a Playwright browser, calls `orchestrator.start_fresh()`.
2. **`orchestrator.start_fresh()`** opens the page with:
   - `page.emulate_media(reduced_motion="reduce")` — skip CSS animations
   - `page.add_locator_handler(...)` — auto-dismiss popups before every locator action
   - `page.add_init_script(...)` — inject human-like observation hooks (MutationObserver, state watcher)
   - `POPUP_AUTO_DISMISS_SETUP_JS` — JS interval (400ms) auto-dismisses all popups via `el.click()`
   - Exception hooks injected conditionally per challenge type (see `MISSION.md` labeled exceptions)
3. **`orchestrator.run_step()`** runs a 4-phase pipeline for each of 30 steps:
   - **Phase 1 — V4 Agents:** 25 deterministic challenge agents + 5 universal agents.
     Zero API cost, sub-second. 3-layer detection: DOM → semantic → text matching.
   - **Phase 2 — Passive Checks:** Mutation observers, DNA clustering,
     `harvest_and_score()` — all zero API cost.
   - **Phase 3 — System 2 (LearningSidecar + Rejection Loop):** VisionLearningAgent proposes
     actions (Claude API). LearningSidecar executes them, measures progress, harvests codes,
     returns ranked candidates with provenance scoring. Orchestrator runs rejection loop:
     submits candidates in order, informs sidecar of rejections (`note_rejection()`), re-invokes
     sidecar up to 2 times with 3 total submissions. On success, `finalize_promotion()` with
     3 guards (causal actions, strong assertions, stable locators).
   - **Phase 4 — Post-sidecar fallback:** Fiber bypass (recursive_iframe only — labeled exception).
4. **Code extraction** uses multiple strategies: DOM observers, `code_scorer.py` scoring,
   agent-specific extraction, DNA clustering, and `harvest_and_score()`.
5. **Learning system** (`knowledge_reader.py` + `agent_tracker.py`): canonical learnings
   with DNA signatures and text context. Scored detection feeds sidecar context.
6. **Coverage Engineer** (`docs/COVERAGE_ENGINEER.md`): Dev-time role for adding new
   capabilities for unsolved challenge types.

## Playwright 1.57 Features Used

These are **already implemented** — do not re-add them:

| Feature | Where | Purpose |
|---------|-------|---------|
| `page.add_locator_handler()` | `start_fresh()` | Auto-dismiss popups before every locator action |
| `page.wait_for_function(polling="mutation")` | `delay.py`, `draw.py` | Detect code appearance in ~16ms vs 1000ms polling |
| `page.wait_for_url(lambda)` | `orchestrator.py` (throughout) | Instant URL change detection vs 200ms polling |
| `page.mouse.move(x, y, steps=N)` | `draw.py`, `drag_drop.py` | Built-in interpolation replaces manual loops |
| `locator.drag_to(target)` | `drag_drop.py` | Native drag-and-drop |
| `page.frame_locator("iframe")` | `shadow_dom.py` | Nested iframe traversal |
| `page.emulate_media(reduced_motion)` | `start_fresh()` | Skip CSS animations |

## Key Invariants — Do NOT Break These

1. **Never call `page.goto()` to a `/stepN` URL** — the site is a React SPA. Only `/` is a
   valid entry point. Steps advance via React Router after code submission.
2. **Never click `<a>` tags** — they cause 404 navigation. All clicks must be filtered
   through `_safe_click()` or use JavaScript `.click()` on buttons only.
3. **Never use `page.reload()`** — results in 404. Use `_recover_from_navigation()` instead.
4. **Codes are 6 chars from `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`** (no I, O, 0, 1).
   Common false positives: SUBMIT, SCROLL, REVEAL, CANCEL, BUTTON, HIDDEN, PUNYYR, CANVAS.
5. **`code_already_submitted` flag** prevents double-advance bugs. Always check it before
   submitting a code.
6. **NEVER modify the DOM or React internals** — causes instant white screen. This includes:
   `el.remove()`, `el.style.*` changes, `el.className` changes, React fiber state mutation
   (`fiber.memoizedState`), `state.queue.dispatch()`, `dispatchEvent(new PopStateEvent(...))`,
   dispatching synthetic PointerEvent/MouseEvent/KeyboardEvent sequences.
   Use `el.click()` (standard DOM API) or Playwright locator clicks instead.
   All injected JavaScript must be **read-only** (observe, query, extract — never mutate).
   See `MISSION.md` for the complete decision framework and labeled exceptions.

## Current Performance (as of 2026-02-11)

- **30/30** on v=1 and v=2 consistently ($0.56, ~645s best run)
- **29/30** on v=3 (step 30 service_worker occasionally flaky)

## Known Issues (as of 2026-02-11)

- v=3 step 30 (service_worker): occasionally fails
- Session 22b changes need validation run to confirm no regressions

## Code Storage (from site internals)

- SessionStorage key: `wo_session`, encoding: Base64 + XOR with key `"WO_2024_CHALLENGE"`
- Format: `{sessionId, codes: string[30], completed: number[]}`
- Array indexing: `codes[N]` validates step N (`codes[1]` for step 1, NOT `codes[0]`)
- Codes generated via `crypto.getRandomValues()` (not deterministic)

## Deeper Documentation

| Doc | What It Covers |
|-----|----------------|
| `v2-multi-agent/MISSION.md` | **Decision framework**: what's human-like, labeled exceptions, framework compatibility |
| `v2-multi-agent/README.md` | Full agent list, config, performance |
| `v2-multi-agent/JOURNEY.md` | V1→V2→V3 design evolution history |
| `v2-multi-agent/docs/COVERAGE_ENGINEER.md` | **Dev-time iteration role**: rules, loop, primitives catalog |
| `v2-multi-agent/docs/NAVIGATION_FLOW.md` | SPA routing rules & recovery |

### Archived (historical reference, in `v2-multi-agent/docs/archive/`)

ARCHITECTURE.md, CHANGELOG.md, SESSION_STATE.md, ROOT_CAUSES.md, LEARNINGS.md, HANDOFF.md
