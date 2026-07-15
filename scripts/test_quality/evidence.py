from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field

from .collection import CollectionResult, nodeid_digest
from .models import NonEmpty, StrictModel


class GitHubRun(StrictModel):
    run_id: str
    run_attempt: str
    job: str


class CollectionEvidence(StrictModel):
    global_digest: NonEmpty
    global_count: int = Field(ge=1)
    selected_digest: NonEmpty
    selected_count: int = Field(ge=1)


class TimingEvidence(StrictModel):
    started_at: datetime
    completed_at: datetime
    wall_seconds: float = Field(ge=0)
    workers: int = Field(ge=0)
    scheduler: Literal["serial", "load", "worksteal"]


class ArtifactEvidence(StrictModel):
    path: Literal["junit.xml", "coverage.json"]
    sha256: NonEmpty


class TestEvidence(StrictModel):
    commit_sha: NonEmpty
    dirty: bool
    github: GitHubRun
    policy_sha256: NonEmpty
    dependency_hashes: dict[str, NonEmpty]
    lane: NonEmpty
    command: list[NonEmpty] = Field(min_length=1)
    collection: CollectionEvidence
    selection: list[NonEmpty]
    versions: dict[str, NonEmpty]
    timing: TimingEvidence
    outcomes: dict[str, Literal["passed", "failed", "skipped"]]
    artifacts: list[ArtifactEvidence]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def git_identity(repo_root: Path) -> tuple[str, bool]:
    commit = _git(repo_root, "rev-parse", "HEAD")
    dirty = bool(_git(repo_root, "status", "--porcelain"))
    return commit, dirty


def dependency_hashes(repo_root: Path) -> Mapping[str, str]:
    paths = ("requirements.txt", "frontend/pnpm-lock.yaml")
    return {path: sha256_file(repo_root / path) for path in paths if (repo_root / path).is_file()}


