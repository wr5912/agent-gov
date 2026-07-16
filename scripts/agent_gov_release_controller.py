#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from agent_gov_release_delivery import flush_outbox
from agent_gov_release_state import (
    ControllerConfig,
    ControllerError,
    GitHubClient,
    ReleaseStatus,
    StateStore,
    controller_lock,
    load_github_token,
    utc_now,
)
from check_pr_aid import extract_aid_identifiers

TERMINAL_RELEASE_STATUSES = {
    ReleaseStatus.SUCCEEDED,
    ReleaseStatus.ROLLED_BACK,
    ReleaseStatus.FAILED,
}


@dataclass(frozen=True)
class PullRequestLink:
    number: int
    aid_identifier: str
    merged_by: str


@dataclass(frozen=True)
class QualityGate:
    complete: bool
    successful: bool
    details_url: str
    conclusion: str
    run_id: int | None


class ReleaseOutboxPayload(TypedDict, total=False):
    aid: str
    marker: str
    content: str
    comment_key: str


def github_path(config: ControllerConfig, suffix: str) -> str:
    owner, repository = config.owner_repo
    return f"/repos/{owner}/{repository}{suffix}"


def enabled(value: object) -> bool:
    return bool(value.get("enabled")) if isinstance(value, dict) else bool(value)


def validate_repository_policy(config: ControllerConfig, github: GitHubClient) -> None:
    repository = github.get(github_path(config, ""))
    if not isinstance(repository, dict):
        raise ControllerError("GitHub repository response has an unexpected shape")
    if not repository.get("allow_squash_merge"):
        raise ControllerError("repository policy requires squash merge")
    if repository.get("allow_merge_commit") or repository.get("allow_rebase_merge"):
        raise ControllerError("merge commits and rebase merges must be disabled")
    if not config.require_branch_protection:
        return

    branch = urllib.parse.quote(config.branch, safe="")
    protection = github.get(github_path(config, f"/branches/{branch}/protection"))
    if not isinstance(protection, dict):
        raise ControllerError("branch protection response has an unexpected shape")
    required_checks = protection.get("required_status_checks")
    if not isinstance(required_checks, dict) or not required_checks.get("strict"):
        raise ControllerError("branch protection must require strict status checks")
    contexts = {
        str(value)
        for value in required_checks.get("contexts", [])
        if isinstance(value, str)
    }
    for check in required_checks.get("checks", []):
        if isinstance(check, dict) and check.get("context"):
            contexts.add(str(check["context"]))
    if config.quality_check not in contexts:
        raise ControllerError(
            f"branch protection does not require {config.quality_check}"
        )
    if protection.get("required_pull_request_reviews") is None:
        raise ControllerError("branch protection must require pull requests")
    if not enabled(protection.get("enforce_admins")):
        raise ControllerError("branch protection must include administrators")
    if enabled(protection.get("allow_force_pushes")):
        raise ControllerError("force pushes must be disabled")
    if enabled(protection.get("allow_deletions")):
        raise ControllerError("branch deletion must be disabled")


def current_branch_head(config: ControllerConfig, github: GitHubClient) -> str:
    branch = urllib.parse.quote(config.branch, safe="")
    payload = github.get(github_path(config, f"/branches/{branch}"))
    try:
        commit_sha = str(payload["commit"]["sha"])
    except (KeyError, TypeError) as exc:
        raise ControllerError("GitHub branch response does not contain a commit SHA") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise ControllerError(f"GitHub returned an invalid branch SHA: {commit_sha}")
    return commit_sha


def compare_lineage(
    config: ControllerConfig,
    github: GitHubClient,
    cursor: str,
    head_sha: str,
) -> list[str]:
    comparison = github.get(github_path(config, f"/compare/{cursor}...{head_sha}"))
    if not isinstance(comparison, dict) or comparison.get("status") != "ahead":
        status = comparison.get("status") if isinstance(comparison, dict) else "invalid"
        raise ControllerError(
            f"protected branch history is not a strict advance: {cursor} -> {head_sha} ({status})"
        )
    commits = comparison.get("commits", [])
    if not isinstance(commits, list) or not commits:
        raise ControllerError("GitHub comparison advanced without returning commits")
    total_commits = int(comparison.get("total_commits", len(commits)))
    if total_commits != len(commits):
        raise ControllerError(
            f"branch advanced by {total_commits} commits, beyond one comparison page; manual audit required"
        )
    result: list[str] = []
    for commit in commits:
        commit_sha = str(commit.get("sha", "")) if isinstance(commit, dict) else ""
        if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
            raise ControllerError(f"GitHub comparison returned an invalid SHA: {commit_sha}")
        result.append(commit_sha)
    if result[-1] != head_sha:
        raise ControllerError("GitHub comparison does not terminate at the current branch head")
    return result


