from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIT_SCRIPT = REPO_ROOT / ".codex/skills/codex-config-optimizer/scripts/audit_codex_config.py"


def _load_audit_module():
    spec = importlib.util.spec_from_file_location("audit_codex_config", AUDIT_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_codex_config_audit_reports_guided_hot_terms(tmp_path, capsys):
    module = _load_audit_module()
    _write(tmp_path / "AGENTS.md", "治理硬门\n治理硬门\n")
    _write(tmp_path / "AGENTS.override.md", "治理硬门\n")
    _write(
        tmp_path / ".codex/skills/project-skill/SKILL.md",
        '---\nname: "project-skill"\ndescription: "使用 编写 调试 配置 skill"\n---\n治理硬门\n治理硬门\n',
    )

    module._print_report(tmp_path)

    output = capsys.readouterr().out
    assert "`治理硬门` 出现 5 次" in output
    assert "建议动作：`merge`" in output
    assert "目标配置面：项目覆盖层保留命令；skill/reference 保留解释" in output
    assert "验证：审计报告仍能定位硬门命令，但常驻说明不重复展开" in output


def test_codex_config_audit_reports_env_override_terminology_risks(tmp_path, capsys):
    module = _load_audit_module()
    _write(tmp_path / "AGENTS.md", "环境变量使用本地私有覆盖文件。\n")
    _write(tmp_path / "AGENTS.override.md", "不要把 env 文件叫覆盖文件。\n")

    module._print_report(tmp_path)

    output = capsys.readouterr().out
    assert "## 术语风险" in output
    assert "`AGENTS.md:1` 命中 `本地私有覆盖`" in output
    assert "选择 env 文件" in output
    assert "AGENTS.override.md:1" not in output


def test_feedback_runtime_preflight_reference_is_linked():
    skill = (REPO_ROOT / ".codex/skills/codex-config-optimizer/SKILL.md").read_text(encoding="utf-8")
    preflight = (REPO_ROOT / ".codex/skills/codex-config-optimizer/references/feedback-runtime-preflight.md").read_text(encoding="utf-8")

    assert "feedback-runtime-preflight.md" in skill
    assert "UI state -> API response -> agent_jobs -> store projection -> formatter/validated output -> persisted payload" in preflight
    assert "backend-owned、agent-owned、boundary-owned" in preflight
    assert "tests/coverage_policy.json" in preflight


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
