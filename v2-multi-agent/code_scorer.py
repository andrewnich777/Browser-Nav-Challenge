"""
Code Scoring System - Multi-factor decoy detection

Scores candidate codes 0-1 based on:
- Entropy (random codes have high entropy)
- Dictionary check (known words = instant reject)
- Character distribution (letters + digits mix)
- Frequency (unique = good, repeated = bad)
- Context signals (on-screen, near instruction, highlighted, monospace)
- Recency (appeared after action = strong signal)
- Style signals (green/activated = preference, red = penalty)
- Temporal stability (persists across polls = stronger)
- Causality (appeared after meaningful state change = stronger)
"""

import re
import time
from math import log2
from collections import Counter
from typing import Optional


# ── Temporal Stability Tracker ──────────────────────────────────────────
# Tracks code persistence across harvest polls. Codes that persist > 400ms
# score higher than those that appear briefly (churn = likely decoy).

_code_tracker: dict[str, dict] = {}  # {code: {first_seen, last_seen, times_seen}}
_tracker_last_cleanup = 0.0


def _track_code(code: str) -> dict:
    """Record a code sighting and return its temporal stats."""
    global _tracker_last_cleanup
    now = time.time()

    # Cleanup stale entries every 30s
    if now - _tracker_last_cleanup > 30:
        cutoff = now - 60  # Forget codes older than 60s
        stale = [c for c, v in _code_tracker.items() if v['last_seen'] < cutoff]
        for c in stale:
            del _code_tracker[c]
        _tracker_last_cleanup = now

    if code in _code_tracker:
        entry = _code_tracker[code]
        entry['last_seen'] = now
        entry['times_seen'] += 1
    else:
        entry = {'first_seen': now, 'last_seen': now, 'times_seen': 1}
        _code_tracker[code] = entry

    return entry


def get_code_stability(code: str) -> float:
    """Return 0-1 stability score. Higher = code persisted longer."""
    entry = _code_tracker.get(code)
    if not entry:
        return 0.0
    duration = entry['last_seen'] - entry['first_seen']
    # Normalize: 0ms=0.0, 400ms=0.5, 800ms+=1.0
    return min(1.0, duration / 0.8)


def reset_code_tracker():
    """Reset between steps so codes from step N don't bleed into step N+1."""
    _code_tracker.clear()


# ── Recent State Change Tracker ─────────────────────────────────────────
# Tracks recent meaningful state changes so we can compute causality score.

_recent_state_changes: list[dict] = []  # [{type, timestamp}, ...]


def note_state_change(change_type: str):
    """Record that a meaningful state change happened now."""
    _recent_state_changes.append({'type': change_type, 'timestamp': time.time()})
    # Keep only last 20
    if len(_recent_state_changes) > 20:
        _recent_state_changes.pop(0)


def get_causality_score(code: str, window_s: float = 1.5) -> float:
    """Return 0-1 score: how close this code's first appearance is to a state change."""
    entry = _code_tracker.get(code)
    if not entry or not _recent_state_changes:
        return 0.0
    code_time = entry['first_seen']
    # Find the closest state change BEFORE the code appeared
    best_gap = float('inf')
    for sc in _recent_state_changes:
        gap = code_time - sc['timestamp']
        if 0 <= gap < best_gap:
            best_gap = gap
    if best_gap == float('inf') or best_gap > window_s:
        return 0.0
    # Closer = higher score. 0ms=1.0, window_s=0.0
    return max(0.0, 1.0 - best_gap / window_s)


def reset_state_change_tracker():
    """Reset between steps."""
    _recent_state_changes.clear()

# Valid charset for codes (no I, O, 0, 1 to avoid confusion)
CHARSET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
CHARSET_SET = set(CHARSET)

