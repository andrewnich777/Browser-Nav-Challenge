# Browser Challenge Solver - Help Request for AI Models

## The Goal
Solve 30 browser challenges at https://serene-frangipane-7fd25b.netlify.app in under 5 minutes using Playwright automation. Each challenge reveals a 6-character code that must be extracted and submitted.

## Constraints
- **Human-like only**: Only actions a human could take (click, scroll, hover, type, wait, draw)
- **No cheating**: Cannot use session storage bypass or formula-generated codes
- **Vision-based**: Must extract codes from what appears on screen
- **Time limit**: ~10 seconds average per challenge

## Tech Stack
- Python 3.11 + Playwright
- Claude Vision API for screenshot analysis
- Multi-agent architecture (popup, scroll, click, hover, extract, decode, etc.)

---

## CRITICAL UNSOLVED PROBLEMS

### Problem 1: Coordinate Mapping for Full-Page Screenshots
**Difficulty: HIGH**

We take full-page screenshots (entire scrollable page) and send to Vision API. Vision returns pixel coordinates like `ACTION_COORDS: (640, 1850)`. But after we scroll, the coordinate system changes.

**Current broken logic:**
```python
# Vision says click at y=1850 on full page
target_y = vision_analysis.action_y  # 1850
scroll_to = max(0, target_y - 200)   # Scroll to 1650
page.evaluate(f'window.scrollTo(0, {scroll_to})')
viewport_y = target_y - scroll_to    # 200 (viewport coords)
page.mouse.click(click_x, viewport_y)  # WRONG - doesn't account for actual scroll position
```

**What we need:**
A reliable algorithm to convert full-page screenshot coordinates to viewport coordinates after scrolling, accounting for:
- Actual scroll position (`window.scrollY`)
- Fixed headers/footers that don't scroll
- Elements that move during scroll (sticky elements)

---

### Problem 2: React SPA Stale Content After Navigation
**Difficulty: HIGH**

After submitting a code and advancing to the next step, React Router updates the URL but sometimes doesn't re-render the page. We see:
- Blank page (no content)
- Previous step's content still showing
- 404 errors

**Current fix (partially works):**
```python
page.goto(BASE_URL)  # Go home
page.evaluate(f'''() => {{
    window.history.pushState({{}}, '', '/step{step}?version={version}');
    window.dispatchEvent(new PopStateEvent('popstate', {{state: {{}}}}));
}}''')
```

**What we need:**
- More reliable way to force React Router to re-render
- Detection of when React has finished hydrating
- Alternative: intercept React Router directly

---

### Problem 3: Multi-Click Reveal Challenges
**Difficulty: MEDIUM**

Challenge says "click here 3 more times to reveal" but:
- The clickable element is hard to identify (could be `<span>`, `<u>`, or parent `<div>`)
- Overlays intercept clicks
- Count doesn't always decrement visibly

**Current approach:**
```python
# Find element with data-click-target attribute we set
for i in range(num_clicks):
    page.evaluate('() => { document.querySelector("[data-click-target]")?.click(); }')
    page.wait_for_timeout(300)
```

**What we need:**
- Better element identification (the EXACT element that responds to clicks)
- Verification that click count is decrementing
- Detection when code is revealed

---

### Problem 4: Decoy Detection (Real vs Fake Codes)
**Difficulty: HIGH**

Page has many 6-character strings that match our charset `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`:
- PUNYYR, SUBMIT, SCROLL, REVEAL, HIDDEN (English words)
- 287988, 655265 (numeric decoys)
- Random strings in comments, attributes, overlays

**Current filtering:**
```python
FALSE_POSITIVES = ['SUBMIT', 'SCROLL', 'REVEAL', ...]  # Hardcoded list
if code.isdigit(): return True  # All-numeric = decoy
if letter_count < 2: return True  # Few letters = decoy
```

**What we need:**
- ML/heuristic approach to score code likelihood
- Context analysis: is code near the challenge instructions?
- Frequency analysis: decoys appear multiple times, real code appears once
- Visual analysis: real codes are often highlighted/styled differently

---

### Problem 5: WebSocket Challenge Timing
**Difficulty: MEDIUM**

Some challenges send the code via WebSocket. We intercept with:
```javascript
const OrigWS = window.WebSocket;
window.WebSocket = function(...args) {
    const ws = new OrigWS(...args);
    ws.addEventListener('message', (e) => {
        window.__wsCode = e.data.match(/[CHARSET]{6}/)?.[0];
    });
    return ws;
};
```

**Problems:**
- WebSocket might connect before our script runs
- Message might arrive before we're listening
- Some challenges use Server-Sent Events instead

**What we need:**
- Reliable interception that works regardless of timing
- Support for SSE, long-polling, and other async patterns
- Timeout handling with retry

---

### Problem 6: Canvas/Drawing Challenges
**Difficulty: HIGH**

Some challenges require drawing gestures on a canvas. Instructions might say:
- "Draw a horizontal line"
- "Swipe right to reveal"
- "Complete the gesture"

**Current approach:**
```python
# Always draw 3 fixed strokes
page.mouse.move(x, y)
page.mouse.down()
page.mouse.move(x + 100, y)  # Horizontal
page.mouse.up()
# ... vertical, diagonal
```

**What we need:**
- Parse instruction to understand required gesture
- Detect canvas element bounds
- Verify drawing was registered (some canvases need specific events)
- Support for complex gestures (circles, patterns)

---

### Problem 7: Shadow DOM Code Extraction
**Difficulty: MEDIUM**

Codes hidden in Shadow DOM are invisible to normal selectors:
```javascript
// This doesn't work for shadow DOM:
document.body.innerText  // Won't include shadow content
```

