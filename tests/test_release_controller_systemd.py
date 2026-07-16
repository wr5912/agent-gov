from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"


def test_systemd_service_uses_dedicated_identity_and_credential_file() -> None:
    service = (SYSTEMD_DIR / "agent-gov-release-controller.service").read_text(encoding="utf-8")

    assert "User=agent-gov-release" in service
    assert "Group=agent-gov-release" in service
    assert "SupplementaryGroups=docker" in service
    assert "TimeoutStartSec=90min" in service
    assert "WorkingDirectory=/var/lib/agent-gov-release-controller/repository" in service
    assert "LoadCredential=github_token:" in service
    assert "Environment=GITHUB_TOKEN=" not in service
    assert "ProtectSystem=strict" in service
    assert "ProtectHome=true" in service
    assert "ReadWritePaths=/var/lib/agent-gov-release-controller" in service


def test_systemd_timer_polls_serially_every_thirty_seconds() -> None:
    timer = (SYSTEMD_DIR / "agent-gov-release-controller.timer").read_text(encoding="utf-8")

    assert "OnUnitInactiveSec=30s" in timer
    assert "Persistent=true" in timer
    assert "agent-gov-release-controller.service" in timer


def test_controller_environment_example_contains_no_secret_value() -> None:
    example = (SYSTEMD_DIR / "agent-gov-release-controller.env.example").read_text(
        encoding="utf-8"
    )

    assert "GITHUB_TOKEN" not in example
    assert "GH_TOKEN" not in example
    assert "github" + "_pat_" not in example
    assert "AGENT_GOV_ENVIRONMENT=staging-232" in example
    assert "AGENT_GOV_DEPLOY_HOST=172.16.112.232" in example
    assert "AGENT_GOV_RELEASE_SRE_AGENT=release-sre" in example
    assert "AGENT_GOV_WORKFLOW_FILE=.github/workflows/governance.yml" in example
    assert "AGENT_GOV_ALLOWED_MERGERS=" in example
    assert "AGENT_GOV_REQUIRE_BRANCH_PROTECTION=true" in example
    assert (
        "AGENT_GOV_SOURCE_REPO_DIR="
        "/var/lib/agent-gov-release-controller/repository"
    ) in example


def test_installer_never_accepts_token_in_argv() -> None:
    installer = (REPO_ROOT / "scripts" / "install_agent_gov_release_controller").read_text(
        encoding="utf-8"
    )

    assert "--token" not in installer
    assert "/etc/agent-gov-release-controller/github_token" in installer
    assert "useradd --system" in installer
    assert "usermod -aG docker" in installer
    assert "git clone --branch master --single-branch" in installer
    assert "github_token must not be a symbolic link" in installer
    service_start = installer.index("systemctl start agent-gov-release-controller.service")
    timer_enable = installer.index("systemctl enable --now agent-gov-release-controller.timer")
    assert service_start < timer_enable
