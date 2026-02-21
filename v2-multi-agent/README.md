# Browser Navigation Challenge Solver

Automated solver for a 30-step browser navigation challenge. Each step presents a UI puzzle
(click buttons, solve math, decode strings, draw gestures, traverse shadow DOM, etc.) that
reveals a 6-character code. Enter the code to advance.

**30/30 steps. ~3:25. $0.00 API cost. All deterministic.**

## Quick Start

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # add your ANTHROPIC_API_KEY (only needed for vision fallback)
python solve.py --headed
```

## Run Statistics

| Version | Steps | Time | API Calls | Tokens | Cost |
|---------|-------|------|-----------|--------|------|
| v=1 | 30/30 | 3:40 | 0 | 0 | $0.00 |
| v=2 | 30/30 | 3:49 | 0 | 0 | $0.00 |
| v=3 | 30/30 | 3:23 | 0 | 0 | $0.00 |

The site randomizes across 3 versions per session. Each version shuffles challenge types
and parameters. The solver handles all three.

## What It Does

A 4-phase pipeline runs for each of 30 steps:

1. **V4 Agents** (Phase 1) — 27 deterministic challenge agents, one per type. Zero API cost, sub-second. Handles click, scroll, hover, decode, drag-drop, gesture, audio, video, shadow DOM, WebSocket, service worker, and 16 more.
2. **Passive Checks** (Phase 2) — Mutation observers, DNA clustering, code scoring. Zero cost.
3. **Vision Sidecar** (Phase 3) — Claude Sonnet 4.5 screenshot analysis as universal fallback. Currently unused (all 30 steps solved by Phase 1).
4. **Exception Handlers** (Phase 4) — Three labeled exceptions for intentionally broken demo challenges.

Detection uses a 3-layer cascade: DOM structure → semantic roles → instruction text matching.

## System Architecture

24,000 lines across 60 files. The key subsystems:

```
solve.py → orchestrator.py (1,400 lines)
              │
              ├── Phase 1: V4 Agents
              │     ├── 27 challenge agents (agents/v4/challenges/)
              │     ├── 5 universal agents (universal.py)
              │     ├── helpers.py (1,200 lines) — shared interaction primitives
              │     │     JS click, shadow root traversal, CSSOM hover scanning,
              │     │     CDP event listener detection, dimension-filtered targeting
              │     └── 3-layer detection cascade (DOM → semantic → text)
              │
              ├── Phase 2: Passive Checks
              │     ├── code_scorer.py — 13-factor scoring (entropy, dictionary,
              │     │     frequency, recency, temporal stability, causality, style)
              │     ├── dna_reasoner.py — cluster DOM elements by computed CSS style,
              │     │     assemble codes from character-shaped clusters
              │     └── init_hooks.py — MutationObserver + WebSocket interceptors
              │         injected at page load, feed codes to a shared bus
              │
              ├── Phase 3: Vision Sidecar (3,800 lines)
              │     ├── learning_sidecar.py — closed-loop controller
              │     │     Action execution with 4-signal progress measurement
              │     │     (DOM change ≥0.05, progress delta, new codes, clickable hash)
              │     │     Near-duplicate action detection, stall detection,
              │     │     frame-aware execution, provenance-scored candidates
              │     ├── vision_learning.py — Claude Sonnet 4.5 planner
              │     │     Screenshot + structured page state → proposed actions
              │     │     BID overlay for element grounding (not pixel coordinates)
              │     └── Promotion pipeline: build candidate during run →
              │           finalize after confirmed advance → 3 guard checks
              │           (causal actions, strong assertions, stable locators)
              │
              └── Support
                    ├── primitives.py (2,300 lines) — canvas drawing, drag sequences,
                    │     split-parts assembly, iframe/shadow enumeration,
                    │     state change classification, interactive container discovery
                    ├── knowledge_reader.py (2,300 lines) — canonical learnings,
                    │     Wilson score confidence, DNA signatures, 6-channel detection
                    └── popup.py — two-layer defense: JS auto-dismiss interval (400ms)
                          + Playwright locator handler with React 18 compatibility
