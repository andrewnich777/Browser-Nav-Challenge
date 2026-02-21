# Mission Statement

> Build a general-purpose computer-use agent that solves browser challenges by
> interacting with web pages the way a human would. Every technique must work on
> an arbitrary website we've never seen.

## Decision Framework

When choosing between two approaches, prefer the one that:

1. **Works on any website** — not just this challenge site
2. **Uses visible information** — what a human can see on screen
3. **Interacts through standard UI** — clicks, hovers, types, scrolls
4. **Falls back to Vision API** — screenshot analysis as the universal fallback

## Labeled Exceptions

Three challenges have intentionally broken UI. Each is a "demo" challenge — the
agent solves the challenge normally through standard UI until hitting the final
button, and only when that button doesn't work does it fall back to the exception.

| Exception | Where | Behavior |
|-----------|-------|----------|
| **recursive_iframe** — broken "Extract Code" button | `recursive_iframe.py` | Agent clicks through iframe levels 1-5 normally, then clicks "Extract Code". The button is intentionally non-functional. **Fallback**: fiber bypass (invoke React onClick via fiber tree) attempts to extract the code; if the handler produces nothing (confirmed: `state_before == state_after`), reads code from `sessionStorage["wo_session"]` (XOR+base64 encoded JSON containing all 30 session codes). |
| **Step 30** — no code exists | `agents/v4/final_step_hook.py` (primary), orchestrator Phase 4 (safety net) | The regular challenge agent (shadow_dom, websocket, etc.) runs normally — the **final step hook** (`wrap_final_step`) wraps it. When the agent returns no code (expected — no code exists for step 30), the hook pushes `history.pushState` to `/finish`. Not a separate agent type — an add-on around whatever agent runs. |
| **Calculated** — arrives bugged | `calculated.py` | Second sequential math puzzle (always follows puzzle_solve). Page arrives with bugged rendering. **Fallback**: client-side React Router refresh via `pushState` to `/` (16ms fast dwell + 200ms fallback) then back to `/stepN`, triggering React unmount/remount. Then normal solve logic runs on fresh page. |

Everything else must work through visible UI + Vision API fallback.

## Framework Compatibility Patterns

Some techniques are framework-specific but **generalizable** — they work on any
website built with that framework (40%+ of the web for React). These are NOT
violations; they're adaptations to how modern web frameworks work.

| Pattern | What It Does | Why It's General |
|---------|-------------|-----------------|
| `nativeInputValueSetter` | Sets input value via `HTMLInputElement.prototype.value.set` + dispatches `input`/`change` events | React controlled components ignore `el.value =` assignments. This pattern works on **any** React app, not just this site. Same category as using `locator.click()` instead of raw mouse events. |
| `el.click()` (JS) | Calls the standard DOM `.click()` method | Fires through the browser's normal event system. Works on any website. Used as Tier 2 fallback when Playwright locator clicks fail. |

**The distinction**: Fiber walking is React-specific AND site-specific (reads internal
data structures). `nativeInputValueSetter` is React-specific but generalizable (uses
the standard property descriptor API to interact with controlled inputs the way the
framework expects).

## What "Human-Like" Means

- **See**: Read visible text, observe layout, notice visual changes
- **Click**: Press buttons, links, and interactive elements
- **Type**: Enter text into inputs and forms
- **Hover**: Move the mouse over elements and observe reactions
- **Scroll**: Scroll to reveal content
- **Wait**: Watch for animations, timers, and delayed content
- **Reason**: Solve puzzles, decode text, follow instructions

## What "Human-Like" Does NOT Mean

- Walking React fiber trees to find hidden onClick handlers
- Using CDP to force CSS pseudo-states
- Scanning invisible DOM attributes (data-*, aria-label) for codes
- Intercepting network traffic
- Modifying the DOM or dispatching synthetic events
- Using `history.pushState` to navigate (except labeled step 30 exception)
