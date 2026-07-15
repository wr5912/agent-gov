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
    assert "`AGENTS.override.md:1` 命中" not in output


def test_codex_config_audit_includes_claude_surfaces(tmp_path, capsys):
    module = _load_audit_module()
    _write(tmp_path / "CLAUDE.md", "# Claude root\n")
    _write(tmp_path / ".claude/rules/agentgov-project.md", "# Claude project\n")
    _write(tmp_path / ".claude/README.md", "# Claude\n")
    _write(tmp_path / ".claude/agents/reviewer.md", "# Reviewer\n")
    _write(tmp_path / ".claude/rules/project.md", "# Project\n")
    _write(
        tmp_path / ".claude/skills/project-skill/SKILL.md",
        '---\nname: "project-skill"\ndescription: "使用 编写 调试 配置 skill"\n---\n\n# Project\n',
    )

    module._print_report(tmp_path)

    output = capsys.readouterr().out
    assert "`CLAUDE.md`" in output
    assert "`.claude/rules/agentgov-project.md`" in output
    assert "`.claude/README.md`" in output
    assert "`.claude/agents/reviewer.md`" in output
    assert "`.claude/rules/project.md`" in output
    assert "`.claude/skills/project-skill/SKILL.md`" in output


def test_codex_config_audit_reports_three_matrix_coverage(tmp_path, capsys):
    module = _load_audit_module()
    for rel_path, _label, markers, _action, _verification in module.MATRIX_EXPECTATIONS:
        _write(tmp_path / rel_path, "\n".join(markers) + "\n")

    module._print_report(tmp_path)

    output = capsys.readouterr().out
    assert "## 三矩阵覆盖" in output
    assert "OK `.codex/skills/codex-config-optimizer/SKILL.md` 覆盖 配置治理三矩阵" in output
    assert "MISSING" not in output


def test_project_config_audit_matrix_coverage_is_complete():
    module = _load_audit_module()

    missing = [
        (coverage.path, coverage.label, coverage.missing_markers)
        for coverage in module._matrix_coverage(REPO_ROOT)
        if coverage.missing_markers
    ]

    assert missing == []


def test_codex_config_audit_treats_missing_matrix_markers_as_blocking(tmp_path):
    module = _load_audit_module()

    assert module._has_blocking_findings(tmp_path, []) is True


def test_feedback_runtime_preflight_reference_is_linked():
    skill = (REPO_ROOT / ".codex/skills/codex-config-optimizer/SKILL.md").read_text(encoding="utf-8")
    preflight = (REPO_ROOT / ".codex/skills/codex-config-optimizer/references/feedback-runtime-preflight.md").read_text(encoding="utf-8")

    assert "feedback-runtime-preflight.md" in skill
    assert "UI state -> API response -> agent_jobs -> store projection -> formatter/validated output -> persisted payload" in preflight
    assert "backend-owned、agent-owned、boundary-owned" in preflight
    assert "tests/coverage_policy.json" in preflight


def test_agentgov_boundary_first_entries_are_kept():
    codex_project = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    claude_project = (REPO_ROOT / ".claude/rules/agentgov-project.md").read_text(encoding="utf-8")
    required = (
        "反复整改前置矩阵",
        "治理对象矩阵",
        "配置面矩阵",
        "验收路径矩阵",
        "UI 语义矩阵",
        "不得用 local-debug 结果声明容器验收通过",
    )

    for text in (codex_project, claude_project):
        for marker in required:
            assert marker in text


def test_codex_config_audit_reports_same_directory_agents_shadowing(tmp_path, capsys):
    module = _load_audit_module()
    _write(tmp_path / "AGENTS.md", "# Base\n")
    _write(tmp_path / "AGENTS.override.md", "# Override\n")

    module._print_report(tmp_path)

    output = capsys.readouterr().out
    assert "`P0` `AGENTS.override.md:1`" in output
    assert "会遮蔽 AGENTS.md，而不是与其叠加" in output
    assert "建议动作：`merge`" in output


def test_codex_config_audit_rejects_shared_env_write_allow(tmp_path, capsys):
    module = _load_audit_module()
    _write(tmp_path / "AGENTS.md", "# Project\n")
    _write(
        tmp_path / ".claude/settings.json",
        '{"permissions":{"allow":["Edit(./**/.env*)","Write(./docker/.env)"]}}\n',
    )

    module._print_report(tmp_path)

    output = capsys.readouterr().out
    assert output.count("共享设置自动放行私有 env 写入") == 2
    assert "建议动作：`delete`" in output


