from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
INSTALLER = REPO_ROOT / "scripts" / "install_agent_gov_ci_status_relay"
RUNBOOK = REPO_ROOT / "docs/engineering/Multica持续CI与联调环境部署.md"


def test_systemd_service_uses_dedicated_identity_and_credential_file() -> None:
    service = (SYSTEMD_DIR / "agent-gov-ci-status-relay.service").read_text(encoding="utf-8")

    assert "User=agent-gov-ci-relay" in service
    assert "Group=agent-gov-ci-relay" in service
    assert "SupplementaryGroups=" not in service
    assert "docker.service" not in service
    assert "TimeoutStartSec=5min" in service
    assert "WorkingDirectory=/opt/agent-gov-ci-status-relay/current" in service
    assert "LoadCredential=github_token:" in service
    assert "Environment=GITHUB_TOKEN=" not in service
    assert "deploy_agent_gov_to_host" not in service
    assert "ProtectSystem=strict" in service
    assert "ProtectHome=true" in service
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in service
    assert "ReadOnlyPaths=/opt/agent-gov-ci-status-relay" in service
    assert "ReadWritePaths=/var/lib/agent-gov-ci-status-relay/state" in service
    assert "ReadWritePaths=/var/lib/agent-gov-ci-status-relay/.multica" in service
    assert "ReadWritePaths=/var/lib/agent-gov-ci-status-relay/.cache" in service
    assert "ReadWritePaths=/var/lib/agent-gov-ci-status-relay\n" not in service


def test_systemd_timer_polls_serially_every_thirty_seconds() -> None:
    timer = (SYSTEMD_DIR / "agent-gov-ci-status-relay.timer").read_text(encoding="utf-8")

    assert "OnUnitInactiveSec=30s" in timer
    assert "Persistent=true" in timer
    assert "agent-gov-ci-status-relay.service" in timer


def test_relay_environment_example_contains_no_secret_or_deployment_value() -> None:
    example = (SYSTEMD_DIR / "agent-gov-ci-status-relay.env.example").read_text(encoding="utf-8")

    assert "GITHUB_TOKEN" not in example
    assert "GH_TOKEN" not in example
    assert "github" + "_pat_" not in example
    assert "AGENT_GOV_WORKFLOW_FILE=.github/workflows/governance.yml" in example
    assert "DEPLOY_" not in example
    assert "SSH_" not in example


def test_installer_never_accepts_token_in_argv() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")

    assert "--token" not in installer
    assert "/etc/agent-gov-ci-status-relay/github_token" in installer
    assert "useradd --system" in installer
    assert "usermod" not in installer
    assert 'usermod -aG docker "$SERVICE_USER"' not in installer
    assert 'gpasswd -d "$LEGACY_SERVICE_USER" docker' in installer
    assert "SSH_PRIVATE_KEY" not in installer
    assert ".ssh" not in installer
    assert "git clone" not in installer
    assert "git pull" not in installer
    assert 'SOURCE_SHA=$(git -c "safe.directory=$ROOT_DIR" -C "$ROOT_DIR" rev-parse --verify HEAD)' in installer
    assert '-C "$ROOT_DIR" archive "$SOURCE_SHA"' in installer
    assert 'VERSIONS_ROOT="/opt/agent-gov-ci-status-relay/versions"' in installer
    assert 'CURRENT_LINK="/opt/agent-gov-ci-status-relay/current"' in installer
    assert "rollback_upgrade" in installer
    assert "trap rollback_upgrade ERR" in installer
    assert 'mv -Tf "$rollback_link" "$CURRENT_LINK"' in installer
    assert '"$CONFIG_BACKUP" "$PROFILE_CONFIG"' in installer
    assert "systemctl start agent-gov-ci-status-relay.service || true" in installer
    stop_timer = installer.index("systemctl stop agent-gov-ci-status-relay.timer")
    switch_current = installer.index('mv -Tf "$temporary_link" "$CURRENT_LINK"')
    assert stop_timer < switch_current
    assert "github_token must not be a symbolic link" in installer
    assert "AGENT_GOV_RELAY_NOT_BEFORE=" in installer
    assert "date -u +%Y-%m-%dT%H:%M:%SZ" in installer
    assert 'if [[ "$multica_binary" != "/usr/local/bin/multica" ]]' in installer
    assert "disable --now agent-gov-release-controller.timer" in installer
    service_start = installer.rindex("systemctl start agent-gov-ci-status-relay.service")
    timer_enable = installer.rindex("systemctl enable --now agent-gov-ci-status-relay.timer")
    assert service_start < timer_enable


