# Browser Nav Challenge — Development Journey

> From reverse engineering to vision-powered autonomy: the story of solving a 30-step
> browser navigation challenge three different ways.

---

## The Challenge

A single-page React app at `https://serene-frangipane-7fd25b.netlify.app` presents
30 sequential challenges. Each step shows a UI puzzle (click a button, solve a math
problem, draw a gesture, decode a string, navigate shadow DOM) that reveals a 6-character
alphanumeric code. Enter the code to advance.

The site randomizes across 3 versions per session. Each version shuffles challenge types
and parameters, so a solver can't just hardcode the answers.

---

## V1: The Deterministic Solver (solve.py at root)

**Result: 30/30, ~75s, $0 API cost.**

The first approach was pure reverse engineering. We decompiled `bundle.js`, traced every
challenge type, and found that codes are stored in sessionStorage (`wo_session`) using
Base64 + XOR with the key `"WO_2024_CHALLENGE"`. The `generate_code(step, version)`
formula was extracted directly.

V1 works by:
1. Navigating to the start page
2. For each step, computing the expected code deterministically
3. Typing it into the input and submitting

It's fast, reliable, and costs nothing. But it only works because we have the source code.
The real challenge is: **can we build a solver that works WITHOUT knowing the answers?**

---

## V2: The Multi-Agent Vision Solver (v2-multi-agent/)

**Result: ~10/30, variable, ~$2-5/run.**

V2 was built to solve the challenge the way a human would: look at the screen, understand
the puzzle, perform the required interaction, and extract the revealed code.

### Architecture

32 specialist agents, each handling one challenge type:
- `scroll.py` — scroll down to find hidden code
- `hover.py` — hover over elements to trigger reveal animation
- `draw.py` — draw gestures on canvas
- `drag_drop.py` — puzzle piece drag-and-drop
- `decode.py` — base64/rot13/hex decoding
- `shadow_dom.py` — traverse shadow DOM and iframes
- `audio.py` — play audio, transcribe to extract code
- ...and 25 more

The orchestrator takes a screenshot, sends it to Claude Sonnet 4.5 for challenge type
detection, then dispatches to the appropriate specialist agent.

### What Worked

- **Popup dismissal**: The site throws decoy popups constantly. Two-layer defense: a
  JS auto-dismiss interval (400ms) running in the page, plus Playwright's
  `add_locator_handler()` firing before every locator action.
- **Code extraction**: Multi-factor scoring (`code_scorer.py`) filters decoy codes
  (SUBMIT, SCROLL, REVEAL, etc.) from real ones.
- **DNA clustering**: `dna_reasoner.py` groups DOM elements by computed CSS style,
  finding code characters that share the same font/color/size.

### What Failed Spectacularly

**The scroll false positive epidemic.** Every page has 99 filler "Section N" blocks as
decoys. Early versions of the orchestrator would see "scroll down" in the page text and
dispatch the scroll agent — on every single step, whether it was a scroll challenge or not.
The fingerprint approach (counting filler sections) failed because the count is the same
on every page. The fix: blacklisting scroll from recipe replay and verified learnings,
only allowing vision dispatch to trigger it.

**The React white screen of death.** Any DOM mutation — `el.remove()`, `el.style.display`,
React fiber state changes — instantly crashes the app. We learned this the hard way when
trying to dismiss popups by removing them, style-hack the modal, or call `onComplete()`
via React fiber internals. The rule became absolute: **read-only JS only**.

**The popup arms race.** The site generates popups with React 18's concurrent renderer.
Plain `.click()` doesn't work because React doesn't see synthetic events. The fix was
a full pointer event sequence (pointerdown → mousedown → pointerup → mouseup → click)
matching what a real user generates. Radio buttons (Radix UI) needed yet another approach:
`focus()` + `KeyboardEvent('keydown', {key: ' '})`.

**The stale code problem.** Codes from previous steps persist in the DOM (React SPA — no
page reload between steps). Without a step boundary marker, extractors would pick up the
old code and try to submit it, getting rejected. Fix: `__snapshotBaseline()` captures all
visible codes at step start, and all extractors filter against the baseline.

**The learning system that didn't learn.** V2 had a learning system (`knowledge_reader.py`)
with 17 seeded strategies, Wilson score confidence, DNA signatures, and LLM-powered
refinement. In practice, the scored detection router mostly matched the wrong challenge
type (keyword overlap between challenges), the seeded strategies were too generic to be
useful, and the refinement budget was spent on hallucinated improvements. The system
compounded noise, not knowledge.

