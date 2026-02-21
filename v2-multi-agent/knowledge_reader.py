"""
Knowledge Reader V2 — One canonical learning per challenge type with bounded variants.

Replaces the old accumulate-many-learnings JSONL system with:
- CanonicalLearning (1 per challenge type) with bounded StrategyVariant list (max 3)
- Wilson score as sole confidence metric (no manual decay)
- Scored detection router: 0.25*kw + 0.10*flags + 0.15*text_ctx + 0.10*dom + 0.10*dna + 0.20*fp
- DOM change score (float, not bool) for failure diagnosis
- Multi-level rollback with previous_versions snapshots
- TTL-based disable (reversible circuit breaker)
- Atomic JSON writes via os.replace + fsync
- One-time JSONL → JSON migration with scoped sanitization
"""

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from log import log, log_stage


# Default router priorities (higher = matched first, resolves keyword overlap)
DEFAULT_PRIORITIES = {
    "drag_drop": 90,
    "keyboard_sequence": 85,
    "hover": 80,
    "draw": 75,
    "hidden_dom": 70,
    "shadow_dom": 65,
    "decode": 60,
    "click_reveal": 50,
    "click": 50,
    "scroll": 40,
    "delay_memory": 35,
    "delay": 35,
    "checkbox": 30,
    "radio": 30,
    "radio_modal": 30,
    "audio": 25,
    "websocket": 20,
    "enable_buttons": 15,
}

# Challenge type name normalization
TYPE_ALIASES = {
    "drag_and_drop": "drag_drop",
    "radio_modal_scroll": "radio_modal",
    "click_n_times": "hidden_dom",
    "delay": "delay_memory",
    "delayed_reveal": "delay_memory",
}

# ─── Wilson score ─────────────────────────────────────────────────────────────

def wilson_score_lower(successes: int, total: int, z: float = 1.0) -> float:
    """Wilson score lower bound — sole confidence metric.

    z=1.0 (~84% CI) is less conservative than the traditional z=1.96 (95% CI).
    With z=1.0: 1/1→0.29, 3/3→0.50, 5/5→0.63 (new learnings aren't crushed).
    """
    if total == 0:
        return 0.0
    p = successes / total
    denominator = 1 + z * z / total
    centre = p + z * z / (2 * total)
    adj_std = math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return max(0.0, (centre - z * adj_std) / denominator)


# ─── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class StrategyVariant:
    """One strategy variant within a canonical learning."""
    variant_id: str                          # e.g. "hover_card", "hover_shadow_hold"
    preconditions: list[dict] = field(default_factory=list)  # DOM signals + instruction regex
    suggested_action: str = ""               # Human-readable description
    action_type: str = "unknown"             # scroll/click/hover/wait/decode/drag_drop/...
    action_params: dict = field(default_factory=dict)

    # Per-variant confidence (Wilson score)
    confidence: float = 0.5
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    verified: bool = False

    # Per-variant rollback
    version: int = 1
    previous_versions: list[dict] = field(default_factory=list)  # Last 2-3 snapshots

    # Per-variant failure history
    failure_history: list[dict] = field(default_factory=list)    # Last 5
    failure_patterns: dict = field(default_factory=dict)         # Grouped: {"timeout": 3, ...}

    # System 1/2: Action recipe + DNA signature (variant-scoped)
    action_recipe: list[dict] = field(default_factory=list)          # Ordered ActionSteps for System 1 replay
    successful_dna_signature: dict = field(default_factory=dict)     # Winning DNA from last success
    page_context: dict = field(default_factory=dict)                 # Text patterns from success

    # Provenance fields (V3)
    created_by: str = "system2"                # "system2" | "manual" | "migrated"
    replay_attempts: int = 0                   # System 1 replay count
    replay_successes: int = 0                  # System 1 replay successes
    last_replay_error: str | None = None       # Last System 1 error message
    assertions_present: bool = False           # Recipe has at least one assertion
    minimized: bool = False                    # Recipe was delta-debug minimized
    avg_runtime_ms: float = 0.0                # Average replay time

    # Compounding tier system (V4)
    tier: int = 0                              # 0=no recipe, 1=soft, 2=confirmed, 3=hardened
    recipe_ttl: int = 5                        # Tier 1: replays remaining before expiry
    non_replayable: bool = False               # Session-specific data — skip System 1 replay

    def snapshot(self) -> dict:
        """Create a rollback snapshot of current state."""
        return {
            "version": self.version,
            "suggested_action": self.suggested_action,
            "action_type": self.action_type,
            "action_params": dict(self.action_params),
            "preconditions": list(self.preconditions),
            "confidence": self.confidence,
            "attempts": self.attempts,
            "successes": self.successes,
            "failures": self.failures,
        }

@dataclass
class CanonicalLearning:
    """One canonical learning per challenge type."""
    challenge_type: str                      # Key: "hover", "drag_drop", etc.
    detection_keywords: list[str] = field(default_factory=list)   # Regex patterns
    router_priority: int = 50                # Higher = matched first

    # Bounded variants (max 3 per type)
    variants: list[StrategyVariant] = field(default_factory=list)
    active_variant_id: str | None = None

    # DOM context (for match scoring)
    dom_signals: list[dict] = field(default_factory=list)

    # Type-level tracking
    last_refinement_reason: str = ""
    last_match_score: float = 0.0

    # Circuit breaker (reversible)
    disabled_until: str | None = None
    disable_reason: str | None = None

    # Metadata
    created_at: str = ""
    last_updated: str = ""
    last_succeeded: str | None = None

    # System 1/2: Aggregated DNA signatures + text context (type-level hints)
    dna_signatures: list[dict] = field(default_factory=list)         # Aggregated DNA (type-level)
    page_text_context: dict = field(default_factory=dict)            # Aggregated text patterns
    dom_fingerprint: dict = field(default_factory=dict)              # DOM fingerprint for detection
    confused_with: list = field(default_factory=list)               # Confusion history [{actual_type, count, last_seen}]

    def get_active_variant(self) -> Optional[StrategyVariant]:
        """Get the active variant, or the highest-confidence one."""
        if self.active_variant_id:
            for v in self.variants:
                if v.variant_id == self.active_variant_id:
                    return v
        # Fallback: highest confidence
        if self.variants:
            return max(self.variants, key=lambda v: v.confidence)
        return None

    def is_disabled(self) -> bool:
        """Check if learning is currently disabled."""
        if not self.disabled_until:
            return False
        try:
            until = datetime.fromisoformat(self.disabled_until)
            return datetime.now() < until
        except (ValueError, TypeError):
            return False


# ─── Detection patterns (comprehensive, from retrieval.py's InstructionMatcher) ──

DETECTION_PATTERNS: list[tuple[str, list[tuple[str, float]]]] = [
    # (challenge_type, [(regex, weight), ...])
    ("hidden_dom", [
        (r"hidden\s*dom", 1.0),
        (r"click\s*here.*times?\s*to\s*reveal", 0.9),
        (r"click.*\d+\s*more\s*times", 0.9),
        (r"inspect\s*the\s*dom", 0.8),
    ]),
    ("drag_drop", [
        (r"drag.*drop", 1.0),
        (r"fill.*slots", 0.9),
        (r"available\s*pieces", 0.8),
        (r"drag.*piece", 0.8),
        (r"slot\s*\d.*slot\s*\d", 0.7),
    ]),
    ("hover", [
        (r"hover\s*here", 1.0),
        (r"hover\s*over", 0.9),
        (r"hover\s*to\s*reveal", 0.9),
        (r"hover.*for.*second", 0.8),
        (r"hover.*box", 0.7),
    ]),
    ("radio_modal", [
        (r"please\s*select\s*an?\s*option", 1.0),
        (r"select\s*the\s*correct\s*radio", 0.9),
        (r"scrollable\s*modal", 0.8),
        (r"must\s*select.*radio", 0.8),
    ]),
    ("delayed_reveal", [
        (r"delayed\s*reveal", 1.0),
        (r"code\s*will\s*appear\s*after\s*wait", 0.9),
        (r"appear\s*after\s*waiting\s*\d+\s*seconds?", 0.9),
        (r"waiting\.\.\.\s*\d+.*remaining", 0.8),
    ]),
    ("delay_memory", [
        (r"remember.*code", 1.0),
        (r"memorize", 0.9),
        (r"flash.*code", 0.9),
        (r"code.*disappear", 0.8),
        (r"wait.*seconds?.*appear", 0.7),
        (r"memory.*challenge", 0.7),
    ]),
    ("decode", [
        (r"base64", 1.0),
        (r"decode\s*the", 0.9),
        (r"encoded", 0.8),
        (r"rot13", 1.0),
        (r"hex.*decode", 0.9),
        (r"cipher", 0.7),
    ]),
    ("audio", [
        (r"listen.*audio", 1.0),
        (r"play.*audio", 0.9),
        (r"audio.*challenge", 0.8),
        (r"listen.*code", 0.8),
        (r"play.*sound", 0.7),
        (r"hear.*code", 0.7),
    ]),
    ("draw", [
        (r"draw\s+.*(?:line|circle|gesture|shape)", 1.0),
        (r"swipe", 0.8),
        (r"stroke", 0.7),
        (r"canvas.*draw", 0.9),
        (r"draw\s+on.*canvas", 0.9),
    ]),
    ("click_reveal", [
        (r"click.*button.*reveal", 1.0),
        (r"reveal\s*code", 0.8),
        (r"click\s*to\s*reveal", 0.9),
        (r"click\s*the\s*button\s*below", 0.7),
    ]),
    ("scroll", [
        # NOTE: "scroll down" is common decoy text across many challenge types.
        # Only match when it's clearly the primary instruction (with "code"/"reveal").
        (r"scroll\s*down\s*to\s*(?:find|reveal|see)", 1.0),
        (r"scroll\s*to\s*find", 0.8),
        (r"scroll.*reveal.*code", 0.9),
        (r"hidden\s*below\s*the\s*fold", 0.7),
        (r"scroll\s*challenge", 0.9),
    ]),
    ("websocket", [
        (r"websocket", 1.0),
        (r"connect\s*to", 0.6),
    ]),
    ("shadow_dom", [
        (r"shadow\s*dom", 1.0),
        (r"inspect\s*element", 0.6),
    ]),
    ("checkbox", [
        (r"select\s*(?:the\s+)?checkbox", 0.9),
        (r"check\s*all", 0.8),
    ]),
    ("keyboard_sequence", [
        (r"keyboard\s*sequence", 1.0),
        (r"keyboard\s*shortcut", 1.0),
        (r"press.*keys?\s*in\s*sequence", 0.9),
        (r"press.*keys?\s*shown\s*below", 0.9),
        (r"required\s*sequence", 0.8),
        (r"press\s*the\s*following\s*keys?", 0.8),
        (r"press.*ctrl|press.*control", 0.7),
        (r"key\s*combo|key\s*combination", 0.7),
    ]),
    # Learning types that previously had no detection patterns — they couldn't
    # compete with scroll/click_reveal on their own pages.
    ("hidden_dom_click", [
        (r"hidden\s*dom", 1.0),
        (r"click\s*here.*times?\s*to\s*reveal", 0.9),
        (r"click.*\d+\s*more\s*times", 0.9),
        (r"click\s*count", 0.8),  # unique to hidden_dom_click
        (r"clicks?\s*remaining", 0.8),  # unique to hidden_dom_click
    ]),
    ("timing", [
        (r"timing.*challenge", 1.0),
        (r"rotating\s*code", 1.0),
        (r"code\s*changes?\s*every\s*\d+\s*seconds?", 0.9),
        (r"capture.*window", 0.9),
        (r"click.*capture.*\d+\s*times?", 0.9),
        (r"click.*capture", 0.8),
        (r"timing\s*window", 0.8),
        (r"capture\s*\(\d+/\d+\)", 0.7),
    ]),
    ("video", [
        (r"video.*challenge", 1.0),
        (r"seek.*position", 0.9),
        (r"video.*frame", 0.8),
        (r"seek.*forward", 0.7),
    ]),
    ("multi_tab", [
        (r"multi.?tab", 1.0),
        (r"visit.*tab", 0.9),
        (r"click\s*each\s*tab", 0.8),
        (r"tab\s*\d.*tab\s*\d", 0.7),
    ]),
    ("split_parts", [
        (r"split.*parts?", 1.0),
        (r"part\s*\d.*part\s*\d", 0.9),
        (r"combine.*parts?", 0.8),
        (r"code.*split", 0.7),
    ]),
    ("sequence_challenge", [
        (r"sequence\s*challenge", 1.0),
        (r"complete\s*all.*tasks?", 0.9),
        (r"mini.?tasks?", 0.8),
        (r"progress.*\d+\s*/\s*\d+", 0.7),
    ]),
    ("gesture", [
        (r"gesture", 1.0),
        (r"swipe.*direction", 0.9),
        (r"draw.*gesture", 0.9),
        (r"perform.*gesture", 0.8),
        (r"touch.*pattern", 0.7),
    ]),
    ("service_worker", [
        (r"service\s*worker", 1.0),
        (r"background.*fetch", 0.8),
        (r"offline.*challenge", 0.7),
        (r"sw\.js", 0.8),
    ]),
    ("puzzle_solve", [
        (r"puzzle", 0.9),
        (r"solve.*equation", 1.0),
        (r"math.*problem", 0.8),
        (r"\d+\s*[\+\-\*\/]\s*\d+\s*=\s*\?", 1.0),
        (r"calculate", 0.7),
    ]),
    ("mutation", [
        (r"mutation.*challenge", 1.0),
        (r"trigger.*mutation", 0.9),
        (r"dom.*mutation", 0.9),
        (r"observe.*change", 0.7),
    ]),
    ("recursive_iframe", [
        (r"recursive.*iframe", 1.0),
        (r"nested.*iframe", 1.0),
        (r"level\s*\d+\s*/\s*\d+", 0.9),
        (r"navigate.*deeper", 0.8),
        (r"depth\s*\d+", 0.7),
    ]),
    ("conditional_reveal", [
        (r"conditional.*reveal", 1.0),
        (r"conditions?.*met", 0.9),
        (r"satisfy.*conditions?", 0.9),
        (r"requirements?.*reveal", 0.8),
        (r"unlock.*code", 0.7),
    ]),
    ("calculated", [
        (r"calculated?.*challenge", 1.0),
        (r"compute.*result", 0.9),
        (r"perform.*calculation", 0.9),
        (r"arithmetic", 0.8),
        (r"formula", 0.7),
    ]),
]

