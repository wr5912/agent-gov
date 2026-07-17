"""运行态 seed catalog 三级管线：仓库出生配置 -> catalog -> live workspace。

为什么要中间这一层：仓库出生配置在容器里是只读挂载，且是 git 资产——在线删除一个内置
Agent 无法作用于它。若 live 直接从仓库播种，删除就不粘：workspace 一旦被清理，下次启动
bootstrap 发现它缺失又照仓库补回来。catalog 是可写副本，删除标记落在这里，删除才有粘性。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.runtime.business_agent_seed_catalog import (
    declared_business_agent_ids,
    runtime_seed_catalog_dir,
    seed_deletion_marker_path,
)
from app.runtime.protected_business_agents import SECURITY_OPERATIONS_EXPERT_AGENT_ID
from scripts.bootstrap_runtime_volume import bootstrap_runtime_volume


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture()
def repo_seeds(tmp_path: Path) -> Path:
    """最小仓库出生配置：两个普通 seed + 受保护 seed + governor + template。"""

    root = tmp_path / "repo-seeds"
    for agent_id in ("main-agent", "alpha-agent", SECURITY_OPERATIONS_EXPERT_AGENT_ID):
        base = root / "data" / "business-agents" / agent_id / "workspace"
        _write(base / "CLAUDE.md", f"{agent_id} 出生配置\n")
        _write(base / ".mcp.json", '{"mcpServers": {}}\n')
    _write(root / "governor-workspace" / "CLAUDE.md", "governor\n")
    _write(root / "templates" / "business-agent" / "general" / "CLAUDE.md", "{{AGENT_NAME}}\n")
    return root


def _bootstrap(runtime_root: Path, repo_seeds: Path):
    return bootstrap_runtime_volume(runtime_root=runtime_root, template_dir=repo_seeds)


def test_repo_seeds_flow_through_catalog_into_live(tmp_path: Path, repo_seeds: Path) -> None:
    runtime_root = tmp_path / "volume"
    _bootstrap(runtime_root, repo_seeds)

    catalog = runtime_seed_catalog_dir(runtime_root / "data")
    for agent_id in ("main-agent", "alpha-agent"):
        catalog_file = catalog / "data" / "business-agents" / agent_id / "workspace" / "CLAUDE.md"
        live_file = runtime_root / "data" / "business-agents" / agent_id / "workspace" / "CLAUDE.md"
        assert catalog_file.read_text(encoding="utf-8") == f"{agent_id} 出生配置\n"
        assert live_file.read_text(encoding="utf-8") == f"{agent_id} 出生配置\n"

    # 声明集以 catalog 为准，与仓库内容一致。
    assert declared_business_agent_ids(seed_root=catalog) == {
        "main-agent",
        "alpha-agent",
        SECURITY_OPERATIONS_EXPERT_AGENT_ID,
    }


def test_governor_and_templates_still_come_straight_from_repo(tmp_path: Path, repo_seeds: Path) -> None:
    runtime_root = tmp_path / "volume"
    _bootstrap(runtime_root, repo_seeds)

    assert (runtime_root / "governor-workspace" / "CLAUDE.md").exists()
    assert (runtime_root / "templates" / "business-agent" / "general" / "CLAUDE.md").exists()
    # 它们不经过 catalog——catalog 只承载可被在线管理的业务 Agent seed。
    catalog = runtime_seed_catalog_dir(runtime_root / "data")
    assert not (catalog / "governor-workspace").exists()


def test_deletion_marker_keeps_seed_deleted_across_bootstrap(tmp_path: Path, repo_seeds: Path) -> None:
    """删除粘性：这是引入 catalog 的全部理由。"""

    runtime_root = tmp_path / "volume"
    _bootstrap(runtime_root, repo_seeds)
    catalog = runtime_seed_catalog_dir(runtime_root / "data")

    # 模拟在线删除：写标记 + 移除 catalog 条目 + 移除 live 目录。
    import shutil

    seed_deletion_marker_path("alpha-agent", seed_root=catalog).write_text("", encoding="utf-8")
    shutil.rmtree(catalog / "data" / "business-agents" / "alpha-agent")
    shutil.rmtree(runtime_root / "data" / "business-agents" / "alpha-agent")

    result = _bootstrap(runtime_root, repo_seeds)

    assert not (catalog / "data" / "business-agents" / "alpha-agent").exists()
    # 关键断言：live 也不得被重建——否则会留下未注册的孤儿目录。
    assert not (runtime_root / "data" / "business-agents" / "alpha-agent").exists()
    assert any("alpha-agent" in item for item in result["seed_catalog_skipped_deleted"])
    assert "alpha-agent" not in declared_business_agent_ids(seed_root=catalog)
    # 未被删除的 seed 不受影响。
    assert (runtime_root / "data" / "business-agents" / "main-agent" / "workspace").is_dir()


def test_protected_seed_marker_is_repaired(tmp_path: Path, repo_seeds: Path) -> None:
    """受保护 Agent 的真相源在仓库：标记只可能来自手工投毒，bootstrap 必须修复。"""

    runtime_root = tmp_path / "volume"
    _bootstrap(runtime_root, repo_seeds)
    catalog = runtime_seed_catalog_dir(runtime_root / "data")

    import shutil

    marker = seed_deletion_marker_path(SECURITY_OPERATIONS_EXPERT_AGENT_ID, seed_root=catalog)
    marker.write_text("", encoding="utf-8")
    shutil.rmtree(catalog / "data" / "business-agents" / SECURITY_OPERATIONS_EXPERT_AGENT_ID)

    _bootstrap(runtime_root, repo_seeds)

    assert not marker.exists()
    restored = catalog / "data" / "business-agents" / SECURITY_OPERATIONS_EXPERT_AGENT_ID / "workspace" / "CLAUDE.md"
    assert restored.read_text(encoding="utf-8") == f"{SECURITY_OPERATIONS_EXPERT_AGENT_ID} 出生配置\n"


def test_live_workspace_edits_are_never_reconciled_from_catalog(tmp_path: Path, repo_seeds: Path) -> None:
    """既有不变量不得回归：live workspace 是行为真相源，bootstrap 绝不回灌。"""

    runtime_root = tmp_path / "volume"
    _bootstrap(runtime_root, repo_seeds)

    live = runtime_root / "data" / "business-agents" / "alpha-agent" / "workspace"
    (live / "CLAUDE.md").write_text("用户优化后的内容\n", encoding="utf-8")
    (live / ".mcp.json").unlink()  # 用户有意删除的文件

    _bootstrap(runtime_root, repo_seeds)

    assert (live / "CLAUDE.md").read_text(encoding="utf-8") == "用户优化后的内容\n"
    assert not (live / ".mcp.json").exists()


def test_bootstrap_script_runs_standalone_without_app_package() -> None:
    """bootstrap 必须能作为独立脚本运行。

    Dockerfile 只 COPY 这个脚本本身（不带 app 包），runtime-init 容器与 Makefile 都直接执行它。
    给它加 `from app...` import 会让容器启动直接崩，而 pytest 因 rootdir 在 sys.path 上仍然全绿
    ——这个契约只能靠子进程验证。
    """

    import subprocess
    import sys

    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "bootstrap_runtime_volume.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd="/",  # 不在仓库根下运行，杜绝隐式 sys.path 命中 app 包
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"独立运行失败（很可能引入了 app.* import）:\n{result.stderr}"


def test_catalog_constants_match_runtime_module() -> None:
    """脚本内联的 catalog 常量必须与 runtime 模块一致。

    脚本不能 import app（见上），因此常量在两处各有一份。这条测试是它们之间唯一的粘合剂。
    """

    from app.runtime.business_agent_seed_catalog import RUNTIME_SEED_CATALOG_DIRNAME as runtime_dirname
    from app.runtime.protected_business_agents import PROTECTED_BUSINESS_AGENT_IDS as runtime_protected
    from scripts.bootstrap_runtime_volume import (
        PROTECTED_BUSINESS_AGENT_IDS as script_protected,
    )
    from scripts.bootstrap_runtime_volume import (
        RUNTIME_SEED_CATALOG_DIRNAME as script_dirname,
    )

    assert script_dirname == runtime_dirname
    assert script_protected == runtime_protected


def test_main_agent_fixed_dirs_are_not_recreated(tmp_path: Path, repo_seeds: Path) -> None:
    """main-agent 是可删除的普通业务 Agent：bootstrap 不得为它无条件建 version/claude-root 骨架。"""

    runtime_root = tmp_path / "volume"
    _bootstrap(runtime_root, repo_seeds)

    import shutil

    catalog = runtime_seed_catalog_dir(runtime_root / "data")
    seed_deletion_marker_path("main-agent", seed_root=catalog).write_text("", encoding="utf-8")
    shutil.rmtree(catalog / "data" / "business-agents" / "main-agent")
    shutil.rmtree(runtime_root / "data" / "business-agents" / "main-agent")

    _bootstrap(runtime_root, repo_seeds)

    assert not (runtime_root / "data" / "business-agents" / "main-agent").exists()
