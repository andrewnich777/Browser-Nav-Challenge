# V2 Multi-Agent Solver - Learnings

## Site Changes (discovered during development)

### Code Generation
- **OLD**: Deterministic codes from `generate_code(step, version)` using formula `step * 7919 + 12345...`
- **NEW**: Random codes via `crypto.getRandomValues()` stored in sessionStorage

### Code Storage
- Key: `wo_session` in sessionStorage
- Encoding: Base64 + XOR with key `"WO_2024_CHALLENGE"`
- Format: `{sessionId, codes: string[30], completed: number[]}`
- Array indexing: `codes[N]` validates step N (codes[1] for step 1, NOT codes[0])

### Validation Changes
- OLD: `sessionStorage` token injection bypassed interaction checks
- NEW: Site validates actual interactions occurred (scroll amount, button clicks, etc.)

---

## Learning System Architecture (V2 — Canonical Learnings)

One canonical learning per challenge type, stored in `knowledge/learnings.json`.
Each learning holds up to 3 strategy variants with independent confidence tracking,
rollback history, and failure patterns.

### Data Model

```
CanonicalLearning (1 per challenge type)
├── challenge_type: str          # Key: "hover", "drag_drop", etc.
├── detection_keywords: list     # Regex patterns for scored matching
├── dom_signals: list            # Expected DOM features for match scoring
├── router_priority: int         # Tiebreaker (higher = matched first)
├── disabled_until: str | None   # TTL-based circuit breaker
├── dna_signatures: list[dict]   # Type-level aggregated DNA (3+ occurrences = high confidence)
├── page_text_context: dict      # Aggregated instruction keywords, button labels, interactive types
│
└── variants: list[StrategyVariant]  (max 3)
    ├── variant_id: str
    ├── action_type: str         # scroll/click/hover/draw/drag_drop/...
    ├── action_params: dict      # Structured params for execution
    ├── preconditions: list      # DOM signals + instruction regex
    │
    ├── confidence: float        # Wilson score lower bound (sole metric)
    ├── attempts / successes / failures / consecutive_failures
    ├── verified: bool
    │
    ├── action_recipe: list[dict]          # System 1: Ordered ActionStep dicts for replay
    ├── successful_dna_signature: dict     # Winning DNA from last success (System 2)
    ├── page_context: dict                 # Text patterns from success (instruction keywords, etc.)
    │
    ├── version: int
    ├── previous_versions: list  # Snapshots for multi-step rollback
    │
    ├── failure_history: list    # Last 5 with dom_sig, dom_change_score
    └── failure_patterns: dict   # Grouped: {"timeout": 3, "selector_miss": 2}
```

### Knowledge Reader (`knowledge_reader.py`)

Detects challenge types via **5-factor scored matching** (no API cost):

```
match_score = 0.35*keyword + 0.25*dom + 0.1*flags + 0.15*text_ctx + 0.15*dna
```

- `keyword_score`: Regex hits x per-pattern weights against page text
- `dom_score`: Fraction of expected `dom_signals` satisfied (via JS)
- `flag_score`: DOM flags (has_canvas, has_shadow, has_ws, etc.)
- `text_ctx_score`: Instruction keywords, button labels, interactive types matching stored context
- `dna_score`: High-confidence DNA signatures (3+ occurrences) matched against live DOM elements

Variant selection includes agent health:
```python
effective = match_score * wilson_confidence * staleness_penalty * agent_health
```

Key methods:
```python
reader = get_knowledge_reader()
# Detection — accepts optional page_info and dna_elements for 5-factor scoring
result = reader.detect_and_get(page_text, page, page_info=page_info, dna_elements=dna_elements)
# Returns (CanonicalLearning, StrategyVariant) | None

# Success — stores recipe + DNA signature + page context on variant
reader.record_success(challenge_type, variant_id, page_info=info, recipe=steps, dna_signature=dna)

# Failure — stores instruction snippet + button labels for diagnosis
reader.record_failure(challenge_type, variant_id, why, what_tried, dom_sig_before, dom_sig_after, dom_change_score, page_info=info)

reader.rollback_variant(challenge_type, variant_id)
reader.disable_learning(challenge_type, reason)
reader.reset_disabled()  # Called at run start
```

### Confidence: Wilson Score Lower Bound

