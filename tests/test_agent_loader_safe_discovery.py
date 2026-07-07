"""agent_loader.discover_skills / discover_agents 的安全元数据发现硬化回归。

这两个函数被 config grounding（喂 governor）和 /api/agents·/api/skills catalog 端点共用。硬化前它们
直接 read_text 解析 frontmatter，绕过大小/symlink/边界防护——workspace 内的 symlink 可读到 workspace 外
文件、超大文件被整体读入。硬化后：拒 symlink（文件/skill 目录/搜索根）、拒逃出 workspace 边界、限制大小、
单项异常降级。
"""

from __future__ import annotations

import os
from pathlib import Path

from app.runtime.agent_loader import MAX_METADATA_FILE_BYTES, discover_agents, discover_skills


def _skill(root: Path, name: str, desc: str) -> None:
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\nbody", encoding="utf-8")


def _agent(root: Path, name: str, desc: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\nprompt", encoding="utf-8")


def test_normal_skills_and_agents_discovered(tmp_path):
    ws = tmp_path / "ws"
    _skill(ws / ".claude" / "skills", "alert-triage", "告警")
    _agent(ws / ".claude" / "agents", "soc-analyst", "分析")
    assert {s["name"] for s in discover_skills(ws)} == {"alert-triage"}
    assert {a["name"] for a in discover_agents(ws)} == {"soc-analyst"}


def test_symlinked_skill_file_escaping_workspace_blocked(tmp_path):
    ws = tmp_path / "ws"
    _skill(ws / ".claude" / "skills", "good", "正常")
    outside = tmp_path / "OUTSIDE.md"
    outside.write_text("---\nname: LEAKED\ndescription: 外部\n---\nx", encoding="utf-8")
    (ws / ".claude" / "skills" / "evil").mkdir()
    os.symlink(outside, ws / ".claude" / "skills" / "evil" / "SKILL.md")
    names = {s["name"] for s in discover_skills(ws)}
    assert names == {"good"}  # symlink 外泄被拦


def test_symlinked_skill_dir_blocked(tmp_path):
    ws = tmp_path / "ws"
    _skill(ws / ".claude" / "skills", "good", "正常")
    os.symlink(tmp_path, ws / ".claude" / "skills" / "dirlink")  # skill 目录是 symlink
    assert {s["name"] for s in discover_skills(ws)} == {"good"}


def test_symlinked_skills_root_blocked(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    outside_skills = tmp_path / "outside_skills"
    _skill(outside_skills, "LEAKED", "外部根")
    os.symlink(outside_skills, ws / ".claude" / "skills")  # 搜索根本身逃逸
    assert discover_skills(ws) == []


def test_oversized_skill_blocked(tmp_path):
    ws = tmp_path / "ws"
    _skill(ws / ".claude" / "skills", "good", "正常")
    (ws / ".claude" / "skills" / "huge").mkdir()
    big = "---\nname: huge\ndescription: 巨大\n---\n" + "x" * (MAX_METADATA_FILE_BYTES + 10)
    (ws / ".claude" / "skills" / "huge" / "SKILL.md").write_text(big, encoding="utf-8")
    assert {s["name"] for s in discover_skills(ws)} == {"good"}


def test_symlinked_agent_file_blocked(tmp_path):
    ws = tmp_path / "ws"
    _agent(ws / ".claude" / "agents", "good", "正常")
    outside = tmp_path / "OUTSIDE_AGENT.md"
    outside.write_text("---\nname: LEAKED\ndescription: 外部\n---\nx", encoding="utf-8")
    os.symlink(outside, ws / ".claude" / "agents" / "evil.md")
    assert {a["name"] for a in discover_agents(ws)} == {"good"}


def test_claude_home_external_root_still_read(tmp_path):
    # claude_home 是合法的 workspace 外根（用户级 .claude），boundary=None，正常文件仍读
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    claude_home = tmp_path / "claude-root" / ".claude"
    _skill(claude_home / "skills", "user-skill", "用户级")
    assert {s["name"] for s in discover_skills(ws, claude_home)} == {"user-skill"}
