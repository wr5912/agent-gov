"""business-agent-workspace-optimizer skill 的回归：存在性、frontmatter、镜像一致、纠偏后的安全边界内容。

该 skill 是开发者离线工具，用于优化业务 Agent 自身 workspace 配置资产；本测试锁定评审纠偏点
（per-agent 版本库不可改、不新增无效 ask、复用现成脱敏扫描、业务 Agent 在 data/ 下、governor 单一）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from check_docs_governance import MIRRORED_SKILLS, _normalized_skill_text  # noqa: E402

SKILL_NAME = "business-agent-workspace-optimizer"
CODEX_PATH = ROOT / ".codex" / "skills" / SKILL_NAME / "SKILL.md"
CLAUDE_PATH = ROOT / ".claude" / "skills" / SKILL_NAME / "SKILL.md"


def _frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n"), "SKILL.md 必须以 frontmatter 开头"
    block = text.split("---\n", 2)[1]
    out: dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip().strip('"')
    return out


def test_skill_pair_exists_with_valid_frontmatter():
    for path in (CODEX_PATH, CLAUDE_PATH):
        assert path.is_file(), f"缺少 {path}"
        fm = _frontmatter(path.read_text(encoding="utf-8"))
        assert fm.get("name") == SKILL_NAME
        assert len(fm.get("description", "")) > 30


def test_skill_pair_registered_in_mirrored_skills():
    pair = (
        f".codex/skills/{SKILL_NAME}/SKILL.md",
        f".claude/skills/{SKILL_NAME}/SKILL.md",
    )
    assert pair in MIRRORED_SKILLS


def test_skill_pair_is_mirror_consistent():
    """除 claude 侧同源镜像提示行外，两侧规范化文本必须一致（与 check_docs_governance 同口径）。"""
    codex = _normalized_skill_text(CODEX_PATH.read_text(encoding="utf-8"))
    claude = _normalized_skill_text(CLAUDE_PATH.read_text(encoding="utf-8"))
    assert codex == claude
    # claude 侧必须带同源镜像提示行；codex 侧不带。
    assert "同源镜像" in CLAUDE_PATH.read_text(encoding="utf-8")
    assert "> 本技能与 `" not in CODEX_PATH.read_text(encoding="utf-8")


def test_skill_encodes_review_corrected_boundaries():
    """锁定评审纠偏：per-agent 版本库/运行态不可改、不新增 ask、复用现成脱敏扫描、业务 Agent 在 data/ 下、governor 单一。"""
    text = CODEX_PATH.read_text(encoding="utf-8")
    # B3 版本库与运行态状态默认拒绝。
    assert "version/" in text and "per-agent" in text
    # data/ 不可整目录拒绝（业务 Agent workspace 在其下）。
    assert "data/business-agents/<agent_id>" in text
    assert "不能整目录拒绝 `data/`" in text
    # 权限模型对齐 #1 整改：不新增 ask、allow/deny + 对话级人审。
    assert "不要新增 `ask` 条目" in text
    # 复用现成模板脱敏扫描，不重复造轮子。
    assert "runtime-template-scan" in text
    assert "scan_path" in text
    # governor 已是单一治理 workspace（合并后），且默认不作为目标。
    assert "governor-workspace" in text
    assert "默认不作为目标" in text
    # 无硬编码他机绝对路径。
    assert "/home/admin" not in text
