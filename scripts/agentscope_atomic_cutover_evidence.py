"""Strict machine-receipt validation for the AgentScope cutover gate."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Final, NoReturn, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

try:
    from scripts.agentscope_atomic_cutover_types import (
        CUTOVER_SERVICE_NAMES,
        CutoverImageIds,
        CutoverManifest,
        FinalEvidence,
        is_cutover_image_ids,
    )
except ModuleNotFoundError:
    from agentscope_atomic_cutover_types import (
        CUTOVER_SERVICE_NAMES,
        CutoverImageIds,
        CutoverManifest,
        FinalEvidence,
        is_cutover_image_ids,
    )


RECEIPT_PRODUCER: Final = "agentgov-cutover-machine-receipt-v1"
COMMAND_IDS: Final = {
    "static_gates": "make-cutover-static-gates-v1",
    "contract_tests": "pytest-agentscope-contracts-v1",
    "container_acceptance": "container-full-recreate-v1",
    "browser_acceptance": "browser-real-flow-three-times-v1",
    "live_runtime": "agentscope-live-fifty-plus-v1",
}
STATIC_CHECKS: Final = (
    "agentscope_cutover",
    "codex_config",
    "docs_governance",
    "frontend_build",
    "frontend_unit",
    "governance",
    "openapi_contract",
    "openapi_type_drift",
    "pyright",
    "ruff",
    "ruff_format",
    "stage_language",
    "test_quality_policy",
    "version_consistency",
)
CONTRACTS: Final = (
    "cancel_and_disconnect",
    "credential_isolation",
    "harness_immutability",
    "hitl_same_run",
    "raw_byte_sse",
    "restart_interrupted",
    "run_idempotency",
    "session_creation_saga",
    "subagent_team",
    "terminal_message_receipt",
    "trace_graph",
)
CUTOVER_SERVICES: Final = CUTOVER_SERVICE_NAMES
BROWSER_WORKFLOWS: Final = (
    "cancel_and_refresh",
    "feedback_to_run",
    "session_two_turn_tool_hitl_trace",
)
HASH_PATTERN: Final = re.compile(r"[0-9a-f]{64}")
MAX_EVIDENCE_BYTES: Final = 4 * 1024 * 1024
REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
SIGNATURE_SCHEME: Final = "ed25519"


class CutoverEvidenceSupport:
    """Build templates and validate only allowlisted, bound machine receipts."""

    def __init__(
        self,
        *,
        error_type: type[RuntimeError],
        final_evidence_keys: Sequence[str],
        sha256_file: Callable[[Path], str],
        write_json: Callable[[Path, Mapping[str, object]], None],
    ) -> None:
        self._error_type = error_type
        self._keys = tuple(final_evidence_keys)
        self._sha256_file = sha256_file
        self._write_json = write_json
        if self._keys != tuple(COMMAND_IDS):
            self._fail("最终验收 gate 集合与固定 receipt allowlist 不一致")

    def _fail(self, message: str) -> NoReturn:
        raise self._error_type(message)

    @staticmethod
    def evidence_binding_sha256(artifacts: Mapping[str, object]) -> str:
        encoded = json.dumps(artifacts, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def write_pending_template(
        self,
        path: Path,
        *,
        cutover_id: str,
        source_artifact_sha256: str,
        key_fingerprint_sha256: str,
    ) -> None:
        self._write_json(
            path,
            self._template(
                cutover_id=cutover_id,
                source_artifact_sha256=source_artifact_sha256,
                acceptance_artifacts_sha256="",
                key_fingerprint_sha256=key_fingerprint_sha256,
            ),
        )

    def bind_final_evidence_template(
        self,
        path: Path,
        manifest: CutoverManifest,
        artifacts: Mapping[str, object],
    ) -> None:
        self._write_json(
            path,
            self._template(
                cutover_id=manifest["cutover_id"],
                source_artifact_sha256=manifest["source_artifact_sha256"],
                acceptance_artifacts_sha256=self.evidence_binding_sha256(artifacts),
                key_fingerprint_sha256=manifest["evidence_verification_key_sha256"],
            ),
        )

    def _template(
        self,
        *,
        cutover_id: str,
        source_artifact_sha256: str,
        acceptance_artifacts_sha256: str,
        key_fingerprint_sha256: str,
    ) -> Mapping[str, object]:
        return {
            "schema_version": 3,
            "cutover_id": cutover_id,
            "source_artifact_sha256": source_artifact_sha256,
            "acceptance_artifacts_sha256": acceptance_artifacts_sha256,
            "status": "pending",
            **{
                key: {
                    "status": "pending",
                    "receipt_path": f"evidence/{key}.receipt.json",
                    "receipt_sha256": "",
                }
                for key in self._keys
            },
            "provenance": {
                "scheme": SIGNATURE_SCHEME,
                "key_fingerprint_sha256": key_fingerprint_sha256,
                "signature_base64": "",
            },
        }

    def verification_key_fingerprint(
        self,
        path: Path,
        *,
        forbidden_roots: Sequence[Path] = (),
    ) -> str:
        _key, fingerprint = self._load_verification_key(path, forbidden_roots=forbidden_roots)
        return fingerprint

    def validate_final_evidence(
        self,
        path: Path,
        manifest: CutoverManifest,
        verification_key_path: Path,
    ) -> tuple[FinalEvidence, str]:
        evidence_root = path.parent.resolve(strict=True)
        runtime_root_value = manifest.get("runtime_root")
        if not isinstance(runtime_root_value, str) or not runtime_root_value:
            self._fail("prepare manifest 缺少 Runtime 根目录")
        public_key, key_fingerprint = self._load_verification_key(
            verification_key_path,
            forbidden_roots=(
                evidence_root,
                REPOSITORY_ROOT,
                Path(runtime_root_value),
            ),
        )
        if key_fingerprint != manifest.get("evidence_verification_key_sha256"):
            self._fail("最终验收公钥未绑定 prepare manifest")
        payload, raw_payload = self._read_object(path, "最终验收 evidence")
        expected_keys = {
            "schema_version",
            "cutover_id",
            "source_artifact_sha256",
            "acceptance_artifacts_sha256",
            "status",
            "provenance",
            *self._keys,
        }
        if set(payload) != expected_keys or payload.get("schema_version") != 3 or payload.get("status") != "passed":
            self._fail("最终验收 evidence schema/status 不精确")
        self._verify_signature(payload, public_key, key_fingerprint, "最终验收 evidence")
        acceptance_digest, identity, image_ids = self._validate_binding(payload, manifest)
        executed_at = self._parse_time(manifest.get("executed_at"), "manifest.executed_at")
        snapshot_archive = manifest.get("snapshot_archive")
        if not isinstance(snapshot_archive, str) or Path(snapshot_archive).parent.resolve() != evidence_root:
            self._fail("最终验收 evidence 必须位于当前 cutover backup 目录")
        for gate in self._keys:
            self._validate_gate(
                gate,
                payload.get(gate),
                evidence_root=evidence_root,
                manifest=manifest,
                acceptance_digest=acceptance_digest,
                identity=identity,
                image_ids=image_ids,
                executed_at=executed_at,
                public_key=public_key,
                key_fingerprint=key_fingerprint,
            )
        return cast(FinalEvidence, payload), hashlib.sha256(raw_payload).hexdigest()

    def _validate_binding(
        self,
        payload: Mapping[str, object],
        manifest: CutoverManifest,
    ) -> tuple[str, str, CutoverImageIds]:
        acceptance = manifest.get("acceptance_artifacts")
        if not isinstance(acceptance, dict):
            self._fail("manifest 缺少 acceptance artifacts")
        digest = self.evidence_binding_sha256(acceptance)
        identity = acceptance.get("acceptance_identity")
        image_ids = acceptance.get("image_ids")
        if payload.get("cutover_id") != manifest.get("cutover_id") or identity != manifest.get("cutover_id"):
            self._fail("最终验收 evidence/acceptance 未绑定当前 cutover_id")
        if payload.get("source_artifact_sha256") != manifest.get("source_artifact_sha256"):
            self._fail("最终验收 evidence 未绑定当前 source artifact")
        if acceptance.get("source_artifact_sha256") != manifest.get("source_artifact_sha256"):
            self._fail("acceptance artifacts 未绑定当前 source artifact")
        if acceptance.get("prepared_build_id") != manifest.get("prepared_build_id"):
            self._fail("acceptance artifacts 未绑定 prepare fresh build identity")
        bootstrap_digest = manifest.get("bootstrap_source_sha256")
        if not self._is_hash(bootstrap_digest) or acceptance.get("bootstrap_source_sha256") != bootstrap_digest:
            self._fail("acceptance artifacts 未绑定 sealed bootstrap digest")
        if payload.get("acceptance_artifacts_sha256") != digest:
            self._fail("最终验收 evidence 未绑定当前 acceptance artifacts")
        if not isinstance(identity, str) or not is_cutover_image_ids(image_ids) or image_ids != manifest.get("prepared_image_ids"):
            self._fail("acceptance artifacts 缺少精确 identity/image IDs")
        return digest, identity, cast(CutoverImageIds, image_ids)

    def _validate_gate(
        self,
        gate: str,
        item: object,
        *,
        evidence_root: Path,
        manifest: CutoverManifest,
        acceptance_digest: str,
        identity: str,
        image_ids: CutoverImageIds,
        executed_at: datetime,
        public_key: Ed25519PublicKey,
        key_fingerprint: str,
    ) -> None:
        if not isinstance(item, dict) or set(item) != {"status", "receipt_path", "receipt_sha256"}:
            self._fail(f"最终验收 evidence gate schema 不精确: {gate}")
        if item.get("status") != "passed":
            self._fail(f"最终验收 evidence 缺少 passed: {gate}")
        receipt = self._resolve_receipt(evidence_root, item.get("receipt_path"), gate)
        digest = item.get("receipt_sha256")
        payload, raw_payload = self._read_object(receipt, f"{gate} machine receipt")
        if not self._is_hash(digest) or hashlib.sha256(raw_payload).hexdigest() != digest:
            self._fail(f"最终验收 machine receipt digest 不匹配: {gate}")
        self._verify_signature(payload, public_key, key_fingerprint, f"{gate} machine receipt")
        self._validate_receipt_common(
            payload,
            gate=gate,
            manifest=manifest,
            acceptance_digest=acceptance_digest,
            identity=identity,
            image_ids=image_ids,
            executed_at=executed_at,
        )
        result = payload.get("result")
        if not isinstance(result, dict):
            self._fail(f"machine receipt 缺少结构化 result: {gate}")
        self._validate_result(gate, result, image_ids)

    def _validate_receipt_common(
        self,
        payload: Mapping[str, object],
        *,
        gate: str,
        manifest: CutoverManifest,
        acceptance_digest: str,
        identity: str,
        image_ids: CutoverImageIds,
        executed_at: datetime,
    ) -> None:
        expected = {
            "schema_version",
            "producer",
            "gate_id",
            "command_id",
            "cutover_id",
            "source_artifact_sha256",
            "acceptance_artifacts_sha256",
            "acceptance_identity",
            "image_ids",
            "status",
            "started_at",
            "completed_at",
            "exit_code",
            "result",
            "provenance",
        }
        if set(payload) != expected or payload.get("schema_version") != 2:
            self._fail(f"machine receipt schema 不精确: {gate}")
        expected_values = (
            payload.get("producer") == RECEIPT_PRODUCER,
            payload.get("gate_id") == gate,
            payload.get("command_id") == COMMAND_IDS[gate],
            payload.get("cutover_id") == manifest.get("cutover_id"),
            payload.get("source_artifact_sha256") == manifest.get("source_artifact_sha256"),
            payload.get("acceptance_artifacts_sha256") == acceptance_digest,
            payload.get("acceptance_identity") == identity,
            payload.get("image_ids") == image_ids,
            payload.get("status") == "passed",
            self._exact_int(payload.get("exit_code"), 0),
        )
        if not all(expected_values):
            self._fail(f"machine receipt 未通过固定 allowlist/binding: {gate}")
        started_at = self._parse_time(payload.get("started_at"), f"{gate}.started_at")
        completed_at = self._parse_time(payload.get("completed_at"), f"{gate}.completed_at")
        if started_at < executed_at or completed_at < started_at:
            self._fail(f"machine receipt 时间不属于当前 acceptance: {gate}")

    def _validate_result(self, gate: str, result: Mapping[str, object], image_ids: CutoverImageIds) -> None:
        validators = {
            "static_gates": self._validate_static,
            "contract_tests": self._validate_contracts,
            "container_acceptance": lambda value: self._validate_container(value, image_ids),
            "browser_acceptance": self._validate_browser,
            "live_runtime": self._validate_live,
        }
        validators[gate](result)

    def _validate_static(self, result: Mapping[str, object]) -> None:
        if set(result) != {"checks", "failure_count"}:
            self._fail("static_gates receipt result schema 不精确")
        if result.get("checks") != list(STATIC_CHECKS) or not self._exact_int(result.get("failure_count"), 0):
            self._fail("static_gates receipt 未覆盖固定检查集合")

    def _validate_contracts(self, result: Mapping[str, object]) -> None:
        if set(result) != {"contracts", "passed", "failed", "skipped"}:
            self._fail("contract_tests receipt result schema 不精确")
        if (
            result.get("contracts") != list(CONTRACTS)
            or not self._exact_int(result.get("passed"), len(CONTRACTS))
            or not self._exact_int(result.get("failed"), 0)
            or not self._exact_int(result.get("skipped"), 0)
        ):
            self._fail("contract_tests receipt 未覆盖固定契约集合")

    def _validate_container(self, result: Mapping[str, object], image_ids: CutoverImageIds) -> None:
        expected = {"profile", "services", "fresh_build", "force_recreate", "image_ids", "failure_count"}
        if set(result) != expected:
            self._fail("container_acceptance receipt result schema 不精确")
        if (
            result.get("profile") != "langfuse"
            or result.get("services") != list(CUTOVER_SERVICES)
            or result.get("fresh_build") is not True
            or result.get("force_recreate") is not True
            or result.get("image_ids") != image_ids
            or not self._exact_int(result.get("failure_count"), 0)
        ):
            self._fail("container_acceptance receipt 与 acceptance image 身份不一致")

    def _validate_browser(self, result: Mapping[str, object]) -> None:
        expected = {"consecutive_passes", "mock_sse", "workflows", "failure_count", "artifact_set_sha256"}
        if set(result) != expected:
            self._fail("browser_acceptance receipt result schema 不精确")
        if (
            not self._at_least(result.get("consecutive_passes"), 3)
            or result.get("mock_sse") is not False
            or result.get("workflows") != list(BROWSER_WORKFLOWS)
            or not self._exact_int(result.get("failure_count"), 0)
            or not self._is_hash(result.get("artifact_set_sha256"))
        ):
            self._fail("browser_acceptance receipt 未证明三次真实浏览器主链")

    def _validate_live(self, result: Mapping[str, object]) -> None:
        zero_fields = (
            "cross_scope_authorizations",
            "unexpected_restarts",
            "unhandled_errors",
            "residual_runs",
            "terminal_loss",
            "missing_required_spans",
            "secret_plaintext_hits",
        )
        hash_fields = ("run_set_sha256", "scenario_set_sha256", "trace_set_sha256")
        scalar_fields = {
            "total_runs",
            "distinct_inputs",
            "max_concurrency",
            "identity_link_percent",
            "feedback_matches",
            "trace_count",
            "unique_trace_count",
            "trace_query_p95_seconds",
            "trace_query_max_seconds",
            "p95_latency_ratio",
            "p99_latency_ratio",
            "error_rate_delta_percentage_points",
            "otel_p95_overhead_ratio",
            *zero_fields,
            *hash_fields,
        }
        if set(result) != scalar_fields:
            self._fail("live_runtime receipt result schema 不精确")
        valid = (
            self._exact_int(result.get("total_runs"), 50)
            and self._exact_int(result.get("distinct_inputs"), 50)
            and self._at_least(result.get("max_concurrency"), 10)
            and self._equal_number(result.get("identity_link_percent"), 100.0)
            and self._exact_int(result.get("feedback_matches"), 10)
            and self._exact_int(result.get("trace_count"), 50)
            and self._exact_int(result.get("unique_trace_count"), 50)
            and all(self._exact_int(result.get(field), 0) for field in zero_fields)
            and self._at_most(result.get("trace_query_p95_seconds"), 30.0)
            and self._at_most(result.get("trace_query_max_seconds"), 60.0)
            and self._at_most(result.get("p95_latency_ratio"), 1.20)
            and self._at_most(result.get("p99_latency_ratio"), 1.50)
            and self._at_most(result.get("error_rate_delta_percentage_points"), 0.5)
            and self._at_most(result.get("otel_p95_overhead_ratio"), 0.10)
            and all(self._is_hash(result.get(field)) for field in hash_fields)
        )
        if not valid:
            self._fail("live_runtime receipt 未满足 50-run/10 并发/trace/performance 硬门")

    def _resolve_receipt(self, root: Path, value: object, gate: str) -> Path:
        expected = f"evidence/{gate}.receipt.json"
        if value != expected:
            self._fail(f"最终验收 evidence 缺少固定 machine receipt path: {gate}")
        current = root
        for part in Path(expected).parts:
            current /= part
            if current.is_symlink():
                self._fail(f"machine receipt 不得经过符号链接: {gate}")
        resolved = current.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file() or resolved.stat().st_size <= 0:
            self._fail(f"machine receipt 必须是当前 cutover 内的非空普通文件: {gate}")
        return resolved

    def _read_object(self, path: Path, label: str) -> tuple[Mapping[str, object], bytes]:
        absolute = path.absolute()
        if path.is_symlink() or absolute.parent.resolve() != absolute.parent:
            self._fail(f"{label} 不得经过符号链接")
        try:
            descriptor = os.open(absolute, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > MAX_EVIDENCE_BYTES:
                    self._fail(f"{label} 必须是大小受限的非空普通文件")
                raw = b""
                while len(raw) <= MAX_EVIDENCE_BYTES:
                    chunk = os.read(descriptor, min(1024 * 1024, MAX_EVIDENCE_BYTES + 1 - len(raw)))
                    if not chunk:
                        break
                    raw += chunk
                if len(raw) > MAX_EVIDENCE_BYTES:
                    self._fail(f"{label} 超过大小上限")
            finally:
                os.close(descriptor)
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise self._error_type(f"{label} 无法读取") from exc
        if not isinstance(payload, dict):
            self._fail(f"{label} 必须是 JSON object")
        return cast(Mapping[str, object], payload), raw

    def _verify_signature(
        self,
        payload: Mapping[str, object],
        public_key: Ed25519PublicKey,
        key_fingerprint: str,
        label: str,
    ) -> None:
        provenance = payload.get("provenance")
        if not isinstance(provenance, dict) or set(provenance) != {
            "scheme",
            "key_fingerprint_sha256",
            "signature_base64",
        }:
            self._fail(f"{label} 缺少精确签名 provenance")
        if provenance.get("scheme") != SIGNATURE_SCHEME or provenance.get("key_fingerprint_sha256") != key_fingerprint:
            self._fail(f"{label} 签名信任根不匹配")
        signature_value = provenance.get("signature_base64")
        if not isinstance(signature_value, str):
            self._fail(f"{label} 缺少 Ed25519 签名")
        try:
            signature = base64.b64decode(signature_value, validate=True)
        except (ValueError, TypeError) as exc:
            raise self._error_type(f"{label} Ed25519 签名编码无效") from exc
        canonical = dict(payload)
        canonical["provenance"] = {
            "scheme": provenance["scheme"],
            "key_fingerprint_sha256": provenance["key_fingerprint_sha256"],
        }
        encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        try:
            public_key.verify(signature, encoded)
        except InvalidSignature as exc:
            raise self._error_type(f"{label} Ed25519 签名无效") from exc

    def _load_verification_key(
        self,
        path: Path,
        *,
        forbidden_roots: Sequence[Path],
    ) -> tuple[Ed25519PublicKey, str]:
        expanded = path.expanduser()
        absolute = expanded.absolute()
        if expanded.is_symlink() or absolute.parent.resolve() != absolute.parent:
            self._fail("验收签名公钥不得经过符号链接")
        try:
            resolved = absolute.resolve(strict=True)
        except OSError as exc:
            raise self._error_type("验收签名公钥不存在") from exc
        for root in forbidden_roots:
            forbidden = root.resolve()
            if resolved == forbidden or resolved.is_relative_to(forbidden):
                self._fail("验收签名公钥必须位于 cutover backup/runtime 之外")
        try:
            raw = resolved.read_bytes()
            if not raw or len(raw) > 16 * 1024:
                self._fail("验收签名公钥文件大小无效")
            loaded = serialization.load_pem_public_key(raw)
        except (OSError, ValueError, TypeError) as exc:
            raise self._error_type("验收签名公钥无法解析") from exc
        if not isinstance(loaded, Ed25519PublicKey):
            self._fail("验收签名公钥必须是 Ed25519")
        encoded = loaded.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return loaded, hashlib.sha256(encoded).hexdigest()

    def _parse_time(self, value: object, field: str) -> datetime:
        if not isinstance(value, str) or not value:
            self._fail(f"machine receipt 缺少时间: {field}")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise self._error_type(f"machine receipt 时间无效: {field}") from exc
        if parsed.tzinfo is None:
            self._fail(f"machine receipt 时间必须含时区: {field}")
        return parsed

    @staticmethod
    def _is_hash(value: object) -> bool:
        return isinstance(value, str) and HASH_PATTERN.fullmatch(value) is not None

    @staticmethod
    def _number(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        return number if math.isfinite(number) else None

    @staticmethod
    def _exact_int(value: object, expected: int) -> bool:
        return type(value) is int and value == expected

    @classmethod
    def _at_least(cls, value: object, minimum: float) -> bool:
        number = cls._number(value)
        return number is not None and number >= minimum

    @classmethod
    def _at_most(cls, value: object, maximum: float) -> bool:
        number = cls._number(value)
        return number is not None and number <= maximum

    @classmethod
    def _equal_number(cls, value: object, expected: float) -> bool:
        number = cls._number(value)
        return number is not None and number == expected
