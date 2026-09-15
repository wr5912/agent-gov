from __future__ import annotations

import gzip
import hashlib
import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest
from scripts import remote_deploy_runtime as runtime
from scripts import remote_deploy_transaction as transaction

REPO_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def _binding() -> Iterator[runtime.DockerBinding]:
    yield _binding_value()


def _binding_value() -> runtime.DockerBinding:
    return runtime.DockerBinding(command=["/usr/bin/docker"], environment={"PATH": os.defpath})


def _state(
    tmp_path: Path,
    *,
    phase: transaction.DeployPhase = "activated",
    dependency: bool = False,
    old_images: bool = False,
    with_langfuse: bool = False,
) -> transaction.DeployTransaction:
    live = tmp_path / "live"
    (live / "docker").mkdir(parents=True, exist_ok=True)
    project = tmp_path / "project.tar.gz"
    project.write_bytes(b"project")
    dependency_path = tmp_path / "dependency.tar.gz"
    dependency_path.write_bytes(b"dependency")
    old_archive_path = tmp_path / "old.tar.gz"
    old_archive_path.write_bytes(b"old")
    images = (transaction.ImageIdentity("agent-gov-api:3.9.9", f"sha256:{'1' * 64}"),) if old_images else ()
    return transaction.DeployTransaction(
        transaction_id="runtime-transaction",
        phase=phase,
        live_root=live.as_posix(),
        stage_root=(tmp_path / "live.stage.runtime").as_posix(),
        backup_root=(tmp_path / "live.previous.runtime").as_posix(),
        toolchain_root=(tmp_path / "toolchain").as_posix(),
        source_sha256="a" * 64,
        version="4.0.1",
        with_langfuse=with_langfuse,
        project_archive=transaction.ArchiveIdentity(project.as_posix(), runtime._sha256(project)),
        dependency_archive=(transaction.ArchiveIdentity(dependency_path.as_posix(), runtime._sha256(dependency_path)) if dependency else None),
        preserved=(),
        moved=(),
        old_version="3.9.9",
        old_source_sha256="b" * 64,
        old_with_langfuse=with_langfuse,
        old_images=images,
        old_archive=(transaction.ArchiveIdentity(old_archive_path.as_posix(), runtime._sha256(old_archive_path)) if old_images else None),
    )


