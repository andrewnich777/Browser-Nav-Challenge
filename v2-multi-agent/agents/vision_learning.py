"""
Vision Learning Agent — multi-turn conversational agent for solving unknown challenges.

V3: Enhanced flight recorder (locator cascade, DOM sig, progress tracking),
single DSL executor (delegates to RecipeExecutor._execute_step), progress binding,
passive harvest between rounds, bounded iteration (max 4 actions/round, stall detection).
"""

import json
import os
import re
import base64
import concurrent.futures
import anthropic

from agents.base import Agent
from agents.popup import dismiss_all_popups
from config import CHARSET
from log import log_stage
from primitives import extract_code_js, read_progress, get_locator_cascade

VISION_MODEL = os.getenv("VISION_MODEL", "claude-sonnet-4-5-20250929")

from code_scorer import DECOY_CODES, harvest_and_score, is_valid_code
FALSE_POS_CODES = frozenset(DECOY_CODES)

MAX_ACTIONS_PER_ROUND = 6
MAX_ROUNDS = 6
MAX_STALLS = 2

SYSTEM_PROMPT = f"""You are a browser automation agent solving a 30-step navigation challenge.
Each step has a hidden 6-character code (charset: {CHARSET}, NO letters I/O or digits 0/1).

You receive a VIEWPORT SCREENSHOT and STRUCTURED PAGE DATA each round.
This is a MULTI-TURN conversation — you can see all your previous rounds.
All coordinates are VIEWPORT-relative (0,0 = top-left of visible area).

KNOWN CHALLENGE TYPES (steps 21-30):
- mutation: Click interactive elements to trigger DOM mutations. Track "N/M mutations".
- service_worker: Service worker sends code via postMessage.
- recursive_iframe: Code split across nested iframes.
- conditional_reveal: Meet conditions (checkbox + slider + dropdown) to unlock reveal.
- shadow_dom: Code hidden in shadow DOM roots.
- websocket: Code arrives via WebSocket.
- sequence: Complete mini-tasks in order (hover, click, type).

ELEMENT TYPES:
- "interactable_elements" are labeled [N] on the screenshot. You MUST use "element_id" to target them.
  Types: button, text_input, drag_source, drop_target, canvas, clickable, checkbox, radio, select, slider, focusable, submit_button
  Example: {{"type": "click", "element_id": 7}}
- "context_elements" help you UNDERSTAND the challenge. NEVER target them with actions.
- For drag-and-drop: drag from "drag_source" to "drop_target" by ID. Prefer drop_confidence: high/medium.
  Example: {{"type": "drag", "source_id": 3, "target_id": 15}}
- Raw x,y coordinates are ONLY for: scroll, canvas drawing, and typing into focused elements.
- CANVAS DRAWING: Canvas elements include "canvas_bounds" with {{left, top, right, bottom, width, height}} in viewport pixels.
  ALL drag/draw coordinates on canvas MUST be WITHIN canvas_bounds. Check bounds BEFORE proposing actions.
  Example: if canvas_bounds={{left:150, top:200, right:550, bottom:500}}, x values must be 150-550, y values 200-500.
  For draw strokes, use NORMALIZED coordinates (0.0 to 1.0) relative to the canvas — the system converts them to viewport pixels.
- DOUBLE CLICK: Use when single click doesn't work, or for elements that need double-click activation.
- FOCUS: Focus an input/element before typing. Use element_id. Useful when type alone doesn't reach the right field.
- ELEMENT SCROLL: Scroll inside a specific container/element. Use element_id + direction + amount. Different from page scroll.
- INTERACTIVE CONTAINERS: When "containers" data is present, it lists nested styled containers (fake iframes, panels, scrollable divs) with their interactive children. Target elements INSIDE these containers — they may not appear in the main catalog.
- NESTED LEVEL CHALLENGES (recursive_iframe): Buttons to enter deeper levels may be HIDDEN below filler content inside scrollable containers. If progress stalls at depth N/(N+1), use element_scroll on the deepest container FIRST to reveal the navigation button, THEN click it. The "Extract Code" button only works after reaching the FINAL depth — do NOT click it prematurely.

RULES:
- Elements marked "decoy: true" in the catalog are known traps. NEVER interact with them.
- PINK/MAGENTA GRADIENT BUTTONS are ALWAYS decoys (e.g. "Next", "Continue", "Proceed", "Advance", "Click Here", "Move On", "Keep Going", "Go Forward", "Next Step", "Next Page", "Continue Journey"). They appear on EVERY page. NEVER click them. Focus on buttons INSIDE the challenge area.
- Decoy popups and fake buttons are everywhere. IGNORE THEM.
- 99 filler "Section N" blocks are noise.
- Codes visible BEFORE completing the challenge are DECOYS.
- The REAL code appears AFTER completing the required interaction.
- INPUT FIELDS: If the challenge has an empty input/textarea, you MUST fill it BEFORE clicking submit/reveal. Read the instructions to determine what to type. For encoded/Base64 challenges, decode the string mentally and type the result (or first 6 chars). For math/puzzle challenges, compute the answer and type it. ALWAYS: type first, THEN click the action button.
- PROGRESS INDICATORS ("3/5 mutations") are your key feedback signal.
- COMPLETION BUTTONS: If you see a "Reveal Code", "Show Code", "Complete Challenge", "Get Code", or "All Tabs Visited" button, click it IMMEDIATELY — this is almost always the correct next action. The code ONLY appears after clicking this. Do NOT re-click already-completed items (marked with checkmarks ✓).
- STATUS vs ACTION: Buttons showing current state (e.g. "Connected", "Active", "Running") are STATUS INDICATORS — clicking them does nothing. Look for ACTION buttons instead ("Reveal Code", "Start", "Submit"). Prioritize action verbs over state descriptions.
- For "scroll box" or "scroll inside" tasks, scroll INSIDE the specific element (use element_scroll with element_id), not the whole page.
- If progress DISAPPEARS after an action, that action BROKE something.
- STATE CHANGES show how the UI reacted to your actions. Check "state_changes" each round. Color transitions carry context:
  * turned_green: element turned green — could mean "task complete, move on" OR "ready/active, click me now." Assess based on text and challenge state.
  * turned_red: element turned red — could mean "error/wrong approach" OR "urgent/important." If it's a button, it might need clicking. If it's a label, your last action may have been wrong.
  * turned_grey: element greyed out — likely consumed/completed. Don't re-click it, look for the NEXT uncompleted element.
  * enabled/appeared/became_clickable: element just became interactable — strong signal to try it.
  Higher priority = stronger signal. These are weighted hints, not commands. Reason about what the change MEANS in context.

OUTPUT FORMAT — respond with ONLY a JSON object, no other text:
{{
  "observation": "What I see on screen",
  "reasoning": "My plan for this round",
  "actions": [
    {{"type": "click", "element_id": 7}},
    {{"type": "click", "x": 312, "y": 418}},
    {{"type": "hover", "element_id": 3, "seconds": 2}},
    {{"type": "scroll", "direction": "down", "amount": 500}},
    {{"type": "type", "text": "hello"}},
    {{"type": "press", "keys": "Enter"}},
    {{"type": "wait", "seconds": 2}},
    {{"type": "drag", "source_id": 3, "target_id": 15}},
    {{"type": "drag", "x1": 100, "y1": 200, "x2": 300, "y2": 400}},
    {{"type": "double_click", "element_id": 5}},
    {{"type": "focus", "element_id": 12}},
    {{"type": "element_scroll", "element_id": 8, "direction": "down", "amount": 300}},
    {{"type": "draw", "element_id": 4, "path": [[0.1, 0.2], [0.5, 0.8], [0.9, 0.3]]}},
    {{"type": "wait_for_state", "change_types": ["enabled", "appeared"], "timeout_ms": 3000}}
  ],
  "code": "NONE",
  "notes": "Brief notes for next round"
}}

IMPORTANT:
- "actions" is an ARRAY. You can include multiple actions per round.
- Maximum {MAX_ACTIONS_PER_ROUND} actions per round — be selective.
- "code" must be exactly 6 chars from {CHARSET}, or "NONE".
- All x,y coordinates are VIEWPORT pixels (what you see in the screenshot).
- You MUST output valid JSON. No markdown, no prose, no explanation outside the JSON."""


