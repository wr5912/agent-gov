from __future__ import annotations

import importlib.util
import json
import shutil
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


def _run_hook(script: Path, cwd: Path, hook_input: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=cwd,
        input=hook_input,
        check=False,
        capture_output=True,
        text=True,
    )


def _standalone_hook_without_project_commands(tmp_path: Path) -> Path:
    copied = tmp_path / ".codex/hooks/codex_governance_stop.py"
    copied.parent.mkdir(parents=True)
    shutil.copy2(HOOK_SCRIPT, copied)
    return copied


def test_stop_hook_first_real_command_failure_requests_one_continuation(tmp_path: Path) -> None:
    copied = _standalone_hook_without_project_commands(tmp_path)

    result = _run_hook(copied, tmp_path, '{"stop_hook_active": false}')

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    assert "[agent configuration]" in payload["reason"]
    assert "No such file or directory" in payload["reason"] or "can't open file" in payload["reason"]


def test_stop_hook_repeated_real_command_failure_warns_without_continuation_loop(tmp_path: Path) -> None:
    copied = _standalone_hook_without_project_commands(tmp_path)

    result = _run_hook(copied, tmp_path, '{"stop_hook_active": true}')

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "decision" not in payload
    assert "[agent configuration]" in payload["systemMessage"]


def test_stop_hook_runs_the_current_real_governance_commands() -> None:
    module = _load_hook_module()
    labels = {label for label, _command in module.GOVERNANCE_COMMANDS}
    assert {"codex governance", "test quality policy", "docs governance"}.issubset(labels)

    result = _run_hook(HOOK_SCRIPT, REPO_ROOT, "{}")

    assert result.returncode == 0, result.stderr
    if result.stdout:
        payload = json.loads(result.stdout)
        assert payload["decision"] == "block"
        assert any(f"[{label}]" in payload["reason"] for label, _command in module.GOVERNANCE_COMMANDS)


def test_stop_hook_command_resolves_nearest_project_from_monorepo_subdirectory(tmp_path: Path) -> None:
    outer_root = tmp_path / "outer"
    project_root = outer_root / "ai" / "agent-gov"
    session_cwd = project_root / "app" / "runtime"
    hook_script = project_root / ".codex" / "hooks" / "codex_governance_stop.py"
    hook_script.parent.mkdir(parents=True)
    session_cwd.mkdir(parents=True)
    shutil.copy2(HOOK_SCRIPT, hook_script)
    subprocess.run(["git", "init", str(outer_root)], check=True, capture_output=True)

    config = json.loads(HOOK_CONFIG.read_text(encoding="utf-8"))
    command = config["hooks"]["Stop"][0]["hooks"][0]["command"]
    hook_input = '{"hook_event_name":"Stop","stop_hook_active":true}'

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
    payload = json.loads(result.stdout)
    assert "decision" not in payload
    assert "[agent configuration]" in payload["systemMessage"]