def _write_archive(path: Path, payload: bytes = b"archive") -> transaction.ArchiveIdentity:
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    path.with_name(f"{path.name}.sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return transaction.ArchiveIdentity(path.as_posix(), digest)


def test_digest_and_trusted_binary_boundaries(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    archive.write_bytes(b"payload")
    assert runtime._sha256(archive) == hashlib.sha256(b"payload").hexdigest()
    runtime._fsync_directory(tmp_path)
    with pytest.raises(runtime.RuntimeDeployError, match="普通非符号链接"):
        runtime._sha256(tmp_path)
    link = tmp_path / "archive-link"
    link.symlink_to(archive)
    with pytest.raises(runtime.RuntimeDeployError, match="普通非符号链接"):
        runtime._sha256(link)
    with pytest.raises(runtime.RuntimeDeployError, match="owner/mode"):
        runtime._trusted_regular_file((archive,), "fixture")
    with pytest.raises(runtime.RuntimeDeployError, match="不可用"):
        runtime._trusted_regular_file((tmp_path / "absent",), "fixture")


def test_bound_docker_uses_immutable_private_cli_and_plugin(tmp_path: Path) -> None:
    with runtime._bound_docker(tmp_path) as binding:
        cli = Path(binding["command"][0])
        plugin = Path(binding["environment"]["DOCKER_CONFIG"]) / "cli-plugins/docker-compose"
        assert cli.is_file() and plugin.is_file()
        assert stat.S_IMODE(cli.stat().st_mode) == 0o500
        assert stat.S_IMODE(plugin.stat().st_mode) == 0o500
        assert binding["environment"]["HOME"] == tmp_path.as_posix()
        assert binding["environment"]["DOCKER_HOST"] == "unix:///var/run/docker.sock"
        assert runtime._docker_output(binding, ("--version",)).startswith("Docker version")
        with pytest.raises(runtime.RuntimeDeployError, match="只读核验失败"):
            runtime._docker_output(binding, ("not-a-docker-command",))


@pytest.mark.parametrize(
    ("value", "expected"),
    [("x86_64", "amd64"), ("aarch64", "arm64"), ("ppc64le", "ppc64le")],
)
def test_architecture_normalization(value: str, expected: str) -> None:
    assert runtime._normalized_architecture(value) == expected


@pytest.mark.parametrize("version", ["", "../4.0.1", "4.0.1 invalid"])
def test_project_references_reject_invalid_versions(version: str) -> None:
    with pytest.raises(runtime.RuntimeDeployError, match="版本无效"):
        runtime._project_references(version)


def test_archive_identity_requires_a_matching_checksum(tmp_path: Path) -> None:
    archive = tmp_path / "images.tar.gz"
    expected = _write_archive(archive)
    assert runtime._archive_identity(archive) == expected
    archive.with_name(f"{archive.name}.sha256").write_text(f"{'0' * 64}\n", encoding="utf-8")
    with pytest.raises(runtime.RuntimeDeployError, match="checksum 不匹配"):
        runtime._archive_identity(archive)
    archive.with_name(f"{archive.name}.sha256").unlink()
    with pytest.raises(runtime.RuntimeDeployError, match="checksum 缺失"):
        runtime._archive_identity(archive)


def test_validate_archives_checks_project_and_dependency_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _write_archive(tmp_path / "project.tar.gz", b"project")
    dependency = _write_archive(tmp_path / "dependency.tar.gz", b"dependency")
    calls: list[tuple[Path, dict[str, object]]] = []
    monkeypatch.setattr(
        runtime,
        "validate_image_archive",
        lambda path, **kwargs: calls.append((path, kwargs)),
    )
    runtime._validate_archives(
        project=project,
        dependency=dependency,
        architecture="amd64",
        version="4.0.1",
        source_digest="a" * 64,
    )
    assert calls[0][1]["expected_images"] == frozenset(runtime._project_references("4.0.1"))
    assert calls[1][1]["forbidden_prefix"] == "agent-gov-"
    drifted = replace(project, sha256="0" * 64)
    with pytest.raises(runtime.RuntimeDeployError, match="项目镜像归档在事务期间变化"):
        runtime._validate_archives(
            project=drifted,
            dependency=None,
            architecture="amd64",
            version="4.0.1",
            source_digest="a" * 64,
        )


def test_compose_identity_env_and_local_langfuse(tmp_path: Path) -> None:
    live = tmp_path / "live"
    (live / "docker").mkdir(parents=True)
    env_file = live / "docker/.env"
    env_file.write_text(
        "# comment\nexport COMPOSE_PROJECT_NAME='agent_gov_local'\nLANGFUSE_ENABLED=yes\nLANGFUSE_BASE_URL=http://langfuse-web:3000/\nIGNORED=value\n",
        encoding="utf-8",
    )
    assert runtime._compose_project(live) == "agent_gov_local"
    assert runtime._local_langfuse(env_file)
    assert runtime._env_values(env_file, frozenset({"IGNORED"})) == {"IGNORED": "value"}
    env_file.write_text("COMPOSE_PROJECT_NAME=Invalid.Project\n", encoding="utf-8")
    with pytest.raises(runtime.RuntimeDeployError, match="project name 无效"):
        runtime._compose_project(live)
    env_file.unlink()
    assert runtime._compose_project(live) == "agent-gov"
    with pytest.raises(runtime.RuntimeDeployError, match="env 不可读"):
        runtime._env_values(env_file, frozenset({"HOST_PORT"}))


def test_assert_no_prior_runtime_requires_empty_compose_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "live"
    (live / "docker").mkdir(parents=True)
    binding = _binding_value()
    monkeypatch.setattr(runtime, "_docker_output", lambda _binding_value, _arguments: "")
    runtime._assert_no_prior_runtime(binding, live)
    monkeypatch.setattr(runtime, "_docker_output", lambda _binding_value, _arguments: "resource-id")
    with pytest.raises(runtime.RuntimeDeployError, match="仍存在既有"):
        runtime._assert_no_prior_runtime(binding, live)


def test_save_old_images_creates_durable_gzip_and_rejects_partial_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    references = runtime._project_references("3.9.9")
    binding = runtime.DockerBinding(
        command=[sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'image-stream')"],
        environment=dict(os.environ),
    )
    monkeypatch.setattr(runtime, "_inspect_image", lambda _binding_value, reference: f"sha256:{'1' * 64}")
    destination = tmp_path / "recovery/images.tar.gz"
    identities = runtime._save_old_images(binding, references, destination)
    assert tuple(item.reference for item in identities) == references
    with gzip.open(destination, "rb") as source:
        assert source.read() == b"image-stream"
    monkeypatch.setattr(
        runtime,
        "_inspect_image",
        lambda _binding_value, reference: None if reference == references[-1] else f"sha256:{'2' * 64}",
    )
    with pytest.raises(runtime.RuntimeDeployError, match="镜像集合不完整"):
        runtime._save_old_images(binding, references, tmp_path / "partial/images.tar.gz")
    monkeypatch.setattr(runtime, "_inspect_image", lambda _binding_value, _reference: None)
    assert runtime._save_old_images(binding, references, tmp_path / "empty/images.tar.gz") == ()


def test_source_digest_handles_deployable_and_absent_roots(tmp_path: Path) -> None:
    assert runtime._source_digest(REPO_ROOT) is not None
    assert runtime._source_digest(tmp_path) is None


def test_prepare_transaction_captures_old_runtime_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "live"
    images = live / "images"
    images.mkdir(parents=True)
    _write_archive(images / "agent-gov-4.0.1-images.tar.gz", b"project")
    _write_archive(images / "agent-gov-4.0.1-langfuse-deps-images.tar.gz", b"dependency")
    (live / "VERSION").write_text("3.9.9\n", encoding="utf-8")
    (live / "docker").mkdir()
    (live / "docker/.env").write_text("LANGFUSE_ENABLED=true\n", encoding="utf-8")
    old_images = tuple(transaction.ImageIdentity(reference, f"sha256:{index + 1:064x}") for index, reference in enumerate(runtime._project_references("3.9.9")))
    captured: dict[str, object] = {}

    def save_images(_binding_value: runtime.DockerBinding, _references: tuple[str, ...], destination: Path) -> tuple[transaction.ImageIdentity, ...]:
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"old-images")
        return old_images

    def begin(**kwargs: object) -> transaction.DeployTransaction:
        captured.update(kwargs)
        return _state(tmp_path, dependency=True, old_images=True, with_langfuse=True)

    monkeypatch.setattr(runtime, "_bound_docker", lambda _home: _binding())
    monkeypatch.setattr(runtime, "_docker_output", lambda _binding_value, _arguments: "x86_64")
    monkeypatch.setattr(runtime, "_validate_archives", lambda **_kwargs: None)
    monkeypatch.setattr(runtime, "_save_old_images", save_images)
    monkeypatch.setattr(runtime, "_source_digest", lambda _root: "b" * 64)
    monkeypatch.setattr(runtime, "begin_transaction", begin)
    result = runtime.prepare_transaction(
        live_root=live,
        stage_root=tmp_path / "live.stage.runtime",
        backup_root=tmp_path / "live.previous.runtime",
        toolchain_root=tmp_path / "toolchain",
        transaction_id="runtime-transaction",
        version="4.0.1",
        source_digest="a" * 64,
        with_langfuse=True,
    )
    assert result.with_langfuse
    assert captured["old_version"] == "3.9.9"
    assert captured["old_images"] == old_images
    assert cast(transaction.ArchiveIdentity, captured["old_archive"]).sha256 == hashlib.sha256(b"old-images").hexdigest()
    assert captured["old_with_langfuse"] is True


def test_prepare_transaction_proves_fresh_runtime_without_old_tags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "live"
    images = live / "images"
    images.mkdir(parents=True)
    _write_archive(images / "agent-gov-4.0.1-images.tar.gz")
    calls: list[str] = []
    monkeypatch.setattr(runtime, "_bound_docker", lambda _home: _binding())
    monkeypatch.setattr(runtime, "_docker_output", lambda _binding_value, _arguments: "amd64")
    monkeypatch.setattr(runtime, "_validate_archives", lambda **_kwargs: None)
    monkeypatch.setattr(runtime, "_assert_no_prior_runtime", lambda _binding_value, _root: calls.append("fresh"))
    monkeypatch.setattr(runtime, "begin_transaction", lambda **_kwargs: _state(tmp_path))
    runtime.prepare_transaction(
        live_root=live,
        stage_root=tmp_path / "live.stage.runtime",
        backup_root=tmp_path / "live.previous.runtime",
        toolchain_root=tmp_path / "toolchain",
        transaction_id="runtime-transaction",
        version="4.0.1",
        source_digest="a" * 64,
        with_langfuse=False,
    )
    assert calls == ["fresh"]


def test_compose_operation_freezes_files_version_and_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path, with_langfuse=True)
    root = Path(state.live_root)
    (root / "docker/.env").write_text("HOST_PORT=50400\n", encoding="utf-8")
    (root / "docker/docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (root / "docker/docker-compose.langfuse.yml").write_text("services: {}\n", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runtime, "_bound_docker", lambda _home: _binding())
    monkeypatch.setattr(runtime.subprocess, "run", run)
    runtime._compose_operation(state, "up")
    runtime._compose_operation(state, "down", old=True)
    up_command, up_kwargs = calls[0]
    assert "--profile" in up_command and "--force-recreate" in up_command and "never" in up_command
    assert cast(dict[str, str], up_kwargs["env"])["APP_VERSION"] == "4.0.1"
    assert up_kwargs["cwd"] == root
    assert up_command[-2:] != ["down", "--remove-orphans"]
    assert calls[1][0][-2:] == ["down", "--remove-orphans"]
    with pytest.raises(runtime.RuntimeDeployError, match="operation 无效"):
        runtime._compose_operation(state, "restart")


def test_compose_operation_rejects_missing_or_failed_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    with pytest.raises(runtime.RuntimeDeployError, match="Compose/env 边界缺失"):
        runtime._compose_operation(state, "up")
    root = Path(state.live_root)
    (root / "docker/.env").write_text("HOST_PORT=50400\n", encoding="utf-8")
    (root / "docker/docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "_bound_docker", lambda _home: _binding())
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "failed"),
    )
    with pytest.raises(runtime.RuntimeDeployError, match="Compose up 失败"):
        runtime._compose_operation(state, "up")


