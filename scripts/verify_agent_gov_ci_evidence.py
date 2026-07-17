#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Protocol, TypedDict, cast

from check_pr_aid import validate_pull_request_metadata

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_AID = re.compile(r"^AID-[0-9]+$")
_BRANCH = re.compile(r"^[A-Za-z0-9_.-]+$")
_WORKFLOW_FILE = re.compile(r"^[A-Za-z0-9_./-]+\.ya?ml$")


class EvidenceError(RuntimeError):
    """The supplied deployment trace is not backed by successful CI evidence."""


class GitHubReader(Protocol):
    def get(self, path: str) -> object: ...


class RepositoryPayload(TypedDict, total=False):
    full_name: str


class WorkflowRunPayload(TypedDict, total=False):
    id: int
    run_attempt: int
    event: str
    head_branch: str
    head_sha: str
    status: str
    conclusion: str
    path: str
    repository: RepositoryPayload


class WorkflowJobPayload(TypedDict, total=False):
    name: str
    status: str
    conclusion: str


class WorkflowJobsPayload(TypedDict, total=False):
    jobs: list[WorkflowJobPayload]


class WorkflowRunsPayload(TypedDict, total=False):
    workflow_runs: list[WorkflowRunPayload]


class PullRefPayload(TypedDict, total=False):
    ref: str


class PullRequestPayload(TypedDict, total=False):
    number: int
    state: str
    merged_at: str | None
    merge_commit_sha: str | None
    base: PullRefPayload
    head: PullRefPayload
    title: str
    body: str | None


@dataclass(frozen=True)
class EvidenceConfig:
    """部署证据的机器事实。

    `aid_identifier` / `pr_number` **可选**:仓库当前允许 master 直推(无分支保护),此时提交
    没有 PR,证据链落在「该 SHA 在 master push 上跑出 quality-gate success」这一条上——
    它已由 _validate_workflow_run 硬校验 event/head_branch/head_sha/conclusion。
    两者提供时按 PR 流程全量校验且必须成对出现:AID 是从 PR 元数据里读出来比对的,
    只给 PR 号会让 AID 校验静默失效,只给 AID 则无从校验。
    """

    repository: str
    commit_sha: str
    workflow_url: str | None = None
    aid_identifier: str | None = None
    pr_number: int | None = None
    branch: str = "master"
    workflow_file: str = ".github/workflows/governance.yml"

    def validate(self) -> None:
        if not _REPOSITORY.fullmatch(self.repository):
            raise EvidenceError(f"invalid repository: {self.repository}")
        if not _FULL_SHA.fullmatch(self.commit_sha):
            raise EvidenceError("commit SHA must be a lowercase full 40-character value")
        if (self.aid_identifier is None) != (self.pr_number is None):
            raise EvidenceError("AID identifier and pull request number must be supplied together")
        if self.aid_identifier is not None and not _AID.fullmatch(self.aid_identifier):
            raise EvidenceError(f"invalid AID identifier: {self.aid_identifier}")
        if self.pr_number is not None and self.pr_number < 1:
            raise EvidenceError("pull request number must be positive")
        if not _BRANCH.fullmatch(self.branch):
            raise EvidenceError(f"invalid branch: {self.branch}")
        if not _WORKFLOW_FILE.fullmatch(self.workflow_file):
            raise EvidenceError(f"invalid workflow file: {self.workflow_file}")


@dataclass(frozen=True)
class WorkflowReference:
    run_id: int
    requested_attempt: int | None

    @classmethod
    def discover(cls, config: EvidenceConfig, reader: GitHubReader) -> WorkflowReference:
        """按 SHA 反查该提交的 master-push run。

        workflow URL 只是定位符、不是成功证明——无论手填还是反查，下面都会重新把 run 的
        event/head_branch/head_sha/conclusion 和 quality-gate 结论整套查一遍。反查在这里
        只做定位，判定标准不变。

        筛选条件与 _validate_workflow_run 一致，反查到什么就必然过得了校验；筛不到就报
        「没有成功证据」，而不是回落到一个不满足条件的 run。
        """
        path = f"/repos/{config.repository}/actions/runs?head_sha={config.commit_sha}&event=push&branch={config.branch}&per_page=100"
        raw_payload = reader.get(path)
        _require_object(raw_payload, label="workflow runs")
        payload = cast(WorkflowRunsPayload, raw_payload)
        runs = payload.get("workflow_runs")
        if not isinstance(runs, list):
            raise EvidenceError("GitHub returned invalid workflow runs")
        candidates: list[int] = []
        for raw_run in runs:
            _require_object(raw_run, label="workflow run")
            run = cast(WorkflowRunPayload, raw_run)
            if str(run.get("path") or "").split("@", 1)[0] != config.workflow_file:
                continue
            if run.get("status") != "completed" or run.get("conclusion") != "success":
                continue
            candidates.append(_positive_int(run.get("id"), label="workflow run id"))
        if not candidates:
            raise EvidenceError(f"no successful {config.workflow_file} run found for {config.branch} push {config.commit_sha}")
        # 同一 SHA 可能有多次成功 run（重跑）。任一成功 run 都证明该 SHA 通过；取最新的一次，
        # 让重跑修好的结果生效而不是钉死在旧 run 上。attempt 由 run 载荷自身决定，不在这里指定。
        return cls(run_id=max(candidates), requested_attempt=None)

    @classmethod
    def parse(cls, repository: str, workflow_url: str) -> WorkflowReference:
        pattern = re.compile(
            rf"^https://github\.com/{re.escape(repository)}/actions/runs/"
            r"(?P<run_id>[1-9][0-9]*)(?:/attempts/(?P<attempt>[1-9][0-9]*))?/?$"
        )
        match = pattern.fullmatch(workflow_url)
        if match is None:
            raise EvidenceError(f"workflow URL must identify a run in {repository}")
        attempt = match.group("attempt")
        return cls(
            run_id=int(match.group("run_id")),
            requested_attempt=int(attempt) if attempt else None,
        )


