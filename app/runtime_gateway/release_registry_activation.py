from __future__ import annotations

from app.runtime.stores.agent_registry_store import AgentRegistryStore

from .store import RuntimeStateConflict


def activate_registry_after_release(registry: AgentRegistryStore, agent_id: str) -> None:
    """幂等完成 draft -> active；其他生命周期一律 fail closed。"""

    record = registry.get_agent(agent_id)
    if record is None:
        raise RuntimeStateConflict("Released business Agent registry identity is missing")
    if record.status == "active":
        return
    if record.status != "draft":
        raise RuntimeStateConflict("Released business Agent lifecycle cannot enter active")
    try:
        registry.activate_business_agent_after_release(agent_id)
    except Exception as exc:
        repeated = registry.get_agent(agent_id)
        if repeated is None or repeated.status != "active":
            raise RuntimeStateConflict("Released business Agent lifecycle activation is pending") from exc
