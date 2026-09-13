from __future__ import annotations

from pathlib import Path

import pytest
from scripts import run_selected_env_operation as runner
from scripts import selected_env_operation_contract as contract
from scripts import selected_env_source_snapshot as source_snapshot

_SOURCE_DIGEST = "a" * 64
_IMAGE_ID = "sha256:" + "b" * 64


def test_ui_recreate_is_scoped_frozen_start_without_implicit_build_or_dependencies() -> None:
    assert "ui-recreate" in contract.OPERATIONS
    assert "ui-recreate" in contract.START_OPERATIONS
    assert "ui-recreate" in contract.DOCKER_BIND_OPERATIONS
    assert "ui-recreate" in contract.DOCKER_MUTATING_OPERATIONS
    assert "ui-recreate" in contract.PREFLIGHT_OPERATIONS
    assert contract.IMAGE_SCOPES["ui-recreate"] == ("agent-gov-ui",)
    assert contract.simple_operation_commands(["docker", "compose"], [])["ui-recreate"] == [
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
        "agent-gov-ui",
    ]


def test_ui_recreate_rejects_non_current_selected_env(tmp_path: Path) -> None:
    selected = tmp_path / "selected.env"
    selected.write_text("AGENTGOV_API_MODE=drain\nAPI_KEY=key\nFRONTEND_RUNTIME_API_KEY=key\n", encoding="utf-8")

    with pytest.raises(contract.SelectedEnvError, match="AGENTGOV_API_MODE=open"):
        contract.require_current_epoch_env(selected, "ui-recreate")


def test_ui_recreate_preflights_without_starting_api_or_runtime(tmp_path: Path, monkeypatch) -> None:
    selected = tmp_path / "selected.env"
    calls: list[object] = []
    monkeypatch.setattr(runner, "_preflight", lambda *_args: calls.append("preflight"))
    monkeypatch.setattr(runner, "_bootstrap", lambda *_args: pytest.fail("UI-only operation must not bootstrap"))
    monkeypatch.setattr(runner, "_prepare_harnesses", lambda *_args: pytest.fail("UI-only operation must not run API"))
    monkeypatch.setattr(runner, "_run", lambda command, _env: calls.append(command) or 0)

    assert (
        runner._execute_operation(
            "ui-recreate",
            selected,
            tmp_path,
            tmp_path,
            {},
            no_build=False,
            force_recreate=False,
        )
        == 0
    )
    assert calls == ["preflight", [*runner._compose(selected, tmp_path), *contract.simple_operation_commands([], [])["ui-recreate"]]]


def test_ui_recreate_rejects_stale_ui_image_before_materializing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_capture_local_daemon", lambda _env: {"id": "daemon"})
    monkeypatch.setattr(runner, "_verify_local_daemon", lambda *_args: None)

    def reject_image(*_args, **kwargs) -> dict[str, str]:
        assert kwargs["services"] == ("agent-gov-ui",)
        assert kwargs["running"] is False
        raise runner.SelectedEnvError("本地镜像不属于当前源码摘要")

    monkeypatch.setattr(runner, "_verify_stack_images", reject_image)
    monkeypatch.setattr(
        runner.persistent_source,
        "materialize",
        lambda *_args, **_kwargs: pytest.fail("过期镜像不得物化 source CAS"),
    )

    with pytest.raises(runner.SelectedEnvError, match="当前源码摘要"):
        runner._prepare_daemon_boundary(
            "ui-recreate",
            tmp_path / "selected.env",
            tmp_path,
            {source_snapshot.SOURCE_DIGEST_ENV: _SOURCE_DIGEST},
            "4.0.1",
            _SOURCE_DIGEST,
        )


def test_ui_recreate_attests_running_ui_without_deleting_api_source_snapshot(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        runner,
        "_verify_stack_images",
        lambda *_args, **kwargs: calls.append(("images", (kwargs["services"], kwargs["running"], kwargs["expected_ids"]))) or {"agent-gov-ui": _IMAGE_ID},
    )
    monkeypatch.setattr(runner, "_verify_local_daemon", lambda _env, _identity, image: calls.append(("daemon", image)))
    monkeypatch.setattr(
        runner.persistent_source,
        "cleanup_obsolete",
        lambda *_args, **_kwargs: pytest.fail("API 容器可能仍引用旧 source CAS"),
    )

    runner._verify_daemon_after_operation(
        "ui-recreate",
        tmp_path / "selected.env",
        tmp_path,
        {},
        "4.0.1",
        _SOURCE_DIGEST,
        {"id": "daemon"},
        runner.HOST_FILESYSTEM_PROBE_IMAGE,
        {"agent-gov-ui": _IMAGE_ID},
    )

    assert calls == [
        ("images", (("agent-gov-ui",), True, {"agent-gov-ui": _IMAGE_ID})),
        ("daemon", _IMAGE_ID),
    ]


def test_public_ui_recreate_target_uses_selected_env_runner() -> None:
    makefile = (runner.REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "ui-recreate:" in makefile
    assert "\t$(SELECTED_ENV_RUNNER) --operation ui-recreate\n" in makefile