# DOM flags extraction JS (single evaluate call)
DOM_FLAGS_JS = '''() => {
    const dom = document.body?.innerHTML || '';
    const text = document.body?.innerText || '';
    return {
        has_canvas: !!document.querySelector('canvas'),
        has_shadow: dom.includes('shadowRoot') || !!document.querySelector('[data-shadow]'),
        has_iframe: !!document.querySelector('iframe'),
        has_button: !!document.querySelector('button'),
        has_input: !!document.querySelector('input'),
        has_slider: !!document.querySelector('input[type="range"]'),
        has_checkbox: !!document.querySelector('input[type="checkbox"]'),
        has_radio: !!document.querySelector('input[type="radio"]'),
        has_audio: !!document.querySelector('audio'),
        // NOTE: Unreliable — checks innerHTML for "WebSocket" string, which only
        // matches if the literal text appears in markup, not if a WS connection exists.
        has_ws: typeof WebSocket !== 'undefined' && dom.includes('WebSocket'),
        element_count: document.querySelectorAll('*').length
    };
}'''

# DOM signature JS (structural hash for change detection)
DOM_SIGNATURE_JS = '''() => {
    const parts = [];
    const counts = {};
    document.querySelectorAll('*').forEach(el => {
        const tag = el.tagName;
        counts[tag] = (counts[tag] || 0) + 1;
    });
    parts.push(Object.entries(counts).sort().map(([k,v]) => `${k}:${v}`).join(','));
    const forms = [];
    document.querySelectorAll('input, select, textarea').forEach(el => {
        forms.push(`${el.tagName}:${el.type || 'text'}:${(el.checked || (el.value && el.value.length > 0)) ? '1' : '0'}`);
    });
    parts.push(forms.join(','));
    parts.push(`canvas:${document.querySelector('canvas') ? '1' : '0'}`);
    parts.push(`audio:${document.querySelector('audio') ? '1' : '0'}`);
    return parts.join('|');
}'''

# DOM fingerprint JS — three-component fingerprint for challenge detection
# Excludes popup overlays (z>=9990) and decoy navigation buttons.
DOM_FINGERPRINT_JS = r'''() => {
    // Build exclusion set: fixed overlays + decoy buttons
    const exclude = new Set();
    const decoyRe = /^(Next|Continue|Proceed|Advance|Click Here|Move On|Keep Going|Go Forward|Next Step|Next Page|Continue Journey|Continue Reading|Load|Try This|New Button|Proceed Forward|Next Section)$/i;
    document.querySelectorAll('*').forEach(el => {
        const s = window.getComputedStyle(el);
        if (s.position === 'fixed' && (parseInt(s.zIndex) || 0) >= 9990) {
            exclude.add(el);
            el.querySelectorAll('*').forEach(c => exclude.add(c));
        }
    });
    document.querySelectorAll('button, [role="button"]').forEach(el => {
        if (decoyRe.test((el.textContent || '').trim())) exclude.add(el);
    });
    function notExcluded(el) {
        let p = el;
        while (p && p !== document.body) {
            if (exclude.has(p)) return false;
            p = p.parentElement;
        }
        return true;
    }
    // Element counts — only non-excluded elements
    const countSel = (sel) => Array.from(document.querySelectorAll(sel)).filter(notExcluded).length;
    const fp = {
        buttons: countSel('button'),
        inputs: countSel('input'),
        canvas: countSel('canvas'),
        iframes: countSel('iframe'),
        audio: countSel('audio'),
        video: countSel('video'),
        draggables: countSel('[draggable="true"]'),
        tabs: countSel('[role="tab"]'),
        sliders: countSel('input[type="range"]'),
        checkboxes: countSel('input[type="checkbox"]'),
    };
    // Instruction text — skip generic page title, find challenge-specific text
    // Look for text containing challenge type keywords (more specific than h1)
    let instr = '';
    const candidates = document.querySelectorAll('h1,h2,h3,p,[class*="title"],[class*="instruction"],[class*="challenge"]');
    for (const el of candidates) {
        if (!notExcluded(el)) continue;
        const t = (el.textContent || '').trim();
        // Skip generic titles like "Challenge Step N"
        if (/^(challenge +step|step +[0-9]|you are on step|browser +navigation)/i.test(t)) continue;
        // Skip very short or very long text
        if (t.length >= 10 && t.length <= 200) {
            instr = t;
            break;
        }
    }
    fp.instruction = instr.toLowerCase().replace(/[0-9]+/g, 'N').substring(0, 100);
    // Interactive signature: only non-excluded, non-decoy interactables
    fp.sig = [];
    const els = Array.from(document.querySelectorAll(
        'button,[role="button"],input,[role="tab"],select,textarea'))
        .filter(notExcluded)
        .filter(el => !exclude.has(el))
        .slice(0, 25);
    for (const el of els) {
        const role = el.getAttribute('role') || el.tagName.toLowerCase();
        let name = (el.getAttribute('aria-label') || el.textContent || el.value || '')
            .trim().toLowerCase();
        name = name.replace(/[^a-z0-9 ]/gi, ' ')
                   .replace(/ +/g, ' ')
                   .trim()
                   .slice(0, 40);
        // Skip universal elements (code submission UI — appears on every page)
        if (/submit\s*code|enter\s*code|submit\s*answer/i.test(name)) continue;
        if (el.tagName === 'INPUT' && el.closest('[class*="code"]')) continue;
        if (name) fp.sig.push(role + ':' + name);
    }
    return fp;
}'''


def fingerprint_similarity(live: dict, stored: dict) -> float:
    """Compute similarity between live DOM fingerprint and stored fingerprint.

    Three weighted components:
    - Signature Jaccard (0.55): most discriminative — interactive element signatures
    - Element counts (0.25): structural similarity
    - Instruction text overlap (0.20): title/instruction text similarity
    """
    if not live or not stored:
        return 0.0
    # Component 1: Signature Jaccard (most discriminative)
    l_sig = set(live.get('sig', []))
    s_sig = set(stored.get('sig', []))
    if l_sig and s_sig:
        sig_score = len(l_sig & s_sig) / max(len(l_sig | s_sig), 1)
    else:
        sig_score = 0.0
    # Component 2: Element counts
    element_keys = ['buttons', 'inputs', 'canvas', 'iframes', 'audio',
                    'video', 'draggables', 'tabs', 'sliders', 'checkboxes']
    matches, total = 0.0, 0
    for key in element_keys:
        lv, sv = live.get(key, 0), stored.get(key, 0)
        if lv > 0 or sv > 0:
            total += 1
            if lv == sv:
                matches += 1
            elif abs(lv - sv) <= 1:
                matches += 0.5
    count_score = matches / max(total, 1)
    # Component 3: Instruction token overlap
    l_words = set(live.get('instruction', '').split())
    s_words = set(stored.get('instruction', '').split())
    if l_words and s_words:
        instr_score = len(l_words & s_words) / max(len(l_words | s_words), 1)
    else:
        instr_score = 0.0
    # Weighted combination
    return 0.55 * sig_score + 0.25 * count_score + 0.20 * instr_score


# ─── Atomic write ─────────────────────────────────────────────────────────────

def atomic_write_json(data: dict, filepath: str):
    """Crash-safe atomic JSON write via tmp + os.replace."""
    tmp_path = f"{filepath}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, filepath)  # Atomic on both POSIX and Windows


# ─── Failure pattern extraction (from agent_improver.py) ─────────────────────