def test_load_archive_streams_gzip_and_detects_digest_or_process_failure(tmp_path: Path) -> None:
    archive = tmp_path / "archive.tar.gz"
    with gzip.open(archive, "wb") as output:
        output.write(b"image-payload")
    identity = transaction.ArchiveIdentity(archive.as_posix(), runtime._sha256(archive))
    success = runtime.DockerBinding(
        command=[sys.executable, "-c", "import sys; assert sys.stdin.buffer.read() == b'image-payload'"],
        environment=dict(os.environ),
    )
    runtime._load_archive(success, identity)
    with pytest.raises(runtime.RuntimeDeployError, match="摘要漂移"):
        runtime._load_archive(success, replace(identity, sha256="0" * 64))
    failure = runtime.DockerBinding(
        command=[sys.executable, "-c", "import sys; sys.stdin.buffer.read(); raise SystemExit(7)"],
        environment=dict(os.environ),
    )
    with pytest.raises(runtime.RuntimeDeployError, match="docker load 失败"):
        runtime._load_archive(failure, identity)


def test_verify_loaded_project_rejects_source_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    binding = _binding_value()
    monkeypatch.setattr(runtime, "_docker_output", lambda _binding_value, _arguments: state.source_sha256)
    runtime._verify_loaded_project(binding, state)
    monkeypatch.setattr(runtime, "_docker_output", lambda _binding_value, _arguments: "wrong")
    with pytest.raises(runtime.RuntimeDeployError, match="source identity 不匹配"):
        runtime._verify_loaded_project(binding, state)


