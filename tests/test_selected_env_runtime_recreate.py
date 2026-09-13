from __future__ import annotations

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


def test_runtime_recreate_preflights_and_never_bootstraps_or_starts_api(tmp_path: Path, monkeypatch) -> None:
    selected = tmp_path / "selected.env"
    calls: list[object] = []
    monkeypatch.setattr(runner, "_preflight", lambda *_args: calls.append("preflight"))
    monkeypatch.setattr(runner, "_bootstrap", lambda *_args: pytest.fail("Runtime-only operation must not bootstrap"))
    monkeypatch.setattr(runner, "_prepare_harnesses", lambda *_args: pytest.fail("Runtime-only operation must not run API"))
    monkeypatch.setattr(runner, "_run", lambda command, _env: calls.append(command) or 0)

    assert (
        runner._execute_operation(
            "runtime-recreate",
            selected,
            tmp_path,
            tmp_path,
            {},
            no_build=False,
            force_recreate=False,
        )
        == 0
    )

    assert calls[0] == "preflight"
    assert calls[1] == [
        *runner._compose(selected, tmp_path),
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
    assert "runtime-recreate:" in makefile
    assert "\t$(SELECTED_ENV_RUNNER) --operation runtime-recreate\n" in makefile
