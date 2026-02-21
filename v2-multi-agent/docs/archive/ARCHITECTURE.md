# System Architecture

> Deep-dive into how the v2 multi-agent solver works internally.

## High-Level Flow

```
┌──────────┐     ┌──────────────────────┐     ┌─────────────────────────────┐
│ solve.py │ ──> │ Orchestrator         │ ──> │ run_step() × 30             │
│          │     │  .start_fresh()      │     │                             │
│ Creates  │     │  Sets up page:       │     │  Phase 1: System 1 Reflex   │
│ browser  │     │  • reduced_motion    │     │    → Recipe replay          │
│ context  │     │  • locator_handler   │     │  Phase 2: Passive Checks    │
│          │     │  • init_script hooks │     │    → Observers, DNA, harvest│
│          │     │  • popup auto-dismiss│     │  Phase 3: System 2 Sidecar  │
│          │     │  • click START       │     │    → VL planner + controller│
└──────────┘     └──────────────────────┘     │    → submit + promote      │
                                               └─────────────────────────────┘
```

## Page Setup (`start_fresh`)

When a new browser page is created, three Playwright features are configured
before any navigation occurs:

```python
# 1. Skip CSS animations on compliant pages
page.emulate_media(reduced_motion="reduce")

# 2. Auto-dismiss popups before every locator-based action
page.add_locator_handler(
    page.locator("[class*='overlay']:visible, [class*='modal']:visible, [role='dialog']"),
    auto_dismiss_overlays,      # Runs DISMISS_JS + CLEAR_BLOCKERS_JS
    no_wait_after=True,
)

# 3. Inject code interceptors (WebSocket, Fetch, XHR, mutations)
page.add_init_script(get_init_script())
```

The locator handler is the key innovation — it fires automatically before every
`.click()`, `.fill()`, `.hover()`, `.drag_to()` call on any locator. This means
popups are dismissed without explicit agent calls throughout the step.

## Per-Step Pipeline (`run_step`)

Each step goes through this pipeline:

