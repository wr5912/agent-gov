from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ...version import APP_VERSION
from ..agent_ownership import require_persisted_agent_id
from ..agent_paths import business_agent_layout
from ..collection_utils import unique_strings
from ..errors import DataIntegrityError
from ..feedback_privacy import SENSITIVE_KEY_PARTS
from ..json_types import JsonObject
from ..runtime_db import (
    make_session_factory,
    runtime_db_path_from_data_dir,
)
from .agent_job_store import AgentJobStoreMixin
from .feedback_case_store import FeedbackCaseStoreMixin
from .feedback_eval_store import FeedbackEvalStoreMixin
from .feedback_evidence_store import FeedbackEvidenceStoreMixin
from .feedback_source_store import FeedbackSourceStoreMixin


class FeedbackStore(
    AgentJobStoreMixin,
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
        agent_version_provider: Optional[Callable[[str], Optional[str]]] = None,
        agent_exists: Optional[Callable[[str], bool]] = None,
        runtime_version: str = APP_VERSION,
        enable_debug_evidence: bool = True,
    ) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.main_workspace_dir = workspace_dir or business_agent_layout(data_dir, "main-agent").workspace
        # main-agent workspace 在 /data 下，确保存在（与 get_settings 一致；执行/证据写入依赖它）。
        self.main_workspace_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = runtime_db_path_from_data_dir(data_dir)
        self.Session = make_session_factory(self.db_path)
        self.agent_version_provider = agent_version_provider
        self.agent_exists = agent_exists
        self.runtime_version = runtime_version
        self.enable_debug_evidence = enable_debug_evidence
        self.langfuse_trace_fetcher: Optional[Callable[[str], Optional[JsonObject]]] = None

    def set_langfuse_trace_fetcher(self, fetcher: Callable[[str], Optional[JsonObject]]) -> None:
        # The fetcher is owned by the backend so Langfuse credentials never enter
        # internal Agent prompts or Claude Code tool configuration.
        self.langfuse_trace_fetcher = fetcher

    def _current_agent_version_id(self, agent_id: str) -> Optional[str]:
        # #24-C/D：版本解析必须携带归属，禁止缺失所有权时静默落到 main 版本库。
        if not self.agent_version_provider:
            return None
        return self.agent_version_provider(require_persisted_agent_id(agent_id, entity="Agent version lookup"))

    def _resolve_task_agent_id(self, *, feedback_case_id: str) -> str:
        feedback_case = self.find_case(feedback_case_id)
        if not feedback_case:
            raise DataIntegrityError(f"FeedbackCase not found for Agent ownership: {feedback_case_id}")
        return require_persisted_agent_id(feedback_case.get("agent_id"), entity=f"FeedbackCase {feedback_case_id}")

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