@pytest.mark.parametrize("value", ["50399", "50500", "abc", "-50400"])
def test_port_rejects_values_outside_reserved_range(value: str) -> None:
    with pytest.raises(runtime.RuntimeDeployError, match="50400-50499"):
        runtime._port({"HOST_PORT": value}, "HOST_PORT", 50400)
    assert runtime._port({}, "HOST_PORT", 50400) == 50400


class ResponseBox:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> ResponseBox:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int) -> bytes:
        return b"x"


class OpenerSequence:
    def __init__(self, results: list[ResponseBox | Exception]) -> None:
        self.results = results
        self.urls: list[str] = []

    def open(self, request: Request, *, timeout: int) -> ResponseBox:
        assert timeout == 5
        self.urls.append(request.full_url)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_health_checks_api_ui_and_langfuse_without_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path, with_langfuse=True)
    Path(state.live_root, "docker/.env").write_text(
        "HOST_PORT=50410\nFRONTEND_HOST_PORT=50411\nLANGFUSE_HOST_PORT=50412\n",
        encoding="utf-8",
    )
    opener = OpenerSequence([ResponseBox(503), ResponseBox(204), ResponseBox(200), ResponseBox(302)])
    monkeypatch.setattr(runtime, "build_opener", lambda _handler: opener)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    runtime._health(state)
    assert opener.urls == [
        "http://127.0.0.1:50410/health/ready",
        "http://127.0.0.1:50410/health/ready",
        "http://127.0.0.1:50411",
        "http://127.0.0.1:50412",
    ]


