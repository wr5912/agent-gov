#!/usr/bin/env python3
"""通过公共部署与 API 验收真实 per-Session Runtime Workspace 回收。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, NotRequired, TypeAlias, TypedDict, cast
from urllib.parse import quote

import httpx

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.agentscope_atomic_cutover import load_env_file
from scripts.agentscope_atomic_cutover_bootstrap import source_artifact_sha256
from scripts.agentscope_live_acceptance_report import BindingEvidence
from scripts.agentscope_live_acceptance_scenarios import LiveAcceptanceError
from scripts.agentscope_live_native_chat import NativeChatAttempt, submit_native_chat
from scripts.run_agentscope_live_acceptance import _validate_canonical_replies, _validate_terminal_run
from scripts.run_self_use_acceptance import (
    DeployedContainerEvidence,
    StackEvidence,
    _refresh_deployment,
    _stack_evidence,
    _verify_selected_inputs,
)
from scripts.selected_env_deployed_context import deployment_urls
from scripts.workspace_reclaim_acceptance_runtime import (
    AcceptanceFailure,
    ArtifactIdentityReport,
    ArtifactReport,
    ReclaimedPathsReport,
    RuntimeMount,
    RuntimeWatchdog,
    SessionArtifact,
    SidecarPhase,
    TreeUsageReport,
    artifact_report,
    capture_tombstone,
    disarm_runtime_watchdog,
    kill_runtime,
    require_same_artifact_identity,
    resume_runtime,
    runtime_mount,
    runtime_python_pid,
    runtime_restart_count,
    sha256_text,
    validate_artifact,
    wait_reclaimed,
    wait_runtime_restart,
)
from scripts.workspace_reclaim_acceptance_sessions import (
    TrackedSession,
    cleanup_tracked_sessions,
    create_tracked_session,
    delete_status,
    materialize_session,
    public_session_absence,
    require_public_absence,
    session_rows,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
SCOPE: Final = "runtime_session_workspace_reclaim"
GOVERNANCE_AGENT_ID: Final = "security-operations-expert"
SHA256_PATTERN: Final = re.compile(r"[0-9a-f]{64}")


class DeleteStatusCounts(TypedDict):
    http_204: int
    http_404: int


class ChildEnvironment(dict[str, str]):
    """Only the selected deployment process environment crosses this boundary."""


class BasicReclaimReport(TypedDict):
    status: Literal["failed", "passed"]
    statuses: NotRequired[DeleteStatusCounts]
    repeat_status: NotRequired[int]
    session_absent: NotRequired[Literal[True]]
    workspace_unreferenced: NotRequired[Literal[True]]
    reclaimed: NotRequired[ArtifactReport]


class CrashRecoveryReport(TypedDict):
    status: Literal["not_run", "not_proven", "passed"]
    attempts: NotRequired[int]
    signal_sequence: NotRequired[list[str]]
    restart_count_before: NotRequired[int]
    restart_count_after: NotRequired[int]
    captured_phase: NotRequired[SidecarPhase]
    external_watchdog_armed: NotRequired[Literal[True]]
    same_container_restarted: NotRequired[Literal[True]]
    runtime_pid_changed: NotRequired[Literal[True]]
    repeat_status: NotRequired[int]
    reclaimed: NotRequired[ArtifactReport]


class SurvivorTurnReport(TypedDict):
    run_sha256: str
    trace_sha256: str
    reply_identity_sha256: str
    reply_count: int
    run_succeeded: Literal[True]
    binding_exact: Literal[True]
    session_exact: Literal[True]
    all_replies_persisted: Literal[True]
    canonical_assistant_completed: Literal[True]
    canonical_text_nonempty: Literal[True]


class NativeTextBlock(TypedDict):
    type: Literal["text"]
    text: str


class NativeSurvivorInput(TypedDict):
    id: str
    name: Literal["user"]
    role: Literal["user"]
    content: list[NativeTextBlock]


class SurvivorReport(TypedDict):
    session_sha256: str
    workspace_sha256: str
    workspace_usage_before_dialogues: TreeUsageReport
    workspace_usage_after_dialogues: TreeUsageReport
    venv_usage_before_dialogues: TreeUsageReport
    venv_usage_after_dialogues: TreeUsageReport
    immutable_identity: ArtifactIdentityReport
    dialogues: list[SurvivorTurnReport]


class ExerciseReport(TypedDict):
    basic_reclaim: BasicReclaimReport
    crash_recovery: CrashRecoveryReport
    cleanup_status: Literal["failed", "passed"]
    survivor: NotRequired[SurvivorReport]
    survivor_final_delete_status: NotRequired[int]
    survivor_final_reclaimed: NotRequired[ArtifactReport]


class ServiceEvidence(TypedDict):
    container_sha256: str
    image_sha256: str
    source_sha256: str


class DeploymentReport(TypedDict):
    services: dict[str, ServiceEvidence]
    runtime_workspace_mount_sha256: str
    runtime_container_sha256: str
    runtime_image_sha256: str


class AcceptanceReport(TypedDict):
    scope: str
    status: Literal["failed", "passed"]
    stage: str
    failure_code: NotRequired[str]
    source_sha256: NotRequired[str]
    selected_env_sha256: NotRequired[str]
    deployment: NotRequired[DeploymentReport]
    basic_reclaim: NotRequired[BasicReclaimReport]
    crash_recovery: NotRequired[CrashRecoveryReport]
    cleanup_status: NotRequired[Literal["failed", "passed"]]
    survivor: NotRequired[SurvivorReport]
    survivor_final_delete_status: NotRequired[int]
    survivor_final_reclaimed: NotRequired[ArtifactReport]


Binding: TypeAlias = BindingEvidence


@dataclass(frozen=True)
class Configuration:
    values: dict[str, str]
    api_base: str
    api_key: str


@dataclass
class ExerciseState:
    created_sessions: dict[str, TrackedSession]
    stopped_runtime_pid: int | None = None
    runtime_watchdog: RuntimeWatchdog | None = None


@dataclass(frozen=True)
class CrashAttemptOutcome:
    captured_phase: SidecarPhase | None
    restart_count_before: int | None
    restart_count: int | None
    same_container_restarted: bool
    runtime_pid_changed: bool
    repeated_status: int
    reclaimed_paths: ReclaimedPathsReport


def _json_payload(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError as exc:
        raise AcceptanceFailure("PUBLIC_API_RESPONSE_INVALID") from exc


def _require_status(response: httpx.Response, expected: set[int], code: str) -> None:
    if response.status_code not in expected:
        raise AcceptanceFailure(code)


def _configuration(env_file: Path, environ: Mapping[str, str]) -> Configuration:
    if environ.get("REQUIRE_LIVE_RUNTIME") != "1":
        raise AcceptanceFailure("EXPLICIT_LIVE_OPT_IN_REQUIRED")
    try:
        values = load_env_file(env_file.resolve(strict=True))
        _ui_base, api_base = deployment_urls(values)
    except (OSError, ValueError, RuntimeError) as exc:
        raise AcceptanceFailure("PUBLIC_DEPLOYMENT_FAILED") from exc
    api_key = values.get("API_KEY", "").strip()
    if not api_key:
        raise AcceptanceFailure("PRIVATE_API_KEY_REQUIRED")
    return Configuration(values, api_base.rstrip("/"), api_key)


def _child_environment(env_file: Path, environ: Mapping[str, str]) -> ChildEnvironment:
    child = ChildEnvironment({key: environ[key] for key in ("HOME", "LANG", "LC_ALL", "PATH", "TMPDIR") if key in environ})
    child.update(
        {
            "COMPOSE_ENV_FILE": str(env_file),
            "PYTHON": str(Path(sys.executable).absolute()),
            "REQUIRE_LIVE_RUNTIME": "1",
        }
    )
    return child


async def _binding(client: httpx.AsyncClient) -> Binding:
    response = await client.get(f"/api/runtime/agents/{quote(GOVERNANCE_AGENT_ID, safe='')}/current")
    _require_status(response, {200}, "INVALID_AGENT_BINDING")
    payload = _json_payload(response)
    if not isinstance(payload, dict):
        raise AcceptanceFailure("INVALID_AGENT_BINDING")
    fields = tuple(payload.get(key) for key in ("runtime_agent_id", "agent_version_id", "harness_digest"))
    if (
        payload.get("governance_agent_id") != GOVERNANCE_AGENT_ID
        or payload.get("provisioned") is not True
        or not all(isinstance(value, str) and value for value in fields)
        or not isinstance(fields[2], str)
        or SHA256_PATTERN.fullmatch(fields[2]) is None
    ):
        raise AcceptanceFailure("INVALID_AGENT_BINDING")
    runtime_agent_id, version_id, digest = cast(tuple[str, str, str], fields)
    return Binding(GOVERNANCE_AGENT_ID, runtime_agent_id, version_id, digest)


_session_rows = session_rows
_require_public_absence = require_public_absence
_delete_status = delete_status


def _require_concurrent_delete_statuses(statuses: tuple[int, ...]) -> None:
    if 204 not in statuses or any(status not in {204, 404} for status in statuses):
        raise AcceptanceFailure("CONCURRENT_DELETE_FAILED")


def _native_survivor_input(step: int) -> NativeSurvivorInput:
    return NativeSurvivorInput(
        id=f"workspace-reclaim-survivor-{step}-{uuid.uuid4().hex}",
        name="user",
        role="user",
        content=[NativeTextBlock(type="text", text="你好，请用一句话确认当前会话可正常响应。")],
    )


async def _run_survivor_dialogue(
    client: httpx.AsyncClient,
    binding: Binding,
    artifact: SessionArtifact,
    step: int,
) -> SurvivorTurnReport:
    try:
        attempt = NativeChatAttempt()
        run_id = await submit_native_chat(
            client,
            binding,
            artifact.session_id,
            cast(dict[str, object], _native_survivor_input(step)),
            attempt,
            timeout_seconds=180.0,
        )
        terminal = await _validate_terminal_run(
            client,
            run_id,
            session_id=artifact.session_id,
            binding=binding,
            timeout_seconds=180.0,
        )
        response = await client.get(
            f"/api/runtime/sessions/{quote(artifact.session_id, safe='')}/messages",
            params={"agent_id": binding.runtime_agent_id, "limit": 200},
        )
        _require_status(response, {200}, "SURVIVOR_DIALOGUE_FAILED")
        payload = _json_payload(response)
        messages = payload.get("messages") if isinstance(payload, dict) else None
        _validate_canonical_replies(messages, terminal.reply_ids)
    except (LiveAcceptanceError, httpx.HTTPError) as exc:
        raise AcceptanceFailure("SURVIVOR_DIALOGUE_FAILED") from exc
    reply_digest = sha256_text(json.dumps(terminal.reply_ids, separators=(",", ":")))
    return SurvivorTurnReport(
        run_sha256=sha256_text(run_id),
        trace_sha256=sha256_text(terminal.trace_id),
        reply_identity_sha256=reply_digest,
        reply_count=len(terminal.reply_ids),
        run_succeeded=True,
        binding_exact=True,
        session_exact=True,
        all_replies_persisted=True,
        canonical_assistant_completed=True,
        canonical_text_nonempty=True,
    )


async def _continue_survivor(
    client: httpx.AsyncClient,
    binding: Binding,
    artifact: SessionArtifact,
    step: int,
) -> tuple[SessionArtifact, SurvivorTurnReport]:
    dialogue = await _run_survivor_dialogue(client, binding, artifact, step)
    status_response = await client.get(
        f"/api/runtime/sessions/{quote(artifact.session_id, safe='')}/workspace/status",
        params={"agent_id": binding.runtime_agent_id},
        timeout=120.0,
    )
    _require_status(status_response, {200}, "SURVIVOR_SESSION_DAMAGED")
    refreshed = await asyncio.to_thread(
        validate_artifact,
        artifact.target.parent,
        artifact.session_id,
        artifact.workspace_id,
        binding.harness_digest,
    )
    require_same_artifact_identity(artifact, refreshed)
    return refreshed, dialogue


async def _concurrent_delete(
    client: httpx.AsyncClient,
    binding: Binding,
    artifact: SessionArtifact,
    mount: RuntimeMount,
    acceptance_name: str,
) -> BasicReclaimReport:
    start = asyncio.Event()

    async def delete_after_start() -> int:
        await start.wait()
        return await _delete_status(client, binding, artifact.session_id)

    tasks = tuple(asyncio.create_task(delete_after_start()) for _ in range(2))
    start.set()
    statuses = tuple(await asyncio.gather(*tasks))
    _require_concurrent_delete_statuses(statuses)
    reclaimed_paths = await wait_reclaimed(mount, artifact)
    session_absent, workspace_unreferenced = await public_session_absence(
        client,
        binding.governance_agent_id,
        artifact,
        acceptance_name,
    )
    repeated = await _delete_status(client, binding, artifact.session_id)
    if repeated not in {204, 404}:
        raise AcceptanceFailure("REPEAT_DELETE_FAILED")
    return BasicReclaimReport(
        status="passed",
        statuses=DeleteStatusCounts(http_204=statuses.count(204), http_404=statuses.count(404)),
        repeat_status=repeated,
        session_absent=session_absent,
        workspace_unreferenced=workspace_unreferenced,
        reclaimed=artifact_report(artifact, reclaimed_paths),
    )


async def _wait_api_ready(client: httpx.AsyncClient, timeout_seconds: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        try:
            response = await client.get("/health/ready", timeout=10.0)
            if response.status_code == 200:
                return
        except httpx.RequestError:
            pass
        await asyncio.sleep(0.25)
    raise AcceptanceFailure("PUBLIC_API_NOT_READY")


async def _crash_attempt(
    client: httpx.AsyncClient,
    binding: Binding,
    mount: RuntimeMount,
    handle: TrackedSession,
    artifact: SessionArtifact,
    state: ExerciseState,
) -> CrashAttemptOutcome:
    runtime_pid = runtime_python_pid(mount.container_id)
    restart_count_before = runtime_restart_count(mount.container_id)
    delete_task = asyncio.create_task(_delete_status(client, binding, artifact.session_id))
    try:
        captured = await capture_tombstone(
            artifact,
            mount,
            runtime_pid,
            restart_count_before,
            delete_task,
            (binding.runtime_agent_id, artifact.session_id),
        )
        state.stopped_runtime_pid = captured.runtime_pid if captured is not None else None
        state.runtime_watchdog = captured.watchdog if captured is not None else None
        if captured is None:
            status = await delete_task
            if status != 204:
                raise AcceptanceFailure("CRASH_RECOVERY_FAILED")
            reclaimed_paths = await wait_reclaimed(mount, artifact)
            await public_session_absence(client, binding.governance_agent_id, artifact, handle.name)
            return CrashAttemptOutcome(None, None, None, False, False, status, reclaimed_paths)
        kill_runtime(captured.runtime_pid)
        restart = await wait_runtime_restart(mount, captured.runtime_pid, restart_count_before)
        await _wait_api_ready(client)
        reclaimed_paths = await wait_reclaimed(mount, artifact)
        await public_session_absence(client, binding.governance_agent_id, artifact, handle.name)
        request_status = await delete_task
        if request_status not in {0, 204, 404, 502, 503, 504}:
            raise AcceptanceFailure("CRASH_RECOVERY_FAILED")
        repeated = await _delete_status(client, binding, artifact.session_id)
        if repeated not in {204, 404}:
            raise AcceptanceFailure("REPEAT_DELETE_FAILED")
        if restart.runtime_pid == captured.runtime_pid:
            raise AcceptanceFailure("RUNTIME_RESTART_FAILED")
        return CrashAttemptOutcome(
            captured.phase,
            restart_count_before,
            restart.restart_count,
            True,
            restart.runtime_pid != captured.runtime_pid,
            repeated,
            reclaimed_paths,
        )
    finally:
        resume_runtime(state.stopped_runtime_pid)
        state.stopped_runtime_pid = None
        if state.runtime_watchdog is not None:
            await asyncio.to_thread(disarm_runtime_watchdog, state.runtime_watchdog)
            state.runtime_watchdog = None
        if not delete_task.done():
            delete_task.cancel()
            with suppress(asyncio.CancelledError):
                await delete_task


async def _crash_recovery(
    client: httpx.AsyncClient,
    binding: Binding,
    mount: RuntimeMount,
    state: ExerciseState,
) -> CrashRecoveryReport:
    for attempt in range(1, 4):
        handle, artifact = await _new_artifact(client, binding, mount, state, f"crash-{attempt}")
        outcome = await _crash_attempt(client, binding, mount, handle, artifact, state)
        state.created_sessions.pop(handle.idempotency_key, None)
        if outcome.captured_phase is None:
            continue
        if await _binding(client) != binding:
            raise AcceptanceFailure("PUBLISHED_BINDING_CHANGED")
        return CrashRecoveryReport(
            status="passed",
            attempts=attempt,
            signal_sequence=["WATCHDOG_READY", "SIGSTOP", "SIGKILL"],
            restart_count_before=cast(int, outcome.restart_count_before),
            restart_count_after=cast(int, outcome.restart_count),
            captured_phase=outcome.captured_phase,
            external_watchdog_armed=True,
            same_container_restarted=cast(Literal[True], outcome.same_container_restarted),
            runtime_pid_changed=cast(Literal[True], outcome.runtime_pid_changed),
            repeat_status=outcome.repeated_status,
            reclaimed=artifact_report(artifact, outcome.reclaimed_paths),
        )
    return CrashRecoveryReport(status="not_proven", attempts=3, signal_sequence=[])


async def _cleanup_sessions(client: httpx.AsyncClient, binding: Binding, mount: RuntimeMount, state: ExerciseState) -> bool:
    resume_runtime(state.stopped_runtime_pid)
    state.stopped_runtime_pid = None
    if state.runtime_watchdog is not None:
        await asyncio.to_thread(disarm_runtime_watchdog, state.runtime_watchdog)
        state.runtime_watchdog = None
    return await cleanup_tracked_sessions(client, binding, mount, state.created_sessions)


async def _new_artifact(
    client: httpx.AsyncClient,
    binding: Binding,
    mount: RuntimeMount,
    state: ExerciseState,
    label: str,
) -> tuple[TrackedSession, SessionArtifact]:
    handle = await create_tracked_session(client, binding, mount, state.created_sessions, label)
    artifact = await materialize_session(client, binding, handle, mount)
    return handle, artifact


def _survivor_report(
    initial: SessionArtifact,
    final: SessionArtifact,
    dialogues: list[SurvivorTurnReport],
) -> SurvivorReport:
    return SurvivorReport(
        session_sha256=sha256_text(final.session_id),
        workspace_sha256=sha256_text(final.workspace_id),
        workspace_usage_before_dialogues=initial.workspace_usage.report(),
        workspace_usage_after_dialogues=final.workspace_usage.report(),
        venv_usage_before_dialogues=initial.venv_usage.report(),
        venv_usage_after_dialogues=final.venv_usage.report(),
        immutable_identity=require_same_artifact_identity(initial, final),
        dialogues=dialogues,
    )


async def _exercise(api_base: str, api_key: str, mount: RuntimeMount) -> ExerciseReport:
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    state = ExerciseState(created_sessions={})
    report = ExerciseReport(
        basic_reclaim=BasicReclaimReport(status="failed"),
        crash_recovery=CrashRecoveryReport(status="not_run"),
        cleanup_status="failed",
    )
    binding: Binding | None = None
    async with httpx.AsyncClient(
        base_url=api_base,
        headers=headers,
        timeout=httpx.Timeout(60.0, connect=10.0),
        trust_env=False,
    ) as client:
        try:
            await _wait_api_ready(client)
            binding = await _binding(client)
            survivor_handle, survivor = await _new_artifact(client, binding, mount, state, "survivor")
            survivor_initial = survivor
            reclaimed_handle, reclaimed = await _new_artifact(client, binding, mount, state, "concurrent")
            if survivor.workspace_id == reclaimed.workspace_id or survivor.target == reclaimed.target:
                raise AcceptanceFailure("SESSION_WORKSPACE_INVALID")
            report["basic_reclaim"] = await _concurrent_delete(
                client,
                binding,
                reclaimed,
                mount,
                reclaimed_handle.name,
            )
            state.created_sessions.pop(reclaimed_handle.idempotency_key, None)
            survivor, first_dialogue = await _continue_survivor(client, binding, survivor, step=1)
            if await _binding(client) != binding:
                raise AcceptanceFailure("PUBLISHED_BINDING_CHANGED")
            dialogues = [first_dialogue]
            report["crash_recovery"] = await _crash_recovery(client, binding, mount, state)
            if report["crash_recovery"]["status"] == "passed":
                survivor, second_dialogue = await _continue_survivor(client, binding, survivor, step=2)
                dialogues.append(second_dialogue)
                if await _binding(client) != binding:
                    raise AcceptanceFailure("PUBLISHED_BINDING_CHANGED")
            report["survivor"] = _survivor_report(survivor_initial, survivor, dialogues)
            final_status = await _delete_status(client, binding, survivor.session_id)
            if final_status != 204:
                raise AcceptanceFailure("PUBLIC_CLEANUP_FAILED")
            final_reclaimed_paths = await wait_reclaimed(mount, survivor)
            await public_session_absence(
                client,
                binding.governance_agent_id,
                survivor,
                survivor_handle.name,
            )
            state.created_sessions.pop(survivor_handle.idempotency_key, None)
            report["survivor_final_delete_status"] = final_status
            report["survivor_final_reclaimed"] = artifact_report(survivor, final_reclaimed_paths)
        finally:
            if binding is not None:
                report["cleanup_status"] = "passed" if await _cleanup_sessions(client, binding, mount, state) else "failed"
    return report


def _service_evidence(container: DeployedContainerEvidence, source_sha: str) -> ServiceEvidence:
    identifier = container.get("id")
    image = container.get("image")
    if not isinstance(identifier, str) or not isinstance(image, str):
        raise AcceptanceFailure("RUNTIME_CONTAINER_INVALID")
    return ServiceEvidence(
        container_sha256=sha256_text(identifier),
        image_sha256=sha256_text(image),
        source_sha256=source_sha,
    )


def _deployment_report(stack: StackEvidence, source_sha: str, mount: RuntimeMount) -> DeploymentReport:
    services = {name: _service_evidence(container, source_sha) for name, container in sorted(stack.items())}
    return DeploymentReport(
        services=services,
        runtime_workspace_mount_sha256=mount.root_sha256,
        runtime_container_sha256=mount.container_sha256,
        runtime_image_sha256=mount.image_sha256,
    )


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    return parser.parse_args(argv)


def _safe_code(error: BaseException) -> str:
    if isinstance(error, AcceptanceFailure):
        return str(error)
    if isinstance(error, KeyboardInterrupt):
        return "ACCEPTANCE_INTERRUPTED"
    return "UNEXPECTED_ACCEPTANCE_FAILURE"


def _deploy(env_file: Path, source_sha: str, env_sha: str) -> StackEvidence:
    progress: dict[str, object] = {}
    try:
        _refresh_deployment(env_file, _child_environment(env_file, os.environ), source_sha, env_sha, progress)
        return _stack_evidence(env_file, source_sha)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise AcceptanceFailure("PUBLIC_DEPLOYMENT_FAILED") from exc


@contextmanager
def _termination_interrupts() -> Iterator[None]:
    """把 SIGINT/SIGTERM 统一转为可执行 async finally 的中断。"""

    previous = {selected: signal.getsignal(selected) for selected in (signal.SIGINT, signal.SIGTERM)}

    def interrupt(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    try:
        for selected in previous:
            signal.signal(selected, interrupt)
        yield
    finally:
        for selected, handler in previous.items():
            signal.signal(selected, handler)


def _run_acceptance(args: argparse.Namespace, report: AcceptanceReport) -> bool:
    env_file = args.env_file.resolve(strict=True)
    configuration = _configuration(env_file, os.environ)
    if args.preflight_only:
        print("WORKSPACE_RECLAIM_ACCEPTANCE_PREFLIGHT_OK")
        return True
    env_sha = hashlib.sha256(env_file.read_bytes()).hexdigest()
    source_sha = source_artifact_sha256(REPO_ROOT)
    report["stage"] = "deployment"
    stack = _deploy(env_file, source_sha, env_sha)
    mount = runtime_mount(stack["agentscope-runtime"], configuration.values)
    report.update(
        source_sha256=source_sha,
        selected_env_sha256=env_sha,
        deployment=_deployment_report(stack, source_sha, mount),
        stage="public_api",
    )
    exercise = asyncio.run(_exercise(configuration.api_base, configuration.api_key, mount))
    report.update(exercise)
    _verify_selected_inputs(env_file, source_sha, env_sha)
    after = _stack_evidence(env_file, source_sha)
    if after["agent-gov-api"] != stack["agent-gov-api"] or after["agent-gov-ui"] != stack["agent-gov-ui"]:
        raise AcceptanceFailure("DEPLOYED_SOURCE_CHANGED")
    report.update(status="passed", stage="completed")
    if exercise["crash_recovery"]["status"] != "passed":
        report.update(status="failed", failure_code="CRASH_RECOVERY_NOT_PROVEN")
    if exercise["cleanup_status"] != "passed":
        raise AcceptanceFailure("PUBLIC_CLEANUP_FAILED")
    return False


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    report = AcceptanceReport(scope=SCOPE, status="failed", stage="preflight")
    try:
        with _termination_interrupts():
            if _run_acceptance(args, report):
                return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, httpx.HTTPError, KeyboardInterrupt) as error:
        report.update(status="failed", failure_code=_safe_code(error))
    print(json.dumps(report, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
