from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = REPO_ROOT / ".codex/hooks/codex_governance_stop.py"
HOOK_CONFIG = REPO_ROOT / ".codex/hooks.json"


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("codex_governance_stop", HOOK_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_stop_hook_first_failure_requests_one_continuation(monkeypatch, capsys):
    module = _load_hook_module()
    monkeypatch.setattr(
        module,
        "GOVERNANCE_COMMANDS",
        (("failing", [sys.executable, "-c", "import sys; print('failed'); sys.exit(1)"]),),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"stop_hook_active": false}'))

    assert module.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "block"
    assert "[failing]\nfailed" in payload["reason"]


def test_stop_hook_repeated_failure_warns_without_continuation_loop(monkeypatch, capsys):
    module = _load_hook_module()
    monkeypatch.setattr(
        module,
        "GOVERNANCE_COMMANDS",
        (("failing", [sys.executable, "-c", "import sys; print('failed'); sys.exit(1)"]),),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"stop_hook_active": true}'))

    assert module.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert "decision" not in payload
    assert "[failing]\nfailed" in payload["systemMessage"]


def test_stop_hook_success_is_silent(monkeypatch, capsys):
    module = _load_hook_module()
    monkeypatch.setattr(
        module,
        "GOVERNANCE_COMMANDS",
        (("passing", [sys.executable, "-c", "raise SystemExit(0)"]),),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    assert module.main() == 0
    assert capsys.readouterr().out == ""


def test_stop_hook_command_resolves_nearest_project_from_monorepo_subdirectory(tmp_path: Path) -> None:
    outer_root = tmp_path / "outer"
    project_root = outer_root / "ai" / "agent-gov"
    session_cwd = project_root / "app" / "runtime"
    hook_script = project_root / ".codex" / "hooks" / "codex_governance_stop.py"
    hook_script.parent.mkdir(parents=True)
    session_cwd.mkdir(parents=True)
    hook_script.write_text("import sys\nprint(sys.stdin.read())\n", encoding="utf-8")
    subprocess.run(["git", "init", str(outer_root)], check=True, capture_output=True)

    config = json.loads(HOOK_CONFIG.read_text(encoding="utf-8"))
    command = config["hooks"]["Stop"][0]["hooks"][0]["command"]
    hook_input = '{"hook_event_name":"Stop","cwd":"nested"}'

    result = subprocess.run(
        command,
        cwd=session_cwd,
        input=hook_input,
        shell=True,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == hook_input
