"""Shared Git governance errors without importing the large store module."""


class AgentGitError(RuntimeError):
    """Raised when Git-backed Agent governance cannot complete an operation."""