@pytest.mark.parametrize(
    "error",
    [HTTPError("http://127.0.0.1", 503, "unavailable", {}, None), URLError("offline")],
)
def test_health_failure_is_bounded_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    state = _state(tmp_path)
    Path(state.live_root, "docker/.env").write_text("HOST_PORT=50400\n", encoding="utf-8")
    opener = OpenerSequence([error] * 60)
    monkeypatch.setattr(runtime, "build_opener", lambda _handler: opener)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    with pytest.raises(runtime.RuntimeDeployError, match="api health 失败"):
        runtime._health(state)


def test_execute_transaction_advances_every_durable_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path, dependency=True)
    phases: list[str] = []
    loaded: list[transaction.ArchiveIdentity] = []

    def advance(current: transaction.DeployTransaction, phase: transaction.DeployPhase) -> transaction.DeployTransaction:
        phases.append(phase)
        return replace(current, phase=phase)

    monkeypatch.setattr(runtime, "load_transaction", lambda _root: state)
    monkeypatch.setattr(runtime, "_bound_docker", lambda _home: _binding())
    monkeypatch.setattr(runtime, "_docker_output", lambda _binding_value, _arguments: "x86_64")
    monkeypatch.setattr(runtime, "_validate_archives", lambda **_kwargs: None)
    monkeypatch.setattr(runtime, "_load_archive", lambda _binding_value, identity: loaded.append(identity))
    monkeypatch.setattr(runtime, "_verify_loaded_project", lambda _binding_value, _state_value: None)
    monkeypatch.setattr(runtime, "_compose_operation", lambda _state_value, _operation: None)
    monkeypatch.setattr(runtime, "_health", lambda _state_value: None)
    monkeypatch.setattr(runtime, "transition", advance)
    result = runtime.execute_transaction(Path(state.live_root))
    assert result.phase == "healthy"
    assert len(loaded) == 2
    assert phases == [
        "images-loading",
        "images-loaded",
        "compose-starting",
        "compose-recreated",
        "health-checking",
        "healthy",
    ]


