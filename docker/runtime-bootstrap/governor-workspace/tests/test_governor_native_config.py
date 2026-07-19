from __future__ import annotations

import json
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]


def test_governor_is_read_only_and_cannot_bypass_permissions() -> None:
    policy = json.loads((WORKSPACE / ".claude" / "settings.json").read_text(encoding="utf-8"))
    permissions = policy["permissions"]
    assert permissions["defaultMode"] == "default"
    assert permissions["disableBypassPermissionsMode"] == "disable"
    assert permissions["ask"] == []
    assert {"Write(/**)", "Edit(/**)", "Bash(*)"} <= set(permissions["deny"])
    assert policy["sandbox"]["enabled"] is True
    assert policy["sandbox"]["failIfUnavailable"] is True


def test_governor_manifest_does_not_declare_business_registry_identity() -> None:
    text = (WORKSPACE / "agent.yaml").read_text(encoding="utf-8")
    assert "\n  id:" not in text
