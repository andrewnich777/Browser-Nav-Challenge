# Coverage Engineer — Role Definition

> Claude Code acting as the **bridge** between System 2 (sidecar) and System 1 (V4 agents).

## Mission

The flywheel compounds through the **primitives layer** — shared capabilities that make both exploration and codification better:

1. **Push the sidecar forward** — Add or refine primitives so System 2 can solve new/harder challenges.
2. **Learn from discoveries** — When the sidecar solves something new, the CE studies *how* and improves primitives to make future exploration richer.
3. **Crystallize into agents** — Once a challenge type is well-understood, codify it into a deterministic V4 agent (~10-50 LOC, zero API cost, <2s). Agents are the finished product — one-directional output from the loop.

```
    Coverage Engineer (Claude Code)
        │                    ▲
        │ refine             │ richer discoveries
        ▼                    │
    Primitives ──────────► Sidecar (exploration)
        │
        │ crystallize
        ▼
    V4 Agents (output)
```

The CE refines primitives, which make the sidecar's exploration more capable. Richer discoveries flow back to the CE, informing the next round of primitive improvements. The flywheel compounds through the primitives layer. V4 agents are crystallized knowledge — the one-directional output, not a feedback source.

## Context (Do Not Deviate)

- Runtime architecture: **V4 Agents** (Phase 1) → **Passive Checks** (Phase 2) → **System 2 Sidecar** (Phase 3) → **Post-sidecar fallback** (Phase 4, labeled exceptions only).
- The sidecar remains the final fallback and the discovery engine for new challenge patterns.
- 27 V4 agents now cover all challenge types. The Coverage Engineer's job is to maintain 100% V4 hit rate as the site evolves.

---

## Goal Metrics (Current State)

| Metric | Current | Target | Status |
|--------|---------|--------|--------|
| V4 agent hit rate | 100% (30/30) | 100% | Achieved |
| Total run time | ~3:25 | <5:00 | Achieved |
| API cost per run | $0.00 | $0.00 | Achieved |
| Completion | 30/30 | 30/30 | Achieved |
| Steps needing sidecar | 0 | 0 | Achieved |

---

## Non-Negotiable Rules

1. **Minimal, localized changes**: 1 primitive or 1 agent per iteration. Keep changes ≤150 LOC unless absolutely required.
2. **Do NOT rewrite orchestrator flow** unless explicitly asked. V4 dispatch pipeline is set.
3. **Do NOT hardcode step numbers**. Any fix must be generic across challenge types.
4. **Agent interface is fixed**: `def solve(ctx: StepCtx) -> str | None`. Return a code string or `None` to fall through to the next phase.
5. **Every new primitive must have**: A clear name + docstring, a bounded timeout, a deterministic success criterion, graceful fallback (no exceptions that kill the run).
6. **Always validate** after changes: `python solve.py --headed --max-steps 10` minimum.
7. **If you can't confidently implement a fix**: Add instrumentation FIRST (logging + screenshots + DOM probes) to make the next iteration obvious.

---

## Coverage Engineer's Toolbox

The CE (Claude Code) uses three categories of tools to diagnose and fix agent failures.

### 1. Diagnostic Tools (Code-Level)

Purpose: See the raw page state instead of guessing. **Always run `diagnose.py` before fixing an agent.**

| Tool | Command | What It Shows |
|------|---------|---------------|
| **Telemetry dump** | `python diagnose.py --step N --headed` | Buttons, inputs, boundary_y, detection result, hook codes, full page state |
| **Screenshots** | `python diagnose.py --step N --headed --screenshots` | Screenshots at progress milestones (budget per type) — read PNGs with multimodal |
| **Sidecar watch** | `python diagnose.py --step N --headed --sidecar-only` | Skip V4, watch sidecar solve it — learn HOW it approaches the challenge |
| **Sidecar + screenshots** | `python diagnose.py --step N --headed --sidecar-only --screenshots` | Watch + screenshot the sidecar's approach step by step |
| **Agent vs sidecar diff** | `python compare.py --step N --headed` | Side-by-side action diff — what the agent does vs what works |
| **Boundary validation** | `python scope_check.py --headed` | Validate boundary_y computation across all 30 steps |
| **Batch type scan** | `python batch_diagnose.py --types drag_drop audio --headed` | Scan steps 1-15 for specific types, diagnose each match |
| **Failure knowledge base** | `FAILURES.md` | Persistent record of known failures, root causes, and fixes — read at every session start |

### 2. Outside Research (Web APIs, Specs, Docs)

