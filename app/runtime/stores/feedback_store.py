from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Optional

from ...version import APP_VERSION
from ..agent_paths import business_agent_layout
from ..collection_utils import unique_strings
from ..execution_targets import WorkspaceExecutionTargetPolicy
from ..feedback_privacy import SENSITIVE_KEY_PARTS
from ..json_types import JsonObject
from ..runtime_db import (
    make_session_factory,
    runtime_db_path_from_data_dir,
)
from .agent_job_queue_store import AgentJobQueueStoreMixin
from .agent_job_store import AgentJobStoreMixin
from .feedback_case_store import FeedbackCaseStoreMixin
from .feedback_eval_store import FeedbackEvalStoreMixin
from .feedback_evidence_store import FeedbackEvidenceStoreMixin
from .feedback_regression_asset_store import FeedbackRegressionAssetStoreMixin
from .feedback_source_store import FeedbackSourceStoreMixin


class FeedbackStore(
    AgentJobQueueStoreMixin,
    AgentJobStoreMixin,
    FeedbackRegressionAssetStoreMixin,
    FeedbackEvalStoreMixin,
    FeedbackEvidenceStoreMixin,
    FeedbackCaseStoreMixin,
    FeedbackSourceStoreMixin,
):
    """SQLAlchemy-backed store for the feedback optimization loop."""

    def __init__(
        self,
        *,
        data_dir: Path,
        workspace_dir: Optional[Path] = None,
        agent_version_provider: Optional[Callable[[Optional[str]], Optional[str]]] = None,
        runtime_version: str = APP_VERSION,
        enable_debug_evidence: bool = True,
        agent_job_timeout_seconds: int = 300,
    ) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.main_workspace_dir = workspace_dir or business_agent_layout(data_dir, "main-agent").workspace
        # main-agent workspace 在 /data 下，确保存在（与 get_settings 一致；执行/证据写入依赖它）。
        self.main_workspace_dir.mkdir(parents=True, exist_ok=True)
        self.execution_targets = WorkspaceExecutionTargetPolicy(self.main_workspace_dir)
        self.db_path = runtime_db_path_from_data_dir(data_dir)
        self.Session = make_session_factory(self.db_path)
        self.agent_version_provider = agent_version_provider
        self.runtime_version = runtime_version
        self.enable_debug_evidence = enable_debug_evidence
        self.agent_job_timeout_seconds = agent_job_timeout_seconds
        self.langfuse_trace_fetcher: Optional[Callable[[str], Optional[JsonObject]]] = None
        # Legacy cleanup anchor only. New Agent jobs keep input/output/error in SQLite.
        self.tmp_jobs_dir = data_dir / ".runtime-tmp" / "jobs"
        self._job_create_lock = RLock()

    def set_langfuse_trace_fetcher(self, fetcher: Callable[[str], Optional[JsonObject]]) -> None:
        # The fetcher is owned by the backend so Langfuse credentials never enter
        # internal Agent prompts or Claude Code tool configuration.
        self.langfuse_trace_fetcher = fetcher

    def _current_agent_version_id(self, agent_id: Optional[str] = None) -> Optional[str]:
        # #24-C/D：按归属业务 Agent 解析其自身版本库 HEAD（agent_id=None/main 走 main 库），使
        # baseline/current/版本归属同源于该 Agent 的 GitAgentVersionStore，杜绝 main↔AAA 库错配 409。
        if not self.agent_version_provider:
            return None
        try:
            return self.agent_version_provider(agent_id)
        except Exception:
            return None

    def _resolve_task_agent_id(self, *, feedback_case_id: Optional[str] = None) -> str:
        if not feedback_case_id:
            return "main-agent"
        feedback_case = self.find_case(feedback_case_id)
        if not feedback_case:
            return "main-agent"
        case_agent_id = self._string(feedback_case.get("agent_id"))
        if case_agent_id:
            return case_agent_id
        for signal_id in feedback_case.get("signal_ids") or []:
            signal = self.find_signal(str(signal_id))
            agent_id = self._string((signal or {}).get("agent_id"))
            if agent_id:
                return agent_id
        return "main-agent"

    def _agent_git_paths_context(self, agent_id: Optional[str] = None) -> JsonObject:
        # 执行 prompt 的仓库/worktrees/releases 路径按归属业务 Agent 解析；main-agent 与动态
        # 业务 Agent 同构，版本治理工件一律落 data/business-agents/<id>/version。
        normalized = (agent_id or "main-agent").strip()
        layout = business_agent_layout(self.data_dir, normalized)
        repository, worktrees, releases = (
            self.main_workspace_dir if normalized == "main-agent" else layout.workspace,
            layout.version_base / "worktrees",
            layout.version_base / "releases",
        )
        return {
            "main_agent_repository_path": str(repository),
            "agent_change_set_worktrees_path": str(worktrees),
            "agent_release_archives_path": str(releases),
            "agent_version_source": "git",
        }

    def _execution_targets_for(self, agent_id: Optional[str]) -> WorkspaceExecutionTargetPolicy:
        # #24-B：执行目标的 sha/存在性必须按归属业务 Agent 的 workspace 计算（与 apply 目标 worktree 同源），
        # 否则拿 main 的 sha 比对 AAA worktree 文件 → 'Target file changed'/存在性 409。main 复用主 policy。
        normalized = (agent_id or "").strip()
        if not normalized or normalized == "main-agent":
            return self.execution_targets
        return WorkspaceExecutionTargetPolicy(business_agent_layout(self.data_dir, normalized).workspace)

    def _execution_target_policy(self, agent_id: Optional[str] = None) -> JsonObject:
        return self._execution_targets_for(agent_id).policy_json()

    def _execution_target_file_contexts(self, target_paths: list[str], agent_id: Optional[str] = None) -> list[JsonObject]:
        return self._execution_targets_for(agent_id).file_contexts(target_paths)

    def _execution_target_file_context(self, target_path: str, agent_id: Optional[str] = None) -> JsonObject:
        return self._execution_targets_for(agent_id).file_context(target_path)

    def _target_allowed(self, target_path: str) -> bool:
        return self.execution_targets.target_allowed(target_path)

    def _target_denied_reason(self, target_path: str) -> Optional[str]:
        return self.execution_targets.denied_reason(target_path)

    def _workspace_relative_path(self, target_path: str) -> Optional[Path]:
        return self.execution_targets.relative_path(target_path)

    def _workspace_rel_excluded(self, rel: Path) -> bool:
        return self.execution_targets.rel_excluded(rel)

    def _workspace_target_path(self, target_path: str) -> Optional[Path]:
        return self.execution_targets.target_path(target_path)

    def _scrub_record(self, value: Any) -> Any:
        if isinstance(value, dict):
            clean: JsonObject = {}
            for key, item in value.items():
                lowered = str(key).lower()
                if any(part in lowered for part in SENSITIVE_KEY_PARTS):
                    clean[key] = "[REDACTED]"
                else:
                    clean[key] = self._scrub_record(item)
            return clean
        if isinstance(value, list):
            return [self._scrub_record(item) for item in value]
        return value

    def _filter_records(
        self,
        records: list[JsonObject],
        filters: JsonObject,
        limit: int,
        *,
        any_key_groups: Optional[list[tuple[str, ...]]] = None,
    ) -> list[JsonObject]:
        result: list[JsonObject] = []
        any_key_groups = any_key_groups or []
        for record in records:
            if self._matches_filters(record, filters, any_key_groups):
                result.append(record)
            if len(result) >= limit:
                break
        return result

    def _matches_filters(self, record: JsonObject, filters: JsonObject, any_key_groups: list[tuple[str, ...]]) -> bool:
        grouped_keys = {key for group in any_key_groups for key in group}
        for key, value in filters.items():
            if value in (None, "") or key in grouped_keys:
                continue
            if record.get(key) != value:
                return False
        for group in any_key_groups:
            expected = next((filters.get(key) for key in group if filters.get(key) not in (None, "")), None)
            if expected is None:
                continue
            if not any(record.get(key) == expected for key in group):
                return False
        return True

    def _sha256_json(self, value: Any) -> str:
        import json

        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def _unique_strings(self, values: Iterable[Any]) -> list[str]:
        return unique_strings(values)

    def _string_list(self, values: Any) -> list[str]:
        if isinstance(values, str):
            return [values] if values else []
        if not isinstance(values, list):
            return []
        return [item for item in values if isinstance(item, str) and item]

    def _short_text(self, value: Optional[str], limit: int = 420) -> str:
        text = " ".join(str(value or "").split())
        if not text:
            return ""
        return text if len(text) <= limit else f"{text[:limit]}..."

    def _latest(self, values: Any) -> Optional[str]:
        if not isinstance(values, list) or not values:
            return None
        value = values[-1]
        return value if isinstance(value, str) and value else None

    def _string(self, value: Any) -> Optional[str]:
        return value if isinstance(value, str) and value else None

    def _parse_datetime(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
