from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

RECEIPT_CONTRACT = "agentgov.agent-test-execution-receipt.v1"
P0_EXACT_COMMIT_LANE = "p0-exact-commit"
SUPPORTED_RECEIPT_CONTRACTS = frozenset({RECEIPT_CONTRACT})
FIXED_PYTEST_COMMAND = (
    "/usr/local/bin/python",
    "-I",
    "-P",
    "-m",
    "pytest",
    "-q",
    "--import-mode=importlib",
    "-p",
    "agentgov_testkit.pytest_plugin",
    "tests",
)
FIXED_SANDBOX_ENV: Mapping[str, str] = MappingProxyType(
    {
        "AGENTGOV_TEST_REPORT_PATH": "/output/pytest-report.json",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
)
SANDBOX_USER = "65532:65532"
SANDBOX_WORKSPACE_MOUNT_TYPE: Final[Literal["volume"]] = "volume"
SANDBOX_WORKSPACE_SOURCE_SCOPE: Final[Literal["run_workspace_subpath"]] = "run_workspace_subpath"
SANDBOX_PIDS_LIMIT = 256
SANDBOX_MEMORY_BYTES = 512 * 1024 * 1024
SANDBOX_NANO_CPUS = 1_000_000_000
SANDBOX_TMPFS_SIZE_BYTES = 64 * 1024 * 1024
SANDBOX_SHM_SIZE_BYTES = 16 * 1024 * 1024
SANDBOX_LOG_MAX_BYTES = 1024 * 1024
SANDBOX_LOG_MAX_FILES = 1

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_COMMIT_PATTERN = r"^[0-9a-f]{40}$"
_IMAGE_ID_PATTERN = r"^sha256:[0-9a-f]{64}$"
_CONTAINER_ID_PATTERN = r"^[0-9a-f]{12,128}$"
_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_TERMINAL_STATUSES = {"passed", "failed", "error", "cancelled", "interrupted"}
SandboxEnvironment: TypeAlias = dict[str, str]
SourceObservation: TypeAlias = Literal["not_observed", "pre_only", "stable", "changed"]


class _StrictReceiptModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentTestTargetReceipt(_StrictReceiptModel):
    agent_id: str = Field(min_length=1, max_length=128)
    commit_sha: str = Field(pattern=_COMMIT_PATTERN)
    tree_sha: str = Field(pattern=_COMMIT_PATTERN)
    source_digest: str = Field(pattern=_SHA256_PATTERN)
    pre_source_digest: str | None = Field(pattern=_SHA256_PATTERN)
    post_source_digest: str | None = Field(pattern=_SHA256_PATTERN)
    source_observation: SourceObservation
    suite_digest: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def observation_must_match_available_evidence(self) -> AgentTestTargetReceipt:
        if self.source_observation == "not_observed":
            if self.pre_source_digest is not None or self.post_source_digest is not None:
                raise ValueError("not_observed source cannot claim pre or post evidence")
        elif self.source_observation == "pre_only":
            if self.pre_source_digest is None or self.post_source_digest is not None:
                raise ValueError("pre_only source requires only a pre-execution digest")
        elif self.source_observation == "stable":
            if self.pre_source_digest != self.source_digest or self.post_source_digest != self.source_digest:
                raise ValueError("stable source requires matching target, pre, and post digests")
        elif self.pre_source_digest != self.source_digest or self.post_source_digest in {None, self.pre_source_digest}:
            raise ValueError("changed source requires the expected pre digest and a different observed post digest")
        return self


class AgentTestSandboxMountReceipt(_StrictReceiptModel):
    target: Literal["/workspace"]
    read_only: Literal[True]
    mount_type: Literal["volume"]
    source_scope: Literal["run_workspace_subpath"]


class AgentTestInvocationReceipt(_StrictReceiptModel):
    image_id: str = Field(pattern=_IMAGE_ID_PATTERN)
    argv: tuple[str, ...] = Field(min_length=len(FIXED_PYTEST_COMMAND), max_length=len(FIXED_PYTEST_COMMAND))
    environment_keys: tuple[str, ...] = Field(min_length=len(FIXED_SANDBOX_ENV), max_length=len(FIXED_SANDBOX_ENV))
    environment_digest: str = Field(pattern=_SHA256_PATTERN)
    working_directory: Literal["/workspace"]

    @model_validator(mode="after")
    def invocation_must_match_platform_contract(self) -> AgentTestInvocationReceipt:
        if self.argv != FIXED_PYTEST_COMMAND:
            raise ValueError("pytest argv does not match the platform contract")
        if self.environment_keys != tuple(sorted(FIXED_SANDBOX_ENV)):
            raise ValueError("sandbox environment keys do not match the platform allowlist")
        if self.environment_digest != sandbox_environment_digest():
            raise ValueError("sandbox environment digest does not match the platform contract")
        return self


class AgentTestIsolationReceipt(_StrictReceiptModel):
    user: Literal["65532:65532"]
    network_mode: Literal["none"]
    network_disabled: Literal[True]
    pid_mode: Literal["private"]
    ipc_mode: Literal["private"]
    uts_mode: Literal["private"]
    readonly_rootfs: Literal[True]
    cap_drop: tuple[str, ...] = Field(min_length=1, max_length=1)
    security_opt: tuple[str, ...] = Field(min_length=1, max_length=1)
    privileged: Literal[False]
    devices: tuple[str, ...] = Field(max_length=0)
    mounts: tuple[AgentTestSandboxMountReceipt, ...] = Field(min_length=1, max_length=1)
    pids_limit: Literal[256]
    memory_bytes: Literal[536870912]
    memory_swap_bytes: Literal[536870912]
    nano_cpus: Literal[1000000000]
    tmpfs_targets: tuple[str, ...] = Field(min_length=2, max_length=2)
    tmpfs_size_bytes: Literal[67108864]
    tmpfs_noexec: Literal[True]
    tmpfs_nosuid: Literal[True]
    tmpfs_nodev: Literal[True]
    shm_size_bytes: Literal[16777216]
    ports_published: Literal[False]
    auto_remove: Literal[False]
    restart_policy: Literal["no"]
    log_driver: Literal["local"]
    log_max_bytes: Literal[1048576]
    log_max_files: Literal[1]
    log_compression: Literal[False]
    docker_socket_mounted: Literal[False]

    @model_validator(mode="after")
    def isolation_must_match_platform_contract(self) -> AgentTestIsolationReceipt:
        if self.cap_drop != ("ALL",):
            raise ValueError("sandbox must drop every Linux capability")
        if self.security_opt != ("no-new-privileges",):
            raise ValueError("sandbox must enable no-new-privileges")
        mounts = {item.target: (item.read_only, item.mount_type, item.source_scope) for item in self.mounts}
        if mounts != {
            "/workspace": (
                True,
                SANDBOX_WORKSPACE_MOUNT_TYPE,
                SANDBOX_WORKSPACE_SOURCE_SCOPE,
            )
        }:
            raise ValueError("sandbox mounts do not match the platform contract")
        if self.tmpfs_targets != ("/output", "/tmp"):
            raise ValueError("sandbox tmpfs targets do not match the platform contract")
        return self


class AgentTestResultReceipt(_StrictReceiptModel):
    status: Literal["passed", "failed", "error", "cancelled", "interrupted"]
    exit_code: int | None
    duration_ms: int = Field(ge=0)
    workspace_report_authority: Literal["agent_owned_unverified"]
    workspace_report_digest: str = Field(pattern=_SHA256_PATTERN)
    stdout_digest: str = Field(pattern=_SHA256_PATTERN)
    stderr_digest: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def exit_code_must_match_status(self) -> AgentTestResultReceipt:
        if self.status == "passed" and self.exit_code != 0:
            raise ValueError("passed receipt requires pytest exit code 0")
        if self.status == "failed" and (self.exit_code is None or self.exit_code == 0):
            raise ValueError("failed receipt requires a non-zero pytest exit code")
        return self


class AgentTestCleanupReceipt(_StrictReceiptModel):
    container_removed: bool
    label_residue_absent: bool
    temporary_paths_removed: bool
    error_codes: tuple[str, ...]

    @model_validator(mode="after")
    def errors_must_be_stable_codes(self) -> AgentTestCleanupReceipt:
        if len(self.error_codes) > 16 or any(not _ERROR_CODE_PATTERN.fullmatch(code) for code in self.error_codes):
            raise ValueError("cleanup errors must be bounded stable error codes")
        return self

    @property
    def complete(self) -> bool:
        return self.container_removed and self.label_residue_absent and self.temporary_paths_removed and not self.error_codes


class AgentTestExecutionReceipt(_StrictReceiptModel):
    contract: Literal["agentgov.agent-test-execution-receipt.v1"]
    lane: Literal["p0-exact-commit"]
    assurance_level: Literal["execution_provenance"]
    receipt_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    test_run_id: str = Field(min_length=1, max_length=128)
    worker_id: str = Field(min_length=1, max_length=128)
    container_id: str | None = Field(default=None, pattern=_CONTAINER_ID_PATTERN)
    target: AgentTestTargetReceipt
    invocation: AgentTestInvocationReceipt | None
    isolation: AgentTestIsolationReceipt | None
    result: AgentTestResultReceipt
    cleanup: AgentTestCleanupReceipt

    @model_validator(mode="after")
    def evidence_must_match_terminal_result(self) -> AgentTestExecutionReceipt:
        if self.result.status not in _TERMINAL_STATUSES:  # pragma: no cover - Literal is the first boundary
            raise ValueError("receipt status must be terminal")
        if self.result.status in {"passed", "failed"} and (self.invocation is None or self.isolation is None):
            raise ValueError("pytest terminal result requires invocation and isolation evidence")
        if self.result.status in {"passed", "failed"} and self.target.source_observation != "stable":
            raise ValueError("pytest terminal result requires stable pre/post source evidence")
        if self.target.source_observation in {"pre_only", "changed"} and self.result.status != "error":
            raise ValueError("incomplete or changed source observation must force the execution result to error")
        if not self.cleanup.complete and self.result.status != "error":
            raise ValueError("cleanup failure must force the execution result to error")
        return self

    def with_digest(self) -> AgentTestExecutionReceipt:
        return self.model_copy(update={"receipt_digest": receipt_digest(self)})


class AgentOwnedPytestReportItem(_StrictReceiptModel):
    nodeid: str = Field(min_length=1, max_length=2048)
    outcome: Literal["passed", "failed", "skipped"]
    duration_seconds: float = Field(default=0.0, ge=0)
    phase: Literal["setup", "call", "teardown"] = "call"
    detail: str | None = Field(default=None, max_length=65536)


class AgentOwnedInvocationRecord(_StrictReceiptModel):
    run_id: str | None = Field(default=None, max_length=256)
    session_id: str | None = Field(default=None, max_length=256)
    agent_version_id: str | None = Field(default=None, max_length=256)
    langfuse_trace_id: str | None = Field(default=None, max_length=256)
    langfuse_trace_url: str | None = Field(default=None, max_length=4096)
    errors: tuple[str, ...] = Field(default=(), max_length=128)


class AgentOwnedPytestReport(_StrictReceiptModel):
    """Bounded diagnostic payload produced inside the Agent-owned pytest process."""

    exit_code: int
    items: tuple[AgentOwnedPytestReportItem, ...] = Field(default=(), max_length=10000)
    invocations: tuple[AgentOwnedInvocationRecord, ...] = Field(default=(), max_length=1000)

    @model_validator(mode="after")
    def outcomes_must_be_consistent_with_reported_exit(self) -> AgentOwnedPytestReport:
        if self.exit_code == 0 and (not self.items or any(item.outcome != "passed" for item in self.items)):
            raise ValueError("exit 0 workspace report requires at least one passed item and no non-passed items")
        if self.exit_code == 1 and not any(item.outcome == "failed" for item in self.items):
            raise ValueError("exit 1 workspace report requires at least one failed item")
        return self


def sandbox_environment() -> SandboxEnvironment:
    """Return a fresh fixed environment without inheriting the worker process."""

    return dict(FIXED_SANDBOX_ENV)


def canonical_json_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sandbox_environment_digest() -> str:
    return canonical_json_digest(dict(sorted(FIXED_SANDBOX_ENV.items())))


def receipt_digest(receipt: AgentTestExecutionReceipt | Mapping[str, object]) -> str:
    payload = (
        receipt.model_dump(mode="json", exclude={"receipt_digest"})
        if isinstance(receipt, AgentTestExecutionReceipt)
        else {key: value for key, value in receipt.items() if key != "receipt_digest"}
    )
    return canonical_json_digest(payload)


def verify_receipt_integrity(receipt: AgentTestExecutionReceipt) -> bool:
    actual = receipt.receipt_digest
    return actual is not None and secrets.compare_digest(actual, receipt_digest(receipt))
