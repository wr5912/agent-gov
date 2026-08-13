#!/usr/bin/env python3
"""真实容器中的 per-Agent exact-commit 隔离测试验收；输出不含凭据、私有路径、测试或响应正文。"""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import subprocess
import sys
import tarfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import httpx
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.agent_testing.execution_contracts import (  # noqa: E402
    AgentTestExecutionReceipt,
    canonical_json_digest,
    verify_receipt_integrity,
)
from scripts import agent_test_acceptance_support as acceptance_support  # noqa: E402
from scripts import durable_agent_cleanup as deletion_cleanup  # noqa: E402

ACTIVE_ENV: Final = "AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE"
RUN_ID_ENV: Final = "AGENT_GOV_ACCEPTANCE_RUN_ID"
PROFILE_ENV: Final = "AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE"
RUNTIME_ROOT_ENV: Final = "AGENT_GOV_ACCEPTANCE_RUNTIME_ROOT"
EXPECTED_PROFILE: Final = "agent-test"
ACCEPTANCE_IMAGE_LABEL: Final = "io.agentgov.acceptance-run-id"
SANDBOX_RUN_LABEL: Final = "io.agentgov.agent-test.run-id"
BOOTSTRAP_AGENT_ID: Final = "security-operations-expert"
VOLUME_ROOT_CANARY: Final = acceptance_support.VOLUME_ROOT_CANARY
VOLUME_SIBLING_RUN: Final = acceptance_support.VOLUME_SIBLING_RUN
VOLUME_SIBLING_CANARY: Final = acceptance_support.VOLUME_SIBLING_CANARY
HOSTILE_NODEIDS: Final = frozenset(
    {
        "tests/test_isolation.py::test_secret_environment_is_not_inherited",
        "tests/test_isolation.py::test_live_runtime_and_host_paths_are_not_mounted",
        "tests/test_isolation.py::test_docker_socket_and_host_tools_are_absent",
        "tests/test_isolation.py::test_network_has_no_default_route",
        "tests/test_isolation.py::test_process_is_non_root_and_source_and_rootfs_are_read_only",
        "tests/test_isolation.py::test_named_volume_subpath_hides_root_and_sibling_runs",
    }
)
TERMINAL_STATUSES: Final = frozenset({"passed", "failed", "error", "cancelled", "interrupted"})
ACTIVE_STATUSES: Final = frozenset({"queued", "running"})
SYNTHETIC_PRIVATE_ASSETS: Final = (
    (".env", b"AGENTGOV_HOSTILE_SECRET=must-not-enter-sandbox\n"),
    (".mcp.json", b'{"mcpServers":{"private":{"command":"tool","env":{"TOKEN":"must-not-enter-sandbox"}}}}\n'),
    (".aws/credentials", b"must-not-enter-sandbox\n"),
    (".docker/config.json", b'{"auths":{"registry.invalid":{"auth":"must-not-enter-sandbox"}}}\n'),
    (".claude/settings.json", b'{"env":{"MODEL_PROVIDER_API_KEY":"must-not-enter-sandbox"},"permissions":{"ask":[]}}\n'),
    (".git-credentials", b"https://user:must-not-enter-sandbox@example.invalid\n"),
    ("credentials.json", b'{"token":"must-not-enter-sandbox"}\n'),
    ("ignored.secret", b"must-not-enter-sandbox\n"),
    ("private.pem.txt", b"must-not-enter-sandbox\n"),
    ("secret/token.txt", b"must-not-enter-sandbox\n"),
    ("secrets/token.txt", b"must-not-enter-sandbox\n"),
)
_SOURCE_CHANGE_TEST_BYTES: Final = b"import time\n\n\ndef test_source_change_probe() -> None:\n    time.sleep(5)\n"
SOURCE_CHANGED_PROJECTED_BYTES: Final = _SOURCE_CHANGE_TEST_BYTES + b"# acceptance-host mutation\n"
_FAILURE_TEST_SOURCE: Final = "def test_expected_failure() -> None:\n    assert False, 'expected acceptance failure'\n"
_SLOW_TEST_SOURCE: Final = "import time\n\n\ndef test_slow_terminal_path() -> None:\n    time.sleep(60)\n"
_FULL_SHA: Final = re.compile(r"^[0-9a-f]{40}$")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

DockerRunner = Callable[[list[str]], str]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]


@contextmanager
def _capture_cleanup_error(errors: list[str], label: str) -> Iterator[None]:
    try:
        yield
    except Exception as exc:  # noqa: BLE001 - cleanup must continue through every exact scope
        errors.append(f"{label}:{exc.__class__.__name__}")


