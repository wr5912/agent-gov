from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from ..automation_db import AutomationPolicyModel
from ..errors import BusinessRuleViolation
from ..runtime_db import utc_now

AUTOMATION_MODES = {"off", "semi", "full"}
DEFAULT_AUTOMATION_MODE = "off"


class AutomationPolicyStore:
    """按业务 Agent 持久化自动化策略 mode；缺省 off（全人工触发）。"""

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def get_mode(self, agent_id: str) -> str:
        key = (agent_id or "").strip()
        if not key:
            return DEFAULT_AUTOMATION_MODE
        with self._session_factory.begin() as db:
            row = db.get(AutomationPolicyModel, key)
            return row.mode if row is not None else DEFAULT_AUTOMATION_MODE

    def set_mode(self, agent_id: str, *, mode: str) -> str:
        key = (agent_id or "").strip()
        if not key:
            raise BusinessRuleViolation("AutomationPolicy requires a business agent id")
        if mode not in AUTOMATION_MODES:
            raise BusinessRuleViolation(f"Unknown automation mode: {mode}; expected one of {sorted(AUTOMATION_MODES)}")
        with self._session_factory.begin() as db:
            row = db.get(AutomationPolicyModel, key)
            if row is None:
                db.add(AutomationPolicyModel(agent_id=key, mode=mode, updated_at=utc_now()))
            else:
                row.mode = mode
                row.updated_at = utc_now()
        return mode