def test_codex_config_audit_reports_cwd_relative_claude_paths(tmp_path, capsys):
    module = _load_audit_module()
    _write(
        tmp_path / ".claude/settings.json",
        '{"permissions":{"deny":["Read(./docker/.env)"]}}\n',
    )

    module._print_report(tmp_path)

    output = capsys.readouterr().out
    assert "Claude `deny` 路径使用 cwd 相对锚点" in output
    assert "从子目录启动时会漂移" in output


def test_codex_config_audit_reports_unanchored_hook_paths(tmp_path, capsys):
    module = _load_audit_module()
    _write(
        tmp_path / ".codex/hooks.json",
        '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":".venv/bin/python .codex/hooks/stop.py"}]}]}}\n',
    )

    module._print_report(tmp_path)

    output = capsys.readouterr().out
    assert "hook 使用未锚定的仓库相对路径" in output
    assert "从子目录启动时会失效" in output


def test_codex_config_audit_accepts_prefix_rules_and_rejects_prose(tmp_path, capsys):
    module = _load_audit_module()
    _write(
        tmp_path / ".codex/rules/valid.rules",
        'prefix_rule(pattern=["git", "status"], decision="allow")\n',
    )
    _write(tmp_path / ".codex/rules/invalid.rules", "这不是执行策略\n")

    module._print_report(tmp_path)

    output = capsys.readouterr().out
    assert "`.codex/rules/valid.rules`" in output
    assert "`P0` `.codex/rules/invalid.rules:1`" in output
    assert ".rules 只允许顶层 `prefix_rule(...)` 命令执行策略" in output


def test_codex_config_audit_reports_non_autonomous_implementation_agent(tmp_path, capsys):
    module = _load_audit_module()
    _write(
        tmp_path / ".claude/agents/project-worker.md",
        "---\nname: project-worker\ntools:\n  - Read\n---\n负责具体开发任务，写文件前等待用户确认。\n",
    )

    module._print_report(tmp_path)

    output = capsys.readouterr().out
    assert "子 Agent 要求每次写文件前等待用户确认" in output
    assert "实现型 Claude subagent 的 tools allowlist 缺少 Edit/Write" in output


def test_codex_config_audit_rejects_invalid_toml_and_hook_json(tmp_path, capsys):
    module = _load_audit_module()
    _write(tmp_path / ".codex/config.toml", "[broken\n")
    _write(tmp_path / ".codex/hooks.json", "{bad json\n")

    module._print_report(tmp_path)

    output = capsys.readouterr().out
    assert "`.codex/config.toml`" in output
    assert "TOML 无法解析" in output
    assert "`.codex/hooks.json:1`" in output
    assert "JSON 无法解析" in output


def test_codex_config_audit_rejects_invalid_command_hook_shape(tmp_path, capsys):
    module = _load_audit_module()
    _write(
        tmp_path / ".codex/hooks.json",
        '{"hooks":{"Stop":[{"hooks":[{"type":"command"},{"type":"command","command":"python3","args":"bad"}]}]}}\n',
    )

    module._print_report(tmp_path)

    output = capsys.readouterr().out
    assert "command hook 必须声明非空 `command`" in output
    assert "command hook 的 `args` 必须是字符串数组" in output


def test_codex_config_audit_includes_reference_surfaces(tmp_path, capsys):
    module = _load_audit_module()
    _write(tmp_path / ".codex/skills/sample/references/details.md", "# Details\n")

    module._print_report(tmp_path)

    assert "`.codex/skills/sample/references/details.md`" in capsys.readouterr().out


def test_test_sync_governance_keeps_targeted_ui_semantic_validation_terms():
    skill = (REPO_ROOT / ".codex/skills/test-sync-governance/SKILL.md").read_text(encoding="utf-8")
    required = (
        "测试选择前置判断",
        "不要在配置、README、docs 或 skill 镜像同步这类低运行时风险改动中默认跑全量",
        "语义负向断言",
        "旧配置入口不存在",
        "pnpm --dir frontend run verify:design-parity",
    )

    for marker in required:
        assert marker in skill


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