def test_installer_retires_legacy_privileges_only_after_first_relay_success() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")

    legacy_stop = installer.rindex("systemctl disable --now agent-gov-release-controller.timer")
    relay_start = installer.rindex("systemctl start agent-gov-ci-status-relay.service")
    timer_enable = installer.rindex("systemctl enable --now agent-gov-ci-status-relay.timer")
    rollback_disabled = installer.rindex("trap - ERR")
    retirement_call = installer.rindex("\nretire_legacy_release_controller\n")

    assert legacy_stop < relay_start < timer_enable < rollback_disabled < retirement_call
    assert "credentials, Docker-group identity and files remain intact" in installer
    retirement_body = installer.split("retire_legacy_release_controller() {", 1)[1].split(
        "\n}\n\ntrap rollback_upgrade ERR",
        1,
    )[0]
    assert retirement_body.index("preserve_legacy_audit_state") < retirement_body.index('rm -rf "$LEGACY_CONFIG_ROOT" "$LEGACY_STATE_ROOT"')
    assert 'userdel "$LEGACY_SERVICE_USER"' in retirement_body
    assert "/usr/local/bin/releasectl" in retirement_body
    assert "ACTION REQUIRED: revoke the retired release-controller PAT" in installer


def test_legacy_audit_snapshot_excludes_free_text_payloads_and_secrets(
    tmp_path: Path,
) -> None:
    source = tmp_path / "state.db"
    output = tmp_path / "audit.json"
    secret = "SECRET-SENTINEL-MUST-NOT-SURVIVE"
    with sqlite3.connect(source) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE releases (
                commit_sha TEXT PRIMARY KEY,
                pr_number INTEGER,
                aid_identifiers TEXT,
                status TEXT NOT NULL,
                reason TEXT,
                workflow_url TEXT,
                workflow_run_id INTEGER,
                release_id TEXT,
                discovered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE commit_links (
                commit_sha TEXT PRIMARY KEY,
                pr_number INTEGER NOT NULL,
                aid_identifier TEXT NOT NULL,
                merged_by TEXT NOT NULL,
                resolved_at TEXT NOT NULL
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY,
                commit_sha TEXT,
                event_type TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE outbox (
                id INTEGER PRIMARY KEY,
                dedupe_key TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO metadata VALUES (?, ?)",
            ("cursor:master", "a" * 40),
        )
        connection.execute(
            "INSERT INTO metadata VALUES (?, ?)",
            ("untrusted", secret),
        )
        connection.execute(
            "INSERT INTO releases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "a" * 40,
                42,
                '["AID-16"]',
                "succeeded",
                secret,
                "https://github.example/actions/runs/1",
                1,
                "staging-aaaaaaaaaaaa",
                "2026-07-16T00:00:00+00:00",
                "2026-07-16T00:01:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO commit_links VALUES (?, ?, ?, ?, ?)",
            (
                "a" * 40,
                42,
                "AID-16",
                "reviewer",
                "2026-07-16T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
            (1, "a" * 40, "deployment_succeeded", secret, "2026-07-16T00:01:00+00:00"),
        )
        connection.execute(
            "INSERT INTO outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                "comment:aaaaaaaa:AID-16:succeeded",
                "multica_comment",
                secret,
                "delivered",
                1,
                secret,
                "2026-07-16T00:01:00+00:00",
                "2026-07-16T00:02:00+00:00",
            ),
        )

    result = subprocess.run(
        [
            "python3",
            str(REPO_ROOT / "scripts/snapshot_legacy_release_controller_audit.py"),
            "--source",
            str(source),
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    raw = output.read_text(encoding="utf-8")
    snapshot = json.loads(raw)
    assert secret not in raw
    assert snapshot["metadata"] == [{"key": "cursor:master", "value": "a" * 40}]
    assert snapshot["releases"][0]["status"] == "succeeded"
    assert "reason" not in snapshot["releases"][0]
    assert "details" not in snapshot["events"][0]
    assert "payload" not in snapshot["outbox"][0]
    assert "last_error" not in snapshot["outbox"][0]
    validation = subprocess.run(
        [
            "python3",
            str(REPO_ROOT / "scripts/snapshot_legacy_release_controller_audit.py"),
            "--validate",
            str(output),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert validation.returncode == 0, validation.stderr

    snapshot["releases"][0]["reason"] = secret
    output.write_text(json.dumps(snapshot), encoding="utf-8")
    rejected = subprocess.run(
        [
            "python3",
            str(REPO_ROOT / "scripts/snapshot_legacy_release_controller_audit.py"),
            "--validate",
            str(output),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "allowlisted audit schema" in rejected.stderr


def test_partial_retirement_rerun_never_overwrites_the_first_audit_snapshot(
    tmp_path: Path,
) -> None:
    output = tmp_path / "audit.json"
    original = (
        json.dumps(
            {
                "schema_version": 1,
                "retired_at": "2026-07-16T00:00:00+00:00",
                "source_state_present": True,
                "metadata": [{"key": "cursor:master", "value": "a" * 40}],
                "releases": [],
                "commit_links": [],
                "events": [],
                "outbox": [],
            },
            sort_keys=True,
        )
        + "\n"
    )
    output.write_text(original, encoding="utf-8")

    result = subprocess.run(
        [
            "python3",
            str(REPO_ROOT / "scripts/snapshot_legacy_release_controller_audit.py"),
            "--source",
            str(tmp_path / "already-removed-state.db"),
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert output.read_text(encoding="utf-8") == original
    installer = INSTALLER.read_text(encoding="utf-8")
    preserve_body = installer.split("preserve_legacy_audit_state() {", 1)[1].split(
        "\n}\n\nretire_legacy_release_controller",
        1,
    )[0]
    assert '[[ -e "$LEGACY_AUDIT_FILE" || -L "$LEGACY_AUDIT_FILE" ]]' in preserve_body
    assert 'ln "$audit_tmp" "$LEGACY_AUDIT_FILE"' in preserve_body
    assert 'mv -f "$audit_tmp" "$LEGACY_AUDIT_FILE"' not in preserve_body
    assert "validate_legacy_audit_state" in preserve_body
    assert '--validate "$LEGACY_AUDIT_FILE"' in installer
    assert '[[ -L "$LEGACY_STATE_ROOT" ]]' in installer
    assert '[[ -L "$LEGACY_CONFIG_ROOT" ]]' in installer


def test_runbook_documents_replay_and_safe_legacy_retirement() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert "下一轮必须先重试尚未 resolved" in runbook
    assert "不另造一份 CI 状态" in runbook
    assert "依赖恢复后恰好投递一次" in runbook
    assert "在新 relay 首次 one-shot 成功之前，不删除旧 PAT 本机副本" in runbook
    assert "/var/lib/agent-gov-release-controller-audit/release-controller-audit.json" in runbook
    assert "安装器只能删除本机" in runbook
    assert "同一 SHA 和证据重跑唯一部署入口" in runbook


def test_relay_installer_is_executable_and_has_valid_syntax() -> None:
    assert os.access(INSTALLER, os.X_OK)
    result = subprocess.run(
        ["bash", "-n", str(INSTALLER)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
