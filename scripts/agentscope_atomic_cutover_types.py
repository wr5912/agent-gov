"""Owned mapping types shared by the AgentScope atomic cutover modules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NotRequired, TypeAlias, TypedDict, TypeGuard

EnvValues: TypeAlias = dict[str, str]
JsonObject: TypeAlias = dict[str, object]

REQUIRED_MANIFEST_STRING_FIELDS = (
    "cutover_id",
    "state",
    "prepared_at",
    "runtime_root",
    "source_env",
    "source_env_sha256",
    "source_artifact_sha256",
    "env_snapshot",
    "env_snapshot_sha256",
    "snapshot_archive",
    "snapshot_sha256",
    "restore_drill",
    "rollback_compose_source",
    "rollback_compose_source_sha256",
    "rollback_compose",
    "rollback_compose_sha256",
    "rollback_image_inventory",
    "rollback_image_inventory_sha256",
    "rollback_image_archive",
    "rollback_image_archive_sha256",
    "rollback_image_restore_drill",
    "execute_token_sha256",
    "finalize_token_sha256",
)


class TreeEntry(TypedDict):
    path: str
    type: str
    mode: int
    uid: int
    gid: int
    size: int
    sha256: NotRequired[str]


class GateState(TypedDict, total=False):
    schema_version: int
    state: str
    cutover_id: str
    updated_at: str
    irreversible_at: str


CoreImageIds = TypedDict(
    "CoreImageIds",
    {
        "agentscope-runtime": str,
        "agent-gov-api": str,
        "agent-gov-ui": str,
    },
)


class RollbackImage(TypedDict):
    reference: str
    id: str
    repo_digests: NotRequired[object]


def is_tree_entry_list(value: object) -> TypeGuard[list[TreeEntry]]:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        path = item.get("path")
        entry_type = item.get("type")
        numeric = (item.get("mode"), item.get("uid"), item.get("gid"), item.get("size"))
        if not isinstance(path, str) or not path or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            return False
        if entry_type not in {"file", "dir"} or not all(type(part) is int for part in numeric):
            return False
        digest = item.get("sha256")
        if entry_type == "file" and (not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
            return False
    return True


def is_rollback_image_list(value: object) -> TypeGuard[list[RollbackImage]]:
    if not isinstance(value, list) or not value:
        return False
    return all(
        isinstance(item, dict)
        and isinstance(item.get("reference"), str)
        and bool(item.get("reference"))
        and isinstance(item.get("id"), str)
        and str(item.get("id")).startswith("sha256:")
        for item in value
    )


class RollbackBundle(TypedDict):
    rollback_compose_source: str
    rollback_compose_source_sha256: str
    rollback_compose: str
    rollback_compose_sha256: str
    rollback_image_inventory: str
    rollback_image_inventory_sha256: str
    rollback_image_archive: str
    rollback_image_archive_sha256: str
    rollback_image_restore_drill: str
    rollback_images: list[RollbackImage]


class EvidenceGate(TypedDict):
    status: str
    receipt_path: str
    receipt_sha256: str


class StaticGateResult(TypedDict):
    checks: list[str]
    failure_count: int


class ContractGateResult(TypedDict):
    contracts: list[str]
    passed: int
    failed: int
    skipped: int


class ContainerGateResult(TypedDict):
    profile: str
    services: list[str]
    fresh_build: bool
    force_recreate: bool
    image_ids: CoreImageIds
    failure_count: int


class BrowserGateResult(TypedDict):
    consecutive_passes: int
    mock_sse: bool
    workflows: list[str]
    failure_count: int
    artifact_set_sha256: str


class LiveRuntimeGateResult(TypedDict):
    total_runs: int
    distinct_inputs: int
    max_concurrency: int
    identity_link_percent: float
    feedback_matches: int
    cross_scope_authorizations: int
    soak_seconds: int
    unexpected_restarts: int
    unhandled_errors: int
    residual_runs: int
    terminal_loss: int
    trace_count: int
    unique_trace_count: int
    missing_required_spans: int
    trace_query_p95_seconds: float
    trace_query_max_seconds: float
    secret_plaintext_hits: int
    p95_latency_ratio: float
    p99_latency_ratio: float
    error_rate_delta_percentage_points: float
    otel_p95_overhead_ratio: float
    run_set_sha256: str
    scenario_set_sha256: str
    trace_set_sha256: str
    soak_artifact_sha256: str


EvidenceResult: TypeAlias = StaticGateResult | ContractGateResult | ContainerGateResult | BrowserGateResult | LiveRuntimeGateResult


class EvidenceReceipt(TypedDict):
    schema_version: int
    producer: str
    gate_id: str
    command_id: str
    receipt_id: str
    cutover_id: str
    source_artifact_sha256: str
    acceptance_artifacts_sha256: str
    acceptance_identity: str
    image_ids: CoreImageIds
    status: str
    started_at: str
    completed_at: str
    exit_code: int
    result: EvidenceResult


class FinalEvidence(TypedDict):
    schema_version: int
    cutover_id: str
    source_artifact_sha256: str
    acceptance_artifacts_sha256: str
    status: str
    static_gates: EvidenceGate
    contract_tests: EvidenceGate
    container_acceptance: EvidenceGate
    browser_acceptance: EvidenceGate
    live_runtime: EvidenceGate


class CutoverManifest(TypedDict):
    schema_version: int
    cutover_id: str
    state: str
    prepared_at: str
    runtime_root: str
    runtime_root_device: int
    runtime_root_inode: int
    source_env: str
    source_env_sha256: str
    source_artifact_sha256: str
    env_snapshot: str
    env_snapshot_sha256: str
    snapshot_archive: str
    snapshot_sha256: str
    snapshot_entries: list[TreeEntry]
    restore_drill: str
    active_counts: object
    rollback_compose_source: str
    rollback_compose_source_sha256: str
    rollback_compose: str
    rollback_compose_sha256: str
    rollback_image_inventory: str
    rollback_image_inventory_sha256: str
    rollback_image_archive: str
    rollback_image_archive_sha256: str
    rollback_image_restore_drill: str
    rollback_images: list[RollbackImage]
    execute_token_sha256: str
    finalize_token_sha256: str
    irreversible: bool
    irreversible_at: NotRequired[str]
    api_gate_state_file: NotRequired[str]
    acceptance_env: NotRequired[str]
    acceptance_failed_at: NotRequired[str]
    acceptance_artifacts: NotRequired[JsonObject]
    executed_at: NotRequired[str]
    production_drain_env: NotRequired[str]
    production_drain_env_sha256: NotRequired[str]
    production_drain_failed_at: NotRequired[str]
    production_drain_ready_at: NotRequired[str]
    production_drain_artifacts: NotRequired[JsonObject]
    deletion_intent_path: NotRequired[str]
    deletion_intent_sha256: NotRequired[str]
    legacy_deletion_deadline: NotRequired[str]
    legacy_deletion_timeout_alert_at: NotRequired[str]
    legacy_deletion_completed_at: NotRequired[str]
    deletion_completion_receipt: NotRequired[str]
    opened_at: NotRequired[str]
    legacy_snapshot_deleted_at: NotRequired[str]
    restore_started_at: NotRequired[str]
    restore_start_failed_at: NotRequired[str]
    restored_at: NotRequired[str]
    rollback_bundle: NotRequired[str]


class ProductionDrainArtifacts(TypedDict):
    evidence_sha256: str
    evidence: FinalEvidence
    openapi_sha256: str
    image_ids: CoreImageIds
    api_mode: str


@dataclass(frozen=True)
class ProductionDrain:
    db_path: Path
    gate_state_file: Path
    evidence_sha256: str
    artifacts: ProductionDrainArtifacts


class DeletionTarget(TypedDict):
    path: str
    sha256: str


class DeletionIntent(TypedDict):
    schema_version: int
    cutover_id: str
    state: str
    created_at: str
    deadline_at: str
    targets: list[DeletionTarget]


class DeletionCompletion(TypedDict):
    schema_version: int
    cutover_id: str
    completed_at: str
    deadline_at: str
    deadline_missed: bool
    deleted_paths: list[str]