### Key Architectural Lessons

1. **Never trust page text for challenge detection.** "Scroll down" appears on 99% of
   pages. "Click here" appears on every page with a button. Detection must use compound
   signals (keywords + DOM flags + DNA + text context).

2. **Execution order matters.** System 1 recipe (instant) → verified learning → early
   checks (specialist agents) → vision API (slow, expensive). Getting this wrong means
   burning $0.02 on a vision call for a challenge that could have been solved in 200ms.

3. **Specialist agents are a local maximum.** Each agent handles one challenge type well,
   but the routing logic to dispatch the right agent is fragile. A single vision-powered
   agent that can handle ANY challenge type, combined with recipe replay for known types,
   is architecturally simpler and more robust.

---

## V3: The Closed-Loop Autonomous Engineer

**Goal: Compound knowledge across episodes. System 2 (vision) discovers, System 1 (recipes) replays.**

V3 collapses the 1100-line `run_step()` into a clean 3-phase pipeline:

### The Pipeline

1. **System 1 Reflex** — Replay a stored recipe (DSL action sequence) with assertions.
   If any assertion fails, fall through. Zero API calls, sub-second.

2. **Passive Checks** — Code observers (WebSocket/mutation hooks), DNA clustering,
   `harvest_and_score()`. No actions taken, just reading what's already there.

3. **System 2 Reasoning** — VisionLearningAgent takes screenshots, reasons about the
   challenge, executes actions through the SAME DSL executor as System 1, and iterates.
   Bounded: max 4 actions per round, max 6 rounds, stall detection.

### The Compounding Loop

When System 2 succeeds:
1. Convert the action log to a DSL recipe (with locator cascade, not just coordinates)
2. Run delta-debugging minimizer to find the minimal action sequence
3. Promote to System 1 knowledge base (keyed by challenge type, not step number)

Next episode: System 1 replays the recipe instantly. No vision call needed.

**The key metric**: `recipe_hit_rate` should rise across episodes while
`vision_call_rate` falls. If not, the learning loop isn't compounding.

### What Changed from V2

- **Single DSL executor** — VisionLearning outputs ActionStep objects and executes them
  through `RecipeExecutor._execute_step()`. Same code path as System 1 replay. No drift.
- **Locator cascade in flight recorder** — Every action captures `data-testid`, ARIA label,
  role, text, tag, and coordinates. Recipes use semantic selectors first, coords last.
- **Recipe failure decay** — Wilson score decays naturally when System 1 replays fail.
  Broken recipes don't poison the system forever.
- **Progress binding** — First `read_progress()` result anchors the progress tracking.
  Prevents stall detection from oscillating between different progress indicators.
- **Evaluation harness** — `benchmark.py` runs N episodes, tracks metrics, detects
  regressions. Objective evidence of whether changes help or hurt.

### Legacy Agents

The 27 specialist agents remain on disk as importable reference. They're not in the active
dispatch pipeline — VisionLearningAgent handles all challenge types. But they represent
hard-won knowledge about each challenge type's quirks, and may be useful for future
bootstrapping.

---

## Timeline

| Date | Milestone |
|------|-----------|
| 2026-02-03 | V1 deterministic solver: 30/30 |
| 2026-02-04 | V2 multi-agent started, popup dismissal, code scorer |
| 2026-02-04 | V2 learning system V1 (JSONL-based, accumulative) |
| 2026-02-05 | V2 learning system V2 (canonical, Wilson score, DNA) |
| 2026-02-05 | Scroll blacklist, memory agent, puzzle workaround |
| 2026-02-05 | V3 closed-loop architecture: single DSL, recipe promotion, benchmark |
| 2026-02-10 | V4 agent overhaul: 27 challenge agents, 20+ helpers, CDP integration |
| 2026-02-11 | V4 cleanup: removed recipe replay (~290 lines), archived 4 dead agents |
| 2026-02-11 | Human-like overhaul: removed synthetic dispatches, MISSION.md framework |
| 2026-02-11 | Playwright 1.57 migration: aria_snapshot, fill(), polling="raf" |
| 2026-02-12 | Coverage engineering: drag_drop rewrite, audio 3-tier, gesture shapes |
| 2026-02-12 | 30/30 V4 on all 3 versions, $0.00, ~3:25 |

