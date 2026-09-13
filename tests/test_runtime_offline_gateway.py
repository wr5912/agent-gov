"""离线 Workspace 的配置与真实文件系统安全边界。

镜像内 wheel/tool、Bubblewrap、MCP gateway、超时清理和 warm restart 只能由当前
镜像的真实容器验收，不在 pytest 中替换 SDK 或操作系统边界。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agentscope_runtime.offline_gateway import validate_runtime_state_links
from agentscope_runtime.workspace_manager import AgentGovLocalWorkspace, harness_digest


def test_workspace_public_env_enforces_offline_without_shared_cache(tmp_path: Path) -> None:
    workspace = AgentGovLocalWorkspace(
        harness_root=tmp_path,
        expected_digest="a" * 64,
        host_workdir=str(tmp_path / "state"),
        host_cache_dir=str(tmp_path / "private-cache"),
        sandbox_env={
            "UV_OFFLINE": "0",
            "UV_NO_CACHE": "0",
            "HTTPS_PROXY": "http://proxy.test:8080",
        },
    )

    assert workspace.env == {
        "HTTPS_PROXY": "http://proxy.test:8080",
        "UV_OFFLINE": "1",
        "UV_NO_CACHE": "1",
        "UV_FIND_LINKS": "file:///usr/local/share/agentgov/gateway-wheels",
        "UV_PYTHON": "/usr/local/bin/python",
        "UV_PYTHON_DOWNLOADS": "never",
    }
    assert workspace.extra_pip == ["agentscope==2.0.8"]
    assert workspace.share_net is True


def test_runtime_state_link_scan_accepts_real_regular_tree(tmp_path: Path) -> None:
    state = tmp_path / ".agentgov-runtime-state"
    (state / "nested").mkdir(parents=True)
    (state / "nested" / "receipt.json").write_text("{}\n", encoding="utf-8")

    validate_runtime_state_links(tmp_path)


def test_runtime_state_link_scan_rejects_real_unapproved_symlink(tmp_path: Path) -> None:
    state = tmp_path / ".agentgov-runtime-state"
    state.mkdir()
    (state / "unsafe-link").symlink_to("/etc/passwd")

    with pytest.raises(ValueError, match="unapproved symlink"):
        validate_runtime_state_links(tmp_path)


def test_harness_digest_changes_with_real_workspace_bytes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent_md = workspace / "AGENT.md"
    agent_md.write_text("# v1\n", encoding="utf-8")
    first = harness_digest(workspace)

    agent_md.write_text("# v2\n", encoding="utf-8")
    second = harness_digest(workspace)

    assert len(first) == 64
    assert len(second) == 64
    assert first != second