JsonDict = dict[str, object]


class AcceptanceError(RuntimeError):
    """验收契约失败；消息不得包含敏感值或响应正文。"""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


def _require_source_observation(receipt: AgentTestExecutionReceipt, expected: Literal["stable", "changed"]) -> None:
    target = receipt.target
    _require(target.source_observation == expected, "executed acceptance run has the wrong source observation")
    if expected == "stable":
        observed = target.pre_source_digest == target.source_digest == target.post_source_digest
    else:
        observed = target.pre_source_digest == target.source_digest and target.post_source_digest not in {None, target.pre_source_digest, target.source_digest}
    _require(observed, "executed acceptance run lacks the required pre/post source evidence")


@dataclass(frozen=True, slots=True)
class AcceptanceSettings:
    base_url: str
    api_key: str
    acceptance_run_id: str
    runtime_root: Path
    api_container: str
    worker_container: str
    request_timeout_seconds: float = 30.0
    run_timeout_seconds: float = 240.0
    poll_seconds: float = 0.2

    @property
    def runtime_data_dir(self) -> Path:
        return self.runtime_root / "volumes/data"

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> AcceptanceSettings:
        active = environ.get(ACTIVE_ENV, "").strip().lower()
        profile = environ.get(PROFILE_ENV, "").strip()
        run_id = environ.get(RUN_ID_ENV, "").strip()
        container_prefix = environ.get("CONTAINER_NAME_PREFIX", "").strip()
        _require(active in {"1", "true", "yes", "on"}, "container acceptance authority is missing")
        _require(profile == EXPECTED_PROFILE, "verification requires the agent-test acceptance profile")
        _require(_SAFE_ID.fullmatch(run_id) is not None, "acceptance run id is missing or invalid")
        _require(_SAFE_ID.fullmatch(container_prefix) is not None, "acceptance worker container prefix is missing or invalid")

        base_url = environ.get("API_BASE", "").strip().rstrip("/")
        try:
            parsed = httpx.URL(base_url)
        except Exception as exc:
            raise AcceptanceError("API_BASE is invalid") from exc
        _require(parsed.scheme in {"http", "https"}, "API_BASE must use HTTP")
        _require(parsed.host in {"127.0.0.1", "localhost", "::1"}, "API_BASE must target the isolated local acceptance API")
        _require(
            not parsed.username and not parsed.password and parsed.path == "/" and not parsed.query and not parsed.fragment,
            "API_BASE contains unsupported authority components",
        )

        raw_root = environ.get(RUNTIME_ROOT_ENV, "").strip()
        candidate = Path(raw_root) if raw_root else Path()
        _require(raw_root != "" and candidate.is_absolute() and candidate != Path("/"), "acceptance runtime root is missing or unsafe")
        runtime_root = _validated_candidate_runtime_root(candidate, run_id)
        data_dir = runtime_root / "volumes/data"
        _require_private_runtime_directory(data_dir, mode=0o755, label="data")
        return cls(
            base_url=base_url,
            api_key=environ.get("API_KEY", ""),
            acceptance_run_id=run_id,
            runtime_root=runtime_root,
            api_container=f"{container_prefix}-api",
            worker_container=f"{container_prefix}-test-worker",
        )


@dataclass(frozen=True, slots=True)
class VerificationSummary:
    runs: int
    terminal_contracts: int
    temporary_agents: int


def _require_private_runtime_directory(path: Path, *, mode: int, label: str) -> os.stat_result:
    try:
        linked = path.lstat()
        resolved = path.resolve(strict=True)
        current = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise AcceptanceError(f"acceptance runtime {label} directory is unavailable") from exc
    valid = (
        resolved == path
        and stat.S_ISDIR(linked.st_mode)
        and (linked.st_dev, linked.st_ino) == (current.st_dev, current.st_ino)
        and current.st_uid == os.geteuid()
        and stat.S_IMODE(current.st_mode) == mode
    )
    _require(valid, f"acceptance runtime {label} directory authority is invalid")
    return current


