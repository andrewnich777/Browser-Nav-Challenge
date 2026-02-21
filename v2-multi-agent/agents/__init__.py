"""Agent registry — V4 core agents only.

Legacy specialist agents are archived in agents/_archived/ for reference.
VisionLearningAgent, DNAReasoner, RecipeExecutor are instantiated directly
in Orchestrator.__init__().
"""

from agents.code_entry import CodeEntryAgent

ALL_AGENTS = {
    "code_entry": CodeEntryAgent(),
}