def runtime_versions() -> Mapping[str, str]:
    versions = {"python": sys.version.split()[0]}
    for package in ("pytest", "pytest-cov", "pytest-xdist", "coverage"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            continue
    return versions


def parse_junit(path: Path) -> tuple[Mapping[str, Literal["passed", "failed", "skipped"]], list[str]]:
    root = ET.parse(path).getroot()
    outcomes: dict[str, Literal["passed", "failed", "skipped"]] = {}
    errors: list[str] = []
    for case in root.iter("testcase"):
        properties = case.find("properties")
        nodeids = (
            []
            if properties is None
            else [prop.attrib.get("value", "") for prop in properties.findall("property") if prop.attrib.get("name") == "agentgov_nodeid"]
        )
        if len(nodeids) != 1 or not nodeids[0]:
            errors.append(f"JUnit testcase lacks one exact agentgov_nodeid property: {case.attrib.get('name', '<unknown>')}")
            continue
        nodeid = nodeids[0]
        if nodeid in outcomes:
            errors.append(f"JUnit contains duplicate pytest leaf: {nodeid}")
            continue
        if case.find("failure") is not None or case.find("error") is not None:
            outcome: Literal["passed", "failed", "skipped"] = "failed"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        outcomes[nodeid] = outcome
    return outcomes, errors


def build_evidence(
    *,
    repo_root: Path,
    policy_path: Path,
    artifact_dir: Path,
    lane: str,
    global_collection: CollectionResult,
    selection: tuple[str, ...],
    command: list[str],
    started_at: datetime,
    completed_at: datetime,
    wall_seconds: float,
    workers: int,
    scheduler: Literal["serial", "load", "worksteal"],
) -> TestEvidence:
    junit_path = artifact_dir / "junit.xml"
    coverage_path = artifact_dir / "coverage.json"
    outcomes, junit_errors = parse_junit(junit_path)
    if junit_errors:
        raise ValueError("; ".join(junit_errors))
    commit, dirty = git_identity(repo_root)
    return TestEvidence(
        commit_sha=commit,
        dirty=dirty,
        github=GitHubRun(
            run_id=os.environ.get("GITHUB_RUN_ID", ""),
            run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            job=os.environ.get("GITHUB_JOB", ""),
        ),
        policy_sha256=sha256_file(policy_path),
        dependency_hashes=dependency_hashes(repo_root),
        lane=lane,
        command=command,
        collection=CollectionEvidence(
            global_digest=global_collection.digest,
            global_count=len(global_collection.nodeids),
            selected_digest=nodeid_digest(selection),
            selected_count=len(selection),
        ),
        selection=list(selection),
        versions=runtime_versions(),
        timing=TimingEvidence(
            started_at=started_at,
            completed_at=completed_at,
            wall_seconds=wall_seconds,
            workers=workers,
            scheduler=scheduler,
        ),
        outcomes=outcomes,
        artifacts=[
            ArtifactEvidence(path="junit.xml", sha256=sha256_file(junit_path)),
            ArtifactEvidence(path="coverage.json", sha256=sha256_file(coverage_path)),
        ],
    )


def write_evidence(evidence: TestEvidence, path: Path) -> None:
    path.write_text(evidence.model_dump_json(indent=2, by_alias=True) + "\n", encoding="utf-8")


def _safe_artifact(artifact_dir: Path, name: str) -> Path:
    root = artifact_dir.resolve()
    path = artifact_dir / name
    if path.is_symlink():
        raise ValueError(f"evidence artifact must not be a symlink: {name}")
    resolved = path.resolve(strict=True)
    if resolved.parent != root:
        raise ValueError(f"evidence artifact escapes artifact directory: {name}")
    return resolved


def validate_evidence(
    *,
    artifact_dir: Path,
    policy_path: Path,
    expected_selection: tuple[str, ...] | None = None,
    expected_collection: CollectionResult | None = None,
    require_clean: bool = False,
    expected_sha: str | None = None,
    expected_run_id: str | None = None,
    expected_run_attempt: str | None = None,
    expected_job: str | None = None,
    require_all_passed: bool = False,
) -> list[str]:
    errors: list[str] = []
    if artifact_dir.is_symlink():
        return ["evidence artifact directory must not be a symlink"]
    try:
        evidence_path = _safe_artifact(artifact_dir, "evidence.json")
        evidence = TestEvidence.model_validate_json(evidence_path.read_text(encoding="utf-8"))
        junit_path = _safe_artifact(artifact_dir, "junit.xml")
        coverage_path = _safe_artifact(artifact_dir, "coverage.json")
    except (OSError, ValueError) as exc:
        return [str(exc)]
    if require_clean and evidence.dirty:
        errors.append("trusted evidence was produced from a dirty tracked worktree")
    if expected_sha and evidence.commit_sha != expected_sha:
        errors.append(f"evidence commit mismatch: {evidence.commit_sha} != {expected_sha}")
    if expected_run_id and evidence.github.run_id != expected_run_id:
        errors.append(f"evidence GitHub run mismatch: {evidence.github.run_id} != {expected_run_id}")
    if expected_run_attempt and evidence.github.run_attempt != expected_run_attempt:
        errors.append(f"evidence GitHub run attempt mismatch: {evidence.github.run_attempt} != {expected_run_attempt}")
    if expected_job and evidence.github.job != expected_job:
        errors.append(f"evidence GitHub job mismatch: {evidence.github.job} != {expected_job}")
    if evidence.policy_sha256 != sha256_file(policy_path):
        errors.append("evidence policy hash does not match the checked-out policy")
    expected_dependencies = dependency_hashes(policy_path.resolve().parents[1])
    if evidence.dependency_hashes != expected_dependencies:
        errors.append("evidence dependency hashes do not match the checked-out dependency manifests")
    if evidence.selection != sorted(set(evidence.selection)):
        errors.append("evidence selection must be sorted and unique")
    if evidence.collection.selected_count != len(evidence.selection):
        errors.append("evidence selected_count does not match selection")
    if evidence.collection.selected_digest != nodeid_digest(evidence.selection):
        errors.append("evidence selected_digest does not match selection")
    if evidence.timing.completed_at < evidence.timing.started_at:
        errors.append("evidence completion time precedes start time")
    if expected_collection is not None:
        if evidence.collection.global_digest != expected_collection.digest:
            errors.append("evidence global collection digest does not match current pytest collection")
        if evidence.collection.global_count != len(expected_collection.nodeids):
            errors.append("evidence global collection count does not match current pytest collection")
    if expected_selection is not None and tuple(evidence.selection) != expected_selection:
        errors.append("evidence selection is not the complete expected lane selection")
    junit_outcomes, junit_errors = parse_junit(junit_path)
    errors.extend(junit_errors)
    if junit_outcomes != evidence.outcomes:
        errors.append("evidence outcomes do not match JUnit outcomes")
    if set(evidence.outcomes) != set(evidence.selection):
        missing = sorted(set(evidence.selection) - set(evidence.outcomes))
        extra = sorted(set(evidence.outcomes) - set(evidence.selection))
        errors.append(f"JUnit is a partial or foreign run: missing={missing[:5]} extra={extra[:5]}")
    if require_all_passed:
        non_passed = sorted((nodeid, outcome) for nodeid, outcome in evidence.outcomes.items() if outcome != "passed")
        if non_passed:
            errors.append(f"evidence contains non-passed pytest leaves: {non_passed[:5]}")
    artifact_paths = {item.path: item.sha256 for item in evidence.artifacts}
    if len(evidence.artifacts) != 2 or set(artifact_paths) != {"junit.xml", "coverage.json"}:
        errors.append("evidence must declare exactly the JUnit and coverage artifacts")
    for name, path in (("junit.xml", junit_path), ("coverage.json", coverage_path)):
        if artifact_paths.get(name) != sha256_file(path):
            errors.append(f"evidence artifact hash mismatch: {name}")
    try:
        coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
        if not isinstance(coverage, dict) or not isinstance(coverage.get("totals"), dict):
            errors.append("coverage artifact does not contain totals")
    except (OSError, json.JSONDecodeError):
        errors.append("coverage artifact is not valid JSON")
    return errors


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
