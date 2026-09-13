"""Continue formal acceptance only after the runner and dependencies were frozen."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from scripts.agentscope_atomic_cutover_bootstrap import source_artifact_sha256
from scripts.agentscope_atomic_cutover_daemon import CutoverDaemonSupport
from scripts.agentscope_atomic_cutover_env import read_stable_env_file, verify_stable_env_file
from scripts.container_acceptance_environment import AcceptanceProfile, IsolatedEnvironment
from scripts.container_acceptance_inputs import (
    ACCEPTANCE_TARGET_ENV,
    LIVE_SOURCE_ROOT_ENV,
    AcceptanceError,
    ScenarioFileSnapshot,
    acceptance_fingerprint,
    source_fingerprint,
)
from scripts.container_acceptance_materialization import ExecutionMutationGuard
from scripts.container_acceptance_reexec import REEXEC_GUARD_FD_ENV, REEXEC_LOCK_FD_ENV, REEXEC_STATE_ENV, decode_state
from scripts.container_acceptance_toolchain import FORMAL_SOURCE_ROOT_ENV, TOOL_PATH_ENV_KEYS, verify_acceptance_toolchain


class CleanupCallback(Protocol):
    def __call__(
        self,
        profile: AcceptanceProfile,
        temp_root: Path,
        isolation: IsolatedEnvironment | None,
        child_env: dict[str, str] | None,
        daemon_identity: dict[str, object] | None,
        *,
        compose_started: bool,
        input_guard: ExecutionMutationGuard | None,
        operation_error: BaseException | None,
    ) -> None: ...


@dataclass(frozen=True)
class FrozenRunnerActions:
    activate_guard: Callable[[ExecutionMutationGuard], None]
    run_checked: Callable[..., str]
    verify_plugins: Callable[[dict[str, str]], None]
    daemon_support: Callable[[dict[str, str]], CutoverDaemonSupport]
    verify_daemon: Callable[..., None]
    bootstrap: Callable[[IsolatedEnvironment, dict[str, str]], None]
    run_refreshed: Callable[
        [
            AcceptanceProfile,
            Path,
            list[str],
            IsolatedEnvironment,
            dict[str, str],
            dict[str, object],
            tuple[ScenarioFileSnapshot, ...],
            str,
            str,
        ],
        int,
    ]
    cleanup: CleanupCallback


def _announce_frozen_acceptance(
    profile: AcceptanceProfile,
    isolation: IsolatedEnvironment,
    child_env: dict[str, str],
) -> None:
    print(
        f"CONTAINER_ACCEPTANCE_REFRESH profile={profile.name} target={child_env[ACCEPTANCE_TARGET_ENV]} "
        f"run_id={child_env['AGENT_GOV_ACCEPTANCE_RUN_ID']} project={isolation.project_name} "
        "isolated=true frozen_reexec=true"
    )


def resume_frozen_acceptance(environ: dict[str, str], actions: FrozenRunnerActions) -> int:
    profile, env_file, command, isolation, snapshots, source_sha256, source_env_identity, input_guard = decode_state(
        environ,
        error_type=AcceptanceError,
    )
    child_env = dict(environ)
    for name in (REEXEC_STATE_ENV, REEXEC_GUARD_FD_ENV, REEXEC_LOCK_FD_ENV):
        child_env.pop(name, None)
    temp_root = isolation.runtime_root.parent
    live_root = Path(child_env[LIVE_SOURCE_ROOT_ENV])
    formal_root = Path(child_env[FORMAL_SOURCE_ROOT_ENV])
    protected = (isolation.env_file, isolation.runtime_root, isolation.source_root, formal_root)
    if any(not path.is_absolute() or not path.is_relative_to(temp_root) for path in protected):
        raise AcceptanceError("冻结 runner resume path 逃离本轮私有根")
    source_env_payload, current_identity = read_stable_env_file(env_file, error_type=AcceptanceError)
    if current_identity != source_env_identity:
        raise AcceptanceError("所选 Compose env 在 frozen re-exec 前发生变化")
    git_path = Path(child_env[TOOL_PATH_ENV_KEYS["git"]])
    source_digest = isolation.overrides["AGENTGOV_SOURCE_ARTIFACT_SHA256"]
    actions.activate_guard(input_guard)
    daemon_identity: dict[str, object] | None = None
    compose_started = False
    operation_error: BaseException | None = None
    try:
        input_guard.check()
        verify_acceptance_toolchain(child_env, error_type=AcceptanceError)
        if source_fingerprint(env_file, repo_root=live_root, git_path=git_path) != source_sha256:
            raise AcceptanceError("工作树或源 env 在 frozen re-exec 前发生变化")
        if source_artifact_sha256(live_root) != source_digest or source_artifact_sha256(isolation.source_root) != source_digest:
            raise AcceptanceError("frozen re-exec 未绑定同一 deployable source")
        actions.run_checked(
            [child_env[TOOL_PATH_ENV_KEYS["python"]], str(formal_root / "scripts/check_no_test_doubles.py")],
            env=child_env,
            label="验收 test-double 防伪扫描",
            cwd=formal_root,
        )
        actions.verify_plugins(child_env)
        daemon_identity = dict(actions.daemon_support(child_env).capture(child_env))
        actions.verify_daemon(child_env, daemon_identity, temp_root)
        verify_stable_env_file(env_file, source_env_payload, source_env_identity, error_type=AcceptanceError)
        initial_fingerprint = acceptance_fingerprint(
            env_file,
            isolation.env_file,
            child_env,
            snapshots,
            repo_root=live_root,
            git_path=git_path,
        )
        _announce_frozen_acceptance(profile, isolation, child_env)
        actions.bootstrap(isolation, child_env)
        compose_started = True
        returncode = actions.run_refreshed(
            profile,
            env_file,
            command,
            isolation,
            child_env,
            daemon_identity,
            snapshots,
            initial_fingerprint,
            source_sha256,
        )
    except BaseException as exc:
        operation_error = exc
        raise
    finally:
        actions.cleanup(
            profile,
            temp_root,
            isolation,
            child_env,
            daemon_identity,
            compose_started=compose_started,
            input_guard=input_guard,
            operation_error=operation_error,
        )
    if returncode == 0:
        print(f"CONTAINER_ACCEPTANCE_OK profile={profile.name} target={child_env[ACCEPTANCE_TARGET_ENV]} run_id={child_env['AGENT_GOV_ACCEPTANCE_RUN_ID']}")
    return returncode