def extract_failure_pattern(error_info: str) -> Optional[str]:
    """Extract a pattern from error info for grouping similar failures."""
    patterns = [
        (r"modal.*not dismissed", "modal_not_dismissed"),
        (r"code not (found|extracted)", "code_not_extracted"),
        (r"element not found", "element_not_found"),
        (r"selector.*miss", "selector_miss"),
        (r"timeout", "timeout"),
        (r"click.*failed", "click_failed"),
        (r"404|not found|error page", "navigation_error"),
        (r"dom.*not.*change", "dom_unchanged"),
    ]
    error_lower = error_info.lower()
    for regex, pattern_name in patterns:
        if re.search(regex, error_lower):
            return pattern_name
    return None


# ─── Sanitization ────────────────────────────────────────────────────────────

_CODE_RE = re.compile(r"\b[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}\b")
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")


def _sanitize_text(text: str) -> str:
    """Strip session-specific codes and hex literals from natural language fields."""
    text = _CODE_RE.sub("{{CODE}}", text)
    text = _HEX_RE.sub("{{HEX}}", text)
    return text


# ─── Default seed strategies (from v1 solver + agent knowledge) ──────────────

V1_SEED_STRATEGIES = {
    "scroll": {
        "action_type": "scroll",
        "action_params": {"direction": "down", "amount": 800},
        "suggested_action": "Scroll down to reveal hidden code below the fold",
        "detection_keywords": ["scroll", "down", "hidden", "below", "find"],
    },
    "click_reveal": {
        "action_type": "click_reveal",
        "action_params": {},
        "suggested_action": "Click the reveal/show button to display the code",
        "detection_keywords": ["click", "button", "reveal", "code"],
    },
    "hidden_dom": {
        "action_type": "hidden_dom",
        "action_params": {"click_count": 5},
        "suggested_action": "Click the hidden element multiple times to reveal the code in the DOM",
        "detection_keywords": ["hidden", "dom", "click", "times", "reveal"],
    },
    "hover": {
        "action_type": "hover",
        "action_params": {"duration_ms": 2000},
        "suggested_action": "Hover over the target element for 2+ seconds to trigger reveal animation",
        "detection_keywords": ["hover", "reveal"],
    },
    "delay_memory": {
        "action_type": "delay_memory",
        "action_params": {"duration_ms": 5000},
        "suggested_action": "Wait for the code to appear (it flashes briefly or appears after a delay)",
        "detection_keywords": ["remember", "memorize", "flash", "code", "wait", "appear", "delay"],
    },
    "radio": {
        "action_type": "radio",
        "action_params": {"option_text": "Correct Choice"},
        "suggested_action": "Select the correct radio button option (often labeled 'Correct Choice')",
        "detection_keywords": ["select", "radio", "option"],
    },
    "radio_modal": {
        "action_type": "radio",
        "action_params": {"option_text": "Correct Choice"},
        "suggested_action": "Scroll within modal, select correct radio button, then submit",
        "detection_keywords": ["select", "option", "scrollable", "modal"],
    },
    "checkbox": {
        "action_type": "checkbox",
        "action_params": {},
        "suggested_action": "Check all unchecked checkboxes to enable the submit button",
        "detection_keywords": ["select", "checkbox", "check"],
    },
    "draw": {
        "action_type": "draw",
        "action_params": {"gesture": "line", "canvas_selector": "canvas"},
        "suggested_action": "Draw the requested gesture (line/circle/shape) on the canvas element",
        "detection_keywords": ["draw", "canvas", "gesture", "line", "circle"],
    },
    "drag_drop": {
        "action_type": "drag_drop",
        "action_params": {},
        "suggested_action": "Drag puzzle pieces from their starting positions to the correct target slots",
        "detection_keywords": ["drag", "drop", "slots", "piece"],
    },
    "decode": {
        "action_type": "decode",
        "action_params": {"encoding": "base64"},
        "suggested_action": "Find and decode the encoded string (base64/rot13/hex) to get the code",
        "detection_keywords": ["base64", "decode", "encoded", "rot13", "cipher"],
    },
    "shadow_dom": {
        "action_type": "shadow_dom",
        "action_params": {"depth": 3},
        "suggested_action": "Search shadow DOM roots and nested iframes for hidden code elements",
        "detection_keywords": ["shadow", "dom", "inspect", "element"],
    },
    "websocket": {
        "action_type": "websocket",
        "action_params": {},
        "suggested_action": "Connect to WebSocket endpoint to receive the code message",
        "detection_keywords": ["websocket", "connect"],
    },
    "audio": {
        "action_type": "audio",
        "action_params": {},
        "suggested_action": "Play audio element and transcribe to extract the spoken code",
        "detection_keywords": ["listen", "play", "audio"],
    },
    "enable_buttons": {
        "action_type": "enable_buttons",
        "action_params": {},
        "suggested_action": "Enable disabled buttons by removing the disabled attribute, then click",
        "detection_keywords": ["enable", "disabled", "button"],
    },
    "keyboard_sequence": {
        "action_type": "keyboard_sequence",
        "action_params": {},
        "suggested_action": "Parse key sequence from page instructions and press keys in order",
        "detection_keywords": ["keyboard", "sequence", "press", "keys"],
    },
}


# ─── KnowledgeReader V2 ──────────────────────────────────────────────────────

