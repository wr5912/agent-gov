#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TypedDict, cast

from agent_gov_ci_relay_store import (
    OutboxSnapshot,
    OutboxStore,
    RunWatermark,
)
from agent_gov_ci_relay_store import (
    utc_now as utc_now,
)
from agent_gov_multica import (
    MulticaConfig,
    MulticaError,
    deliver_comment,
)
from check_pr_aid import validate_pull_request_metadata

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_WORKFLOW_FILE = re.compile(r"^[A-Za-z0-9_./-]+\.ya?ml$")
_TERMINAL_EVENTS = ("pull_request", "push")
_FAILED_JOB_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "failure",
    "stale",
    "startup_failure",
    "timed_out",
}
_TERMINAL_CONCLUSIONS = _FAILED_JOB_CONCLUSIONS | {"neutral", "skipped", "success"}


class RelayError(RuntimeError):
    """The relay cannot safely discover or deliver a CI result."""


class TraceResolutionError(RelayError):
    """A workflow run cannot be mapped to exactly one PR and AID."""


class GitHubTransportError(RelayError):
    """GitHub was temporarily unavailable."""


class PollSummary(TypedDict):
    enqueued: int
    skipped: int
    delivered: int
    delivery_pending: int
    outbox: OutboxSnapshot


class GitHubReader(Protocol):
    def get(self, path: str) -> Any: ...


@dataclass(frozen=True)
class RelayConfig:
    repository: str
    branch: str
    workflow_file: str
    github_api_url: str
    state_dir: Path
    multica_profile: str
    run_limit: int
    not_before: datetime | None = None

    @classmethod
    def from_environment(cls) -> RelayConfig:
        raw_run_limit = os.environ.get("AGENT_GOV_RELAY_RUN_LIMIT", "50")
        try:
            run_limit = int(raw_run_limit)
        except ValueError as exc:
            raise RelayError("AGENT_GOV_RELAY_RUN_LIMIT must be an integer") from exc
        raw_not_before = os.environ.get("AGENT_GOV_RELAY_NOT_BEFORE", "").strip()
        not_before = _parse_timestamp(raw_not_before) if raw_not_before else None
        config = cls(
            repository=os.environ.get("AGENT_GOV_REPOSITORY", "wr5912/agent-gov"),
            branch=os.environ.get("AGENT_GOV_BRANCH", "master"),
            workflow_file=os.environ.get(
                "AGENT_GOV_WORKFLOW_FILE",
                ".github/workflows/governance.yml",
            ),
            github_api_url=os.environ.get(
                "GITHUB_API_URL",
                "https://api.github.com",
            ).rstrip("/"),
            state_dir=Path(
                os.environ.get(
                    "AGENT_GOV_RELAY_STATE_DIR",
                    "~/.local/state/agent-gov-ci-status-relay",
                )
            ).expanduser(),
            multica_profile=os.environ.get(
                "MULTICA_PROFILE",
                "ci-status-relay",
            ),
            run_limit=run_limit,
            not_before=not_before,
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not _REPOSITORY.fullmatch(self.repository):
            raise RelayError(f"invalid repository: {self.repository}")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.branch):
            raise RelayError(f"invalid branch: {self.branch}")
        if not _WORKFLOW_FILE.fullmatch(self.workflow_file):
            raise RelayError(f"invalid workflow file: {self.workflow_file}")
        if not self.github_api_url.startswith("https://"):
            raise RelayError("GITHUB_API_URL must be an HTTPS URL")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.multica_profile):
            raise RelayError(f"invalid Multica profile: {self.multica_profile}")
        if not 1 <= self.run_limit <= 100:
            raise RelayError("AGENT_GOV_RELAY_RUN_LIMIT must be between 1 and 100")

    @property
    def owner_repo(self) -> tuple[str, str]:
        owner, repository = self.repository.split("/", 1)
        return owner, repository


@dataclass(frozen=True)
class WorkflowRun:
    run_id: int
    attempt: int
    event: str
    head_sha: str
    conclusion: str
    workflow_url: str
    pull_numbers: tuple[int, ...]
    updated_at: datetime


@dataclass(frozen=True)
class RunTrace:
    aid: str
    pr_number: int


