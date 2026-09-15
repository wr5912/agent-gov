from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import (
    run_selected_env_operation as runner,
)
from scripts import (
    selected_env_operation_contract as contract,
)
from scripts import (
    selected_env_source_snapshot as source_snapshot,
)
from scripts.container_acceptance_toolchain import _resolve_node
from scripts.selected_env_reexec import frozen_command

_SOURCE_DIGEST = "a" * 64
_IMAGE_ID = "sha256:" + "b" * 64


def test_runtime_recreate_is_mutating_frozen_start_with_one_service() -> None:
    assert "runtime-recreate" in contract.OPERATIONS
    assert "runtime-recreate" in contract.START_OPERATIONS
    assert "runtime-recreate" in contract.DOCKER_BIND_OPERATIONS
    assert "runtime-recreate" in contract.DOCKER_MUTATING_OPERATIONS
    assert "runtime-recreate" in contract.PREFLIGHT_OPERATIONS
    assert contract.IMAGE_SCOPES["runtime-recreate"] == ("agentscope-runtime",)
    assert contract.simple_operation_commands(["docker", "compose"], [])["runtime-recreate"] == [
        "docker",
        "compose",
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "--wait",
        "--pull",
        "never",
        "--no-build",
        "agentscope-runtime",
    ]


def test_runtime_recreate_requires_current_selected_env_epoch(tmp_path: Path) -> None:
    selected = tmp_path / "selected.env"
    selected.write_text("AGENTGOV_API_MODE=drain\nAPI_KEY=key\nFRONTEND_RUNTIME_API_KEY=key\n", encoding="utf-8")

    with pytest.raises(contract.SelectedEnvError, match="AGENTGOV_API_MODE=open"):
        contract.require_current_epoch_env(selected, "runtime-recreate")


def test_runtime_recreate_rejects_unverified_idle_state_before_starting_commands(tmp_path: Path) -> None:
    selected = tmp_path / "selected.env"
    selected.write_text("AGENTGOV_API_MODE=open\n", encoding="utf-8")
    with pytest.raises(contract.SelectedEnvError, match="无法只读核验部署环境活动任务"):
        runner._execute_operation(
            "runtime-recreate",
            selected,
            tmp_path,
            tmp_path,
            {},
            no_build=False,
            force_recreate=False,
            require_idle=True,
        )


def test_runtime_recreate_idle_opt_in_is_preserved_by_frozen_cli_and_rejected_for_other_operations(tmp_path: Path) -> None:
    args = (tmp_path, tmp_path / "selected.env", tmp_path, "runtime-recreate")
    assert "--require-idle" not in frozen_command(*args, no_build=False, force_recreate=False)
    assert frozen_command(*args, no_build=False, force_recreate=False, require_idle=True)[-1] == "--require-idle"
    result = subprocess.run(
        [sys.executable, "scripts/run_selected_env_operation.py", "--env-file", str(tmp_path / "absent.env"), "--operation", "up", "--require-idle"],
        cwd=runner.REPO_ROOT,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "--require-idle 只允许用于 runtime-recreate" in result.stderr


def test_candidate_runtime_restart_rejects_unsafe_signals_in_real_node_process() -> None:
    node, _version = _resolve_node(dict(os.environ), error_type=contract.SelectedEnvError)
    result = subprocess.run(
        [str(node), "--test", "tests/candidate_runtime_restart.test.mjs"],
        cwd=runner.REPO_ROOT,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_runtime_recreate_attests_only_current_runtime_image_before_compose(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    identity = {"id": "daemon"}
    child_env = {source_snapshot.SOURCE_DIGEST_ENV: _SOURCE_DIGEST}
    monkeypatch.setattr(runner, "_capture_local_daemon", lambda _env: identity)
    monkeypatch.setattr(runner, "_verify_local_daemon", lambda _env, _identity, image: calls.append(("daemon", image)))

    def verify_images(*_args, **kwargs) -> dict[str, str]:
        calls.append(("images", (kwargs["services"], kwargs["running"])))
        return {"agentscope-runtime": _IMAGE_ID}

    monkeypatch.setattr(runner, "_verify_stack_images", verify_images)
    monkeypatch.setattr(
        runner.persistent_source,
        "materialize",
        lambda *_args, **_kwargs: calls.append(("materialize", None)),
    )

    assert runner._prepare_daemon_boundary(
        "runtime-recreate",
        tmp_path / "selected.env",
        tmp_path,
        child_env,
        "4.0.1",
        _SOURCE_DIGEST,
    ) == (identity, runner.HOST_FILESYSTEM_PROBE_IMAGE, {"agentscope-runtime": _IMAGE_ID})
    assert calls == [
        ("daemon", runner.HOST_FILESYSTEM_PROBE_IMAGE),
        ("images", (("agentscope-runtime",), False)),
        ("materialize", None),
    ]


def test_runtime_recreate_rejects_stale_image_before_materializing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_capture_local_daemon", lambda _env: {"id": "daemon"})
    monkeypatch.setattr(runner, "_verify_local_daemon", lambda *_args: None)
    monkeypatch.setattr(
        runner,
        "_verify_stack_images",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(runner.SelectedEnvError("本地镜像不属于当前源码摘要")),
    )
    monkeypatch.setattr(
        runner.persistent_source,
        "materialize",
        lambda *_args, **_kwargs: pytest.fail("stale image must not be materialized"),
    )

    with pytest.raises(runner.SelectedEnvError, match="当前源码摘要"):
        runner._prepare_daemon_boundary(
            "runtime-recreate",
            tmp_path / "selected.env",
            tmp_path,
            {source_snapshot.SOURCE_DIGEST_ENV: _SOURCE_DIGEST},
            "4.0.1",
            _SOURCE_DIGEST,
        )


def test_runtime_recreate_attests_running_runtime_without_deleting_api_cas(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        runner,
        "_verify_stack_images",
        lambda *_args, **kwargs: calls.append(("images", (kwargs["services"], kwargs["running"], kwargs["expected_ids"]))) or {"agentscope-runtime": _IMAGE_ID},
    )
    monkeypatch.setattr(runner, "_verify_local_daemon", lambda _env, _identity, image: calls.append(("daemon", image)))
    monkeypatch.setattr(
        runner.persistent_source,
        "cleanup_obsolete",
        lambda *_args, **_kwargs: pytest.fail("running API may still mount the prior source CAS"),
    )

    runner._verify_daemon_after_operation(
        "runtime-recreate",
        tmp_path / "selected.env",
        tmp_path,
        {},
        "4.0.1",
        _SOURCE_DIGEST,
        {"id": "daemon"},
        runner.HOST_FILESYSTEM_PROBE_IMAGE,
        {"agentscope-runtime": _IMAGE_ID},
    )

    assert calls == [
        ("images", (("agentscope-runtime",), True, {"agentscope-runtime": _IMAGE_ID})),
        ("daemon", _IMAGE_ID),
    ]


def test_public_runtime_recreate_target_uses_selected_env_runner() -> None:
    makefile = (runner.REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    recipe = makefile.split("\nruntime-recreate:\n", 1)[1].split("\n\n", 1)[0]
    assert "\t$(SELECTED_ENV_RUNNER) --operation runtime-recreate " in recipe
    assert "$(if $(filter 1,$(RUNTIME_RECREATE_REQUIRE_IDLE)),--require-idle,)" in recipe
    assert '""|0|1)' in recipe and "exit 2" in recipe
