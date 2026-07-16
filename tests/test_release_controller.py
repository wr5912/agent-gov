from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import agent_gov_release_controller as controller  # noqa: E402
import agent_gov_release_delivery as delivery  # noqa: E402
from agent_gov_release_state import (  # noqa: E402
    ControllerConfig,
    ControllerError,
    ReleaseStatus,
    StateStore,
    controller_lock,
    load_github_token,
)

SHA_CURSOR = "a" * 40
SHA_FIRST = "b" * 40
SHA_HEAD = "c" * 40


class FakeGitHub:
    def __init__(
        self,
        responses: dict[str, Any | Callable[[str], Any]],
    ) -> None:
        self.responses = responses
        self.requested: list[str] = []

    def get(self, path: str) -> Any:
        self.requested.append(path)
        if path not in self.responses:
            raise AssertionError(f"unexpected GitHub GET: {path}")
        response = self.responses[path]
        return response(path) if callable(response) else response


def config_for(tmp_path: Path, *, require_branch_protection: bool = True) -> ControllerConfig:
    return ControllerConfig(
        repository="wr5912/agent-gov",
        branch="master",
        environment="staging-232",
        deploy_host="172.16.112.232",
        deploy_user="root",
        remote_dir="~/work/agent-gov",
        state_dir=tmp_path / "state",
        deploy_script=SCRIPTS_DIR / "deploy_agent_gov_to_host",
        github_api_url="https://api.github.test",
        multica_profile="release-controller",
        quality_check="quality-gate",
        workflow_file=".github/workflows/governance.yml",
        allowed_mergers=("trusted-merger",),
        release_sre_agent="release-sre",
        release_sre_metadata_key="release_sre_issue_id",
        require_branch_protection=require_branch_protection,
        ci_timeout_seconds=7200,
    )


def store_for(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.db")


def valid_repository_policy() -> dict[str, Any]:
    return {
        "repository": {
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": False,
        },
        "protection": {
            "required_status_checks": {
                "strict": True,
                "contexts": ["quality-gate"],
                "checks": [],
            },
            "required_pull_request_reviews": {},
            "enforce_admins": {"enabled": True},
            "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": False},
        },
    }


def merged_pull(
    commit_sha: str,
    *,
    number: int,
    aid: str,
    merger: str = "trusted-merger",
) -> dict[str, Any]:
    return {
        "number": number,
        "merged_at": "2026-07-15T00:00:00Z",
        "merge_commit_sha": commit_sha,
        "merged_by": {"login": merger},
        "base": {"ref": "master"},
        "head": {"ref": f"{aid.lower()}-delivery"},
        "title": f"{aid}: controlled release",
        "body": f"Tracks {aid}",
    }


def workflow_runs_path(commit_sha: str) -> str:
    return (
        "/repos/wr5912/agent-gov/actions/workflows/governance.yml/runs"
        f"?branch=master&event=push&head_sha={commit_sha}&per_page=20"
    )


def successful_workflow(commit_sha: str, *, run_id: int = 501) -> dict[str, Any]:
    return {
        "workflow_runs": [
            {
                "id": run_id,
                "run_attempt": 1,
                "path": ".github/workflows/governance.yml",
                "event": "push",
                "head_branch": "master",
                "head_sha": commit_sha,
                "status": "completed",
                "conclusion": "success",
                "html_url": f"https://github.test/actions/runs/{run_id}",
            }
        ]
    }


def linked_waiting_release(store: StateStore, commit_sha: str = SHA_HEAD) -> None:
    store.discover(commit_sha)
    store.set_linkage(
        commit_sha,
        pr_number=27,
        aid_identifiers=["AID-27"],
        release_id=f"staging-232-{commit_sha[:12]}",
    )
    store.transition(commit_sha, ReleaseStatus.WAITING_CI)