# Unified decoy set — charset-valid only (entries with I, O, 0, 1 are excluded
# since they can NEVER match the charset regex and don't need filtering).
# This is the single source of truth for all decoy filtering across the codebase.
DECOY_CODES = {
    # Common UI words (charset-valid only: no I, O, 0, 1; exactly 6 chars)
    'PUNYYR', 'SBYZBJ', 'CANVAS', 'SEARCH', 'EXPAND', 'ENABLE',
    'CHANGE', 'SAMPLE', 'PLEASE', 'SCREEN', 'PUZZLE',
    'RESULT', 'STATUS', 'ANSWER',
    # ROT13 variants (charset-valid only)
    'PBZCYR', 'FVZCYR', 'ENAQBZ', 'FNZCYR', 'CYRNFR', 'PUEBZR',
    'JVAQBJ', 'FPERRA', 'CEBCRQ', 'CEBPRQ', 'VAFCRP', 'RYRZRA',
    'UVQQRA', 'FBZRJU',
    # Truncated challenge words (charset-valid only)
    'ADVANC', 'CHALLE', 'CHARAC',
}
# Filter to charset-valid only (safety check — all entries above should already pass)
DECOY_CODES = {c for c in DECOY_CODES if len(c) == 6 and all(ch in CHARSET_SET for ch in c)}

def calculate_entropy(code: str) -> float:
    """
    Calculate normalized Shannon entropy of the code.
    Higher entropy = more random = more likely to be real.

    Returns 0-1 where 1 = maximum entropy (all chars unique).
    """
    if not code:
        return 0.0

    # Count character frequencies
    freq = Counter(code)
    total = len(code)

    # Calculate entropy
    entropy = 0.0
    for count in freq.values():
        p = count / total
        if p > 0:
            entropy -= p * log2(p)

    # Maximum entropy for 6 chars = log2(6) ≈ 2.58
    # Normalize to 0-1
    max_entropy = log2(len(code)) if len(code) > 1 else 1
    return entropy / max_entropy if max_entropy > 0 else 0.0


def score_candidate(code: str, context: Optional[dict] = None) -> float:
    """
    Score a code candidate from 0 to 1, where 1 = most likely real code.

    Args:
        code: The 6-character candidate code
        context: Optional dict with context signals:
            - frequency: How many times this code appears on page
            - on_screen: Is it visible in viewport?
            - near_instruction: Is it near instruction text?
            - is_highlighted: Does it have background color?
            - is_monospace: Is it in monospace font?
            - appeared_after_action: Did it appear after user action?
            - color_bucket: 'green'|'red'|'grey'|'blue'|'yellow'|'other'|'none'
            - is_vibrant: bool (high saturation, not grey)

    Returns:
        Score from 0.0 to 1.0
    """
    if context is None:
        context = {}

    # === HARD FILTERS (instant reject) ===

    # Wrong length
    if len(code) != 6:
        return 0.0

    # Invalid characters
    if not all(c in CHARSET_SET for c in code):
        return 0.0

    # Dictionary word (known decoy)
    if code in DECOY_CODES:
        return 0.0

    # === SOFT SCORING ===
    score = 0.5  # Start neutral

    # --- NEGATIVE SIGNALS ---

    # All numeric (very suspicious)
    if code.isdigit():
        score -= 0.4

    # Low character diversity (e.g., "AAAAAA", "ABABAB")
    unique_chars = len(set(code))
    if unique_chars <= 2:
        score -= 0.3
    elif unique_chars <= 3:
        score -= 0.15

    # Repeated patterns (e.g., "ABCABC")
    if code[:3] == code[3:]:
        score -= 0.2

    # Appears multiple times on page (decoy signal)
    # NOTE: frequency is currently always 1 due to JS-side dedup in harvest.
    # Penalty kept for future use if frequency tracking is added.
    frequency = context.get('frequency', 1)
    if frequency > 1:
        score -= 0.15 * min(frequency - 1, 3)  # Cap penalty

    # Looks like a word (consonant-vowel patterns)
    vowels = sum(1 for c in code if c in 'AEUY')
    if vowels >= 2 and vowels <= 3:  # Word-like pattern
        score -= 0.1

    # Red/error color penalty (soft — decoys are often red)
    color_bucket = context.get('color_bucket', 'none')
    if color_bucket == 'red':
        score -= 0.15

    # --- POSITIVE SIGNALS ---

    # Entropy check (random codes have high entropy)
    entropy = calculate_entropy(code)
    score += entropy * 0.25  # Up to +0.25 for perfect entropy

    # Good letter/digit mix (typical of real codes)
    letters = sum(1 for c in code if c.isalpha())
    digits = sum(1 for c in code if c.isdigit())
    if 2 <= letters <= 5 and 1 <= digits <= 4:
        score += 0.15

    # Unique on page — inert when frequency is always 1 (JS dedup).
    # Removed: was giving +0.15 to ALL candidates uniformly.
    # if frequency == 1:
    #     score += 0.15

    # Visible on screen
    if context.get('on_screen', False):
        score += 0.15

    # Near instruction text
    if context.get('near_instruction', False):
        score += 0.15

    # Highlighted (background color)
    if context.get('is_highlighted', False):
        score += 0.1

    # Monospace font (code-like presentation)
    if context.get('is_monospace', False):
        score += 0.1

    # Appeared after user action (very strong signal)
    if context.get('appeared_after_action', False):
        score += 0.25

    # Green/success color — strong signal (memory challenges show real code in green box)
    if color_bucket == 'green':
        score += 0.30

    # Vibrancy boost (saturated color = intentional styling = likely real)
    if context.get('is_vibrant', False):
        score += 0.05

    # Temporal stability (persists across polls = more likely real)
    stability = get_code_stability(code)
    score += stability * 0.20  # Up to +0.20 for 800ms+ persistence

    # Causality (appeared right after a state change = strong signal)
    causality = get_causality_score(code)
    score += causality * 0.15  # Up to +0.15 for immediate post-change

    # Clamp to [0, 1]
    return max(0.0, min(1.0, score))


