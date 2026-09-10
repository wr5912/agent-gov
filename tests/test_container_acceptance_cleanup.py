from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from scripts import run_container_acceptance as acceptance


@pytest.fixture
def isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(acceptance.tempfile, "gettempdir", lambda: str(tmp_path))
    parent = tmp_path / f"agentgov-acceptance-{acceptance.os.getuid()}-cleanup"
    parent.mkdir(mode=0o700)
    root = parent / "runtime-root"
    root.mkdir()
    return root


def test_cleanup_removes_only_isolated_data_without_docker(isolated_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sibling = isolated_root.parent / "compose.acceptance.env"
    sibling.write_text("private-env", encoding="utf-8")
    (isolated_root / "data").mkdir()
    command = Mock()
    monkeypatch.setattr(acceptance, "_run_checked", command)

    acceptance.cleanup_runtime_root([], isolated_root, {})

    assert not isolated_root.exists()
    assert sibling.read_text(encoding="utf-8") == "private-env"
    command.assert_not_called()


@pytest.mark.parametrize("target", ["wrong-name", "outside-temp", "root-link", "parent-link", "public-parent"])
def test_cleanup_rejects_untrusted_roots(isolated_root: Path, monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    root = isolated_root
    if target == "wrong-name":
        root = root.parent / "data"
        root.mkdir()
    elif target == "outside-temp":
        root = root.parent.parent / "runtime-root"
        root.mkdir()
    elif target == "root-link":
        root.rmdir()
        root.symlink_to(root.parent.parent, target_is_directory=True)
    elif target == "parent-link":
        alias = root.parent.parent / f"agentgov-acceptance-{acceptance.os.getuid()}-alias"
        alias.symlink_to(root.parent, target_is_directory=True)
        root = alias / "runtime-root"
    else:
        root.parent.chmod(0o755)
    remove = Mock()
    command = Mock()
    monkeypatch.setattr(acceptance.shutil, "rmtree", remove)
    monkeypatch.setattr(acceptance, "_run_checked", command)

    with pytest.raises(acceptance.AcceptanceError, match="拒绝清理"):
        acceptance.cleanup_runtime_root([], root, {})

    remove.assert_not_called()
    command.assert_not_called()


def test_permission_repair_has_one_mount_and_no_network_or_secret_env(isolated_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remove = Mock(side_effect=[PermissionError(), None])
    command = Mock(side_effect=[json.dumps({"services": {"agent-gov-api": {"image": "local-api:acceptance-test"}}}), ""])
    monkeypatch.setattr(acceptance.shutil, "rmtree", remove)
    monkeypatch.setattr(acceptance, "_run_checked", command)

    acceptance.cleanup_runtime_root(["docker", "compose"], isolated_root, {"API_KEY": "do-not-inject"})

    assert remove.call_count == 2
    args = command.call_args_list[1].args[0]
    assert args.count("--mount") == 1
    assert args[args.index("--mount") + 1] == f"type=bind,src={isolated_root},dst=/acceptance-runtime"
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--pull") + 1] == "never"
    assert "--read-only" in args and "no-new-privileges" in args
    assert "--privileged" not in args and "--env" not in args and "do-not-inject" not in args
    assert "local-api:acceptance-test" in args
    assert "followlinks=False" in args[args.index("-c") + 1]


def test_cleanup_does_not_follow_child_symlinks(isolated_root: Path) -> None:
    outside = isolated_root.parent / "keep"
    outside.mkdir()
    (outside / "data").write_text("keep", encoding="utf-8")
    (isolated_root / "linked").symlink_to(outside, target_is_directory=True)

    acceptance.cleanup_runtime_root([], isolated_root, {})

    assert (outside / "data").read_text(encoding="utf-8") == "keep"


def test_failed_compose_down_never_removes_bound_data(isolated_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolation = acceptance.IsolatedEnvironment(isolated_root.parent / "env", isolated_root, "test", "test", {})
    monkeypatch.setattr(acceptance, "_run_checked", Mock(side_effect=acceptance.AcceptanceError("down failed")))
    cleanup = Mock()
    monkeypatch.setattr(acceptance, "cleanup_runtime_root", cleanup)

    with pytest.raises(acceptance.AcceptanceError, match="down failed"):
        acceptance.cleanup_profile(acceptance.PROFILES["core"], isolation, {})

    assert isolated_root.exists()
    cleanup.assert_not_called()


@pytest.mark.parametrize("failure", ["image", "repair", "remove"])
def test_cleanup_failures_do_not_report_success(isolated_root: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    monkeypatch.setattr(acceptance.shutil, "rmtree", Mock(side_effect=PermissionError()))
    config = json.dumps({"services": {"agent-gov-api": {"image": "local-api:acceptance-test"}}})
    effects = ["{}"] if failure == "image" else [config, acceptance.AcceptanceError("repair failed") if failure == "repair" else ""]
    monkeypatch.setattr(acceptance, "_run_checked", Mock(side_effect=effects))

    with pytest.raises(acceptance.AcceptanceError):
        acceptance.cleanup_runtime_root([], isolated_root, {})

    assert isolated_root.exists()


@pytest.mark.parametrize("phase", ["prepare", "refresh", "finished"])
def test_cleanup_oserror_does_not_hide_the_original_failure(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], phase: str
) -> None:
    source_env = isolated_root.parent.parent / "source.env"
    source_env.write_text("", encoding="utf-8")
    monkeypatch.setattr(acceptance, "LOCK_FILE", source_env.parent / "lock")
    monkeypatch.setattr(acceptance, "source_fingerprint", lambda _path: "stable")
    monkeypatch.setattr(acceptance.tempfile, "mkdtemp", lambda **_kwargs: str(isolated_root.parent))
    monkeypatch.setattr(acceptance, "_allocate_loopback_ports", lambda _count: tuple(range(50400, 50405)))
    monkeypatch.setattr(acceptance, "_bootstrap_isolated_runtime", Mock())
    monkeypatch.setattr(acceptance, "_run_child", Mock(return_value=0))
    if phase == "prepare":
        monkeypatch.setattr(acceptance, "prepare_isolated_environment", Mock(side_effect=acceptance.AcceptanceError("original failure")))
        monkeypatch.setattr(acceptance.shutil, "rmtree", Mock(side_effect=OSError("private path must not be printed")))
    else:
        refresh = Mock(side_effect=acceptance.AcceptanceError("original failure")) if phase == "refresh" else Mock()
        monkeypatch.setattr(acceptance, "refresh_profile", refresh)
        monkeypatch.setattr(acceptance, "cleanup_profile", Mock(side_effect=OSError("private path must not be printed")))

    expected = "隔离临时目录回收失败" if phase == "finished" else "original failure"
    with pytest.raises(acceptance.AcceptanceError, match=expected):
        acceptance.run_acceptance(acceptance.PROFILES["core"], source_env, ["true"], {})

    output = capsys.readouterr()
    assert "CONTAINER_ACCEPTANCE_OK" not in output.out
    assert "private path" not in output.err
    assert isolated_root.parent.exists()