def resolve_pull_request(
    config: ControllerConfig,
    github: GitHubClient,
    commit_sha: str,
) -> PullRequestLink:
    pulls = github.get(github_path(config, f"/commits/{commit_sha}/pulls"))
    if not isinstance(pulls, list):
        raise ControllerError(f"pull-request linkage for {commit_sha} has an unexpected shape")
    merged = [
        pull
        for pull in pulls
        if isinstance(pull, dict)
        and pull.get("merged_at")
        and pull.get("base", {}).get("ref") == config.branch
        and pull.get("merge_commit_sha") == commit_sha
    ]
    if len(merged) != 1:
        raise ControllerError(
            f"commit {commit_sha} is not the final SHA of exactly one merged PR"
        )
    pull = merged[0]
    merged_by = str(pull.get("merged_by", {}).get("login") or "")
    if merged_by.lower() not in {login.lower() for login in config.allowed_mergers}:
        raise ControllerError(
            f"PR #{pull.get('number')} was merged by unauthorized login {merged_by or '<missing>'}"
        )
    identifiers = extract_aid_identifiers(
        str(pull.get("head", {}).get("ref", "")),
        str(pull.get("title", "")),
        str(pull.get("body") or ""),
    )
    if len(identifiers) != 1:
        raise ControllerError(
            f"PR #{pull.get('number')} must resolve to exactly one AID identifier"
        )
    return PullRequestLink(
        number=int(pull["number"]),
        aid_identifier=identifiers[0],
        merged_by=merged_by,
    )


def validate_lineage(
    config: ControllerConfig,
    github: GitHubClient,
    commit_shas: Sequence[str],
) -> list[PullRequestLink]:
    return [resolve_pull_request(config, github, commit_sha) for commit_sha in commit_shas]


def quality_gate(
    config: ControllerConfig,
    github: GitHubClient,
    commit_sha: str,
) -> QualityGate:
    workflow_name = Path(config.workflow_file).name
    workflow = urllib.parse.quote(workflow_name, safe="")
    query = urllib.parse.urlencode(
        {
            "branch": config.branch,
            "event": "push",
            "head_sha": commit_sha,
            "per_page": "20",
        }
    )
    response = github.get(
        github_path(config, f"/actions/workflows/{workflow}/runs?{query}")
    )
    runs = response.get("workflow_runs", []) if isinstance(response, dict) else []
    matching = [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("path") == config.workflow_file
        and run.get("event") == "push"
        and run.get("head_branch") == config.branch
        and run.get("head_sha") == commit_sha
    ]
    if not matching:
        return QualityGate(False, False, "", "missing", None)
    matching.sort(
        key=lambda run: (int(run.get("run_attempt") or 0), int(run.get("id") or 0)),
        reverse=True,
    )
    run = matching[0]
    run_id = int(run["id"])
    details_url = str(run.get("html_url") or "")
    if run.get("status") != "completed":
        return QualityGate(False, False, details_url, "pending", run_id)
    run_conclusion = str(run.get("conclusion") or "unknown")
    if run_conclusion != "success":
        return QualityGate(True, False, details_url, run_conclusion, run_id)

    jobs_response = github.get(github_path(config, f"/actions/runs/{run_id}/jobs?per_page=100"))
    jobs = jobs_response.get("jobs", []) if isinstance(jobs_response, dict) else []
    gates = [job for job in jobs if isinstance(job, dict) and job.get("name") == config.quality_check]
    if len(gates) != 1:
        return QualityGate(True, False, details_url, "quality-gate-ambiguous", run_id)
    conclusion = str(gates[0].get("conclusion") or "unknown")
    return QualityGate(True, conclusion == "success", details_url, conclusion, run_id)


def deploy_command(config: ControllerConfig, row: sqlite3.Row) -> list[str]:
    command = [
        str(config.deploy_script),
        "--ref",
        str(row["commit_sha"]),
        "--host",
        config.deploy_host,
        "--environment",
        config.environment,
        "--aid",
        first_aid(row),
        "--pr-number",
        str(row["pr_number"]),
    ]
    if row["workflow_url"]:
        command.extend(("--workflow-url", str(row["workflow_url"])))
    return command


