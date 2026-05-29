from __future__ import annotations


class FeedbackStoreError(ValueError):
    """Base class for route-safe feedback optimization domain errors."""

    status_code = 400
    error_code = "FEEDBACK_STORE_ERROR"


class BusinessRuleViolation(FeedbackStoreError):
    """Raised when a user action violates a feedback workflow business rule."""

    error_code = "BUSINESS_RULE_VIOLATION"


class ConflictError(FeedbackStoreError):
    """Raised when a valid user action conflicts with current workflow state."""

    status_code = 409
    error_code = "CONFLICT"


class ConfigurationError(FeedbackStoreError):
    """Raised when feedback workflow configuration is missing or malformed."""

    error_code = "CONFIGURATION_ERROR"


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
