"""业务 Agent 删除：清磁盘、不复活、重建不继承。

删除此前只是注册表 tombstone，磁盘原样保留。这带来一个实测可复现的缺陷：删除后用同一
agent_id 重建，新 Agent 会**静默继承**被删 Agent 的 prompt/skills/MCP 配置。本文件把清理与
其后果固化为契约。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.runtime.protected_business_agents import SECURITY_OPERATIONS_EXPERT_AGENT_ID
from app.services.business_agent_deletion import purge_business_agent_storage
from fastapi.testclient import TestClient

from test_agent_workspace_packages import _import_new_agent
from test_api_execution_optimizer import _load_app


@pytest.fixture()
def app_module(monkeypatch, tmp_path: Path):
    return _load_app(monkeypatch, tmp_path)


def _create(client: TestClient, agent_id: str, name: str = "受测 Agent") -> Path:
    created = _import_new_agent(client, agent_id=agent_id, name=name)
    assert created.status_code == 200, created.text
    return Path(created.json()["agent"]["workspace_dir"])


def test_delete_purges_disk_and_recreate_does_not_inherit(app_module) -> None:
    """核心回归：删除清磁盘，同 id 重建得到全新 workspace。

    这条断言的是先前实测的缺陷不再成立——删除只 tombstone、磁盘保留时，重建会继承旧内容。
    """

    with TestClient(app_module.app) as client:
        workspace = _create(client, "probe-agent")
        (workspace / "CLAUDE.md").write_text("前一个 Agent 的私有内容\n", encoding="utf-8")

        deleted = client.request("DELETE", "/api/agent-registry/probe-agent")
        assert deleted.status_code == 200, deleted.text
        body = deleted.json()
        assert body["workspace_removed"] is True
        assert body["cleanup_complete"] is True
        assert not workspace.exists()
        assert not workspace.parent.exists()  # 整个 root（含 claude-root/version）都清掉

        recreated = _create(client, "probe-agent", name="重建的 Agent")
        content = (recreated / "CLAUDE.md").read_text(encoding="utf-8")
        assert "前一个 Agent 的私有内容" not in content
        assert "重建的 Agent" in content


def test_delete_reports_workspace_cleanup_outcome(app_module) -> None:
    """删除响应必须暴露清理结果——部分失败不能被吞掉。"""

    with TestClient(app_module.app) as client:
        _create(client, "outcome-agent")
        body = client.request("DELETE", "/api/agent-registry/outcome-agent").json()

    assert set(body) >= {"deleted", "impact", "workspace_removed", "cleanup_complete"}
    assert "seed_removed" not in body
    assert body["deleted"]["agent_id"] == "outcome-agent"


def test_protected_agent_delete_is_rejected(app_module) -> None:
    """bootstrap 已登记的受保护 Agent 在线删除必须被拒。"""

    with TestClient(app_module.app) as client:
        builtin = next(item for item in client.get("/api/agent-registry").json() if item["agent_id"] == SECURITY_OPERATIONS_EXPERT_AGENT_ID)
        assert builtin["builtin"] is True
        assert builtin["default"] is True
        assert builtin["protected"] is True
        response = client.request("DELETE", f"/api/agent-registry/{SECURITY_OPERATIONS_EXPERT_AGENT_ID}")

    assert response.status_code == 400


def test_deleted_agent_is_not_runnable(app_module) -> None:
    """删除后该 agent_id 不可运行——resolver 走注册表，tombstone 后 404。"""

    with TestClient(app_module.app) as client:
        _create(client, "gone-agent")
        client.request("DELETE", "/api/agent-registry/gone-agent")

        response = client.post("/api/chat", json={"message": "hi", "agent_id": "gone-agent"})

    assert response.status_code == 404


def test_purge_rejects_path_traversal_agent_id(tmp_path: Path) -> None:
    """越权输入：agent_id 直接作路径段，穿越形态必须在删除前被拒。"""

    from app.services.business_agent_deletion import BusinessAgentDeletionError

    for hostile in ["../escape", "a/b", "..", "."]:
        with pytest.raises(BusinessAgentDeletionError):
            purge_business_agent_storage(data_dir=tmp_path / "data", agent_id=hostile)


def test_purge_does_not_follow_symlinked_workspace(tmp_path: Path) -> None:
    """symlink 不跟随：删除的目标是该 Agent 自己的目录，跟随会把删除放大到目录之外。"""

    data_dir = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("must survive\n", encoding="utf-8")

    agent_root = data_dir / "business-agents" / "linked-agent"
    agent_root.parent.mkdir(parents=True)
    agent_root.symlink_to(outside, target_is_directory=True)

    result = purge_business_agent_storage(data_dir=data_dir, agent_id="linked-agent")

    assert result.workspace_removed is False  # 拒绝删除，且如实报告未清理
    assert (outside / "keep.txt").exists()


def test_purge_is_idempotent(tmp_path: Path) -> None:
    """恢复器会重跑清理：对已清干净的 Agent 再删一次必须成功且不报错。"""

    data_dir = tmp_path / "data"
    (data_dir / "business-agents" / "absent-agent").mkdir(parents=True)

    first = purge_business_agent_storage(data_dir=data_dir, agent_id="absent-agent")
    second = purge_business_agent_storage(data_dir=data_dir, agent_id="absent-agent")

    assert first.cleanup_complete is True
    assert second.cleanup_complete is True