def sanitized_environment(config: ControllerConfig) -> Mapping[str, str]:
    environment = os.environ.copy()
    for credential_name in ("GITHUB_TOKEN", "GH_TOKEN", "CREDENTIALS_DIRECTORY"):
        environment.pop(credential_name, None)
    environment.update({"DEPLOY_USER": config.deploy_user, "REMOTE_DIR": config.remote_dir})
    return environment


def run_logged(command: Sequence[str], log_path: Path, config: ControllerConfig) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with log_path.open("a", encoding="utf-8") as log_handle:
        os.chmod(log_path, 0o600)
        log_handle.write(f"[{utc_now()}] command={json.dumps(list(command))}\n")
        log_handle.flush()
        completed = subprocess.run(
            list(command),
            check=False,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=sanitized_environment(config),
        )
        log_handle.write(f"[{utc_now()}] exit_code={completed.returncode}\n")
    return completed.returncode


def aids_from_row(row: sqlite3.Row) -> list[str]:
    raw = row["aid_identifiers"]
    if not raw:
        return []
    parsed = json.loads(str(raw))
    if not isinstance(parsed, list) or not all(isinstance(value, str) for value in parsed):
        raise ControllerError("stored AID lineage is invalid")
    return parsed


def first_aid(row: sqlite3.Row) -> str:
    aids = aids_from_row(row)
    if not aids:
        raise ControllerError("release has no AID linkage")
    return aids[-1]


def release_comment(
    config: ControllerConfig,
    row: sqlite3.Row,
    outcome: str,
    aid: str,
    reason: str,
) -> str:
    marker = f"<!-- agent-gov-release:{row['commit_sha']}:{aid}:{outcome} -->"
    return "\n".join(
        (
            marker,
            "## staging 发布结果",
            "",
            f"- 状态：`{outcome}`",
            f"- Release：`{row['release_id']}`",
            f"- Commit：`{row['commit_sha']}`",
            f"- 触发发布的最终 PR：`#{row['pr_number']}`",
            f"- 环境：`{config.environment}`（{config.deploy_host}）",
            f"- CI：{row['workflow_url'] or config.quality_check}",
            f"- 控制器判定：{reason}",
            "",
            "由 PAT-only 控制器按精确 SHA 幂等发布；失败或回滚不会关闭父 Issue。",
        )
    )


def release_outbox_items(
    config: ControllerConfig,
    row: sqlite3.Row,
    outcome: str,
    reason: str,
) -> list[tuple[str, str, ReleaseOutboxPayload]]:
    commit_sha = str(row["commit_sha"])
    items: list[tuple[str, str, ReleaseOutboxPayload]] = []
    for aid in aids_from_row(row):
        comment_key = f"comment:{commit_sha}:{aid}:{outcome}"
        items.append(
            (
                comment_key,
                "multica_comment",
                {
                    "aid": aid,
                    "marker": f"agent-gov-release:{commit_sha}:{aid}:{outcome}",
                    "content": release_comment(config, row, outcome, aid, reason),
                },
            )
        )
        items.append(
            (
                f"activate-sre:{commit_sha}:{aid}:{outcome}",
                "activate_release_sre",
                {"aid": aid, "comment_key": comment_key},
            )
        )
    return items


def enqueue_release_outbox(
    config: ControllerConfig,
    store: StateStore,
    row: sqlite3.Row,
    outcome: str,
    reason: str | None = None,
) -> None:
    for dedupe_key, kind, payload in release_outbox_items(
        config,
        row,
        outcome,
        reason or str(row["reason"] or outcome),
    ):
        store.enqueue_outbox(dedupe_key, kind, payload)


def complete_release(
    config: ControllerConfig,
    store: StateStore,
    row: sqlite3.Row,
    target: ReleaseStatus,
    outcome: str,
    reason: str,
) -> None:
    commit_sha = str(row["commit_sha"])
    metadata = {f"cursor:{config.branch}": commit_sha}
    if target == ReleaseStatus.SUCCEEDED:
        metadata[f"active:{config.environment}"] = str(row["release_id"])
    store.finalize_release(
        commit_sha,
        target,
        reason=reason,
        metadata=metadata,
        outbox=release_outbox_items(config, row, outcome, reason),
    )


