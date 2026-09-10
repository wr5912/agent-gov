"""Crash-safe irreversible gate transition and rollback-artifact cleanup."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import NoReturn, cast

try:
    from scripts.agentscope_atomic_cutover_types import (
        CutoverManifest,
        DeletionCompletion,
        DeletionIntent,
        DeletionTarget,
        GateState,
        ProductionDrain,
        ProductionDrainArtifacts,
    )
except ModuleNotFoundError:
    from agentscope_atomic_cutover_types import (
        CutoverManifest,
        DeletionCompletion,
        DeletionIntent,
        DeletionTarget,
        GateState,
        ProductionDrain,
        ProductionDrainArtifacts,
    )


class CutoverRecoverySupport:
    """Resume the irreversible latch from every persisted crash boundary."""

    def __init__(
        self,
        *,
        error_type: type[RuntimeError],
        utc_now: Callable[[], str],
        sha256_file: Callable[[Path], str],
        write_json: Callable[[Path, Mapping[str, object]], None],
        read_gate_state: Callable[[CutoverManifest], GateState | None],
        atomic_write_gate_state: Callable[..., GateState],
        record_ledger: Callable[..., None],
        database_path: Callable[[Path, Path], Path],
    ) -> None:
        self._error_type = error_type
        self._utc_now = utc_now
        self._sha256_file = sha256_file
        self._write_json = write_json
        self._read_gate_state = read_gate_state
        self._atomic_write_gate_state = atomic_write_gate_state
        self._record_ledger = record_ledger
        self._database_path = database_path

    def _fail(self, message: str) -> NoReturn:
        raise self._error_type(message)

    def open_production_gate(
        self,
        *,
        manifest_path: Path,
        manifest: CutoverManifest,
        drain: ProductionDrain,
    ) -> None:
        gate_state = self._read_gate_state(manifest)
        if gate_state is not None and gate_state.get("state") == "open":
            self._reconcile_irreversible_open(manifest_path, manifest, drain.db_path)
            return
        if gate_state is None or gate_state.get("state") != "drain" or gate_state.get("irreversible_at"):
            self._fail("open 前 API gate 必须为可恢复 drain")
        if manifest.get("state") not in {"production_drain_ready", "deletion_intent_ready"}:
            self._fail("open 前 manifest 未持久化 production drain artifacts")
        self._ensure_deletion_intent(manifest_path, manifest)
        irreversible_at = self._utc_now()
        self._atomic_write_gate_state(
            drain.gate_state_file,
            state="open",
            cutover_id=str(manifest["cutover_id"]),
            irreversible_at=irreversible_at,
        )
        self._reconcile_irreversible_open(manifest_path, manifest, drain.db_path)

    def resume_irreversible_transition(
        self,
        *,
        manifest_path: Path,
        manifest: CutoverManifest,
        runtime_root: Path,
        env_file: Path,
        expected_evidence_sha256: str | None = None,
    ) -> None:
        """Resume a pre-open or post-open transition without enabling restore."""

        gate_state = self._read_gate_state(manifest)
        if gate_state is None:
            self._fail("恢复不可逆切换时缺少 API gate-state")
        db_path = self._database_path(runtime_root, env_file)
        if gate_state.get("state") == "open" or gate_state.get("irreversible_at"):
            self._reconcile_irreversible_open(manifest_path, manifest, db_path)
            return
        if gate_state.get("state") != "drain":
            self._fail("只有 drain/open gate 可以恢复 finalize")
        if expected_evidence_sha256 is None:
            self._fail("open 前恢复 finalize 必须重新校验 machine receipts")
        drain = self._production_drain_from_manifest(manifest, db_path, expected_evidence_sha256)
        self.open_production_gate(manifest_path=manifest_path, manifest=manifest, drain=drain)

    def _production_drain_from_manifest(
        self,
        manifest: CutoverManifest,
        db_path: Path,
        expected_evidence_sha256: str,
    ) -> ProductionDrain:
        raw = manifest.get("production_drain_artifacts")
        expected_keys = {"evidence_sha256", "evidence", "openapi_sha256", "image_ids", "api_mode"}
        if not isinstance(raw, dict) or set(raw) != expected_keys or raw.get("api_mode") != "drain":
            self._fail("恢复 finalize 时缺少精确 production drain artifacts")
        evidence_sha = raw.get("evidence_sha256")
        openapi_sha = raw.get("openapi_sha256")
        image_ids = raw.get("image_ids")
        evidence = raw.get("evidence")
        acceptance = manifest.get("acceptance_artifacts")
        if (
            not isinstance(evidence_sha, str)
            or re.fullmatch(r"[0-9a-f]{64}", evidence_sha) is None
            or not isinstance(openapi_sha, str)
            or re.fullmatch(r"[0-9a-f]{64}", openapi_sha) is None
            or not isinstance(evidence, dict)
            or not self._valid_core_image_ids(image_ids)
            or evidence_sha != expected_evidence_sha256
            or not isinstance(acceptance, dict)
            or acceptance.get("openapi_sha256") != openapi_sha
            or acceptance.get("image_ids") != image_ids
        ):
            self._fail("恢复 finalize 时 production drain identity/digest 无效")
        raw_gate = manifest.get("api_gate_state_file")
        if not isinstance(raw_gate, str):
            self._fail("恢复 finalize 时缺少 gate-state 路径")
        artifacts = cast(ProductionDrainArtifacts, raw)
        return ProductionDrain(db_path, Path(raw_gate), evidence_sha, artifacts)

    @staticmethod
    def _valid_core_image_ids(value: object) -> bool:
        expected = {"agentscope-runtime", "agent-gov-api", "agent-gov-ui"}
        return (
            isinstance(value, dict)
            and set(value) == expected
            and all(isinstance(item, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", item) for item in value.values())
        )

    def _parse_time(self, value: object, field: str) -> datetime:
        if not isinstance(value, str) or not value:
            self._fail(f"cutover 时间字段缺失: {field}")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise self._error_type(f"cutover 时间字段无效: {field}") from exc
        if parsed.tzinfo is None:
            self._fail(f"cutover 时间字段必须含时区: {field}")
        return parsed

    def _ensure_deletion_intent(self, manifest_path: Path, manifest: CutoverManifest) -> DeletionIntent:
        path = manifest_path.parent / "irreversible-deletion-intent.json"
        if path.exists():
            intent = self._load_deletion_intent(
                manifest_path,
                manifest,
                require_manifest_binding=False,
                allow_missing_targets=False,
            )
        else:
            created_at = self._utc_now()
            deadline_at = (self._parse_time(created_at, "deletion intent created_at") + timedelta(minutes=15)).isoformat()
            intent = DeletionIntent(
                schema_version=1,
                cutover_id=str(manifest["cutover_id"]),
                state="pending",
                created_at=created_at,
                deadline_at=deadline_at,
                targets=self._deletion_targets(manifest_path, manifest),
            )
            self._write_json(path, intent)
        manifest.update(
            state="deletion_intent_ready",
            deletion_intent_path=path.as_posix(),
            deletion_intent_sha256=self._sha256_file(path),
            legacy_deletion_deadline=intent["deadline_at"],
            irreversible=False,
        )
        self._write_json(manifest_path, manifest)
        return intent

    def _deletion_targets(self, manifest_path: Path, manifest: CutoverManifest) -> list[DeletionTarget]:
        paths = (
            manifest.get("snapshot_archive"),
            manifest.get("rollback_image_archive"),
            manifest.get("env_snapshot"),
            manifest.get("rollback_compose"),
            manifest.get("rollback_image_inventory"),
            manifest.get("acceptance_env"),
            manifest.get("production_drain_env"),
        )
        targets: list[DeletionTarget] = []
        for raw_path in paths:
            if not isinstance(raw_path, str):
                self._fail("deletion intent 缺少固定 rollback artifact")
            path = Path(raw_path)
            if path.parent != manifest_path.parent or path.is_symlink() or not path.is_file():
                self._fail("deletion intent 仅允许当前 cutover 目录内的普通文件")
            targets.append(DeletionTarget(path=path.name, sha256=self._sha256_file(path)))
        if len({item["path"] for item in targets}) != len(targets):
            self._fail("deletion intent rollback artifact 路径重复")
        return targets

    def _load_deletion_intent(
        self,
        manifest_path: Path,
        manifest: CutoverManifest,
        *,
        require_manifest_binding: bool = True,
        allow_missing_targets: bool = False,
    ) -> DeletionIntent:
        expected_path = manifest_path.parent / "irreversible-deletion-intent.json"
        raw_path = manifest.get("deletion_intent_path")
        if require_manifest_binding and raw_path != expected_path.as_posix():
            self._fail("manifest 未绑定固定 deletion intent")
        if expected_path.is_symlink() or not expected_path.is_file():
            self._fail("不可逆 gate 已打开但 deletion intent 缺失")
        if require_manifest_binding and manifest.get("deletion_intent_sha256") != self._sha256_file(expected_path):
            self._fail("deletion intent digest 与 manifest 不一致")
        try:
            payload = json.loads(expected_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise self._error_type("deletion intent 无法读取") from exc
        if not self._valid_deletion_intent(payload, manifest_path, manifest):
            self._fail("deletion intent schema/binding 无效")
        intent = cast(DeletionIntent, payload)
        self._validate_intent_targets(manifest_path, intent, allow_missing=allow_missing_targets)
        return intent

    def _validate_intent_targets(
        self,
        manifest_path: Path,
        intent: DeletionIntent,
        *,
        allow_missing: bool,
    ) -> None:
        for target in intent["targets"]:
            candidate = manifest_path.parent / target["path"]
            if not candidate.exists():
                if allow_missing:
                    continue
                self._fail("open gate 前 rollback artifact 已缺失")
            if candidate.is_symlink() or not candidate.is_file() or self._sha256_file(candidate) != target["sha256"]:
                self._fail("rollback artifact 与 deletion intent 不一致")

    def _valid_deletion_intent(self, value: object, manifest_path: Path, manifest: CutoverManifest) -> bool:
        expected_schema = {"schema_version", "cutover_id", "state", "created_at", "deadline_at", "targets"}
        if not isinstance(value, dict) or set(value) != expected_schema:
            return False
        targets = value.get("targets")
        expected_names = self._expected_target_names(manifest)
        if not isinstance(targets, list) or len(targets) != 7:
            return False
        target_names = {
            item.get("path")
            for item in targets
            if isinstance(item, dict)
            and set(item) == {"path", "sha256"}
            and isinstance(item.get("path"), str)
            and Path(str(item["path"])).name == item["path"]
            and isinstance(item.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", str(item["sha256"]))
        }
        try:
            created = self._parse_time(value.get("created_at"), "deletion intent created_at")
            deadline = self._parse_time(value.get("deadline_at"), "deletion intent deadline_at")
        except self._error_type:
            return False
        return (
            value.get("schema_version") == 1
            and value.get("cutover_id") == manifest.get("cutover_id")
            and value.get("state") == "pending"
            and len(target_names) == 7
            and target_names == expected_names
            and deadline - created == timedelta(minutes=15)
            and all((manifest_path.parent / name).parent == manifest_path.parent for name in target_names)
        )

    @staticmethod
    def _expected_target_names(manifest: CutoverManifest) -> set[str]:
        keys = (
            "snapshot_archive",
            "rollback_image_archive",
            "env_snapshot",
            "rollback_compose",
            "rollback_image_inventory",
            "acceptance_env",
            "production_drain_env",
        )
        names: set[str] = set()
        for key in keys:
            value = manifest.get(key)
            if isinstance(value, str) and value:
                names.add(Path(value).name)
        return names

    def _reconcile_irreversible_open(self, manifest_path: Path, manifest: CutoverManifest, db_path: Path) -> None:
        gate_state = self._read_gate_state(manifest)
        irreversible_at = gate_state.get("irreversible_at") if gate_state is not None else None
        if gate_state is None or gate_state.get("state") != "open" or not isinstance(irreversible_at, str):
            self._fail("只有已原子 open 的 gate 可以执行不可逆恢复")
        intent = self._load_deletion_intent(manifest_path, manifest, allow_missing_targets=True)
        completed_at = manifest.get("legacy_deletion_completed_at")
        if not isinstance(completed_at, str):
            completed_at = None
        manifest.update(
            state="irreversible_cleanup_pending",
            irreversible=True,
            irreversible_at=irreversible_at,
        )
        self._write_json(manifest_path, manifest)
        completion = self._delete_legacy_rollback_artifacts(
            manifest_path,
            manifest,
            intent,
            completed_at,
        )
        completed_at = completion["completed_at"]
        deadline_missed = completion["deadline_missed"]
        alert_at = manifest.get("legacy_deletion_timeout_alert_at")
        if deadline_missed and not isinstance(alert_at, str):
            alert_at = completed_at
            manifest["legacy_deletion_timeout_alert_at"] = alert_at
        completion_path = manifest_path.parent / "irreversible-deletion-completion.json"
        self._write_json(completion_path, completion)
        manifest.update(
            state="irreversible",
            irreversible=True,
            opened_at=irreversible_at,
            legacy_snapshot_deleted_at=completed_at,
            legacy_deletion_completed_at=completed_at,
            deletion_completion_receipt=completion_path.as_posix(),
        )
        self._write_json(manifest_path, manifest)
        self._record_opened_ledger(db_path, manifest)
        if deadline_missed:
            self._record_timeout_alert(db_path, manifest, intent, str(alert_at))

    def _record_opened_ledger(self, db_path: Path, manifest: CutoverManifest) -> None:
        self._record_ledger(
            db_path,
            cutover_id=f"{manifest['cutover_id']}:opened",
            phase="opened",
            status="irreversible",
            detail="The mounted gate-state file was atomically replaced with open; legacy snapshot restoration is forbidden.",
            artifacts=self._opened_ledger_artifacts(manifest),
        )

    def _record_timeout_alert(
        self,
        db_path: Path,
        manifest: CutoverManifest,
        intent: DeletionIntent,
        observed_at: str,
    ) -> None:
        self._record_ledger(
            db_path,
            cutover_id=f"{manifest['cutover_id']}:deletion-timeout",
            phase="legacy_deletion",
            status="alert",
            detail="Legacy rollback artifact deletion exceeded the persisted 15-minute deadline; cleanup remains mandatory.",
            artifacts={"deadline_at": intent["deadline_at"], "observed_at": observed_at},
        )

    def _opened_ledger_artifacts(self, manifest: CutoverManifest) -> Mapping[str, object]:
        artifacts = manifest.get("production_drain_artifacts")
        if not isinstance(artifacts, dict):
            self._fail("不可逆恢复缺少 production drain artifacts")
        evidence_sha = artifacts.get("evidence_sha256")
        openapi_sha = artifacts.get("openapi_sha256")
        if not isinstance(evidence_sha, str) or not isinstance(openapi_sha, str):
            self._fail("不可逆恢复缺少 evidence/OpenAPI digest")
        return {"evidence_sha256": evidence_sha, "openapi_sha256": openapi_sha}

    def _delete_legacy_rollback_artifacts(
        self,
        manifest_path: Path,
        manifest: CutoverManifest,
        intent: DeletionIntent,
        completed_at: str | None,
    ) -> DeletionCompletion:
        deleted: list[str] = []
        for target in intent["targets"]:
            candidate = manifest_path.parent / target["path"]
            if candidate.is_symlink():
                self._fail("rollback artifact 在删除前发生替换；保持 fail-closed")
            if candidate.exists():
                if not candidate.is_file() or self._sha256_file(candidate) != target["sha256"]:
                    self._fail("rollback artifact 在删除前发生替换；保持 fail-closed")
                candidate.unlink()
                self._fsync_directory(candidate.parent)
            deleted.append(target["path"])
        if completed_at is None:
            completed_at = self._utc_now()
        completion_time = self._parse_time(completed_at, "legacy deletion completed_at")
        deadline = self._parse_time(intent["deadline_at"], "deletion intent deadline_at")
        return DeletionCompletion(
            schema_version=1,
            cutover_id=str(manifest["cutover_id"]),
            completed_at=completed_at,
            deadline_at=intent["deadline_at"],
            deadline_missed=completion_time > deadline,
            deleted_paths=deleted,
        )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
