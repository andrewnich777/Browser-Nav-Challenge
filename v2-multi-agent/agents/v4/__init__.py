"""V4 Agent System — deterministic challenge agents replacing recipe replay.

Usage:
    from agents.v4 import UNIVERSAL_AGENTS, CHALLENGE_AGENTS, StepCtx
"""

from agents.v4.context import StepCtx
from agents.v4.universal import UNIVERSAL_AGENTS
from agents.v4.challenges import CHALLENGE_AGENTS

__all__ = ['StepCtx', 'UNIVERSAL_AGENTS', 'CHALLENGE_AGENTS']