def execute_release(config: ControllerConfig, store: StateStore, row: sqlite3.Row) -> None:
    commit_sha = str(row["commit_sha"])
    store.transition(commit_sha, ReleaseStatus.DEPLOYING)
    log_path = config.state_dir / "logs" / f"{commit_sha}.log"
    exit_code = run_logged(deploy_command(config, row), log_path, config)
    refreshed = store.get_release(commit_sha)
    assert refreshed is not None
    if exit_code == 0:
        complete_release(
            config,
            store,
            refreshed,
            ReleaseStatus.SUCCEEDED,
            "succeeded",
            f"healthy release {refreshed['release_id']}",
        )
    elif exit_code == 2:
        complete_release(
            config,
            store,
            refreshed,
            ReleaseStatus.ROLLED_BACK,
            "rolled_back",
            f"release {refreshed['release_id']} failed; automatic rollback succeeded",
        )
    elif exit_code == 3:
        complete_release(
            config,
            store,
            refreshed,
            ReleaseStatus.FAILED,
            "failed",
            f"release {refreshed['release_id']} failed with exit code {exit_code}",
        )
    else:
        reason = (
            f"deploy transport or preflight returned ambiguous exit code {exit_code}; "
            "the exact release will be reconciled idempotently"
        )
        store.transition(commit_sha, ReleaseStatus.WAITING_CI, reason=reason)
        store.add_event("deployment_ambiguous", reason, commit_sha)
        raise ControllerError(reason)


def elapsed_seconds(timestamp: str) -> float:
    started = datetime.fromisoformat(timestamp)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds()


def link_head_release(
    config: ControllerConfig,
    github: GitHubClient,
    store: StateStore,
    cursor: str,
    head_sha: str,
) -> None:
    lineage = compare_lineage(config, github, cursor, head_sha)
    try:
        links = validate_lineage(config, github, lineage)
    except ControllerError as exc:
        store.discover(head_sha)
        store.transition(head_sha, ReleaseStatus.QUARANTINED, reason=str(exc))
        store.add_event("lineage_quarantined", str(exc), head_sha)
        raise

    aids = list(dict.fromkeys(link.aid_identifier for link in links))
    store.discover(head_sha)
    store.set_linkage(
        head_sha,
        pr_number=links[-1].number,
        aid_identifiers=aids,
        release_id=f"{config.environment}-{head_sha[:12]}",
    )
    store.transition(head_sha, ReleaseStatus.WAITING_CI)
    store.supersede_pending(head_sha)


def trusted_gate_allows_deploy(
    config: ControllerConfig,
    github: GitHubClient,
    store: StateStore,
    head_sha: str,
) -> bool:
    gate = quality_gate(config, github, head_sha)
    if gate.run_id is not None:
        store.set_workflow(head_sha, gate.run_id, gate.details_url)
    if not gate.complete:
        row = store.get_release(head_sha)
        assert row is not None
        if elapsed_seconds(str(row["discovered_at"])) > config.ci_timeout_seconds:
            reason = f"trusted push workflow remained {gate.conclusion} past CI timeout"
            store.transition(head_sha, ReleaseStatus.QUARANTINED, reason=reason)
            raise ControllerError(reason)
        store.transition(
            head_sha,
            ReleaseStatus.WAITING_CI,
            reason=f"trusted push workflow is {gate.conclusion}",
        )
        return False
    if not gate.successful:
        reason = f"trusted push workflow conclusion is {gate.conclusion}"
        store.transition(head_sha, ReleaseStatus.QUARANTINED, reason=reason)
        raise ControllerError(reason)
    if current_branch_head(config, github) != head_sha:
        store.transition(
            head_sha,
            ReleaseStatus.SUPERSEDED,
            reason="master advanced before deployment; newer head will be reconciled",
        )
        return False
    return True


def reconcile_head(
    config: ControllerConfig,
    github: GitHubClient,
    store: StateStore,
) -> None:
    head_sha = current_branch_head(config, github)
    cursor_key = f"cursor:{config.branch}"
    cursor = store.get_metadata(cursor_key)
    if cursor is None:
        store.set_metadata(cursor_key, head_sha)
        store.add_event("cursor_initialized", head_sha)
        print(f"Initialized {config.branch} cursor at {head_sha}; no historical release queued")
        return
    if cursor == head_sha:
        existing = store.get_release(head_sha)
        if (
            existing is not None
            and ReleaseStatus(existing["status"]) in TERMINAL_RELEASE_STATUSES
        ):
            if (
                ReleaseStatus(existing["status"]) == ReleaseStatus.SUCCEEDED
                and existing["release_id"]
            ):
                store.set_metadata(
                    f"active:{config.environment}", str(existing["release_id"])
                )
            enqueue_release_outbox(
                config,
                store,
                existing,
                ReleaseStatus(existing["status"]).value,
            )
        return

    existing = store.get_release(head_sha)
    if existing is not None and ReleaseStatus(existing["status"]) in TERMINAL_RELEASE_STATUSES:
        store.set_metadata(cursor_key, head_sha)
        enqueue_release_outbox(config, store, existing, ReleaseStatus(existing["status"]).value)
        return
    if existing is not None and ReleaseStatus(existing["status"]) == ReleaseStatus.QUARANTINED:
        raise ControllerError(f"current master head is quarantined: {existing['reason']}")

    link_head_release(config, github, store, cursor, head_sha)
    if not trusted_gate_allows_deploy(config, github, store, head_sha):
        return
    row = store.get_release(head_sha)
    assert row is not None
    execute_release(config, store, row)


