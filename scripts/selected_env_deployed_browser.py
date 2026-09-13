"""同源构建、部署与真实验收；复用部署初始化，不发布或原地改写业务 Harness。"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from scripts.agentscope_atomic_cutover_env import parse_selected_env_bindings
from scripts.agentscope_atomic_cutover_types import DockerDaemonIdentity
from scripts.container_acceptance_identity import write_exclusive_file
from scripts.selected_env_browser_toolchain import BROWSER_TOOLCHAIN_ENV, browser_mutation_monitor, prepare_browser_toolchain
from scripts.selected_env_deployed_context import (
    API_KEY_ENV,
    OPERATION,
    SCOPE,
    BrowserMetadata,
    DeployedBrowserContext,
    deployment_urls,
    require_live_opt_in,
    require_no_active_work,
    seal_context,
    stack_container_ids,
    verify_context,
)
from scripts.selected_env_operation_contract import SelectedEnvError, StackImageIds


def require_operation_opt_in(operation: str, environ: Mapping[str, str]) -> None:
    if operation == OPERATION:
        require_live_opt_in(environ)


def run_frozen_command(operation: str, directory: Path, source_root: Path, child_env: dict[str, str], command: list[str]) -> int:
    from scripts import run_selected_env_operation as runner

    if operation != OPERATION:
        return runner._run(command, child_env)
    require_live_opt_in(os.environ)
    child_env.update(prepare_browser_toolchain(directory, source_root, dict(os.environ)))
    child_env["REQUIRE_LIVE_RUNTIME"] = "1"
    return runner._run(command, child_env)


def _deploy_stage(
    operation: str,
    snapshot: Path,
    source_root: Path,
    source_base: Path,
    child_env: dict[str, str],
    version: str,
    digest: str,
    locked_identity: Mapping[str, object] | None,
) -> StackImageIds | None:
    from scripts import run_selected_env_operation as runner

    identity, probe, image_ids = runner._prepare_daemon_boundary(
        operation,
        snapshot,
        source_root,
        child_env,
        version,
        digest,
        locked_identity,
    )
    try:
        require_no_active_work(snapshot)
        runner._execute_operation(
            operation,
            snapshot,
            source_root,
            source_base,
            child_env,
            no_build=operation == "all-up",
            force_recreate=operation == "all-up",
            daemon_identity=identity,
        )
        runner._verify_daemon_after_operation(
            operation,
            snapshot,
            source_root,
            child_env,
            version,
            digest,
            identity,
            probe,
            image_ids,
        )
    except BaseException:
        runner._verify_daemon_after_failure(child_env, identity, probe)
        raise
    return image_ids


def _run_browser(source_root: Path, child_env: dict[str, str]) -> BrowserMetadata:
    from scripts import run_selected_env_operation as runner

    verify_context(child_env, require_node_parent=False)
    with browser_mutation_monitor(child_env) as toolchain, runner._command_monitor(child_env):
        command = [toolchain["node"]["path"], str(source_root / "scripts/verify_playground_deployed.mjs")]
        with subprocess.Popen(
            command,
            cwd=source_root,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            try:
                stdout, _stderr = process.communicate(timeout=1200)
            except BaseException as exc:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                if isinstance(exc, subprocess.TimeoutExpired):
                    raise SelectedEnvError("部署浏览器验收超时；已停止本轮浏览器进程组") from exc
                raise
    verify_context(child_env, require_node_parent=False)
    try:
        if len(stdout.encode()) > 256_000:
            raise ValueError("oversized metadata")
        result = json.loads(stdout)
        if not isinstance(result, dict) or result.get("scope") != SCOPE:
            raise ValueError("invalid metadata")
        if result.get("status") not in {"passed", "failed"}:
            raise ValueError("invalid status")
        if process.returncode and result.get("status") != "failed":
            raise ValueError("inconsistent exit")
        if child_env[API_KEY_ENV] in stdout:
            raise ValueError("credential in metadata")
    except (ValueError, TypeError, KeyError) as exc:
        raise SelectedEnvError("部署浏览器返回无效或未脱敏的元数据") from exc
    return cast(BrowserMetadata, result)


def _browser_context(
    snapshot: Path,
    source_root: Path,
    child_env: dict[str, str],
    version: str,
    digest: str,
    acceptance_id: str,
    image_ids: StackImageIds,
    daemon_identity: DockerDaemonIdentity,
) -> DeployedBrowserContext:
    values = {item.key: item.value or "" for item in parse_selected_env_bindings(snapshot) if item.key}
    ui_base, api_base = deployment_urls(values)
    context = DeployedBrowserContext(
        acceptance_id=acceptance_id,
        source_sha256=digest,
        selected_env_sha256=hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        version=version,
        ui_base=ui_base,
        api_base=api_base,
        image_ids=image_ids,
        container_ids=stack_container_ids(snapshot, source_root, child_env),
        runner_pid=os.getpid(),
        daemon_identity=daemon_identity,
        browser_toolchain_sha256=hashlib.sha256(child_env[BROWSER_TOOLCHAIN_ENV].encode()).hexdigest(),
    )
    child_env.update(seal_context(snapshot.parent.parent / "deployed-browser-context", context))
    child_env[API_KEY_ENV] = values["API_KEY"]
    return context


def run_deployed_browser(
    snapshot: Path,
    source_root: Path,
    source_base: Path,
    child_env: dict[str, str],
    version: str,
    digest: str,
) -> int:
    from scripts import run_selected_env_operation as runner

    require_live_opt_in(child_env)
    acceptance_id = f"deployed-{uuid.uuid4()}"
    evidence_root = Path(tempfile.mkdtemp(prefix="agentgov-deployed-browser-evidence-"))
    evidence_root.chmod(0o700)
    report: BrowserMetadata = {
        "acceptance_id": acceptance_id,
        "scope": SCOPE,
        "status": "failed",
        "source_sha256": digest,
        "selected_env_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "version": version,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "stage": "preflight",
    }
    try:
        values = {item.key: item.value or "" for item in parse_selected_env_bindings(snapshot) if item.key}
        deployment_urls(values)
        require_no_active_work(snapshot)
        with browser_mutation_monitor(child_env), runner._daemon_mutation_lock(OPERATION, child_env) as identity:
            for stage in ("build", "all-up"):
                report["stage"] = stage
                images = _deploy_stage(stage, snapshot, source_root, source_base, child_env, version, digest, identity)
            if images is None or identity is None:
                raise SelectedEnvError("部署浏览器验收缺少已核验镜像/daemon 身份")
            context = _browser_context(snapshot, source_root, child_env, version, digest, acceptance_id, images, cast(DockerDaemonIdentity, identity))
            report.update({key: value for key, value in asdict(context).items() if key not in {"runner_pid", "browser_toolchain_sha256"}})
            report["stage"] = "browser"
            result = _run_browser(source_root, child_env)
            if result.get("acceptance_id") != acceptance_id:
                raise SelectedEnvError("浏览器结果不属于当前验收")
            report["browser_result"] = result
            report["stage"] = "postconditions"
            runner._verify_local_daemon(child_env, identity, next(iter(images.values())))
            runner._verify_frozen_stage_postconditions(snapshot, source_root, child_env, digest)
            if result.get("status") == "failed":
                raise SelectedEnvError("真实部署浏览器交互验收失败；仅保留脱敏元数据")
            report["status"] = "passed"
        return 0
    except BaseException as exc:
        report["status"] = "failed"
        report["failure_type"] = type(exc).__name__
        raise
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_exclusive_file(
            evidence_root / "report.json",
            json.dumps(report, ensure_ascii=False, sort_keys=True).encode(),
            error_type=SelectedEnvError,
            label="部署浏览器脱敏报告",
        )
        print(f"部署浏览器验收元数据报告: {evidence_root / 'report.json'}", flush=True)