def test_first_poll_initializes_cursor_without_a_historical_release(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    store = store_for(tmp_path)
    github = FakeGitHub(
        {"/repos/wr5912/agent-gov/branches/master": {"commit": {"sha": SHA_HEAD}}}
    )
    try:
        controller.reconcile_head(config, github, store)

        assert store.get_metadata("cursor:master") == SHA_HEAD
        assert store.snapshot()["releases"] == []
        assert store.snapshot()["events"][0]["event_type"] == "cursor_initialized"
    finally:
        store.close()


def test_repository_policy_accepts_only_strict_squash_protection(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    policy = valid_repository_policy()
    github = FakeGitHub(
        {
            "/repos/wr5912/agent-gov": policy["repository"],
            "/repos/wr5912/agent-gov/branches/master/protection": policy["protection"],
        }
    )

    controller.validate_repository_policy(config, github)

    assert github.requested == [
        "/repos/wr5912/agent-gov",
        "/repos/wr5912/agent-gov/branches/master/protection",
    ]


@pytest.mark.parametrize(
    ("section", "key", "value", "message"),
    [
        ("repository", "allow_squash_merge", False, "requires squash merge"),
        ("repository", "allow_merge_commit", True, "must be disabled"),
        ("protection", "required_status_checks", {"strict": False}, "strict status"),
        ("protection", "required_pull_request_reviews", None, "require pull requests"),
        ("protection", "enforce_admins", {"enabled": False}, "administrators"),
        ("protection", "allow_force_pushes", {"enabled": True}, "force pushes"),
        ("protection", "allow_deletions", {"enabled": True}, "deletion"),
    ],
)
def test_repository_policy_fails_closed(
    tmp_path: Path,
    section: str,
    key: str,
    value: object,
    message: str,
) -> None:
    config = config_for(tmp_path)
    policy = valid_repository_policy()
    policy[section][key] = value
    github = FakeGitHub(
        {
            "/repos/wr5912/agent-gov": policy["repository"],
            "/repos/wr5912/agent-gov/branches/master/protection": policy["protection"],
        }
    )

    with pytest.raises(ControllerError, match=message):
        controller.validate_repository_policy(config, github)


def test_every_lineage_commit_requires_one_squash_pr_aid_and_allowed_merger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_for(tmp_path)
    store = store_for(tmp_path)
    store.set_metadata("cursor:master", SHA_CURSOR)
    linked_waiting_release(store, "d" * 40)
    github = FakeGitHub(
        {
            "/repos/wr5912/agent-gov/branches/master": {
                "commit": {"sha": SHA_HEAD}
            },
            f"/repos/wr5912/agent-gov/compare/{SHA_CURSOR}...{SHA_HEAD}": {
                "status": "ahead",
                "total_commits": 2,
                "commits": [{"sha": SHA_FIRST}, {"sha": SHA_HEAD}],
            },
            f"/repos/wr5912/agent-gov/commits/{SHA_FIRST}/pulls": [
                merged_pull(SHA_FIRST, number=16, aid="AID-16")
            ],
            f"/repos/wr5912/agent-gov/commits/{SHA_HEAD}/pulls": [
                merged_pull(SHA_HEAD, number=27, aid="AID-27")
            ],
            workflow_runs_path(SHA_HEAD): successful_workflow(SHA_HEAD),
            "/repos/wr5912/agent-gov/actions/runs/501/jobs?per_page=100": {
                "jobs": [
                    {"name": "governance-static", "conclusion": "success"},
                    {"name": "quality-gate", "conclusion": "success"},
                ]
            },
        }
    )
    deployed: list[str] = []
    monkeypatch.setattr(
        controller,
        "execute_release",
        lambda _config, _store, row: deployed.append(str(row["commit_sha"])),
    )
    try:
        controller.reconcile_head(config, github, store)

        head = store.get_release(SHA_HEAD)
        old = store.get_release("d" * 40)
        assert head is not None and old is not None
        assert json.loads(head["aid_identifiers"]) == ["AID-16", "AID-27"]
        assert head["pr_number"] == 27
        assert old["status"] == ReleaseStatus.SUPERSEDED
        assert deployed == [SHA_HEAD]
        assert f"/repos/wr5912/agent-gov/commits/{SHA_FIRST}/pulls" in github.requested
        assert f"/repos/wr5912/agent-gov/commits/{SHA_HEAD}/pulls" in github.requested
    finally:
        store.close()


@pytest.mark.parametrize(
    ("pulls", "message"),
    [
        ([], "exactly one merged PR"),
        (
            [
                merged_pull(SHA_HEAD, number=27, aid="AID-27"),
                merged_pull(SHA_HEAD, number=28, aid="AID-28"),
            ],
            "exactly one merged PR",
        ),
        (
            [
                {
                    **merged_pull(SHA_HEAD, number=27, aid="AID-27"),
                    "head": {"ref": "feature/no-aid"},
                    "title": "missing trace",
                    "body": "",
                }
            ],
            "exactly one AID",
        ),
        (
            [merged_pull(SHA_HEAD, number=27, aid="AID-27", merger="intruder")],
            "unauthorized login",
        ),
        (
            [
                {
                    **merged_pull(SHA_HEAD, number=27, aid="AID-27"),
                    "merge_commit_sha": SHA_FIRST,
                }
            ],
            "exactly one merged PR",
        ),
    ],
)
def test_invalid_lineage_is_quarantined(
    tmp_path: Path,
    pulls: list[dict[str, Any]],
    message: str,
) -> None:
    config = config_for(tmp_path)
    store = store_for(tmp_path)
    store.set_metadata("cursor:master", SHA_CURSOR)
    github = FakeGitHub(
        {
            "/repos/wr5912/agent-gov/branches/master": {
                "commit": {"sha": SHA_HEAD}
            },
            f"/repos/wr5912/agent-gov/compare/{SHA_CURSOR}...{SHA_HEAD}": {
                "status": "ahead",
                "total_commits": 1,
                "commits": [{"sha": SHA_HEAD}],
            },
            f"/repos/wr5912/agent-gov/commits/{SHA_HEAD}/pulls": pulls,
        }
    )
    try:
        with pytest.raises(ControllerError, match=message):
            controller.reconcile_head(config, github, store)

        row = store.get_release(SHA_HEAD)
        assert row is not None
        assert row["status"] == ReleaseStatus.QUARANTINED
        assert store.get_metadata("cursor:master") == SHA_CURSOR
    finally:
        store.close()


def test_quality_gate_matches_exact_push_workflow_sha_path_branch_and_job(
    tmp_path: Path,
) -> None:
    config = config_for(tmp_path)
    exact = successful_workflow(SHA_HEAD, run_id=503)["workflow_runs"][0]
    github = FakeGitHub(
        {
            workflow_runs_path(SHA_HEAD): {
                "workflow_runs": [
                    {**exact, "id": 900, "event": "pull_request"},
                    {**exact, "id": 901, "path": ".github/workflows/other.yml"},
                    {**exact, "id": 902, "head_branch": "feature"},
                    {**exact, "id": 903, "head_sha": SHA_FIRST},
                    exact,
                ]
            },
            "/repos/wr5912/agent-gov/actions/runs/503/jobs?per_page=100": {
                "jobs": [
                    {"name": "backend-main-full", "conclusion": "success"},
                    {"name": "quality-gate", "conclusion": "success"},
                ]
            },
        }
    )

    gate = controller.quality_gate(config, github, SHA_HEAD)

    assert gate.complete is True
    assert gate.successful is True
    assert gate.run_id == 503
    assert gate.conclusion == "success"
    assert github.requested == [
        workflow_runs_path(SHA_HEAD),
        "/repos/wr5912/agent-gov/actions/runs/503/jobs?per_page=100",
    ]


def test_quality_gate_rejects_an_ambiguous_named_job(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    github = FakeGitHub(
        {
            workflow_runs_path(SHA_HEAD): successful_workflow(SHA_HEAD),
            "/repos/wr5912/agent-gov/actions/runs/501/jobs?per_page=100": {
                "jobs": [
                    {"name": "quality-gate", "conclusion": "success"},
                    {"name": "quality-gate", "conclusion": "success"},
                ]
            },
        }
    )

    gate = controller.quality_gate(config, github, SHA_HEAD)

    assert gate.complete is True
    assert gate.successful is False
    assert gate.conclusion == "quality-gate-ambiguous"


def test_unknown_deploy_exit_returns_release_to_waiting_ci(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_for(tmp_path)
    store = store_for(tmp_path)
    linked_waiting_release(store)
    monkeypatch.setattr(controller, "run_logged", lambda *_args, **_kwargs: 42)
    try:
        row = store.get_release(SHA_HEAD)
        assert row is not None
        with pytest.raises(ControllerError, match="ambiguous exit code 42"):
            controller.execute_release(config, store, row)

        refreshed = store.get_release(SHA_HEAD)
        assert refreshed is not None
        assert refreshed["status"] == ReleaseStatus.WAITING_CI
        assert store.get_metadata("cursor:master") is None
        assert store.snapshot()["events"][0]["event_type"] == "deployment_ambiguous"
    finally:
        store.close()


def test_success_finalizes_cursor_active_and_outbox_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_for(tmp_path)
    store = store_for(tmp_path)
    linked_waiting_release(store)
    monkeypatch.setattr(controller, "run_logged", lambda *_args, **_kwargs: 0)
    try:
        row = store.get_release(SHA_HEAD)
        assert row is not None
        controller.execute_release(config, store, row)

        refreshed = store.get_release(SHA_HEAD)
        assert refreshed is not None
        assert refreshed["status"] == ReleaseStatus.SUCCEEDED
        assert store.get_metadata("cursor:master") == SHA_HEAD
        assert store.get_metadata("active:staging-232") == refreshed["release_id"]
        assert len(store.pending_outbox()) == 2
    finally:
        store.close()


def test_finalize_release_rolls_back_all_fields_if_outbox_serialization_fails(
    tmp_path: Path,
) -> None:
    store = store_for(tmp_path)
    linked_waiting_release(store)
    store.transition(SHA_HEAD, ReleaseStatus.DEPLOYING)
    try:
        with pytest.raises(TypeError):
            store.finalize_release(
                SHA_HEAD,
                ReleaseStatus.SUCCEEDED,
                reason="healthy",
                metadata={
                    "cursor:master": SHA_HEAD,
                    "active:staging-232": "staging-232-c",
                },
                outbox=[("invalid", "multica_comment", {"bad": object()})],
            )

        refreshed = store.get_release(SHA_HEAD)
        assert refreshed is not None
        assert refreshed["status"] == ReleaseStatus.DEPLOYING
        assert store.get_metadata("cursor:master") is None
        assert store.get_metadata("active:staging-232") is None
        assert store.pending_outbox() == []
    finally:
        store.close()


def test_deploy_subprocess_does_not_inherit_github_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_for(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leak")
    monkeypatch.setenv("GH_TOKEN", "must-not-leak-either")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/example")
    log_path = tmp_path / "deploy.log"
    command = [
        sys.executable,
        "-c",
        "import os; print(os.getenv('GITHUB_TOKEN', 'missing')); "
        "print(os.getenv('GH_TOKEN', 'missing')); "
        "print(os.getenv('CREDENTIALS_DIRECTORY', 'missing'))",
    ]

    assert controller.run_logged(command, log_path, config) == 0
    log_text = log_path.read_text(encoding="utf-8")
    assert log_text.splitlines().count("missing") == 3
    assert "must-not-leak" not in log_text
    assert os.stat(log_path).st_mode & 0o777 == 0o600


def test_outbox_survives_reopen_and_delivers_dependency_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_for(tmp_path)
    database = tmp_path / "state.db"
    store = StateStore(database)
    linked_waiting_release(store)
    store.transition(SHA_HEAD, ReleaseStatus.DEPLOYING)
    store.transition(SHA_HEAD, ReleaseStatus.SUCCEEDED)
    row = store.get_release(SHA_HEAD)
    assert row is not None
    controller.enqueue_release_outbox(config, store, row, "succeeded")
    assert len(store.pending_outbox()) == 2
    store.close()

    delivered: list[tuple[str, str]] = []
    monkeypatch.setattr(
        delivery,
        "deliver_comment",
        lambda _config, aid, _marker, _content: delivered.append(("comment", aid)),
    )
    monkeypatch.setattr(
        delivery,
        "activate_release_sre",
        lambda _config, aid: delivered.append(("activate", aid)),
    )
    reopened = StateStore(database)
    try:
        delivery.flush_outbox(config, reopened)
        delivery.flush_outbox(config, reopened)

        assert delivered == [("comment", "AID-27"), ("activate", "AID-27")]
        assert reopened.pending_outbox() == []
        assert {item["status"] for item in reopened.snapshot()["outbox"]} == {
            "delivered"
        }
    finally:
        reopened.close()


def test_comment_marker_makes_delivery_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_for(tmp_path)
    marker = f"agent-gov-release:{SHA_HEAD}:AID-27:succeeded"
    monkeypatch.setattr(
        delivery,
        "run_multica_json",
        lambda _config, arguments: {
            "comments": [{"content": f"<!-- {marker} -->\nalready delivered"}]
        }
        if arguments == ("issue", "comment", "list", "AID-27")
        else pytest.fail(f"unexpected Multica call: {arguments}"),
    )

    def forbid_subprocess(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an existing marker must prevent a duplicate comment subprocess")

    monkeypatch.setattr(delivery.subprocess, "run", forbid_subprocess)

    delivery.deliver_comment(config, "AID-27", marker, "content")


def test_release_sre_metadata_promotes_backlog_to_todo_without_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_for(tmp_path)
    child_status = "backlog"
    calls: list[tuple[str, ...]] = []

    def fake_multica(_config: ControllerConfig, arguments: tuple[str, ...]) -> object:
        nonlocal child_status
        calls.append(arguments)
        if arguments == ("issue", "get", "AID-27"):
            return {
                "id": "parent-id",
                "identifier": "AID-27",
                "metadata": {"release_sre_issue_id": "AID-28"},
            }
        if arguments == ("issue", "get", "AID-28"):
            return {
                "id": "child-id",
                "identifier": "AID-28",
                "parent_issue_id": "parent-id",
                "assignee_type": "agent",
                "assignee_id": "release-sre-id",
                "status": child_status,
            }
        if arguments == ("agent", "list"):
            return [
                {
                    "id": "release-sre-id",
                    "name": "release-sre",
                    "archived_at": None,
                }
            ]
        if arguments == ("issue", "status", "AID-28", "todo"):
            child_status = "todo"
            return {"status": "todo"}
        raise AssertionError(f"unexpected Multica call: {arguments}")

    monkeypatch.setattr(delivery, "run_multica_json", fake_multica)

    delivery.activate_release_sre(config, "AID-27")
    delivery.activate_release_sre(config, "AID-27")

    assert calls.count(("issue", "status", "AID-28", "todo")) == 1
    assert not any("rerun" in call for call in calls)


def test_restart_recovers_deploying_release_for_idempotent_reconciliation(
    tmp_path: Path,
) -> None:
    store = store_for(tmp_path)
    linked_waiting_release(store)
    try:
        store.transition(SHA_HEAD, ReleaseStatus.DEPLOYING)
        store.recover_incomplete()

        row = store.get_release(SHA_HEAD)
        assert row is not None
        assert row["status"] == ReleaseStatus.WAITING_CI
        assert "reconciliation" in row["reason"]
    finally:
        store.close()


def test_controller_lock_rejects_concurrent_invocation(tmp_path: Path) -> None:
    with controller_lock(tmp_path):
        with pytest.raises(ControllerError, match="another controller invocation"):
            with controller_lock(tmp_path):
                pass


def test_systemd_credential_takes_precedence_over_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "github_token").write_text(
        "credential-token\n", encoding="utf-8"
    )
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credential_directory))
    monkeypatch.setenv("GITHUB_TOKEN", "environment-token")

    assert load_github_token() == "credential-token"
