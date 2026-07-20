"""business-agent-workspace-optimizer skill 的回归：存在性、frontmatter、镜像一致和通用边界。

该 skill 是开发者离线工具，用于优化业务 Agent 自身 workspace 配置资产；本测试锁定评审纠偏点
（per-agent 版本库不可改、权限来自目标 Workspace、复用现成准入扫描、业务 Agent 在 data/ 下、governor 单一）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from check_docs_governance import _normalized_skill_text, collect_mirrored_skill_pairs  # noqa: E402

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


def test_skill_pair_is_dynamically_discovered_as_mirrored():
    pair = (
        f".codex/skills/{SKILL_NAME}/SKILL.md",
        f".claude/skills/{SKILL_NAME}/SKILL.md",
    )
    assert pair in collect_mirrored_skill_pairs(ROOT)


def test_skill_pair_is_mirror_consistent():
    """除 claude 侧同源镜像提示行外，两侧规范化文本必须一致（与 check_docs_governance 同口径）。"""
    codex = _normalized_skill_text(CODEX_PATH.read_text(encoding="utf-8"))
    claude = _normalized_skill_text(CLAUDE_PATH.read_text(encoding="utf-8"))
    assert codex == claude
    # claude 侧必须带同源镜像提示行；codex 侧不带。
    assert "同源镜像" in CLAUDE_PATH.read_text(encoding="utf-8")
    assert "> 本技能与 `" not in CODEX_PATH.read_text(encoding="utf-8")


def test_skill_encodes_review_corrected_boundaries():
    """锁定评审纠偏：per-agent 边界、原生权限、现成准入扫描、data/ 业务路径与 governor 单一性。"""
    text = CODEX_PATH.read_text(encoding="utf-8")
    # B3 版本库与运行态状态默认拒绝。
    assert "version/" in text and "per-Agent" in text
    # data/ 不可整目录拒绝（业务 Agent workspace 在其下）。
    assert "data/business-agents/<agent_id>" in text
    assert "目标解析矩阵" in text
    assert "runtime 父目录" in text
    assert "no-op 并重新定位" in text
    assert "不能整目录拒绝 `data/`" in text
    assert "`${RUNTIME_ROOT}/data/business-agents/<agent_id>/workspace/`" in text
    assert "`data/` 和 `data/business-agents/` 父目录本身也不是优化目标" in text
    # 权限模型对齐：通用 Bash 进入 ask，run grant 只覆盖低风险类别，具体工具来自目标 Workspace。
    assert "宽泛 Bash 默认进入 `ask`" in text
    assert "run 级授权必须按低风险类别隔离" in text
    assert "高风险或未分类请求不得整轮放行" in text
    assert "工具、权限、确认和业务流程契约只从目标 Workspace" in text
    assert "本通用 skill 不硬编码具体工具名或领域流程" in text
    assert "soc_api__" not in text
    assert "security-operations-expert" not in text
    assert "后端不得" in text and "按 Agent ID 添加第二套授权" in text
    # 复用现成运行卷初始化源准入扫描，不重复造轮子。
    assert "runtime-bootstrap-scan" in text
    assert "runtime_bootstrap_safety.py" in text
    assert "scan_path" in text
    assert "不存在运行态 seed catalog" in text
    assert "不得把 `main-agent`" in text
    # governor 已是单一治理 workspace（合并后），且默认不作为目标。
    assert "governor-workspace" in text
    assert "用户明确要求时" in text
    # 无硬编码他机绝对路径。
    assert "/home/admin" not in text