```

## Design Decisions That Matter

**$0.60 → $0.00**: Built the AI vision system first (Phase 3), used it to discover how each challenge works, then systematically replaced every API call with a deterministic agent. 27 challenge types, zero AI cost, same 100% accuracy.

**Strict invariants**: Never modify DOM. Never dispatch synthetic events. Never `page.goto()` to step URLs. Production-quality discipline applied to a puzzle challenge — because these rules make the solver work on *any* website, not just this one (see [MISSION.md](MISSION.md)).

**Observability-first**: Built diagnostic tools (`diagnose.py`, `compare.py`, `scope_check.py`) to debug the automation itself. When an agent fails, you can see exactly what it saw and what it did wrong.

**Generalizable**: Every technique works on arbitrary websites. React-compatible input filling, Playwright-native drag-and-drop, accessible shadow DOM traversal — no site-specific hacks.

## How This Was Built

This project was built across 29 sessions (~45 hours) using Claude Code as the implementation partner in a deliberate human-AI systems engineering workflow.

**Division of labor.** The human defined the architecture (V1 → V2 → V3 → V4 pipeline evolution), wrote the decision framework ([MISSION.md](MISSION.md)), set the constraints (never modify DOM, never synthetic dispatch, every technique must work on arbitrary websites), and designed the diagnostic methodology. Claude Code wrote the Python implementation, debugged agent failures, iterated on fixes, and executed experiments. The human decided *what* to build and *why*. Claude Code figured out *how* and ground through the iteration cycles.

**The Coverage Engineer workflow.** The core development loop is a flywheel that compounds through a shared primitives layer. The Coverage Engineer improves primitives (helpers for clicking, hovering, dragging, traversing shadow DOM, etc.), which makes the Vision Sidecar's exploration more capable. The sidecar — Claude Sonnet 4.5 taking screenshots and reasoning about challenges — discovers how to solve new challenge types using those primitives. The CE studies those discoveries and feeds the insights back into better primitives, making the next round of exploration richer. V4 agents are the one-directional output of this loop: once a challenge type is well-understood, the pair crystallizes the solution into a deterministic agent (10-50 lines of Python, zero API cost, sub-second). This is how 27 agents were built. The sidecar went from solving 20+ steps per run to solving zero — replaced by agents extracted from knowledge it helped generate.

**Key human decisions that shaped the project:**
- Killed the recipe replay system (Session 22) after recognizing it had become over-engineered liability — 290 lines of tier management, TTL decay, and routing mismatch detection that the V4 agents made unnecessary
- Wrote [MISSION.md](MISSION.md) to prevent the solver from drifting into site-specific hacks — every technique had to be generalizable. This constraint caught 6 violations when re-examined with fresh eyes in Session 22b
- Designed the diagnostic tooling workflow (`diagnose.py`, `compare.py`, `scope_check.py`) so that agent failures could be debugged from data instead of guesswork — Claude Code then implemented and iterated on these tools
- Established the "fresh session" methodology: when stuck for 2+ iterations on the same bug, start a new Claude Code session with only the diagnosis data (not the failed attempts). This broke several multi-session dead ends where context accumulation was causing tunnel vision

**What this demonstrates.** A methodology for building complex systems with AI as an implementation partner: human sets architecture and constraints, AI handles volume and iteration, diagnostic tooling keeps both honest, and persistent artifacts ([FAILURES.md](FAILURES.md), [COVERAGE_ENGINEER.md](docs/COVERAGE_ENGINEER.md)) compound knowledge across sessions. The result — $0.60/run → $0.00/run with the same 100% accuracy — came from the human recognizing when to change strategy and AI executing the changes at scale.

## What I'd Build Next

If this were production: parallel step execution across browser pools, challenge fingerprinting for sub-second routing, CI/CD with regression detection, self-healing agents that learn from FAILURES.md automatically.

## Architecture Deep Dives

| Doc | What It Covers |
|-----|----------------|
| [JOURNEY.md](JOURNEY.md) | V1 → V2 → V3 → V4 design evolution, what failed and why |
| [MISSION.md](MISSION.md) | Decision framework: what's human-like, labeled exceptions, framework compatibility |
| [COVERAGE_ENGINEER.md](docs/COVERAGE_ENGINEER.md) | Dev-time iteration role: diagnostic toolbox, outside research, multi-model approach |
| [FAILURES.md](FAILURES.md) | Persistent failure knowledge base — every known issue with root cause |

## Diagnostic Tools

```bash
python diagnose.py --step 18 --headed              # Full telemetry dump for one step
python diagnose.py --step 18 --headed --screenshots # Screenshots at progress milestones
python compare.py --step 18 --headed                # Agent vs sidecar action diff
python scope_check.py --headed                      # Boundary validation for all 30 steps
```