```
┌──────────────────────────────────────────────────────────────┐
│ 1. SETUP                                                      │
│    • Clear observer codes from previous step                  │
│    • Scroll to top                                            │
│    • Check for error page recovery                            │
│    • Extract page info (instruction, buttons, interactives)   │
│    • DNA scan gating (only if no recipe + many small texts)   │
├──────────────────────────────────────────────────────────────┤
│ 1b. SYSTEM 1: REFLEX (Recipe Replay)                          │
│    • If stored recipe exists → replay ActionSteps             │
│    • Locator cascade: role/name → text → CSS → DNA → coords  │
│    • Post-step assertions (selector visible, text match, DOM) │
│    • On assertion fail → abort, fall through to System 2      │
│    • On success → submit code, record success + DNA           │
├──────────────────────────────────────────────────────────────┤
│ 2. POPUP DISMISSAL                                            │
│    • Run popup agent once (safety net)                        │
│    • Check for blocking modals with radio buttons             │
│    • Locator handler covers the rest automatically            │
├──────────────────────────────────────────────────────────────┤
│ 3. CHALLENGE DETECTION                                        │
│    • Scored matching: 0.35*keyword + 0.25*dom + 0.1*flags     │
│      + 0.15*text_ctx + 0.15*dna (no API cost)                │
│    • Vision API: screenshot → ChallengeAnalysis               │
│    • Fallback: text-based detection via RealChallengeDetector │
│    • Result: action type (scroll/click/draw/drag/hover/etc.)  │
├──────────────────────────────────────────────────────────────┤
│ 4. ACTION DISPATCH                                            │
│    • Route to specialist agent based on detected action       │
│    • Early-detect patterns (hidden DOM, canvas, radio, etc.)  │
│    • Instruction-based fallback detection                     │
├──────────────────────────────────────────────────────────────┤
│ 5. CODE EXTRACTION (priority order)                           │
│    a. Vision-extracted code                                   │
│    b. Agent return value (draw, drag_drop, decode, etc.)      │
│    c. Observer hooks (window.__getAllCodes)                    │
│    d. harvest_and_score() with code_scorer                    │
│    e. extract_code agent (broad DOM search)                   │
│    f. Fallbacks: hidden_dom, shadow_dom, decode               │
├──────────────────────────────────────────────────────────────┤
│ 6. SUBMISSION                                                 │
│    • _submit_and_wait(code) → code_entry + wait_for_url      │
│    • Instant detection via wait_for_url(lambda)               │
│    • code_already_submitted flag prevents double-advance      │
├──────────────────────────────────────────────────────────────┤
│ 7. SYSTEM 2: LEARNING SIDECAR (if still stuck)                │
│    • VisionLearningAgent.propose_actions() — Claude plans     │
│    • LearningSidecar executes actions (anchor-safe, framed)   │
│    • Measures: DOM change, progress delta, clickable hash     │
│    • Harvest codes: observers → JS extraction → score         │
│    • Up to 6 rounds × 3 actions, stall detection (2 rounds)  │
│    • Frame sweep on iframe pages when stalled                 │
│    • Returns {code, candidates, promotion_candidate}          │
│    • Candidates ranked by provenance scoring (0.0-1.0)        │
├──────────────────────────────────────────────────────────────┤
│ 8. REJECTION LOOP (orchestrator-owned)                        │
│    • MAX_SIDECAR_CALLS=2, MAX_SUBMISSIONS=3 per step          │
│    • For each candidate in ranked list:                       │
│      - Submit via code_entry + wait_for_url                   │
│      - If rejected → sidecar.note_rejection(code, meta)       │
│      - Try next candidate                                     │
│    • If all candidates rejected → re-invoke sidecar           │
│      (sidecar filters rejected codes on 2nd call)             │
│    • On success: finalize_promotion() with 3 guards:          │
│      ① Causal actions exist (code didn't fall from sky)       │
│      ② Strong assertions (dom_change/progress/code_visible)  │
│      ③ Stable locator (testid/role/aria/text, not coords)    │
│    • Recipe compressed + stored for System 1 replay           │
└──────────────────────────────────────────────────────────────┘
```

## Agent System

### Base Class

All agents inherit from `Agent` (in `agents/base.py`):

```python
class Agent:
    name = "unnamed"
    def run(self, page, step: int, version: int) -> bool:
        raise NotImplementedError
```

Some agents return `str | None` (a code) instead of `bool`. The orchestrator
handles both.

### Agent Registry

`agents/__init__.py` instantiates every agent and builds `ALL_AGENTS`:

```python
ALL_AGENTS = {
    "popup": PopupAgent(),
    "scroll": ScrollAgent(),
    "click_reveal": ClickRevealAgent(),
    "dna_reasoner": DNAReasoner(),
    "recipe_executor": RecipeExecutor(),
    # ... 25 agents total
}
```

The orchestrator accesses agents via `self.a["agent_name"].run(page, step, version)`.

### Agent Invocation Patterns

```python
# Simple call with logging
self._run_agent("scroll", page, step, version)

# Call with safety check (detects accidental 404 navigation)
self._run_agent_safe("click_reveal", page, step, version)

# Submit code and wait for URL change (instant detection)
self._submit_and_wait(page, code, step, version, current_url)
```

## Code Scoring System

`code_scorer.py` prevents decoy codes from being submitted. A candidate code
goes through:

1. **Hard filters** (instant reject):
   - Not 6 characters → reject
   - Characters outside CHARSET → reject
   - In dictionary (SUBMIT, SCROLL, REVEAL, etc.) → reject
   - All same character → reject

2. **Soft scoring** (0.0 to 1.0):
   - Shannon entropy (higher = more likely real)
   - Letter-digit mix bonus
   - On-screen visibility bonus
   - Appeared-after-action recency bonus
   - Frequency penalty (seen too many times = decoy)

Threshold: score > 0.5 to accept.

## JavaScript Hooks (`init_hooks.py`)

Injected before page load via `page.add_init_script()`. Creates global observers:

| Hook | What It Captures |
|------|-----------------|
| `WebSocket.onmessage` | Codes sent via WebSocket |
| `EventSource.onmessage` | Codes sent via Server-Sent Events |
| `fetch()` wrapper | Codes in fetch response bodies |
| `XMLHttpRequest` wrapper | Codes in XHR responses |
| `MutationObserver` | Codes appearing in DOM mutations |
| Shadow DOM forced open | Makes all shadow roots accessible |

Captured codes are stored in `window.__codeBus` and `window.__mutCodes` with
timestamps, accessible via `window.__getAllCodes()`.

## Navigation Safety

### The Problem
The challenge site is a React SPA. Direct navigation to `/stepN` returns 404.
Clicking `<a>` tags can trigger browser navigation away from the SPA.

### The Solution (Multiple Layers)

1. **`_safe_click(x, y)`** — checks URL before/after every mouse click. If URL
   changes unexpectedly, triggers recovery.

2. **Anchor tag filter** — Vision click coordinates are validated against
   `elementFromPoint()`. Elements inside `<a>` tags are refused.

3. **`_recover_from_navigation(url)`** — if we land on a 404, navigates to `/`
   and uses `pushState` + `PopStateEvent` to restore the step.

4. **`_is_error_page()`** — checks title and body for 404 indicators.

## Learning System (V2 — Canonical Learnings)

### Overview

One canonical learning per challenge type, stored in `knowledge/learnings.json`.
Each learning holds up to 3 strategy variants with independent confidence tracking,
rollback history, and failure patterns.

### Knowledge Reader (`knowledge_reader.py`)

Detects challenge types via **5-factor scored matching** (no API cost):

```
match_score = 0.35*keyword + 0.25*dom + 0.1*flags + 0.15*text_ctx + 0.15*dna
```

- `keyword_score`: Regex hits × per-pattern weights against page text
- `dom_score`: Fraction of expected `dom_signals` satisfied (via JS)
- `flag_score`: DOM flags (has_canvas, has_shadow, has_ws, etc.)
- `text_ctx_score`: Instruction keywords, button labels, interactive types matching stored context
- `dna_score`: High-confidence DNA signatures (3+ occurrences) matched against live DOM elements

Returns `(CanonicalLearning, StrategyVariant)` — the best-matching learning and
variant, selected by `match_score * wilson_confidence * staleness_penalty * agent_health`.

The `agent_health` factor (0.3–1.0) penalizes variants whose `action_type` maps to
an underperforming agent: `agent_health = max(0.3, 1.0 - failure_rate * 0.5)`.

### System 1/System 2 Tiered Solver

Each `StrategyVariant` can now store:
- `action_recipe`: List of `ActionStep` dicts for System 1 replay
- `successful_dna_signature`: Winning DNA from last success
- `page_context`: Text patterns (instruction keywords, button labels, interactive types)

Each `CanonicalLearning` aggregates at type level:
- `dna_signatures`: DNA entries with occurrence counts (3+ = high confidence)
- `page_text_context`: Merged instruction keywords, button labels, interactive types

**System 1 Flow:** Before vision/agents, check for stored recipe → replay with assertions →
submit code. If any `expect_*` assertion fails, abort and continue to System 2.

**System 2 Flow:** After all agents fail, DNA scan → cluster → assemble → submit. On
success, record DNA signature for future `detect_and_get()` scoring.

**Guard Rails:**
- DNA scan only runs when: no recipe + confidence < 0.7 + many small text fragments (>10)
- DOM stability check before scanning (wait for element count to stabilize, up to 2s)
- Max 10 steps per recipe, max 5 DNA signatures per learning type
- Failed DNA code submits are blacklisted for the rest of the step

### Confidence: Wilson Score Lower Bound

