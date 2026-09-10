from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Optional

import yaml

from ..feedback_privacy import SENSITIVE_KEY_PARTS
from ..json_types import JsonObject
from ..protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from ..records.evidence_records import EvidenceIncludedFileRecord, EvidencePackageFileRecord, EvidencePackageRecord
from ..runtime_db import EvidenceFileModel, EvidencePackageModel, utc_now

_RUNTIME_ENV_SNAPSHOT_KEYS = (
    "AGENTSCOPE_RUNTIME_URL",
    "AGENTSCOPE_RUNTIME_USER_ID",
    "AGENTSCOPE_MODEL_TYPE",
    "AGENTSCOPE_MODEL_NAME",
    "RUNTIME_CANDIDATES_DIR",
    "LANGFUSE_OTEL_ENDPOINT",
)
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_PLACEHOLDER_SCAN_EXTENSIONS = {".json", ".md", ".sh", ".txt", ".yaml", ".yml"}
_PLACEHOLDER_SCAN_SKIP_PARTS = {".git", ".env", "secrets", "node_modules", "dist", "__pycache__"}
_PLACEHOLDER_SCAN_MAX_BYTES = 512_000
_TRACE_ATTRIBUTE_ALLOWLIST = {
    "agentgov.run.id",
    "agentgov.agent.id",
    "agentgov.agent.version_id",
    "agentgov.harness.digest",
    "agentscope.agent.id",
    "agentscope.session.id",
    "agentscope.agent.reply_id",
    "agentscope.runtime.version",
    "gen_ai.request.model",
    "gen_ai.provider.name",
    "gen_ai.operation.name",
    "gen_ai.tool.name",
    "tool.name",
    "mcp.server.name",
    "mcp.connection.status",
}


