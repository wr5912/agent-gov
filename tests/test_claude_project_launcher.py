from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "scripts/run_claude.py"


def _load_launcher_module():
    spec = importlib.util.spec_from_file_location("run_claude", LAUNCHER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_launcher_changes_to_repo_root_before_exec(monkeypatch):
    module = _load_launcher_module()
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(module.os, "chdir", lambda path: calls.append(("chdir", path)))
    monkeypatch.setattr(module.os, "execvp", lambda executable, argv: calls.append(("execvp", (executable, argv))))

    assert module.main(["--version"]) == 0
    assert calls == [
        ("chdir", REPO_ROOT),
        ("execvp", ("claude", ["claude", "--version"])),
    ]
