"""持久化 verifier evidence 的自包含结构与摘要校验。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Final

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_VERIFIER_KEYS: Final = {
    "contract",
    "profile",
    "identity",
    "environment_contract",
    "preserved_environment_keys",
    "preserved_environment_prefixes",
    "path_authority",
    "toolchain",
    "toolchain_sha256",
    "invocation_argv",
    "execution_template",
    "contract_sha256",
}
_TOOLCHAIN_KEYS: Final = {
    "contract",
    "node_version",
    "pnpm_version",
    "tools",
    "source_contract_sha256",
    "python_environment",
    "frontend_dependencies",
    "pnpm_runtime",
    "browser_runtime",
    "docker_daemon",
    "private_state_authority_sha256",
}
_DEPENDENCY_KEYS: Final = {"entries", "regular_bytes", "sha256", "projection_sha256"}
_VERIFIER_V1_CONTRACT: Final = "agentgov.container-acceptance-verifier.v1"
_VERIFIER_V1_ENV_CONTRACT: Final = "agentgov.container-acceptance-verifier-env.v1"
_VERIFIER_V1_TOOLCHAIN_CONTRACT: Final = "agentgov.container-acceptance-toolchain.v2"
_VERIFIER_V1_MAKE_EXECUTABLE: Final = "/usr/bin/make"
_VERIFIER_V1_PRESERVED_KEYS: Final = (
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LANGUAGE",
    "NO_PROXY",
    "TZ",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
_VERIFIER_V1_TOOL_COMMANDS: Final = (
    "python",
    "bootstrap-python",
    "node",
    "pnpm",
    "docker-compose",
    "chromium",
    "awk",
    "bash",
    "curl",
    "docker",
    "env",
    "git",
    "make",
    "sh",
    "sleep",
    "tr",
)
_VERIFIER_V1_IDENTITIES: Final = {
    "core": (
        "ui-smoke",
        "ui-feedback-smoke",
        "ui-openai-responses-smoke",
        "ui-playground-cancel-smoke",
        "smoke",
        "container-core-smoke",
        "container-openapi-check",
        "container-live-test",
        "container-speech-summary-test",
    ),
    "langfuse": ("langfuse-smoke",),
    "agent-test": ("container-workspace-pytest-test",),
    "isolated-health": ("container-health-e2e",),
}
_VERSION = re.compile(r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$")


class FrozenVerifierEvidenceError(RuntimeError):
    """持久化 verifier evidence 不满足自包含契约。"""


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _valid_dependency(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == _DEPENDENCY_KEYS
        and type(value.get("entries")) is int
        and 0 <= value["entries"] <= 50_000
        and type(value.get("regular_bytes")) is int
        and 0 <= value["regular_bytes"] <= 2 * 1024 * 1024 * 1024
        and _is_sha256(value.get("sha256"))
        and _is_sha256(value.get("projection_sha256"))
    )


def _valid_toolchain_v2(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _TOOLCHAIN_KEYS or value.get("contract") != _VERIFIER_V1_TOOLCHAIN_CONTRACT:
        return False
    tools = value.get("tools")
    python = value.get("python_environment")
    daemon = value.get("docker_daemon")
    valid_tools = (
        isinstance(tools, list)
        and len(tools) == len(_VERIFIER_V1_TOOL_COMMANDS)
        and all(isinstance(item, dict) and set(item) == {"command", "authority_sha256"} for item in tools)
        and all(isinstance(item["command"], str) and _IDENTITY.fullmatch(item["command"]) is not None for item in tools)
        and tuple(item["command"] for item in tools) == _VERIFIER_V1_TOOL_COMMANDS
        and all(_is_sha256(item["authority_sha256"]) for item in tools)
    )
    valid_python = (
        isinstance(python, dict)
        and set(python) == {"version", "config_sha256", "dependency_contract_sha256", "site_packages"}
        and isinstance(python.get("version"), str)
        and _VERSION.fullmatch(python["version"]) is not None
        and _is_sha256(python.get("config_sha256"))
        and _is_sha256(python.get("dependency_contract_sha256"))
        and _valid_dependency(python.get("site_packages"))
    )
    valid_daemon = (
        isinstance(daemon, dict) and set(daemon) == {"socket_authority_sha256", "daemon_identity_sha256"} and all(_is_sha256(item) for item in daemon.values())
    )
    return (
        valid_tools
        and valid_python
        and valid_daemon
        and isinstance(value.get("node_version"), str)
        and _VERSION.fullmatch(value["node_version"]) is not None
        and isinstance(value.get("pnpm_version"), str)
        and _VERSION.fullmatch(value["pnpm_version"]) is not None
        and _is_sha256(value.get("source_contract_sha256"))
        and _is_sha256(value.get("private_state_authority_sha256"))
        and all(_valid_dependency(value.get(key)) for key in ("frontend_dependencies", "pnpm_runtime", "browser_runtime"))
    )


def verifier_v1_identities(profile: str) -> tuple[str, ...]:
    return _VERIFIER_V1_IDENTITIES.get(profile, ())


def verifier_descriptor(value: object, *, profile: str) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, dict) or set(value) != _VERIFIER_KEYS:
        raise FrozenVerifierEvidenceError("frozen verifier evidence shape is invalid")
    identity = value.get("identity")
    argv = value.get("invocation_argv")
    template = value.get("execution_template")
    unsigned = {key: item for key, item in value.items() if key != "contract_sha256"}
    valid = (
        _IDENTITY.fullmatch(profile) is not None
        and value.get("contract") == _VERIFIER_V1_CONTRACT
        and value.get("profile") == profile
        and isinstance(identity, str)
        and identity in verifier_v1_identities(profile)
        and value.get("environment_contract") == _VERIFIER_V1_ENV_CONTRACT
        and value.get("preserved_environment_keys") == list(_VERIFIER_V1_PRESERVED_KEYS)
        and value.get("preserved_environment_prefixes") == ["LC_"]
        and value.get("path_authority") == "repository-pinned-node-fixed-system-tools-and-bounded-dependencies"
        and isinstance(argv, list)
        and len(argv) == 3
        and all(isinstance(item, str) and item and len(item) <= 512 and not any(ord(char) < 32 for char in item) for item in argv)
        and argv == ["make", "--no-print-directory", f"_{identity}"]
        and isinstance(template, dict)
        and template == {"executable": _VERIFIER_V1_MAKE_EXECUTABLE, "arguments": ["--no-print-directory", "-f", "Makefile", argv[-1]]}
        and _valid_toolchain_v2(value.get("toolchain"))
        and _is_sha256(value.get("toolchain_sha256"))
        and value.get("contract_sha256") == hashlib.sha256(_canonical_json(unsigned)).hexdigest()
    )
    if not valid:
        raise FrozenVerifierEvidenceError("frozen verifier evidence is invalid")
    return identity, tuple(argv)
