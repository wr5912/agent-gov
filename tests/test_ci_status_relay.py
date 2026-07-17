from __future__ import annotations

from pathlib import Path

import pytest
from tests.ci_status_relay_support import (
    SCRIPTS_DIR,
    FakeGitHub,
    _config,
    _responses,
    multica,
    relay,
)


def test_outbox_retries_multica_failure_and_recovers_from_existing_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store = relay.OutboxStore(tmp_path / "outbox.sqlite3")
    store.enqueue(
        "github-run:wr5912/agent-gov:501:1:success",
        {
            "aid": "AID-16",
            "marker": "agent-gov-ci:wr5912/agent-gov:501:1:success",
            "content": "terminal result",
        },
    )

    def fail_delivery(*_args: object, **_kwargs: object) -> bool:
        raise multica.MulticaError("temporary outage")

    monkeypatch.setattr(relay, "deliver_comment", fail_delivery)
    assert relay.flush_outbox(config, store) == (0, 1)
    pending = store.pending()
    assert len(pending) == 1
    assert pending[0]["status"] == "pending"
    assert pending[0]["attempts"] == 1
    assert "temporary outage" in str(pending[0]["last_error"])

    delivered: list[tuple[str, str]] = []

    def marker_exists(
        selected: multica.MulticaConfig,
        *,
        aid: str,
        marker: str,
        content: str,
    ) -> bool:
        assert selected.profile == "ci-status-relay"
        assert content == "terminal result"
        delivered.append((aid, marker))
        return False

    monkeypatch.setattr(relay, "deliver_comment", marker_exists)
    assert relay.flush_outbox(config, store) == (1, 0)
    assert store.snapshot() == {
        "pending": 0,
        "delivered": 1,
        "pending_items": [],
        "discovery_failures": 0,
        "failure_items": [],
        "watermarks": [],
    }
    assert delivered == [("AID-16", "agent-gov-ci:wr5912/agent-gov:501:1:success")]
    store.close()


def test_one_poll_does_not_retry_the_same_multica_failure_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store = relay.OutboxStore(config.state_dir / "outbox.sqlite3")
    store.enqueue(
        "github-run:wr5912/agent-gov:550:1:success",
        {
            "aid": "AID-16",
            "marker": "agent-gov-ci:wr5912/agent-gov:550:1:success",
            "content": "terminal result",
        },
    )
    store.close()
    attempts = 0

    def fail_delivery(*_args: object, **_kwargs: object) -> bool:
        nonlocal attempts
        attempts += 1
        raise multica.MulticaError("temporary outage")

    monkeypatch.setattr(relay, "deliver_comment", fail_delivery)
    github = FakeGitHub(_responses(config, pull_runs=[], push_runs=[]))

    summary = relay.run_poll(config, github)

    assert attempts == 1
    assert summary["delivery_pending"] == 1
    assert summary["outbox"]["pending"] == 1


def test_github_transport_failure_is_persisted_before_poll_fails(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    class OfflineGitHub:
        def get(self, path: str) -> object:
            raise relay.GitHubTransportError(f"offline while reading {path}")

    with pytest.raises(relay.GitHubTransportError, match="offline"):
        relay.run_poll(config, OfflineGitHub())

    store = relay.OutboxStore(config.state_dir / "outbox.sqlite3")
    try:
        failures = store.failure_evidence()
    finally:
        store.close()

    assert len(failures) == 1
    assert failures[0]["category"] == "github_transport"
    assert "pull_request" in failures[0]["failure_key"]


def test_poll_command_returns_nonzero_when_stream_payload_is_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    responses = _responses(config, pull_runs=[], push_runs=[])
    responses[relay.workflow_runs_path(config, "pull_request", page=1)] = {"workflow_runs": "invalid"}
    monkeypatch.setattr(
        relay.RelayConfig,
        "from_environment",
        classmethod(lambda cls: config),
    )
    monkeypatch.setattr(relay, "load_github_token", lambda: "test-token")
    monkeypatch.setattr(
        relay,
        "GitHubClient",
        lambda **_kwargs: FakeGitHub(responses),
    )

    assert relay.main(["poll"]) == 1
    store = relay.OutboxStore(config.state_dir / "outbox.sqlite3")
    try:
        failures = store.failure_evidence()
    finally:
        store.close()
    assert failures[0]["category"] == "github_payload"


def test_multica_marker_check_avoids_duplicate_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "agent-gov-ci:wr5912/agent-gov:601:1:success"
    monkeypatch.setattr(
        multica,
        "run_multica_json",
        lambda *_args, **_kwargs: [{"content": f"<!-- {marker} -->"}],
    )

    def unexpected_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("comment add must not run when marker already exists")

    monkeypatch.setattr(multica.subprocess, "run", unexpected_run)
    assert (
        multica.deliver_comment(
            multica.MulticaConfig(profile="ci-status-relay"),
            aid="AID-16",
            marker=marker,
            content="duplicate",
        )
        is False
    )


def test_multica_subprocess_environment_never_receives_github_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "not-forwarded")
    monkeypatch.setenv("GH_TOKEN", "not-forwarded")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/not-forwarded")
    monkeypatch.setenv("MULTICA_SERVER_URL", "https://multica.test")

    environment = multica.sanitized_environment()

    assert "GITHUB_TOKEN" not in environment
    assert "GH_TOKEN" not in environment
    assert "CREDENTIALS_DIRECTORY" not in environment
    assert environment["MULTICA_SERVER_URL"] == "https://multica.test"


def test_status_does_not_create_state_when_relay_has_never_run(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert relay.status(config) == {
        "pending": 0,
        "delivered": 0,
        "pending_items": [],
        "discovery_failures": 0,
        "failure_items": [],
        "watermarks": [],
    }
    assert not config.state_dir.exists()


def test_load_github_token_prefers_systemd_credential_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "github_token").write_text("credential-token\n", encoding="utf-8")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    monkeypatch.setenv("GITHUB_TOKEN", "environment-token")

    assert relay.load_github_token() == "credential-token"


def test_relay_config_rejects_non_integer_run_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_GOV_RELAY_RUN_LIMIT", "many")

    with pytest.raises(relay.RelayError, match="must be an integer"):
        relay.RelayConfig.from_environment()


def test_relay_config_requires_https_github_api_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_API_URL", "http://api.github.test")

    with pytest.raises(relay.RelayError, match="must be an HTTPS URL"):
        relay.RelayConfig.from_environment()


def test_relay_script_keeps_terminal_scope_and_no_deployment_authority() -> None:
    text = (SCRIPTS_DIR / "agent_gov_ci_status_relay.py").read_text(encoding="utf-8")

    assert '"status": "completed"' in text
    assert '"pull_request", "push"' in text
    assert "deploy_agent_gov_to_host" not in text
    assert "subprocess" not in text
    assert "SSH" not in text
    assert "docker" not in text.lower()