def test_execute_transaction_persists_recovery_requirement_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    errors: list[str] = []
    monkeypatch.setattr(runtime, "load_transaction", lambda _root: state)
    monkeypatch.setattr(runtime, "_bound_docker", lambda _home: _binding())
    monkeypatch.setattr(runtime, "_docker_output", lambda _binding_value, _arguments: "amd64")
    monkeypatch.setattr(runtime, "_validate_archives", lambda **_kwargs: (_ for _ in ()).throw(runtime.RuntimeDeployError("drift")))
    monkeypatch.setattr(runtime, "mark_recovery_required", lambda _root, code: errors.append(code))
    with pytest.raises(runtime.RuntimeDeployError, match="必须执行持久恢复事务"):
        runtime.execute_transaction(Path(state.live_root))
    assert errors == ["candidate-deploy-failed"]
    monkeypatch.setattr(
        runtime,
        "mark_recovery_required",
        lambda _root, _code: (_ for _ in ()).throw(transaction.TransactionError("disk")),
    )
    with pytest.raises(runtime.RuntimeDeployError, match="恢复状态无法持久化"):
        runtime.execute_transaction(Path(state.live_root))


def test_restore_old_images_verifies_archive_and_each_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path, old_images=True)
    calls: list[str] = []
    monkeypatch.setattr(runtime, "validate_image_archive", lambda *_args, **_kwargs: calls.append("validate"))
    monkeypatch.setattr(runtime, "_load_archive", lambda _binding_value, _identity: calls.append("load"))
    monkeypatch.setattr(runtime, "_inspect_image", lambda _binding_value, _reference: state.old_images[0].image_id)
    runtime._restore_old_images(_binding_value(), state, "amd64")
    assert calls == ["validate", "load"]
    monkeypatch.setattr(runtime, "_inspect_image", lambda _binding_value, _reference: None)
    with pytest.raises(runtime.RuntimeDeployError, match="identity 恢复失败"):
        runtime._restore_old_images(_binding_value(), state, "amd64")
    runtime._restore_old_images(_binding_value(), _state(tmp_path / "empty"), "amd64")


@pytest.mark.parametrize(
    ("phase", "expected", "health_calls", "success_calls", "rollback_calls"),
    [
        ("healthy", "committed", 1, 1, 0),
        ("finalizing-success", "committed", 0, 1, 0),
        ("rolled-back", "rolled-back", 0, 0, 1),
        ("finalizing-rollback", "rolled-back", 0, 0, 1),
    ],
)
def test_recover_transaction_finishes_terminal_phases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: transaction.DeployPhase,
    expected: str,
    health_calls: int,
    success_calls: int,
    rollback_calls: int,
) -> None:
    state = _state(tmp_path, phase=phase)
    calls = {"health": 0, "success": 0, "rollback": 0}
    monkeypatch.setattr(runtime, "load_transaction", lambda _root: state)
    monkeypatch.setattr(runtime, "_health", lambda _state_value: calls.__setitem__("health", calls["health"] + 1))
    monkeypatch.setattr(runtime, "finalize_success", lambda _root: calls.__setitem__("success", calls["success"] + 1))
    monkeypatch.setattr(runtime, "finalize_rollback", lambda _root: calls.__setitem__("rollback", calls["rollback"] + 1))
    assert runtime.recover_transaction(Path(state.live_root)) == expected
    assert calls == {"health": health_calls, "success": success_calls, "rollback": rollback_calls}


