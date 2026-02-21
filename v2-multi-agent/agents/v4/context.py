"""Shared step context for V4 agents."""

from dataclasses import dataclass, field
from playwright.sync_api import Page


@dataclass
class StepCtx:
    page: Page
    step: int
    version: int
    t0: float                        # step start time
    boundary_y: int | None = None    # challenge area bottom boundary (viewport Y)
    instruction: str = ''            # normalized instruction text
    scope_selector: str | None = None  # CSS selector for challenge container
    budget_ms: int = 2500            # time budget for this step
    debug: dict = field(default_factory=dict)  # per-step telemetry accumulator
    used_codes: set = field(default_factory=set)  # codes from previous steps (filter stale fiber results)
