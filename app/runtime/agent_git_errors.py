class AgentGitError(RuntimeError):
    """Raised when Git-backed Agent governance cannot complete an operation."""


class AgentGitInitializationConflict(AgentGitError):
    """A foreign or non-canonical authority blocks repository initialization."""