class WorkflowRunReplay(TypedDict):
    run_id: int
    attempt: int
    event: str
    head_sha: str
    conclusion: str
    workflow_url: str
    pull_numbers: list[int]
    updated_at: str


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RelayError(f"invalid RFC3339 timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise RelayError(f"timestamp must include a timezone: {value}")
    return parsed.astimezone(timezone.utc)


def load_github_token() -> str:
    credentials_directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if credentials_directory:
        credential_path = Path(credentials_directory) / "github_token"
        if credential_path.is_file():
            token = credential_path.read_text(encoding="utf-8").strip()
            if token:
                return token
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(name, "").strip()
        if token:
            return token
    raise RelayError("GitHub credential is unavailable; use systemd LoadCredential=github_token")


class GitHubClient:
    def __init__(self, *, api_url: str, token: str) -> None:
        self._api_url = api_url
        self._token = token

    def get(self, path: str) -> Any:
        request = urllib.request.Request(
            f"{self._api_url}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "agent-gov-ci-status-relay",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read(2000).decode("utf-8", "replace")
            error = GitHubTransportError if exc.code >= 500 or exc.code == 429 else RelayError
            raise error(f"GitHub API GET {path} failed with HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GitHubTransportError(f"GitHub API GET {path} failed: {exc}") from exc
        try:
            return json.loads(body) if body else None
        except json.JSONDecodeError as exc:
            raise RelayError(f"GitHub API GET {path} returned invalid JSON") from exc


@contextlib.contextmanager
def relay_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state_dir / "relay.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RelayError("another CI status relay invocation is running") from exc
        yield


def github_path(config: RelayConfig, suffix: str) -> str:
    owner, repository = config.owner_repo
    return f"/repos/{owner}/{repository}{suffix}"


def workflow_runs_path(
    config: RelayConfig,
    event: str,
    *,
    page: int = 1,
) -> str:
    workflow = urllib.parse.quote(Path(config.workflow_file).name, safe="")
    query_values = {
        "event": event,
        "status": "completed",
        "per_page": str(config.run_limit),
        "page": str(page),
    }
    if event == "push":
        query_values["branch"] = config.branch
    if config.not_before is not None:
        not_before = (
            config.not_before.astimezone(timezone.utc)
            .isoformat()
            .replace(
                "+00:00",
                "Z",
            )
        )
        query_values["created"] = f">={not_before}"
    return github_path(
        config,
        f"/actions/workflows/{workflow}/runs?{urllib.parse.urlencode(query_values)}",
    )


def _positive_int(value: object, *, label: str) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise RelayError(f"GitHub returned an invalid {label}: {value!r}") from exc
    if parsed < 1:
        raise RelayError(f"GitHub returned an invalid {label}: {value!r}")
    return parsed


def _pull_numbers(run: Mapping[str, object]) -> tuple[int, ...]:
    raw_pulls = run.get("pull_requests")
    if not isinstance(raw_pulls, list):
        return ()
    numbers: list[int] = []
    for pull in raw_pulls:
        if not isinstance(pull, dict) or not pull.get("number"):
            continue
        number = _positive_int(pull["number"], label="pull request number")
        if number not in numbers:
            numbers.append(number)
    return tuple(numbers)


def parse_workflow_run(
    config: RelayConfig,
    payload: object,
    *,
    expected_event: str,
) -> WorkflowRun | None:
    if not isinstance(payload, dict):
        return None
    workflow_path = str(payload.get("path") or "").split("@", 1)[0]
    if workflow_path != config.workflow_file or payload.get("event") != expected_event or payload.get("status") != "completed":
        return None
    if expected_event == "push" and payload.get("head_branch") != config.branch:
        return None
    head_sha = str(payload.get("head_sha") or "")
    if not _FULL_SHA.fullmatch(head_sha):
        raise RelayError(f"GitHub returned an invalid workflow head SHA: {head_sha}")
    conclusion = payload.get("conclusion")
    if not isinstance(conclusion, str) or conclusion not in _TERMINAL_CONCLUSIONS:
        raise RelayError(f"GitHub returned an invalid workflow conclusion: {conclusion!r}")
    updated_at = _parse_timestamp(str(payload.get("updated_at") or ""))
    if config.not_before is not None and updated_at < config.not_before:
        return None
    return WorkflowRun(
        run_id=_positive_int(payload.get("id"), label="workflow run id"),
        attempt=_positive_int(
            payload.get("run_attempt") or 1,
            label="workflow run attempt",
        ),
        event=expected_event,
        head_sha=head_sha,
        conclusion=conclusion,
        workflow_url=str(payload.get("html_url") or ""),
        pull_numbers=_pull_numbers(cast(Mapping[str, object], payload)),
        updated_at=updated_at,
    )


def _run_watermark(run: WorkflowRun) -> RunWatermark:
    return RunWatermark(
        updated_at=run.updated_at,
        run_id=run.run_id,
        attempt=run.attempt,
    )


def completed_runs(
    config: RelayConfig,
    github: GitHubReader,
    *,
    event: str,
    watermark: RunWatermark | None,
) -> tuple[list[WorkflowRun], RunWatermark | None]:
    discovered: dict[tuple[int, int], WorkflowRun] = {}
    newest = watermark
    page = 1
    while True:
        response = github.get(workflow_runs_path(config, event, page=page))
        values = response.get("workflow_runs") if isinstance(response, dict) else None
        if not isinstance(values, list):
            raise RelayError(f"GitHub {event} workflow list has an unexpected shape")
        for payload in values:
            run = parse_workflow_run(config, payload, expected_event=event)
            if run is None:
                continue
            current = _run_watermark(run)
            if newest is None or current > newest:
                newest = current
            # GitHub timestamps have finite precision. A run with a lower id may
            # become visible later with the same updated_at as the persisted
            # watermark, so keep the whole boundary timestamp and let the
            # durable run/attempt identity perform exact deduplication.
            if watermark is None or current.updated_at >= watermark.updated_at:
                discovered[(run.run_id, run.attempt)] = run
        # GitHub does not promise that workflow pages are ordered by updated_at.
        # The watermark can filter already observed runs, but it must never stop
        # pagination: an older-created run may complete later on a deeper page.
        if len(values) < config.run_limit:
            break
        page += 1
    runs = sorted(discovered.values(), key=_run_watermark)
    return runs, newest


def _validated_trace_from_pull(
    config: RelayConfig,
    payload: object,
    *,
    expected_number: int,
) -> RunTrace:
    if not isinstance(payload, dict):
        raise TraceResolutionError(f"PR #{expected_number} has an unexpected shape")
    try:
        number = int(payload.get("number") or 0)
    except (TypeError, ValueError) as exc:
        raise TraceResolutionError(f"PR #{expected_number} has an invalid number") from exc
    base = payload.get("base")
    if number != expected_number or not isinstance(base, dict) or base.get("ref") != config.branch:
        raise TraceResolutionError(f"PR #{expected_number} is not uniquely bound to {config.branch}")
    head = payload.get("head")
    try:
        aid = validate_pull_request_metadata(
            str(head.get("ref") or "") if isinstance(head, dict) else "",
            str(payload.get("title") or ""),
            str(payload.get("body") or ""),
        )
    except ValueError as exc:
        raise TraceResolutionError(f"PR #{expected_number}: {exc}") from exc
    return RunTrace(aid=aid, pr_number=expected_number)


def resolve_run_trace(
    config: RelayConfig,
    github: GitHubReader,
    run: WorkflowRun,
) -> RunTrace:
    if run.event == "pull_request":
        if len(run.pull_numbers) != 1:
            raise TraceResolutionError(f"workflow run {run.run_id}/{run.attempt} must reference exactly one PR")
        number = run.pull_numbers[0]
        return _validated_trace_from_pull(
            config,
            github.get(github_path(config, f"/pulls/{number}")),
            expected_number=number,
        )

    pulls = github.get(github_path(config, f"/commits/{run.head_sha}/pulls"))
    if not isinstance(pulls, list):
        raise TraceResolutionError(f"push {run.head_sha} pull-request linkage has an unexpected shape")
    matching = [
        pull
        for pull in pulls
        if isinstance(pull, dict)
        and pull.get("merged_at")
        and pull.get("merge_commit_sha") == run.head_sha
        and isinstance(pull.get("base"), dict)
        and pull["base"].get("ref") == config.branch
    ]
    if len(matching) != 1:
        raise TraceResolutionError(f"push {run.head_sha} must be the final SHA of exactly one merged PR")
    number = _positive_int(matching[0].get("number"), label="pull request number")
    return _validated_trace_from_pull(
        config,
        matching[0],
        expected_number=number,
    )


def failed_job_names(
    config: RelayConfig,
    github: GitHubReader,
    run: WorkflowRun,
) -> tuple[str, ...]:
    if run.conclusion == "success":
        return ()
    payload = github.get(
        github_path(
            config,
            f"/actions/runs/{run.run_id}/attempts/{run.attempt}/jobs?per_page=100",
        )
    )
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise RelayError(f"workflow run {run.run_id} jobs have an unexpected shape")
    names = {
        str(job.get("name") or "")
        for job in jobs
        if isinstance(job, dict) and str(job.get("conclusion") or "") in _FAILED_JOB_CONCLUSIONS and str(job.get("name") or "")
    }
    return tuple(sorted(names))


def relay_comment(
    config: RelayConfig,
    run: WorkflowRun,
    trace: RunTrace,
    *,
    failed_jobs: Sequence[str],
) -> tuple[str, str]:
    marker = f"agent-gov-ci:{config.repository}:{run.run_id}:{run.attempt}:{run.conclusion}"
    lines = [
        f"<!-- {marker} -->",
        "## AgentGov 持续 CI 结果",
        "",
        f"- Repository：`{config.repository}`",
        f"- Branch：`{config.branch}`",
        f"- 状态：`{run.conclusion}`",
        f"- 事件：`{run.event}`",
        f"- PR：`#{trace.pr_number}`",
        f"- Commit：`{run.head_sha}`",
        f"- Run ID：`{run.run_id}`",
        f"- Run attempt：`{run.attempt}`",
        f"- Workflow：{run.workflow_url or '(GitHub 未返回 URL)'}",
    ]
    if run.conclusion != "success":
        lines.append(f"- 失败 job：`{', '.join(failed_jobs) if failed_jobs else '未返回具体 job'}")
    lines.extend(("", "该评论由 228 CI status relay 写入 Multica。"))
    return marker, "\n".join(lines)


def _run_identity(config: RelayConfig, run: WorkflowRun) -> str:
    return f"github-run:{config.repository}:{run.run_id}:{run.attempt}:{run.conclusion}"


def _run_replay_payload(run: WorkflowRun) -> str:
    payload: WorkflowRunReplay = {
        "run_id": run.run_id,
        "attempt": run.attempt,
        "event": run.event,
        "head_sha": run.head_sha,
        "conclusion": run.conclusion,
        "workflow_url": run.workflow_url,
        "pull_numbers": list(run.pull_numbers),
        "updated_at": run.updated_at.isoformat(),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _replayed_workflow_run(raw_payload: str) -> WorkflowRun:
    try:
        payload = json.loads(raw_payload)
        if not isinstance(payload, dict):
            raise TypeError("replay payload is not an object")
        event = str(payload["event"])
        if event not in _TERMINAL_EVENTS:
            raise ValueError("replay event is invalid")
        head_sha = str(payload["head_sha"])
        if not _FULL_SHA.fullmatch(head_sha):
            raise ValueError("replay head SHA is invalid")
        conclusion = str(payload["conclusion"])
        if conclusion not in _TERMINAL_CONCLUSIONS:
            raise ValueError("replay conclusion is invalid")
        pull_numbers = payload["pull_numbers"]
        if not isinstance(pull_numbers, list):
            raise TypeError("replay pull_numbers is not a list")
        return WorkflowRun(
            run_id=_positive_int(payload["run_id"], label="replay workflow run id"),
            attempt=_positive_int(
                payload["attempt"],
                label="replay workflow run attempt",
            ),
            event=event,
            head_sha=head_sha,
            conclusion=conclusion,
            workflow_url=str(payload["workflow_url"]),
            pull_numbers=tuple(_positive_int(value, label="replay pull request number") for value in pull_numbers),
            updated_at=_parse_timestamp(str(payload["updated_at"])),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RelayError("stored workflow replay payload is invalid") from exc


def _record_run_failure(
    store: OutboxStore,
    config: RelayConfig,
    run: WorkflowRun,
    *,
    category: str,
    detail: str,
) -> None:
    store.record_failure(
        failure_key=f"{_run_identity(config, run)}:{category}",
        category=category,
        run_id=run.run_id,
        attempt=run.attempt,
        detail=detail,
        replay_payload=_run_replay_payload(run),
    )


def _enqueue_notification(
    config: RelayConfig,
    github: GitHubReader,
    store: OutboxStore,
    run: WorkflowRun,
) -> tuple[int, int]:
    if store.has_run_attempt(config.repository, run.run_id, run.attempt):
        store.resolve_run_failures(
            config.repository,
            run.run_id,
            run.attempt,
        )
        return 0, 0
    try:
        trace = resolve_run_trace(config, github, run)
        jobs = failed_job_names(config, github, run)
    except GitHubTransportError:
        raise
    except TraceResolutionError as exc:
        _record_run_failure(
            store,
            config,
            run,
            category="trace_resolution",
            detail=str(exc),
        )
        print(f"[ci-relay] SKIP: {exc}", file=sys.stderr)
        return 0, 1
    except RelayError as exc:
        _record_run_failure(
            store,
            config,
            run,
            category="github_payload",
            detail=str(exc),
        )
        print(f"[ci-relay] SKIP: {exc}", file=sys.stderr)
        return 0, 1
    marker, content = relay_comment(
        config,
        run,
        trace,
        failed_jobs=jobs,
    )
    inserted = store.enqueue(
        _run_identity(config, run),
        {"aid": trace.aid, "marker": marker, "content": content},
    )
    store.resolve_run_failures(
        config.repository,
        run.run_id,
        run.attempt,
    )
    return int(inserted), 0


def retry_failed_notifications(
    config: RelayConfig,
    github: GitHubReader,
    store: OutboxStore,
) -> tuple[int, int]:
    enqueued = 0
    skipped = 0
    attempted: set[tuple[int, int]] = set()
    for row in store.retryable_failures(config.repository):
        run = _replayed_workflow_run(str(row["replay_payload"]))
        identity = (run.run_id, run.attempt)
        if identity in attempted:
            continue
        attempted.add(identity)
        inserted, run_skipped = _enqueue_notification(
            config,
            github,
            store,
            run,
        )
        enqueued += inserted
        skipped += run_skipped
    return enqueued, skipped


def discover_notifications(
    config: RelayConfig,
    github: GitHubReader,
    store: OutboxStore,
) -> tuple[int, int]:
    enqueued, skipped = retry_failed_notifications(config, github, store)
    for event in _TERMINAL_EVENTS:
        watermark = store.get_watermark(config, event)
        try:
            runs, newest = completed_runs(
                config,
                github,
                event=event,
                watermark=watermark,
            )
            for run in runs:
                inserted, run_skipped = _enqueue_notification(
                    config,
                    github,
                    store,
                    run,
                )
                enqueued += inserted
                skipped += run_skipped
        except GitHubTransportError as exc:
            store.record_failure(
                failure_key=(f"github-stream:{config.repository}:{config.workflow_file}:{event}:github_transport"),
                category="github_transport",
                detail=str(exc),
            )
            raise
        except RelayError as exc:
            store.record_failure(
                failure_key=(f"github-stream:{config.repository}:{config.workflow_file}:{event}:github_payload"),
                category="github_payload",
                detail=str(exc),
            )
            raise
        if newest is not None and (watermark is None or newest > watermark):
            store.set_watermark(config, event, newest)
    return enqueued, skipped


def flush_outbox(
    config: RelayConfig,
    store: OutboxStore,
    *,
    unattempted_only: bool = False,
) -> tuple[int, int]:
    delivered = 0
    failed = 0
    multica = MulticaConfig(profile=config.multica_profile)
    for row in store.pending(unattempted_only=unattempted_only):
        try:
            payload = json.loads(str(row["payload"]))
            if not isinstance(payload, dict):
                raise RelayError("outbox payload is not an object")
            deliver_comment(
                multica,
                aid=str(payload["aid"]),
                marker=str(payload["marker"]),
                content=str(payload["content"]),
            )
        except (KeyError, json.JSONDecodeError, MulticaError, RelayError) as exc:
            store.mark_failed(int(row["id"]), str(exc))
            failed += 1
            print(
                f"[ci-relay] DELIVERY_PENDING: {row['dedupe_key']}: {exc}",
                file=sys.stderr,
            )
            continue
        store.mark_delivered(int(row["id"]))
        delivered += 1
    return delivered, failed


def run_poll(config: RelayConfig, github: GitHubReader) -> PollSummary:
    with relay_lock(config.state_dir):
        store = OutboxStore(config.state_dir / "outbox.sqlite3")
        try:
            delivered_before, failed_before = flush_outbox(config, store)
            enqueued, skipped = discover_notifications(config, github, store)
            delivered_after, failed_after = flush_outbox(
                config,
                store,
                unattempted_only=True,
            )
            return {
                "enqueued": enqueued,
                "skipped": skipped,
                "delivered": delivered_before + delivered_after,
                "delivery_pending": failed_before + failed_after,
                "outbox": store.snapshot(),
            }
        finally:
            store.close()


def status(config: RelayConfig) -> OutboxSnapshot:
    database = config.state_dir / "outbox.sqlite3"
    if not database.is_file():
        return {
            "pending": 0,
            "delivered": 0,
            "pending_items": [],
            "discovery_failures": 0,
            "failure_items": [],
            "watermarks": [],
        }
    store = OutboxStore(database)
    try:
        return store.snapshot()
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Relay terminal AgentGov governance CI results to Multica")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("poll", help="discover and deliver recent terminal workflow runs")
    subparsers.add_parser("status", help="print read-only outbox status")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = RelayConfig.from_environment()
        if args.command == "status":
            print(json.dumps(status(config), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        github = GitHubClient(
            api_url=config.github_api_url,
            token=load_github_token(),
        )
        print(
            json.dumps(
                run_poll(config, github),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (OSError, RelayError, sqlite3.Error) as exc:
        print(f"[ci-relay] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
