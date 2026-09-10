"""AgentScope Harness Skill/subagent 元数据发现的边界回归。"""

from __future__ import annotations

import os
from pathlib import Path

from app.runtime.agent_loader import MAX_METADATA_FILE_BYTES, discover_agents, discover_skills


def _skill(root: Path, name: str, desc: str) -> None:
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\nbody", encoding="utf-8")


def _agent(root: Path, name: str, desc: str) -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "agent.yaml").write_text(
        "schema_version: 1\n"
        "agent:\n"
        f"  id: {name}\n"
        f"  name: {name}\n"
        f"  description: {desc}\n"
        "  runtime: agentscope\n"
        "  runtime_contract: agentscope-app/2.0.8\n"
        "workspace_policy:\n"
        "  allowed_tools: [Read]\n",
        encoding="utf-8",
    )
    (directory / "AGENT.md").write_text("prompt\n", encoding="utf-8")


def test_normal_skills_and_agents_discovered(tmp_path):
    ws = tmp_path / "ws"
    _skill(ws / "skills", "alert-triage", "告警")
    _agent(ws / "subagents", "soc-analyst", "分析")
    assert {s["name"] for s in discover_skills(ws)} == {"alert-triage"}
    assert {a["name"] for a in discover_agents(ws)} == {"soc-analyst"}


def test_symlinked_skill_file_escaping_workspace_blocked(tmp_path):
    ws = tmp_path / "ws"
    _skill(ws / "skills", "good", "正常")
    outside = tmp_path / "OUTSIDE.md"
    outside.write_text("---\nname: LEAKED\ndescription: 外部\n---\nx", encoding="utf-8")
    (ws / "skills" / "evil").mkdir()
    os.symlink(outside, ws / "skills" / "evil" / "SKILL.md")
    names = {s["name"] for s in discover_skills(ws)}
    assert names == {"good"}  # symlink 外泄被拦


def test_symlinked_skill_dir_blocked(tmp_path):
    ws = tmp_path / "ws"
    _skill(ws / "skills", "good", "正常")
    os.symlink(tmp_path, ws / "skills" / "dirlink")  # skill 目录是 symlink
    assert {s["name"] for s in discover_skills(ws)} == {"good"}


def test_symlinked_skills_root_blocked(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    outside_skills = tmp_path / "outside_skills"
    _skill(outside_skills, "LEAKED", "外部根")
    os.symlink(outside_skills, ws / "skills")  # 搜索根本身逃逸
    assert discover_skills(ws) == []


def test_oversized_skill_blocked(tmp_path):
    ws = tmp_path / "ws"
    _skill(ws / "skills", "good", "正常")
    (ws / "skills" / "huge").mkdir()
    big = "---\nname: huge\ndescription: 巨大\n---\n" + "x" * (MAX_METADATA_FILE_BYTES + 10)
    (ws / "skills" / "huge" / "SKILL.md").write_text(big, encoding="utf-8")
    assert {s["name"] for s in discover_skills(ws)} == {"good"}


def test_symlinked_agent_file_blocked(tmp_path):
    ws = tmp_path / "ws"
    _agent(ws / "subagents", "good", "正常")
    outside = tmp_path / "OUTSIDE_AGENT.md"
    outside.write_text("outside", encoding="utf-8")
    (ws / "subagents" / "evil").mkdir()
    (ws / "subagents" / "evil" / "agent.yaml").write_text("agent: {id: evil}\n", encoding="utf-8")
    os.symlink(outside, ws / "subagents" / "evil" / "AGENT.md")
    assert {a["name"] for a in discover_agents(ws)} == {"good"}


def test_external_runtime_home_is_never_merged_into_harness(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    runtime_home = tmp_path / "runtime-home"
    _skill(runtime_home / "skills", "user-skill", "用户级")
    assert discover_skills(ws, runtime_home) == []