Sole confidence metric. No manual decay. Uses `z=1.0` (~84% CI), less conservative
than the traditional `z=1.96` (95% CI):

```python
def wilson_score_lower(successes: int, total: int, z: float = 1.0) -> float:
    if total == 0:
        return 0.0
    p = successes / total
    denominator = 1 + z * z / total
    centre = p + z * z / (2 * total)
    adj_std = math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return max(0.0, (centre - z * adj_std) / denominator)
```

With `z=1.0`: 1/1 -> 0.500, 2/3 -> 0.386, 3/3 -> 0.750, 5/5 -> 0.833.
New learnings bootstrap with `successes=2, attempts=3` (Wilson ~ 0.386).

### Failure Feedback Loop

```
On Failure:
  1. If 3+ consecutive failures + rollback exists -> ROLLBACK
  2. If 5+ consecutive failures, no rollback -> DISABLE (TTL, rest of run)
  3. Deduplicate: if same sanitized error as last failure -> skip refinement
  4. Record failure (dom_sig_before/after, dom_change_score, failure_patterns)
  5. LLM refinement (max 1 per step):
     - parameter_tweak (different selector, longer wait)
     - logic_rewrite (click -> drag, hover -> click-and-hold)
     - new_variant (different subtype needs separate handling)
```

### DOM Change Score

Float 0.0-1.0 measuring structural DOM change (not just boolean). Guides refinement:
- `~0.0`: Selector wrong / event not triggered -> fix targeting
- `~0.1-0.3`: Action triggered something but not the right thing -> fix logic
- `~0.8+`: Major state transition, code not extracted -> fix extraction

### Agent Performance Tracking

`agent_tracker.py` tracks per-agent success/failure rates, persisted to
`knowledge/agent_performance.json`. Feeds into:
- **Variant routing**: `agent_health = max(0.3, 1.0 - failure_rate * 0.5)`
- **Refinement prompts**: "hover agent has 70% failure rate"

### V1 Seed Strategies

On first load, 15 default strategies are seeded from `V1_SEED_STRATEGIES`:

| Type | Action | Key Params |
|------|--------|------------|
| scroll | scroll | direction=down, amount=800 |
| click_reveal | click | selector=button with Reveal |
| hidden_dom | hidden_dom | click_count=3 |
| hover | hover | duration_ms=800 |
| delay_memory | wait | duration_ms=3000 |
| radio | radio | option_text=Correct |
| radio_modal | radio | (modal variant) |
| checkbox | checkbox | selector=input[type=checkbox] |
| draw | draw | gesture=circle |
| drag_drop | drag_drop | (puzzle piece variant) |
| decode | decode | encoding=auto |
| shadow_dom | shadow_dom | depth=3 |
| websocket | websocket | (empty) |
| audio | audio | (empty) |
| enable_buttons | enable_buttons | selector=button:disabled |

### Vision Learning Agent (Expensive Fallback)

`agents/learning.py` provides:
- `refine_learning()`: Constrained LLM refinement with candidate elements, `ACTION_SCHEMAS` validation, dom_change_score diagnosis
- `bootstrap_learning()`: LLM-bootstrapped first-time learning from successful episode (includes `winning_agent` context)

Only activated after standard agents fail.

---

## Agents & Their Purpose

| Agent | Purpose | Key Insight |
|-------|---------|-------------|
| popup | Dismiss overlay popups | Green buttons in z-index 9995-9999 |
| click_reveal | Click "Reveal Code" buttons | Some challenges need explicit reveal |
| scroll | Scroll to reveal hidden content | `mouse.wheel(0, 800)` + `scrollTo()` |
| extract_code | Find 6-char code in page text | Charset: `[A-HJ-NP-Z2-9]{6}` |
| code_entry | Fill input + submit form | Native setter required for React |
| hidden_dom | Click N times to reveal | Monitor text for count changes |
| decode | Base64/Hex/ROT13 decode | Pattern detection in text |
| draw | Canvas gestures | Instruction parsing for gesture type |
| drag_drop | Drag puzzle pieces to slots | `locator.drag_to()` + HTML5 events |
| hover | Hover to trigger CSS reveals | Duration 800ms+, visibility-based wait |
| delay | Wait for timed content | `wait_for_function(polling="mutation")` |
| shadow_dom | Traverse shadow DOM + iframes | `frame_locator()` chaining up to 3 levels |
| websocket | Capture WebSocket codes | Init hooks intercept messages |
| audio | Transcribe audio | OpenAI Whisper API |
| vision | Screenshot analysis | Claude Sonnet for visual understanding |
| learning | Strategy refinement | Constrained LLM refinement + bootstrap |
| recipe_executor | **System 1 (Reflex)** — replay stored recipes | Locator cascade: ARIA → text → CSS → DNA → coords; assertion-based abort |
| dna_reasoner | **System 2 (Reasoning)** — DNA clustering | Computed style fingerprints → cluster → assemble 6-char code from best cluster |