def _validated_candidate_runtime_root(candidate: Path, run_id: str) -> Path:
    runtime_root = candidate
    root = runtime_root.parent
    snapshot_parent = root.parent
    expected_root = re.compile(rf"^agentgov-acceptance-candidate-{re.escape(run_id)}-[0-9a-f]{{32}}$")
    runtime_identity = _require_private_runtime_directory(runtime_root, mode=0o700, label="root")
    root_identity = _require_private_runtime_directory(root, mode=0o500, label="candidate")
    parent_identity = _require_private_runtime_directory(snapshot_parent, mode=0o700, label="parent")
    valid = (
        runtime_root.name == "runtime"
        and expected_root.fullmatch(root.name) is not None
        and runtime_identity.st_dev == root_identity.st_dev == parent_identity.st_dev
    )
    _require(valid, "acceptance runtime root is outside the candidate snapshot boundary")
    return runtime_root


def _default_docker_runner(command: list[str]) -> str:
    try:
        result = subprocess.run(command, cwd=REPO_ROOT, check=False, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AcceptanceError("Docker evidence command could not run") from exc
    if result.returncode:
        raise AcceptanceError("Docker evidence command failed")
    return result.stdout.strip()


def _workspace_package(agent_id: str, *, test_filename: str, test_source: str) -> bytes:
    _require(_SAFE_ID.fullmatch(agent_id) is not None, "temporary Agent id is invalid")
    _require(re.fullmatch(r"test_[a-z0-9_]+\.py", test_filename) is not None, "temporary test filename is invalid")
    pytest_shadow = (
        b"import json\n"
        b"import os\n"
        b"from pathlib import Path\n\n"
        b"Path(os.environ['AGENTGOV_TEST_REPORT_PATH']).write_text(\n"
        b"    json.dumps({'exit_code': 0, 'items': [], 'invocations': []}), encoding='utf-8'\n"
        b")\n"
        b"raise SystemExit(0)\n"
    )
    files = {
        **dict(SYNTHETIC_PRIVATE_ASSETS),
        "CLAUDE.md": b"# Isolated Agent test acceptance\n",
        "agent.yaml": f"agent:\n  id: {agent_id}\n".encode(),
        "pytest.py": pytest_shadow,
        "pytest/__init__.py": pytest_shadow,
        "tests/README.md": b"# Container isolation acceptance\n",
        f"tests/{test_filename}": test_source.encode("utf-8"),
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        root = tarfile.TarInfo("workspace/")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        for path, content in sorted(files.items()):
            member = tarfile.TarInfo(f"workspace/{path}")
            member.size = len(content)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


def _hostile_test_source(host_marker: Path) -> str:
    return acceptance_support.hostile_test_source(
        host_marker=host_marker,
        private_asset_paths=tuple(path for path, _content in SYNTHETIC_PRIVATE_ASSETS),
    )


class AgentTestContainerAcceptance:
    def __init__(
        self,
        settings: AcceptanceSettings,
        *,
        client: httpx.Client | None = None,
        docker_runner: DockerRunner = _default_docker_runner,
        clock: Clock = time.monotonic,
        sleep: Sleeper = time.sleep,
    ) -> None:
        headers = {"Accept": "application/json"}
        if settings.api_key.strip():
            headers["Authorization"] = f"Bearer {settings.api_key.strip()}"
        self.settings = settings
        self.client = client or httpx.Client(
            base_url=settings.base_url,
            headers=headers,
            timeout=httpx.Timeout(settings.request_timeout_seconds, connect=10.0),
        )
        self._owns_client = client is None
        self._docker_runner = docker_runner
        self._clock = clock
        self._sleep = sleep
        self._temporary_agent_id = f"agent-test-e2e-{uuid.uuid4().hex[:12]}"
        self._temporary_agent_created = False
        self._temporary_agent_instance_etag: str | None = None
        self._run_ids: list[str] = []
        self._host_marker: Path | None = None
        self._volume_canaries_created = False

    def verify(self) -> VerificationSummary:
        acceptance_support.prove_runtime_bootstrap_not_mounted(
            api_container=self.settings.api_container,
            acceptance_run_id=self.settings.acceptance_run_id,
            docker_runner=self._docker_runner,
        )
        acceptance_support.prove_worker_runtime_authority(
            worker_container=self.settings.worker_container,
            acceptance_run_id=self.settings.acceptance_run_id,
            runtime_data_dir=self.settings.runtime_data_dir,
            docker_runner=self._docker_runner,
        )
        health = self._request_json("GET", "/health")
        _require(health.get("status") == "ok", "API health check did not report ok")

        bootstrap_commit = self._current_commit(BOOTSTRAP_AGENT_ID)
        bootstrap_suite = self._inspect_suite(BOOTSTRAP_AGENT_ID, bootstrap_commit)
        _require(int(bootstrap_suite.get("test_file_count") or 0) > 0, "bootstrap Agent suite has no static test files")
        _require(bootstrap_suite.get("requires_live_agent") is False, "bootstrap Agent suite unexpectedly requires the live lane")
        bootstrap = self._execute_run(BOOTSTRAP_AGENT_ID, bootstrap_commit, expected_status="passed")
        _require(bootstrap.get("suite_digest") == bootstrap_suite.get("suite_digest"), "bootstrap run did not bind the inspected suite")

        marker = self._create_host_marker()
        hostile_commit = self._import_workspace(
            test_filename="test_isolation.py",
            test_source=_hostile_test_source(marker),
        )
        self._assert_private_assets_preserved(hostile_commit)
        hostile_suite = self._inspect_suite(self._temporary_agent_id, hostile_commit)
        _require(hostile_suite.get("requires_live_agent") is False, "hostile isolation suite escaped into the live lane")
        hostile_run = self._execute_run(self._temporary_agent_id, hostile_commit, expected_status="passed")
        self._require_expected_nodeids(hostile_run, expected=HOSTILE_NODEIDS)

        failure_commit = self._import_workspace(
            test_filename="test_failure.py",
            test_source=_FAILURE_TEST_SOURCE,
            expected_current_commit=hostile_commit,
        )
        self._execute_run(self._temporary_agent_id, failure_commit, expected_status="failed")

        slow_commit = self._import_workspace(
            test_filename="test_slow.py",
            test_source=_SLOW_TEST_SOURCE,
            expected_current_commit=failure_commit,
        )
        self._execute_cancelled_run(self._temporary_agent_id, slow_commit)
        self._execute_run(
            self._temporary_agent_id,
            slow_commit,
            expected_status="error",
            expected_error_code="AGENT_TEST_RUN_TIMEOUT",
        )
        source_change_commit = self._import_workspace(
            test_filename="test_source_change.py", test_source=_SOURCE_CHANGE_TEST_BYTES.decode("utf-8"), expected_current_commit=slow_commit
        )
        self._execute_source_changed_run(self._temporary_agent_id, source_change_commit)
        return VerificationSummary(
            runs=len(self._run_ids),
            terminal_contracts=6,
            temporary_agents=1,
        )

    def cleanup(self) -> tuple[str, ...]:
        errors: list[str] = []
        for test_run_id in reversed(self._run_ids):
            with _capture_cleanup_error(errors, "run_cleanup"):
                current = self._request_json("GET", f"/api/agent-test-runs/{test_run_id}")
                if current.get("status") in ACTIVE_STATUSES:
                    self._request_json("POST", f"/api/agent-test-runs/{test_run_id}/cancel")
                    current = self._wait_for_terminal(test_run_id, timeout_seconds=45.0)
                if current.get("status") in TERMINAL_STATUSES:
                    self._assert_no_run_residue(test_run_id)

        if self._temporary_agent_created and self._temporary_agent_instance_etag is not None:
            with _capture_cleanup_error(errors, "delete_agent"):
                deletion_cleanup.delete_business_agent_and_wait(
                    self.client,
                    agent_id=self._temporary_agent_id,
                    instance_etag=self._temporary_agent_instance_etag,
                    clock=self._clock,
                    sleep=self._sleep,
                )

        if self._volume_canaries_created:
            with _capture_cleanup_error(errors, "volume_canary"):
                acceptance_support.remove_volume_canaries(
                    worker_container=self.settings.worker_container,
                    docker_runner=self._docker_runner,
                )

        if self._host_marker is not None:
            with _capture_cleanup_error(errors, "host_marker"):
                self._host_marker.unlink(missing_ok=True)
                if self._host_marker.exists() or self._host_marker.is_symlink():
                    errors.append("host_marker:residue")
        if self._owns_client:
            with _capture_cleanup_error(errors, "client_close"):
                self.client.close()
        return tuple(errors)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: JsonDict | None = None,
        expected: tuple[int, ...] = (200,),
        params: dict[str, str] | None = None,
    ) -> JsonDict:
        try:
            response = self.client.request(method, path, json=payload, params=params)
        except httpx.HTTPError as exc:
            raise AcceptanceError(f"{method} request failed") from exc
        if response.status_code not in expected:
            raise AcceptanceError(f"{method} request returned HTTP {response.status_code}")
        return self._response_object(response, operation=f"{method} response")

    @staticmethod
    def _response_object(response: httpx.Response, *, operation: str) -> JsonDict:
        try:
            payload = response.json()
        except ValueError as exc:
            raise AcceptanceError(f"{operation} was not JSON") from exc
        if not isinstance(payload, dict):
            raise AcceptanceError(f"{operation} was not an object")
        return payload

    def _current_commit(self, agent_id: str) -> str:
        payload = self._request_json("GET", "/api/agent-repository/current", params={"agent_id": agent_id})
        commit = payload.get("commit_sha")
        _require(isinstance(commit, str) and _FULL_SHA.fullmatch(commit) is not None, "Agent current ref has no exact commit")
        return commit

    def _inspect_suite(self, agent_id: str, commit_sha: str) -> JsonDict:
        suite = self._request_json(
            "GET",
            f"/api/agent-registry/{agent_id}/test-suite",
            params={"commit_sha": commit_sha},
        )
        _require(suite.get("commit_sha") == commit_sha, "suite inspection returned a different commit")
        _require(suite.get("tests_directory_present") is True, "suite tests directory is missing")
        diagnostics = suite.get("diagnostics")
        if isinstance(diagnostics, list):
            _require(
                not any(isinstance(item, dict) and item.get("level") == "error" for item in diagnostics),
                "suite inspection returned an error diagnostic",
            )
        digest = suite.get("suite_digest")
        _require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None, "suite digest is missing")
        return suite

    def _import_workspace(
        self,
        *,
        test_filename: str,
        test_source: str,
        expected_current_commit: str | None = None,
    ) -> str:
        package = _workspace_package(
            self._temporary_agent_id,
            test_filename=test_filename,
            test_source=test_source,
        )
        data = (
            {"name": "Agent test isolation acceptance"}
            if expected_current_commit is None
            else {
                "expected_current_commit_sha": expected_current_commit,
                "reason": "Advance isolated container acceptance scenario",
            }
        )
        try:
            response = self.client.post(
                f"/api/agent-registry/{self._temporary_agent_id}/workspace/import",
                data=data,
                files={"package": ("workspace.tar.gz", package, "application/gzip")},
            )
        except httpx.HTTPError as exc:
            raise AcceptanceError("temporary Workspace import failed") from exc
        if response.status_code != 200:
            raise AcceptanceError(f"temporary Workspace import returned HTTP {response.status_code}")
        payload = self._response_object(response, operation="temporary Workspace import")
        commit = payload.get("current_commit_sha")
        _require(isinstance(commit, str) and _FULL_SHA.fullmatch(commit) is not None, "Workspace import returned no exact commit")
        _require(payload.get("test_suite_status") == "ready", "Workspace import did not produce a ready static suite")
        self._temporary_agent_instance_etag = deletion_cleanup.instance_etag_from_import(payload)
        self._temporary_agent_created = True
        return commit

    def _assert_private_assets_preserved(self, commit_sha: str) -> None:
        try:
            response = self.client.post(f"/api/agent-registry/{self._temporary_agent_id}/workspace/export")
        except httpx.HTTPError as exc:
            raise AcceptanceError("private asset export evidence failed") from exc
        _require(response.status_code == 200, "private asset export evidence returned an unexpected status")
        package = response.content
        package_digest = response.headers.get("x-workspace-package-sha256", "")
        _require(response.headers.get("x-agent-commit-sha") == commit_sha, "private asset export advanced the exact imported commit")
        _require(
            _SHA256.fullmatch(package_digest) is not None and hashlib.sha256(package).hexdigest() == package_digest,
            "private asset export package digest is invalid",
        )
        _require(_SHA256.fullmatch(response.headers.get("x-workspace-tree-sha256", "")) is not None, "private asset export tree digest is invalid")
        try:
            with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
                members = archive.getmembers()
                for path, expected in SYNTHETIC_PRIVATE_ASSETS:
                    matches = [member for member in members if member.name == f"workspace/{path}"]
                    _require(len(matches) == 1 and matches[0].isfile() and matches[0].size == len(expected), "private asset path is not byte-preserved")
                    source = archive.extractfile(matches[0])
                    _require(
                        source is not None and hashlib.sha256(source.read()).digest() == hashlib.sha256(expected).digest(),
                        "private asset digest is not byte-preserved",
                    )
        except (OSError, tarfile.TarError) as exc:
            raise AcceptanceError("private asset export archive is invalid") from exc

    def _create_run(self, agent_id: str, commit_sha: str) -> str:
        run = self._request_json(
            "POST",
            "/api/agent-test-runs",
            payload={"agent_id": agent_id, "commit_sha": commit_sha},
            expected=(202,),
        )
        test_run_id = run.get("test_run_id")
        _require(isinstance(test_run_id, str) and _SAFE_ID.fullmatch(test_run_id) is not None, "Agent test API returned an invalid run id")
        self._run_ids.append(test_run_id)
        return test_run_id

    def _execute_run(
        self,
        agent_id: str,
        commit_sha: str,
        *,
        expected_status: str,
        expected_error_code: str | None = None,
    ) -> JsonDict:
        test_run_id = self._create_run(agent_id, commit_sha)
        run = self._wait_for_terminal(test_run_id)
        self._validate_terminal_run(
            run,
            expected_status=expected_status,
            expected_error_code=expected_error_code,
        )
        self._assert_no_run_residue(test_run_id)
        return run

    def _execute_cancelled_run(self, agent_id: str, commit_sha: str) -> JsonDict:
        test_run_id = self._create_run(agent_id, commit_sha)
        self._wait_until_running(test_run_id)
        self._wait_for_running_sandbox(test_run_id)
        self._request_json("POST", f"/api/agent-test-runs/{test_run_id}/cancel")
        run = self._wait_for_terminal(test_run_id)
        self._validate_terminal_run(run, expected_status="cancelled")
        self._assert_no_run_residue(test_run_id)
        return run

    def _execute_source_changed_run(self, agent_id: str, commit_sha: str) -> JsonDict:
        test_run_id = self._create_run(agent_id, commit_sha)
        self._wait_until_running(test_run_id)
        self._wait_for_running_sandbox(test_run_id)
        self._mutate_projected_source(test_run_id)
        run = self._wait_for_terminal(test_run_id)
        self._validate_terminal_run(run, expected_status="error", expected_error_code="AGENT_TEST_SOURCE_CHANGED", expected_source_observation="changed")
        passed = self._request_json(
            "GET",
            "/api/agent-test-runs/history",
            params={"agent_id": agent_id, "commit_sha": commit_sha, "status": "passed", "limit": "1"},
        ).get("items")
        _require(passed == [], "source-changed run became eligible as latest passed")
        self._assert_no_run_residue(test_run_id)
        return run

    def _mutate_projected_source(self, test_run_id: str) -> None:
        pre_digest = hashlib.sha256(_SOURCE_CHANGE_TEST_BYTES).hexdigest()
        post_digest = hashlib.sha256(SOURCE_CHANGED_PROJECTED_BYTES).hexdigest()
        program = (
            "import hashlib,os,sys; from pathlib import Path; "
            "p=Path('/agent-test-runs')/sys.argv[1]/'workspace/tests/test_source_change.py'; "
            "b=p.read_bytes(); assert hashlib.sha256(b).hexdigest()==sys.argv[2]; "
            "os.chmod(p,0o644); p.write_bytes(bytes.fromhex(sys.argv[3])); "
            "print(hashlib.sha256(p.read_bytes()).hexdigest())"
        )
        observed = self._docker_runner(
            [
                "docker",
                "exec",
                self.settings.worker_container,
                "/usr/local/bin/python",
                "-c",
                program,
                test_run_id,
                pre_digest,
                SOURCE_CHANGED_PROJECTED_BYTES.hex(),
            ]
        )
        _require(observed == post_digest, "source-change projection mutation was not observed by the worker")

    def _worker_run_path_present(self, test_run_id: str) -> bool:
        program = "import sys; from pathlib import Path; p=Path('/agent-test-runs')/sys.argv[1]; print('present' if p.exists() or p.is_symlink() else 'absent')"
        state = self._docker_runner(["docker", "exec", self.settings.worker_container, "/usr/local/bin/python", "-c", program, test_run_id])
        _require(state in {"present", "absent"}, "worker run residue evidence is invalid")
        return state == "present"

    def _wait_until_running(self, test_run_id: str) -> JsonDict:
        deadline = self._clock() + 45.0
        while self._clock() < deadline:
            run = self._request_json("GET", f"/api/agent-test-runs/{test_run_id}")
            status = run.get("status")
            if status == "running":
                return run
            if status in TERMINAL_STATUSES:
                raise AcceptanceError("cancel scenario terminated before a running claim was observed")
            _require(status == "queued", "Agent test run returned an unknown active status")
            self._sleep(self.settings.poll_seconds)
        raise AcceptanceError("cancel scenario did not reach running before the deadline")

    def _wait_for_running_sandbox(self, test_run_id: str) -> None:
        deadline = self._clock() + 30.0
        while self._clock() < deadline:
            raw_ids = self._docker_runner(
                [
                    "docker",
                    "ps",
                    "-q",
                    "--filter",
                    f"label={SANDBOX_RUN_LABEL}={test_run_id}",
                ]
            )
            container_ids = tuple(item for item in raw_ids.splitlines() if item.strip())
            if len(container_ids) == 1:
                return
            _require(len(container_ids) == 0, "cancel scenario found multiple running sandboxes")
            current = self._request_json("GET", f"/api/agent-test-runs/{test_run_id}")
            if current.get("status") in TERMINAL_STATUSES:
                raise AcceptanceError("cancel scenario terminated before its sandbox started")
            self._sleep(self.settings.poll_seconds)
        raise AcceptanceError("cancel scenario sandbox did not start before the deadline")

    def _wait_for_terminal(self, test_run_id: str, *, timeout_seconds: float | None = None) -> JsonDict:
        deadline = self._clock() + (timeout_seconds or self.settings.run_timeout_seconds)
        while self._clock() < deadline:
            run = self._request_json("GET", f"/api/agent-test-runs/{test_run_id}")
            status = run.get("status")
            if status in TERMINAL_STATUSES:
                return run
            _require(status in ACTIVE_STATUSES, "Agent test run returned an unknown status")
            self._sleep(self.settings.poll_seconds)
        raise AcceptanceError("Agent test run did not reach a terminal state before the deadline")

    def _validate_terminal_run(
        self,
        run: JsonDict,
        *,
        expected_status: str,
        expected_error_code: str | None = None,
        expected_source_observation: Literal["stable", "changed"] = "stable",
    ) -> None:
        actual_status = run.get("status")
        if actual_status != expected_status:
            error = run.get("error") if isinstance(run.get("error"), dict) else {}
            error_code = error.get("error_code") if isinstance(error.get("error_code"), str) else "none"
            error_message = error.get("message") if isinstance(error.get("message"), str) else "none"
            safe_error_message = re.sub(r"[^A-Za-z0-9 _():.-]", "?", error_message)[:160]
            raise AcceptanceError(
                f"Agent test run terminal mismatch: expected={expected_status}, actual={actual_status}, error_code={error_code}, operation={safe_error_message}"
            )
        raw_receipt = run.get("receipt")
        try:
            receipt = AgentTestExecutionReceipt.model_validate(raw_receipt)
        except ValidationError as exc:
            raise AcceptanceError("Agent test run returned an invalid typed receipt") from exc
        _require(verify_receipt_integrity(receipt), "Agent test receipt digest is invalid")
        _require(receipt.test_run_id == run.get("test_run_id"), "Agent test receipt is bound to another run")
        _require(receipt.result.status == expected_status, "Agent test receipt has the wrong terminal status")
        _require(receipt.target.agent_id == run.get("agent_id"), "Agent test receipt has the wrong Agent")
        _require(receipt.target.commit_sha == run.get("commit_sha"), "Agent test receipt has the wrong commit")
        _require(receipt.target.suite_digest == run.get("suite_digest"), "Agent test receipt has the wrong suite digest")
        _require(receipt.target.source_digest == run.get("source_digest"), "Agent test receipt has the wrong source digest")
        _require(receipt.target.tree_sha == run.get("source_tree_sha"), "Agent test receipt has the wrong source tree")
        _require_source_observation(receipt, expected_source_observation)
        _require(receipt.cleanup.complete, "Agent test receipt cleanup is incomplete")
        _require(receipt.container_id is not None, "Agent test receipt lacks a sandbox container id")
        if receipt.invocation is None or receipt.isolation is None:
            raise AcceptanceError("Agent test receipt lacks sandbox evidence")
        report = run.get("report") if isinstance(run.get("report"), dict) else {}
        stdout = run.get("stdout") if isinstance(run.get("stdout"), str) else ""
        stderr = run.get("stderr") if isinstance(run.get("stderr"), str) else ""
        _require(receipt.assurance_level == "execution_provenance", "Agent test receipt overstates its assurance level")
        _require(
            receipt.result.workspace_report_authority == "agent_owned_unverified",
            "Agent test receipt overstates workspace report authority",
        )
        _require(
            receipt.result.workspace_report_digest == canonical_json_digest(report),
            "Agent test receipt workspace report digest is invalid",
        )
        _require(receipt.result.stdout_digest == canonical_json_digest(stdout), "Agent test receipt stdout digest is invalid")
        _require(receipt.result.stderr_digest == canonical_json_digest(stderr), "Agent test receipt stderr digest is invalid")
        _require(receipt.result.exit_code == run.get("exit_code"), "Agent test receipt exit code disagrees with the run")
        if expected_status == "cancelled":
            _require(receipt.result.exit_code is not None, "cancel scenario did not terminate a started sandbox")
        self._assert_current_acceptance_image(receipt.invocation.image_id)

        error = run.get("error") if isinstance(run.get("error"), dict) else {}
        if expected_error_code is not None:
            _require(error.get("error_code") == expected_error_code, "Agent test run returned the wrong stable error code")
        elif expected_status in {"passed", "failed", "cancelled"}:
            _require(not error, "Agent test run unexpectedly returned an error payload")

        items = run.get("items")
        if not isinstance(items, list):
            raise AcceptanceError("Agent test run items are missing")
        if expected_status == "passed":
            _require(bool(items), "passed Agent test run has no collected leaves")
            _require(
                all(isinstance(item, dict) and item.get("outcome") == "passed" for item in items),
                "passed Agent test run contains a non-passed leaf",
            )
        if expected_status == "failed":
            _require(any(isinstance(item, dict) and item.get("outcome") == "failed" for item in items), "failed run has no failed leaf")

    def _assert_current_acceptance_image(self, image_id: str) -> None:
        value = self._docker_runner(["docker", "image", "inspect", "--format", f'{{{{index .Config.Labels "{ACCEPTANCE_IMAGE_LABEL}"}}}}', image_id])
        _require(value == self.settings.acceptance_run_id, "sandbox image does not belong to the current acceptance run")

    @staticmethod
    def _require_expected_nodeids(run: JsonDict, *, expected: frozenset[str]) -> None:
        projected_items = run.get("items")
        report = run.get("report")
        report_items = report.get("items") if isinstance(report, dict) else None
        if not isinstance(projected_items, list) or not isinstance(report_items, list):
            raise AcceptanceError("hostile suite result items are missing")
        projected_nodeids = {str(item.get("nodeid")) for item in projected_items if isinstance(item, dict)}
        report_nodeids = {str(item.get("nodeid")) for item in report_items if isinstance(item, dict)}
        _require(projected_nodeids == expected, "hostile suite projected the wrong test leaves")
        _require(report_nodeids == expected, "hostile suite receipt report projected the wrong test leaves")

    def _assert_no_run_residue(self, test_run_id: str) -> None:
        _require(_SAFE_ID.fullmatch(test_run_id) is not None, "run residue audit received an invalid id")
        container_ids = self._docker_runner(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label={SANDBOX_RUN_LABEL}={test_run_id}",
            ]
        )
        _require(not container_ids, "sandbox container label residue remains after the run")
        _require(not self._worker_run_path_present(test_run_id), "sandbox temporary run directory remains after the run")

    def _create_host_marker(self) -> Path:
        marker = self.settings.runtime_root / f"host-boundary-probe-{uuid.uuid4().hex}"
        try:
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
        except OSError as exc:
            raise AcceptanceError("host boundary marker could not be created") from exc
        self._host_marker = marker
        acceptance_support.create_volume_canaries(
            worker_container=self.settings.worker_container,
            docker_runner=self._docker_runner,
        )
        self._volume_canaries_created = True
        return marker


