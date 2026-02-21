"""Metrics collection and reporting."""

import json
import time
from datetime import datetime


class Metrics:
    def __init__(self):
        self.start_time = time.time()
        self.steps = []
        self.current_step_start = None
        # Vision API tracking
        self.vision_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost_usd = 0.0

    def begin_step(self, step: int):
        self.current_step_start = time.time()

    def end_step(self, step: int, success: bool, agents_used: list[str] = None):
        elapsed = time.time() - self.current_step_start if self.current_step_start else 0
        self.steps.append({
            "step": step,
            "time_sec": round(elapsed, 2),
            "success": success,
            "agents_used": agents_used or [],
        })
        self.current_step_start = None
        return elapsed

    def add_vision_usage(self, calls: int, input_tokens: int, output_tokens: int, cost: float):
        """Add vision API usage from the vision agent."""
        self.vision_calls += calls
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.estimated_cost_usd += cost

    @property
    def total_time(self) -> float:
        return time.time() - self.start_time

    @property
    def completed(self) -> int:
        return sum(1 for s in self.steps if s["success"])

    def to_dict(self) -> dict:
        return {
            "date": datetime.now().isoformat(),
            "model": "v2-multi-agent (vision-guided)",
            "steps_completed": self.completed,
            "total_steps": 30,
            "total_time_sec": round(self.total_time, 2),
            "steps": self.steps,
            "vision_calls": self.vision_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 4),
        }

    def save(self, path: str = "metrics.json"):
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    def print_summary(self):
        print(f'\n{"=" * 60}')
        print(f'  V2 MULTI-AGENT SOLVER - RESULTS')
        print(f'{"=" * 60}')
        print(f'  Steps:  {self.completed}/30')
        print(f'  Time:   {self.total_time:.1f}s')
        print(f'  Vision: {self.vision_calls} calls, {self.input_tokens} in, {self.output_tokens} out')
        print(f'  Cost:   ${self.estimated_cost_usd:.4f}')
        print(f'{"  -" * 20}')
        for s in self.steps:
            mark = "PASS" if s["success"] else "FAIL"
            agents = ",".join(s["agents_used"]) if s["agents_used"] else "-"
            print(f'  Step {s["step"]:2d} [{mark}] {s["time_sec"]:5.1f}s  agents: {agents}')
        print(f'{"=" * 60}')
