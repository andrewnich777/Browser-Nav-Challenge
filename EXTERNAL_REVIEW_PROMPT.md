# Browser Challenge Solver — Architecture Review Request

## What I Need From You

I've built a system that solves a 30-step browser navigation challenge automatically. It works — it solves all 30 steps every run. But the **recipe learning system** that's supposed to make it faster over time is unreliable. Recipes get promoted but then fail on replay at a ~50% rate when they should be hitting 90%+.

I want you to look at the architecture with fresh eyes and tell me what's fundamentally wrong. Not band-aid fixes — I've been doing those for weeks and it's a treadmill. What's the design-level issue?

---

## The Challenge

A website at a URL presents 30 sequential steps. Each step is a different UI challenge:
- Click a button to reveal a code
- Solve a math problem and submit
- Drag puzzle pieces into slots
- Press keyboard shortcuts
- Hover over an element for 2 seconds
- Scroll to find hidden content
- Click a timed "Capture" button that appears briefly
- Listen to audio / watch video for a code
- Navigate shadow DOM / iframes
- Register a service worker and retrieve from cache
- Multi-step sequences (click, then hover, then type, then scroll)

Each step reveals a 6-character alphanumeric code (charset: `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`). You enter the code to advance to the next step.

The website has **3 versions** (v1, v2, v3) assigned randomly per session. Each version shuffles which challenge type appears at which step number, but the set of ~20 challenge types is the same.

**Constraints**: It's a React SPA. You can only navigate to `/`. Never click `<a>` tags (404). Never modify the DOM or React state (white screen crash). Read-only JavaScript only.

---

## The Architecture

Built with Python + Playwright (browser automation) + Claude Vision API (for the AI reasoning).

### The Pipeline (per step)

```
Phase 1: Recipe Replay (System 1) — ~0.5-5s, $0
    Look up a stored recipe for this challenge type.
    Execute the action sequence (click, type, scroll, etc.).
    Check assertions (did a code appear? did progress increase?).
    If assertions pass → extract code → submit → done.
    If assertions fail → abort, fall through to Phase 2.

Phase 2: Passive Checks — ~0.1s, $0
    Check DOM mutation observers, WebSocket interceptors, DNA clustering.
    If a code is visible → submit → done.
    Otherwise → fall through to Phase 3.

Phase 3: Vision AI Sidecar (System 2) — ~15-30s, ~$0.02/call
    Take a screenshot of the page.
    Send to Claude Vision API with the screenshot + action history.
    AI proposes actions (click coordinates, type text, etc.).
    Execute the actions via Playwright.
    Check if a code appeared.
    If code found → submit → if accepted → PROMOTE recipe → done.
    If code rejected → tell AI about rejection → re-invoke (up to 3x).

Phase 3.5: Fast Twitch — ~5-15s, $0
    For timing challenges: wait for a button to appear, click instantly.
    (AI is too slow for sub-second timing windows.)

Phase 4+: Specialized fallbacks for broken challenge types.
```

### The Recipe System (where the problems are)

**Promotion**: When the AI sidecar (Phase 3) successfully solves a step, it builds a "recipe" from the actions it took and stores it. Next time the same challenge type appears, Phase 1 replays the recipe instantly.

**Recipe structure**: A recipe is a list of action steps:
```json
[
  {"action_type": "click", "target_text": "Reveal Code", "target_coords": [176, 303], "expect_code_visible": true}
]
```

Each step has a **locator cascade** for finding the target:
1. ARIA role + accessible name
2. Text content match (`page.get_by_text(target_text, exact=False)`)
3. CSS selector
4. DNA query (computed style fingerprint matching)
5. Raw pixel coordinates (last resort)

Each step has optional **assertions** that must pass:
- `expect_code_visible`: Poll for 6 seconds — is a valid 6-char code on the page?
- `expect_progress_delta`: Did the progress bar advance?
- `expect_dom_change_score`: Did the DOM change meaningfully?
- `expect_state_changes`: Did elements change state (green, enabled, etc.)?

If ANY assertion fails on ANY step, the entire recipe aborts → falls through to the AI sidecar.

**Tier system**:
- Tier 1: Just promoted, unproven. TTL=5 (decrement on failure, delete at 0).
- Tier 2: 1-2 replay successes. More trusted.
- Tier 3: 3+ replay successes. Fully proven.

**Detection**: Recipes are stored under a challenge type key like `timing_v3`. To match a recipe to a step, the system:
1. Checks a config table (steps 16-30 have known types).
2. For steps 1-15 (all return "simple"), runs keyword detection against page text.
3. Keyword detection uses weighted regex patterns + stored detection keywords.
4. Minimum match score threshold: 0.12.

**Auto-demotion**: Recipes with 2+ consecutive failures and 0 lifetime successes get permanently deleted.

---

## Current Recipe Stats (30 recipes)

**Tier 3 (proven, reliable)**:
| Recipe | Replay Rate | Steps |
|--------|------------|-------|
| click_reveal_v1 | 13/13 (100%) | click "Reveal Code" |
| click_reveal_v2 | 4/4 (100%) | click "Reveal Code" |
| delay_memory_v2 | 10/10 (100%) | click "I Remember - Reveal Real" + click again |
| delay_memory_v3 | 9/9 (100%) | same |
| hidden_dom_v1 | 6/6 (100%) | repeat_click "Hidden DOM Challenge" x3 |
| hover_v1 | 6/6 (100%) | click + hover "Hover here to reveal code" |
| scroll_v1 | 10/11 (91%) | scroll down:3000 |
| timing_v3 | 7/7 (100%) | click "Capture" x2, click "Capture" again |
| keyboard_sequence_v2 | 10/11 (91%) | press Ctrl+V |
| keyboard_sequence_v3 | 7/10 (70%) | press Tab |
| keyboard_sequence_v1 | 7/9 (78%) | press Ctrl+V |