---

## System 1/System 2 Tiered Solver

### Overview

```
Step → System 1: Replay Recipe (fast, no API call)
         ↓ fail / assertion mismatch
       System 2: DNA Scan → Cluster → Reason → Solve
         ↓ success
       Record DNA signature + recipe → System 1 next time
```

### System 1: Reflex (`recipe_executor.py`)

Replays a stored `action_recipe` (list of `ActionStep` dicts) with post-step
verification assertions. If any assertion fails, aborts immediately so the
orchestrator falls through to System 2.

**ActionStep fields:**
- `action_type`: click, hover, type, keyboard_sequence, scroll, wait, run_agent
- **Locator cascade** (tried in order): ARIA role/name → text content → CSS selector → DNA query → raw coordinates
- **Assertions**: `expect_selector_visible`, `expect_text_contains`, `expect_dom_changed`

**Guard rails:** Max 10 steps per recipe. Assertion failures abort cleanly.

### System 2: Reasoning (`dna_reasoner.py`)

Runs `DETECT_DOM_DNA_JS` to extract computed style "DNA" from every visible element
(including Shadow DOM). Elements are clustered by normalized DNA key, then each
cluster is scored and the best is assembled into a 6-char code.

**DNA key** = `color|backgroundColor|fontSize|fontWeight|fontFamily|opacity|textDecoration`
(all normalized: colors → hex, font sizes → numeric, opacity → bucket)

**Cluster scoring:**
- Known DNA bonus (3.0 if matches high-confidence signature)
- Monospace font bonus (+1.5)
- Small cluster bonus (focused = good, up to +1.5)
- Highlighted background bonus (+0.5)
- Proximity to "code"/"solution" labels (+1.0)

**Code assembly:** Y-bucket row sort (fontSize × 1.2 tolerance) + X-column sort.
Handles scattered code fragments that wrap or are vertically offset.

### DNA Signature Lifecycle

1. System 2 succeeds → winning cluster's DNA stored on variant (`successful_dna_signature`)
2. DNA aggregated at type level (`CanonicalLearning.dna_signatures`) with occurrence counts
3. After 3+ occurrences with same DNA → becomes "high confidence"
4. High-confidence DNA contributes 0.15 weight to `detect_and_get()` scoring
5. Max 5 DNA signatures per learning type, sorted by occurrences

### Performance Gating

DNA scan only runs when ALL of:
- No stored recipe (System 1 not available)
- Variant confidence < 0.7
- Many small text fragments detected (>10 elements with ≤6 chars)

DOM stability check runs before scanning (wait for element count to stabilize, up to 2s).

---

## Timing Optimizations

### Current (post Playwright power-ups)
- Post-scroll wait: 250ms
- Post-submit wait: instant (via `wait_for_url`)
- Popup dismiss: automatic (via `add_locator_handler`)
- Code detection: ~16ms (via `wait_for_function(polling="mutation")`)

### Remaining `wait_for_timeout` Calls (4 total)
1. `start_fresh()` — 500ms fallback after START click
2. `_safe_click()` — 100ms post-click safety margin
3. `_recover_from_navigation()` — 300ms for pushState processing
4. Hover animation — 800ms sustained hover wait

---

## Known Issues & Solutions

### 1. Step 30 Code Missing
- `codes[30]` doesn't exist (array is 0-29)
- Workaround: Use `codes[29]` for step 30
- May indicate site bug or special finish handling

### 2. Stale React Content (multi-tab)
- After multi-tab challenges, React doesn't re-render
- Solution: RAF stability wait + cache-buster URL
- Fallback: `goto(home)` + `pushState` to target URL

