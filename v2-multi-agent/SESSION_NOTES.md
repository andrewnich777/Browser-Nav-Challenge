# Browser Challenge Solver - Session Notes

## Overview
Building an automated browser challenge solver that works **like a human would** - using Claude Vision API to visually identify challenges and extract codes without cheating (no session storage bypass).

## Architecture
- **Multi-agent system** with specialized agents for different challenge types
- **Vision API** (Claude Sonnet) for screenshot analysis and challenge detection
- **Learning Agent** (Claude Opus 4.5) for failure analysis and pattern learning
- **Knowledge Reader** for accessing stored learnings WITHOUT API calls
- **Playwright** for browser automation

## Key Files

### Core Files
| File | Purpose |
|------|---------|
| `solve.py` | CLI entry point |
| `orchestrator.py` | Main orchestration logic |
| `vision_client.py` | Claude Vision API client with token tracking |
| `knowledge_reader.py` | Direct access to learnings (NO API calls) |
| `code_scorer.py` | Multi-factor decoy detection |
| `init_hooks.py` | Browser protocol interception |

### Agent Files (`agents/`)
| Agent | Purpose |
|-------|---------|
| `vision.py` | Screenshot analysis via Claude Sonnet |
| `learning.py` | Opus 4.5 failure analysis and pattern extraction |
| `hidden_dom.py` | "Click X times to reveal" challenges |
| `decode.py` | Base64/Hex/ROT13 decoding |
| `click_reveal.py` | Click reveal buttons with force |
| `popup.py` | Dismiss blocking modals/overlays |
| `scroll.py` | Scroll to reveal hidden content |
| `draw.py` | Canvas gesture drawing |
| `delay.py` | Timed wait challenges |
| `extract_code.py` | Find 6-char codes |
| `code_entry.py` | Submit codes |

### Learning System (`learning/`)
| Module | Purpose |
|--------|---------|
| `observation.py` | StepObservation capture for every action |
| `memory.py` | Episode/Pattern/Strategy stores with SQLite |
| `patterns.py` | ChallengePattern definitions and built-ins |
| `retrieval.py` | Fast (<100ms) pattern matching |
| `execution.py` | Safe strategy code execution |
| `self_play.py` | Autonomous exploration when stuck |
| `feedback.py` | Wilson score confidence tracking |

---

## The Knowledge Reader: Key to Zero API Calls

The `knowledge_reader.py` module allows the orchestrator to use stored learnings WITHOUT calling the Learning Agent API:

```python
from knowledge_reader import get_knowledge_reader

# Get the global reader instance
reader = get_knowledge_reader()

# Check if we have knowledge for this step
if reader.has_knowledge_for(step, version):
    # Get the best learning (highest confidence)
    learning = reader.get_best_learning(step, version)

    # Convert to actionable plan
    plan = learning.to_action_plan()
    # plan = {
    #     'challenge_type': 'scroll_reveal',
    #     'action': 'scroll',
    #     'params': {'direction': 'down', 'amount': 500},
    #     'code': 'await page.mouse.wheel(0, 500)',
    #     'confidence': 0.85,
    #     'source': 'stored_learning'
    # }
```

### Integration in Orchestrator

The orchestrator checks stored knowledge BEFORE calling any API:

```python
# 1. FIRST: Check stored knowledge (FREE)
if self.knowledge_reader:
    stored_learning = self.knowledge_reader.get_best_learning(step, version)
    if stored_learning and stored_learning.confidence >= 0.5:
        log(f"Using STORED KNOWLEDGE (no API call)")
        # Execute the stored action plan
        success = self._execute_stored_action(page, stored_learning)
        if success:
            self.metrics['knowledge_used'] += 1
            return True

# 2. ONLY IF NO STORED KNOWLEDGE: Use expensive APIs
# ... vision calls, learning agent calls ...
```

---

## Challenges Solved

### 1. Scroll Reveal
- Vision detects "scroll down X px" instruction
- Scroll agent handles scrolling
- Code extracted after scroll

### 2. Delayed Reveal
- Vision detects "wait X seconds" instruction
- Delay agent waits and polls for code appearance

### 3. Click to Reveal
- Vision detects click action
- `click_reveal` agent with `force=True` bypasses overlays
- Extracts code immediately after clicking

### 4. Hidden DOM Challenge
- Detects "click here X times to reveal" pattern
- Uses JavaScript clicks (bypasses overlay interception)
- Monitors text for count changes

### 5. Decode Challenges
- Detects Base64/Hex/ROT13 encoded content
- Automatically decodes and extracts

### 6. Canvas/Gesture
- Parses instruction for gesture type
- Draws horizontal/vertical/circle on canvas

