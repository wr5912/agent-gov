from pathlib import Path

from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway.store import RuntimeRunStore


def store_with_agent_version(tmp_path: Path) -> RuntimeRunStore:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    store.bind_agent_version(
        agent_id="agent-a",
        agent_version_id="version-a",
        digest="a" * 64,
        runtime_agent_id="runtime-a",
    )
    return store