**Tier 2 (promising)**:
| Recipe | Replay Rate |
|--------|------------|
| click_reveal_v3 | 2/3 (67%) |
| delay_memory_v1 | 2/3 (67%) |
| scroll_v2 | 2/2 (100%) |
| multi_tab_v3 | 2/2 (100%) |
| delayed_reveal_v1 | 5/6 (83%) |

**Tier 1 (unproven/new)**: 14 recipes, most with 0-1 replay attempts.

**Recently deleted (0% replay success)**: calculated_v3, decode_v3, drag_drop_v3, hidden_dom_v3, puzzle_solve_v3, service_worker_v3.

---

## The Recurring Problems

### 1. Promoted recipes fail on replay (~50% overall hit rate)

The AI sidecar solves a step, the system extracts a recipe from its action log, stores it. Next run, the recipe replays... and fails. Common failure modes:

**a) Target not found**: The recipe stores `target_text: "Capture (0/3)"` but on replay the button says `"Capture"` or `"Capture (1/3)"`. The locator cascade fails because `get_by_text("Capture (0/3)")` doesn't match `"Capture"`. Falls back to coordinates, which work sometimes but are fragile.

**b) Wrong challenge matched**: Recipe keywords are too broad. A "scroll" recipe matches a "split_parts" challenge because both pages contain the word "scroll". The recipe executes the wrong actions, fails, wastes time.

**c) Resolver failures**: Some recipes use dynamic resolvers (e.g., `eval_expression` reads a math problem from the page and computes the answer). The resolver's regex doesn't match the page text format on replay.

**d) Assertion too strict / too slow**: The recipe's actions complete correctly but the 6-second code visibility poll times out. The challenge needed 8 seconds, or the code appears in a non-standard location (shadow DOM, iframe, etc.).

**e) Session-specific data baked into recipe**: Button text like "Tab 1: KA" (where "KA" is a session code), frame numbers like "Frame 44", counter states like "(0/3)".

### 2. Fast twitch (timing challenge handler) solves but doesn't save recipe

The fast twitch handler clicks a timed button, extracts the code, submits it. But the recipe promotion step fails silently. The `finalize_promotion` function has ~10 guards that can reject a recipe, and no logging when it does.

### 3. Recipe overwrites

When the AI sidecar re-solves a challenge that already has a working tier-3 recipe, `finalize_promotion` OVERWRITES the proven recipe with the new one. The new recipe may be worse. This can degrade a 100% recipe to 0%.

### 4. Detection cross-contamination

Steps 1-15 all report as "simple" from the config table, so keyword detection is the only way to match them. Multiple challenge types share keywords (e.g., "puzzle_solve" and "calculated" both have math-related words). A recipe can match the wrong challenge type.

### 5. The recipe promotion pipeline is Rube Goldberg

The path from "sidecar solved it" to "recipe stored and replayable" goes through:
1. `_build_promotion_candidate()` — extracts recipe from action log, detects challenge type
2. `_clean_recipe_steps()` — strips noise (popup clicks, coord-only noise, session data)
3. `compress_recipe()` — merges repeated actions
4. `_lint_recipe()` — scores replayability, blocks low scores
5. `finalize_promotion()` — 10+ guards (generic type, empty recipe, missing fields, single-completion-click, etc.)
6. Keyword generation
7. Existing recipe update vs. new creation

Any step can silently fail. There's minimal logging between steps. When a recipe "doesn't save", debugging requires reading through 200+ lines of promotion code to find which guard rejected it.

---

## What Works Well

- **Simple recipes with text-based targeting**: "click Reveal Code", "hover 'Hover here to reveal code'", "press Ctrl+V" — these have 90-100% replay rates.
- **The overall system**: It solves 30/30 every run, even when recipes fail (the AI sidecar catches everything).
- **Tier system**: Proven recipes (tier 3) rarely break. The problem is getting new recipes FROM tier 1 TO tier 3.

## What Doesn't Work

- **Complex multi-step recipes**: More than 2 steps → reliability drops sharply.
- **Recipes with dynamic content**: Math answers, counter states, session labels.
- **Detection/matching**: Too many false positives and false negatives.
- **Promotion quality**: The sidecar's action log is noisy. The cleanup pipeline tries to strip noise but is brittle and loses important context.

---

## My Questions

1. **Is the overall architecture sound?** Having an AI solve things, then extracting replayable recipes from its actions — is this fundamentally a good approach? Or is there a better pattern?

2. **What's wrong with the recipe promotion pipeline?** Why does extracting a recipe from an AI's action log produce unreliable recipes ~50% of the time?

3. **How should detection/matching work?** Using keywords extracted from page text to match stored recipes seems fragile. What's a better approach for "given this page, which stored recipe should I replay?"

4. **Should recipes even store target_text/coords?** Maybe recipes should be more abstract (like "click the main action button" rather than "click the element containing 'Reveal Code' at coords [176, 303]")?

5. **How do you handle the session-specific data problem?** Every run has different math problems, different tab labels, different counter states. How do you make recipes that work across sessions?

6. **Is there a simpler architecture** that would get 90%+ recipe replay rates without the current complexity?

---

## Technical Details (if needed)

- **Language**: Python 3.14 + Playwright 1.57 (Chromium)
- **AI**: Claude Sonnet 4.5 via Anthropic API (vision/screenshots)
- **Storage**: Single `learnings.json` file with all recipes
- **Run cost**: ~$0.50-1.20 per 30-step run (mostly vision API calls)
- **Run time**: ~600-800s per run (mostly AI round-trips)
- **Target**: Recipe replay rate ≥90%, minimizing AI calls and run time