---

## Major Issues Encountered & Solutions

### 1. Overlay/Modal Blocking Clicks
**Problem**: "Please Select an Option" modal blocking interactions
**Solution**:
- `popup.py` aggressively dismisses modals
- Use `force=True` in Playwright clicks
- JavaScript `el.click()` ignores overlay interception

### 2. Wrong Element Clicked
**Problem**: Clicking wrong "Click Here" button (opened modal instead of revealing)
**Solution**:
- Find challenge container first
- Search for clickable elements WITHIN that container
- Skip `<button>` elements (usually open modals)
- Prefer `<u>`, `<a>`, `<span>` with underline styling

### 3. False Positive Code Extraction (PUNYYR)
**Problem**: Extracted decoy codes instead of real codes
**Solution**:
- `code_scorer.py` with multi-factor analysis
- Dictionary check for known decoys
- Entropy check for random-looking codes
- Frequency check for uniqueness

### 4. Blank Page After Navigation
**Problem**: React SPA doesn't re-render after navigation
**Solution**:
- RAF stability wait for React hydration
- Cache-buster URL parameter
- Nuclear fix with pushState as fallback

### 5. Vision Coordinates Wrong
**Problem**: Vision gave coordinates that didn't match actual button positions
**Solution**:
- Scale coordinates based on screenshot vs document height
- Scroll to target area, then convert to viewport coordinates
- Local element search with elementFromPoint

---

## Current Test Results

Run `python test_learning_system.py` to verify all components:

```
============================================================
Learning System Test Suite
============================================================
Testing imports...
  observation.py: OK
  memory.py: OK
  patterns.py: OK
  retrieval.py: OK
  execution.py: OK
  self_play.py: OK
  feedback.py: OK
Testing code scorer...
  PUNYYR: 0.00 (correctly rejected)
  SUBMIT: 0.00 (correctly rejected)
  ...
All tests PASSED!
```

---

## Code Patterns That Work

### JavaScript Click (Bypass Overlays)
```python
page.evaluate('() => { document.querySelector("[data-click-target]")?.click(); }')
```

### Force Click with Playwright
```python
page.locator('button:has-text("Reveal")').click(force=True, timeout=2000)
```

### Wait for React Content (RAF Stability)
```python
page.evaluate('''() => new Promise(resolve => {
    let last = 0, stable = 0;
    function tick() {
        const h = document.body?.innerText.length || 0;
        if (h === last) stable++;
        else stable = 0;
        last = h;
        if (stable >= 3) resolve(true);
        else requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
})''')
```

### Extract Code After Click
```python
# In click_reveal agent
code = self._extract_revealed_code(page)
if code:
    self._revealed_code = code  # Store for orchestrator
```

---

## Learning Agent Insights

The Opus 4.5 learning agent provides valuable analysis when needed:
- Identifies challenge type
- Explains why extraction failed
- Suggests specific actions
- Can flag need for new agents
- Saves learnings to `knowledge/learnings.jsonl`

**Goal**: After one successful run, learnings are stored and the Learning Agent is never called again.

---

## Metrics Tracking

The orchestrator tracks comprehensive metrics:

```python
metrics = {
    'steps_attempted': 0,
    'steps_succeeded': 0,
    'vision_calls': 0,
    'vision_tokens': 0,
    'learning_agent_calls': 0,
    'knowledge_used': 0,  # Times stored knowledge used (FREE)
    'total_time_ms': 0,
}
```

Check `metrics.json` after a run for detailed results.

---

## Commands

```bash
cd v2-multi-agent

# Run solver
python solve.py --headed  # Watch browser
python solve.py           # Headless mode
python solve.py --steps 5 # First 5 steps only

# Test learning system
python test_learning_system.py

# View results
cat metrics.json

# View stored learnings
cat knowledge/learnings.jsonl
```

---

## Files for Reference

- `knowledge/` - Screenshots and learnings
- `knowledge/learnings.jsonl` - Stored patterns (KEY FILE)
- `metrics.json` - Run results
- `.env` - API key (ANTHROPIC_API_KEY)

---

## Environment

- Python 3.11+ with Playwright
- Claude Vision API (Sonnet) for screenshots
- Claude Opus 4.5 for learning/analysis (goal: minimize usage)
- Target: 30/30 steps, under 5 minutes, eventually zero Learning Agent calls

---

## Next Steps

1. Run full 30-step challenge to populate `knowledge/learnings.jsonl`
2. Verify subsequent runs use stored knowledge (check `knowledge_used` metric)
3. Reduce Vision API calls with better pattern matching
4. Achieve zero Learning Agent calls after initial learning
5. Optimize timing for faster completion
