from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from ..feedback_privacy import SENSITIVE_KEY_PARTS
from ..mcp_config import build_mcp_config_summary, resolve_main_mcp_config_path
from ..records.evidence_records import EvidenceIncludedFileRecord, EvidencePackageFileRecord, EvidencePackageRecord
from ..json_types import JsonObject
from ..runtime_db import EvidenceFileModel, EvidencePackageModel, utc_now

_MAIN_MCP_SERVERS = ("sec-ops-data", "security-kb")
_RUNTIME_ENV_SNAPSHOT_KEYS = (
    "CLAUDE_MCP_CONFIG_PATH",
    "MCP_SERVER_URL",
    "NO_PROXY",
    "no_proxy",
    "CLAUDE_ENV_JSON",
    "MAX_TURNS",
    "DEFAULT_ALLOWED_TOOLS",
    "DEFAULT_DISALLOWED_TOOLS",
)
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_PLACEHOLDER_SCAN_EXTENSIONS = {".json", ".md", ".sh", ".txt", ".yaml", ".yml"}
_PLACEHOLDER_SCAN_SKIP_PARTS = {".git", ".env", "secrets", "node_modules", "dist", "__pycache__"}
_PLACEHOLDER_SCAN_MAX_BYTES = 512_000


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
        main_agent_version: JsonObject = {"main_agent_version_id": self._current_agent_version_id(), "captured_at": utc_now()}
        redaction_report: JsonObject = {
            "enabled": not self.enable_debug_evidence,
            "policy": "debug-evidence-raw-v1" if self.enable_debug_evidence else "security-redaction-v1",
            "redacted_fields": list(SENSITIVE_KEY_PARTS),
        }
        files = self._build_evidence_files(context, main_agent_version, redaction_report)
        included_files = self._included_evidence_files(files)
        manifest = self._build_evidence_manifest(
            evidence_id=evidence_id,
            feedback_case_id=feedback_case_id,
            feedback_case=feedback_case,
            context=context,
            main_agent_version=main_agent_version,
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
        signals_clean = [item for item in (self.find_signal(source_id) for source_id in feedback_case.get("signal_ids", [])) if item]
        events_clean = [item for item in (self.find_event(source_id) for source_id in feedback_case.get("event_ids", [])) if item]
        runs_clean = [item for item in (self.find_run(run_id=run_id) for run_id in feedback_case.get("run_ids", [])) if item]
        sessions = [
            {
                "session_id": session_id,
                "run_ids": [run.get("run_id") for run in runs_clean if run.get("session_id") == session_id],
            }
            for session_id in feedback_case.get("session_ids", [])
        ]
        tool_calls = [
            call
            for run in runs_clean
            for call in (run.get("agent_activity") or {}).get("tool_calls", [])
            if isinstance(call, dict)
        ]
        messages = [
            {"run_id": run.get("run_id"), "session_id": run.get("session_id"), "messages": run.get("messages") or []}
            for run in runs_clean
        ]
        agent_activity = [
            {"run_id": run.get("run_id"), "session_id": run.get("session_id"), "agent_activity": run.get("agent_activity") or {}}
            for run in runs_clean
        ]
        langfuse_trace_refs = self._langfuse_trace_refs(runs_clean)
        trace_summary = [
            {
                "run_id": run.get("run_id"),
                "session_id": run.get("session_id"),
                "answer_summary": run.get("answer_summary"),
                "tool_names": (run.get("agent_activity") or {}).get("tool_names") or [],
                "errors": run.get("errors") or [],
                "langfuse_trace_id": run.get("langfuse_trace_id"),
                "langfuse_trace_url": run.get("langfuse_trace_url"),
            }
            for run in runs_clean
        ]
        runtime_env_snapshot = self._runtime_env_snapshot()
        effective_mcp_config = self._effective_mcp_config()
        return {
            "signals_clean": signals_clean,
            "events_clean": events_clean,
            "runs_clean": runs_clean,
            "sessions": sessions,
            "tool_calls": tool_calls,
            "messages": messages,
            "agent_activity": agent_activity,
            "langfuse_trace_refs": langfuse_trace_refs,
            "trace_summary": trace_summary,
            "runtime_config_summary": self._runtime_config_summary(effective_mcp_config),
            "effective_mcp_config": effective_mcp_config,
            "mcp_connection_summary": self._mcp_connection_summary(runs_clean),
            "runtime_env_snapshot": runtime_env_snapshot,
            "workspace_placeholder_summary": self._workspace_placeholder_summary(),
        }

    def _build_evidence_files(
        self,
        context: JsonObject,
        main_agent_version: JsonObject,
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
            "main_agent_version.json": main_agent_version,
            "redaction_report.json": redaction_report,
        }
        if self.enable_debug_evidence:
            files.update(
                {
                    "messages.json": context["messages"],
                    "agent_activity.json": context["agent_activity"],
                    "langfuse_trace_refs.json": context["langfuse_trace_refs"],
                }
            )
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
        main_agent_version: JsonObject,
        redaction_report: JsonObject,
        included_files: list[JsonObject],
    ) -> JsonObject:
        trace_ids = self._unique_strings([item.get("trace_id") for item in context["langfuse_trace_refs"]])
        record = EvidencePackageRecord.model_validate(
            {
                "schema_version": "evidence-package/v1",
                "evidence_package_id": evidence_id,
                "feedback_case_id": feedback_case_id,
                "created_at": utc_now(),
                "created_by": "system",
                "main_agent_version_id": main_agent_version["main_agent_version_id"],
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
                    "has_main_agent_version": bool(main_agent_version["main_agent_version_id"]),
                    "has_messages": bool(context["messages"] and any(item.get("messages") for item in context["messages"])),
                    "has_agent_activity": bool(context["agent_activity"] and any(item.get("agent_activity") for item in context["agent_activity"])),
                    "has_langfuse_trace_refs": bool(context["langfuse_trace_refs"]),
                    "has_langfuse_trace_details": False,
                },
            }
        )
        return record.to_payload()

    def _runtime_config_summary(self, effective_mcp_config: JsonObject) -> JsonObject:
        return {
            "main_workspace_dir": str(self.main_workspace_dir),
            "data_dir": str(self.data_dir),
            "report_output_dir": str(self.data_dir / "outputs" / "reports"),
            "main_profile_writable_paths": [str(self.data_dir / "outputs")],
            "default_max_turns_env": self._safe_env_value("MAX_TURNS"),
            "default_allowed_tools_env": self._safe_env_value("DEFAULT_ALLOWED_TOOLS"),
            "default_disallowed_tools_env": self._safe_env_value("DEFAULT_DISALLOWED_TOOLS"),
            "effective_mcp_config_source": effective_mcp_config.get("source"),
            "effective_mcp_config_path": effective_mcp_config.get("path"),
        }

    def _effective_mcp_config(self) -> JsonObject:
        env = self._mcp_expansion_env()
        explicit = Path(env["CLAUDE_MCP_CONFIG_PATH"]) if env.get("CLAUDE_MCP_CONFIG_PATH") else None
        resolution = resolve_main_mcp_config_path(self.main_workspace_dir, explicit)
        summary = build_mcp_config_summary(resolution.path, _MAIN_MCP_SERVERS, env)
        return {
            "profile": "main-agent",
            "source": resolution.source,
            "explicit_env_present": explicit is not None,
            "workspace_local_path": str(self.main_workspace_dir / ".mcp.local.json"),
            "workspace_local_exists": (self.main_workspace_dir / ".mcp.local.json").exists(),
            "workspace_template_path": str(self.main_workspace_dir / ".mcp.json"),
            "workspace_template_exists": (self.main_workspace_dir / ".mcp.json").exists(),
            **summary,
        }

    def _runtime_env_snapshot(self) -> JsonObject:
        parsed_claude_env, claude_env_error = self._parsed_claude_env_json()
        keys = {key: self._safe_env_value(key) for key in _RUNTIME_ENV_SNAPSHOT_KEYS}
        return {
            "keys": keys,
            "claude_env_json_keys": sorted(parsed_claude_env),
            "claude_env_json_error": claude_env_error,
        }

    def _mcp_connection_summary(self, runs: list[JsonObject]) -> JsonObject:
        run_summaries: list[JsonObject] = []
        failed: set[str] = set()
        connected: set[str] = set()
        for run in runs:
            servers: list[JsonObject] = []
            for raw in self._iter_mcp_server_entries(run.get("messages") or []):
                name = self._string(raw.get("name"))
                status = self._string(raw.get("status"))
                if status == "failed" and name:
                    failed.add(name)
                if status == "connected" and name:
                    connected.add(name)
                servers.append({"name": name, "status": status})
            run_summaries.append(
                {
                    "run_id": run.get("run_id"),
                    "session_id": run.get("session_id"),
                    "mcp_servers": servers,
                    "errors": run.get("errors") or [],
                }
            )
        return {"runs": run_summaries, "failed_server_names": sorted(failed), "connected_server_names": sorted(connected)}

    def _iter_mcp_server_entries(self, value: Any) -> Iterable[JsonObject]:
        if isinstance(value, dict):
            servers = value.get("mcp_servers")
            if isinstance(servers, list):
                for server in servers:
                    if isinstance(server, dict):
                        yield server
            for child in value.values():
                yield from self._iter_mcp_server_entries(child)
        elif isinstance(value, list):
            for child in value:
                yield from self._iter_mcp_server_entries(child)

    def _mcp_expansion_env(self) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if isinstance(value, str)}
        parsed, _ = self._parsed_claude_env_json()
        env.update(parsed)
        return env

    def _parsed_claude_env_json(self) -> tuple[Mapping[str, str], str | None]:
        raw = os.environ.get("CLAUDE_ENV_JSON")
        if not raw:
            return {}, None
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {}, f"JSONDecodeError: {exc.msg}"
        if not isinstance(loaded, dict):
            return {}, "CLAUDE_ENV_JSON is not an object"
        parsed = {str(key): str(value) for key, value in loaded.items() if isinstance(value, str | int | float | bool)}
        return parsed, None

    def _safe_env_value(self, key: str) -> JsonObject:
        value = os.environ.get(key)
        payload: JsonObject = {"present": value is not None, "is_empty": value == "" if value is not None else None}
        if value is None:
            return payload
        payload["length"] = len(value)
        lowered = key.lower()
        if any(part in lowered for part in SENSITIVE_KEY_PARTS):
            return payload
        if key == "CLAUDE_ENV_JSON":
            parsed, error = self._parsed_claude_env_json()
            payload["json_keys"] = sorted(parsed)
            payload["json_error"] = error
            return payload
        if key.endswith("_PATH") or key in {"MAX_TURNS", "DEFAULT_ALLOWED_TOOLS", "DEFAULT_DISALLOWED_TOOLS"}:
            payload["value_preview"] = value[:160]
        return payload

    def _workspace_placeholder_summary(self) -> JsonObject:
        items: list[JsonObject] = []
        if not self.main_workspace_dir.exists():
            return {"workspace_dir": str(self.main_workspace_dir), "exists": False, "items": items}
        for path in sorted(self.main_workspace_dir.rglob("*")):
            if not self._placeholder_scan_allowed(path):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            matches = sorted({match.group(1) for match in _PLACEHOLDER_RE.finditer(text)})
            if not matches:
                continue
            rel = path.relative_to(self.main_workspace_dir).as_posix()
            items.append(
                {
                    "path": rel,
                    "placeholder_names": matches,
                    "category": self._placeholder_category(rel),
                    "attribution_hint": self._placeholder_attribution_hint(rel),
                }
            )
        return {"workspace_dir": str(self.main_workspace_dir), "exists": True, "items": items}

    def _placeholder_scan_allowed(self, path: Path) -> bool:
        if not path.is_file() or path.suffix not in _PLACEHOLDER_SCAN_EXTENSIONS:
            return False
        rel_parts = set(path.relative_to(self.main_workspace_dir).parts)
        if rel_parts & _PLACEHOLDER_SCAN_SKIP_PARTS:
            return False
        try:
            return path.stat().st_size <= _PLACEHOLDER_SCAN_MAX_BYTES
        except OSError:
            return False

    def _placeholder_category(self, rel_path: str) -> str:
        if rel_path in {".mcp.json", ".mcp.local.json"}:
            return "mcp_config"
        if rel_path == ".claude/settings.json":
            return "claude_project_settings"
        if rel_path.startswith("mcp_servers/") and rel_path.endswith(".json"):
            return "mcp_sample_data"
        if rel_path.endswith(".md") or rel_path.endswith(".example"):
            return "documentation_or_example"
        if rel_path.endswith(".sh"):
            return "shell_default_or_script"
        return "workspace_template_file"

    def _placeholder_attribution_hint(self, rel_path: str) -> str:
        category = self._placeholder_category(rel_path)
        if category == "mcp_config":
            return "Use effective_mcp_config.json for final MCP config attribution."
        if category == "claude_project_settings":
            return "If this affected runtime permissions, prefer runtime_code/runtime_fix."
        if category == "mcp_sample_data":
            return "If returned by an MCP tool, prefer external_mcp_service/tool_data_quality."
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
            trace_id = self._string(run.get("langfuse_trace_id"))
            trace_url = self._string(run.get("langfuse_trace_url"))
            if not trace_id and not trace_url:
                continue
            refs.append({"run_id": run.get("run_id"), "session_id": run.get("session_id"), "trace_id": trace_id, "trace_url": trace_url})
        return refs

    def _materialize_evidence_files(self, job_id: str, job_type: str, evidence_package_id: str, names: Iterable[str]) -> list[str]:
        paths: list[str] = []
        evidence_dir = self.tmp_jobs_dir / job_id / job_type / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            evidence_file = self.get_evidence_package_file(evidence_package_id, name)
            if not evidence_file:
                continue
            path = evidence_dir / name
            self._write_json(path, evidence_file["content"])
            paths.append(str(path))
        return paths

    def _materialize_manifest(self, job_id: str, job_type: str, evidence_package_id: str) -> str:
        manifest = self.get_evidence_package(evidence_package_id) or {}
        path = self.tmp_jobs_dir / job_id / job_type / "evidence" / "manifest.json"
        self._write_json(path, manifest)
        return str(path)