Sole confidence metric. No manual decay. Uses `wilson_score_lower(successes, total, z=1.0)`
from the beta distribution. Small sample sizes are handled correctly (new learnings
with 1 success don't get 100% confidence).

### Failure Feedback Loop

```
On Failure:
  1. If 3+ consecutive failures and rollback exists → ROLLBACK (restore previous version)
  2. If 5+ consecutive failures, no rollback → DISABLE (TTL, rest of run)
  3. Record failure (dom_sig_before/after, dom_change_score, failure_patterns)
  4. LLM refinement (max 1 per step):
     - parameter_tweak (different selector, longer wait)
     - logic_rewrite (click → drag, hover → click-and-hold)
     - new_variant (different subtype needs separate handling)
```

### DOM Change Score

Float 0.0–1.0 measuring structural DOM change (not just boolean). Guides refinement:
- `~0.0`: Selector wrong / event not triggered → fix targeting
- `~0.1-0.3`: Action triggered something but not the right thing → fix logic
- `~0.8+`: Major state transition, code not extracted → fix extraction

### V1 Seeding (`V1_SEED_STRATEGIES`)

On first load (or when types are missing), 15 default strategies are seeded from
`V1_SEED_STRATEGIES` in `knowledge_reader.py`. Each gets a bootstrap prior of
`successes=2, attempts=3` (Wilson ≈ 0.386) so they're available but don't outcompete
learnings refined through actual experience.

### Agent Performance Tracking (`agent_tracker.py`)

Per-agent success/failure rates persisted to `knowledge/agent_performance.json`.
Feeds into both refinement prompts ("hover agent has 70% failure rate") AND variant
routing (agent health penalty in `_pick_best_variant`).

### System 2: LearningSidecar + VisionLearning (Planner/Controller)

The System 2 pipeline splits into two components:

**VisionLearningAgent (planner)** — `agents/vision_learning.py`:
- `propose_actions(page, step, version, context, history, timeout_s)`: Captures screenshot +
  page info, calls Claude API, returns `{actions, notes, stop, extracted_codes}` without executing.
- `reset_conversation()`: Clears multi-turn state between steps.
- `run()`: Legacy monolithic loop (preserved as revert path).
- Multi-turn conversation accumulates in `self._messages`.

**LearningSidecar (controller)** — `agents/learning_sidecar.py`:
- Owns the closed loop: propose → execute → observe → iterate → return code.
- Up to 6 rounds, 3 actions/round. Stall detection after 2 no-progress rounds.
- Executes actions itself (no VL state mutation) with anchor blocking and frame awareness.
- Measures progress via 4 signals: DOM change >= 0.05, progress delta > 0, new codes, clickable hash changed.
- Three-layer code harvest: observers → JS extraction → harvest_and_score.
- All codes validated via charset regex + `__isValidCode` JS oracle.
- **Rejection tracking**: Per-step `_rejected_codes` (exact blacklist) + `_rejected_signatures`
  (source+evidence tuples). Filters rejected codes from all harvest points. Clears signature
  rejections on meaningful progress (exact code blacklist stays).
- **Ranked candidates**: `_harvest_all_candidates()` returns all valid non-rejected codes with
  provenance metadata. `_score_candidate()` ranks by baseline timing, harvest score, DOM change,
  progress, source preference, and signature rejection penalty (0.0-1.0 scale).
- Builds promotion candidate with recipe steps, locator cascades, assertions, DNA signatures.
- Never submits codes — returns `{code, candidates, promotion_candidate}` to orchestrator.
- `note_rejection(code, meta)`: Called by orchestrator when a submission is rejected.
- `finalize_promotion()`: Called by orchestrator ONLY after confirmed advancement. Three guards:
  causal actions exist, strong assertions present, at least one stable locator.

**Orchestrator Rejection Loop** — `orchestrator.py` Phase 3:
- `_invoke_sidecar()` (renamed from `_system2_reasoning()`): Returns full result dict.
- `_submit_and_record()`: Returns `(bool, str)` — `'solved'`, `'rejected'`, or `'already_failed'`.
- Loop: up to `MAX_SIDECAR_CALLS=2` sidecar invocations × `MAX_SUBMISSIONS=3` total submissions.
- On rejection: `sidecar.note_rejection(code, candidate_meta)` then try next candidate.
- If all candidates exhausted, re-invoke sidecar (which now filters rejected codes).

**LLM Learning Agent** — `agents/learning.py`:
- `refine_learning()`: Constrained LLM refinement with ACTION_SCHEMAS validation
- `bootstrap_learning()`: LLM-bootstrapped first-time learning from successful episode
- Only activated after standard agents fail.

## Data Flow Diagram

```
                        ┌─────────────┐
                        │  Challenge  │
                        │    Page     │
                        └──────┬──────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                 ▼
      ┌──────────────┐ ┌────────────┐  ┌───────────────┐
      │ init_hooks   │ │ Vision API │  │ page.inner_   │
      │ (WebSocket,  │ │ screenshot │  │ text('body')  │
      │  Fetch, DOM) │ │ analysis   │  │               │
      └──────┬───────┘ └─────┬──────┘  └───────┬───────┘
             │               │                  │
             ▼               ▼                  ▼
      window.__codeBus  ChallengeAnalysis  Text detection
             │               │                  │
             └───────────────┼──────────────────┘
                             ▼
                    ┌────────────────┐
                    │  Agent Action  │
                    │  (draw, drag,  │
                    │   scroll, etc) │
                    └────────┬───────┘
                             │
                             ▼
                    ┌────────────────┐
                    │ Code Extraction│
                    │ + code_scorer  │
                    └────────┬───────┘
                             │
                             ▼
                    ┌────────────────┐
                    │  code_entry    │
                    │  + wait_for_url│
                    └────────┬───────┘
                             │
                             ▼
                      Next step (or retry)
```

## File Dependency Graph

```
solve.py
  └── orchestrator.py
        ├── config.py
        ├── page_state.py
        ├── log.py
        ├── code_scorer.py
        ├── init_hooks.py
        ├── primitives.py            (extract_code_js, read_progress, get_locator_cascade, reset)
        ├── knowledge_reader.py      (CanonicalLearning, StrategyVariant, 5-factor scored detection, DNA + recipes)
        ├── agent_tracker.py         (per-agent success/failure tracking)
        ├── agents/__init__.py
        │     └── agents/*.py (32 agents)
        │           └── agents/base.py
        │           └── agents/popup.py          (DISMISS_JS, CLEAR_BLOCKERS_JS, POPUP_AUTO_DISMISS_SETUP_JS)
        │           └── agents/learning.py       (refine_learning, bootstrap_learning)
        │           └── agents/dna_reasoner.py   (System 2: DNA clustering + code assembly)
        │           └── agents/recipe_executor.py (System 1: recipe replay + assertions)
        │           └── agents/vision_learning.py (VL planner: propose_actions + run)
        │           └── agents/learning_sidecar.py (System 2 controller: execute + observe + iterate)
        └── knowledge/
              ├── learnings.json          (canonical learnings, atomic writes)
              └── agent_performance.json  (agent success/failure stats)
```

**Archived (no longer imported):**
- `learning_archived/` — 7 files (~2500 lines): observation.py, memory.py, retrieval.py, execution.py, self_play.py, feedback.py, patterns.py
- `learning_db.py` — deleted (was JSON LRU cache)

## Configuration Reference

All in `config.py`:

| Constant | Value | Purpose |
|----------|-------|---------|
| `BASE_URL` | `https://serene-frangipane-7fd25b.netlify.app` | Challenge site |
| `CHARSET` | `ABCDEFGHJKLMNPQRSTUVWXYZ23456789` | Valid code characters (no I,O,0,1) |
| `VIEWPORT` | `1280×1024` | Browser viewport size |
| `STEP_TIMEOUT` | 15s | Max time per step |
| `POPUP_WAIT` | 200ms | Wait after popup dismissal |
| `POST_SUBMIT_WAIT` | 400ms | Wait after code submission |
| `VISION_MIN_CONFIDENCE` | 0.5 | Minimum confidence to trust vision |

## Metrics

After each run, `metrics.json` contains per-step timing, agents used, vision API
token counts, and overall success rate. The `Metrics` class in `metrics.py` tracks
everything and prints a summary table to stdout.
