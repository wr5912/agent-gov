"""复用公共 Make 构建、强制重建，再驱动双 Agent 真实治理验收。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, NotRequired, TypeAlias, TypedDict, cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.agentscope_atomic_cutover import load_env_file
from scripts.agentscope_atomic_cutover_bootstrap import source_artifact_sha256
from scripts.agentscope_live_acceptance_scenarios import Scenario, load_scenarios
from scripts.selected_env_deployed_context import deployment_urls, require_no_active_work
from scripts.selected_env_operation_contract import OperationEnvironment

REPO_ROOT = Path(__file__).resolve().parents[1]
SCOPE = "self_use_dual_agent_governance"
_SAFE_FAILURE_CODES = frozenset(
    {
        "EXPLICIT_LIVE_OPT_IN_REQUIRED",
        "EXPECTED_SECURITY_OPERATIONS_AGENT_REQUIRED",
        "EXACT_EXISTING_DOCS_COMMIT_REQUIRED",
        "PRIVATE_API_KEY_REQUIRED",
        "LANGFUSE_REQUIRED_FOR_GOVERNANCE_EVIDENCE",
        "TWO_DISTINCT_AGENTS_REQUIRED",
        "PRIVATE_ACCEPTANCE_INPUT_REQUIRED",
        "ONE_IMPROVEMENT_SCENARIO_PER_AGENT_REQUIRED",
        "LOCAL_BROWSER_TOOLCHAIN_REQUIRED",
        "LOCAL_DOCKER_CLI_REQUIRED",
        "EXACT_DEPLOYMENT_CONTAINER_REQUIRED",
        "DEPLOYED_SOURCE_DOES_NOT_MATCH_CURRENT_TREE",
        "DEPLOYED_BROWSER_ENDPOINT_DOES_NOT_MATCH_CONTAINER",
        "SOURCE_CHANGED_DURING_ACCEPTANCE",
        "SELECTED_ENV_CHANGED_DURING_ACCEPTANCE",
        "SELECTED_ENV_CHANGED_DURING_INPUT_LOADING",
        "DEPLOYMENT_CHANGED_DURING_ACCEPTANCE",
        "ACCEPTANCE_REPORT_TOO_LARGE",
        "INVALID_ACCEPTANCE_REPORT",
        "UNSAFE_ACCEPTANCE_REPORT",
        "PUBLIC_BUILD_FAILED",
        "PUBLIC_ALL_UP_FAILED",
        "NEW_PRIVATE_REPORT_PATH_REQUIRED",
        "INVALID_RUNTIME_MAINTENANCE_RECEIPT",
    },
)


class GovernanceCaseInput(TypedDict):
    agentId: str
    scenario: Scenario
    scenarioFileSha256: str


class BrowserConnectionInput(TypedDict):
    uiBase: str
    apiBase: str
    apiKey: str
    actionTimeoutMs: int
    testRunTimeoutMs: int


class WorkspaceImportInput(TypedDict):
    agentId: str
    packagePath: str
    name: str
    expectedExistingCommitSha: str | None


class DualGovernancePlan(TypedDict):
    workspace: WorkspaceImportInput
    cases: list[GovernanceCaseInput]


class SelfUseBrowserInput(TypedDict):
    config: BrowserConnectionInput
    plan: DualGovernancePlan


class PublishedPortEvidence(TypedDict):
    HostIp: str
    HostPort: str


class DeployedContainerEvidence(TypedDict):
    id: str
    image: str
    running: bool
    source_sha256: str
    ports: dict[str, list[PublishedPortEvidence] | None] | None


StackEvidence: TypeAlias = dict[str, DeployedContainerEvidence]


class RuntimeMaintenanceReceipt(TypedDict):
    operation: Literal["runtime-recreate"]
    completed: Literal[True]
    stage: Literal["candidate_test"]


class RuntimeMaintenanceEvidence(TypedDict):
    runtime_recreated: bool
    runtime_container_before_id: str
    runtime_container_after_id: str


class SelfUseBrowserOutcome(TypedDict):
    """Python 消费的 Node 回执字段；其余逐 Agent 元数据仍由 Node 定义并原样落盘。"""

    scope: str
    status: Literal["passed", "failed"]
    runtime_maintenance: NotRequired[list[RuntimeMaintenanceReceipt]]


def _private_file(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or resolved.is_relative_to(REPO_ROOT):
        raise ValueError("PRIVATE_ACCEPTANCE_INPUT_REQUIRED")
    return resolved


def _case(path: Path, agent_id: str) -> GovernanceCaseInput:
    reviewed = load_scenarios(_private_file(path), expected_agent_id=agent_id)
    cases = [item for item in reviewed.scenarios if item.purpose == "improvement"]
    if len(cases) != 1:
        raise ValueError("ONE_IMPROVEMENT_SCENARIO_PER_AGENT_REQUIRED")
    return {"agentId": agent_id, "scenario": cases[0], "scenarioFileSha256": reviewed.sha256}


def _inputs(args: argparse.Namespace, environ: Mapping[str, str]) -> tuple[SelfUseBrowserInput, OperationEnvironment, str, str]:
    if environ.get("REQUIRE_LIVE_RUNTIME") != "1":
        raise ValueError("EXPLICIT_LIVE_OPT_IN_REQUIRED")
    if args.soc_agent_id != "security-operations-expert":
        raise ValueError("EXPECTED_SECURITY_OPERATIONS_AGENT_REQUIRED")
    if args.existing_docs_commit is not None and re.fullmatch(r"[0-9a-f]{40}", args.existing_docs_commit) is None:
        raise ValueError("EXACT_EXISTING_DOCS_COMMIT_REQUIRED")
    values = load_env_file(args.env_file.resolve(strict=True))
    if not values.get("API_KEY", "").strip():
        raise ValueError("PRIVATE_API_KEY_REQUIRED")
    ui_base, api_base = deployment_urls(values)
    if values.get("LANGFUSE_ENABLED", "").lower() not in {"1", "true"}:
        raise ValueError("LANGFUSE_REQUIRED_FOR_GOVERNANCE_EVIDENCE")
    if args.docs_agent_id == args.soc_agent_id:
        raise ValueError("TWO_DISTINCT_AGENTS_REQUIRED")
    package = _private_file(args.workspace_package)
    plan: DualGovernancePlan = {
        "workspace": {
            "agentId": args.docs_agent_id,
            "packagePath": str(package),
            "name": "项目文档助手",
            "expectedExistingCommitSha": args.existing_docs_commit,
        },
        "cases": [_case(args.docs_scenarios, args.docs_agent_id), _case(args.soc_scenarios, args.soc_agent_id)],
    }
    config: BrowserConnectionInput = {
        "uiBase": ui_base.rstrip("/"),
        "apiBase": api_base.rstrip("/"),
        "apiKey": values["API_KEY"],
        "actionTimeoutMs": 300_000,
        "testRunTimeoutMs": 900_000,
    }
    node = shutil.which("node")
    if node is None or not (REPO_ROOT / "frontend/node_modules/playwright").exists():
        raise ValueError("LOCAL_BROWSER_TOOLCHAIN_REQUIRED")
    child_env = {key: environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "PLAYWRIGHT_BROWSERS_PATH") if key in environ}
    # 保留 venv 解释器入口，不 resolve 到裸基础解释器而丢失项目依赖。
    child_env.update({"COMPOSE_ENV_FILE": str(args.env_file.resolve()), "PYTHON": str(Path(sys.executable).absolute()), "REQUIRE_LIVE_RUNTIME": "1"})
    # Provider/MCP 凭据不进入 Node 进程；API 凭据只从 stdin 传递，不放命令行。
    return {"config": config, "plan": plan}, child_env, node, values["API_KEY"]


def _browser_input_json(inputs: SelfUseBrowserInput) -> str:
    cases = []
    for case in inputs["plan"]["cases"]:
        scenario = asdict(case["scenario"])
        scenario["input"] = scenario.pop("input_text")
        cases.append({**case, "scenario": scenario})
    return json.dumps({"config": inputs["config"], "plan": {**inputs["plan"], "cases": cases}})


def _stack_evidence(env_file: Path, digest: str) -> StackEvidence:
    """只读精确项目的容器和镜像身份，不读取容器 env 或日志。"""
    values = load_env_file(env_file)
    project = values.get("COMPOSE_PROJECT_NAME", "agent-gov")
    docker_path = shutil.which("docker")
    if docker_path is None:
        raise ValueError("LOCAL_DOCKER_CLI_REQUIRED")
    # 当前部署默认 socket 已只读核对；不允许 ambient DOCKER_HOST 指向另一套 daemon。
    docker = [docker_path, "--host", "unix:///var/run/docker.sock"]
    docker_env = {name: os.environ[name] for name in ("PATH", "HOME", "LANG") if name in os.environ}
    expected_ports = {
        "agent-gov-api": (f"{values.get('API_PORT', '8080')}/tcp", values.get("HOST_PORT", "50400")),
        "agent-gov-ui": (f"{values.get('FRONTEND_PORT', '5173')}/tcp", values.get("FRONTEND_HOST_PORT", "50401")),
    }
    result: StackEvidence = {}
    template = (
        '{"id":{{json .Id}},"image":{{json .Image}},"running":{{json .State.Running}},'
        '"source_sha256":{{json (index .Config.Labels "io.agentgov.source-artifact-sha256")}},'
        '"ports":{{json .NetworkSettings.Ports}}}'
    )
    for service in ("agentscope-runtime", "agent-gov-api", "agent-gov-ui"):
        listing = subprocess.run(
            [
                *docker,
                "ps",
                "--no-trunc",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                f"label=com.docker.compose.service={service}",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            env=docker_env,
        ).stdout.splitlines()
        if len(listing) != 1 or re.fullmatch(r"[0-9a-f]{64}", listing[0]) is None:
            raise ValueError("EXACT_DEPLOYMENT_CONTAINER_REQUIRED")
        inspected = subprocess.run(
            [*docker, "inspect", "--format", template, listing[0]], capture_output=True, text=True, check=True, timeout=30, env=docker_env
        )
        container: DeployedContainerEvidence = json.loads(inspected.stdout)
        if container.get("source_sha256") != digest or container.get("running") is not True:
            raise ValueError("DEPLOYED_SOURCE_DOES_NOT_MATCH_CURRENT_TREE")
        if service in expected_ports:
            internal, published = expected_ports[service]
            if (container.get("ports") or {}).get(internal) != [{"HostIp": "127.0.0.1", "HostPort": published}]:
                raise ValueError("DEPLOYED_BROWSER_ENDPOINT_DOES_NOT_MATCH_CONTAINER")
        result[service] = container
    return result


def _run_child(command: list[str], child_env: dict[str, str], *, input_text: str | None = None, timeout_seconds: float = 5400) -> tuple[int, str]:
    with subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=child_env,
        cwd=REPO_ROOT,
        start_new_session=True,
    ) as child:
        try:
            stdout, _stderr = child.communicate(input=input_text, timeout=timeout_seconds)
        except BaseException:
            # 不先 poll 回收已退出的 leader：其后代仍可能占用管道，组身份应保留到清理后。
            if child.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
            raise
    return child.returncode, stdout


def _verify_selected_inputs(env_file: Path, source_sha: str, env_sha: str) -> None:
    if source_artifact_sha256(REPO_ROOT) != source_sha:
        raise ValueError("SOURCE_CHANGED_DURING_ACCEPTANCE")
    if hashlib.sha256(env_file.read_bytes()).hexdigest() != env_sha:
        raise ValueError("SELECTED_ENV_CHANGED_DURING_ACCEPTANCE")


def _browser_outcome(stdout: str, api_key: str) -> SelfUseBrowserOutcome:
    if len(stdout.encode()) > 1_000_000:
        raise ValueError("ACCEPTANCE_REPORT_TOO_LARGE")
    outcome = json.loads(stdout)
    if not isinstance(outcome, dict) or outcome.get("scope") != SCOPE or outcome.get("status") not in {"passed", "failed"}:
        raise ValueError("INVALID_ACCEPTANCE_REPORT")
    if api_key in json.dumps(outcome, ensure_ascii=False):
        raise ValueError("UNSAFE_ACCEPTANCE_REPORT")
    return cast(SelfUseBrowserOutcome, outcome)


def _verify_deployment_transition(before: StackEvidence, after: StackEvidence, maintenance: object) -> RuntimeMaintenanceEvidence:
    if not isinstance(maintenance, list) or len(maintenance) > 1:
        raise ValueError("INVALID_RUNTIME_MAINTENANCE_RECEIPT")
    for receipt in maintenance:
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {"operation", "completed", "stage"}
            or receipt["operation"] != "runtime-recreate"
            or receipt["completed"] is not True
            or receipt["stage"] != "candidate_test"
        ):
            raise ValueError("INVALID_RUNTIME_MAINTENANCE_RECEIPT")
    if set(before) != {"agentscope-runtime", "agent-gov-api", "agent-gov-ui"} or set(after) != set(before):
        raise ValueError("DEPLOYMENT_CHANGED_DURING_ACCEPTANCE")
    if any(before[service] != after[service] for service in ("agent-gov-api", "agent-gov-ui")):
        raise ValueError("DEPLOYMENT_CHANGED_DURING_ACCEPTANCE")
    original, current = before["agentscope-runtime"], after["agentscope-runtime"]
    changed = original["id"] != current["id"]
    if current != {**original, "id": current["id"]} or changed != bool(maintenance):
        raise ValueError("DEPLOYMENT_CHANGED_DURING_ACCEPTANCE")
    return {
        "runtime_recreated": changed,
        "runtime_container_before_id": original["id"],
        "runtime_container_after_id": current["id"],
    }


def _refresh_deployment(env_file: Path, child_env: dict[str, str], source_sha: str, env_sha: str, report: dict[str, object]) -> None:
    # 调用项目现有公共启动链，不复制 selected-env 冻结、镜像或部署实现。
    env_argument = f"COMPOSE_ENV_FILE={env_file}"
    commands = (
        ["/usr/bin/make", "--no-print-directory", "build", env_argument],
        ["/usr/bin/make", "--no-print-directory", "all-up", env_argument, "COMPOSE_UP_FLAGS=--force-recreate"],
    )
    for command, stage, failure_code in zip(commands, ("build", "all_up"), ("PUBLIC_BUILD_FAILED", "PUBLIC_ALL_UP_FAILED"), strict=True):
        report["stage"] = stage
        _verify_selected_inputs(env_file, source_sha, env_sha)
        require_no_active_work(env_file)
        returncode, _stdout = _run_child(command, child_env)
        _verify_selected_inputs(env_file, source_sha, env_sha)
        if returncode != 0:
            raise ValueError(failure_code)


def _safe_failure_code(error: BaseException) -> str:
    if isinstance(error, subprocess.TimeoutExpired):
        return "ACCEPTANCE_PROCESS_TIMEOUT"
    if isinstance(error, KeyboardInterrupt):
        return "ACCEPTANCE_INTERRUPTED"
    return str(error) if str(error) in _SAFE_FAILURE_CODES else "SELF_USE_ACCEPTANCE_FAILED"


def _verify_idle_after_work(env_file: Path, report: dict[str, object]) -> None:
    try:
        if hashlib.sha256(env_file.read_bytes()).hexdigest() != report["selected_env_sha256"]:
            raise ValueError("SELECTED_ENV_CHANGED_DURING_ACCEPTANCE")
        require_no_active_work(env_file.resolve())
        report["active_work_at_exit"] = "idle"
    except (OSError, ValueError, KeyError, RuntimeError):
        # 只读核验不确定归属的活动工作；不自动取消用户任务，不把关闭浏览器当作服务端终态。
        report["status"] = "failed"
        report["active_work_at_exit"] = "not_idle_or_unavailable"
        report["manual_follow_up_required"] = True


def _write_report(path: Path, report: dict[str, object]) -> bool:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def _report_path(path: Path) -> Path:
    parent = path.parent.resolve(strict=True)
    if parent.is_relative_to(REPO_ROOT) or parent.stat().st_mode & 0o077 or path.exists() or path.is_symlink():
        raise ValueError("NEW_PRIVATE_REPORT_PATH_REQUIRED")
    return parent / path.name


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("env-file", "workspace-package", "docs-scenarios", "soc-scenarios", "report"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--docs-agent-id", default="documentation-assistant-e2e")
    parser.add_argument("--soc-agent-id", default="security-operations-expert")
    parser.add_argument("--existing-docs-commit", type=lambda value: value or None, default=None)
    parser.add_argument("--preflight-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    report: dict[str, object] = {"scope": SCOPE, "status": "failed", "stage": "preflight", "started_at": datetime.now(timezone.utc).isoformat()}
    report_path = None
    operation_attempted = False
    try:
        report_path = _report_path(args.report)
        args.env_file = args.env_file.resolve(strict=True)
        report["selected_env_sha256"] = hashlib.sha256(args.env_file.read_bytes()).hexdigest()
        inputs, child_env, node, api_key = _inputs(args, os.environ)
        if hashlib.sha256(args.env_file.read_bytes()).hexdigest() != report["selected_env_sha256"]:
            raise ValueError("SELECTED_ENV_CHANGED_DURING_INPUT_LOADING")
        require_no_active_work(args.env_file.resolve())
        if args.preflight_only:
            print("SELF_USE_ACCEPTANCE_PREFLIGHT_OK")
            return 0
        source_sha = source_artifact_sha256(REPO_ROOT)
        report["source_sha256"] = source_sha
        operation_attempted = True
        _refresh_deployment(args.env_file, child_env, source_sha, str(report["selected_env_sha256"]), report)
        report["deployment_steps"] = ["build", "all-up --force-recreate"]
        # 实际注入由公共 Make build + all-up 核验；绑定同一源/env 输入和容器，不读取秘密值。
        report["deployment_check_scope"] = "source_labels_local_container_ports_and_unchanged_selected_input"
        report["stage"] = "deployment_before_browser"
        deployment = _stack_evidence(args.env_file, source_sha)
        report["deployment"] = deployment
        report["stage"] = "browser"
        command = [node, str(REPO_ROOT / "scripts/verify_self_use_governance.mjs")]
        returncode, stdout = _run_child(command, child_env, input_text=_browser_input_json(inputs))
        outcome = _browser_outcome(stdout, api_key)
        report["result"] = outcome
        report["stage"] = "deployment_after_browser"
        _verify_selected_inputs(args.env_file, source_sha, str(report["selected_env_sha256"]))
        after = _stack_evidence(args.env_file, source_sha)
        report.update(_verify_deployment_transition(deployment, after, outcome.get("runtime_maintenance", [])))
        report["status"] = "passed" if returncode == 0 and outcome["status"] == "passed" else "failed"
        report["stage"] = "completed" if report["status"] == "passed" else "browser"
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        report["failure_code"] = _safe_failure_code(error)
    finally:
        if operation_attempted:
            _verify_idle_after_work(args.env_file, report)
        if not args.preflight_only and report_path is not None:
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            if not _write_report(report_path, report):
                report["status"] = "failed"
    print("SELF_USE_ACCEPTANCE_PASSED" if report["status"] == "passed" else "SELF_USE_ACCEPTANCE_FAILED")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
