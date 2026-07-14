"""#27：bootstrap 对 data/business-agents/* 只做存在性对账，绝不覆盖活的优化配置；新 seed Agent 仍渲染。"""
from __future__ import annotations

from pathlib import Path

from scripts.bootstrap_runtime_volume import bootstrap_runtime_volume


def _seed_agent(root: Path, agent_id: str, content: str) -> Path:
    ws = root / "data" / "business-agents" / agent_id / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    claude_md = ws / "CLAUDE.md"
    claude_md.write_text(content, encoding="utf-8")
    return claude_md


def test_bootstrap_preserves_optimized_business_agent_and_renders_new_seed(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    root = tmp_path / "root"
    # seed 声明 AAA（卷里已存在、已优化）+ CCC（卷里缺失，开发者新增的预置 Agent）
    _seed_agent(seed, "AAA", "AAA seed birth config\n")
    _seed_agent(seed, "CCC", "CCC seed birth config\n")
    # 卷里 AAA 已被反馈优化闭环改写（与 seed 不同）——绝不能被 seed 覆盖
    optimized = _seed_agent(root, "AAA", "AAA OPTIMIZED by feedback loop — must NOT be overwritten\n")

    # 已存在 workspace 只做存在性对账，不逐文件回灌 seed。
    result = bootstrap_runtime_volume(runtime_root=root, template_dir=seed)

    # AAA 优化配置原样保留（未被 seed 覆盖、未被 repair）
    assert optimized.read_text(encoding="utf-8") == "AAA OPTIMIZED by feedback loop — must NOT be overwritten\n"
    # CCC（新 seed Agent，卷里缺失）被渲染补全（存在性对账）
    ccc = root / "data" / "business-agents" / "CCC" / "workspace" / "CLAUDE.md"
    assert ccc.read_text(encoding="utf-8") == "CCC seed birth config\n"
    # AAA 未出现在 copied（整个 workspace 被保护跳过）。
    assert not any("business-agents/AAA/" in p for p in result["copied"])