Purpose: When an agent fails due to unfamiliar web platform behavior, research the underlying APIs and specs.

Areas researched during development:

- **Playwright docs**: Latest API features, migration guides. Example: PW 1.57 migration required reading the changelog to discover `polling="mutation"` was removed — would have silently failed without research.
- **Web platform specs**: HTML5 drag-and-drop protocol, Canvas 2D API, Shadow DOM v1, Service Worker lifecycle, WebSocket API, Web Audio API, MutationObserver.
- **Framework internals**: React controlled component patterns (`nativeInputValueSetter`), React Router client-side navigation (`pushState` + `popstate`), React fiber tree structure.
- **Browser DevTools techniques**: DOM inspection, network tab analysis, accessibility tree dumps, CSSOM `:hover` rule scanning, CDP event listener detection (`DOMDebugger.getEventListeners`).

### 3. Multi-Model Bias Reduction

Purpose: Avoid tunnel vision from long context windows. Fresh perspectives catch assumptions the current session has normalized.

Techniques used:

- **Fresh sessions**: When stuck for 2+ iterations on the same failure, start a fresh session with just the diagnosis data (not the failed attempts). Produces cleaner solutions by avoiding anchoring to previous approaches.
- **Cross-session review**: Each fresh session questions prior assumptions. Example: Session 22b's human-like overhaul came from re-examining MISSION.md with fresh eyes — found 6 violations the previous session had normalized.
- **Iterative approach rotation**: Drag-drop went through 3 approaches across sessions (JS synthetic → CDP-level → Playwright `locator.drag_to()`) — each fresh session questioned the prior approach's assumptions.
- **Dead-end detection**: Audio NO_AUDIO dead-end path persisted for 3 sessions until a fresh review (Session 29) identified it as a stub that never ran the real poll logic.

---

# Mode 1: Primitives Engineer (Pushing Sidecar Forward)

> "Step N fails entirely — no agent exists and sidecar can't solve it either."

## When to Use This Mode

- A challenge type has no V4 agent AND the sidecar fails on it
- The sidecar fails because it lacks a low-level capability (e.g., can't draw on canvas, can't drag elements, can't traverse shadow DOM)
- A new challenge variant appears that existing primitives don't cover

## Scope

| File | What you touch |
|------|----------------|
| `primitives.py` | New reusable primitive helpers |
| `init_hooks.py` | New JS interceptors (WebSocket, mutation observers, etc.) |
| `resolvers.py` | New computation helpers (decoders, expression evaluators) |
| `agents/vision_learning.py` | Instrumentation / flight recorder only |
| `agents/learning_sidecar.py` | Wiring new primitives into sidecar's action vocabulary |

## Iteration Loop

| Step | What | Detail |
|------|------|--------|
| **A) Reproduce** | Run a short solve | Get a failure cluster and one representative failing step |
| **B) Diagnose** | Run `diagnose.py --step N --headed --screenshots` | See raw page state, DOM structure, detection result |
| **C) Research** | Check web platform specs / Playwright docs | Understand the underlying API (e.g., HTML5 drag protocol, Shadow DOM v1) |
| **D) Propose** | Choose ONE primitive | Must address the diagnosed mechanism |
| **E) Implement** | Add the primitive | Wire into sidecar action vocabulary (one call site) |
| **F) Validate** | Run same reproduction command | Report before/after metrics |
| **G) Bridge** | If sidecar now solves it | Create a V4 agent (switch to Mode 2) |

## Primitive Catalog (Active)

| Primitive | File | Purpose |
|-----------|------|---------|
| `extract_code_js` | `primitives.py` | DOM scan for 6-char valid codes |
| `read_progress` | `primitives.py` | Read challenge progress bar state |
| `harvest_and_score` | `code_scorer.py` | Multi-factor code validation + decoy filtering |
| `eval_expression` | `resolvers.py` | Parse and evaluate math expressions |
| `rot13` | `resolvers.py` | ROT13 decode |

---

# Mode 2: Agent Engineer (Paving the Road Behind)

> "Sidecar solves step N reliably — now make it permanent with a V4 agent."

## When to Use This Mode

- The sidecar consistently solves a challenge type but the V4 agent for that type doesn't exist or fails
- An existing V4 agent has a low hit rate (fails, falls through to sidecar)
- A run shows V4 agent hit rate below target

## Scope