class VisionLearningAgent(Agent):
    name = "vision_learning"

    def __init__(self):
        super().__init__()
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = VISION_MODEL

    def run(self, page, step: int, version: int, context: dict = None) -> str | None:
        """
        Multi-turn vision loop with strict JSON contract:
        1. Capture viewport state (screenshot + page info + scroll pos)
        2. Send to Claude as part of conversation
        3. Parse JSON response (actions array)
        4. Execute each action via RecipeExecutor (single DSL executor)
        5. Re-capture state, describe changes (source of truth)
        6. Feed changes back to Claude
        """
        context = context or {}
        messages = []
        self._action_log = []
        self._round_observations = []
        self._stall_count = 0
        self._progress_binding = None  # First progress read anchors this

        log_stage("vision_learn", f"starting multi-turn loop (up to {MAX_ROUNDS} rounds)")

        # Clear popups before starting
        try:
            for _ in range(8):
                if not dismiss_all_popups(page):
                    break
        except Exception:
            pass

        for round_num in range(MAX_ROUNDS):
            # === Stall check ===
            if self._stall_count >= MAX_STALLS:
                log_stage("vision_learn", f"stalled {self._stall_count}x, stopping")
                break

            # === 1. Capture state ===
            viewport_state = self._capture_viewport_state(page)
            page_info = self._extract_page_info(page)

            screenshot_bytes = page.screenshot(type="png", full_page=False)
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

            log_stage("vision_learn",
                      f"round {round_num+1}: {len(page_info['buttons'])} buttons, "
                      f"{len(page_info['clickables'])} clickables, "
                      f"progress={page_info.get('progress')}, "
                      f"scroll_y={viewport_state['scroll_y']}")

            # === 2. Build user message ===
            user_content = self._build_user_message(
                round_num, step, context, page_info, viewport_state, screenshot_b64
            )
            messages.append({"role": "user", "content": user_content})

            # === 3. Call Claude ===
            response_text = self._call_claude(messages)
            if not response_text:
                log_stage("vision_learn", f"round {round_num+1}: no response")
                messages.pop()
                continue

            log_stage("vision_learn", f"round {round_num+1} raw: {response_text[:300]}")
            messages.append({"role": "assistant", "content": response_text})

            # === 4. Parse JSON response ===
            parsed = self._parse_json_response(response_text)
            if not parsed:
                log_stage("vision_learn", f"round {round_num+1}: failed to parse JSON")
                continue

            self._round_observations.append({
                'round': round_num + 1,
                'observation': parsed.get('observation', '')[:200],
                'reasoning': parsed.get('reasoning', '')[:200],
                'actions': parsed.get('actions', []),
                'progress': page_info.get('progress'),
            })

            # Check if code was spotted
            code_val = parsed.get('code', 'NONE')
            if code_val and code_val.upper() != 'NONE':
                code = code_val.strip().upper()
                if len(code) == 6 and all(c in CHARSET for c in code) and code not in FALSE_POS_CODES:
                    log_stage("vision_learn", f"round {round_num+1}: found code {code}")
                    return code
                else:
                    log_stage("vision_learn", f"round {round_num+1}: rejected code '{code_val}'")

            # === 5. Execute actions with flight recorder ===
            actions = parsed.get('actions', [])
            if not actions:
                log_stage("vision_learn", f"round {round_num+1}: no actions")
            else:
                for i, action in enumerate(actions[:MAX_ACTIONS_PER_ROUND]):
                    # Capture state before action
                    dom_sig_before = self._get_dom_signature(page)
                    progress_before = self._read_bound_progress(page)
                    scroll_y_before = viewport_state.get('scroll_y', 0)

                    # Get locator cascade before action (for flight recorder)
                    locator_info = None
                    action_type = action.get('type', '').lower()
                    if action_type in ('click', 'hover') and 'x' in action and 'y' in action:
                        locator_info = get_locator_cascade(
                            page, float(action['x']), float(action['y'])
                        )

                    # Execute via single DSL executor path
                    hit_info = self._execute_action(page, action, viewport_state)

                    # Capture state after action
                    dom_sig_after = self._get_dom_signature(page)
                    dom_change_score = self._compute_change_score(dom_sig_before, dom_sig_after)
                    progress_after = self._read_bound_progress(page)
                    scroll_y_after = page.evaluate('() => Math.round(window.scrollY)')

                    # Record in flight log
                    self._action_log.append({
                        'round': round_num + 1,
                        'action': action,
                        'hit': hit_info,
                        'locator': locator_info,
                        'dom_sig_before': dom_sig_before,
                        'dom_sig_after': dom_sig_after,
                        'dom_change_score': dom_change_score,
                        'scroll_y': scroll_y_after,
                        'frame_path': 'main',
                        'progress_before': progress_before.get('fraction') if progress_before else None,
                        'progress_after': progress_after.get('fraction') if progress_after else None,
                        'found_code': False,
                    })

                    page.wait_for_timeout(150)  # Reduced from 250ms

                    # Passive harvest after each action (Fix L)
                    code = self._check_observers_and_harvest(page)
                    if code:
                        log_stage("vision_learn",
                                  f"round {round_num+1}: code after action {i+1}: {code}")
                        self._action_log[-1]['found_code'] = True
                        return code

            # === 6. Wait, dismiss popups, check code ===
            page.wait_for_timeout(500)
            try:
                dismiss_all_popups(page)
            except Exception:
                pass

            code = self._check_observers_and_harvest(page)
            if code:
                log_stage("vision_learn", f"round {round_num+1}: code after settle: {code}")
                return code

            # === 7. Re-capture state and build authoritative change description ===
            post_info = self._extract_page_info(page)
            change_desc = self._describe_changes_authoritative(page_info, post_info)
            context['last_result'] = change_desc

            # === 8. Stall detection via progress binding ===
            self._update_stall_count(page_info, post_info)

            if self._stall_count >= MAX_STALLS:
                context['last_result'] = f"STALLED: {change_desc}"

            log_stage("vision_learn",
                      f"round {round_num+1} changes: {change_desc} "
                      f"(stalls={self._stall_count})")

        log_stage("vision_learn", "all rounds exhausted")
        return None

    # ── Plan-only interface (used by LearningSidecar) ─────────────────────

    def reset_conversation(self):
        """Reset multi-turn state for a new step."""
        self._messages = []
        self._progress_binding = None
        self._stall_count = 0

    def _extract_page_info_minimal(self, page) -> dict:
        """Lightweight page info when element_catalog is already available.

        Only fetches: text, progress, scroll_y, viewport_h, codes_on_page.
        Skips: buttons, clickables, inputs, interactives, iframes scans.
        """
        try:
            return page.evaluate(f'''() => {{
                const CHARSET = "{CHARSET}";
                const codePattern = new RegExp('[' + CHARSET + ']{{6}}', 'g');
                const rawText = document.body?.innerText || '';
                const lines = rawText.split('\\n');
                const filtered = lines.filter(l => !l.match(/^Section \\d+$/));
                const text = filtered.join('\\n').replace(/\\n{{3,}}/g, '\\n\\n').substring(0, 3000);
                const codes = [];
                const matches = rawText.match(codePattern) || [];
                for (const m of matches) {{ if (m === m.toUpperCase()) codes.push(m); }}
                const progressMatch = rawText.match(/(\\d+)\\s*\\/\\s*(\\d+)\\s*(?:mutation|task|step|complete|done|click|interaction)/i)
                    || rawText.match(/(?:mutation|task|step|click|progress|interaction)[:\\s]*(\\d+)\\s*\\/\\s*(\\d+)/i)
                    || rawText.match(/(\\d+)\\s*of\\s*(\\d+)\\s*(?:mutation|task|complete|interaction)/i);
                const progress = progressMatch
                    ? {{ current: parseInt(progressMatch[1]), total: parseInt(progressMatch[2]),
                         raw: progressMatch[0] }}
                    : null;
                return {{
                    text, codes_on_page: codes, progress,
                    scroll_y: Math.round(window.scrollY),
                    viewport_h: window.innerHeight,
                    buttons: [], clickables: [], inputs: [],
                    interactives: [], iframes: [],
                }};
            }}''')
        except Exception:
            return {
                'text': '', 'buttons': [], 'clickables': [], 'inputs': [],
                'codes_on_page': [], 'interactives': [], 'iframes': [],
                'progress': None, 'scroll_y': 0, 'viewport_h': 1024
            }

    def propose_actions(self, page, step: int, version: int,
                        context: dict = None, history: list = None,
                        timeout_s: float | None = None) -> dict:
        """Plan-only: propose actions without executing. Returns strict dict.

        Uses existing VL internals for state capture and Claude API but skips execution.
        The sidecar owns execution, observation, and iteration.
        """
        context = context or {}

        # 1. Capture state (lightweight when catalog already available)
        viewport_state = self._capture_viewport_state(page)
        extra = context.get('_page_info_extra', {})
        if extra.get('element_catalog'):
            page_info = self._extract_page_info_minimal(page)
        else:
            page_info = self._extract_page_info(page)
        screenshot_bytes = page.screenshot(type="png", full_page=False)
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

        # Merge sidecar-injected page info (BID catalog, challenge region, etc.)
        if extra.get('element_catalog'):
            page_info['element_catalog'] = extra['element_catalog']
        if extra.get('challenge_region'):
            page_info['challenge_region'] = extra['challenge_region']
        if extra.get('frames'):
            page_info['frames'] = extra['frames']
        if extra.get('shadow_roots'):
            page_info['shadow_roots'] = extra['shadow_roots']
        if extra.get('containers'):
            page_info['containers'] = extra['containers']

        # 2. Build user message (with history support)
        round_num = len(self._messages) // 2  # user+assistant pairs
        user_content = self._build_user_message(
            round_num, step, context, page_info, viewport_state, screenshot_b64,
            history=history,
        )
        self._messages.append({"role": "user", "content": user_content})

        # 3. Call Claude (with optional timeout)
        response_text = None
        if timeout_s is not None:
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(self._call_claude, self._messages)
                    response_text = future.result(timeout=timeout_s)
            except (concurrent.futures.TimeoutError, Exception) as e:
                log_stage("vision_learn", f"propose_actions timeout/error: {e}")
                self._messages.pop()  # Remove unanswered user message
                return {'actions': [], 'notes': 'timeout', 'stop': False, 'extracted_codes': []}
        else:
            response_text = self._call_claude(self._messages)

        if not response_text:
            self._messages.pop()
            return {'actions': [], 'notes': 'no_response', 'stop': False, 'extracted_codes': []}

        # Append assistant response (multi-turn accumulates)
        self._messages.append({"role": "assistant", "content": response_text})

        # 4. Parse JSON
        parsed = self._parse_json_response(response_text)
        if not parsed:
            return {'actions': [], 'notes': 'parse_failed', 'stop': False, 'extracted_codes': []}

        # 5. Extract and validate codes
        raw_code = parsed.get('code', 'NONE')
        extracted = []
        if raw_code and str(raw_code).upper() != 'NONE':
            codes = [str(raw_code).upper()]
            for c in codes:
                if re.fullmatch(r'[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}', c):
                    try:
                        js_valid = page.evaluate(
                            f"() => !window.__isValidCode || window.__isValidCode('{c}')"
                        )
                    except Exception:
                        js_valid = True
                    if js_valid:
                        extracted.append(c)

        # 6. Return strict dict
        return {
            'actions': parsed.get('actions', [])[:6],
            'notes': parsed.get('notes', '') or parsed.get('observation', ''),
            'observation': (parsed.get('observation') or '')[:500],  # Layer A: preserve for recipe review
            'reasoning': (parsed.get('reasoning') or '')[:500],   # Layer A: preserve for recipe review
            'stop': parsed.get('stop', False),
            'extracted_codes': extracted,
        }

    # ── Progress binding ──────────────────────────────────────────────────

    def _read_bound_progress(self, page) -> dict | None:
        """Read progress, anchored to first successful binding."""
        prog = read_progress(page)
        if prog is None:
            return None

        if self._progress_binding is None:
            # First read: bind
            self._progress_binding = {
                'total': prog['total'],
                'text_prefix': prog['text'][:10],
            }
            return prog

        # Subsequent reads: filter to matching binding
        if prog['total'] != self._progress_binding['total']:
            return None  # Different denominator, not our progress
        return prog

    def _update_stall_count(self, before_info: dict, after_info: dict):
        """Track stalls via bound progress."""
        old_prog = before_info.get('progress')
        new_prog = after_info.get('progress')

        if old_prog and new_prog:
            if new_prog['current'] <= old_prog['current']:
                self._stall_count += 1
            else:
                self._stall_count = 0  # Reset on progress
        elif not old_prog and not new_prog:
            # No progress indicator at all — count as mild stall
            self._stall_count += 0.5

    # ── State capture ────────────────────────────────────────────────────────

    def _capture_viewport_state(self, page) -> dict:
        """Capture viewport geometry at screenshot time for coordinate verification."""
        try:
            return page.evaluate('''() => ({
                scroll_x: Math.round(window.scrollX),
                scroll_y: Math.round(window.scrollY),
                viewport_w: window.innerWidth,
                viewport_h: window.innerHeight,
                dpr: window.devicePixelRatio || 1
            })''')
        except Exception:
            return {'scroll_x': 0, 'scroll_y': 0, 'viewport_w': 1280, 'viewport_h': 1024, 'dpr': 1}

    def _extract_page_info(self, page) -> dict:
        """Extract structured page data: text, buttons, clickables, inputs, progress, etc."""
        try:
            info = page.evaluate(f'''() => {{
                const CHARSET = "{CHARSET}";
                const codePattern = new RegExp('[' + CHARSET + ']{{6}}', 'g');
                const vh = window.innerHeight;

                // 1. Text (skip filler sections)
                const rawText = document.body?.innerText || '';
                const lines = rawText.split('\\n');
                const filtered = lines.filter(l => !l.match(/^Section \\d+$/));
                const text = filtered.join('\\n').replace(/\\n{{3,}}/g, '\\n\\n').substring(0, 3000);

                // 2. Buttons (viewport only)
                const buttons = [];
                for (const btn of document.querySelectorAll('button')) {{
                    const rect = btn.getBoundingClientRect();
                    if (rect.width < 5 || rect.height < 5) continue;
                    if (rect.bottom < -50 || rect.top > vh + 50) continue;
                    const style = getComputedStyle(btn);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    const txt = (btn.textContent || '').trim().substring(0, 60);
                    if (!txt) continue;
                    buttons.push({{
                        text: txt,
                        x: Math.round(rect.left + rect.width/2),
                        y: Math.round(rect.top + rect.height/2),
                        w: Math.round(rect.width),
                        h: Math.round(rect.height),
                        enabled: !btn.disabled
                    }});
                }}

                // 3. Clickable non-button elements (cursor:pointer divs/spans)
                const clickables = [];
                for (const el of document.querySelectorAll('div, span, li, label')) {{
                    const style = getComputedStyle(el);
                    if (style.cursor !== 'pointer') continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 15 || rect.height < 15) continue;
                    if (rect.width > 600) continue;
                    if (rect.bottom < -50 || rect.top > vh + 50) continue;
                    if (el.closest('button')) continue;
                    clickables.push({{
                        text: (el.textContent || '').trim().substring(0, 40) || '(empty)',
                        x: Math.round(rect.left + rect.width/2),
                        y: Math.round(rect.top + rect.height/2),
                        w: Math.round(rect.width),
                        h: Math.round(rect.height),
                        bg: style.backgroundColor || '',
                        tag: el.tagName.toLowerCase()
                    }});
                }}

                // 4. Inputs
                const inputs = [];
                for (const inp of document.querySelectorAll('input, textarea, select')) {{
                    const rect = inp.getBoundingClientRect();
                    if (rect.width < 5) continue;
                    inputs.push({{
                        type: inp.type || inp.tagName.toLowerCase(),
                        placeholder: inp.placeholder || '',
                        value: inp.value || '',
                        x: Math.round(rect.left + rect.width/2),
                        y: Math.round(rect.top + rect.height/2)
                    }});
                }}

                // 5. Codes on page
                const codes = [];
                const matches = rawText.match(codePattern) || [];
                for (const m of matches) {{
                    if (m === m.toUpperCase()) codes.push(m);
                }}

                // 6. Special interactive elements
                const interactives = [];
                const selectors = 'canvas, audio, video, iframe, [draggable="true"], '
                    + 'input[type="range"], input[type="checkbox"], input[type="radio"], '
                    + '[role="slider"], [role="switch"], [role="tab"]';
                for (const el of document.querySelectorAll(selectors)) {{
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 5 || rect.height < 5) continue;
                    if (rect.bottom < -50 || rect.top > vh + 100) continue;
                    interactives.push({{
                        tag: el.tagName.toLowerCase(),
                        type: el.type || el.getAttribute('role') || '',
                        classes: (el.className?.toString() || '').substring(0, 60),
                        x: Math.round(rect.left + rect.width/2),
                        y: Math.round(rect.top + rect.height/2),
                        w: Math.round(rect.width),
                        h: Math.round(rect.height)
                    }});
                }}

                // 7. Progress indicators
                const progressMatch = rawText.match(/(\\d+)\\s*\\/\\s*(\\d+)\\s*(?:mutation|task|step|complete|done|click|interaction)/i)
                    || rawText.match(/(?:mutation|task|step|click|progress|interaction)[:\\s]*(\\d+)\\s*\\/\\s*(\\d+)/i)
                    || rawText.match(/(\\d+)\\s*of\\s*(\\d+)\\s*(?:mutation|task|complete|interaction)/i);
                const progress = progressMatch
                    ? {{ current: parseInt(progressMatch[1]), total: parseInt(progressMatch[2]),
                         raw: progressMatch[0] }}
                    : null;

                // 8. Iframes
                const iframes = [];
                for (const iframe of document.querySelectorAll('iframe')) {{
                    const rect = iframe.getBoundingClientRect();
                    iframes.push({{
                        src: (iframe.src || '').substring(0, 100),
                        x: Math.round(rect.left + rect.width/2),
                        y: Math.round(rect.top + rect.height/2),
                        w: Math.round(rect.width),
                        h: Math.round(rect.height)
                    }});
                }}

                return {{
                    text, buttons: buttons.slice(0, 20),
                    clickables: clickables.slice(0, 15),
                    inputs: inputs.slice(0, 10),
                    codes_on_page: codes,
                    interactives: interactives.slice(0, 15),
                    iframes,
                    progress,
                    scroll_y: Math.round(window.scrollY),
                    viewport_h: vh
                }};
            }}''')
            return info
        except Exception as e:
            log_stage("vision_learn", f"page info error: {e}")
            return {
                'text': '', 'buttons': [], 'clickables': [], 'inputs': [],
                'codes_on_page': [], 'interactives': [], 'iframes': [],
                'progress': None, 'scroll_y': 0, 'viewport_h': 1024
            }

    # ── Message building ─────────────────────────────────────────────────────

    def _build_user_message(self, round_num: int, step: int, context: dict,
                            page_info: dict, viewport_state: dict,
                            screenshot_b64: str, history: list = None) -> list:
        """Build multimodal user message: screenshot + structured JSON data."""
        data = {
            'round': round_num + 1,
            'step': step,
            'viewport': {
                'width': viewport_state['viewport_w'],
                'height': viewport_state['viewport_h'],
                'scroll_y': viewport_state['scroll_y'],
            },
        }

        if round_num == 0:
            data['context'] = {
                'config_challenge_type': context.get('config_challenge_type', 'unknown'),
                'detected_challenge_type': context.get('detected_challenge_type'),
                'agents_tried': context.get('agents_tried', []),
                'failure_reason': context.get('failure_reason', ''),
            }
            # Include archived recipe hints if available
            if context.get('archived_recipe_hint'):
                data['context']['archived_recipe_hint'] = context['archived_recipe_hint']

        # History from sidecar takes priority over last_result
        if history and len(history) > 0:
            data['history'] = history
        elif round_num > 0 and context.get('last_result'):
            data['last_round_result'] = context['last_result']

        if page_info.get('progress'):
            data['progress'] = page_info['progress']

        # Page text (filtered)
        page_text = page_info.get('text', '')
        data['page_text'] = page_text[:2000] if len(page_text) > 2000 else page_text

        # Elements inventory
        if page_info.get('buttons'):
            data['buttons'] = page_info['buttons']
        if page_info.get('clickables'):
            data['clickables'] = page_info['clickables']
        if page_info.get('inputs'):
            data['inputs'] = page_info['inputs']
        if page_info.get('interactives'):
            data['interactives'] = page_info['interactives']
        if page_info.get('iframes'):
            data['iframes'] = page_info['iframes']

        codes = [c for c in page_info.get('codes_on_page', []) if c not in FALSE_POS_CODES]
        if codes:
            data['codes_visible'] = codes

        if page_info.get('element_catalog'):
            catalog = page_info['element_catalog']
            data['interactable_elements'] = [e for e in catalog if e.get('interactable', True)]
            context_els = [e for e in catalog if not e.get('interactable', True)]
            if context_els:
                data['context_elements'] = context_els
        if page_info.get('challenge_region'):
            data['challenge_region'] = page_info['challenge_region']
        if page_info.get('frames'):
            data['frames'] = page_info['frames']
        if page_info.get('shadow_roots'):
            data['shadow_roots'] = page_info['shadow_roots']
        if context.get('suggested_strategy'):
            data['suggested_strategy'] = context['suggested_strategy']
        if context.get('challenge_archetype'):
            data['challenge_archetype'] = context['challenge_archetype']
        # State changes: include in data AND surface as prominent header
        state_header = ""
        if context.get('state_changes'):
            data['state_changes'] = context['state_changes']
            # Build contextual header — color transitions include semantic hints
            COLOR_HINTS = {
                'turned_green': '(could mean "done" or "active/click me")',
                'turned_red': '(could mean "error" or "urgent/click me")',
                'turned_grey': '(likely consumed/completed — skip it)',
            }
            lines = []
            for sc in context['state_changes'][:5]:
                changes = sc.get('changes', [])
                changes_str = ', '.join(changes)
                bid = sc.get('matched_bid', '?')
                text = sc.get('text', '')[:30]
                tag = sc.get('tag', '')
                # Add color hint if a color transition is present
                hint = ''
                for ch in changes:
                    if ch in COLOR_HINTS:
                        hint = ' ' + COLOR_HINTS[ch]
                        break
                lines.append(f"  {tag} \"{text}\" [BID {bid}]: {changes_str}{hint}")
            state_header = (
                "UI STATE CHANGES since last round:\n"
                + "\n".join(lines)
                + "\nConsider these signals when choosing your next action.\n\n"
            )

        prompt_text = json.dumps(data, indent=2)

        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": screenshot_b64,
                },
            },
        ]
        # State change header goes BEFORE the JSON data for maximum visibility
        if state_header:
            content.append({"type": "text", "text": state_header})
        content.append({"type": "text", "text": prompt_text})
        return content

    # ── Change description (source of truth) ─────────────────────────────────

    def _describe_changes_authoritative(self, before: dict, after: dict) -> str:
        """Authoritative change description. Progress loss = failure signal."""
        changes = []

        # Progress (SOURCE OF TRUTH)
        old_prog = before.get('progress')
        new_prog = after.get('progress')
        if old_prog and new_prog:
            if new_prog['current'] > old_prog['current']:
                changes.append(f"PROGRESS_UP: {old_prog['current']}/{old_prog['total']} -> "
                               f"{new_prog['current']}/{new_prog['total']}")
            elif new_prog['current'] < old_prog['current']:
                changes.append(f"PROGRESS_REGRESSED: {old_prog['current']}/{old_prog['total']} -> "
                               f"{new_prog['current']}/{new_prog['total']} -- last action may have broken state")
            else:
                changes.append(f"PROGRESS_UNCHANGED: {new_prog['current']}/{new_prog['total']}")
        elif old_prog and not new_prog:
            changes.append(f"PROGRESS_LOST: was {old_prog['current']}/{old_prog['total']}, "
                           "indicator no longer visible -- possible page break or scroll drift")
        elif not old_prog and new_prog:
            changes.append(f"PROGRESS_APPEARED: {new_prog['current']}/{new_prog['total']}")

        # New codes
        old_codes = set(before.get('codes_on_page', []))
        new_codes = set(after.get('codes_on_page', [])) - old_codes
        new_codes = {c for c in new_codes if c not in FALSE_POS_CODES}
        if new_codes:
            changes.append(f"NEW_CODES: {', '.join(new_codes)}")

        # Buttons
        old_btns = {b['text'] for b in before.get('buttons', [])}
        new_btns = {b['text'] for b in after.get('buttons', [])} - old_btns
        gone_btns = old_btns - {b['text'] for b in after.get('buttons', [])}
        if new_btns:
            changes.append(f"NEW_BUTTONS: {', '.join(list(new_btns)[:5])}")
        if gone_btns:
            changes.append(f"BUTTONS_GONE: {', '.join(list(gone_btns)[:5])}")

        # Clickables count change
        old_click_count = len(before.get('clickables', []))
        new_click_count = len(after.get('clickables', []))
        if abs(new_click_count - old_click_count) >= 2:
            changes.append(f"CLICKABLES: {old_click_count} -> {new_click_count}")

        if not changes:
            changes.append("NO_VISIBLE_CHANGE")

        return "; ".join(changes)

    # ── Passive harvest (code observers + harvest_and_score) ─────────────────

    def _check_observers_and_harvest(self, page) -> str | None:
        """Check code observers + harvest_and_score for codes. Returns first valid code."""
        # 1. Check mutation/bus observers
        try:
            all_codes = page.evaluate(
                '() => window.__getAllCodes ? window.__getAllCodes() : {bus: [], mut: []}'
            )
            for item in all_codes.get('bus', []) + all_codes.get('mut', []):
                code = item.get('c', '')
                if is_valid_code(code):
                    return code
        except Exception:
            pass

        # 2. JS extraction
        code = extract_code_js(page)
        if code:
            return code

        # 3. harvest_and_score
        try:
            last_action_time = page.evaluate('() => window.__lastActionTime || 0')
            score, code = harvest_and_score(page, '', last_action_time)
            if code and score >= 0.5:
                return code
        except Exception:
            pass

        return None

    # ── Claude API ───────────────────────────────────────────────────────────

    def _trim_messages_for_api(self, messages: list) -> list:
        """Keep last 2 full rounds; collapse older to compact summaries.

        Each round = 1 user message (screenshot + JSON) + 1 assistant message.
        For older rounds: drop images, truncate JSON to 200 chars.
        This cuts ~40% API latency on rounds 3+.
        """
        if len(messages) <= 4:  # 2 rounds = 4 messages
            return messages
        trimmed = []
        for i, msg in enumerate(messages):
            if i < len(messages) - 4:
                if msg['role'] == 'user':
                    # Extract text-only content, drop images
                    text_parts = []
                    if isinstance(msg['content'], list):
                        for part in msg['content']:
                            if isinstance(part, dict) and part.get('type') == 'text':
                                text_parts.append(part['text'][:200])
                    elif isinstance(msg['content'], str):
                        text_parts.append(msg['content'][:200])
                    trimmed.append({
                        'role': 'user',
                        'content': f"[Round {i//2+1} - screenshot omitted]\n" +
                                   '\n'.join(text_parts)
                    })
                else:
                    # Keep assistant responses (small, ~800 tokens)
                    trimmed.append(msg)
            else:
                trimmed.append(msg)
        return trimmed

    def _call_claude(self, messages: list) -> str | None:
        """Send multi-turn conversation to Claude (with message trimming)."""
        try:
            trimmed = self._trim_messages_for_api(messages)
            response = self.client.messages.create(
                model=self.model,
                max_tokens=800,
                temperature=0.1,
                system=SYSTEM_PROMPT,
                messages=trimmed,
            )
            # Token tracking
            if hasattr(response, 'usage') and response.usage:
                u = response.usage
                inp = getattr(u, 'input_tokens', 0)
                out = getattr(u, 'output_tokens', 0)
                self._total_input_tokens = getattr(self, '_total_input_tokens', 0) + inp
                self._total_output_tokens = getattr(self, '_total_output_tokens', 0) + out
                log_stage("vision_learn", f"tokens: {inp} in / {out} out "
                          f"(cumulative: {self._total_input_tokens} in / {self._total_output_tokens} out)")
            return response.content[0].text
        except Exception as e:
            log_stage("vision_learn", f"Claude error: {e}")
            return None

    # ── JSON response parsing ────────────────────────────────────────────────

    def _parse_json_response(self, text: str) -> dict | None:
        """Parse strict JSON response. Handles markdown fences and minor issues."""
        text = text.strip()

        # Strip markdown code fences if present
        if text.startswith('```'):
            first_newline = text.index('\n') if '\n' in text else len(text)
            text = text[first_newline + 1:]
            if text.rstrip().endswith('```'):
                text = text.rstrip()[:-3].rstrip()

        # Try direct JSON parse
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return self._validate_parsed(result)
        except json.JSONDecodeError:
            pass

        # Fallback: find first { ... } block
        brace_start = text.find('{')
        if brace_start == -1:
            log_stage("vision_learn", "no JSON object found in response")
            return None

        depth = 0
        for i in range(brace_start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    try:
                        result = json.loads(text[brace_start:i+1])
                        if isinstance(result, dict):
                            return self._validate_parsed(result)
                    except json.JSONDecodeError:
                        break

        log_stage("vision_learn", "JSON parse failed, attempting freeform extraction")
        return self._parse_freeform_fallback(text)

    def _validate_parsed(self, result: dict) -> dict:
        """Validate and normalize parsed JSON response."""
        actions = result.get('actions', [])
        if not isinstance(actions, list):
            actions = []
        valid_actions = []
        for a in actions:
            if isinstance(a, dict) and 'type' in a:
                valid_actions.append(a)
        # Enforce max actions per round (Invariant E)
        result['actions'] = valid_actions[:MAX_ACTIONS_PER_ROUND]

        code = result.get('code', 'NONE')
        if not isinstance(code, str):
            code = 'NONE'
        result['code'] = code.strip().upper()

        return result

    def _parse_freeform_fallback(self, text: str) -> dict | None:
        """Extract actions from freeform text if JSON parsing fails entirely."""
        actions = []
        code = 'NONE'

        for line in text.split('\n'):
            line = line.strip().upper()
            if line.startswith('CLICK') and len(line.split()) >= 3:
                parts = line.split()
                try:
                    actions.append({'type': 'click', 'x': float(parts[1]), 'y': float(parts[2])})
                except (ValueError, IndexError):
                    pass
            elif line.startswith('HOVER') and len(line.split()) >= 3:
                parts = line.split()
                try:
                    secs = float(parts[3]) if len(parts) >= 4 else 2
                    actions.append({'type': 'hover', 'x': float(parts[1]), 'y': float(parts[2]),
                                    'seconds': secs})
                except (ValueError, IndexError):
                    pass
            elif line.startswith('SCROLL') and len(line.split()) >= 3:
                parts = line.split()
                try:
                    actions.append({'type': 'scroll', 'direction': parts[1].lower(),
                                    'amount': int(parts[2])})
                except (ValueError, IndexError):
                    pass
            elif line.startswith('WAIT') and len(line.split()) >= 2:
                parts = line.split()
                try:
                    actions.append({'type': 'wait', 'seconds': float(parts[1])})
                except (ValueError, IndexError):
                    pass

        code_match = re.search(rf'\b([{CHARSET}]{{6}})\b', text)
        if code_match:
            candidate = code_match.group(1)
            if candidate not in FALSE_POS_CODES:
                code = candidate

        if actions:
            return {'actions': actions[:MAX_ACTIONS_PER_ROUND], 'code': code,
                    'observation': '', 'reasoning': ''}
        return None

    # ── Action execution with hit verification ───────────────────────────────

    def _execute_action(self, page, action: dict, viewport_state: dict) -> dict:
        """Execute a single action and return hit verification info.

        Uses direct Playwright calls (same primitives as RecipeExecutor._execute_step)
        to ensure identical behavior. The flight recorder enriches with locator cascade.
        """
        action_type = action.get('type', '').lower()
        hit_info = {'type': action_type, 'executed': False}

        # BID resolution: convert element_id to x,y coordinates
        if 'element_id' in action:
            bid = action['element_id']
            try:
                box = page.locator(f'[data-bid="{bid}"]').bounding_box(timeout=2000)
                if box:
                    action['x'] = box['x'] + box['width'] / 2
                    action['y'] = box['y'] + box['height'] / 2
                    hit_info['resolved_bid'] = bid
                else:
                    log_stage("vision_learn", f"BID {bid} not found, skipping")
                    return hit_info
            except Exception as e:
                log_stage("vision_learn", f"BID {bid} resolution failed: {e}")
                return hit_info

        # Action validity gate — catch corruption before wasting execution
        from primitives import validate_action
        valid, reason = validate_action(action)
        if not valid:
            log_stage("vision_learn", f"action rejected by validity gate: {reason}")
            return hit_info

        try:
            if action_type == 'click':
                x, y = float(action.get('x', 0)), float(action.get('y', 0))
                # Clamp to viewport
                if not (0 <= x <= viewport_state['viewport_w'] and
                        0 <= y <= viewport_state['viewport_h']):
                    log_stage("vision_learn",
                              f"click ({x},{y}) outside viewport, clamping")
                    x = max(0, min(x, viewport_state['viewport_w'] - 1))
                    y = max(0, min(y, viewport_state['viewport_h'] - 1))

                # Hit test BEFORE click
                before_hit = page.evaluate(f'''() => {{
                    const el = document.elementFromPoint({x}, {y});
                    if (!el) return null;
                    return {{
                        tag: el.tagName,
                        text: (el.textContent || '').trim().substring(0, 40),
                        classes: (el.className?.toString() || '').substring(0, 60),
                        isAnchor: !!el.closest('a[href]')
                    }};
                }}''')

                if before_hit and before_hit.get('isAnchor'):
                    log_stage("vision_learn", f"blocked click at ({x},{y}) -- anchor tag")
                    hit_info['blocked'] = 'anchor'
                    return hit_info

                log_stage("vision_learn",
                          f"click ({x},{y}) -> {before_hit['tag'] if before_hit else '?'}"
                          f" \"{before_hit['text'][:25] if before_hit else ''}\"")

                url_before = page.url
                page.mouse.click(x, y)
                page.wait_for_timeout(200)
                self._check_navigation(page, url_before)

                hit_info.update({
                    'executed': True,
                    'x': x, 'y': y,
                    'hit_tag': before_hit['tag'] if before_hit else None,
                    'hit_text': before_hit['text'][:30] if before_hit else None,
                })

            elif action_type == 'hover':
                x, y = float(action.get('x', 0)), float(action.get('y', 0))
                seconds = min(float(action.get('seconds', 2)), 5)
                log_stage("vision_learn", f"hover ({x},{y}) for {seconds}s")
                page.mouse.move(x, y, steps=5)
                polls = int(seconds * 4)
                for _ in range(polls):
                    page.wait_for_timeout(250)
                    code = self._check_observers_and_harvest(page)
                    if code:
                        hit_info['found_code_during'] = True
                        break
                hit_info['executed'] = True

            elif action_type == 'scroll':
                direction = action.get('direction', 'down')
                amount = min(int(action.get('amount', 300)), 2000)
                delta = amount if direction == 'down' else -amount
                log_stage("vision_learn", f"scroll {direction} {amount}px")
                page.evaluate(f'window.scrollBy(0, {delta})')
                page.wait_for_timeout(300)
                hit_info['executed'] = True

            elif action_type == 'type':
                text = str(action.get('text', ''))
                log_stage("vision_learn", f"type '{text[:30]}'")
                page.keyboard.type(text)
                hit_info['executed'] = True

            elif action_type == 'press':
                keys = str(action.get('keys', ''))
                keys = keys.replace('Ctrl', 'Control').replace('ctrl', 'Control')
                keys = keys.replace('cmd', 'Meta').replace('Cmd', 'Meta')
                log_stage("vision_learn", f"press {keys}")
                page.keyboard.press(keys)
                page.wait_for_timeout(300)
                hit_info['executed'] = True

            elif action_type == 'wait':
                seconds = min(float(action.get('seconds', 1)), 10)
                log_stage("vision_learn", f"wait {seconds}s")
                page.wait_for_timeout(int(seconds * 1000))
                hit_info['executed'] = True

            elif action_type == 'drag':
                # Resolve source_id/target_id BIDs to coordinates
                src_id = action.get('source_id')
                tgt_id = action.get('target_id')
                if src_id is not None:
                    try:
                        src_box = page.locator(f'[data-bid="{src_id}"]').bounding_box(timeout=2000)
                        if src_box:
                            action['x1'] = src_box['x'] + src_box['width'] / 2
                            action['y1'] = src_box['y'] + src_box['height'] / 2
                        else:
                            log_stage("vision_learn", f"drag source BID {src_id} not found")
                            return hit_info
                    except Exception as e:
                        log_stage("vision_learn", f"drag source BID {src_id} failed: {e}")
                        return hit_info
                if tgt_id is not None:
                    try:
                        tgt_box = page.locator(f'[data-bid="{tgt_id}"]').bounding_box(timeout=2000)
                        if tgt_box:
                            action['x2'] = tgt_box['x'] + tgt_box['width'] / 2
                            action['y2'] = tgt_box['y'] + tgt_box['height'] / 2
                        else:
                            log_stage("vision_learn", f"drag target BID {tgt_id} not found")
                            return hit_info
                    except Exception as e:
                        log_stage("vision_learn", f"drag target BID {tgt_id} failed: {e}")
                        return hit_info

                # Validate both endpoints exist and aren't (0,0)
                x1 = action.get('x1')
                y1 = action.get('y1')
                x2 = action.get('x2')
                y2 = action.get('y2')
                if x1 is None or y1 is None or x2 is None or y2 is None:
                    log_stage("vision_learn", "drag: missing source or destination coords")
                    return hit_info
                x1, y1, x2, y2 = float(x1), float(y1), float(x2), float(y2)
                if (abs(x1) < 2 and abs(y1) < 2) or (abs(x2) < 2 and abs(y2) < 2):
                    log_stage("vision_learn", f"drag: rejecting near-origin coords — src=({x1},{y1}) dst=({x2},{y2})")
                    return hit_info

                log_stage("vision_learn", f"drag ({x1},{y1}) -> ({x2},{y2})")
                page.mouse.move(x1, y1)
                page.wait_for_timeout(100)
                page.mouse.down()
                page.wait_for_timeout(100)
                page.mouse.move(x2, y2, steps=10)
                page.wait_for_timeout(100)
                page.mouse.up()
                hit_info['executed'] = True

            elif action_type == 'double_click':
                x, y = float(action.get('x', 0)), float(action.get('y', 0))
                log_stage("vision_learn", f"double_click ({x},{y})")
                page.mouse.dblclick(x, y)
                hit_info['executed'] = True

            elif action_type == 'focus':
                bid = action.get('element_id')
                if bid is not None:
                    try:
                        page.locator(f'[data-bid="{bid}"]').first.click(timeout=2000)
                        page.wait_for_timeout(50)
                        hit_info['executed'] = True
                    except Exception as e:
                        log_stage("vision_learn", f"focus BID {bid} failed: {e}")
                else:
                    x, y = float(action.get('x', 0)), float(action.get('y', 0))
                    page.mouse.click(x, y)
                    page.wait_for_timeout(50)
                    hit_info['executed'] = True

            elif action_type == 'element_scroll':
                # Scroll inside a specific element (not the page)
                bid = action.get('element_id')
                direction = action.get('direction', 'down')
                amount = min(int(action.get('amount', 300)), 2000)
                v_delta = amount if direction == 'down' else -amount
                if bid is not None:
                    try:
                        box = page.locator(f'[data-bid="{bid}"]').bounding_box(timeout=2000)
                        if box:
                            cx = box['x'] + box['width'] / 2
                            cy = box['y'] + box['height'] / 2
                            page.mouse.move(cx, cy)
                            page.mouse.wheel(0, v_delta)
                            log_stage("vision_learn", f"element_scroll {direction} {amount}px on BID {bid}")
                            hit_info['executed'] = True
                    except Exception as e:
                        log_stage("vision_learn", f"element_scroll BID {bid} failed: {e}")
                else:
                    log_stage("vision_learn", f"element_scroll {direction} {amount}px (page)")
                    page.evaluate(f'window.scrollBy(0, {v_delta})')
                    hit_info['executed'] = True
                page.wait_for_timeout(200)

            elif action_type in ('draw', 'canvas_draw'):
                from primitives import draw_stroke_on_canvas
                path = action.get('path', [])
                bid = action.get('element_id')
                canvas_ref = bid if bid is not None else 'canvas'
                log_stage("vision_learn",
                          f"draw {len(path)} points on "
                          f"{'BID ' + str(bid) if bid else 'canvas'}")
                result = draw_stroke_on_canvas(page, canvas_ref, path)
                hit_info['executed'] = result.get('success', False)
                hit_info['draw_result'] = result

            elif action_type == 'wait_for_state':
                from primitives import wait_for_state
                change_types = set(action.get('change_types',
                                              ['enabled', 'appeared', 'new_element',
                                               'became_clickable', 'activated']))
                timeout = min(int(action.get('timeout_ms', 3000)), 5000)
                log_stage("vision_learn",
                          f"wait_for_state types={change_types} timeout={timeout}ms")
                result = wait_for_state(
                    page, change_types=change_types, timeout_ms=timeout,
                )
                hit_info['executed'] = True
                if result:
                    hit_info['state_change'] = result
                    log_stage("vision_learn",
                              f"  state change: {','.join(result.get('changes',[]))}"
                              f" \"{result.get('text','')}\"")

            else:
                log_stage("vision_learn", f"unknown action type: {action_type}")

        except Exception as e:
            log_stage("vision_learn", f"action error ({action_type}): {e}")
            hit_info['error'] = str(e)

        return hit_info

    def _check_navigation(self, page, url_before: str):
        """If we accidentally navigated away, go back."""
        url_after = page.url
        if url_after == url_before:
            return
        if '/step' in url_after and 'version=' in url_after:
            return
        log_stage("vision_learn", f"accidental navigation to {url_after[:60]}, recovering...")
        try:
            page.go_back()
            page.wait_for_timeout(500)
            log_stage("vision_learn", f"recovered to {page.url[:60]}")
        except Exception as e:
            log_stage("vision_learn", f"recovery failed: {e}")
