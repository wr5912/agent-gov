from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = REPO_ROOT / ".codex/hooks/codex_governance_stop.py"


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