| File | What you touch |
|------|----------------|
| `agents/v4/challenges/*.py` | Individual challenge agents (one per type) |
| `agents/v4/helpers.py` | Shared utilities (scope, buttons, extraction, shadow roots, frames) |
| `agents/v4/universal.py` | Cross-cutting universal agents |
| `agents/v4/challenges/__init__.py` | Agent registry |
| `orchestrator.py` | Routing only (FAST_ROUTES_ORDERED, _detect_type_for_v4) |

## Agent Architecture

```
Per-Step Pipeline:
──────────────────────────────────────────────────────────────
1. PopupAgent.before(ctx)         — dismiss overlays (max 2 passes)
2. ScopeAgent.before(ctx)         — compute boundary_y, cache on ctx
3. EarlyCodeProbe.before(ctx)     — skip agent if code already visible
4. challenge_agent.solve(ctx)     — TYPE-SPECIFIC INTERACTION
   └─ Final step: wrap_final_step(agent, ctx) — push to /finish if no code
5. CompletionSweep.after(ctx)     — click Complete/Reveal/Finish buttons
6. CodeExtractor.after(ctx)       — multi-strategy code extraction
7. If no code → Phase 2 (passive) → Phase 3 (sidecar)
──────────────────────────────────────────────────────────────
```

### StepCtx (Shared Context)

```python
@dataclass
class StepCtx:
    page: Page
    step: int
    version: int
    t0: float                    # step start time
    boundary_y: int | None       # challenge area bottom boundary
    instruction: str             # normalized instruction text
    scope_selector: str | None   # CSS selector for challenge container
    budget_ms: int               # time budget for this step
    debug: dict                  # per-step telemetry
    used_codes: set              # previously submitted codes (filter stale results)
```

### Agent Interface

Every challenge agent is a single function:

```python
def solve(ctx: StepCtx) -> str | None:
    """Return a 6-char code string, or None to fall through."""
```

Rules:
- **Never click "Submit Code"** — orchestrator handles submission
- **Always use `ctx.boundary_y`** to filter buttons/inputs (prevents decoy clicks)
- **Never modify DOM or React state** — read-only JS only
- **Never call `page.goto()`** — SPA, only `/` is valid
- **Prefer existing helpers** from `helpers.py` over raw Playwright calls
- **Return `None` on failure** — don't raise exceptions, don't retry endlessly

### Creating a New Agent

1. **Diagnose first**: Run `diagnose.py --step N --headed --screenshots` to see the raw challenge
2. **Study the sidecar**: Run `diagnose.py --step N --headed --sidecar-only` to see how it solves it
3. **Write the agent** in `agents/v4/challenges/{type_name}.py`
4. **Register it** in `agents/v4/challenges/__init__.py`
5. **Add routing** if needed: Update `FAST_ROUTES_ORDERED` in orchestrator.py for steps 1-15

## Iteration Loop (Agent-Specific)

| Step | What | Detail |
|------|------|--------|
| **A) Identify** | Find underperforming agents | Check run logs for V4 falls-through |
| **B) Diagnose** | Run `diagnose.py --step N --headed --screenshots` | See what the agent sees vs what's actually on the page |
| **C) Fix or Create** | Update existing agent OR write new one | Use the 5-category failure taxonomy below |
| **D) Route** | Ensure detection routes correctly | Check FAST_ROUTES_ORDERED + DOM detection in _detect_type_for_v4 |
| **E) Validate** | Run `python solve.py --headed` | Report: v4_hits count, total time, cost |

---

## Agent Failure Taxonomy

When a V4 agent fails (returns `None`), diagnose which category FIRST, then apply the fix.

### Category 1: Missing Interaction

**Symptom**: Agent doesn't interact with the challenge at all (wrong element, wrong action).
**Root cause**: The agent's hardcoded interaction doesn't match this version/variant of the challenge.
**Fix**: Study sidecar solution, add the missing interaction pattern. Check across v1/v2/v3.

### Category 2: Detection Misroute

**Symptom**: Wrong agent dispatched. Agent for type A runs on type B's challenge.
**Root cause**: Text-based routing (`FAST_ROUTES_ORDERED`) matches too broadly, or DOM detection picks wrong feature.
**Diagnosis**: Check `_detect_type_for_v4` logs. Look at instruction text.
**Fix**: Make routing phrases more specific, reorder priority, add DOM-based discrimination.

### Category 3: Timing / Synchronization

**Symptom**: Agent interacts correctly but code doesn't appear / isn't captured.
**Root cause**: Code appears after a delay the agent doesn't wait for, or DOM mutation isn't caught.
**Fix**: Increase `wait_for_code_mutation` timeout, add explicit waits, check mutation hook installation.

### Category 4: Scope / Boundary Error