class FeedbackEvidenceStoreMixin:
    """Store operations for evidence package manifests, files, and job materialization."""

    def create_evidence_package(self, feedback_case_id: str) -> Optional[JsonObject]:
        feedback_case = self.find_case(feedback_case_id)
        if not feedback_case:
            return None
        existing_id = self._latest(feedback_case.get("evidence_package_ids"))
        if existing_id:
            existing = self.get_evidence_package(existing_id)
            if existing:
                return existing

        evidence_id = f"evp-{uuid.uuid4()}"
        context = self._collect_evidence_context(feedback_case)
        business_agent_version: JsonObject = {
            "business_agent_version_id": self._current_agent_version_id(self._resolve_task_agent_id(feedback_case_id=feedback_case_id)),
            "captured_at": utc_now(),
        }
        redaction_report: JsonObject = {
            "enabled": not self.enable_debug_evidence,
            "policy": "debug-evidence-raw-v1" if self.enable_debug_evidence else "security-redaction-v1",
            "redacted_fields": list(SENSITIVE_KEY_PARTS),
        }
        files = self._build_evidence_files(context, business_agent_version, redaction_report)
        included_files = self._included_evidence_files(files)
        manifest = self._build_evidence_manifest(
            evidence_id=evidence_id,
            feedback_case_id=feedback_case_id,
            feedback_case=feedback_case,
            context=context,
            business_agent_version=business_agent_version,
            redaction_report=redaction_report,
            included_files=included_files,
        )
        with self.Session.begin() as db:
            self._store_evidence_package_rows(
                db,
                manifest=manifest,
                files=files,
            )
            if not self._append_case_update_row(
                db,
                feedback_case,
                evidence_package_id=evidence_id,
                status="pending_attribution",
            ):
                raise RuntimeError("Feedback case disappeared during evidence package creation.")
        return manifest

    def _collect_evidence_context(self, feedback_case: JsonObject) -> JsonObject:
        raw_signal_ids = feedback_case.get("signal_ids")
        raw_event_ids = feedback_case.get("event_ids")
        raw_run_ids = feedback_case.get("run_ids")
        raw_session_ids = feedback_case.get("session_ids")
        signal_ids = raw_signal_ids if isinstance(raw_signal_ids, list) else []
        event_ids = raw_event_ids if isinstance(raw_event_ids, list) else []
        run_ids = raw_run_ids if isinstance(raw_run_ids, list) else []
        session_ids = raw_session_ids if isinstance(raw_session_ids, list) else []
        signals_clean = [item for item in (self.find_signal(str(source_id)) for source_id in signal_ids) if item]
        events_clean = [item for item in (self.find_event(str(source_id)) for source_id in event_ids) if item]
        runs_clean = [item for item in (self.find_run(run_id=str(run_id)) for run_id in run_ids) if item]
        sessions = [
            {
                "session_id": session_id,
                "run_ids": [run.get("run_id") for run in runs_clean if run.get("session_id") == session_id],
            }
            for session_id in session_ids
        ]
        langfuse_trace_refs = self._langfuse_trace_refs(runs_clean)
        langfuse_trace_details = self._fetch_langfuse_trace_details(langfuse_trace_refs)
        tool_calls = self._tool_call_summaries(langfuse_trace_details)
        trace_summary = self._trace_summaries(langfuse_trace_details)
        runtime_env_snapshot = self._runtime_env_snapshot()
        effective_mcp_config = self._effective_mcp_config()
        return {
            "signals_clean": signals_clean,
            "events_clean": events_clean,
            "runs_clean": runs_clean,
            "sessions": sessions,
            "tool_calls": tool_calls,
            "langfuse_trace_refs": langfuse_trace_refs,
            "langfuse_trace_details": langfuse_trace_details,
            "trace_summary": trace_summary,
            "runtime_config_summary": self._runtime_config_summary(effective_mcp_config),
            "effective_mcp_config": effective_mcp_config,
            "mcp_connection_summary": self._mcp_connection_summary(langfuse_trace_details),
            "runtime_env_snapshot": runtime_env_snapshot,
            "workspace_placeholder_summary": self._workspace_placeholder_summary(),
        }

    def _build_evidence_files(
        self,
        context: JsonObject,
        business_agent_version: JsonObject,
        redaction_report: JsonObject,
    ) -> JsonObject:
        files: JsonObject = {
            "feedback.json": context["signals_clean"],
            "runs.json": context["runs_clean"],
            "sessions.json": context["sessions"],
            "tool_calls.json": context["tool_calls"],
            "soc_events.json": context["events_clean"],
            "trace_summary.json": context["trace_summary"],
            "runtime_config_summary.json": context["runtime_config_summary"],
            "effective_mcp_config.json": context["effective_mcp_config"],
            "mcp_connection_summary.json": context["mcp_connection_summary"],
            "runtime_env_snapshot.json": context["runtime_env_snapshot"],
            "workspace_placeholder_summary.json": context["workspace_placeholder_summary"],
            "business_agent_version.json": business_agent_version,
            "langfuse_trace_details.json": context["langfuse_trace_details"],
            "redaction_report.json": redaction_report,
        }
        return files

    def _included_evidence_files(self, files: JsonObject) -> list[JsonObject]:
        return [
            EvidenceIncludedFileRecord(
                path=name,
                sha256=self._sha256_json(self._evidence_payload(payload)),
                type=name.removesuffix(".json"),
            ).to_payload()
            for name, payload in files.items()
        ]

    def _build_evidence_manifest(
        self,
        *,
        evidence_id: str,
        feedback_case_id: str,
        feedback_case: JsonObject,
        context: JsonObject,
        business_agent_version: JsonObject,
        redaction_report: JsonObject,
        included_files: list[JsonObject],
    ) -> JsonObject:
        raw_trace_refs = context.get("langfuse_trace_refs")
        trace_refs = raw_trace_refs if isinstance(raw_trace_refs, list) else []
        raw_trace_details = context.get("langfuse_trace_details")
        trace_details = raw_trace_details if isinstance(raw_trace_details, list) else []
        trace_ids = self._unique_strings([item.get("trace_id") for item in trace_refs if isinstance(item, dict)])
        record = EvidencePackageRecord.model_validate(
            {
                "schema_version": "evidence-package/v1",
                "evidence_package_id": evidence_id,
                "feedback_case_id": feedback_case_id,
                "created_at": utc_now(),
                "created_by": "system",
                "business_agent_version_id": business_agent_version["business_agent_version_id"],
                "source_refs": {
                    "feedback_ids": feedback_case.get("signal_ids", []),
                    "signal_ids": feedback_case.get("signal_ids", []),
                    "run_ids": feedback_case.get("run_ids", []),
                    "session_ids": feedback_case.get("session_ids", []),
                    "trace_ids": trace_ids,
                    "alert_ids": feedback_case.get("alert_ids", []),
                    "case_ids": feedback_case.get("case_ids", []),
                    "event_ids": feedback_case.get("event_ids", []),
                },
                "included_files": included_files,
                "redaction": redaction_report,
                "completeness": {
                    "has_feedback": bool(context["signals_clean"]),
                    "has_runs": bool(context["runs_clean"]),
                    "has_tool_calls": bool(context["tool_calls"]),
                    "has_trace_summary": bool(context["trace_summary"]),
                    "has_runtime_config_summary": bool(context["runtime_config_summary"]),
                    "has_effective_mcp_config": bool(context["effective_mcp_config"]),
                    "has_mcp_connection_summary": bool(context["mcp_connection_summary"]),
                    "has_runtime_env_snapshot": bool(context["runtime_env_snapshot"]),
                    "has_workspace_placeholder_summary": bool(context["workspace_placeholder_summary"]),
                    "has_business_agent_version": bool(business_agent_version["business_agent_version_id"]),
                    # AgentScope 是 Message/AgentState 唯一事实源；AgentGov evidence
                    # 不复制正文，也不再把已删除的旧 run 字段宣称为完整证据。
                    "has_messages": False,
                    "has_agent_activity": False,
                    "has_langfuse_trace_refs": bool(context["langfuse_trace_refs"]),
                    "has_langfuse_trace_details": any(item.get("fetch_status") == "completed" for item in trace_details if isinstance(item, dict)),
                },
            }
        )
        return record.to_payload()

    def _runtime_config_summary(self, effective_mcp_config: JsonObject) -> JsonObject:
        manifest_path = self.default_workspace_dir / "agent.yaml"
        try:
            manifest_bytes = manifest_path.read_bytes()
        except OSError:
            manifest_bytes = None
        manifest: JsonObject = {}
        manifest_error: str | None = None
        if manifest_bytes is not None:
            try:
                loaded = yaml.safe_load(manifest_bytes)
                manifest = loaded if isinstance(loaded, dict) else {}
                if not manifest:
                    manifest_error = "agent.yaml is not a non-empty object"
            except yaml.YAMLError as exc:
                manifest_error = f"{exc.__class__.__name__}: invalid agent.yaml"
        agent = manifest.get("agent") if isinstance(manifest.get("agent"), dict) else {}
        session = manifest.get("session") if isinstance(manifest.get("session"), dict) else {}
        workspace_policy = manifest.get("workspace_policy") if isinstance(manifest.get("workspace_policy"), dict) else {}
        return {
            "default_workspace_dir": str(self.default_workspace_dir),
            "data_dir": str(self.data_dir),
            "report_output_dir": str(self.data_dir / "outputs" / "reports"),
            "agent_manifest": {
                "source": "workspace_agent_manifest" if manifest_bytes is not None else "missing",
                "exists": manifest_bytes is not None,
                "sha256": hashlib.sha256(manifest_bytes).hexdigest() if manifest_bytes is not None else None,
                "error": manifest_error,
                "runtime": agent.get("runtime"),
                "runtime_contract": agent.get("runtime_contract"),
                "permission_mode": session.get("permission_mode"),
                "immutable_harness": workspace_policy.get("immutable_harness"),
                "fail_closed": workspace_policy.get("fail_closed"),
            },
            "runtime_config_source": "agentscope_harness",
            "effective_mcp_config_source": effective_mcp_config.get("source"),
            "effective_mcp_config_path": effective_mcp_config.get("path"),
        }

    def _effective_mcp_config(self) -> JsonObject:
        mcp_root = self.default_workspace_dir / "mcp"
        configs: list[JsonObject] = []
        errors: list[JsonObject] = []
        if mcp_root.is_dir() and not mcp_root.is_symlink():
            for path in sorted(mcp_root.glob("*.json")):
                try:
                    raw = path.read_bytes()
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise ValueError("MCP document must be an object")
                    config = payload.get("mcp_config")
                    if not isinstance(config, dict):
                        raise ValueError("mcp_config must be an object")
                    credential_refs = payload.get("credential_refs")
                    configs.append(
                        {
                            "name": path.stem,
                            "path": path.relative_to(self.default_workspace_dir).as_posix(),
                            "sha256": hashlib.sha256(raw).hexdigest(),
                            "type": config.get("type"),
                            "credential_ref_count": len(credential_refs) if isinstance(credential_refs, list) else 0,
                            "unresolved_placeholders": self._unresolved_placeholder_names(payload),
                        }
                    )
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    errors.append(
                        {
                            "path": path.relative_to(self.default_workspace_dir).as_posix(),
                            "error": exc.__class__.__name__,
                        }
                    )
        return {
            "profile": DEFAULT_BUSINESS_AGENT_ID,
            "source": "workspace_mcp_directory",
            "path": str(mcp_root),
            "exists": mcp_root.is_dir() and not mcp_root.is_symlink(),
            "selected_servers": [item["name"] for item in configs],
            "server_summaries": configs,
            "errors": errors,
        }

    def _runtime_env_snapshot(self) -> JsonObject:
        keys = {key: self._safe_env_value(key) for key in _RUNTIME_ENV_SNAPSHOT_KEYS}
        return {"keys": keys, "runtime": "agentscope"}

    def _mcp_connection_summary(self, trace_details: list[JsonObject]) -> JsonObject:
        run_summaries: list[JsonObject] = []
        failed: set[str] = set()
        connected: set[str] = set()
        for detail in trace_details:
            observations: list[JsonObject] = []
            raw_observations = detail.get("observations")
            for raw in raw_observations if isinstance(raw_observations, list) else []:
                if not isinstance(raw, dict):
                    continue
                attributes = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {}
                observation_name = self._string(raw.get("name")) or ""
                server_name = self._string(attributes.get("mcp.server.name")) or ""
                if "mcp" not in observation_name.casefold() and not server_name:
                    continue
                explicit_status = (self._string(attributes.get("mcp.connection.status")) or "").casefold()
                failed_observation = bool(raw.get("error")) or (self._string(raw.get("level")) or "").upper() == "ERROR"
                status = "failed" if failed_observation else explicit_status or "observed"
                if status == "failed" and server_name:
                    failed.add(server_name)
                if status in {"connected", "success", "succeeded", "ok"} and server_name:
                    connected.add(server_name)
                observations.append(
                    {
                        "observation_id": raw.get("observation_id"),
                        "name": observation_name,
                        "server_name": server_name or None,
                        "status": status,
                    }
                )
            run_summaries.append(
                {
                    "run_id": detail.get("run_id"),
                    "session_id": detail.get("session_id"),
                    "trace_id": detail.get("trace_id"),
                    "observations": observations,
                }
            )
        return {
            "source": "langfuse_trace_details",
            "semantic_evidence_available": any(item.get("observations") for item in run_summaries),
            "runs": run_summaries,
            "failed_server_names": sorted(failed),
            "connected_server_names": sorted(connected),
        }

    def _safe_env_value(self, key: str) -> JsonObject:
        value = os.environ.get(key)
        payload: JsonObject = {"present": value is not None, "is_empty": value == "" if value is not None else None}
        if value is None:
            return payload
        payload["length"] = len(value)
        lowered = key.lower()
        if any(part in lowered for part in SENSITIVE_KEY_PARTS):
            return payload
        if key.endswith(("_PATH", "_DIR", "_URL")) or key.endswith("_NAME"):
            payload["value_preview"] = value[:160]
        return payload

    @classmethod
    def _unresolved_placeholder_names(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            return sorted({match.group(1) for match in _PLACEHOLDER_RE.finditer(value)})
        if isinstance(value, list):
            return sorted({name for item in value for name in cls._unresolved_placeholder_names(item)})
        if isinstance(value, dict):
            return sorted({name for item in value.values() for name in cls._unresolved_placeholder_names(item)})
        return []

    def _workspace_placeholder_summary(self) -> JsonObject:
        items: list[JsonObject] = []
        if not self.default_workspace_dir.exists():
            return {"workspace_dir": str(self.default_workspace_dir), "exists": False, "items": items}
        for path in sorted(self.default_workspace_dir.rglob("*")):
            if not self._placeholder_scan_allowed(path):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            matches = sorted({match.group(1) for match in _PLACEHOLDER_RE.finditer(text)})
            if not matches:
                continue
            rel = path.relative_to(self.default_workspace_dir).as_posix()
            items.append(
                {
                    "path": rel,
                    "placeholder_names": matches,
                    "category": self._placeholder_category(rel),
                    "attribution_hint": self._placeholder_attribution_hint(rel),
                }
            )
        return {"workspace_dir": str(self.default_workspace_dir), "exists": True, "items": items}

    def _placeholder_scan_allowed(self, path: Path) -> bool:
        if not path.is_file() or path.suffix not in _PLACEHOLDER_SCAN_EXTENSIONS:
            return False
        rel_parts = set(path.relative_to(self.default_workspace_dir).parts)
        if rel_parts & _PLACEHOLDER_SCAN_SKIP_PARTS:
            return False
        try:
            return path.stat().st_size <= _PLACEHOLDER_SCAN_MAX_BYTES
        except OSError:
            return False

    def _placeholder_category(self, rel_path: str) -> str:
        if rel_path.startswith("mcp/") and rel_path.endswith(".json"):
            return "mcp_config"
        if rel_path == "agent.yaml":
            return "agent_manifest"
        if rel_path == "AGENT.md" or rel_path.startswith(("skills/", "subagents/")):
            return "harness_instruction"
        if rel_path.endswith(".md") or rel_path.endswith(".example"):
            return "documentation_or_example"
        if rel_path.endswith(".sh"):
            return "shell_default_or_script"
        return "workspace_template_file"

    def _placeholder_attribution_hint(self, rel_path: str) -> str:
        category = self._placeholder_category(rel_path)
        if category == "mcp_config":
            return "Use effective_mcp_config.json for final MCP config attribution."
        if category == "agent_manifest":
            return "If this affected runtime policy, prefer governed Harness configuration attribution."
        if category == "harness_instruction":
            return "Attribute to the exact immutable Harness version and source path."
        if category == "documentation_or_example":
            return "Usually not_actionable unless evidence shows the example was used at runtime."
        if category == "shell_default_or_script":
            return "Do not treat shell default syntax as unresolved unless execution evidence shows failure."
        return "Classify by the runtime component that consumed the placeholder."

    def _store_evidence_package_rows(
        self,
        db: Any,
        *,
        manifest: JsonObject,
        files: JsonObject,
    ) -> None:
        record = EvidencePackageRecord.model_validate(manifest)
        db.add(
            EvidencePackageModel(
                evidence_package_id=record.evidence_package_id,
                feedback_case_id=record.feedback_case_id,
                created_at=record.created_at,
                manifest_json=record.to_payload(),
            )
        )
        db.flush()
        for item in record.included_files:
            content = self._evidence_payload(files[item.path])
            db.add(
                EvidenceFileModel(
                    evidence_package_id=record.evidence_package_id,
                    file_name=item.path,
                    file_type=item.type,
                    sha256=item.sha256,
                    content_json=content,
                )
            )

    def get_evidence_package(self, evidence_package_id: str) -> Optional[JsonObject]:
        if not evidence_package_id:
            return None
        with self.Session() as db:
            record = db.get(EvidencePackageModel, evidence_package_id)
            return EvidencePackageRecord.from_row(record).to_payload() if record else None

    def get_evidence_package_file(self, evidence_package_id: str, file_name: str) -> Optional[JsonObject]:
        if not file_name or Path(file_name).name != file_name or file_name == "manifest.json":
            return None
        with self.Session() as db:
            record = db.get(EvidenceFileModel, {"evidence_package_id": evidence_package_id, "file_name": file_name})
            if not record:
                return None
            return EvidencePackageFileRecord.from_row(record).to_payload()

    def _evidence_payload(self, value: Any) -> Any:
        if self.enable_debug_evidence:
            return value
        return self._scrub_record(value)

    def _langfuse_trace_refs(self, runs: list[JsonObject]) -> list[JsonObject]:
        refs: list[JsonObject] = []
        for run in runs:
            trace_id = self._string(run.get("trace_id"))
            trace_url = self._string(run.get("trace_url"))
            if not trace_id and not trace_url:
                continue
            refs.append({"run_id": run.get("run_id"), "session_id": run.get("session_id"), "trace_id": trace_id, "trace_url": trace_url})
        return refs

    def _fetch_langfuse_trace_details(self, refs: list[JsonObject]) -> list[JsonObject]:
        fetcher = self.langfuse_trace_fetcher
        details: list[JsonObject] = []
        seen: set[str] = set()
        for ref in refs:
            trace_id = self._string(ref.get("trace_id"))
            if not trace_id or trace_id in seen:
                continue
            seen.add(trace_id)
            base: JsonObject = {
                "run_id": ref.get("run_id"),
                "session_id": ref.get("session_id"),
                "trace_id": trace_id,
            }
            if not fetcher:
                details.append({**base, "fetch_status": "skipped", "reason": "langfuse_trace_fetcher_unavailable"})
                continue
            try:
                payload = fetcher(trace_id)
            except Exception as exc:
                details.append({**base, "fetch_status": "failed", "error_type": exc.__class__.__name__})
                continue
            if not payload:
                details.append({**base, "fetch_status": "empty"})
                continue
            if payload.get("fetch_status") == "failed":
                details.append(
                    {
                        **base,
                        "fetch_status": "failed",
                        "error_type": self._safe_upstream_error_type(payload.get("error")),
                    }
                )
                continue
            details.append(self._minimal_trace_detail(base, payload))
        return details

    def _minimal_trace_detail(self, base: JsonObject, trace_data: JsonObject) -> JsonObject:
        observations = trace_data.get("observations")
        summaries = [self._observation_summary(item) for item in observations if isinstance(item, dict)] if isinstance(observations, list) else []
        result: JsonObject = {
            **base,
            "fetch_status": "completed",
            "trace_name": self._safe_trace_label(trace_data.get("name")),
            "timestamp": self._safe_trace_label(trace_data.get("timestamp")),
            "attributes": self._safe_trace_attributes(trace_data),
            "observation_count": len(summaries),
            "observations": summaries,
        }
        if "input" in trace_data:
            result["input_fingerprint"] = self._content_fingerprint(trace_data.get("input"))
        if "output" in trace_data:
            result["output_fingerprint"] = self._content_fingerprint(trace_data.get("output"))
        return result

    def _observation_summary(self, observation: JsonObject) -> JsonObject:
        attributes = self._safe_trace_attributes(observation)
        level = self._safe_trace_label(observation.get("level")).upper()
        status = self._safe_trace_label(observation.get("status")).upper()
        summary: JsonObject = {
            "observation_id": self._safe_trace_label(observation.get("id") or observation.get("observation_id")) or None,
            "parent_observation_id": self._safe_trace_label(observation.get("parent_observation_id") or observation.get("parentObservationId")) or None,
            "name": self._safe_trace_label(observation.get("name")),
            "type": self._safe_trace_label(observation.get("type")),
            "level": level,
            "error": level == "ERROR" or status in {"ERROR", "FAILED"},
            "start_time": self._safe_trace_label(observation.get("start_time") or observation.get("startTime")) or None,
            "end_time": self._safe_trace_label(observation.get("end_time") or observation.get("endTime")) or None,
            "attributes": attributes,
            "usage": self._safe_usage(observation),
        }
        if "input" in observation:
            summary["input_fingerprint"] = self._content_fingerprint(observation.get("input"))
        if "output" in observation:
            summary["output_fingerprint"] = self._content_fingerprint(observation.get("output"))
        return summary

    def _tool_call_summaries(self, trace_details: list[JsonObject]) -> list[JsonObject]:
        calls: list[JsonObject] = []
        for detail in trace_details:
            observations = detail.get("observations")
            for observation in observations if isinstance(observations, list) else []:
                if not isinstance(observation, dict) or not self._is_tool_observation(observation):
                    continue
                calls.append(
                    {
                        "run_id": detail.get("run_id"),
                        "session_id": detail.get("session_id"),
                        "trace_id": detail.get("trace_id"),
                        "observation_id": observation.get("observation_id"),
                        "name": observation.get("name"),
                        "type": observation.get("type"),
                        "level": observation.get("level"),
                        "error": bool(observation.get("error")),
                        "attributes": observation.get("attributes") or {},
                        "usage": observation.get("usage") or {},
                        "input_fingerprint": observation.get("input_fingerprint"),
                        "output_fingerprint": observation.get("output_fingerprint"),
                    }
                )
        return calls

    def _trace_summaries(self, trace_details: list[JsonObject]) -> list[JsonObject]:
        summaries: list[JsonObject] = []
        for detail in trace_details:
            observations = detail.get("observations")
            items = [item for item in observations if isinstance(item, dict)] if isinstance(observations, list) else []
            summaries.append(
                {
                    "run_id": detail.get("run_id"),
                    "session_id": detail.get("session_id"),
                    "trace_id": detail.get("trace_id"),
                    "fetch_status": detail.get("fetch_status"),
                    "trace_name": detail.get("trace_name"),
                    "observation_count": len(items),
                    "observation_names": self._unique_strings([item.get("name") for item in items]),
                    "tool_observation_count": sum(self._is_tool_observation(item) for item in items),
                    "error_observation_names": self._unique_strings([item.get("name") for item in items if item.get("error") is True]),
                    "input_fingerprint": detail.get("input_fingerprint"),
                    "output_fingerprint": detail.get("output_fingerprint"),
                }
            )
        return summaries

    @staticmethod
    def _safe_trace_attributes(value: JsonObject) -> JsonObject:
        safe: JsonObject = {}
        for field in ("metadata", "attributes"):
            candidate = value.get(field)
            if isinstance(candidate, dict):
                for key, item in candidate.items():
                    if key in _TRACE_ATTRIBUTE_ALLOWLIST and isinstance(item, str | int | float | bool):
                        safe[key] = item[:256] if isinstance(item, str) else item
        return safe

    @staticmethod
    def _safe_usage(observation: JsonObject) -> JsonObject:
        for field in ("usage_details", "usageDetails", "usage"):
            value = observation.get(field)
            if isinstance(value, dict):
                return {str(key)[:64]: item for key, item in value.items() if isinstance(item, int | float) and not isinstance(item, bool)}
        return {}

    @staticmethod
    def _content_fingerprint(value: Any) -> JsonObject:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return {"byte_length": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}

    @staticmethod
    def _safe_trace_label(value: Any) -> str:
        return str(value or "")[:160]

    @staticmethod
    def _safe_upstream_error_type(value: Any) -> str:
        candidate = str(value or "").split(":", 1)[0].strip()
        return candidate if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,127}", candidate) else "upstream_error"

    @staticmethod
    def _is_tool_observation(observation: JsonObject) -> bool:
        name = str(observation.get("name") or "").casefold()
        kind = str(observation.get("type") or "").casefold()
        attributes = observation.get("attributes") if isinstance(observation.get("attributes"), dict) else {}
        operation = str(attributes.get("gen_ai.operation.name") or "").casefold()
        return "tool" in name or kind == "tool" or operation in {"execute_tool", "tool_call"}