def poll(config: ControllerConfig) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with controller_lock(config.state_dir):
        store = StateStore(config.state_dir / "state.db")
        try:
            store.recover_incomplete()
            flush_outbox(config, store)
            github = GitHubClient(api_url=config.github_api_url, token=load_github_token())
            validate_repository_policy(config, github)
            reconcile_head(config, github, store)
            flush_outbox(config, store)
        except ControllerError as exc:
            store.add_event("poll_blocked", str(exc))
            raise
        finally:
            store.close()


def show_status(config: ControllerConfig) -> None:
    store = StateStore(config.state_dir / "state.db")
    try:
        print(json.dumps(store.snapshot(), ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        store.close()


def diagnose(config: ControllerConfig, release_id: str) -> int:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", release_id):
        raise ControllerError(f"invalid release id: {release_id}")
    command = [
        str(config.deploy_script),
        "--diagnose",
        release_id,
        "--host",
        config.deploy_host,
        "--environment",
        config.environment,
    ]
    return subprocess.run(command, check=False, env=sanitized_environment(config)).returncode


def rollback(config: ControllerConfig, release_id: str, approved_by: str) -> int:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", release_id):
        raise ControllerError(f"invalid release id: {release_id}")
    if not approved_by.strip():
        raise ControllerError("manual rollback requires --approved-by")
    with controller_lock(config.state_dir):
        store = StateStore(config.state_dir / "state.db")
        try:
            row = store.get_release_by_id(release_id)
            if row is None:
                raise ControllerError(f"release is not tracked by the controller: {release_id}")
            command = [
                str(config.deploy_script),
                "--rollback",
                release_id,
                "--host",
                config.deploy_host,
                "--environment",
                config.environment,
            ]
            event_id = f"manual-{release_id}-{int(datetime.now(timezone.utc).timestamp())}"
            log_path = config.state_dir / "logs" / f"{event_id}.log"
            exit_code = run_logged(command, log_path, config)
            outcome = "manual_rollback_succeeded" if exit_code == 0 else "manual_rollback_failed"
            store.add_event(
                outcome,
                f"target={release_id}; approved_by={approved_by}",
                str(row["commit_sha"]),
            )
            if exit_code == 0:
                store.set_metadata(f"active:{config.environment}", release_id)
            enqueue_release_outbox(config, store, row, outcome)
            flush_outbox(config, store)
            return exit_code
        finally:
            store.close()


def flush_only(config: ControllerConfig) -> None:
    with controller_lock(config.state_dir):
        store = StateStore(config.state_dir / "state.db")
        try:
            flush_outbox(config, store)
        finally:
            store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PAT-only AgentGov staging controller")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("poll", help="reconcile the latest protected master SHA")
    subparsers.add_parser("status", help="print controller state as JSON")
    subparsers.add_parser("flush-outbox", help="retry durable Multica notifications")
    diagnose_parser = subparsers.add_parser("diagnose", help="run target diagnostics")
    diagnose_parser.add_argument("release_id")
    rollback_parser = subparsers.add_parser("rollback", help="activate a tracked release")
    rollback_parser.add_argument("release_id")
    rollback_parser.add_argument("--approved-by", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = ControllerConfig.from_environment()
        if args.command == "poll":
            poll(config)
            return 0
        if args.command == "status":
            show_status(config)
            return 0
        if args.command == "flush-outbox":
            flush_only(config)
            return 0
        if args.command == "diagnose":
            return diagnose(config, args.release_id)
        if args.command == "rollback":
            return rollback(config, args.release_id, args.approved_by)
        raise ControllerError(f"unsupported command: {args.command}")
    except ControllerError as exc:
        print(f"release-controller: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
