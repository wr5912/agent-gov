from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast
from urllib.parse import quote

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.runtime.recovery_read_only_git import (
    HardenedReadOnlyGitRepository,
    workspace_bytes_fingerprint,
)
from app.runtime.workspace_activation_graph import (
    workspace_activation_early_graph_shape_is_valid,
    workspace_activation_graph_is_valid,
    workspace_activation_graph_shape_is_valid,
)

if TYPE_CHECKING:
    from app.services.agent_workspace_activation_recovery import (
        WorkspaceActivationOperatorRecoveryService,
    )


class OperatorRecoveryError(RuntimeError):
    """可安全投影到本机恢复 CLI 的稳定错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


DurableRefMap = TypedDict(
    "DurableRefMap",
    {
        "original": str,
        "base": str,
        "candidate": str,
        "target": str,
        "original-index-tree": str,
    },
    total=False,
)
RefObjectTypeMap = TypedDict(
    "RefObjectTypeMap",
    {
        "original": str,
        "base": str,
        "candidate": str,
        "target": str,
        "original-index-tree": str,
    },
    total=False,
)


class RepositoryFingerprint(TypedDict, total=False):
    available: bool
    error_code: str | None
    head_sha: str | None
    head_position: str
    status_digest: str | None
    index_digest: str | None
    workspace_digest: str | None
    clean: bool
    object_types: RefObjectTypeMap
    graph_valid: bool
    live_state_valid: bool
    refs: DurableRefMap


class OperationFingerprint(TypedDict):
    operation_id: str
    import_id: str | None
    agent_id: str
    action: str
    state: str
    original_head_sha: str
    base_commit_sha: str | None
    candidate_commit_sha: str | None
    candidate_tree_sha: str | None
    target_commit_sha: str | None
    snapshot_created: bool
    original_status_digest: str
    original_index_fingerprint: str
    original_workspace_fingerprint: str
    original_index_tree_sha: str | None
    original_index_snapshot_digest: str
    recovery_phase: str
    package_sha256: str | None
    tree_sha256: str | None
    suite_status: str | None
    suite_digest: str
    diagnostics_digest: str
    maintenance_token_digest: str
    maintenance_generation: int
    maintenance_expires_at: str
    error_digest: str
    created_at: str
    updated_at: str
    completed_at: str | None


class AuditFingerprint(TypedDict):
    import_id: str
    agent_id: str
    action: str
    status: str
    package_sha256: str | None
    tree_sha256: str | None
    commit_sha: str | None
    suite_status: str | None
    suite_digest: str
    diagnostics_digest: str
    error_digest: str
    created_at: str
    completed_at: str | None


class AdmissionFingerprint(TypedDict):
    maintenance_token_digest: str
    maintenance_generation: int
    generation: int
    maintenance_kind: str | None
    maintenance_owner_id_digest: str
    maintenance_expires_at: str | None
    updated_at: str


class ActiveCounts(TypedDict):
    active_sessions: int
    active_turns: int
    active_hitl: int
    active_tests: int


RecoveryRefMode = Literal["preparing", "journal", "staged", "invalid"]


class RecoveryAttemptFingerprint(TypedDict):
    recovery_id: str
    action: str
    state: str
    requested_state_digest: str
    observed_state_digest: str | None
    observed_context_digest: str | None
    operator_digest: str
    reason_digest: str
    result_digest: str
    error_digest: str
    created_at: str
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True)
class RepositoryObservation:
    available: bool
    error_code: str | None
    head_sha: str | None
    head_position: str
    status_digest: str | None
    index_digest: str | None
    workspace_digest: str | None
    clean: bool
    refs: DurableRefMap
    object_types: RefObjectTypeMap
    graph_valid: bool
    live_state_valid: bool

    def fingerprint(self, *, include_refs: bool) -> RepositoryFingerprint:
        fingerprint = RepositoryFingerprint(
            available=self.available,
            error_code=self.error_code,
            head_sha=self.head_sha,
            head_position=self.head_position,
            status_digest=self.status_digest,
            index_digest=self.index_digest,
            workspace_digest=self.workspace_digest,
            clean=self.clean,
            object_types=self.object_types,
            graph_valid=self.graph_valid,
            live_state_valid=self.live_state_valid,
        )
        if include_refs:
            fingerprint["refs"] = self.refs
        return fingerprint


def canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        _json_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def value_digest(payload: object) -> str:
    return canonical_digest(payload).removeprefix("sha256:")


def normalize_operation_id(value: str) -> str:
    normalized = value.strip()
    suffix = normalized.removeprefix("wao-")
    try:
        parsed = uuid.UUID(suffix)
    except (AttributeError, ValueError) as exc:
        raise OperatorRecoveryError(
            "INVALID_OPERATION_ID",
            "operation_id must be a canonical wao- UUID",
        ) from exc
    if normalized != f"wao-{parsed}":
        raise OperatorRecoveryError(
            "INVALID_OPERATION_ID",
            "operation_id must be a canonical wao- UUID",
        )
    return normalized


def normalize_recovery_id(value: str) -> str:
    normalized = value.strip()
    suffix = normalized.removeprefix("war-")
    try:
        parsed = uuid.UUID(suffix)
    except (AttributeError, ValueError) as exc:
        raise OperatorRecoveryError(
            "INVALID_RECOVERY_ID",
            "recovery_id must be a canonical war- UUID",
        ) from exc
    if normalized != f"war-{parsed}":
        raise OperatorRecoveryError(
            "INVALID_RECOVERY_ID",
            "recovery_id must be a canonical war- UUID",
        )
    return normalized


def new_recovery_id() -> str:
    return f"war-{uuid.uuid4()}"


def normalize_state_digest(value: str) -> str:
    normalized = value.strip().lower()
    prefix, separator, digest = normalized.partition(":")
    if prefix != "sha256" or not separator or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise OperatorRecoveryError(
            "INVALID_STATE_DIGEST",
            "state_digest must use sha256:<64 lowercase hex characters>",
        )
    return normalized


def normalize_audit_text(value: str, *, field: str, maximum: int) -> str:
    normalized = value.strip()
    invalid_control = any(ord(character) < 32 and character not in "\t" for character in normalized)
    if not normalized or len(normalized) > maximum or invalid_control:
        raise OperatorRecoveryError(
            f"INVALID_{field.upper()}",
            f"{field} must contain 1 to {maximum} printable characters",
        )
    return normalized


def read_only_session_factory(db_path: Path) -> tuple[sessionmaker, Engine]:
    resolved = db_path.expanduser().resolve()
    if not resolved.is_file():
        raise OperatorRecoveryError(
            "RUNTIME_DB_UNAVAILABLE",
            "Runtime database is unavailable",
        )
    uri = f"file:{quote(resolved.as_posix(), safe='/')}?mode=ro"

    def connect_read_only() -> sqlite3.Connection:
        connection = sqlite3.connect(
            uri,
            uri=True,
            check_same_thread=False,
            timeout=30.0,
        )
        connection.execute("PRAGMA query_only=ON")
        return connection

    engine = create_engine("sqlite://", creator=connect_read_only, future=True)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True), engine


def observe_activation_repository(
    repository: Path,
    *,
    operation: dict[str, object],
    expected_refs: DurableRefMap,
) -> RepositoryObservation:
    if not repository.is_dir() or not (repository / ".git").exists():
        return _unavailable_repository("WORKSPACE_REPOSITORY_UNAVAILABLE")
    try:
        git = HardenedReadOnlyGitRepository(repository)
        head = git.head_sha()
        status = git.status()
        index_digest = git.index_fingerprint()
        workspace_digest = workspace_bytes_fingerprint(repository)
        refs = _read_operation_refs(
            git,
            operation_id=str(operation["operation_id"]),
        )
        ref_mode = workspace_activation_ref_mode(operation)
        direct_empty = not refs and ref_mode in {"preparing", "staged"}
        if direct_empty:
            object_types = RefObjectTypeMap()
            graph_valid = _direct_empty_graph_is_valid(operation, ref_mode=ref_mode)
        elif expected_refs:
            object_types = cast(
                RefObjectTypeMap,
                {name: git.object_type(object_sha) for name, object_sha in expected_refs.items()},
            )
            graph_valid = _graph_is_valid(git, operation, object_types)
        else:
            object_types = RefObjectTypeMap()
            graph_valid = False
        position = _head_position(head, operation)
        live_valid = _live_state_is_valid(
            head_position=position,
            clean=not status,
            status=status,
            index_digest=index_digest,
            workspace_digest=workspace_digest,
            operation=operation,
        )
        return RepositoryObservation(
            available=True,
            error_code=None,
            head_sha=head,
            head_position=position,
            status_digest=value_digest(status),
            index_digest=index_digest,
            workspace_digest=workspace_digest,
            clean=not status,
            refs=refs,
            object_types=object_types,
            graph_valid=graph_valid,
            live_state_valid=live_valid,
        )
    except Exception:  # noqa: BLE001 - raw Git/path details must never reach operator output.
        return _unavailable_repository("WORKSPACE_REPOSITORY_INSPECTION_FAILED")


def workspace_activation_ref_mode(
    operation: dict[str, object],
) -> RecoveryRefMode:
    state = str(operation["state"])
    phase = str(operation["recovery_phase"])
    base = operation.get("base_commit_sha")
    candidate = operation.get("candidate_commit_sha")
    candidate_tree = operation.get("candidate_tree_sha")
    original_index_tree = operation.get("original_index_tree_sha")
    journal_complete = all(isinstance(value, str) and bool(value) for value in (base, candidate, candidate_tree, original_index_tree))
    early_rejection = not any(
        (base, candidate, candidate_tree, original_index_tree),
    )
    if phase == "completion_outcome":
        valid_state = state in {"completing", "recovery_required", "completed"}
        return "staged" if valid_state and journal_complete else "invalid"
    if phase == "rejection_outcome":
        valid_state = state in {"rejecting", "recovery_required", "rejected"}
        return "staged" if valid_state and (journal_complete or early_rejection) else "invalid"
    if state == "preparing" and phase == "none":
        return "preparing"
    if state == "recovery_required" and phase == "none" and (not base or not candidate):
        return "preparing"
    journal_state = state in {"prepared", "recovery_required"}
    compensation = phase in {
        "candidate_reset",
        "base_reset",
        "head_reset",
        "index_restore",
    }
    if journal_complete and ((journal_state and phase == "none") or (state == "recovery_required" and compensation)):
        return "journal"
    return "invalid"


def _unavailable_repository(code: str) -> RepositoryObservation:
    return RepositoryObservation(
        available=False,
        error_code=code,
        head_sha=None,
        head_position="unavailable",
        status_digest=None,
        index_digest=None,
        workspace_digest=None,
        clean=False,
        refs={},
        object_types={},
        graph_valid=False,
        live_state_valid=False,
    )


def _read_operation_refs(
    git: HardenedReadOnlyGitRepository,
    *,
    operation_id: str,
) -> DurableRefMap:
    prefix = f"refs/agentgov/workspace-activations/{operation_id}/"
    raw = git.operation_ref_rows(prefix)
    refs: dict[str, str] = {}
    for line in raw.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            raise OperatorRecoveryError(
                "DURABLE_REF_NAMESPACE_INVALID",
                "Workspace activation durable ref namespace is invalid",
            )
        ref_name, object_sha, symbolic_target = fields
        if symbolic_target or not ref_name.startswith(prefix):
            raise OperatorRecoveryError(
                "DURABLE_REF_NAMESPACE_INVALID",
                "Workspace activation durable ref namespace is invalid",
            )
        refs[ref_name.removeprefix(prefix)] = object_sha
    return cast(DurableRefMap, refs)


def _graph_is_valid(
    git: HardenedReadOnlyGitRepository,
    operation: dict[str, object],
    object_types: RefObjectTypeMap,
) -> bool:
    expected_types = {name: "tree" if name == "original-index-tree" else "commit" for name in object_types}
    if object_types != expected_types:
        return False
    return workspace_activation_graph_is_valid(
        action=str(operation["action"]),
        snapshot_created=bool(operation["snapshot_created"]),
        original_commit=str(operation["original_head_sha"] or ""),
        base_commit=str(operation["base_commit_sha"] or ""),
        candidate_commit=str(operation["candidate_commit_sha"] or ""),
        candidate_tree=str(operation["candidate_tree_sha"] or ""),
        target_commit=str(operation["target_commit_sha"]) if operation["target_commit_sha"] else None,
        original_index_tree=str(operation["original_index_tree_sha"] or ""),
        object_type=git.object_type,
        commit_parent_shas=git.commit_parent_shas,
        commit_tree_sha=git.commit_tree_sha,
    )


def _direct_empty_graph_is_valid(
    operation: dict[str, object],
    *,
    ref_mode: RecoveryRefMode,
) -> bool:
    fields = {
        "action": str(operation["action"]),
        "snapshot_created": bool(operation["snapshot_created"]),
        "original_commit": str(operation["original_head_sha"] or ""),
        "base_commit": str(operation["base_commit_sha"]) if operation["base_commit_sha"] else None,
        "candidate_commit": str(operation["candidate_commit_sha"]) if operation["candidate_commit_sha"] else None,
        "candidate_tree": str(operation["candidate_tree_sha"]) if operation["candidate_tree_sha"] else None,
        "target_commit": str(operation["target_commit_sha"]) if operation["target_commit_sha"] else None,
        "original_index_tree": str(operation["original_index_tree_sha"]) if operation["original_index_tree_sha"] else None,
    }
    if workspace_activation_early_graph_shape_is_valid(**fields):
        return True
    if ref_mode != "staged":
        return False
    return workspace_activation_graph_shape_is_valid(
        **{key: value or "" for key, value in fields.items() if key != "snapshot_created"},
        snapshot_created=fields["snapshot_created"],
    )


def _head_position(head: str, operation: dict[str, object]) -> str:
    candidates = {
        "candidate": operation.get("candidate_commit_sha"),
        "base": operation.get("base_commit_sha"),
        "original": operation.get("original_head_sha"),
        "target": operation.get("target_commit_sha"),
    }
    return next((name for name, object_sha in candidates.items() if head == object_sha), "unknown")


def _live_state_is_valid(
    *,
    head_position: str,
    clean: bool,
    status: str,
    index_digest: str,
    workspace_digest: str,
    operation: dict[str, object],
) -> bool:
    phase = operation["recovery_phase"]
    ref_mode = workspace_activation_ref_mode(operation)
    if phase == "completion_outcome":
        return head_position == "candidate" and clean
    if phase == "rejection_outcome" or ref_mode == "preparing":
        return _original_live_state_is_valid(
            head_position=head_position,
            status=status,
            index_digest=index_digest,
            workspace_digest=workspace_digest,
            operation=operation,
        )
    if head_position == "candidate":
        return clean
    if head_position == "base" and operation["base_commit_sha"] != operation["original_head_sha"]:
        return clean
    if head_position not in {"original", "base"}:
        return False
    return _original_live_state_is_valid(
        head_position=head_position,
        status=status,
        index_digest=index_digest,
        workspace_digest=workspace_digest,
        operation=operation,
    )


def _original_live_state_is_valid(
    *,
    head_position: str,
    status: str,
    index_digest: str,
    workspace_digest: str,
    operation: dict[str, object],
) -> bool:
    at_original = head_position == "original" or (head_position == "base" and operation["base_commit_sha"] == operation["original_head_sha"])
    return (
        at_original
        and value_digest(status) == operation["original_status_digest"]
        and index_digest == operation["original_index_fingerprint"]
        and workspace_digest == operation["original_workspace_fingerprint"]
    )


def print_json(payload: dict[str, object], *, stream: Any | None = None) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        file=stream or sys.stdout,
        flush=True,
    )


def build_workspace_activation_recovery_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or reconcile one durable Workspace activation operation.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    listing = subparsers.add_parser(
        "list",
        help="List fenced activation operations read-only.",
    )
    listing.add_argument("--limit", type=int, default=100)
    inspection = subparsers.add_parser(
        "inspect",
        help="Inspect one activation operation read-only.",
    )
    inspection.add_argument("--operation-id", required=True)
    apply = subparsers.add_parser(
        "apply",
        help="Apply one exact, journaled recovery attempt.",
    )
    apply.add_argument("--operation-id", required=True)
    apply.add_argument("--recovery-id", required=True)
    apply.add_argument("--state-digest", required=True)
    apply.add_argument("--operator", required=True)
    apply.add_argument("--reason", required=True)
    apply.add_argument(
        "--action",
        choices=("reconcile", "repair-missing-refs"),
        default="reconcile",
    )
    resume = subparsers.add_parser(
        "resume",
        help="Resume one exact reserved recovery attempt from its durable journal.",
    )
    resume.add_argument("--recovery-id", required=True)
    return parser


def run_workspace_activation_recovery_cli(
    argv: Sequence[str] | None = None,
    *,
    service: WorkspaceActivationOperatorRecoveryService | None = None,
) -> int:
    from app.runtime.settings import get_settings
    from app.services.agent_workspace_activation_recovery import (
        WorkspaceActivationOperatorRecoveryService,
        build_default_workspace_activation_recovery_service,
    )

    args = build_workspace_activation_recovery_parser().parse_args(argv)
    engine = None
    try:
        settings = get_settings()
        if service is None and args.command in {"list", "inspect"}:
            factory, engine = read_only_session_factory(settings.runtime_db_path)
            service = WorkspaceActivationOperatorRecoveryService(
                session_factory=factory,
                data_dir=settings.data_dir,
            )
        elif service is None:
            service = build_default_workspace_activation_recovery_service(settings)
        assert service is not None
        _execute_workspace_activation_command(args, service)
        return 0
    except OperatorRecoveryError as exc:
        print_json(
            {"status": "error", "code": exc.code, "message": str(exc)},
            stream=sys.stderr,
        )
        return 2
    except (OSError, sqlite3.Error, SQLAlchemyError):
        print_json(
            {
                "status": "error",
                "code": "RECOVERY_RUNTIME_UNAVAILABLE",
                "message": "Workspace activation recovery runtime is unavailable",
            },
            stream=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


def _execute_workspace_activation_command(
    args: argparse.Namespace,
    service: WorkspaceActivationOperatorRecoveryService,
) -> None:
    from app.runtime.workspace_activation_recovery import (
        RecoveryAction,
        RecoveryAttemptRequest,
    )

    if args.command == "list":
        print_json(
            {
                "mode": "read-only",
                "status": "ok",
                "operations": service.list(limit=args.limit),
            }
        )
        return
    if args.command == "inspect":
        print_json(
            {
                "mode": "read-only",
                "status": "ok",
                "recovery_id": new_recovery_id(),
                **service.inspect(args.operation_id),
            }
        )
        return
    if args.command == "resume":
        outcome = service.resume(args.recovery_id)
        print_json({"mode": "resume", "status": "ok", **outcome})
        return
    action = "repair_missing_refs" if args.action == "repair-missing-refs" else "reconcile"
    outcome = service.apply(
        RecoveryAttemptRequest(
            recovery_id=args.recovery_id,
            operation_id=args.operation_id,
            action=cast(RecoveryAction, action),
            state_digest=args.state_digest,
            operator=args.operator,
            reason=args.reason,
        )
    )
    print_json({"mode": "apply", "status": "ok", **outcome})


def main(argv: Sequence[str] | None = None) -> int:
    return run_workspace_activation_recovery_cli(argv)


def _json_value(value: object) -> object:
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


if __name__ == "__main__":
    raise SystemExit(main())
