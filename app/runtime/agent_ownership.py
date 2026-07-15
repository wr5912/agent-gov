"""持久化治理对象的业务 Agent 所有权校验。"""

from __future__ import annotations

from .agent_paths import InvalidAgentId, validate_agent_id
from .errors import DataIntegrityError


def require_persisted_agent_id(value: object, *, entity: str) -> str:
    """返回对象的合法 agent_id；缺失或非法时禁止静默归入 main-agent。"""
    if not isinstance(value, str):
        raise DataIntegrityError(f"{entity} is missing valid business agent ownership")
    try:
        return validate_agent_id(value)
    except InvalidAgentId as exc:
        raise DataIntegrityError(f"{entity} is missing valid business agent ownership") from exc