---

## V4: The Agent-First Pipeline (Session 21–22)

**Result: 30/30 on v=1/v=2, 29/30 on v=3. $0.56, ~645s best run.**

V4 was born from a realization: the V3 recipe system had become an over-engineered
liability. Recipe replay was disabled by default (`--with-recipes`), and nearly all
solves came from either V4 deterministic agents (Phase 1) or the vision sidecar
(Phase 3). The recipe code was 290+ lines of dead weight in the orchestrator.

### What V4 Added (Session 21)

**27 handcrafted challenge agents** (`agents/v4/challenges/`), one per challenge type.
Each agent is a pure function `solve(ctx: StepCtx) -> str | None` — deterministic,
zero API cost, sub-second. Combined with 5 universal agents (EarlyCodeProbe,
CompletionSweep, etc.), these replaced the old detection → specialist dispatch pattern.

**20+ shared helpers** (`agents/v4/helpers.py`):
- `js_click_button_by_text()` — React-compatible JS click (bypasses `.click()` issues)
- `js_click_in_shadow_roots()` — recursive shadow DOM traversal
- `click_button_in_frames()` — cross-iframe operations
- `scan_dom_attributes_for_code()` — finds codes in data-\*, title, aria-label, CSS ::before/::after
- `do_hover_with_js_events()` — hover with explicit mouseenter/mouseover dispatch
- `detect_type_from_semantics()` — accessibility-tree-like role+label detection

**CDP helpers** (`agents/v4/cdp_helpers.py`):
- `get_elements_with_listeners()` — DOMDebugger event listener detection
- `find_codes_in_pierced_dom()` — pierce shadow roots to find hidden codes
- `find_hover_targets_via_css()` — force :hover pseudo-state, detect style changes

**3-layer detection cascade** in the orchestrator:
1. DOM-first (canvas? draggable? iframe? shadow buttons?)
2. Semantic structure (roles + labels, cuts filler text noise)
3. Text matching (instruction keywords, only after DOM/semantic miss)

### What V4 Removed (Session 22 — The Great Cleanup)

Session 22 audited the pipeline and removed ~1000 lines of dead code:

**Phase 1.5: Recipe Replay (~290 lines)**
The crown jewel of V3's "compounding loop" — stored DSL recipes replayed instantly.
In practice: hit rate peaked at 62%, but false-positive routing was endemic (keyword
overlap between challenge types), tier management was complex (TTL, demotion, purging,
healing), and the recipe executor's assertion system was too brittle for layout
variations across versions. The V4 deterministic agents replaced it: same zero-cost
instant solving, but with explicit code instead of learned recipes.

Features removed:
- `_system1_reflex()` — 267-line recipe replay engine with tier-aware routing
- `_record_recipe_success()` — 37-line deferred success recording with tier promotion
- `_record_recipe_failure()` — failure tracking, TTL decrement, auto-purge, demotion
- Recipe healing — merging working recipe prefix with sidecar tail on partial failures
- Startup cleanup — purging zombie tier-0 recipes on boot
- Routing mismatch detection — tracking when recipe detection matched the wrong type
- Per-run replay budgets and unproven recipe throttling
- 6 recipe-specific state fields and 3 threshold constants
- 3 CLI flags: `--with-recipes`, `--sidecar-smoke`, `--pause-on-fail`

**4 Dead Agent Files → `agents/_archived/`:**
- `extract_code.py` (237 lines) — superseded by V4 CompletionSweep universal agent
- `real_challenge.py` (253 lines) — early prototype, zero importers
- `instruction_parser.py` (115 lines) — never integrated
- `calculated.py` (78 lines) — superseded by `agents/v4/challenges/calculated.py`

**ALL_AGENTS Registry** simplified from 5 entries to 1 (code_entry — the only one
accessed via `self.a.get()` in the orchestrator). popup, dna_reasoner, and
recipe_executor are instantiated directly where used.

### The Resulting Pipeline

```
V4 Agents (Phase 1) → Passive Checks (Phase 2) → Sidecar (Phase 3) →
Fiber Bypass (Phase 4, iframe only) → Step 30 /finish
```

Four phases instead of six. No recipe replay, no recipe healing, no routing mismatch
detection, no tier management. The orchestrator dropped from ~2100 lines to ~1500.

