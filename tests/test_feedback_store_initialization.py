from pathlib import Path

from app.runtime.agent_paths import business_agent_layout
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.stores.feedback_store import FeedbackStore


def test_feedback_store_initialization_does_not_create_live_workspace(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    workspace = business_agent_layout(data_dir, DEFAULT_BUSINESS_AGENT_ID).workspace

    store = FeedbackStore(data_dir=data_dir, workspace_dir=workspace)

    assert store.default_workspace_dir == workspace
    assert not workspace.exists()