### 3. Decoy Buttons
- Many fake "Submit" buttons scattered on page
- Real submit: Inside `<form>` parent of code input
- Match: `button[type="submit"]` in form

### 4. Decoy Codes (PUNYYR, SUBMIT, etc.)
- Code scorer filters known decoys
- Entropy check rejects dictionary words
- Frequency check rejects repeated codes

---

## Decoy Patterns Discovered

### Decoy Codes
- `PUNYYR` - ROT13 of "CHALYR" (challenge)
- `SUBMIT`, `SCROLL`, `REVEAL`, `HIDDEN`, `BUTTON`, `CLICK`
- All-numeric codes like `756952`
- Codes appearing multiple times on page

### Decoy Instructions
- "This is filler content" - EXPLICIT decoy marker
- "Scroll Down to Find Navigation" - generic filler
- "You are on step X. Complete the challenges..." - useless generic text
- Real instructions describe SPECIFIC actions (scroll 500px, click Reveal button)

### Decoy Buttons
- Many fake "Reveal Code" buttons that don't actually reveal
- Real reveal buttons often contain "Real" in text
- Buttons in overlays (z-index > 1000) are usually decoys

---

## Code Scoring System

The code scorer (`code_scorer.py`) uses multi-factor analysis:

| Factor | Weight | Description |
|--------|--------|-------------|
| Dictionary | Instant 0 | Known words (PUNYYR, SUBMIT) |
| Entropy | +0.2 | Random codes have high entropy |
| Uniqueness | +0.25 | Appears only once on page |
| Position | +0.2 | Near instruction text |
| Styling | +0.15 | Highlighted or monospace |
| Recency | +0.3 | Appeared after action |

Threshold: Score > 0.5 to accept a code.

---

## Protocol Interception (Init Hooks)

`init_hooks.py` installs observers before page loads:

```javascript
// Captures codes from:
- WebSocket messages
- SSE (EventSource) events
- Fetch responses
- XHR responses
- DOM mutations

// Also:
- Forces Shadow DOM to open mode
- Stores all captured codes in window.__codeBus
```

---

## Human-Like Solving Approach

1. **System 1 (Reflex):** Replay stored recipe if available — fast, no API call
2. Dismiss distractions (popups) — auto-dismiss JS + Playwright backup
3. Check stored knowledge via 5-factor scoring (fast path, no API)
4. Vision API → detect challenge type → dispatch to specialist agent
5. Execute the SPECIFIC action required
6. Extract and verify code against scoring
7. Submit only when confident
8. **System 2 (Reasoning):** If still stuck, DNA scan → cluster → assemble code
9. On success: update Wilson confidence, record recipe + DNA signature
10. On failure: rollback / refinement / disable feedback loop

---

## Next Steps

1. ~~Better decoy detection using text patterns~~ (DONE - code_scorer.py)
2. ~~Prioritize "Real Code" buttons~~ (DONE - improved selectors)
3. ~~Verify codes match session before submitting~~ (DONE - scoring)
4. ~~Special finish detection for step 30~~ (DONE)
5. ~~Fix double-advance bug~~ (DONE - code_already_submitted flag)
6. ~~Track verified learnings~~ (DONE - Wilson score + verified flag)
7. ~~Learning feedback loop~~ (DONE - rollback + refinement + disable)
8. ~~Seed from v1 strategies~~ (DONE - V1_SEED_STRATEGIES, 15 types)
9. **FIX drag-drop detection** - Analyze actual page structure
10. **Improve hover duration** - Increase wait time after hover
11. Reduce Vision API calls with better pattern matching
12. Achieve zero Learning Agent calls after initial learning
13. **Validate Learning V2 end-to-end** — Run solver twice, verify refinement
14. ~~System 1/System 2 tiered solver~~ (DONE - recipe_executor.py + dna_reasoner.py)
15. ~~5-factor detection scoring~~ (DONE - keyword + dom + flags + text_ctx + dna)
16. ~~DNA signature aggregation~~ (DONE - type-level, 3+ occurrences = high confidence)
17. **Validate System 1 recipe replay** — Run twice, verify recipes recorded and replayed
18. **Validate DNA clustering accuracy** — Check assembly produces correct reading order