@pytest.mark.parametrize("old_images", [False, True])
def test_recover_transaction_restores_source_and_optional_old_compose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    old_images: bool,
) -> None:
    state = _state(tmp_path, old_images=old_images)
    operations: list[tuple[str, bool]] = []
    health: list[bool] = []

    def advance(current: transaction.DeployTransaction, phase: transaction.DeployPhase) -> transaction.DeployTransaction:
        return replace(current, phase=phase)

    monkeypatch.setattr(runtime, "load_transaction", lambda _root: state)
    monkeypatch.setattr(runtime, "_bound_docker", lambda _home: _binding())
    monkeypatch.setattr(runtime, "_docker_output", lambda _binding_value, _arguments: "amd64")
    monkeypatch.setattr(runtime, "_restore_old_images", lambda _binding_value, _state_value, _architecture: None)
    monkeypatch.setattr(
        runtime,
        "_compose_operation",
        lambda _state_value, operation, *, old=False: operations.append((operation, old)),
    )
    monkeypatch.setattr(runtime, "rollback_source", lambda _root: replace(state, phase="source-rolled-back"))
    monkeypatch.setattr(runtime, "transition", advance)
    monkeypatch.setattr(runtime, "_health", lambda _state_value, *, old=False: health.append(old))
    monkeypatch.setattr(runtime, "finalize_rollback", lambda _root: None)
    assert runtime.recover_transaction(Path(state.live_root)) == "rolled-back"
    assert operations == ([("up", True)] if old_images else [("down", False)])
    assert health == ([True] if old_images else [])


def test_recover_transaction_remains_fail_closed_when_rollback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    errors: list[str] = []
    monkeypatch.setattr(runtime, "load_transaction", lambda _root: state)
    monkeypatch.setattr(runtime, "_bound_docker", lambda _home: _binding())
    monkeypatch.setattr(runtime, "_docker_output", lambda _binding_value, _arguments: (_ for _ in ()).throw(runtime.RuntimeDeployError("daemon")))
    monkeypatch.setattr(runtime, "mark_recovery_required", lambda _root, code: errors.append(code))
    with pytest.raises(runtime.RuntimeDeployError, match="保持 fail closed"):
        runtime.recover_transaction(Path(state.live_root))
    assert errors == ["rollback-failed"]
    monkeypatch.setattr(
        runtime,
        "mark_recovery_required",
        lambda _root, _code: (_ for _ in ()).throw(transaction.TransactionError("disk")),
    )
    with pytest.raises(runtime.RuntimeDeployError, match="事务状态无法持久化"):
        runtime.recover_transaction(Path(state.live_root))


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("activate", "activated"),
        ("execute", "healthy"),
        ("commit", "committed"),
        ("recover", "rolled-back"),
        ("status", '"phase": "activated"'),
    ],
)
def test_command_entrypoint_dispatches_runtime_transactions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    expected: str,
) -> None:
    state = _state(tmp_path)
    monkeypatch.setattr(runtime, "activate_transaction", lambda _root: state)
    monkeypatch.setattr(runtime, "execute_transaction", lambda _root: state)
    monkeypatch.setattr(runtime, "finalize_success", lambda _root: None)
    monkeypatch.setattr(runtime, "recover_transaction", lambda _root: "rolled-back")
    monkeypatch.setattr(runtime, "load_transaction", lambda _root: state)
    assert runtime._main([command, "--live-root", state.live_root]) == 0
    assert expected in capsys.readouterr().out


def test_command_entrypoint_prepares_and_redacts_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = _state(tmp_path)
    monkeypatch.setattr(runtime, "prepare_transaction", lambda **_kwargs: state)
    arguments = [
        "prepare",
        "--live-root",
        state.live_root,
        "--stage-root",
        state.stage_root,
        "--backup-root",
        state.backup_root,
        "--toolchain-root",
        state.toolchain_root,
        "--transaction-id",
        state.transaction_id,
        "--version",
        state.version,
        "--source-sha256",
        state.source_sha256,
        "--with-langfuse",
        "0",
    ]
    assert runtime._main(arguments) == 0
    assert capsys.readouterr().out == "prepared\n"
    monkeypatch.setattr(runtime, "prepare_transaction", lambda **_kwargs: (_ for _ in ()).throw(runtime.RuntimeDeployError("private input")))
    with pytest.raises(SystemExit) as error:
        runtime._main(arguments)
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert "private input" not in captured.err
    assert "未输出私有 env" in captured.err
