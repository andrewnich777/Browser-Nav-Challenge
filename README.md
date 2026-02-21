# Browser Navigation Challenge Solver

Two approaches to solving a 30-step browser navigation challenge at [serene-frangipane-7fd25b.netlify.app](https://serene-frangipane-7fd25b.netlify.app). Each step presents a different UI puzzle — click buttons, solve math, decode strings, draw gestures, drag puzzle pieces, navigate shadow DOM, intercept WebSockets — and reveals a 6-character code you enter to advance.

The site randomizes across 3 versions per session. Each version shuffles which challenge type appears at which step, so you can't hardcode the answers.

## Results

| Approach | Steps | Time | API Cost | How |
|----------|-------|------|----------|-----|
| **V1** — Reverse-engineer JS | 30/30 | ~75s | $0.00 | Compute codes from decompiled source |
| **V2-V4** — Multi-agent system | 30/30 | ~3:25 | $0.00 | 25 deterministic agents that actually solve each challenge |

V1 is a shortcut. It reads the answers from the source code. V2-V4 is the real solve — it interacts with each challenge the way a human would, using clicks, hovers, scrolls, keyboard input, canvas drawing, and drag-and-drop.

---

## V1: The Reverse-Engineering Bypass (`solve.py`)

CTF-style source code analysis. Decompiled the minified `bundle.js` and found:

- **Code generation** is deterministic: `Rl(step+1, version)` produces a 6-char code from charset `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`
- **Interaction check** reads a token from `sessionStorage` at key `challenge_interaction_step_{N}`
- **Validation** requires both the correct code and the interaction token
- Codes are stored in `sessionStorage` under key `wo_session`, encoded as Base64 + XOR with key `"WO_2024_CHALLENGE"`

The solver computes codes directly, injects interaction tokens, and dismisses popup overlays. Zero LLM calls.

**30/30 in ~75 seconds, $0.00.** Works perfectly, but only because we have the source. See [APPROACH.md](APPROACH.md) for the full reverse-engineering notes.

### Run V1

```bash
pip install playwright && python -m playwright install chromium
python solve.py
```

---

## V2-V4: The Multi-Agent System (`v2-multi-agent/`)

This is the actual project — a system that solves browser challenges by looking at the screen, understanding the puzzle, and performing the required interaction. No knowledge of the source code. Every technique is designed to work on arbitrary websites we've never seen (see [MISSION.md](v2-multi-agent/MISSION.md)).

Built across 29 sessions (~45 hours) with Claude Code as the implementation partner. I did architecture, constraints, and diagnostic methodology. Claude Code wrote the Python — 24K lines across 60 files.

### Architecture

A 4-phase pipeline runs for each of 30 steps:

1. **V4 Agents** (Phase 1) — 25 deterministic challenge agents + 5 universal agents. Zero API cost, sub-second. One agent per challenge type: click_reveal, scroll, hover, decode, drag_drop, gesture, audio, video, shadow_dom, websocket, service_worker, puzzle_solve, and 13 more.

2. **Passive Checks** (Phase 2) — MutationObservers, WebSocket interceptors, DNA clustering (grouping DOM elements by computed CSS style to find code characters). Zero cost.

3. **Vision Sidecar** (Phase 3) — Claude Sonnet 4.5 takes screenshots and reasons about challenges. Currently unused — all 30 steps are solved by Phase 1 agents. But it's still there as a universal fallback.

4. **Exception Handlers** (Phase 4) — Three intentionally broken demo challenges that need specific workarounds after the agent solves the challenge normally.

Detection uses a 3-layer cascade: DOM structure first, then semantic roles/labels, then instruction text matching.

### The 25 Challenge Types

| Complexity | Agents |
|------------|--------|
| Simple (5-15 LOC) | click_reveal, scroll, hidden_dom, timing, audio |
| Medium (15-30 LOC) | hover, decode, delayed_reveal, delay_memory, gesture, keyboard_sequence, split_parts, puzzle_solve, calculated, video, sequence, mutation, multi_tab, conditional_reveal |
| Complex (30-50 LOC) | drag_drop, shadow_dom, recursive_iframe, websocket, service_worker |

Each agent is a pure function: `solve(ctx: StepCtx) -> str | None`. Returns a 6-char code or `None` to fall through to the next phase.

### Run V2-V4

```bash
cd v2-multi-agent
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # add ANTHROPIC_API_KEY (only needed if sidecar fallback is triggered)
python solve.py --headed
```

### Run Statistics (V4, Session 29)

| Version | Steps | Time | API Calls | Cost |
|---------|-------|------|-----------|------|
| v=1 | 30/30 | 3:40 | 0 | $0.00 |
| v=2 | 30/30 | 3:49 | 0 | $0.00 |
| v=3 | 30/30 | 3:23 | 0 | $0.00 |

---

## What Failed Along the Way

V4 is the version that works. V2 and V3 didn't.

### V2: 32 Specialist Agents with Vision Dispatch (~10/30, $2-5/run)

The first attempt at "real" solving. 32 specialist agents, each handling one challenge type, with Claude Sonnet 4.5 doing challenge type detection from screenshots and dispatching to the right agent.

What went wrong:

- **Scroll false positive epidemic.** Every page has 99 filler "Section N" blocks as decoys. The orchestrator would see "scroll down" in the page text and dispatch the scroll agent on every step. Fingerprinting the filler section count didn't help — the count is the same on every page.
- **React white screen of death.** Any DOM mutation — `el.remove()`, `el.style.display`, React fiber state changes — instantly crashes the app. We learned this the hard way trying to dismiss popups by removing them.
- **The popup arms race.** The site generates popups with React 18's concurrent renderer. Plain `.click()` doesn't work because React doesn't see synthetic events. Required a full pointer event sequence matching what a real user generates.
- **Stale code problem.** React SPA — no page reload between steps. Codes from previous steps persist in the DOM. Without a step boundary marker, extractors would pick up old codes.
- **The learning system that didn't learn.** 17 seeded strategies, Wilson score confidence, DNA signatures, LLM-powered refinement. In practice: scored detection mostly matched the wrong challenge type (keyword overlap), seeded strategies were too generic, refinement budget was spent on hallucinated improvements. The system compounded noise, not knowledge.

### V3: Recipe Replay System (peaked at 62% hit rate, then killed)

The idea: when the vision sidecar solves a challenge, extract a replayable "recipe" (a DSL action sequence), store it, and replay it instantly next time. System 2 (AI) discovers, System 1 (recipes) replays.

In theory: elegant. In practice:

- **Promoted recipes fail on replay ~50% of the time.** The recipe stores `target_text: "Capture (0/3)"` but on replay the button says `"Capture"`. Locator cascade fails. Falls back to coordinates, which are fragile.
- **Detection cross-contamination.** Steps 1-15 all report as "simple" from the config table, so keyword detection is the only way to match recipes. Multiple challenge types share keywords (e.g., "puzzle_solve" and "calculated" both have math words). Recipes match the wrong challenge type.
- **Session-specific data baked into recipes.** Button text like "Tab 1: KA" (session code), frame numbers, counter states — all baked into the recipe and wrong on the next run.
- **Rube Goldberg promotion pipeline.** The path from "sidecar solved it" to "recipe stored" went through 7 stages, any of which could silently fail: build candidate → clean steps → compress → lint → 10+ guard checks → keyword generation → storage. Debugging required reading 200+ lines of promotion code.
- **Recipe overwrites.** When the sidecar re-solved a challenge that already had a working tier-3 recipe, it overwrote the proven recipe with a potentially worse one.

Session 22 deleted the entire recipe system (~290 lines) along with 4 dead agent files and ~1000 lines of dead code. The V4 deterministic agents replaced it — same zero-cost instant solving, but with explicit code instead of learned recipes.

See [JOURNEY.md](v2-multi-agent/JOURNEY.md) for the full V1 → V2 → V3 → V4 evolution with timeline.

---

## How V2-V4 Was Built

### Division of Labor

**Me (human):** Architecture decisions, constraints ([MISSION.md](v2-multi-agent/MISSION.md)), diagnostic methodology, knowing when to change strategy.

**Claude Code:** All the Python. 24K lines, 60 files, 25 challenge agents, 30+ shared helpers, diagnostic tools, the vision sidecar, the orchestrator pipeline.

Key human decisions that shaped the project:
- Killed the recipe replay system (Session 22) when it became an over-engineered liability
- Wrote [MISSION.md](v2-multi-agent/MISSION.md) to prevent drift into site-specific hacks — every technique had to be generalizable. This constraint caught 6 violations when re-examined with fresh eyes
- Designed the diagnostic tooling workflow so failures could be debugged from data instead of guesswork
- Established the "fresh session" methodology: when stuck for 2+ iterations on the same bug, start a new Claude Code session with only the diagnosis data. This broke several multi-session dead ends where context accumulation caused tunnel vision

### The Coverage Engineer Flywheel

The core development loop that built all 25 agents:

```
    Coverage Engineer (Claude Code)
        |                    ^
        | refine             | richer discoveries
        v                    |
    Primitives ----------> Sidecar (exploration)
        |
        | crystallize
        v
    V4 Agents (output)
```

The Coverage Engineer improves shared primitives (helpers for clicking, hovering, dragging, traversing shadow DOM). The Vision Sidecar — Claude Sonnet 4.5 taking screenshots and reasoning about challenges — uses those primitives to discover how to solve new challenge types. The CE studies those discoveries and feeds insights back into better primitives.

Once a challenge type is well-understood, the pair crystallizes the solution into a deterministic agent (10-50 lines of Python, zero API cost, sub-second). This is how 25 agents were built. The sidecar went from solving 20+ steps per run to solving zero — replaced by agents extracted from knowledge it helped generate.

**$0.60/run → $0.00/run with the same 100% accuracy.**

### The Recurring Theme

Every major improvement came from making the system simpler, not more complex:
- Scroll blacklist (3 lines) beat scroll fingerprinting (50 lines)
- Read-only JS policy beat DOM manipulation workarounds
- Single DSL executor beat 32 independent agent dispatch paths
- Deterministic agents ($0.00) beat vision API calls ($0.60/run)
- Diagnostic tooling beat guesswork debugging
- Deleting the recipe system (290 lines) beat fixing it

---

## Design Principles

**Every technique must work on arbitrary websites** ([MISSION.md](v2-multi-agent/MISSION.md)). React-compatible input filling, Playwright-native drag-and-drop, accessible shadow DOM traversal. Three intentionally broken demo challenges have labeled exceptions; everything else works through standard UI.

**Never modify the DOM.** All injected JavaScript is read-only — observe, query, extract, never mutate. DOM mutations crash the React app instantly.

**Observability-first.** Built diagnostic tools (`diagnose.py`, `compare.py`, `scope_check.py`) to debug the automation itself. When an agent fails, you see exactly what it saw and what it did wrong.

---

## Project Structure

```
Browser Nav Challenge/
├── README.md              ← you are here
├── solve.py               ← V1 deterministic solver
├── APPROACH.md            ← V1 reverse-engineering notes
├── CLAUDE.md              ← Session orientation for Claude Code
│
└── v2-multi-agent/        ← V2-V4 multi-agent system (active codebase)
    ├── solve.py            ← Entry point
    ├── orchestrator.py     ← Core loop: detect → agent → extract → submit
    ├── agents/
    │   ├── v4/
    │   │   ├── challenges/ ← 25 challenge agents (one per type)
    │   │   ├── helpers.py  ← 30+ shared interaction primitives
    │   │   ├── universal.py← 5 cross-cutting agents
    │   │   └── context.py  ← StepCtx dataclass
    │   ├── learning_sidecar.py  ← Vision sidecar controller
    │   └── vision_learning.py   ← Claude Sonnet 4.5 planner
    ├── code_scorer.py      ← 13-factor code validation & decoy filtering
    ├── init_hooks.py       ← MutationObserver + WebSocket interceptors
    ├── primitives.py       ← Canvas drawing, drag sequences, iframe enumeration
    ├── knowledge_reader.py ← Canonical learnings, Wilson confidence, detection
    ├── diagnose.py         ← Single-step telemetry dump
    ├── compare.py          ← Agent vs sidecar action diff
    ├── scope_check.py      ← Boundary validation for all 30 steps
    ├── MISSION.md          ← Decision framework (source of truth)
    ├── JOURNEY.md          ← V1 → V2 → V3 → V4 design evolution
    ├── FAILURES.md         ← Known failure patterns with root causes
    └── docs/               ← Architecture deep dives and archive
```

## Further Reading

| Doc | What It Covers |
|-----|----------------|
| [APPROACH.md](APPROACH.md) | V1 reverse-engineering: how the JS bundle was decompiled |
| [JOURNEY.md](v2-multi-agent/JOURNEY.md) | Full design evolution — what failed, what worked, and why |
| [MISSION.md](v2-multi-agent/MISSION.md) | Decision framework: what counts as "human-like", labeled exceptions |
| [FAILURES.md](v2-multi-agent/FAILURES.md) | Every known agent failure with root cause and diagnosis |
| [COVERAGE_ENGINEER.md](v2-multi-agent/docs/COVERAGE_ENGINEER.md) | The dev-time iteration role and diagnostic toolbox |
| [EXTERNAL_REVIEW_PROMPT.md](EXTERNAL_REVIEW_PROMPT.md) | Architecture review request written mid-V3 (shows the recipe problems in real-time) |

## Prerequisites

- Python 3.11+
- Playwright (`pip install playwright && playwright install chromium`)
- For V2-V4 vision fallback only: Anthropic API key in `v2-multi-agent/.env`
