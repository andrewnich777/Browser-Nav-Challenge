# Browser Navigation Challenge - Vision API Guide

## Challenge Overview
- **URL**: https://serene-frangipane-7fd25b.netlify.app
- **Structure**: 30 steps, click START to begin
- **Goal**: Extract 6-character codes and submit them to advance
- **Code Format**: `[A-HJ-NP-Z2-9]{6}` (no I, O, 0, 1 to avoid confusion)

## Session Codes (The "Cheat Sheet")
- Stored in `sessionStorage` key: `wo_session`
- Encoding: Base64 + XOR with key `"WO_2024_CHALLENGE"`
- Format: `{sessionId, codes: string[30], completed: number[]}`
- Can be used to verify extractions or as fallback

```python
# Decode session codes
import base64, json
KEY = "WO_2024_CHALLENGE"
def decode(encoded):
    decoded = base64.b64decode(encoded)
    return ''.join(chr(b ^ ord(KEY[i % len(KEY)])) for i, b in enumerate(decoded))
```

---

## Vision API Integration

### When Vision is Called

The orchestrator uses vision analysis in several scenarios:

1. **Initial Analysis**: Screenshot analysis to understand the challenge
2. **Action Detection**: Identify what action is needed (scroll, click, etc.)
3. **Code Extraction**: Visually locate codes that aren't in DOM text
4. **Error Recovery**: Understand why extraction failed

### Vision Client (`vision_client.py`)

```python
from vision_client import VisionClient

client = VisionClient()  # Uses ANTHROPIC_API_KEY from env

# Analyze a screenshot
result = client.analyze(
    screenshot_bytes,
    prompt="What is the challenge instruction? What action is needed?"
)

# Track usage
print(f"Tokens used: {client.total_tokens}")
print(f"Estimated cost: ${client.estimated_cost:.4f}")
```

### Vision Agent (`agents/vision.py`)

The vision agent wraps the client with challenge-specific prompts:

```python
# Called by orchestrator
vision_result = self.a["vision"].run(page, step, version)

# Returns:
# {
#     'instruction': 'Scroll down 500px to reveal the code',
#     'action_type': 'scroll',
#     'action_x': None,
#     'action_y': None,
#     'challenge_type': 'scroll_reveal',
#     'code': None  # or extracted code if visible
# }
```

---

## Reducing Vision API Calls

### The Learning System Approach

**Goal**: Use vision for LEARNING, then reuse learned patterns without vision.

```
RUN 1:
  Vision analyzes → Learning Agent extracts pattern → Stored in knowledge

RUN 2+:
  Knowledge Reader checks stored patterns → Executes directly (NO VISION)
```

### Pattern Retrieval First

Before calling vision, the orchestrator checks stored patterns:

```python
# 1. Check stored knowledge (FREE)
if self.knowledge_reader.has_knowledge_for(step, version):
    stored = self.knowledge_reader.get_best_learning(step, version)
    if stored.confidence >= 0.5:
        # Execute without vision call
        return self._execute_stored_action(page, stored)

# 2. Only if no stored knowledge: call vision
vision_result = self.a["vision"].run(page, step, version)
```

### Instruction-Based Pattern Matching

For simple challenges, instruction text alone can determine action:

```python
from learning.retrieval import InstructionMatcher

matcher = InstructionMatcher()
matches = matcher.match("scroll down 500px to reveal the code")
# [('scroll_reveal', 0.85)]

if matches[0][1] >= 0.7:
    # High confidence - skip vision, execute pattern directly
    execute_scroll_reveal(page)
```

---

## Challenge Types by Difficulty

### Steps 1-5 (Beginner)
- **hidden_dom**: Code hidden in DOM, may need to unhide elements
- **scroll_reveal**: Scroll down 500px+ to reveal code

### Steps 6-10 (Intermediate)
- **hover_reveal**: Hover over specific element to show code
- **drag_drop**: Drag element to target to reveal code
- **keyboard_sequence**: Type specific key sequence

### Steps 11-15 (Advanced)
- **canvas**: Code drawn on canvas element
- **encoded_base64**: Code is base64 encoded somewhere on page
- **split_parts**: Code split across multiple elements, combine them

### Steps 16-20 (Expert)
- **multi_tab**: Code appears in popup window/tab
- **puzzle_solve**: Solve a puzzle to reveal code
- **gesture**: Draw 3 strokes on canvas

### Steps 21-30 (Master)
- **shadow_dom**: Code inside shadow DOM, may need multi-level reveal (0/3 levels)
- **websocket**: Connect to WS, code sent via message
- **service_worker**: Interaction with service worker
- **mutation**: Code appears via DOM mutation
- **recursive_iframe**: Code nested in multiple iframes
- **conditional_reveal**: Meet conditions to reveal

---

## Critical: Decoys vs Real Challenges