def run_verification(environ: Mapping[str, str] | None = None) -> VerificationSummary:
    settings = AcceptanceSettings.from_environment(environ if environ is not None else os.environ)
    acceptance = AgentTestContainerAcceptance(settings)
    result: VerificationSummary | None = None
    primary_error: AcceptanceError | None = None
    try:
        result = acceptance.verify()
    except AcceptanceError as exc:
        primary_error = exc
    except KeyboardInterrupt:
        primary_error = AcceptanceError("verification was interrupted")
    except Exception as exc:  # noqa: BLE001 - public output must stay sanitized
        primary_error = AcceptanceError(f"verification failed with {exc.__class__.__name__}")
    cleanup_errors = acceptance.cleanup()
    if primary_error is not None:
        if cleanup_errors:
            raise AcceptanceError(f"{primary_error}; cleanup also failed") from primary_error
        raise primary_error
    if cleanup_errors:
        raise AcceptanceError("temporary acceptance cleanup failed")
    _require(result is not None, "verification produced no result")
    return result


def main() -> int:
    try:
        result = run_verification()
    except AcceptanceError as exc:
        print(f"AGENT_TEST_CONTAINER_E2E_FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"AGENT_TEST_CONTAINER_E2E_OK runs={result.runs} terminal_contracts={result.terminal_contracts} temporary_agents={result.temporary_agents}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