class KnowledgeReader:
    """
    One canonical learning per challenge type with bounded variants.

    Storage: knowledge/learnings.json — dict keyed by challenge_type.
    """

    MAX_VARIANTS = 3
    MAX_FAILURE_HISTORY = 5
    MAX_PREVIOUS_VERSIONS = 3

    def __init__(self, knowledge_path: Path = None):
        self.knowledge_path = knowledge_path or Path("knowledge")
        self.knowledge_path.mkdir(exist_ok=True)
        self._learnings: dict[str, CanonicalLearning] = {}
        self._json_path = self.knowledge_path / "learnings.json"
        self._agent_tracker = None  # Set via set_agent_tracker() for routing integration
        self._load()

    def set_agent_tracker(self, tracker):
        """Wire agent tracker for routing integration (fix #7)."""
        self._agent_tracker = tracker

    # ── Loading / Migration ──────────────────────────────────────────────

    def _load(self):
        """Load from JSON, or migrate from JSONL if JSON doesn't exist."""
        if self._json_path.exists():
            self._load_json()
        else:
            jsonl_path = self.knowledge_path / "learnings.jsonl"
            if jsonl_path.exists():
                self._migrate_from_jsonl(jsonl_path)
            else:
                log_stage("knowledge", "no existing learnings found, starting fresh (V3)")
                self._save()  # Write empty JSON

        # Bootstrap default learnings if no learnings loaded
        if not self._learnings:
            self._seed_defaults()

        # One-time startup: seed versioned recipes from strong unversioned ones
        self._seed_versioned_from_unversioned()

        # Pre-warm dom_signals for known challenge types (avoids dead channels)
        self._prewarm_dom_signals()

    def _seed_versioned_from_unversioned(self):
        """Seed weak/missing versioned recipes from strong unversioned ones.

        For each unversioned recipe with tier >= 2 and replay_successes >= 3,
        clone it to type_v1/v2/v3 if the versioned entry is missing or weaker.
        Then archive the unversioned entry (tier=0).
        """
        import copy
        unversioned = {}
        for ctype, learning in list(self._learnings.items()):
            # Skip already-versioned entries
            if re.search(r'_v\d+$', ctype):
                continue
            variant = learning.get_active_variant()
            if not variant:
                continue
            if variant.tier >= 2 and variant.replay_successes >= 3:
                unversioned[ctype] = (learning, variant)

        if not unversioned:
            return

        seeded = 0
        for base_type, (learning, src_variant) in unversioned.items():
            local_seeded = 0
            for v in [1, 2, 3]:
                versioned_key = f"{base_type}_v{v}"
                existing = self._learnings.get(versioned_key)
                existing_variant = existing.get_active_variant() if existing else None

                # Skip if versioned entry already has equal or better tier
                if existing_variant and existing_variant.tier >= src_variant.tier:
                    continue

                # Skip if already seeded and has been tested
                if (existing_variant and
                        getattr(existing_variant, 'created_by', '') == 'seeded' and
                        existing_variant.replay_attempts > 0):
                    continue

                # Clone recipe and detection keywords
                new_variant = StrategyVariant(
                    variant_id=f"seeded_from_{base_type}",
                    preconditions=list(src_variant.preconditions),
                    suggested_action=f"seeded from {base_type} (T{src_variant.tier})",
                    action_type=src_variant.action_type,
                    action_params=copy.deepcopy(src_variant.action_params),
                    action_recipe=copy.deepcopy(src_variant.action_recipe),
                    successful_dna_signature=copy.deepcopy(src_variant.successful_dna_signature),
                    page_context=copy.deepcopy(src_variant.page_context),
                    created_by="seeded",
                    tier=1,  # Must prove itself versioned
                    recipe_ttl=5,
                    assertions_present=src_variant.assertions_present,
                    non_replayable=src_variant.non_replayable,
                    confidence=0.3,
                )

                if existing:
                    # Replace only the active variant, preserve others
                    replaced = False
                    for i, ev in enumerate(existing.variants):
                        if ev.variant_id == existing.active_variant_id:
                            existing.variants[i] = new_variant
                            replaced = True
                            break
                    if not replaced:
                        existing.variants.append(new_variant)
                    existing.active_variant_id = new_variant.variant_id
                    if not existing.detection_keywords:
                        existing.detection_keywords = list(learning.detection_keywords)
                else:
                    # Create new learning entry
                    self._learnings[versioned_key] = CanonicalLearning(
                        challenge_type=versioned_key,
                        detection_keywords=list(learning.detection_keywords),
                        router_priority=learning.router_priority,
                        variants=[new_variant],
                        active_variant_id=new_variant.variant_id,
                        dom_signals=list(learning.dom_signals),
                        created_at=datetime.now().isoformat(),
                        last_updated=datetime.now().isoformat(),
                        dna_signatures=list(learning.dna_signatures),
                        page_text_context=dict(learning.page_text_context),
                    )
                seeded += 1
                local_seeded += 1
                log_stage("knowledge",
                          f"seeded {versioned_key} from {base_type} "
                          f"(T{src_variant.tier}, {src_variant.replay_successes} successes)")

            # Only archive if at least one versioned entry was actually created
            if local_seeded > 0:
                src_variant.tier = 0
                log_stage("knowledge", f"archived unversioned {base_type} (tier->0)")

        if seeded > 0:
            self._save()
            log_stage("knowledge", f"startup migration: seeded {seeded} versioned recipes")

    def _prewarm_dom_signals(self):
        """Seed dom_signals for learnings where the challenge type implies known DOM features.

        Without this, old learnings have empty dom_signals and the detection
        formula's dom channel is always 0, wasting its weight even after
        adaptive redistribution.
        """
        # Map base challenge type → expected DOM features
        KNOWN_SIGNALS = {
            'draw': [{'type': 'element_exists', 'selector': 'canvas'}],
            'canvas': [{'type': 'element_exists', 'selector': 'canvas'}],
            'gesture': [{'type': 'element_exists', 'selector': 'canvas'}],
            'drag_drop': [{'type': 'element_exists', 'selector': '[draggable=true]'}],
            'audio': [{'type': 'element_exists', 'selector': 'audio'}],
            'video': [{'type': 'element_exists', 'selector': 'video'}],
            'recursive_iframe': [{'type': 'element_exists', 'selector': 'iframe'}],
            'shadow_dom': [{'type': 'element_exists', 'selector': 'iframe'}],
        }
        warmed = 0
        for ctype, learning in self._learnings.items():
            if learning.dom_signals:
                continue  # already has data
            base = re.sub(r'_v\d+$', '', ctype)
            signals = KNOWN_SIGNALS.get(base)
            if signals:
                learning.dom_signals = list(signals)
                warmed += 1
        if warmed:
            self._save()
            log_stage("knowledge", f"pre-warmed dom_signals for {warmed} learnings")

    def _load_json(self):
        """Load learnings from JSON file."""
        try:
            with open(self._json_path, encoding="utf-8") as f:
                raw = json.load(f)
            for ctype, data in raw.items():
                self._learnings[ctype] = self._dict_to_learning(ctype, data)
            log_stage("knowledge", f"loaded {len(self._learnings)} canonical learnings from JSON")
        except Exception as e:
            log_stage("knowledge", f"error loading learnings.json: {e}")

        # Backfill: populate empty page_text_context from stored dom_fingerprint.instruction
        backfilled = 0
        for ctype, learning in self._learnings.items():
            if learning.page_text_context:
                continue
            instr = learning.dom_fingerprint.get('instruction', '')
            if not instr:
                continue
            ctx = self._extract_text_context({'instruction': instr})
            if ctx:
                self._update_type_text_context(learning, ctx)
                backfilled += 1
        if backfilled:
            self._save()
            log_stage("knowledge", f"backfilled page_text_context for {backfilled} learnings")

    def _dict_to_learning(self, ctype: str, data: dict) -> CanonicalLearning:
        """Reconstruct a CanonicalLearning from a dict."""
        variants = []
        for vd in data.get("variants", []):
            variants.append(StrategyVariant(
                variant_id=vd.get("variant_id", f"{ctype}_default"),
                preconditions=vd.get("preconditions", []),
                suggested_action=vd.get("suggested_action", ""),
                action_type=vd.get("action_type", "unknown"),
                action_params=vd.get("action_params", {}),
                confidence=vd.get("confidence", 0.5),
                attempts=vd.get("attempts", 0),
                successes=vd.get("successes", 0),
                failures=vd.get("failures", 0),
                consecutive_failures=vd.get("consecutive_failures", 0),
                verified=vd.get("verified", False),
                version=vd.get("version", 1),
                previous_versions=vd.get("previous_versions", []),
                failure_history=vd.get("failure_history", []),
                failure_patterns=vd.get("failure_patterns", {}),
                action_recipe=vd.get("action_recipe", []),
                successful_dna_signature=vd.get("successful_dna_signature", {}),
                page_context=vd.get("page_context", {}),
                # V3 provenance fields
                created_by=vd.get("created_by", "system2"),
                replay_attempts=vd.get("replay_attempts", 0),
                replay_successes=vd.get("replay_successes", 0),
                last_replay_error=vd.get("last_replay_error"),
                assertions_present=vd.get("assertions_present", False),
                minimized=vd.get("minimized", False),
                avg_runtime_ms=vd.get("avg_runtime_ms", 0.0),
                tier=vd.get("tier", 0),
                recipe_ttl=vd.get("recipe_ttl", 5),
                non_replayable=vd.get("non_replayable", False),
            ))
        return CanonicalLearning(
            challenge_type=ctype,
            detection_keywords=data.get("detection_keywords", []),
            router_priority=data.get("router_priority", DEFAULT_PRIORITIES.get(ctype, 50)),
            variants=variants,
            active_variant_id=data.get("active_variant_id"),
            dom_signals=data.get("dom_signals", []),
            last_refinement_reason=data.get("last_refinement_reason", ""),
            last_match_score=data.get("last_match_score", 0.0),
            disabled_until=data.get("disabled_until"),
            disable_reason=data.get("disable_reason"),
            created_at=data.get("created_at", ""),
            last_updated=data.get("last_updated", ""),
            last_succeeded=data.get("last_succeeded"),
            dna_signatures=data.get("dna_signatures", []),
            page_text_context=data.get("page_text_context", {}),
            dom_fingerprint=data.get("dom_fingerprint", {}),
            confused_with=data.get("confused_with", []),
        )

    def _migrate_from_jsonl(self, jsonl_path: Path):
        """One-time migration: JSONL → JSON with de-duplication and sanitization."""
        log_stage("knowledge", f"migrating from {jsonl_path} to JSON...")
        best_per_type: dict[str, dict] = {}

        try:
            with open(jsonl_path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ctype = record.get("challenge_type", "unknown")
                    # Normalize type names
                    ctype = TYPE_ALIASES.get(ctype, ctype)
                    ctype = ctype.lower().replace(" ", "_").replace("/", "_")

                    conf = record.get("confidence", 0.5)
                    existing = best_per_type.get(ctype)
                    if not existing or conf > existing.get("confidence", 0):
                        best_per_type[ctype] = record
                        best_per_type[ctype]["challenge_type"] = ctype
        except Exception as e:
            log_stage("knowledge", f"migration read error: {e}")
            return

        # Build canonical learnings
        now = datetime.now().isoformat()
        for ctype, record in best_per_type.items():
            # Sanitize natural language fields only
            suggested = _sanitize_text(record.get("suggested_action", ""))
            why_failed = _sanitize_text(record.get("why_it_failed", ""))

            # Infer action_type from challenge_type
            action_type = ctype  # Default: same as type
            action_params = self._infer_action_params(ctype, suggested)

            variant = StrategyVariant(
                variant_id=f"{ctype}_default",
                preconditions=[],
                suggested_action=suggested,
                action_type=action_type,
                action_params=action_params,
                confidence=record.get("confidence", 0.5),
                attempts=record.get("num_observations", 1),
                successes=1 if record.get("verified") else 0,
                failures=max(0, record.get("num_observations", 1) - (1 if record.get("verified") else 0)),
                consecutive_failures=0,
                verified=record.get("verified", False),
                version=1,
                previous_versions=[],
                failure_history=[],
                failure_patterns={},
            )

            # Recalculate Wilson confidence
            variant.confidence = wilson_score_lower(variant.successes, variant.attempts)
            if variant.confidence == 0.0 and variant.attempts > 0:
                variant.confidence = 0.3  # Floor for migrated entries

            detection_kw = record.get("detection_keywords", [])

            self._learnings[ctype] = CanonicalLearning(
                challenge_type=ctype,
                detection_keywords=detection_kw,
                router_priority=DEFAULT_PRIORITIES.get(ctype, 50),
                variants=[variant],
                active_variant_id=variant.variant_id,
                dom_signals=[],
                last_refinement_reason="migrated from JSONL",
                last_match_score=0.0,
                disabled_until=None,
                disable_reason=None,
                created_at=now,
                last_updated=now,
                last_succeeded=now if record.get("verified") else None,
            )

        self._save()

        # Backup old JSONL
        bak_path = jsonl_path.with_suffix(".jsonl.migrated_bak")
        try:
            if not bak_path.exists():
                os.rename(str(jsonl_path), str(bak_path))
                log_stage("knowledge", f"backed up JSONL to {bak_path.name}")
        except Exception as e:
            log_stage("knowledge", f"backup rename failed: {e}")

        log_stage("knowledge", f"migrated {len(self._learnings)} canonical learnings")

    def _infer_action_params(self, ctype: str, suggested: str) -> dict:
        """Infer initial action_params from challenge type and suggested action text."""
        params: dict = {}
        action_lower = suggested.lower()

        if ctype == "scroll":
            params["direction"] = "down"
            amount_match = re.search(r"(\d+)\s*(?:px|pixels?)", action_lower)
            if amount_match:
                params["amount"] = int(amount_match.group(1))
            else:
                params["amount"] = 500

        elif ctype == "hover":
            params["duration_ms"] = 2000
            # Try to extract selector
            sel_match = re.search(r'"([^"]+)"', suggested)
            if sel_match:
                params["selector"] = sel_match.group(1)

        elif ctype == "hidden_dom":
            count_match = re.search(r"(\d+)\s*(?:more\s+)?times?", action_lower)
            params["click_count"] = int(count_match.group(1)) if count_match else 5

        elif ctype == "delay_memory":
            time_match = re.search(r"(\d+)\s*(?:second|sec|s)", action_lower)
            params["duration_ms"] = int(time_match.group(1)) * 1000 if time_match else 5000

        elif ctype == "decode":
            if "base64" in action_lower:
                params["encoding"] = "base64"
            elif "rot13" in action_lower:
                params["encoding"] = "rot13"
            elif "hex" in action_lower:
                params["encoding"] = "hex"

        return params

    # ── Seeding ────────────────────────────────────────────────────────

    def _seed_defaults(self):
        """Seed canonical learnings for all known challenge types from V1_SEED_STRATEGIES.

        Only adds types not already present. Each seeded learning starts with a
        prior of successes=2, attempts=3 so Wilson score is reasonable (~0.38)
        rather than zero.
        """
        now = datetime.now().isoformat()
        seeded = 0

        for ctype, strategy in V1_SEED_STRATEGIES.items():
            if ctype in self._learnings:
                continue

            variant = StrategyVariant(
                variant_id=f"{ctype}_default",
                preconditions=[],
                suggested_action=strategy["suggested_action"],
                action_type=strategy["action_type"],
                action_params=dict(strategy["action_params"]),
                confidence=wilson_score_lower(2, 3),  # Prior: 2/3 success
                attempts=3,
                successes=2,
                failures=1,
                consecutive_failures=0,
                verified=False,
                version=1,
                previous_versions=[],
                failure_history=[],
                failure_patterns={},
            )

            self._learnings[ctype] = CanonicalLearning(
                challenge_type=ctype,
                detection_keywords=strategy.get("detection_keywords", []),
                router_priority=DEFAULT_PRIORITIES.get(ctype, 50),
                variants=[variant],
                active_variant_id=variant.variant_id,
                dom_signals=[],
                last_refinement_reason="seeded from v1",
                last_match_score=0.0,
                disabled_until=None,
                disable_reason=None,
                created_at=now,
                last_updated=now,
                last_succeeded=None,
            )
            seeded += 1

        if seeded:
            self._save()
            log_stage("knowledge", f"seeded {seeded} default learnings from V1_SEED_STRATEGIES")

    # ── Persistence ──────────────────────────────────────────────────────

    def _save(self):
        """Save all learnings to JSON atomically."""
        data = {}
        for ctype, learning in self._learnings.items():
            variants_data = []
            for v in learning.variants:
                variants_data.append({
                    "variant_id": v.variant_id,
                    "preconditions": v.preconditions,
                    "suggested_action": v.suggested_action,
                    "action_type": v.action_type,
                    "action_params": v.action_params,
                    "confidence": v.confidence,
                    "attempts": v.attempts,
                    "successes": v.successes,
                    "failures": v.failures,
                    "consecutive_failures": v.consecutive_failures,
                    "verified": v.verified,
                    "version": v.version,
                    "previous_versions": v.previous_versions[-self.MAX_PREVIOUS_VERSIONS:],
                    "failure_history": v.failure_history[-self.MAX_FAILURE_HISTORY:],
                    "failure_patterns": v.failure_patterns,
                    "action_recipe": v.action_recipe,
                    "successful_dna_signature": v.successful_dna_signature,
                    "page_context": v.page_context,
                    # V3 provenance
                    "created_by": v.created_by,
                    "replay_attempts": v.replay_attempts,
                    "replay_successes": v.replay_successes,
                    "last_replay_error": v.last_replay_error,
                    "assertions_present": v.assertions_present,
                    "minimized": v.minimized,
                    "avg_runtime_ms": v.avg_runtime_ms,
                    "tier": v.tier,
                    "recipe_ttl": v.recipe_ttl,
                    "non_replayable": v.non_replayable,
                })
            data[ctype] = {
                "detection_keywords": learning.detection_keywords,
                "router_priority": learning.router_priority,
                "variants": variants_data,
                "active_variant_id": learning.active_variant_id,
                "dom_signals": learning.dom_signals,
                "last_refinement_reason": learning.last_refinement_reason,
                "last_match_score": learning.last_match_score,
                "disabled_until": learning.disabled_until,
                "disable_reason": learning.disable_reason,
                "created_at": learning.created_at,
                "last_updated": learning.last_updated,
                "last_succeeded": learning.last_succeeded,
                "dna_signatures": learning.dna_signatures,
                "page_text_context": learning.page_text_context,
                "dom_fingerprint": learning.dom_fingerprint,
                "confused_with": learning.confused_with,
            }
        try:
            atomic_write_json(data, str(self._json_path))
        except Exception as e:
            log_stage("knowledge", f"save error: {e}")

    # ── Detection & Routing ──────────────────────────────────────────────

    def detect_and_get(self, page_text: str, page=None,
                       page_info: dict = None,
                       dna_elements: list = None,
                       version: int = None) -> tuple[Optional[CanonicalLearning], Optional[StrategyVariant]]:
        """Detect challenge type and return (learning, best_variant) or (None, None).

        Scoring with fingerprint (6 channels):
          Default: 0.25*keyword + 0.10*flags + 0.15*text_ctx + 0.10*dom + 0.10*dna + 0.20*fp
          If fingerprint >= 0.7: fp weight ramps to 0.45, others scale down proportionally.
        Within type, pick variant with highest match_score * wilson * staleness.
        If version is set, only score version-matching or unversioned learnings.
        """
        text_lower = _strip_filler_text(page_text).lower()
        self._last_detection_gap = 0.0  # Reset to prevent stale gap from previous call

        # Get DOM flags if page available
        dom_flags = self._get_dom_flags(page) if page else {}

        # Get live DOM fingerprint if page available
        live_fingerprint = self._get_dom_fingerprint(page) if page else {}

        # Score each candidate type
        scored: list[tuple[float, CanonicalLearning]] = []

        for ctype, learning in self._learnings.items():
            if learning.is_disabled():
                continue
            # Version filter: only consider matching version or unversioned
            if version is not None:
                version_match = re.search(r'_v(\d+)$', ctype)
                if version_match and int(version_match.group(1)) != version:
                    continue

            # 1. Keyword score (from detection patterns)
            keyword_score = self._compute_keyword_score(ctype, text_lower, learning.detection_keywords)

            # 6. Fingerprint score
            # If stored fingerprint is missing (old learning), use neutral 0.5
            # so it doesn't get penalized vs types that have fingerprints
            if not learning.dom_fingerprint:
                fp_score = 0.5
            elif live_fingerprint:
                fp_score = fingerprint_similarity(live_fingerprint, learning.dom_fingerprint)
            else:
                fp_score = 0.0

            # Hard gate: require meaningful keyword evidence OR strong fingerprint.
            flag_score = self._compute_flag_score(ctype, dom_flags)
            if keyword_score < 0.15 and flag_score == 0.0 and fp_score < 0.5:
                continue

            # Text context score
            text_ctx_score = self._compute_text_context_score(learning, text_lower, page_info)

            # DOM signals score (structural features like canvas, audio, iframe)
            dom_score = self._compute_dom_score(learning.dom_signals, page)

            # DNA signature score (code element visual fingerprint)
            dna_score = self._compute_dna_score(learning, dna_elements or [])

            # Weighted combination — smooth fingerprint-adaptive ramp.
            # Instead of a sharp cutoff at fp=0.7, smoothly interpolate weights
            # between fp=0.5 (default) and fp=0.7 (fingerprint-dominant).
            # This prevents a 7-basis-point fp change from causing massive re-weighting.
            #
            # 6-channel scoring: kw + flags + text_ctx + fp + dom + dna
            # dom_signals and dna_signatures are populated during promotion.
            # Adaptive weights: exclude channels with no data on this learning,
            # redistributing their weight proportionally to populated channels.
            # Without this, old learnings with empty dom/dna lose 20% max score.
            t = max(0.0, min(1.0, (fp_score - 0.5) / 0.2))  # 0→1 over [0.5, 0.7]
            fp_w = 0.20 + 0.25 * t          # 0.20 → 0.45
            remaining = 1.0 - fp_w           # sum of other weights
            # Base weights — zero out channels with no data on this learning
            w_kw, w_flags, w_text, w_dom, w_dna = 0.25, 0.10, 0.15, 0.10, 0.10
            if not learning.dom_signals:
                w_dom = 0.0
            if not learning.dna_signatures:
                w_dna = 0.0
            base_other = w_kw + w_flags + w_text + w_dom + w_dna
            scale = remaining / max(base_other, 0.01)
            match_score = (scale * w_kw * keyword_score
                           + scale * w_flags * flag_score
                           + scale * w_text * text_ctx_score
                           + scale * w_dom * dom_score
                           + scale * w_dna * dna_score
                           + fp_w * fp_score)

            # Action-signature penalty: DOM + instruction text mismatch
            sig_penalty = self._action_signature_penalty(learning, live_fingerprint, text_lower)
            if sig_penalty > 0:
                match_score -= sig_penalty
                log_stage("knowledge", f"action-signature mismatch: '{ctype}' "
                          f"-{sig_penalty:.2f}")
            # Cache for orchestrator routing guard (avoids redundant eval)
            learning._last_sig_penalty = sig_penalty

            if match_score > 0.1:
                scored.append((match_score, learning))

        # Confusion penalty: demote types with confusion history (with age decay)
        # Gap-gated: only apply to candidates competitive with top score
        if len(scored) > 1:
            top_score = max(s for s, _ in scored)
            candidate_bases = set()
            for _, lrn in scored:
                candidate_bases.add(re.sub(r'_v\d+$', '', lrn.challenge_type))

            adjusted = []
            for score_val, lrn in scored:
                penalty = 0.0
                # Only penalize candidates within 0.10 of top (competitive)
                if top_score - score_val < 0.10:
                    for conf in getattr(lrn, 'confused_with', []):
                        if conf.get('actual_type', '') in candidate_bases:
                            count = conf.get('count', 1)
                            # Age decay: reduce effective count if older than 7 days
                            last_seen = conf.get('last_seen', '')
                            if last_seen:
                                try:
                                    age_days = (datetime.now() - datetime.fromisoformat(last_seen)).days
                                    if age_days > 7:
                                        count = max(1, count - 1)
                                except (ValueError, TypeError):
                                    pass
                            # Escalating: 1→0.05, 2→0.10, 3+→0.15 per pair; total capped at 0.15
                            penalty += min(0.05 + 0.05 * min(count - 1, 2), 0.15)
                    penalty = min(penalty, 0.15)
                adjusted.append((score_val - penalty, lrn))
                if penalty > 0:
                    log_stage("knowledge", f"confusion penalty: '{lrn.challenge_type}' "
                              f"-{penalty:.2f} → {score_val - penalty:.2f}")
            scored = adjusted

        if not scored:
            # Fallback: try pure pattern detection (no stored learning needed)
            detected = detect_challenge_type(page_text)
            if detected and detected in self._learnings:
                learning = self._learnings[detected]
                if not learning.is_disabled():
                    variant = learning.get_active_variant()
                    learning.last_match_score = 0.5  # Pattern-only match
                    return learning, variant
            return None, None

        # Sort by score, break ties by router_priority
        scored.sort(key=lambda x: (x[0], x[1].router_priority), reverse=True)

        # Save gap for routing guard (used by orchestrator)
        self._last_detection_gap = (scored[0][0] - scored[1][0]) if len(scored) > 1 else 1.0

        # Score histogram: calibrate thresholds before locking
        top1_type = scored[0][1].challenge_type
        top1_score = scored[0][0]
        top2_info = f"{scored[1][1].challenge_type}={scored[1][0]:.3f}" if len(scored) > 1 else "none"
        log_stage("knowledge", f"detection: top1={top1_type} score={top1_score:.3f} "
                  f"top2={top2_info} gap={self._last_detection_gap:.3f}")

        best_score, best_learning = scored[0]
        best_learning.last_match_score = best_score

        # Pick best variant within the type
        variant = self._pick_best_variant(best_learning, best_score)

        if variant:
            log_stage("knowledge",
                      f"matched '{best_learning.challenge_type}' "
                      f"(score={best_score:.2f}, variant={variant.variant_id}, "
                      f"wilson={variant.confidence:.2f})")
        return best_learning, variant

    def _compute_keyword_score(self, ctype: str, text_lower: str, stored_keywords: list[str]) -> float:
        """Compute keyword match score from detection patterns + stored keywords."""
        total_weight = 0.0
        matched_weight = 0.0

        # Strip version suffix for pattern matching (e.g., "delayed_reveal_v2" -> "delayed_reveal")
        base_ctype = re.sub(r'_v\d+$', '', ctype)

        # Check built-in detection patterns
        for pattern_type, patterns in DETECTION_PATTERNS:
            if pattern_type == base_ctype:
                for regex, weight in patterns:
                    total_weight += weight
                    if re.search(regex, text_lower):
                        matched_weight += weight

        # Check stored keywords (word-boundary match to avoid substring false positives)
        for kw in stored_keywords:
            total_weight += 0.5
            if re.search(r'\b' + re.escape(kw.lower()) + r'\b', text_lower):
                matched_weight += 0.5

        return matched_weight / max(total_weight, 1.0)

    def _compute_dom_score(self, dom_signals: list[dict], page) -> float:
        """Compute DOM signal satisfaction score."""
        if not dom_signals or not page:
            return 0.0  # No signals defined = no evidence, not neutral
        matched = 0
        for signal in dom_signals:
            try:
                sig_type = signal.get("type", "element_exists")
                selector = signal.get("selector", "")
                if sig_type == "element_exists" and selector:
                    if page.query_selector(selector):
                        matched += 1
                elif sig_type == "text_contains":
                    el = page.query_selector(selector) if selector else None
                    if el:
                        el_text = el.inner_text() or ""
                        value = signal.get("value", "")
                        if value.lower() in el_text.lower():
                            matched += 1
            except Exception as e:
                log_stage("knowledge", f"DOM signal eval failed for '{signal.get('selector')}': {e}")
        return matched / len(dom_signals)

    def _compute_flag_score(self, ctype: str, dom_flags: dict) -> float:
        """Compute DOM flag match score."""
        if not dom_flags:
            return 0.0  # No page available — no evidence

        flag_map = {
            "draw": ["has_canvas"],
            "gesture": ["has_canvas"],
            "shadow_dom": ["has_shadow"],
            "checkbox": ["has_checkbox"],
            "radio": ["has_radio"],
            "radio_modal": ["has_radio"],
            "audio": ["has_audio"],
            "websocket": ["has_ws"],
        }
        # Strip version suffix (e.g., "draw_v2" -> "draw")
        base_ctype = re.sub(r'_v\d+$', '', ctype)
        expected = flag_map.get(base_ctype, [])
        if not expected:
            return 0.0  # No flag requirements = no evidence, not neutral
        matched = sum(1 for f in expected if dom_flags.get(f, False))
        return matched / len(expected)

    def _action_signature_penalty(self, learning, live_fp: dict, page_text: str = '') -> float:
        """Hard penalty when recipe requires DOM features absent from live page,
        or challenge type's instruction keywords don't match page instruction text,
        or recipe target texts are absent from the page."""
        variant = learning.get_active_variant()
        if not variant or not variant.action_recipe:
            return 0.0

        recipe_types = {s.get('action_type') for s in variant.action_recipe}
        base_type = re.sub(r'_v\d+$', '', learning.challenge_type)

        # Normalize instruction: collapse whitespace, strip punctuation
        raw_instr = (live_fp.get('instruction', '') or '')
        instr = re.sub(r'\s+', ' ', raw_instr).strip()
        instr = re.sub(r'[^a-z0-9 ]', '', instr)

        # ── DOM structure requirements (hard) ──
        DOM_REQ = {
            'drag_drop_auto': ('draggables', 1),
            'drag': ('draggables', 1),
            'canvas_draw': ('canvas', 1),
            'draw': ('canvas', 1),
        }
        TYPE_DOM_REQ = {
            'canvas': ('canvas', 1),
            'draw': ('canvas', 1),
            'gesture': ('canvas', 1),
            'audio': ('audio', 1),
            'multi_tab': ('tabs', 1),
        }

        # Canvas/audio/tabs: hard penalty (0.30) — reliable signals
        for action_type in recipe_types:
            req = DOM_REQ.get(action_type)
            if not req:
                continue
            if live_fp.get(req[0], 0) < req[1]:
                if req[0] == 'draggables':
                    # Drag is softer (0.15) — [draggable="true"] isn't always set
                    if not any(w in instr for w in ('drag', 'drop', 'slot', 'piece')):
                        return 0.15
                else:
                    return 0.30

        req = TYPE_DOM_REQ.get(base_type)
        if req and live_fp.get(req[0], 0) < req[1]:
            return 0.30

        # ── Instruction text requirements (soft) ──
        # Gate: skip if instruction is too short/generic to be meaningful
        if len(instr) < 8:
            return 0.0

        INSTR_REQ = {
            'keyboard_sequence': ['press', 'ctrl', 'shift', 'tab', 'keyboard',
                                  'paste', 'copy', 'key', 'sequence'],
            'hover': ['hover'],
            'delay_memory': ['flash', 'remember', 'memorize', 'memory'],
            'delayed_reveal': ['timer', 'wait', 'seconds', 'delay', 'timed'],
            'hidden_dom': ['hidden', 'hidden dom', 'not visible', 'reveal hidden'],
            'drag_drop': ['drag', 'drop', 'slot', 'piece'],
            'audio': ['listen', 'audio', 'sound', 'play'],
            'decode': ['decode', 'base64', 'rot13', 'cipher', 'encoded'],
            'scroll': ['scroll', 'bottom', 'container'],
            'puzzle_solve': ['puzzle', 'pieces', 'jigsaw', 'arrange', 'rearrange',
                             'assemble', 'put together'],
            'calculated': ['calculate', 'compute', 'expression', 'evaluate',
                           'what is', 'sum', 'result', 'math'],
        }

        type_keywords = INSTR_REQ.get(base_type, [])
        if type_keywords and instr:
            has_own_keywords = any(kw in instr for kw in type_keywords)
            if not has_own_keywords:
                # Check if instruction matches a DIFFERENT type's keywords
                for other_type, other_kws in INSTR_REQ.items():
                    if other_type == base_type:
                        continue
                    has_other = any(kw in instr for kw in other_kws)
                    if has_other:
                        return 0.20  # Strong negative: page says X, recipe is Y
                return 0.10  # Weak negative: no matching keywords found
        elif not type_keywords and instr:
            # Type has no INSTR_REQ entry — still check if page matches another type
            for other_type, other_kws in INSTR_REQ.items():
                has_other = any(kw in instr for kw in other_kws)
                if has_other:
                    return 0.15  # Medium negative: page is specific, recipe is generic

        # ── Recipe target verification (structural) ──
        # Check if the recipe's target_text values exist on the current page.
        # If a recipe expects to click "Part 1:", "Part 2:" but the page text
        # doesn't contain these, the recipe was likely misclassified and will
        # fail at runtime. Apply penalty proportional to the miss ratio.
        # Generic button labels that appear on every page (popups, navigation)
        _GENERIC = {
            'submit', 'close', 'dismiss', 'accept', 'ok', 'continue',
            'next', 'proceed', 'got it', 'no thanks', 'decline',
            'submit code', 'enter code', 'next step', 'reveal',
            'solve', 'complete', 'start', 'begin', 'reset',
        }
        if page_text and variant.action_recipe:
            target_texts = []
            for step_def in variant.action_recipe:
                tt = step_def.get('target_text', '')
                if tt and len(tt) >= 3:
                    if tt.strip().lower() not in _GENERIC:
                        target_texts.append(tt.lower())
            if len(target_texts) >= 2:
                hits = sum(1 for t in target_texts if t in page_text)
                miss_ratio = 1.0 - hits / len(target_texts)
                if miss_ratio >= 0.5:
                    return 0.15 + 0.15 * miss_ratio  # 0.225 at 50%, 0.30 at 100%

        return 0.0

    def _pick_best_variant(self, learning: CanonicalLearning, match_score: float) -> Optional[StrategyVariant]:
        """Pick the best variant: highest match_score * wilson * staleness * agent_health."""
        if not learning.variants:
            return None

        now = time.time()
        best = None
        best_effective = -1.0

        for v in learning.variants:
            # Staleness penalty: decay over 24h since last success
            staleness = 1.0
            if learning.last_succeeded:
                try:
                    last_t = datetime.fromisoformat(learning.last_succeeded).timestamp()
                    hours_since = (now - last_t) / 3600
                    staleness = max(0.5, 1.0 - hours_since / 48)
                except (ValueError, TypeError):
                    pass

            # Agent health penalty (fix #7): penalize if the agent for this action
            # type has a high failure rate. Steers routing away from broken agents.
            agent_health = 1.0
            if self._agent_tracker:
                rate = self._agent_tracker.get_failure_rate(v.action_type)
                if rate > 0:
                    agent_health = max(0.3, 1.0 - rate * 0.5)

            effective = match_score * v.confidence * staleness * agent_health
            if effective > best_effective:
                best_effective = effective
                best = v

        if best:
            learning.active_variant_id = best.variant_id
        return best

    # ── DOM utilities ────────────────────────────────────────────────────

    def _get_dom_flags(self, page) -> dict:
        """Single JS call to get all DOM features."""
        try:
            return page.evaluate(DOM_FLAGS_JS)
        except Exception:
            return {}

    def _get_dom_fingerprint(self, page) -> dict:
        """Get DOM fingerprint for challenge detection."""
        try:
            return page.evaluate(DOM_FINGERPRINT_JS)
        except Exception:
            return {}

    def get_dom_signature(self, page) -> str:
        """Structural DOM signature for change detection.

        Returns raw structural string (tag counts + form states).
        No longer hashed — enables proportional diff computation.
        """
        try:
            return page.evaluate(DOM_SIGNATURE_JS)
        except Exception:
            return ""

    def compute_dom_change_score(self, sig_before: str, sig_after: str) -> float:
        """Compute proportional DOM change score (0.0=identical, 1.0=completely different).

        Parses the structural signature (TAG:count,...|forms|canvas|audio)
        and computes normalized L1 distance across tag counts + text features.
        """
        if not sig_before or not sig_after:
            return 0.5  # Unknown
        if sig_before == sig_after:
            return 0.0
        try:
            b_parts = sig_before.split('|')
            a_parts = sig_after.split('|')
            # Part 0: tag counts (TAG:count,...)
            b_tags = dict(p.split(':') for p in b_parts[0].split(',') if ':' in p) if b_parts[0] else {}
            a_tags = dict(p.split(':') for p in a_parts[0].split(',') if ':' in p) if a_parts[0] else {}
            all_tags = set(b_tags) | set(a_tags)
            if all_tags:
                tag_diff = sum(abs(int(b_tags.get(t, 0)) - int(a_tags.get(t, 0))) for t in all_tags)
                tag_total = sum(int(b_tags.get(t, 0)) for t in all_tags) + sum(int(a_tags.get(t, 0)) for t in all_tags)
                tag_score = tag_diff / max(tag_total, 1)
            else:
                tag_score = 0.0
            # Parts 1+: form states, canvas, audio (binary diff)
            other_diffs = 0
            other_total = 0
            for i in range(1, max(len(b_parts), len(a_parts))):
                bp = b_parts[i] if i < len(b_parts) else ''
                ap = a_parts[i] if i < len(a_parts) else ''
                other_total += 1
                if bp != ap:
                    other_diffs += 1
            other_score = other_diffs / max(other_total, 1) if other_total > 0 else 0.0
            return min(1.0, tag_score * 0.7 + other_score * 0.3)
        except Exception:
            return 0.5  # Assume some change on parse error

    # ── Recording ────────────────────────────────────────────────────────

    def record_success(self, challenge_type: str, variant_id: str,
                       page_info: dict = None, recipe: list[dict] = None,
                       dna_signature: dict = None):
        """Record a success: Wilson update, reset consecutive failures, mark verified.

        Optionally stores recipe, DNA signature, and page context for System 1/2.
        """
        learning = self._learnings.get(challenge_type)
        if not learning:
            log_stage("knowledge", f"record_success: unknown type '{challenge_type}', skipping")
            return

        matched_variant = None
        for v in learning.variants:
            if v.variant_id == variant_id:
                matched_variant = v
                v.attempts += 1
                v.successes += 1
                v.consecutive_failures = 0
                v.confidence = wilson_score_lower(v.successes, v.attempts)
                v.verified = True

                # Store recipe on the VARIANT (variant-scoped)
                # Guard: don't overwrite an existing recipe with a different-type recipe
                # (prevents cross-type contamination when detection matcher is loose)
                if recipe:
                    if not v.action_recipe:
                        v.action_recipe = recipe
                    elif recipe[0].get('action_type') == v.action_recipe[0].get('action_type'):
                        v.action_recipe = recipe
                    # else: skip — different action_type would contaminate

                # Store DNA signature on VARIANT + aggregate at type level
                if dna_signature:
                    v.successful_dna_signature = dna_signature
                    self._aggregate_dna_signature(learning, dna_signature)

                # Store page context on variant + aggregate at type level
                if page_info:
                    v.page_context = self._extract_text_context(page_info)
                    self._update_type_text_context(learning, v.page_context)

                break

        if matched_variant:
            learning.last_succeeded = datetime.now().isoformat()
            learning.last_updated = datetime.now().isoformat()

            # Clear disable on any success
            if learning.disabled_until:
                learning.disabled_until = None
                learning.disable_reason = None
                log_stage("knowledge", f"re-enabled '{challenge_type}' after success")

            self._save()
            log_stage("knowledge",
                      f"recorded SUCCESS for '{challenge_type}/{variant_id}' "
                      f"(wilson={matched_variant.confidence:.3f})")
        else:
            log_stage("knowledge",
                      f"record_success: variant '{variant_id}' not found in '{challenge_type}'")

    def record_failure(self, challenge_type: str, variant_id: str,
                       why: str, what_tried: str,
                       dom_sig_before: str = "", dom_sig_after: str = "",
                       dom_change_score: float = 0.0,
                       page_info: dict = None):
        """Record a failure on a variant."""
        learning = self._learnings.get(challenge_type)
        if not learning:
            log_stage("knowledge", f"record_failure: unknown type '{challenge_type}', skipping")
            return

        matched_variant = None
        for v in learning.variants:
            if v.variant_id == variant_id:
                matched_variant = v
                v.attempts += 1
                v.failures += 1
                v.consecutive_failures += 1
                v.confidence = wilson_score_lower(v.successes, v.attempts)

                # Append to failure history (capped, sanitized to strip session codes)
                entry = {
                    "why": _sanitize_text(why[:200]),
                    "what_tried": _sanitize_text(what_tried[:200]),
                    "timestamp": datetime.now().isoformat(),
                    "dom_sig_before": dom_sig_before,
                    "dom_sig_after": dom_sig_after,
                    "dom_changed": dom_sig_before != dom_sig_after,
                    "dom_change_score": dom_change_score,
                    "instruction_snippet": (page_info.get("instruction", "")[:200] if page_info else ""),
                    "button_labels": ([b.get("text", "") for b in page_info.get("buttons", [])[:10]] if page_info else []),
                }
                v.failure_history.append(entry)
                v.failure_history = v.failure_history[-self.MAX_FAILURE_HISTORY:]

                # Group into failure patterns
                pattern = extract_failure_pattern(why)
                if pattern:
                    v.failure_patterns[pattern] = v.failure_patterns.get(pattern, 0) + 1

                break

        if matched_variant:
            learning.last_updated = datetime.now().isoformat()
            self._save()
            log_stage("knowledge",
                      f"recorded FAILURE for '{challenge_type}/{variant_id}' "
                      f"(consecutive={matched_variant.consecutive_failures}, dom_change={dom_change_score:.2f})")
        else:
            log_stage("knowledge",
                      f"record_failure: variant '{variant_id}' not found in '{challenge_type}'")

    def record_confusion(self, wrongly_matched: str, actual_type: str):
        """Record that wrongly_matched was detected when actual_type was correct.
        Stores at base-type level so learning transfers across versions."""
        wrongly_base = re.sub(r'_v\d+$', '', wrongly_matched)
        actual_base = re.sub(r'_v\d+$', '', actual_type)

        if wrongly_base == actual_base:
            return
        GENERIC = {'simple', 'unknown', 'default', 'basic'}
        if wrongly_base in GENERIC or actual_base in GENERIC:
            return

        now = datetime.now().isoformat()
        updated = False
        for ctype, learning in self._learnings.items():
            if re.sub(r'_v\d+$', '', ctype) != wrongly_base:
                continue
            for entry in learning.confused_with:
                if entry.get('actual_type') == actual_base:
                    entry['count'] = entry.get('count', 0) + 1
                    entry['last_seen'] = now
                    updated = True
                    break
            else:
                learning.confused_with.append({
                    'actual_type': actual_base, 'count': 1, 'last_seen': now,
                })
                updated = True
            if len(learning.confused_with) > 5:
                learning.confused_with.sort(key=lambda e: e.get('count', 0), reverse=True)
                learning.confused_with = learning.confused_with[:5]
            learning.last_updated = now

        if updated:
            self._save()
            log_stage("knowledge", f"recorded CONFUSION: '{wrongly_base}' → actual '{actual_base}'")

    # ── DNA + Text Context Helpers ────────────────────────────────────────

    def _aggregate_dna_signature(self, learning: CanonicalLearning, new_dna: dict):
        """Track DNA frequency at type level. 3+ occurrences = High Confidence."""
        for sig in learning.dna_signatures:
            if (sig.get('color') == new_dna.get('color') and
                sig.get('fontSize') == new_dna.get('fontSize') and
                sig.get('fontWeight') == new_dna.get('fontWeight') and
                sig.get('backgroundColor') == new_dna.get('backgroundColor')):
                sig['occurrences'] = sig.get('occurrences', 1) + 1
                return
        entry = dict(new_dna)
        entry['occurrences'] = 1
        learning.dna_signatures.append(entry)
        if len(learning.dna_signatures) > 5:
            learning.dna_signatures.sort(key=lambda s: s.get('occurrences', 0), reverse=True)
            learning.dna_signatures = learning.dna_signatures[:5]

    def _extract_text_context(self, page_info: dict) -> dict:
        """Extract text context from page info for learning system."""
        instruction = page_info.get("instruction", "")
        buttons = page_info.get("buttons", [])
        interactives = page_info.get("interactives", [])
        keywords = [kw for kw in ["hover", "click", "scroll", "drag", "draw", "wait",
                                  "listen", "decode", "press", "select", "check", "reveal",
                                  "play", "complete"]
                    if kw in instruction.lower()]
        return {
            "instruction_keywords": keywords,
            "button_labels": [b.get("text", "")[:30] for b in buttons[:10]],
            "interactive_types": list({el.get("tag", "") for el in interactives}),
            "text_fingerprint": instruction[:200],
        }

    def _update_type_text_context(self, learning: CanonicalLearning, new_context: dict):
        """Merge new text context into type-level aggregated context."""
        existing = learning.page_text_context
        for key in ["instruction_keywords", "button_labels", "interactive_types"]:
            merged = set(existing.get(key, []))
            merged.update(new_context.get(key, []))
            existing[key] = list(merged)[:15]
        learning.page_text_context = existing

    def _compute_dna_score(self, learning: CanonicalLearning, dna_elements: list) -> float:
        """Score DNA match: 1.0 if high-confidence signature found on page."""
        if not learning.dna_signatures or not dna_elements:
            return 0.0  # No DNA signatures = no evidence, not neutral
        high_conf = [s for s in learning.dna_signatures if s.get('occurrences', 0) >= 3]
        if not high_conf:
            return 0.3
        from agents.dna_reasoner import DNAReasoner
        reasoner = DNAReasoner()
        for sig in high_conf:
            sig_key = reasoner._make_dna_key(sig)
            for el in dna_elements[:100]:  # Cap for performance
                el_key = reasoner._make_dna_key(el.get('dna', {}))
                if el_key == sig_key:
                    return 1.0  # Strong match
        return 0.1

    def _compute_text_context_score(self, learning: CanonicalLearning,
                                    page_text: str, page_info: dict = None) -> float:
        """Score text context match against stored patterns."""
        ctx = learning.page_text_context
        if not ctx:
            return 0.0
        score, total = 0.0, 0.0
        stored_kw = ctx.get("instruction_keywords", [])
        if stored_kw:
            text_lower = page_text  # Already lowercased by caller (detect_and_get)
            matched = sum(1 for kw in stored_kw if kw in text_lower)
            score += matched / len(stored_kw)
            total += 1.0
        # NOTE: button_labels matching DISABLED — stored labels are almost entirely
        # decoy popup buttons ("Proceed Forward", "Continue", etc.) that appear on
        # every page, providing zero discriminative power and inflating all scores.
        # NOTE: interactive_types matching DISABLED — "input" is on every page
        # (code entry form), "div" is universal. Zero discriminative power.
        return score / max(total, 1.0)

    # ── Refinement ───────────────────────────────────────────────────────

    def refine_variant(self, challenge_type: str, variant_id: str,
                       new_action: str, new_action_type: str,
                       new_params: dict, reason: str):
        """Snapshot current variant, update with refined strategy."""
        learning = self._learnings.get(challenge_type)
        if not learning:
            return

        matched_variant = None
        for v in learning.variants:
            if v.variant_id == variant_id:
                # Guard: reject refinements that change action_type on high-confidence learnings
                # This prevents cross-type contamination (e.g. scroll → click when a
                # non-scroll challenge is misrouted to the scroll learning)
                if (v.action_type != new_action_type
                        and v.verified):
                    log_stage("knowledge",
                              f"BLOCKED refinement of '{challenge_type}/{variant_id}': "
                              f"action_type change {v.action_type} → {new_action_type} "
                              f"on verified learning (conf={v.confidence:.2f})")
                    return

                matched_variant = v
                # Snapshot for rollback
                v.previous_versions.append(v.snapshot())
                v.previous_versions = v.previous_versions[-self.MAX_PREVIOUS_VERSIONS:]

                # Update
                v.suggested_action = new_action
                v.action_type = new_action_type
                v.action_params = new_params
                v.version += 1
                v.consecutive_failures = 0  # Reset after refinement
                break

        if matched_variant:
            learning.last_refinement_reason = reason
            learning.last_updated = datetime.now().isoformat()
            self._save()
            log_stage("knowledge",
                      f"refined '{challenge_type}/{variant_id}' v{matched_variant.version}: {reason[:60]}")

    def create_variant(self, challenge_type: str, variant_id: str,
                       preconditions: list[dict],
                       action_type: str, action_params: dict,
                       suggested_action: str = ""):
        """Add a new variant if room (max 3), else replace lowest-confidence."""
        learning = self._learnings.get(challenge_type)
        if not learning:
            return

        new_variant = StrategyVariant(
            variant_id=variant_id,
            preconditions=preconditions,
            suggested_action=suggested_action,
            action_type=action_type,
            action_params=action_params,
            confidence=0.3,  # New variant starts low
            attempts=0,
            successes=0,
            failures=0,
            consecutive_failures=0,
            verified=False,
            version=1,
            previous_versions=[],
            failure_history=[],
            failure_patterns={},
        )

        if len(learning.variants) < self.MAX_VARIANTS:
            learning.variants.append(new_variant)
        else:
            # Replace worst variant, but only evict those with >= 3 attempts
            # (don't kill untested variants). Use success ratio, not raw Wilson.
            evictable = [
                (i, v) for i, v in enumerate(learning.variants)
                if v.attempts >= 3
            ]
            if evictable:
                worst_idx = min(
                    evictable,
                    key=lambda iv: iv[1].successes / max(iv[1].attempts, 1)
                )[0]
            else:
                # All variants are undertested — replace the oldest (first)
                worst_idx = 0
            old = learning.variants[worst_idx]
            log_stage("knowledge",
                      f"replacing variant '{old.variant_id}' "
                      f"(ratio={old.successes}/{old.attempts}) with '{variant_id}'")
            learning.variants[worst_idx] = new_variant

        learning.last_updated = datetime.now().isoformat()
        self._save()
        log_stage("knowledge",
                  f"created variant '{variant_id}' for '{challenge_type}' "
                  f"({len(learning.variants)}/{self.MAX_VARIANTS})")

    def rollback_variant(self, challenge_type: str, variant_id: str) -> bool:
        """Restore variant from most recent previous_versions entry."""
        learning = self._learnings.get(challenge_type)
        if not learning:
            return False

        for v in learning.variants:
            if v.variant_id == variant_id:
                if not v.previous_versions:
                    log_stage("knowledge", f"no rollback available for '{variant_id}'")
                    return False

                snapshot = v.previous_versions.pop()
                v.suggested_action = snapshot.get("suggested_action", v.suggested_action)
                v.action_type = snapshot.get("action_type", v.action_type)
                v.action_params = snapshot.get("action_params", v.action_params)
                v.preconditions = snapshot.get("preconditions", v.preconditions)
                v.version = snapshot.get("version", v.version)
                v.consecutive_failures = 0  # Reset after rollback

                # Restore stats from snapshot if available
                v.confidence = snapshot.get("confidence", v.confidence)

                learning.last_refinement_reason = f"rollback to v{v.version}"
                learning.last_updated = datetime.now().isoformat()
                self._save()
                log_stage("knowledge",
                          f"ROLLED BACK '{challenge_type}/{variant_id}' to v{v.version}")
                return True

        return False

    # ── Circuit breaker ──────────────────────────────────────────────────

    def disable_learning(self, challenge_type: str, reason: str,
                         until: str | None = None):
        """Disable a learning (TTL-based). Default: end of current run (~1 hour)."""
        learning = self._learnings.get(challenge_type)
        if not learning:
            return

        if until is None:
            # Default: disable for 1 hour (rest of run)
            from datetime import timedelta
            until = (datetime.now() + timedelta(hours=1)).isoformat()

        learning.disabled_until = until
        learning.disable_reason = reason
        learning.last_updated = datetime.now().isoformat()
        self._save()
        log_stage("knowledge", f"DISABLED '{challenge_type}' until {until}: {reason}")

    def reset_disabled(self):
        """Called at run start: clear all expired disabled_until entries."""
        now = datetime.now()
        cleared = 0
        for learning in self._learnings.values():
            if learning.disabled_until:
                try:
                    until = datetime.fromisoformat(learning.disabled_until)
                    if now >= until:
                        learning.disabled_until = None
                        learning.disable_reason = None
                        cleared += 1
                except (ValueError, TypeError):
                    learning.disabled_until = None
                    learning.disable_reason = None
                    cleared += 1
        if cleared:
            self._save()
            log_stage("knowledge", f"reset {cleared} expired disabled learnings")

    # ── Creation ─────────────────────────────────────────────────────────

    def create_learning(self, challenge_type: str, variant: StrategyVariant,
                        detection_keywords: list[str] = None,
                        dom_signals: list[dict] = None,
                        router_priority: int = 50):
        """Create a new canonical learning entry."""
        now = datetime.now().isoformat()
        self._learnings[challenge_type] = CanonicalLearning(
            challenge_type=challenge_type,
            detection_keywords=detection_keywords or [],
            router_priority=router_priority,
            variants=[variant],
            active_variant_id=variant.variant_id,
            dom_signals=dom_signals or [],
            last_refinement_reason="initial creation",
            last_match_score=0.0,
            disabled_until=None,
            disable_reason=None,
            created_at=now,
            last_updated=now,
            last_succeeded=now if variant.successes > 0 else None,
        )
        self._save()
        log_stage("knowledge",
                  f"created learning for '{challenge_type}' "
                  f"(variant={variant.variant_id})")

    # ── Replay Success Context Update ────────────────────────────────────

    def record_replay_success(self, challenge_type: str, variant_id: str,
                              snapshot) -> None:
        """Update detection context from a successful replay (lightweight).

        Only updates text context, fingerprint, and dom_signals — does NOT
        modify recipe or stats (those are handled in LearningSidecar.finalize_promotion).
        """
        learning = self._learnings.get(challenge_type)
        if not learning:
            return
        updated = False
        # Update text context from snapshot instruction
        if snapshot and getattr(snapshot, 'text_ctx', None):
            self._update_type_text_context(learning, snapshot.text_ctx)
            updated = True
        # Reinforce fingerprint (merge, don't replace)
        if snapshot and getattr(snapshot, 'fingerprint', None):
            self._merge_fingerprint(learning, snapshot.fingerprint)
            updated = True
        # Reinforce dom_signals if not yet populated
        if snapshot and getattr(snapshot, 'dom_signals', None) and not learning.dom_signals:
            learning.dom_signals = snapshot.dom_signals
            updated = True
        if updated:
            self._save()
            log_stage("knowledge", f"replay success context updated for '{challenge_type}'")

    def update_recipe_delays(self, ctype, variant_id, observed_delays):
        """Update recipe step delay_ms from observed replay timing (EMA).

        Only updates click/type/press/select/keyboard_sequence steps.
        Excludes hover, wait, drag, and other actions with semantic delays.
        """
        learning = self._learnings.get(ctype)
        if not learning:
            return
        variant = learning.get_active_variant()
        if not variant or variant.variant_id != variant_id:
            return
        recipe = variant.action_recipe
        SAFE_TYPES = {'click', 'type', 'press', 'select', 'keyboard_sequence'}
        updated = False
        for step_idx, observed_ms in observed_delays.items():
            if step_idx >= len(recipe):
                continue
            step = recipe[step_idx]
            if step.get('action_type') not in SAFE_TYPES:
                continue
            # Sanity: skip outliers
            if observed_ms < 0 or observed_ms > 2000:
                continue
            # Adaptive buffer: fast steps get +120ms, slow steps get +25%
            buffer = max(120, int(0.25 * observed_ms))
            # Floor 250ms (React re-render + animation), ceiling 1500ms
            new_delay = max(250, min(int(observed_ms + buffer), 1500))
            old_delay = step.get('delay_ms', 100)
            if old_delay > 200:  # already learned from prior replay
                # EMA: 70% old + 30% new (converges over ~5 runs)
                step['delay_ms'] = int(old_delay * 0.7 + new_delay * 0.3)
            else:  # first observation (still at default 100ms)
                step['delay_ms'] = new_delay
            updated = True
        if updated:
            self._save()
            log_stage("knowledge", f"updated recipe delays for '{ctype}' "
                      f"({len(observed_delays)} steps)")

    def _merge_fingerprint(self, learning: CanonicalLearning, new_fp: dict):
        """Merge new fingerprint into existing, keeping union of elements."""
        existing = learning.dom_fingerprint
        if not existing:
            learning.dom_fingerprint = new_fp
            return
        # Keep higher element counts (more complete DOM observation)
        for key in new_fp:
            if key == 'sig':
                # Merge signature lists (union)
                old_sigs = set(existing.get('sig', []))
                new_sigs = set(new_fp.get('sig', []))
                existing['sig'] = list(old_sigs | new_sigs)[:25]
            elif key == 'instruction':
                # Keep longer instruction text
                if len(new_fp.get('instruction', '')) > len(existing.get('instruction', '')):
                    existing['instruction'] = new_fp['instruction']
            elif isinstance(new_fp[key], (int, float)):
                if key not in existing or new_fp[key] > existing.get(key, 0):
                    existing[key] = new_fp[key]

    # ── Queries ──────────────────────────────────────────────────────────

    def get_by_type(self, challenge_type: str) -> Optional[CanonicalLearning]:
        """Get learning for a specific challenge type."""
        return self._learnings.get(challenge_type)

    def get_all_learnings(self) -> dict:
        """Return all learnings dict (read-only snapshot for auditing)."""
        return dict(self._learnings)

    def delete_learning(self, challenge_type: str) -> bool:
        """Hard-delete a learning entry. Returns True if it existed."""
        if challenge_type in self._learnings:
            del self._learnings[challenge_type]
            self._save()
            return True
        return False

    def get_stats(self) -> dict:
        """Get statistics about stored knowledge."""
        total_variants = sum(len(l.variants) for l in self._learnings.values())
        verified = sum(1 for l in self._learnings.values()
                      for v in l.variants if v.verified)
        disabled = sum(1 for l in self._learnings.values() if l.is_disabled())
        return {
            "total_learnings": len(self._learnings),
            "total_variants": total_variants,
            "verified": verified,
            "disabled": disabled,
            "steps_covered": len(self._learnings),  # backward compat
            "generated_agents": 0,
            "learnings_by_type": {k: len(v.variants) for k, v in self._learnings.items()},
        }


# ─── Standalone detection function (no learning lookup needed) ───────────────

def _strip_filler_text(text: str) -> str:
    """Remove filler/decoy sections that pollute challenge type detection.

    The site pads pages with "Section N" blocks containing misleading text like
    "Scroll Down to Find All Hidden Sections" which triggers false scroll detection.
    Also strips known decoy button labels.
    """
    # Strip everything after "Section 1" / "Section 2" etc. — these are always filler
    text = re.split(r'\bsection\s+\d+\b', text, flags=re.IGNORECASE)[0]
    # Strip decoy button labels — multi-word phrases only (safe to remove)
    # Single words like "Next", "Continue" are NOT stripped: they may appear
    # in genuine challenge instructions.
    text = re.sub(
        r'\b(?:Next Step|Next Page|Next Section|Continue Journey|Continue Reading|'
        r'Proceed Forward|Go Forward|Move On|Keep Going|Click Here|'
        r'Load|Try This|New Button)\b', '', text, flags=re.IGNORECASE)
    return text


def detect_challenge_type(page_text: str) -> Optional[str]:
    """Detect challenge type from page text using weighted pattern matching.

    Returns the best-matching challenge type, or None.
    """
    text = _strip_filler_text(page_text).lower()
    best_type = None
    best_score = 0.0

    # NOTE: On tie (equal scores), the first matching type in DETECTION_PATTERNS
    # wins due to strict `>` comparison (insertion-order bias). If this becomes a
    # problem, add a secondary tiebreaker (e.g. DEFAULT_PRIORITIES).
    for ctype, patterns in DETECTION_PATTERNS:
        score = 0.0
        for regex, weight in patterns:
            if re.search(regex, text):
                score += weight

        if score > best_score:
            best_score = score
            best_type = ctype

    return best_type if best_score >= 0.5 else None


# ─── Global instance ─────────────────────────────────────────────────────────

_reader: Optional[KnowledgeReader] = None


def get_knowledge_reader() -> KnowledgeReader:
    """Get the global KnowledgeReader instance."""
    global _reader
    if _reader is None:
        _reader = KnowledgeReader()
    return _reader