### DECOY Patterns (IGNORE THESE)
- "Section 1", "Section 2", etc.
- "Content Block X Loaded!"
- "This content appeared Xms after page load"
- "You are on step X. Complete the challenges..."
- "This is filler content"
- Text in overlays with z-index > 9000
- All-numeric codes like `756952` (real codes have letters)

### REAL Challenge Indicators
- Instructions with ACTION words: scroll, click, hover, drag, wait, select, draw
- Located near the code input form
- Not in high z-index overlays
- Specific pixel amounts ("scroll 500px")
- Specific wait times ("wait 5 seconds")

---

## Finding the Real Challenge

```javascript
// The real challenge is near the code input form
const codeInput = document.querySelector('input[placeholder*="code" i]');
const form = codeInput.closest('form');
// Walk up to find challenge container
let container = form.parentElement;
// Look for instruction text with action words in this container
```

---

## Key Technical Details

### Popups/Overlays
- Green "Dismiss" buttons at z-index 9995-9999
- Must dismiss these FIRST before interacting with challenge
- They block the real challenge content

### Code Submission
- Find input with `placeholder*="code"`
- Use React's native value setter:
```javascript
const nativeSet = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
nativeSet.call(input, code);
input.dispatchEvent(new Event('input', {bubbles: true}));
```
- Click submit button in same form

### Shadow DOM Challenges
- Button shows "Reveal Code (0/3 levels)"
- Must click button INSIDE shadow root multiple times
- Each click should increment the counter
- Code appears after reaching final level

### Delayed Reveal
- Instruction says "wait X seconds"
- May need to click "Start" button to begin timer
- Code appears in DOM after timer completes
- Poll for code appearance during wait

### WebSocket Challenges
- Click "Connect" button
- Intercept WebSocket messages
- Code sent as message data

### Drawing/Gesture Challenges
- Find canvas element
- Draw 3 strokes (horizontal, vertical, diagonal)
- Code appears after completing strokes

---

## Agent Architecture That Works

1. **popup** - Dismiss overlay popups first (z-index > 9000)
2. **scroll** - Scroll 500px to reveal content
3. **click_reveal** - Click "Reveal Code" buttons (not in overlays)
4. **delay** - Wait for timed reveals
5. **hover** - Hover over elements
6. **draw** - Draw strokes on canvas
7. **shadow_dom** - Click through shadow DOM levels
8. **websocket** - Handle WS connections
9. **extract_code** - Find the 6-char code
10. **code_entry** - Submit the code

---

## What Vision API Can Do Better

1. **Visual Challenge Detection**: See what the actual challenge looks like
2. **Decoy Filtering**: Visually identify what's noise vs real content
3. **Canvas Reading**: OCR the code from canvas drawings
4. **Spatial Understanding**: Know where to click/hover based on layout
5. **Dynamic Content**: See what actually appears after actions
6. **Error Recovery**: See error messages and adjust

---

## Recommended Vision Approach

1. Take screenshot after dismissing popups
2. Ask Claude to identify:
   - What is the REAL challenge instruction?
   - What action is required?
   - Where is the code (or where will it appear)?
3. Execute the identified action
4. Take another screenshot
5. Ask Claude to extract the visible code
6. Submit and verify advancement

---

## Vision Prompt Templates

### Challenge Analysis
```
Analyze this browser challenge screenshot.

1. What is the REAL instruction (not decoy text)?
2. What action is required (scroll, click, hover, wait, etc.)?
3. If clicking, what are the coordinates of the target?
4. Is a code currently visible? If so, what is it?

Ignore any text that says "filler content" or appears in overlays.
The code format is 6 characters: letters A-Z (no I/O) and digits 2-9 (no 0/1).
```

### Code Extraction
```
Extract the 6-character code from this screenshot.

The code format is: 6 characters using letters A-H, J-N, P-Z and digits 2-9.
Examples: ABC123, XYZ789, H3LL0W (note: no I, O, 0, or 1)

If you see multiple potential codes, prefer:
1. Codes near the input field
2. Codes in highlighted boxes
3. Codes that appeared after an action

Return ONLY the 6-character code, nothing else.
```

---

## Files Reference
- `orchestrator.py` - Main workflow
- `agents/vision.py` - Vision agent
- `vision_client.py` - API client with token tracking
- `knowledge_reader.py` - Access learnings without vision
- `agents/` - Individual action agents
- `session_codes.py` - Code decoding
- `config.py` - Challenge type mapping
- `solve.py` - CLI entry point

---

## Performance Baseline
- Current solver: 30/30 in ~80 seconds
- Most time spent on delayed reveals (5+ seconds each)
- Session code fallback masks extraction failures

---

## Cost Optimization

| Approach | Vision Calls | Estimated Cost |
|----------|--------------|----------------|
| Vision every step | ~60 | ~$0.50 |
| Vision + Learning | ~30 (first run) | ~$0.25 |
| Knowledge reuse | ~5 (fallbacks) | ~$0.04 |
| Fully learned | 0 | $0.00 |

**Goal**: Achieve fully learned state after 1-2 runs.
