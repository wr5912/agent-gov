"""AgentScope Runtime 的单副本容器配置。"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

RUNTIME_USER_ID = "agentgov-runtime"
_CREDENTIAL_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}")
_VERSION_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,126}")
_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def _absolute_path(value: str, *, name: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path


def _positive_float(value: str, *, name: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


def _positive_int(value: str, *, name: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


def _port(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 65535:
        raise ValueError("AGENTSCOPE_RUNTIME_PORT must be between 1 and 65535")
    return parsed


def _internal_http_base_url(value: str) -> str:
    normalized = value.rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("AGENTGOV_INTERNAL_API_BASE_URL must be an HTTP base URL")
    if parsed.query or parsed.fragment:
        raise ValueError("AGENTGOV_INTERNAL_API_BASE_URL must not contain query or fragment")
    return normalized


def _provider_http_url(value: str) -> str | None:
    if not value:
        return None
    normalized = value.rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("MODEL_PROVIDER_API_URL must be an HTTP URL")
    if parsed.query or parsed.fragment:
        raise ValueError("MODEL_PROVIDER_API_URL must not contain query or fragment")
    return normalized


def _credential_token(value: str, *, name: str) -> str:
    if _CREDENTIAL_TOKEN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a safe non-empty token")
    return value


def _version_token(value: str, *, name: str) -> str:
    if _VERSION_TOKEN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a safe non-empty version token")
    return value


def _required_value(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "")
    if not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _forbid_proxy_environment(values: Mapping[str, str]) -> None:
    configured = sorted(name for name in _PROXY_ENV_KEYS if values.get(name, "").strip())
    if configured:
        raise ValueError(
            "AgentScope Runtime proxy environment is forbidden because MCP credentials share the process: " + ", ".join(configured),
        )


def _sqlite_url(value: str, *, data_dir: Path) -> str:
    if not value.startswith("sqlite+aiosqlite:////"):
        raise ValueError(
            "AGENTSCOPE_RUNTIME_DATABASE_URL must be an absolute sqlite+aiosqlite URL",
        )
    database_path = Path(value.removeprefix("sqlite+aiosqlite:///")).resolve()
    try:
        database_path.relative_to(data_dir.resolve())
    except ValueError as exc:
        raise ValueError(
            "AGENTSCOPE_RUNTIME_DATABASE_URL must stay inside AGENTSCOPE_RUNTIME_DATA_DIR",
        ) from exc
    return value


def _runtime_storage(values: Mapping[str, str]) -> tuple[Path, Path, Path, Path, str]:
    data_dir = _absolute_path(
        values.get("AGENTSCOPE_RUNTIME_DATA_DIR", "/runtime-data"),
        name="AGENTSCOPE_RUNTIME_DATA_DIR",
    )
    business_root = _absolute_path(
        values.get("AGENTSCOPE_RUNTIME_BUSINESS_AGENTS_ROOT", "/business-agents"),
        name="AGENTSCOPE_RUNTIME_BUSINESS_AGENTS_ROOT",
    )
    candidates_root = _absolute_path(
        values.get("AGENTSCOPE_RUNTIME_CANDIDATES_ROOT", "/candidate-workspaces"),
        name="AGENTSCOPE_RUNTIME_CANDIDATES_ROOT",
    )
    workspaces_root = _absolute_path(
        values.get("AGENTSCOPE_RUNTIME_WORKSPACES_ROOT", "/runtime-workspaces"),
        name="AGENTSCOPE_RUNTIME_WORKSPACES_ROOT",
    )
    database_url = _sqlite_url(
        values.get(
            "AGENTSCOPE_RUNTIME_DATABASE_URL",
            f"sqlite+aiosqlite:///{data_dir / 'agentscope.db'}",
        ),
        data_dir=data_dir,
    )
    return data_dir, business_root, candidates_root, workspaces_root, database_url


@dataclass(frozen=True)
class RuntimeSettings:
    """独立 Runtime 的边界配置，不读取 AgentGov 业务配置。"""

    shared_secret: str = field(repr=False)
    provider_api_key: str = field(repr=False)
    agentgov_api_base_url: str = "http://agent-gov-api:8080"
    provider_api_url: str | None = None
    credential_type: str = "openai_credential"
    credential_id: str = "agentgov-runtime-provider"
    runtime_version: str = "dev"
    data_dir: Path = Path("/runtime-data")
    business_agents_root: Path = Path("/business-agents")
    candidates_root: Path = Path("/candidate-workspaces")
    workspaces_root: Path = Path("/runtime-workspaces")
    database_url: str = "sqlite+aiosqlite:////runtime-data/agentscope.db"
    request_timeout_seconds: float = 10.0
    receipt_retry_attempts: int = 5
    receipt_retry_backoff_seconds: float = 0.2
    receipt_flush_timeout_seconds: float = 10.0
    host: str = "0.0.0.0"
    port: int = 8090
    require_read_only_source_mounts: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RuntimeSettings:
        """Parse one complete container environment and fail closed on secrets."""

        values = os.environ if environ is None else environ
        _forbid_proxy_environment(values)
        secret = _required_value(values, "AGENTGOV_RUNTIME_SHARED_SECRET")
        provider_api_key = _required_value(values, "MODEL_PROVIDER_API_KEY")

        (
            data_dir,
            business_root,
            candidates_root,
            workspaces_root,
            database_url,
        ) = _runtime_storage(values)
        port = _port(values.get("AGENTSCOPE_RUNTIME_PORT", "8090"))

        return cls(
            shared_secret=secret,
            provider_api_key=provider_api_key,
            agentgov_api_base_url=_internal_http_base_url(
                values.get(
                    "AGENTGOV_INTERNAL_API_BASE_URL",
                    "http://agent-gov-api:8080",
                ),
            ),
            provider_api_url=_provider_http_url(
                values.get("MODEL_PROVIDER_API_URL", ""),
            ),
            credential_type=_credential_token(
                values.get("AGENTSCOPE_CREDENTIAL_TYPE", "openai_credential"),
                name="AGENTSCOPE_CREDENTIAL_TYPE",
            ),
            credential_id=_credential_token(
                values.get("AGENTSCOPE_CREDENTIAL_ID", "agentgov-runtime-provider"),
                name="AGENTSCOPE_CREDENTIAL_ID",
            ),
            runtime_version=_version_token(
                values.get("AGENTGOV_RUNTIME_VERSION", "dev"),
                name="AGENTGOV_RUNTIME_VERSION",
            ),
            data_dir=data_dir,
            business_agents_root=business_root,
            candidates_root=candidates_root,
            workspaces_root=workspaces_root,
            database_url=database_url,
            request_timeout_seconds=_positive_float(
                values.get("AGENTSCOPE_RUNTIME_REQUEST_TIMEOUT_SECONDS", "10"),
                name="AGENTSCOPE_RUNTIME_REQUEST_TIMEOUT_SECONDS",
            ),
            receipt_retry_attempts=_positive_int(
                values.get("AGENTSCOPE_RUNTIME_RECEIPT_RETRY_ATTEMPTS", "5"),
                name="AGENTSCOPE_RUNTIME_RECEIPT_RETRY_ATTEMPTS",
            ),
            receipt_retry_backoff_seconds=_positive_float(
                values.get("AGENTSCOPE_RUNTIME_RECEIPT_RETRY_BACKOFF_SECONDS", "0.2"),
                name="AGENTSCOPE_RUNTIME_RECEIPT_RETRY_BACKOFF_SECONDS",
            ),
            receipt_flush_timeout_seconds=_positive_float(
                values.get("AGENTSCOPE_RUNTIME_RECEIPT_FLUSH_TIMEOUT_SECONDS", "10"),
                name="AGENTSCOPE_RUNTIME_RECEIPT_FLUSH_TIMEOUT_SECONDS",
            ),
            host=values.get("AGENTSCOPE_RUNTIME_HOST", "0.0.0.0"),
            port=port,
            require_read_only_source_mounts=True,
        )

    def prepare_writable_directories(self) -> None:
        """Create only Runtime-owned writable roots; source Harness roots stay read-only."""

        for path in (self.data_dir, self.workspaces_root):
            if path.is_symlink():
                raise ValueError("Runtime-owned writable roots must not be symlinks")
            path.mkdir(parents=True, exist_ok=True)
            if not path.is_dir():
                raise ValueError("Runtime-owned writable roots must be directories")

    def validate_source_mounts(self) -> None:
        """Production env must expose Harness sources through kernel read-only mounts."""

        if not self.require_read_only_source_mounts:
            return
        for path in (self.business_agents_root, self.candidates_root):
            if path.is_symlink() or not path.is_dir():
                raise ValueError("Harness source roots must be existing non-symlink directories")
            if not os.statvfs(path).f_flag & os.ST_RDONLY:
                raise ValueError("Harness source roots must be mounted read-only")