**Current approach:**
```javascript
function searchShadow(root) {
    const text = root.innerHTML;
    // ... search for codes
    for (const el of root.querySelectorAll('*')) {
        if (el.shadowRoot) searchShadow(el.shadowRoot);
    }
}
```

**What we need:**
- Handle closed shadow roots (not accessible via `el.shadowRoot`)
- Handle nested shadow DOM (3+ levels deep)
- Handle dynamically created shadow DOM

---

### Problem 8: Conditional/Sequenced Challenges
**Difficulty: HIGH**

Some challenges require multiple steps in order:
1. Click button A → reveals intermediate code
2. Enter intermediate code → enables button B
3. Click button B → reveals final code

**Current approach:** None - we treat each extraction independently

**What we need:**
- State machine to track challenge progress
- Detection of intermediate steps
- Ability to chain actions based on revealed content

---

### Problem 9: iframe Challenges
**Difficulty: MEDIUM**

Codes may be inside nested iframes:
```html
<iframe src="...">
  <iframe src="...">
    <div>CODE_HERE</div>
  </iframe>
</iframe>
```

**Problems:**
- Cross-origin iframes block access
- Nested iframes require recursive traversal
- Some iframes load asynchronously

**What we need:**
- Reliable iframe content extraction
- Handling for cross-origin restrictions
- Async loading detection

---

### Problem 10: Timing-Based Challenges
**Difficulty: MEDIUM**

"Wait 5 seconds for the code to appear" or "Code changes every 2 seconds"

**Current approach:**
```python
page.wait_for_timeout(wait_time)
```

**Problems:**
- Don't know exact timing
- Code might appear early/late
- Changing codes need to be captured at right moment

**What we need:**
- Polling with change detection
- MutationObserver-based waiting
- Capture codes that only appear briefly

---

## ARCHITECTURE QUESTIONS

### Q1: Should we use computer-use style approach?
Instead of specialized agents, use a single vision model that:
1. Looks at screenshot
2. Decides action (click at x,y, type text, scroll, etc.)
3. Executes action
4. Repeats until code found

Pros: More flexible, handles novel challenges
Cons: Slower, more API calls, higher cost

### Q2: How to build a learning system that improves over runs?
Current: Save failure analysis to JSONL, rarely use it
Desired: Each run gets smarter based on previous failures

Ideas needed:
- What to store per challenge?
- How to index/retrieve relevant learnings?
- How to apply learnings automatically?

### Q3: Parallel vs Sequential agent execution?
Current: Run agents sequentially, stop when one works
Alternative: Run multiple agents in parallel, take first success

Trade-offs to analyze:
- Speed vs reliability
- Resource usage
- Error handling complexity

---

## CODE SAMPLES FOR CONTEXT

### Main Orchestrator Loop
```python
def run_step(self, page, step: int, version: int):
    # 1. Clear popups
    self._run_agent("popup", page, step, version)

    # 2. Vision analysis
    vision_analysis = self.a["vision"].run(page, step, version)
    action = vision_analysis.action_type  # scroll, click, hover, etc.

    # 3. Execute action based on vision
    if action == 'scroll':
        self._run_agent("scroll", page, step, version)
    elif action == 'click':
        # Use vision coordinates...
    # ... etc

    # 4. Extract code
    code = self.a["extract_code"].run(page, step, version)

    # 5. Submit
    self._run_agent("code_entry", page, step, version, code=code)
```

### Vision Prompt (what we send to Claude)
```
You are analyzing a FULL PAGE browser screenshot.
Identify the REAL challenge instruction (not decoys).

CHALLENGE TYPES: Click/Reveal, Scroll, Hover, Delay/Wait,
Checkbox/Radio, Slider, Draw/Gesture, WebSocket, Decode

Respond with:
INSTRUCTION: [the challenge instruction]
ACTION: [scroll|click|hover|...]
CODE_VISIBLE: [yes/no]
CODE_VALUE: [6-char code or "none"]
ACTION_COORDS: [x, y for click/hover]
```

---

## WHAT I NEED FROM YOU

Please provide:

1. **Specific code solutions** for any of the 10 problems above
2. **Algorithm designs** for coordinate mapping, decoy detection, etc.
3. **Architecture recommendations** for the Q1-Q3 questions
4. **Novel approaches** we haven't considered
5. **Patterns from similar problems** (web scraping, browser automation, captcha solving)

Focus on solutions that are:
- **Fast** (sub-second per operation)
- **Reliable** (>95% success rate)
- **Generalizable** (work across challenge variations)

---

## EXAMPLE CHALLENGE TYPES (for reference)

| Type | Description | Current Success |
|------|-------------|-----------------|
| Scroll Reveal | Scroll down to find code | 90% |
| Click Reveal | Click button to show code | 70% |
| Hidden DOM | Code in data-* attributes, click 3x to reveal | 40% |
| Hover Reveal | Hover over element | 60% |
| Delay/Wait | Wait N seconds | 85% |
| Checkbox/Radio | Select correct options | 75% |
| Canvas Draw | Draw gesture to reveal | 30% |
| WebSocket | Code sent via WS | 50% |
| Shadow DOM | Code in shadow tree | 45% |
| Decode | Base64/Hex/ROT13 encoded | 65% |
| Multi-tab | Code in popup window | 55% |
| Conditional | Multi-step sequential | 20% |

---

## RESPONSE FORMAT

Please structure your response as:

```
## Problem X: [Name]

### Analysis
[Why current approach fails]

### Solution
[Detailed code/algorithm]

### Implementation
[Python code or pseudocode]

### Edge Cases
[What to watch out for]
```

Thank you for your help!
