from __future__ import annotations

from app.runtime.json_types import JsonObject


class FeedbackStoreError(Exception):
    """Base class for route-safe feedback optimization domain errors."""

    status_code = 400
    error_code = "FEEDBACK_STORE_ERROR"
    error_details: JsonObject | None = None

    def __init__(self, message: str = "", *, error_details: JsonObject | None = None) -> None:
        super().__init__(message)
        self.error_details = error_details


class BusinessRuleViolation(FeedbackStoreError):
    """Raised when a user action violates a feedback workflow business rule."""

    error_code = "BUSINESS_RULE_VIOLATION"


class ConflictError(FeedbackStoreError):
    """Raised when a valid user action conflicts with current workflow state."""

    status_code = 409
    error_code = "CONFLICT"


class MainWorkspaceDirtyError(ConflictError):
    """Raised when main Agent workspace has uncommitted changes."""

    error_code = "MAIN_WORKSPACE_DIRTY"

    def __init__(self, repository_status: JsonObject) -> None:
        super().__init__(
            "Main Agent workspace has uncommitted changes",
            error_details={
                "repository_status": repository_status,
                "changed_files": repository_status.get("changed_files") if isinstance(repository_status.get("changed_files"), list) else [],
                "file_diffs": repository_status.get("file_diffs") if isinstance(repository_status.get("file_diffs"), list) else [],
            },
        )


class ConfigurationError(FeedbackStoreError):
    """Raised when feedback workflow configuration is missing or malformed."""

    error_code = "CONFIGURATION_ERROR"


class RuntimeUnavailableError(FeedbackStoreError):
    """Raised when the runtime is temporarily unavailable for a retryable reason."""

    status_code = 503
    error_code = "RUNTIME_UNAVAILABLE"


class DataIntegrityError(FeedbackStoreError):
    """Raised when persisted feedback data is internally inconsistent."""

    status_code = 409
    error_code = "DATA_INTEGRITY_ERROR"


class AgentVersionIntegrityError(DataIntegrityError):
    """Raised when an Agent version bundle or archive path fails integrity checks."""

    error_code = "AGENT_VERSION_INTEGRITY_ERROR"


class AgentOutputParseError(FeedbackStoreError):
    """Raised when Agent output or feedback job JSON payload cannot be parsed."""

    status_code = 409
    error_code = "AGENT_OUTPUT_PARSE_ERROR"


class NotFoundError(FeedbackStoreError):
    """Raised when a feedback workflow entity cannot be found."""

    status_code = 404
    error_code = "NOT_FOUND"