### V4 Agent Bug Fixes (Session 22)

- **timing.py**: Click-first-then-wait pattern (was waiting full interval before first click)
- **decode.py**: Added reverse string detection, Caesar cipher, try quoted strings before DECODE fallback
- **drag_drop.py**: Progress-gating per drag pair, retry with `locator.drag_to()` fallback
- **hover.py**: Added JS mouseenter/mouseover events, CDP hover target detection

---

## V4 Maturity: From 90% to 100% (Sessions 23–29)

**Result: 30/30, $0.00, ~3:25 across all 3 versions. Zero API calls.**

With V4 agents covering all 27 challenge types, the remaining work was hardening
every agent against version variations and edge cases.

### Playwright 1.57 Migration (Session 23)

Upgraded to Playwright 1.57, adopting new APIs and fixing 8 broken agents:
- `page.accessibility.snapshot()` removed — replaced with `locator.aria_snapshot()` (YAML)
- `polling="mutation"` removed — replaced with `polling="raf"` (~16ms per frame)
- `locator.fill()` as primary input method — triggers React `onChange` natively
- `get_by_role()` auto-pierces open shadow DOM — eliminated manual traversal

### Coverage Engineering (Sessions 24–26)

Built diagnostic tooling (`diagnose.py`, `compare.py`, `scope_check.py`, `batch_diagnose.py`)
to debug agent failures systematically instead of guessing:

- **drag_drop rewrite**: O(pieces x slots) brute-force → O(slots) with `locator.drag_to()`.
  Any piece fits any slot (uniqueness constraint only). Greyed-out piece filtering.
- **audio 3-tier play**: Click Play button → `audio.play()` JS → poll for `<audio>` element.
  Handles all 3 versions including NO_AUDIO_ELEMENT edge case.
- **gesture shapes**: 3-point paths for L/triangle, canvas focus management, multi-directional
  stroke detection.
- **hover scoring**: Dimension-filtered target search (instruction paragraphs >400px width are
  decoys, not targets). Sort by area — interactive elements are larger.
- **timing multi-capture**: Parse "at least N times" from instructions, click Capture button
  N+1 times with proper interval spacing.
- **fill() timeout**: 30s default → 3000ms. Prevents 22s waste on overlay-blocked inputs.
- **scrollTo(0,0) after popup dismissal**: Popup button clicks scroll page down, making
  elements invisible. Must scroll after `flush_popup_batch()`, not before.

Result: **30/30 V4 on v=1** (Session 26).

### Polish & Perfection (Sessions 27–29)

Final agent fixes to reach 30/30 on all three versions:

- **decode text-first clicking**: "Reveal" element is `<div>` on v=2/v=3, not `<button>`.
  Flipped click order: `get_by_text()` first (finds any element type), `get_by_role('button')`
  as fallback. Reduced from 62.5s (sidecar) to 4.5s (V4).
- **audio greedy match fix**: `el.click()` exact text match prevents `t.includes('complete')`
  from matching instruction paragraphs.
- **calculated pushState nav**: Client-side React Router navigation via `pushState` + `popstate`
  to `/` then back to `/stepN`. No full page load, no start menu flash.
- **puzzle_solve delayed code**: Added 10s `wait_for_code_mutation` when answer accepted but
  code hasn't appeared. Some versions have delayed content blocks.
- **hidden_dom attribute scan**: Delayed mutation wait + attribute scan (searches all element
  attributes for 6-char codes). Handles "Check attributes, aria labels, meta tags" hints.
- **Dead code cleanup**: Removed unused Ctrl+A deselect, audio NO_AUDIO stub, and other
  per-session artifacts.

Result: **30/30, $0.00, ~3:25 across all 3 versions** (Session 29).

---

## The Recurring Theme

Every major improvement came from making the system **simpler**, not more complex:
- Scroll blacklist (3 lines) beat scroll fingerprinting (50 lines)
- Read-only JS policy beat DOM manipulation workarounds
- Single DSL executor beat 32 independent agent dispatch paths
- Deterministic agents ($0.00) beat vision API calls ($0.60/run)
- Diagnostic tooling beat guesswork debugging

The V1 solver is perfect: 30/30, zero cost — but only works with the source code.
V4 achieves the same result (30/30, zero cost) while being general enough to solve
challenges it hasn't seen before.