@dataclass(frozen=True)
class VerifiedEvidence:
    repository: str
    commit_sha: str
    workflow_run_id: int
    workflow_attempt: int
    workflow_file: str
    quality_gate: str
    pr_number: int | None
    aid_identifier: str | None
    # 实际校验的那个 run 的规范 URL。反查(未传 --workflow-url)时由此回吐给调用方写进
    # release.json——留痕的必须是真正被校验的 run，不能是调用者转述的字符串。
    workflow_url: str


class GitHubClient:
    def __init__(self, api_url: str = "https://api.github.com") -> None:
        if api_url != "https://api.github.com":
            raise EvidenceError("deployment evidence must use https://api.github.com")
        self._api_url = api_url

    def get(self, path: str) -> object:
        request = urllib.request.Request(
            f"{self._api_url}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "agent-gov-deploy-evidence-verifier",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", "replace")
            raise EvidenceError(f"GitHub API GET {path} failed with HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise EvidenceError(f"GitHub API GET {path} failed: {exc}") from exc
        try:
            return json.loads(body) if body else None
        except json.JSONDecodeError as exc:
            raise EvidenceError(f"GitHub API GET {path} returned invalid JSON") from exc


def _require_object(value: object, *, label: str) -> None:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise EvidenceError(f"GitHub returned invalid {label}")


def _positive_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise EvidenceError(f"GitHub returned invalid {label}")
    return value


def _validate_workflow_run(
    config: EvidenceConfig,
    reference: WorkflowReference,
    payload: object,
) -> int:
    _require_object(payload, label="workflow run")
    run = cast(WorkflowRunPayload, payload)
    run_id = _positive_int(run.get("id"), label="workflow run id")
    attempt = _positive_int(run.get("run_attempt"), label="workflow run attempt")
    if run_id != reference.run_id:
        raise EvidenceError(f"workflow run id mismatch: {run_id} != {reference.run_id}")
    if reference.requested_attempt is not None and attempt != reference.requested_attempt:
        raise EvidenceError(f"workflow run attempt mismatch: {attempt} != {reference.requested_attempt}")
    expected = {
        "event": "push",
        "head_branch": config.branch,
        "head_sha": config.commit_sha,
        "status": "completed",
        "conclusion": "success",
    }
    for field, expected_value in expected.items():
        if run.get(field) != expected_value:
            raise EvidenceError(f"workflow run {field} mismatch: {run.get(field)!r} != {expected_value!r}")
    workflow_path = str(run.get("path") or "").split("@", 1)[0]
    if workflow_path != config.workflow_file:
        raise EvidenceError(f"workflow file mismatch: {workflow_path!r} != {config.workflow_file!r}")
    raw_repository = run.get("repository")
    _require_object(raw_repository, label="workflow repository")
    repository = cast(RepositoryPayload, raw_repository)
    if repository.get("full_name") != config.repository:
        raise EvidenceError(f"workflow repository mismatch: {repository.get('full_name')!r} != {config.repository!r}")
    return attempt


def _validate_quality_gate(
    reader: GitHubReader,
    *,
    repository: str,
    run_id: int,
    attempt: int,
) -> None:
    path = f"/repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"
    raw_payload = reader.get(path)
    _require_object(raw_payload, label="workflow jobs")
    payload = cast(WorkflowJobsPayload, raw_payload)
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise EvidenceError("GitHub returned invalid workflow jobs")
    matching: list[WorkflowJobPayload] = []
    for raw_job in jobs:
        _require_object(raw_job, label="workflow job")
        job = cast(WorkflowJobPayload, raw_job)
        if job.get("name") == "quality-gate":
            matching.append(job)
    if len(matching) != 1:
        raise EvidenceError(f"workflow attempt must contain exactly one quality-gate job; found {len(matching)}")
    quality_gate = matching[0]
    if quality_gate.get("status") != "completed" or quality_gate.get("conclusion") != "success":
        raise EvidenceError(f"quality-gate job is not successful: status={quality_gate.get('status')!r}, conclusion={quality_gate.get('conclusion')!r}")


def _validate_associated_pull_request(
    config: EvidenceConfig,
    reader: GitHubReader,
) -> PullRequestPayload:
    path = f"/repos/{config.repository}/commits/{config.commit_sha}/pulls?per_page=100"
    payload = reader.get(path)
    if not isinstance(payload, list):
        raise EvidenceError("GitHub returned invalid commit pull requests")
    qualifying: list[int] = []
    for raw_pull in payload:
        _require_object(raw_pull, label="commit pull request")
        pull = cast(PullRequestPayload, raw_pull)
        raw_base = pull.get("base")
        _require_object(raw_base, label="commit pull request base")
        base = cast(PullRefPayload, raw_base)
        if pull.get("merge_commit_sha") == config.commit_sha and pull.get("merged_at") and base.get("ref") == config.branch:
            qualifying.append(_positive_int(pull.get("number"), label="pull request number"))
    if qualifying != [config.pr_number]:
        raise EvidenceError(f"commit must be the merge commit of exactly the supplied pull request; found {qualifying or 'none'}")
    raw_pull = reader.get(f"/repos/{config.repository}/pulls/{config.pr_number}")
    _require_object(raw_pull, label="pull request")
    return cast(PullRequestPayload, raw_pull)


def _validate_pull_request(config: EvidenceConfig, pull: PullRequestPayload) -> None:
    raw_base = pull.get("base")
    _require_object(raw_base, label="pull request base")
    base = cast(PullRefPayload, raw_base)
    raw_head = pull.get("head")
    _require_object(raw_head, label="pull request head")
    head = cast(PullRefPayload, raw_head)
    expected = {
        "number": config.pr_number,
        "state": "closed",
        "merge_commit_sha": config.commit_sha,
    }
    for field, expected_value in expected.items():
        if pull.get(field) != expected_value:
            raise EvidenceError(f"pull request {field} mismatch: {pull.get(field)!r} != {expected_value!r}")
    if not pull.get("merged_at"):
        raise EvidenceError("pull request is not merged")
    if base.get("ref") != config.branch:
        raise EvidenceError(f"pull request base mismatch: {base.get('ref')!r} != {config.branch!r}")
    try:
        aid = validate_pull_request_metadata(
            str(head.get("ref") or ""),
            str(pull.get("title") or ""),
            str(pull.get("body") or ""),
        )
    except ValueError as exc:
        raise EvidenceError(f"pull request AID metadata is invalid: {exc}") from exc
    if aid != config.aid_identifier:
        raise EvidenceError(f"pull request AID mismatch: {aid!r} != {config.aid_identifier!r}")


def verify_ci_evidence(
    config: EvidenceConfig,
    reader: GitHubReader,
) -> VerifiedEvidence:
    config.validate()
    if config.workflow_url is None:
        reference = WorkflowReference.discover(config, reader)
    else:
        reference = WorkflowReference.parse(config.repository, config.workflow_url)
    run_path = f"/repos/{config.repository}/actions/runs/{reference.run_id}"
    attempt = _validate_workflow_run(config, reference, reader.get(run_path))
    _validate_quality_gate(
        reader,
        repository=config.repository,
        run_id=reference.run_id,
        attempt=attempt,
    )
    if config.pr_number is not None:
        pull = _validate_associated_pull_request(config, reader)
        _validate_pull_request(config, pull)
    return VerifiedEvidence(
        repository=config.repository,
        commit_sha=config.commit_sha,
        workflow_run_id=reference.run_id,
        workflow_attempt=attempt,
        workflow_file=config.workflow_file,
        quality_gate="success",
        pr_number=config.pr_number,
        aid_identifier=config.aid_identifier,
        workflow_url=(f"https://github.com/{config.repository}/actions/runs/{reference.run_id}/attempts/{attempt}"),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify public GitHub CI evidence before an AgentGov staging deployment")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--ref", dest="commit_sha", required=True)
    parser.add_argument("--aid", dest="aid_identifier", default=None)
    parser.add_argument("--pr-number", type=int, default=None)
    parser.add_argument("--workflow-url", default=None)
    parser.add_argument("--branch", default="master")
    parser.add_argument("--workflow-file", default=".github/workflows/governance.yml")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = EvidenceConfig(
        repository=args.repository,
        commit_sha=args.commit_sha,
        aid_identifier=args.aid_identifier,
        pr_number=args.pr_number,
        workflow_url=args.workflow_url,
        branch=args.branch,
        workflow_file=args.workflow_file,
    )
    try:
        evidence = verify_ci_evidence(config, GitHubClient())
    except EvidenceError as exc:
        print(f"[ci-evidence] ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(evidence), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
