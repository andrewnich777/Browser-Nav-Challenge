"""
Agent Performance Tracker — Lightweight per-agent success/failure tracking.

Extracted from learning/agent_improver.py. Tracks per-agent success rates
and failure patterns, persisted to knowledge/agent_performance.json.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from log import log_stage
from knowledge_reader import atomic_write_json


PERFORMANCE_FILE = "knowledge/agent_performance.json"


@dataclass
class AgentPerformance:
    """Tracks an agent's performance over time."""
    agent_name: str
    total_calls: int = 0
    successes: int = 0
    failures: int = 0
    recent_failures: list = field(default_factory=list)  # Last 10
    failure_patterns: dict = field(default_factory=dict)  # Pattern → count

    @property
    def failure_rate(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return self.failures / self.total_calls


class AgentTracker:
    """Lightweight agent performance tracker."""

    def __init__(self, path: str = PERFORMANCE_FILE):
        self._path = path
        self._performance: dict[str, AgentPerformance] = {}
        self._load()

    def _load(self):
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for name, d in data.items():
                    self._performance[name] = AgentPerformance(
                        agent_name=name,
                        total_calls=d.get("total_calls", 0),
                        successes=d.get("successes", 0),
                        failures=d.get("failures", 0),
                        recent_failures=d.get("recent_failures", []),
                        failure_patterns=d.get("failure_patterns", {}),
                    )
            except Exception as e:
                log_stage("agent_tracker", f"load error: {e}")

    def _save(self):
        data = {}
        for name, perf in self._performance.items():
            data[name] = {
                "total_calls": perf.total_calls,
                "successes": perf.successes,
                "failures": perf.failures,
                "recent_failures": perf.recent_failures[-10:],
                "failure_patterns": perf.failure_patterns,
            }
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            atomic_write_json(data, self._path)
        except Exception as e:
            log_stage("agent_tracker", f"save error: {e}")

    def _get_or_create(self, agent_name: str) -> AgentPerformance:
        if agent_name not in self._performance:
            self._performance[agent_name] = AgentPerformance(agent_name=agent_name)
        return self._performance[agent_name]

    def get_failure_rate(self, agent_name: str) -> float:
        """Get failure rate for a specific agent (0.0 if unknown or < 3 calls)."""
        perf = self._performance.get(agent_name)
        if not perf or perf.total_calls < 3:
            return 0.0
        return perf.failure_rate

    def get_performance_summary(self) -> dict:
        """Get summary of all agent performance."""
        return {
            name: {
                "total": perf.total_calls,
                "successes": perf.successes,
                "failures": perf.failures,
                "rate": f"{perf.failure_rate:.0%}",
            }
            for name, perf in self._performance.items()
            if perf.total_calls > 0
        }


# Singleton
_tracker: Optional[AgentTracker] = None


def get_agent_tracker() -> AgentTracker:
    global _tracker
    if _tracker is None:
        _tracker = AgentTracker()
    return _tracker
