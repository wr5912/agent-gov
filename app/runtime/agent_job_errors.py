from __future__ import annotations

from .json_types import JsonObject

AGENT_AUTH_REQUIRED = "AGENT_AUTH_REQUIRED"
AGENT_RUNTIME_ERROR = "AGENT_RUNTIME_ERROR"
_PLACEHOLDER_PROVIDER_API_KEYS = {"", "sk-ant-xxxx", "change-me", "change-me-model-provider-key"}


class AgentJobRuntimeError(RuntimeError):
    """Base exception for Agent job failures that need stable error codes."""

    error_code: str
    raw_output_json: JsonObject | None

    def __init__(self, *, error_code: str, message: str, raw_output_json: JsonObject | None = None) -> None:
        self.error_code = error_code
        self.raw_output_json = raw_output_json
        super().__init__(message)


class AgentAuthenticationRequiredError(AgentJobRuntimeError):
    """Raised before launching Claude Code when a background profile has no model credentials."""

    def __init__(
        self,
        *,
        profile_name: str,
        runtime_volume_mode: str,
        settings_env_file: str | None,
        missing: list[str],
    ) -> None:
        missing_text = ", ".join(missing)
        location = settings_env_file or "the selected runtime env file"
        super().__init__(
            error_code=AGENT_AUTH_REQUIRED,
            message=(f"Agent profile {profile_name} requires model provider credentials. Configure {missing_text} in {location}."),
            raw_output_json={
                "error_type": "agent_auth_required",
                "profile_name": profile_name,
                "runtime_volume_mode": runtime_volume_mode,
                "settings_env_file": settings_env_file,
                "missing": missing,
            },
        )


def agent_error_code(exc: Exception) -> str:
    error_code = getattr(exc, "error_code", None)
    return error_code if isinstance(error_code, str) and error_code else AGENT_RUNTIME_ERROR


def agent_error_message(exc: Exception) -> str:
    return f"{exc.__class__.__name__}: {exc}"


def exception_raw_output_json(exc: Exception) -> JsonObject | None:
    raw_output = getattr(exc, "raw_output_json", None)
    return raw_output if isinstance(raw_output, dict) else None


def provider_api_key_configured(value: str | None) -> bool:
    return (value or "").strip() not in _PLACEHOLDER_PROVIDER_API_KEYS
