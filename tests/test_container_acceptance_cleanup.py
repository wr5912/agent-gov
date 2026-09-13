from __future__ import annotations

import inspect
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest
from scripts import agentscope_atomic_cutover_cleanup as cleanup_support
from scripts import run_container_acceptance as acceptance

PYTHON_IMAGE = "python:3.11-slim"


def _docker_with_python_image() -> str:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker is unavailable")
    version = subprocess.run([docker, "version"], check=False, capture_output=True, text=True)
    image = subprocess.run([docker, "image", "inspect", PYTHON_IMAGE], check=False, capture_output=True, text=True)
    if version.returncode != 0 or image.returncode != 0:
        pytest.skip(f"docker or local {PYTHON_IMAGE} image is unavailable")
    return docker


def _repair_test_ownership(parent: Path) -> None:
    if not parent.exists():
        return
    try:
        shutil.rmtree(parent)
        return
    except PermissionError:
        pass
    docker = shutil.which("docker")
    if docker is not None:
        subprocess.run(
            [
                docker,
                "run",
                "--rm",
                "--network",
                "none",
                "--mount",
                f"type=bind,src={parent},dst=/test-root",
                "--entrypoint",
                "python",
                PYTHON_IMAGE,
                "-c",
                "import os,sys; uid,gid=map(int,sys.argv[1:]); [(os.chown(p,uid,gid,follow_symlinks=False),os.chmod(p,0o700)) for p,d,f in os.walk('/test-root',followlinks=False)]",
                str(os.getuid()),
                str(os.getgid()),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    shutil.rmtree(parent, ignore_errors=True)


@pytest.fixture
def isolated_root():
    parent = Path(tempfile.mkdtemp(prefix=f"agentgov-acceptance-{os.getuid()}-"))
    parent.chmod(0o700)
    root = parent / "runtime-root"
    root.mkdir()
    try:
        yield root
    finally:
        _repair_test_ownership(parent)


def _write_compose(path: Path, image: str, *, service: str = "agent-gov-api") -> list[str]:
    path.write_text(f"services:\n  {service}:\n    image: {image}\n", encoding="utf-8")
    return ["docker", "compose", "-f", str(path)]


def _create_root_owned_content(root: Path) -> None:
    docker = _docker_with_python_image()
    result = subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "--network",
            "none",
            "--mount",
            f"type=bind,src={root},dst=/test-root",
            "--entrypoint",
            "python",
            PYTHON_IMAGE,
            "-c",
            "from pathlib import Path; p=Path('/test-root/root-owned'); p.mkdir(mode=0o700); (p/'data').write_text('owned')",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_cleanup_removes_only_isolated_data_without_docker(isolated_root: Path) -> None:
    sibling = isolated_root.parent / "compose.acceptance.env"
    sibling.write_text("private-env", encoding="utf-8")
    (isolated_root / "data").mkdir()
    buildx_state = isolated_root / "buildx-state"
    buildx_state.mkdir(mode=0o700)
    (buildx_state / "current").write_text("default", encoding="utf-8")

    acceptance.cleanup_runtime_root([], isolated_root, {})

    assert not isolated_root.exists()
    assert sibling.read_text(encoding="utf-8") == "private-env"


@pytest.mark.parametrize("target", ["wrong-name", "outside-temp", "root-link", "parent-link", "public-parent"])
def test_cleanup_rejects_untrusted_roots(isolated_root: Path, target: str) -> None:
    root = isolated_root
    extra_parent: Path | None = None
    if target == "wrong-name":
        root = root.parent / "data"
        root.mkdir()
    elif target == "outside-temp":
        extra_parent = Path(tempfile.mkdtemp(prefix="agentgov-untrusted-"))
        root = extra_parent / "runtime-root"
        root.mkdir()
    elif target == "root-link":
        root.rmdir()
        root.symlink_to(root.parent.parent, target_is_directory=True)
    elif target == "parent-link":
        extra_parent = root.parent.parent / f"agentgov-acceptance-{os.getuid()}-alias-{root.parent.name.rsplit('-', 1)[-1]}"
        extra_parent.symlink_to(root.parent, target_is_directory=True)
        root = extra_parent / "runtime-root"
    else:
        root.parent.chmod(0o755)

    try:
        with pytest.raises(acceptance.AcceptanceError, match="拒绝清理"):
            acceptance.cleanup_runtime_root([], root, {})
        assert root.exists()
    finally:
        if target == "public-parent":
            isolated_root.parent.chmod(0o700)
        if extra_parent is not None:
            if extra_parent.is_symlink():
                extra_parent.unlink()
            else:
                shutil.rmtree(extra_parent)


def test_permission_repair_runs_real_locked_down_container(isolated_root: Path) -> None:
    _create_root_owned_content(isolated_root)
    base = _write_compose(isolated_root.parent / "cleanup-compose.yml", PYTHON_IMAGE)

    acceptance.cleanup_runtime_root(base, isolated_root, dict(os.environ))

    assert not isolated_root.exists()


def test_permission_repair_command_has_one_mount_no_network_and_no_secret_env() -> None:
    source = inspect.getsource(cleanup_support.cleanup_runtime_root) + inspect.getsource(cleanup_support._repair_ownership)

    assert source.count('"--mount"') == 1
    assert '"--network",\n            "none"' in source
    assert '"--pull",\n            "never"' in source
    assert '"--read-only"' in source and '"no-new-privileges"' in source
    assert '"--privileged"' not in source and '"--env"' not in source
    assert "followlinks=False" in source


def test_cleanup_runs_final_boundary_probe_before_deleting(isolated_root: Path) -> None:
    calls: list[str] = []
    (isolated_root / "private").write_text("secret", encoding="utf-8")

    def before_delete() -> None:
        assert (isolated_root / "private").exists()
        calls.append("probe")

    acceptance.cleanup_runtime_root([], isolated_root, {}, before_delete=before_delete)

    assert calls == ["probe"]
    assert not isolated_root.exists()


def test_failed_final_boundary_probe_preserves_isolated_data(isolated_root: Path) -> None:
    private = isolated_root / "private"
    private.write_text("secret", encoding="utf-8")

    def before_delete() -> None:
        raise acceptance.AcceptanceError("daemon changed")

    with pytest.raises(acceptance.AcceptanceError, match="daemon changed"):
        acceptance.cleanup_runtime_root([], isolated_root, {}, before_delete=before_delete)

    assert private.read_text(encoding="utf-8") == "secret"


def test_cleanup_does_not_follow_child_symlinks(isolated_root: Path) -> None:
    outside = isolated_root.parent / "keep"
    outside.mkdir()
    (outside / "data").write_text("keep", encoding="utf-8")
    (isolated_root / "linked").symlink_to(outside, target_is_directory=True)

    acceptance.cleanup_runtime_root([], isolated_root, {})

    assert (outside / "data").read_text(encoding="utf-8") == "keep"


def test_failed_cleanup_boundary_never_removes_bound_data(isolated_root: Path) -> None:
    env_file = isolated_root.parent / "compose.env"
    env_file.write_text("COMPOSE_PROJECT_NAME=acceptance-down-failure\n", encoding="utf-8")
    isolation = acceptance.IsolatedEnvironment(env_file, isolated_root, "acceptance-down-failure", "test", {})
    child_env = dict(os.environ)
    child_env["DOCKER_HOST"] = f"unix://{isolated_root.parent}/missing-docker.sock"

    with pytest.raises(acceptance.AcceptanceError, match="固定本机 Docker Unix socket"):
        acceptance.cleanup_profile(acceptance.PROFILES["core"], isolation, child_env, {})

    assert isolated_root.exists()


@pytest.mark.parametrize("failure", ["image-metadata", "repair-container"])
def test_real_cleanup_failures_do_not_report_success(isolated_root: Path, failure: str) -> None:
    _create_root_owned_content(isolated_root)
    if failure == "image-metadata":
        base = _write_compose(isolated_root.parent / "cleanup-compose.yml", PYTHON_IMAGE, service="other")
    else:
        base = _write_compose(
            isolated_root.parent / "cleanup-compose.yml",
            "agentgov-cleanup-image-does-not-exist:invalid",
        )

    with pytest.raises(acceptance.AcceptanceError):
        acceptance.cleanup_runtime_root(base, isolated_root, dict(os.environ))

    assert isolated_root.exists()


def test_prepare_failure_cleans_real_temporary_acceptance_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    listeners: list[socket.socket] = []
    try:
        for port in range(50400, 50500):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                listener.close()
                continue
            listener.listen(1)
            listeners.append(listener)
        source_env = tmp_path / "source.env"
        source_env.write_text("", encoding="utf-8")
        existing = set(Path(tempfile.gettempdir()).glob(f"agentgov-acceptance-{os.getuid()}-*"))

        result = acceptance.main(
            [
                "--profile",
                "core",
                "--env-file",
                str(source_env),
                "--",
                "/usr/bin/make",
                "--no-print-directory",
                "_container-core-smoke",
            ]
        )
        created = set(Path(tempfile.gettempdir()).glob(f"agentgov-acceptance-{os.getuid()}-*")) - existing
    finally:
        for listener in listeners:
            listener.close()

    output = capsys.readouterr()
    assert result == 1
    assert not created
    assert "CONTAINER_ACCEPTANCE_OK" not in output.out
    assert "50400–50499" in output.err


@pytest.mark.parametrize("failure", ["build-env", "daemon-capture", "source-fingerprint", "acceptance-fingerprint"])
def test_pre_compose_failures_remove_private_temporary_snapshot(tmp_path: Path, monkeypatch, failure: str) -> None:
    selected = tmp_path / "selected.env"
    selected.write_text("MODEL_PROVIDER_API_KEY=private-provider\n", encoding="utf-8")
    temp_root = tmp_path / f"agentgov-acceptance-{os.getuid()}-early-failure"

    def make_temp_root(*, prefix: str) -> str:
        assert prefix == f"agentgov-acceptance-{os.getuid()}-"
        temp_root.mkdir(mode=0o700)
        return temp_root.as_posix()

    def freeze_source(root: Path) -> tuple[Path, str]:
        snapshot = root / "source-snapshot"
        snapshot.mkdir(mode=0o700)
        (snapshot / "private-copy").write_text("sensitive", encoding="utf-8")
        return acceptance.REPO_ROOT, "a" * 64

    def prepare(_source: Path, run_id: str, root: Path, **_kwargs) -> acceptance.IsolatedEnvironment:
        runtime_root = root / "runtime-root"
        runtime_root.mkdir()
        isolated_env = root / "compose.acceptance.env"
        isolated_env.write_text("MODEL_PROVIDER_API_KEY=private-provider\n", encoding="utf-8")
        return acceptance.IsolatedEnvironment(isolated_env, runtime_root, run_id, run_id, {})

    class Daemon:
        def capture(self, _env):
            if failure == "daemon-capture":
                raise acceptance.AcceptanceError("capture failed")
            return {"endpoint": "unix:///run/docker.sock", "id": "engine"}

    def build_env(*_args, **_kwargs) -> dict[str, str]:
        if failure == "build-env":
            raise acceptance.AcceptanceError("build env failed")
        return {"DOCKER_HOST": "unix:///var/run/docker.sock"}

    def source_fingerprint(_path: Path) -> str:
        if failure == "source-fingerprint":
            raise acceptance.AcceptanceError("source fingerprint failed")
        return "b" * 64

    def acceptance_fingerprint(*_args, **_kwargs) -> str:
        if failure == "acceptance-fingerprint":
            raise acceptance.AcceptanceError("acceptance fingerprint failed")
        return "c" * 64

    monkeypatch.setattr(acceptance, "LOCK_FILE", tmp_path / "acceptance.lock")
    monkeypatch.setattr(acceptance.tempfile, "mkdtemp", make_temp_root)
    monkeypatch.setattr(acceptance, "_freeze_acceptance_source", freeze_source)
    monkeypatch.setattr(acceptance, "prepare_isolated_environment", prepare)
    monkeypatch.setattr(acceptance, "build_acceptance_env", build_env)
    monkeypatch.setattr(acceptance, "_daemon_support", lambda _env: Daemon())
    monkeypatch.setattr(acceptance, "_verify_daemon_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(acceptance, "source_fingerprint", source_fingerprint)
    monkeypatch.setattr(acceptance, "acceptance_fingerprint", acceptance_fingerprint)

    with pytest.raises(acceptance.AcceptanceError):
        acceptance.run_acceptance(
            acceptance.PROFILES["core"],
            selected,
            ["/usr/bin/make", "--no-print-directory", "_container-core-smoke"],
            {},
        )

    assert not temp_root.exists()
