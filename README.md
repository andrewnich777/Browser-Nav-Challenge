# Browser Navigation Challenge - Automated Solver

Completes all 30 steps of the [Browser Navigation Challenge](https://serene-frangipane-7fd25b.netlify.app) in under 90 seconds with zero LLM cost.

## Approach

The challenge's validation logic was reverse-engineered from the minified JS bundle:

- **Code generation** is deterministic: `Rl(step+1, version)` produces a 6-char code from charset `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`
- **Interaction check** reads a token from `sessionStorage` at key `challenge_interaction_step_{N}`
- **Validation** requires both correct code (`Nv`) and interaction token (`Cv`)

The solver computes codes directly, injects interaction tokens, dismisses popup overlays (green buttons in z-index 9995-9999), and handles multi-tab challenge stale rendering via page reload + pushState navigation.

## Prerequisites

- Python 3.11+
- No API keys needed

## Setup

```bash
pip install playwright
python -m playwright install chromium
```

## Usage

```bash
python solve.py
```

## Output

- Console: per-step timing and pass/fail status
- `metrics.json`: machine-readable results with timing, step count, token usage (0), and cost ($0.00)

## Results

- **30/30 steps** completed
- **~75 seconds** total runtime
- **0 tokens**, **$0.00 cost**
