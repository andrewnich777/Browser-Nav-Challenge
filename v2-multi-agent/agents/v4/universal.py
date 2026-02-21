"""Universal agents — run on every step (cross-cutting concerns).

Each agent has optional before(ctx) and after(ctx) methods.
Return a code string to short-circuit, or None to continue.
"""

from agents.v4.context import StepCtx
from agents.v4.helpers import (
    compute_challenge_scope, click_button_by_text, extract_code,
    wait_for_code_mutation, harvest_fallback, get_all_hook_codes,
    SUBMIT_EXCLUDE, complete_challenge_sweep,
)
from agents.v4.cdp_helpers import find_codes_in_pierced_dom
from log import log
from code_scorer import is_valid_code


# ── PopupAgent ───────────────────────────────────────────────────────────────

class PopupAgent:
    """Dismiss overlays before challenge interaction."""

    def before(self, ctx: StepCtx) -> str | None:
        from agents.popup import dismiss_all_popups
        for _ in range(2):
            if not dismiss_all_popups(ctx.page):
                break
        return None

    def after(self, ctx: StepCtx) -> str | None:
        return None


# ── ScopeAgent ───────────────────────────────────────────────────────────────

class ScopeAgent:
    """Compute challenge boundary and cache on context."""

    def before(self, ctx: StepCtx) -> str | None:
        selector, boundary_y = compute_challenge_scope(ctx.page)
        ctx.scope_selector = selector
        ctx.boundary_y = boundary_y
        ctx.debug['boundary_y'] = boundary_y
        return None

    def after(self, ctx: StepCtx) -> str | None:
        return None


# ── EarlyCodeProbe ───────────────────────────────────────────────────────────

class EarlyCodeProbe:
    """Check if a code is already visible (skip agent entirely).

    Only uses harvest_and_score with high threshold to avoid picking up
    decoy codes that delay_memory and other challenges show before interaction.
    Skips challenges that show intentional decoys (memory, rotating, timing).
    """

    # Instruction text patterns that indicate the challenge shows decoy codes
    _DECOY_PATTERNS = [
        'memory challenge', 'memorize', 'remember the code', 'did you see',
        'flash', 'rotating', 'changes every', 'capture while',
        'click before it disappears', 'click while',
        'timing challenge', 'capture', 'real code',
    ]

    def before(self, ctx: StepCtx) -> str | None:
        # Skip if challenge shows intentional decoys
        instr = ctx.instruction.lower()
        if any(p in instr for p in self._DECOY_PATTERNS):
            return None

        from code_scorer import harvest_and_score
        try:
            t0_ms = int(ctx.t0 * 1000)  # convert to Unix ms for timestamp filter
            score, code = harvest_and_score(ctx.page, ctx.instruction, t0_ms)
            if code and score >= 0.7:
                log(f"step {ctx.step}: [EarlyCodeProbe] high-confidence code: "
                    f"{code} (score={score:.2f})")
                return code
        except Exception:
            pass
        return None

    def after(self, ctx: StepCtx) -> str | None:
        return None


# ── CompletionSweep ──────────────────────────────────────────────────────────

COMPLETION_KEYWORDS = [
    'complete challenge', 'reveal code', 'show code', 'finish',
    'complete', 'reveal', 'show', 'done', 'extract',
]


class CompletionSweep:
    """Click Complete/Reveal/Finish buttons to trigger code reveal."""

    def before(self, ctx: StepCtx) -> str | None:
        return None

    def after(self, ctx: StepCtx) -> str | None:
        boundary_y = ctx.boundary_y or 99999
        code = complete_challenge_sweep(ctx.page, boundary_y)
        if code:
            log(f"step {ctx.step}: [CompletionSweep] code: {code}")
            return code
        return None


# ── CodeExtractor ────────────────────────────────────────────────────────────

class CodeExtractor:
    """Multi-strategy code extraction after challenge interaction."""

    def before(self, ctx: StepCtx) -> str | None:
        return None

    def after(self, ctx: StepCtx) -> str | None:
        # Strategy 1: Hook codes (prefer most recent)
        codes = get_all_hook_codes(ctx.page)
        if codes:
            log(f"step {ctx.step}: [CodeExtractor] hook code: {codes[-1]}")
            return codes[-1]

        # Strategy 2: DOM text scan
        code = extract_code(ctx.page)
        if code:
            log(f"step {ctx.step}: [CodeExtractor] DOM code: {code}")
            return code

        # Strategy 3: harvest_and_score
        code = harvest_fallback(ctx.page, ctx.instruction)
        if code:
            log(f"step {ctx.step}: [CodeExtractor] harvest code: {code}")
            return code

        # Strategy 4: CDP pierced DOM (shadow roots + iframes)
        code = find_codes_in_pierced_dom(ctx.page)
        if code:
            log(f"step {ctx.step}: [CodeExtractor] pierced DOM code: {code}")
            return code

        return None


# ── Registry ─────────────────────────────────────────────────────────────────

UNIVERSAL_AGENTS = [
    PopupAgent(),
    ScopeAgent(),
    EarlyCodeProbe(),
    CompletionSweep(),
    CodeExtractor(),
]