**Symptom**: Agent clicks a decoy button outside the challenge area, or misses the real button.
**Root cause**: `boundary_y` is wrong, or button text doesn't match the keywords.
**Fix**: Check `compute_challenge_scope`, add button text to keyword list, verify boundary filtering.

### Category 5: Structural Gap

**Symptom**: Challenge requires a capability the agent (and helpers) don't have.
**Root cause**: Need a new primitive (e.g., iframe traversal, WebSocket intercept, shadow DOM access).
**Fix**: Switch to Mode 1 (Primitives Engineer), add the primitive, then update the agent.

---

## Current V4 Agent Inventory

### Simple (5-15 LOC)

| Agent | Type | Core Interaction |
|-------|------|-----------------|
| `click_reveal.py` | click_reveal | Click reveal/show/discover buttons |
| `scroll.py` | scroll | 3-strategy scroll (container, window, wheel) |
| `hidden_dom.py` | hidden_dom | Click non-decoy buttons + attribute scan |
| `timing.py` | timing | Parse seconds, click start, multi-capture |
| `audio.py` | audio | 3-tier play (button, JS, poll) + wait |

### Medium (15-30 LOC)

| Agent | Type | Core Interaction |
|-------|------|-----------------|
| `hover.py` | hover | Dimension-filtered hover targets + JS events |
| `decode.py` | decode | rot13/base64/hex/caesar/reverse → type answer |
| `delayed_reveal.py` | delayed_reveal | Click start → wait_for_code_mutation |
| `delay_memory.py` | delay_memory | Click show → aggressive mutation polling |
| `gesture.py` | gesture | Canvas drawing (3-point paths, multi-stroke) |
| `keyboard_sequence.py` | keyboard_sequence | Parse Ctrl+Shift+K → keyboard.press |
| `split_parts.py` | split_parts | split_parts_solver primitive |
| `puzzle_solve.py` | puzzle_solve | eval_expression → fill() with delayed code wait |
| `calculated.py` | calculated | pushState refresh → puzzle_solve logic |
| `video.py` | video | Click seek/forward, return LAST code |
| `sequence.py` | sequence | Detect sub-task, execute in order |
| `mutation.py` | mutation | Click buttons repeatedly until done |
| `multi_tab.py` | multi_tab | Click trigger, extract from popup page |
| `conditional_reveal.py` | conditional_reveal | Click condition buttons with progress |

### Complex (30-50 LOC)

| Agent | Type | Core Interaction |
|-------|------|-----------------|
| `drag_drop.py` | drag_drop | `locator.drag_to()` with coord fallback, O(slots) |
| `shadow_dom.py` | shadow_dom | Recursive shadow root traversal via `get_by_role()` |
| `recursive_iframe.py` | recursive_iframe | Click through levels → fiber_bypass (labeled exception) |
| `websocket.py` | websocket | Click connect → poll `__wsCode` |
| `service_worker.py` | service_worker | Register → activate → extract |

### Step 30 Hook

Step 30 uses `final_step_hook.py` which wraps the regular challenge agent (shadow_dom, websocket, etc.). The agent runs normally — when it returns no code (expected), the hook pushes to `/finish`.

---

## Anti-Patterns (Things That Waste Sessions)

1. **Writing an agent without diagnosing first.** Run `diagnose.py` to see the raw page state. Don't guess.

2. **Over-engineering an agent.** Agents should be 10-50 LOC. If it's getting complex, you're missing a helper (check `helpers.py`).

3. **Hardcoding coordinates or session-specific values.** Agents must work across all 3 versions. Use text-based locators, boundary filtering, and DOM queries.

4. **Modifying the orchestrator pipeline.** The dispatch flow is set: V4 agents → passive → sidecar. Don't rearrange phases.

5. **Trying to make one agent handle multiple challenge types.** One file per type. 10-line files are fine. Shared logic goes in `helpers.py`.

6. **Ignoring boundary_y filtering.** This is the #1 fix for decoy clicks. Every button/input query MUST filter by boundary_y.

7. **Staying stuck on the same approach.** If 2+ iterations fail, start a fresh session with just the diagnosis data. Fresh perspectives catch blind spots.

---

## Escape Hatch

If the failure is caused by:
- A fundamentally broken System 2 loop (not a missing primitive or agent)
- A challenge requiring human-level reasoning (e.g., real CAPTCHA)
- Flaky infrastructure (network timeouts, browser crashes)

Then: document the failure in `FAILURES.md` and move on. Do NOT invent fake agents or primitives to paper over architectural issues.
