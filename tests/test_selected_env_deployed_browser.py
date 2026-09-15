"""部署浏览器入口的纯契约、真实文件与真实 SQLite 回归；不替代 live 验收。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from scripts.agentscope_atomic_cutover import active_work_counts
from scripts.container_acceptance_toolchain import _resolve_node
from scripts.selected_env_browser_toolchain import BROWSER_TOOLCHAIN_ENV, verify_browser_toolchain
from scripts.selected_env_deployed_browser import decode_browser_result
from scripts.selected_env_deployed_context import (
    CONTEXT_PATH_ENV,
    DeployedBrowserContext,
    deployment_urls,
    load_context,
    require_idle_counts,
    require_idle_database,
    require_live_opt_in,
    seal_context,
)
from scripts.selected_env_operation_contract import (
    BUILD_OPERATIONS,
    DEPLOYED_BROWSER_OPERATIONS,
    DOCKER_BIND_OPERATIONS,
    SOURCE_FREEZE_OPERATIONS,
    START_OPERATIONS,
    SelectedEnvError,
)

from runtime_gateway_test_utils import store_with_agent_version

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("value", [None, "", "0", "true", " 1", "1 "])
def test_deployed_browser_requires_exact_explicit_live_opt_in(value: str | None) -> None:
    with pytest.raises(SelectedEnvError, match="REQUIRE_LIVE_RUNTIME"):
        require_live_opt_in({} if value is None else {"REQUIRE_LIVE_RUNTIME": value})
    require_live_opt_in({"REQUIRE_LIVE_RUNTIME": "1"})


@pytest.mark.parametrize(
    "values",
    [
        {"HOST_PORT": "50399"},
        {"FRONTEND_HOST_PORT": "50500"},
        {"FRONTEND_HOST_PORT": "50400"},
        {"HOST_PORT": "50400/redirect"},
        {"API_BIND_IP": "0.0.0.0"},
        {"FRONTEND_RUNTIME_API_BASE": "http://remote.invalid:50400"},
    ],
)
def test_deployed_browser_rejects_nonlocal_or_mismatched_frontend_api(values: dict[str, str]) -> None:
    with pytest.raises(SelectedEnvError):
        deployment_urls(values)
    assert deployment_urls({}) == ("http://localhost:50401", "http://localhost:50400")
    assert deployment_urls({"HOST_PORT": "50499"}) == ("http://localhost:50401", "http://localhost:50499")


def _sealed_context(tmp_path: Path, operation: str = "ui-playground-deployed-smoke") -> tuple[DeployedBrowserContext, dict[str, str]]:
    context = DeployedBrowserContext(
        operation=operation,
        acceptance_id="deployed-contract",
        source_sha256="a" * 64,
        selected_env_sha256="b" * 64,
        version="4.0.1",
        ui_base="http://localhost:50401",
        api_base="http://localhost:50400",
        image_ids={},
        container_ids=(),
        runner_pid=os.getpid(),
        browser_toolchain_sha256=hashlib.sha256(b"{}").hexdigest(),
        daemon_identity={"endpoint": "unix:///var/run/docker.sock", "id": "contract", "socket_device": 0, "socket_inode": 0, "socket_uid": 0, "socket_mode": 0},
    )
    environment = {"REQUIRE_LIVE_RUNTIME": "1", BROWSER_TOOLCHAIN_ENV: "{}"}
    environment.update(seal_context(tmp_path / "receipt", context))
    return context, environment


def test_deployed_context_binds_real_private_inode_bytes_and_toolchain(tmp_path: Path) -> None:
    context, environment = _sealed_context(tmp_path)
    assert asdict(load_context(environment)) == {**asdict(context), "container_ids": []}
    path = Path(environment[CONTEXT_PATH_ENV])
    assert path.stat().st_mode & 0o777 == 0o400
    assert path.parent.stat().st_mode & 0o777 == 0o500
    for changed in ({**environment, BROWSER_TOOLCHAIN_ENV: "[]"}, {**environment, "REQUIRE_LIVE_RUNTIME": "0"}):
        with pytest.raises(SelectedEnvError):
            load_context(changed)
    path.chmod(0o600)
    path.write_bytes(path.read_bytes().replace(b"4.0.1", b"4.0.2"))
    path.chmod(0o400)
    with pytest.raises(SelectedEnvError, match="inode"):
        load_context(environment)
    path.parent.chmod(0o700)


def test_deployed_context_rejects_symlink_replacement_even_with_original_bytes(tmp_path: Path) -> None:
    _context, environment = _sealed_context(tmp_path)
    path = Path(environment[CONTEXT_PATH_ENV])
    path.parent.chmod(0o700)
    original = path.with_name("original.json")
    path.rename(original)
    path.symlink_to(original)
    path.parent.chmod(0o500)
    with pytest.raises(SelectedEnvError):
        load_context(environment)
    path.parent.chmod(0o700)


def test_deployed_browser_rejects_empty_or_malformed_toolchain(tmp_path: Path) -> None:
    root = tmp_path / "browser"
    root.mkdir(mode=0o500)
    for value in ("not-json", "[]", json.dumps({"root": str(root), "artifacts": {}})):
        with pytest.raises(SelectedEnvError):
            verify_browser_toolchain({BROWSER_TOOLCHAIN_ENV: value, "PLAYWRIGHT_BROWSERS_PATH": str(root / "browsers")})
    root.chmod(0o700)


def test_deployed_idle_gate_observes_real_sqlite_run_and_session_fence(tmp_path: Path) -> None:
    store = store_with_agent_version(tmp_path)
    store.bind_session(
        session_id="deployed-contract-session",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
    )
    database = tmp_path / "runtime.db"
    require_idle_counts(active_work_counts(database))
    run = store.begin_run(
        session_id="deployed-contract-session",
        runtime_agent_id="runtime-a",
        input_value={"name": "user", "role": "user", "content": [{"type": "text", "text": "contract input"}]},
        entities={},
        metadata={},
    )
    counts = active_work_counts(database)
    assert counts["active_runs"] == 1 and counts["active_sessions"] == 1
    with pytest.raises(SelectedEnvError, match="force-recreate"):
        require_idle_counts(counts)
    store.fail_trigger(run.run_id, error={"code": "CONTRACT_NOT_TRIGGERED"})
    require_idle_counts(active_work_counts(database))


def test_deployed_idle_gate_rejects_symlink_to_real_idle_database(tmp_path: Path) -> None:
    store_with_agent_version(tmp_path)
    database = tmp_path / "runtime.db"
    require_idle_database(database)
    indirect = tmp_path / "indirect.db"
    indirect.symlink_to(database)
    with pytest.raises(SelectedEnvError, match="非符号链接"):
        require_idle_database(indirect)


@pytest.mark.parametrize("asset", ["tests/deployed_browser_ownership.test.mjs", "tests/playground_recovery_evidence.test.mjs"])
def test_deployed_browser_ownership_metadata_contract_uses_real_node(asset: str) -> None:
    node, _version = _resolve_node(dict(os.environ), error_type=SelectedEnvError)
    result = subprocess.run(
        [str(node), "--test", asset],
        cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("operation", DEPLOYED_BROWSER_OPERATIONS)
def test_deployed_public_operation_rejects_opt_out_before_reading_env(tmp_path: Path, operation: str) -> None:
    result = subprocess.run(
        [sys.executable, "scripts/run_selected_env_operation.py", "--env-file", str(tmp_path / "absent.env"), "--operation", operation],
        cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0 and "REQUIRE_LIVE_RUNTIME" in result.stderr
    assert "无法稳定读取" not in result.stderr


def test_deployed_guard_and_browser_reject_direct_execution_without_network() -> None:
    environment = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
    node, _version = _resolve_node(dict(os.environ), error_type=SelectedEnvError)
    for command in (
        [sys.executable, "scripts/verify_deployed_browser_context.py"],
        [str(node), "scripts/verify_playground_deployed.mjs"],
        [str(node), "scripts/verify_playground_recovery.mjs"],
    ):
        result = subprocess.run(command, cwd=REPO_ROOT, env=environment, capture_output=True, text=True, timeout=30)
        assert result.returncode != 0
        evidence = json.loads(result.stdout)
        assert evidence["status"] == "failed"
        assert "Traceback" not in result.stderr and not result.stderr.strip()


@pytest.mark.parametrize("operation", DEPLOYED_BROWSER_OPERATIONS)
def test_browser_scope_is_sealed_and_requires_existing_build_lock_boundary(tmp_path: Path, operation: str) -> None:
    context, environment = _sealed_context(tmp_path, operation)
    assert load_context(environment).operation == operation
    for operations in (BUILD_OPERATIONS, DOCKER_BIND_OPERATIONS, SOURCE_FREEZE_OPERATIONS, START_OPERATIONS):
        assert operation in operations
    scope, script = DEPLOYED_BROWSER_OPERATIONS[operation]
    assert (REPO_ROOT / script).is_file()
    result = {"scope": scope, "acceptance_id": context.acceptance_id, "status": "passed"}
    assert decode_browser_result(json.dumps(result), 0, context, "credential-sentinel") == result


def test_deployed_context_rejects_unregistered_operation_in_real_sealed_file(tmp_path: Path) -> None:
    _context, environment = _sealed_context(tmp_path, "arbitrary-script")
    with pytest.raises(SelectedEnvError, match="固定入口"):
        load_context(environment)


def test_recovery_not_proven_cannot_satisfy_old_scope_or_exit_success(tmp_path: Path) -> None:
    old_dir, recovery_dir = tmp_path / "old", tmp_path / "recovery"
    old_dir.mkdir()
    recovery_dir.mkdir()
    old, _old_environment = _sealed_context(old_dir)
    recovery, _recovery_environment = _sealed_context(recovery_dir, "ui-playground-deployed-recovery-smoke")
    result = {"scope": DEPLOYED_BROWSER_OPERATIONS[recovery.operation][0], "acceptance_id": recovery.acceptance_id, "status": "not_proven"}
    assert decode_browser_result(json.dumps(result), 2, recovery, "credential-sentinel") == result
    for context, code, payload in (
        (old, 2, result), (recovery, 0, result),
        (old, 2, {**result, "scope": DEPLOYED_BROWSER_OPERATIONS[old.operation][0]}),
        (recovery, 2, {**result, "status": "passed"}),
        (recovery, 2, {**result, "acceptance_id": "another-acceptance"}),
        (recovery, 2, {**result, "secret": "credential-sentinel"}),
    ):
        with pytest.raises(SelectedEnvError, match="元数据"):
            decode_browser_result(json.dumps(payload), code, context, "credential-sentinel")
