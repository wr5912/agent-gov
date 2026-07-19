from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import sessionmaker

from ..json_types import JsonObject
from ..runtime_db import RuntimeSettingModel, utc_now

# /v1/chat/completions（OpenAI 兼容入口）跑哪个 Agent，由运营者经设置 API 配置；
# 未配置时由调用方选择 DEFAULT_BUSINESS_AGENT_ID。该值是 backend-owned 运行时配置。
OPENAI_COMPAT_AGENT_KEY = "openai_compat_agent_id"


class RuntimeSettingsStore:
    """Operator-level runtime settings, persisted as a minimal SQLite key-value store.

    Public accessors are typed and purpose-specific; the generic JsonObject KV access stays
    private so the store never hands out an unowned dict.
    """

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def get_openai_compat_agent_id(self) -> Optional[str]:
        """The explicitly-configured /v1 出口 Agent id.

        Returns ``None`` only when the operator has **never** configured it (no row): that is a
        distinct state from an explicit Agent choice. The caller derives the effective Agent from
        ``configured or DEFAULT_BUSINESS_AGENT_ID``.
        """
        value = self._get(OPENAI_COMPAT_AGENT_KEY)
        agent_id = value.get("agent_id") if isinstance(value, dict) else None
        # 空白 / 非法值按"未配置"处理（防御历史或异常行），与 configured 语义一致。
        return agent_id if isinstance(agent_id, str) and agent_id.strip() else None

    def set_openai_compat_agent_id(self, agent_id: str) -> None:
        self._set(OPENAI_COMPAT_AGENT_KEY, {"agent_id": agent_id})

    def clear_openai_compat_agent_id(self) -> bool:
        """Reset to unconfigured (back to the platform default). Returns True if a row existed."""
        return self._delete(OPENAI_COMPAT_AGENT_KEY)

    def _get(self, key: str) -> Optional[JsonObject]:
        with self._session_factory() as db:
            record = db.get(RuntimeSettingModel, key)
            return record.value_json if record else None

    def _set(self, key: str, value: JsonObject) -> None:
        with self._session_factory.begin() as db:
            existing = db.get(RuntimeSettingModel, key)
            if existing:
                existing.value_json = value
                existing.updated_at = utc_now()
            else:
                db.add(RuntimeSettingModel(key=key, value_json=value, updated_at=utc_now()))

    def _delete(self, key: str) -> bool:
        with self._session_factory.begin() as db:
            record = db.get(RuntimeSettingModel, key)
            if not record:
                return False
            db.delete(record)
            return True