def harvest_and_score(page, instruction_text: str = '', last_action_time: int = 0):
    """
    Harvest all candidate codes from page with context and return best one.

    Args:
        page: Playwright page object
        instruction_text: The challenge instruction for context
        last_action_time: Timestamp of last user action (for recency check)

    Returns:
        Tuple of (score, code) for the best candidate, or (0, None) if none found
    """
    # Harvest candidates with their context from the page
    candidates = page.evaluate(r'''(lastActionTime) => {
        const RE = /\b[A-HJ-NP-Z2-9]{6}\b/g;
        const results = [];
        const seen = new Set();

        // Color bucket classifier (matches init_hooks._colorCat)
        function colorCat(colorStr) {
            const m = (colorStr || '').match(/[\d.]+/g);
            if (!m || m.length < 3) return 'none';
            const r = +m[0], g = +m[1], b = +m[2];
            const a = m.length >= 4 ? (+m[3] > 1 ? +m[3]/255 : +m[3]) : 1;
            if (a < 0.1) return 'none';
            const mx = Math.max(r,g,b), mn = Math.min(r,g,b);
            const l = (mx+mn)/2, d = mx-mn;
            if (d < 30 || l < 30 || l > 240) return 'grey';
            if (g > r * 1.3 && g > b * 1.3) return 'green';
            if (r > g * 1.3 && r > b * 1.3) return 'red';
            if (b > r * 1.3 && b > g * 1.3) return 'blue';
            if (r > 180 && g > 140 && b < 100) return 'yellow';
            return 'other';
        }

        function isVibrant(colorStr) {
            const m = (colorStr || '').match(/[\d.]+/g);
            if (!m || m.length < 3) return false;
            const r = +m[0], g = +m[1], b = +m[2];
            const mx = Math.max(r,g,b), mn = Math.min(r,g,b);
            const d = mx - mn;
            const l = (mx+mn)/2;
            return d > 60 && l > 40 && l < 220;  // High saturation, not too dark/light
        }

        // Walk through text nodes to find codes with context
        const walker = document.createTreeWalker(
            document.body,
            NodeFilter.SHOW_TEXT,
            null,
            false
        );

        let node;
        while ((node = walker.nextNode())) {
            const text = node.nodeValue;
            if (!text || text.length < 6) continue;

            const matches = text.match(RE);
            if (!matches) continue;

            const el = node.parentElement;
            if (!el) continue;

            const rect = el.getBoundingClientRect();
            const cs = getComputedStyle(el);

            // Check both element and ancestors for color (codes often in <span> inside colored <div>)
            let bgColor = 'none', fgColor = 'none', vibrant = false;
            let cur = el;
            for (let depth = 0; depth < 4 && cur && cur !== document.body; depth++) {
                const curCs = depth === 0 ? cs : getComputedStyle(cur);
                const bg = colorCat(curCs.backgroundColor);
                const fg = colorCat(curCs.color);
                if (bg !== 'none' && bgColor === 'none') bgColor = bg;
                if (fg !== 'none' && fgColor === 'none') fgColor = fg;
                if (!vibrant && (isVibrant(curCs.backgroundColor) || isVibrant(curCs.color)))
                    vibrant = true;
                cur = cur.parentElement;
            }

            for (const code of matches) {
                if (seen.has(code)) continue;
                // Centralized validation: filter decoys, used codes, bad charset
                if (window.__isValidCode && !window.__isValidCode(code)) continue;
                seen.add(code);

                results.push({
                    code,
                    onScreen: rect.bottom > 0 && rect.top < window.innerHeight,
                    isHighlighted: cs.backgroundColor !== 'rgba(0, 0, 0, 0)' &&
                                   cs.backgroundColor !== 'transparent',
                    isMonospace: cs.fontFamily.toLowerCase().includes('mono') ||
                                 cs.fontFamily.toLowerCase().includes('courier'),
                    nearInstruction: (() => {
                        // Check only immediate parent's own text (not all descendants)
                        const pt = (el.childNodes.length < 20)
                            ? (el.textContent || '').substring(0, 200).toLowerCase()
                            : '';
                        return pt.includes('code') || pt.includes('enter');
                    })(),
                    colorBucket: (bgColor !== 'none' && bgColor !== 'grey') ? bgColor : (fgColor !== 'none' ? fgColor : bgColor),
                    isVibrant: vibrant
                });
            }
        }

        // Also check code bus for recently appeared codes
        const busCodes = (window.__codeBus || []);
        const mutCodes = (window.__mutCodes || []);

        for (const item of [...busCodes, ...mutCodes]) {
            if (!seen.has(item.c)) {
                if (window.__isValidCode && !window.__isValidCode(item.c)) continue;
                seen.add(item.c);
                results.push({
                    code: item.c,
                    onScreen: true,
                    isHighlighted: false,
                    isMonospace: false,
                    nearInstruction: false,
                    appearedAfterAction: item.t > lastActionTime,
                    colorBucket: 'none',
                    isVibrant: false
                });
            }
        }

        return results;
    }''', last_action_time)

    if not candidates:
        return (0.0, None)

    # Note: JS-side deduplication via `seen` set means each code appears at most
    # once in candidates, so frequency is always 1.

    # Track all codes for temporal stability
    for c in candidates:
        _track_code(c['code'])

    # Score each candidate
    scored = []
    for c in candidates:
        ctx = {
            'frequency': 1,
            'on_screen': c.get('onScreen', False),
            'near_instruction': c.get('nearInstruction', False) or
                               (instruction_text and c['code'] in instruction_text),
            'is_highlighted': c.get('isHighlighted', False),
            'is_monospace': c.get('isMonospace', False),
            'appeared_after_action': c.get('appearedAfterAction', False),
            'color_bucket': c.get('colorBucket', 'none'),
            'is_vibrant': c.get('isVibrant', False),
        }
        s = score_candidate(c['code'], ctx)
        scored.append((s, c['code']))

    # Sort by score descending
    scored.sort(reverse=True, key=lambda x: x[0])

    # Return best if above threshold
    if scored and scored[0][0] > 0.4:
        return scored[0]

    return (0.0, None)


def is_static_decoy(code: str) -> bool:
    """Soft signal — code matches a known decoy word. Can be overridden by DNA confidence."""
    return code in DECOY_CODES


def is_valid_code_hard(code: str, used_codes: set = None) -> bool:
    """Hard validation: charset + length + used-in-session. No decoy filter."""
    if len(code) != 6:
        return False
    if not all(c in CHARSET_SET for c in code):
        return False
    if used_codes and code in used_codes:
        return False
    return True


def is_valid_code(code: str, used_codes: set = None) -> bool:
    """Soft validation (default): hard + decoy filter.

    Args:
        code: The candidate code
        used_codes: Optional set of already-used codes to reject

    Returns:
        True if it could be valid, False if it's definitely a decoy or invalid
    """
    if not is_valid_code_hard(code, used_codes):
        return False
    if is_static_decoy(code):
        return False
    return True
