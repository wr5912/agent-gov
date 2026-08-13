from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import py_compile
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import scripts.container_acceptance_bootstrap as bootstrap
import scripts.container_acceptance_dependency_authority as dependency_authority
import scripts.container_acceptance_docker_authority as docker_authority
import scripts.container_acceptance_import_authority as import_authority
import scripts.container_acceptance_snapshot_authority as snapshot_authority
import scripts.container_acceptance_tool_authority as tool_authority
import scripts.container_acceptance_toolchain as toolchain


def _dependency(path: Path) -> dependency_authority.DependencyTreeAuthority:
    identity = path.stat(follow_symlinks=False)
    return {
        "root": str(path),
        "device": identity.st_dev,
        "inode": identity.st_ino,
        "mode": stat.S_IMODE(identity.st_mode),
        "uid": identity.st_uid,
        "gid": identity.st_gid,
        "mtime_ns": identity.st_mtime_ns,
        "ctime_ns": identity.st_ctime_ns,
        "entries": 1,
        "regular_bytes": 1,
        "sha256": "d" * 64,
        "projection_sha256": "e" * 64,
        "generation_sha256": "f" * 64,
    }


def _daemon() -> docker_authority.DockerDaemonAuthority:
    return {"socket_authority_sha256": "e" * 64, "daemon_identity_sha256": "f" * 64}


@pytest.fixture(scope="module")
def captured_toolchain() -> toolchain.CapturedToolchainAuthority:
    return toolchain.capture_toolchain_authority(dependency_capturer=_dependency, daemon_capturer=_daemon)


def test_import_is_lazy_and_does_not_capture_dependency_or_daemon() -> None:
    command = (
        "import sys;"
        f"sys.path.insert(0,{str(toolchain.REPO_ROOT)!r});"
        "from scripts import container_acceptance_toolchain as module;"
        "assert module._ACTIVE_AUTHORITY is None"
    )
    result = subprocess.run(
        ("/usr/bin/python3", "-I", "-c", command),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        env={"LC_ALL": "C.UTF-8"},
    )

    assert result.returncode == 0, result.stderr


def test_tool_file_failures_expose_only_bounded_authority_categories(
    monkeypatch: pytest.MonkeyPatch,
    captured_toolchain: toolchain.CapturedToolchainAuthority,
) -> None:
    secret = "private-path-and-value"

    monkeypatch.setattr(
        tool_authority,
        "capture_tool_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(tool_authority.ToolFileAuthorityError(secret)),
    )
    with pytest.raises(tool_authority.CapturedSmallFileAuthorityError) as captured:
        tool_authority.capture_small_file(Path("/not-exposed"), "candidate-python-toolchain")
    assert secret not in str(captured.value)

    record = captured_toolchain.payload["tools"][0]
    monkeypatch.setattr(
        tool_authority,
        "_open_verified_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(tool_authority.ToolFileAuthorityError(secret)),
    )
    with pytest.raises(tool_authority.VerifiedToolFileAuthorityError) as verified:
        tool_authority.open_verified_file(record, require_executable=True)
    assert secret not in str(verified.value)

    monkeypatch.setattr(
        tool_authority,
        "_capture_private_state_authority",
        lambda: (_ for _ in ()).throw(tool_authority.ToolFileAuthorityError(secret)),
    )
    with pytest.raises(tool_authority.PrivateStateAuthorityError) as private:
        tool_authority.capture_private_state_authority()
    assert secret not in str(private.value)


def test_verified_tool_ignores_unrelated_ancestor_entries_but_rejects_ancestor_replacement() -> None:
    paths = tool_authority.capture_private_state_authority().paths
    with tempfile.TemporaryDirectory(dir=paths.candidates) as raw_directory:
        base = Path(raw_directory)
        ancestor = base / "ancestor"
        ancestor.mkdir()
        executable = ancestor / "tool"
        executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        record, _encoded = tool_authority.capture_tool_file(executable, "tool", executable=True)

        unrelated = ancestor / "unrelated"
        unrelated.write_text("normal sibling activity", encoding="utf-8")
        os.close(tool_authority.open_verified_file(record, require_executable=True))
        unrelated.unlink()
        os.close(tool_authority.open_verified_file(record, require_executable=True))

        original_ancestor = base / "original-ancestor"
        ancestor.rename(original_ancestor)
        ancestor.mkdir()
        (original_ancestor / "tool").rename(ancestor / "tool")
        with pytest.raises(tool_authority.VerifiedToolFileAuthorityError):
            tool_authority.open_verified_file(record, require_executable=True)


def test_capture_binds_repository_pins_absolute_tools_and_private_state(
    captured_toolchain: toolchain.CapturedToolchainAuthority,
) -> None:
    payload = captured_toolchain.payload
    commands = {record["command"] for record in payload["tools"]}
    paths = tool_authority.private_state_paths()

    assert payload["node_version"] == "22.22.0"
    assert payload["pnpm_version"] == "10.30.3"
    assert {"python", "bootstrap-python", "git", "docker", "docker-compose", "node", "pnpm", "make", "chromium"} <= commands
    assert all(Path(record["invocation_path"]).is_absolute() and len(record["sha256"]) == 64 for record in payload["tools"])
    assert captured_toolchain.controlled_path.split(os.pathsep) == [
        str(tool_authority.trusted_home() / ".config/nvm/versions/node/v22.22.0/bin"),
        str(toolchain.REPO_ROOT / "frontend/node_modules/.bin"),
        "/usr/bin",
    ]
    assert stat.S_IMODE(paths.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.docker_config.stat().st_mode) == 0o500
    assert stat.S_IMODE(paths.git_home.stat().st_mode) == 0o500
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in (paths.candidates, paths.receipts))
    assert all(not any(path.iterdir()) for path in (paths.docker_config, paths.git_home))
    assert not (paths.root / "runtime-home").exists()


def test_caller_home_nvm_path_and_docker_routing_do_not_change_capture(
    monkeypatch: pytest.MonkeyPatch,
    captured_toolchain: toolchain.CapturedToolchainAuthority,
) -> None:
    monkeypatch.setenv("HOME", "/untrusted/home")
    monkeypatch.setenv("NVM_BIN", "/untrusted/node")
    monkeypatch.setenv("PATH", "/untrusted/bin")
    monkeypatch.setenv("DOCKER_HOST", "unix:///untrusted.sock")
    monkeypatch.setenv("DOCKER_CONFIG", "/untrusted/docker")

    observed = toolchain.capture_toolchain_authority(
        dependency_capturer=_dependency,
        daemon_capturer=_daemon,
        baseline=captured_toolchain,
    )

    assert observed == captured_toolchain


def test_managed_docker_discovery_is_read_only_and_remains_revalidatable(
    monkeypatch: pytest.MonkeyPatch,
    captured_toolchain: toolchain.CapturedToolchainAuthority,
) -> None:
    monkeypatch.setattr(toolchain, "_ACTIVE_AUTHORITY", captured_toolchain)

    environment = toolchain.managed_tool_environment()
    docker_config = Path(environment["DOCKER_CONFIG"])
    observed = toolchain.capture_toolchain_authority(
        dependency_capturer=_dependency,
        daemon_capturer=_daemon,
        baseline=captured_toolchain,
    )

    assert environment["DOCKER_HOST"] == "unix:///run/docker.sock"
    assert environment["DOCKER_CLI_PLUGIN_EXTRA_DIRS"] == "/usr/libexec/docker/cli-plugins"
    assert stat.S_IMODE(docker_config.stat().st_mode) == 0o500
    assert not any(docker_config.iterdir())
    assert observed == captured_toolchain


def test_browser_runtime_validation_rehashes_the_frozen_tree(
    monkeypatch: pytest.MonkeyPatch,
    captured_toolchain: toolchain.CapturedToolchainAuthority,
) -> None:
    monkeypatch.setattr(toolchain, "_ACTIVE_AUTHORITY", captured_toolchain)
    observed_commands: list[tuple[str, ...] | None] = []
    monkeypatch.setattr(
        toolchain,
        "validate_execution_tool_authority",
        lambda _environ=None, *, commands=None: observed_commands.append(commands),
    )
    monkeypatch.setattr(
        toolchain.dependency_authority,
        "capture_dependency_tree",
        lambda root: ({**captured_toolchain.payload["browser_runtime"], "root": str(root)}),
    )

    toolchain.validate_browser_runtime_authority()

    assert observed_commands == [("chromium",)]

    drifted = {**captured_toolchain.payload["browser_runtime"], "sha256": "0" * 64}
    monkeypatch.setattr(toolchain.dependency_authority, "capture_dependency_tree", lambda _root: drifted)
    with pytest.raises(toolchain.ToolchainAuthorityError, match="browser runtime authority drifted"):
        toolchain.validate_browser_runtime_authority()

    observed_generations: list[str] = []
    monkeypatch.setattr(
        toolchain.dependency_authority,
        "require_dependency_generation_current",
        lambda expected: observed_generations.append(expected["root"]),
    )
    toolchain.validate_dependency_tree_generations()
    assert observed_generations == [
        captured_toolchain.payload["python_environment"]["site_packages"]["root"],
        captured_toolchain.payload["frontend_dependencies"]["root"],
        captured_toolchain.payload["pnpm_runtime"]["root"],
        captured_toolchain.payload["browser_runtime"]["root"],
    ]


def test_snapshot_source_root_reuses_serialized_dependency_roots(
    monkeypatch: pytest.MonkeyPatch,
    captured_toolchain: toolchain.CapturedToolchainAuthority,
) -> None:
    paths = tool_authority.capture_private_state_authority().paths
    observed_roots: list[Path] = []
    with tempfile.TemporaryDirectory(dir=paths.candidates) as raw_snapshot:
        snapshot = Path(raw_snapshot)
        (snapshot / "frontend").mkdir()
        for relative in (Path(".node-version"), Path("frontend/package.json"), Path("requirements.txt"), Path("pyproject.toml"), Path("uv.lock")):
            destination = snapshot / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(toolchain.REPO_ROOT / relative, destination)
        monkeypatch.setattr(toolchain, "REPO_ROOT", snapshot)
        monkeypatch.setattr(toolchain, "NODE_VERSION_FILE", snapshot / ".node-version")
        monkeypatch.setattr(toolchain, "FRONTEND_PACKAGE_FILE", snapshot / "frontend/package.json")

        def capture(path: Path) -> dependency_authority.DependencyTreeAuthority:
            observed_roots.append(path)
            return _dependency(path)

        observed = toolchain.capture_toolchain_authority(
            dependency_capturer=capture,
            daemon_capturer=_daemon,
            baseline=captured_toolchain,
        )

    assert observed == captured_toolchain
    dependency_root = Path(captured_toolchain.payload["python_environment"]["prefix"]).parent
    assert observed_roots == [
        dependency_root / ".venv/lib/python3.11/site-packages",
        dependency_root / "frontend/node_modules",
        Path(captured_toolchain.payload["pnpm_runtime"]["root"]),
        toolchain.BROWSER_ROOT,
    ]


def test_git_api_uses_fixed_binary_empty_home_and_only_explicit_index(
    monkeypatch: pytest.MonkeyPatch,
    captured_toolchain: toolchain.CapturedToolchainAuthority,
) -> None:
    monkeypatch.setattr(toolchain, "_ACTIVE_AUTHORITY", captured_toolchain)
    repository = Path(captured_toolchain.payload["python_environment"]["prefix"]).parent
    index = repository / ".git/acceptance-index"

    argv = toolchain.git_argv(repository, "status", "--porcelain")
    environment = toolchain.git_environment(index_file=index)

    assert argv[0] == "/usr/bin/git"
    assert "filter.lfs.process=" in argv and "core.attributesFile=/dev/null" in argv
    assert "core.fsmonitor=false" in argv and "core.hooksPath=/dev/null" in argv
    assert "core.excludesFile=/dev/null" in argv
    assert environment == {
        "HOME": str(tool_authority.private_state_paths().git_home),
        "XDG_CONFIG_HOME": str(tool_authority.private_state_paths().git_home),
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_INDEX_FILE": str(index),
    }


def test_fixed_git_ignores_repository_fsmonitor_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    captured_toolchain: toolchain.CapturedToolchainAuthority,
) -> None:
    repository = tmp_path / "repository"
    helper = tmp_path / "fsmonitor-helper"
    marker = tmp_path / "helper-ran"
    setup_home = tmp_path / "setup-home"
    setup_home.mkdir()
    setup_env = {
        "HOME": str(setup_home),
        "PATH": "/usr/bin",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    subprocess.run(("/usr/bin/git", "init", "-q", str(repository)), check=True, env=setup_env)
    helper.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    helper.chmod(0o700)
    subprocess.run(
        ("/usr/bin/git", "-C", str(repository), "config", "core.fsmonitor", str(helper)),
        check=True,
        env=setup_env,
    )
    payload = copy.deepcopy(captured_toolchain.payload)
    payload["python_environment"]["prefix"] = str(repository / ".venv")
    authority = toolchain.CapturedToolchainAuthority(payload, "0" * 64, captured_toolchain.controlled_path)
    monkeypatch.setattr(toolchain, "_ACTIVE_AUTHORITY", authority)

    completed = subprocess.run(
        toolchain.git_argv(repository, "status", "--porcelain"),
        check=False,
        capture_output=True,
        env=toolchain.git_environment(),
    )

    assert completed.returncode == 0
    assert not marker.exists()


def test_receipt_toolchain_evidence_contains_no_private_paths(
    monkeypatch: pytest.MonkeyPatch,
    captured_toolchain: toolchain.CapturedToolchainAuthority,
) -> None:
    monkeypatch.setattr(toolchain, "_ACTIVE_AUTHORITY", captured_toolchain)

    encoded = json.dumps(toolchain.toolchain_receipt_payload(), sort_keys=True)

    assert str(toolchain.REPO_ROOT) not in encoded
    assert str(tool_authority.trusted_home()) not in encoded
    assert "invocation_path" not in encoded and "resolved_path" not in encoded


class _ExecObserved(RuntimeError):
    pass


def test_first_bootstrap_scrubs_caller_internal_authority_before_fd_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    descriptor = os.open("/usr/bin/python3.10", os.O_RDONLY | os.O_CLOEXEC)
    repository_descriptor = os.open(toolchain.REPO_ROOT, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    observed: dict[str, object] = {}
    monkeypatch.setattr(bootstrap, "_initial_repository_descriptor", lambda: repository_descriptor)
    monkeypatch.setattr(bootstrap, "_open_venv_python", lambda _repository_fd: (descriptor, "a" * 64))
    monkeypatch.setattr(bootstrap, "__agentgov_loaded_sha256__", "b" * 64, raising=False)
    monkeypatch.setattr(bootstrap, "__agentgov_system_python_sha256__", "c" * 64, raising=False)

    def observe(fd: int, argv: tuple[str, ...], environ: dict[str, str]) -> None:
        observed.update(fd=fd, argv=argv, environ=environ)
        raise _ExecObserved

    monkeypatch.setattr(bootstrap.os, "execve", observe)
    caller = {
        "COMPOSE_ENV_FILE": "docker/.env",
        "HOME": "/untrusted/home",
        "PYTHONPATH": "/untrusted/python",
        "AGENT_GOV_ACCEPTANCE_LOCK_FD": "9",
        "AGENT_GOV_ACCEPTANCE_LOCK_COOKIE": "0" * 16,
        "AGENT_GOV_ACCEPTANCE_REEXEC_STAGE": "locked-snapshot-v1",
        "AGENT_GOV_ACCEPTANCE_TOOLCHAIN_EVIDENCE": "{}",
    }

    with pytest.raises(_ExecObserved):
        bootstrap.launch(caller)

    child = observed["environ"]
    argv = observed["argv"]
    assert isinstance(child, dict)
    assert isinstance(argv, tuple)
    assert child["COMPOSE_ENV_FILE"] == "docker/.env"
    assert all(key not in child for key in caller if key != "COMPOSE_ENV_FILE")
    assert child[bootstrap.BOOTSTRAP_STAGE_ENV] == "c" * 64
    assert child[bootstrap.LOADED_BOOTSTRAP_SHA256_ENV] == "b" * 64
    assert argv[6].startswith("/proc/self/fd/")
    assert argv[10] == str(bootstrap.IMPORT_AUTHORITY_PATH)
    assert argv[11] == str(bootstrap.TOOLCHAIN_PATH)
    assert child[bootstrap.BOOTSTRAP_IMPORT_AUTHORITY_SHA256_ENV]


def test_fd_import_authority_rejects_toolchain_path_replacement(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    helper = scripts / "container_acceptance_import_authority.py"
    toolchain_source = scripts / "container_acceptance_toolchain.py"
    bootstrap_source = scripts / "container_acceptance_bootstrap.py"
    shutil.copyfile(import_authority.__file__, helper)
    toolchain_source.write_text("def main():\n    return 29\n", encoding="utf-8")
    bootstrap_source.write_text("BOOTSTRAP = True\n", encoding="utf-8")
    helper_descriptor = os.open(helper, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    toolchain_descriptor = os.open(toolchain_source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    repository_descriptor = os.open(repository, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        helper_digest = hashlib.sha256(helper.read_bytes()).hexdigest()
        toolchain_digest = hashlib.sha256(toolchain_source.read_bytes()).hexdigest()
        replacement = scripts / "replacement.py"
        replacement.write_text("def main():\n    return 7\n", encoding="utf-8")
        os.replace(replacement, toolchain_source)
        result = subprocess.run(
            (
                sys.executable,
                "-I",
                "-P",
                "-S",
                f"/proc/self/fd/{helper_descriptor}",
                str(repository_descriptor),
                str(helper_descriptor),
                str(toolchain_descriptor),
                str(helper),
                str(toolchain_source),
                str(repository),
                "launch",
            ),
            check=False,
            pass_fds=(repository_descriptor, helper_descriptor, toolchain_descriptor),
            env={
                bootstrap.BOOTSTRAP_IMPORT_AUTHORITY_SHA256_ENV: helper_digest,
                bootstrap.BOOTSTRAP_TOOLCHAIN_SHA256_ENV: toolchain_digest,
                bootstrap.LOADED_BOOTSTRAP_SHA256_ENV: hashlib.sha256(bootstrap_source.read_bytes()).hexdigest(),
                "LC_ALL": "C.UTF-8",
            },
            timeout=5,
        )
    finally:
        os.close(repository_descriptor)
        os.close(toolchain_descriptor)
        os.close(helper_descriptor)

    assert result.returncode == 1


def test_actual_import_bytes_reject_path_restore_after_module_load(tmp_path: Path) -> None:
    source = tmp_path / "captured_module.py"
    source.write_text("VALUE = 29\n", encoding="utf-8")
    registry = import_authority.ActualLoadedSourceRegistry(tmp_path)
    finder = import_authority._RepositorySourceFinder(registry)
    sys.meta_path.insert(0, finder)
    sys.path.insert(0, str(tmp_path))
    try:
        module = importlib.import_module("captured_module")
        actual = registry.freeze()["captured_module.py"]
        replacement = tmp_path / "expected.py"
        replacement.write_text("VALUE = 7\n", encoding="utf-8")
        os.replace(replacement, source)
        assert module.VALUE == 29
        with pytest.raises(import_authority.ImportAuthorityError, match="drifted"):
            registry.register_expected("captured_module.py", actual)
    finally:
        sys.modules.pop("captured_module", None)
        sys.path.remove(str(tmp_path))
        sys.meta_path.remove(finder)
        os.close(registry.root_fd)


def test_repository_namespace_cannot_fall_through_to_replacement_path(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    replacement = tmp_path / "replacement"
    (trusted / "scripts").mkdir(parents=True)
    (replacement / "scripts").mkdir(parents=True)
    probe = "authority_escape_probe"
    (replacement / "scripts" / f"{probe}.py").write_text("VALUE = 7\n", encoding="utf-8")
    registry = import_authority.ActualLoadedSourceRegistry(trusted)
    finder = import_authority._RepositorySourceFinder(registry)
    package = sys.modules["scripts"]
    original_path = list(package.__path__)
    package.__path__ = [str(replacement / "scripts")]
    sys.meta_path.insert(0, finder)
    try:
        with pytest.raises(import_authority.ImportAuthorityError, match="escaped"):
            importlib.import_module(f"scripts.{probe}")
        assert f"scripts.{probe}" not in sys.modules
    finally:
        package.__path__ = original_path
        sys.meta_path.remove(finder)
        os.close(registry.root_fd)


def test_repository_sourceless_pyc_cannot_fall_through_to_default_finder(tmp_path: Path) -> None:
    source = tmp_path / "sourceless_authority_probe.py"
    bytecode = tmp_path / "sourceless_authority_probe.pyc"
    source.write_text("VALUE = 7\n", encoding="utf-8")
    py_compile.compile(str(source), cfile=str(bytecode), doraise=True)
    source.unlink()
    registry = import_authority.ActualLoadedSourceRegistry(tmp_path)
    finder = import_authority._RepositorySourceFinder(registry)
    sys.meta_path.insert(0, finder)
    sys.path.insert(0, str(registry.import_root))
    try:
        with pytest.raises(import_authority.ImportAuthorityError, match="source-only"):
            importlib.import_module("sourceless_authority_probe")
        assert "sourceless_authority_probe" not in sys.modules
    finally:
        sys.path.remove(str(registry.import_root))
        sys.meta_path.remove(finder)
        os.close(registry.root_fd)


def test_snapshot_bootstrap_inserts_verified_candidate_root_without_pythonpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = tool_authority.capture_private_state_authority().paths
    with tempfile.TemporaryDirectory(dir=paths.candidates) as raw_snapshot:
        root = Path(raw_snapshot)
        scripts = root / "repository/scripts"
        scripts.mkdir(parents=True)
        snapshot_bootstrap = scripts / "container_acceptance_bootstrap.py"
        snapshot_validator = scripts / "container_acceptance_snapshot_authority.py"
        runner = scripts / "run_container_acceptance.py"
        shutil.copyfile(bootstrap.__file__, snapshot_bootstrap)
        shutil.copyfile(snapshot_authority.__file__, snapshot_validator)
        probe = root / "repository/probe.py"
        probe.write_text("VALUE = 29\n", encoding="utf-8")
        runner.write_text("import probe\nraise SystemExit(probe.VALUE)\n", encoding="utf-8")
        for source in (snapshot_bootstrap, snapshot_validator, runner, probe):
            source.chmod(0o400)
        frontend = root / "repository/frontend/node_modules"
        python_dependencies = root / "dependencies/python-site-packages"
        pnpm_dependencies = root / "dependencies/pnpm"
        node_executable = root / "dependencies/node/bin/node"
        frontend.mkdir(parents=True)
        python_dependencies.mkdir(parents=True)
        pnpm_dependencies.mkdir(parents=True)
        node_executable.parent.mkdir(parents=True)
        node_executable.write_bytes(b"node-test")
        os.chmod(frontend, 0o500)
        os.chmod(python_dependencies, 0o500)
        os.chmod(pnpm_dependencies, 0o500)
        os.chmod(node_executable, 0o500)
        empty_digest = hashlib.sha256(b"agentgov-dependency-snapshot-v1\0").hexdigest()
        evidence = {"contract": toolchain.TOOLCHAIN_CONTRACT}
        evidence_raw = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        snapshot: list[object] = [None] * 32
        snapshot[5] = str(root)
        snapshot[7] = str(root / "repository")
        snapshot[20] = [str(frontend), [], "1" * 64, empty_digest, empty_digest, 0, 0]
        snapshot[21] = [str(python_dependencies), [], "2" * 64, empty_digest, empty_digest, 0, 0]
        snapshot[22] = [str(pnpm_dependencies), [], "3" * 64, empty_digest, empty_digest, 0, 0]
        node_digest = hashlib.sha256(node_executable.read_bytes()).hexdigest()
        snapshot[23] = [str(node_executable), [], "/source/node", [], node_digest, node_digest]
        loaded = sorted(
            [
                [path.relative_to(root / "repository").as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()]
                for path in (snapshot_bootstrap, snapshot_validator, runner, probe)
            ]
        )
        snapshot[24] = loaded
        snapshot[25] = hashlib.sha256(json.dumps(loaded, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        candidate = json.dumps({"contract": "agentgov.container-acceptance-candidate.v5", "source": [], "snapshot": snapshot})
        environ = {
            bootstrap.SNAPSHOT_ROOT_ENV: str(root),
            bootstrap.PREPARED_CANDIDATE_ENV: candidate,
            bootstrap.PREPARED_RECEIPT_ENV: "{}",
            bootstrap.TOOLCHAIN_EVIDENCE_ENV: evidence_raw,
            bootstrap.TOOLCHAIN_SHA256_ENV: hashlib.sha256(evidence_raw.encode()).hexdigest(),
            bootstrap.LOCK_FD_ENV: "3",
            bootstrap.LOCK_COOKIE_ENV: "0" * 16,
            bootstrap.REEXEC_STAGE_ENV: bootstrap.REEXEC_STAGE_VALUE,
            bootstrap.SIGNAL_HANDOFF_ENV: bootstrap.SIGNAL_HANDOFF_VALUE,
            "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCY_ROOT": str(frontend),
            "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCIES_SHA256": empty_digest,
            "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCIES_ENTRIES": "0",
            "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCIES_BYTES": "0",
            "AGENT_GOV_ACCEPTANCE_PYTHON_SITE_PACKAGES": str(python_dependencies),
            "AGENT_GOV_ACCEPTANCE_PYTHON_DEPENDENCIES_SHA256": empty_digest,
            "AGENT_GOV_ACCEPTANCE_PYTHON_DEPENDENCIES_ENTRIES": "0",
            "AGENT_GOV_ACCEPTANCE_PYTHON_DEPENDENCIES_BYTES": "0",
            "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCY_ROOT": str(pnpm_dependencies),
            "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCIES_SHA256": empty_digest,
            "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCIES_ENTRIES": "0",
            "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCIES_BYTES": "0",
            "AGENT_GOV_ACCEPTANCE_NODE": str(node_executable),
        }
        os.chmod(root, 0o500)
        snapshot[6] = bootstrap._path_identity(root.stat(follow_symlinks=False))
        snapshot[8] = bootstrap._path_identity((root / "repository").stat(follow_symlinks=False))
        snapshot[20][1] = bootstrap._path_identity(frontend.stat(follow_symlinks=False))
        snapshot[21][1] = bootstrap._path_identity(python_dependencies.stat(follow_symlinks=False))
        snapshot[22][1] = bootstrap._path_identity(pnpm_dependencies.stat(follow_symlinks=False))
        snapshot[23][1] = bootstrap._path_identity(node_executable.stat(follow_symlinks=False))
        snapshot[31] = bootstrap._path_identity((root / "dependencies").stat(follow_symlinks=False))
        environ[bootstrap.PREPARED_CANDIDATE_ENV] = json.dumps({"contract": "agentgov.container-acceptance-candidate.v5", "source": [], "snapshot": snapshot})
        bootstrap_descriptor = os.open(snapshot_bootstrap, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        monkeypatch.setattr(bootstrap, "__file__", f"/proc/self/fd/{bootstrap_descriptor}")
        original_path = list(sys.path)
        original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            with pytest.raises(SystemExit, match="29"):
                bootstrap.resume([str(runner)], environ)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)
            sys.path[:] = original_path
            os.close(bootstrap_descriptor)
            os.chmod(root, 0o700)


def test_tool_hash_and_exec_rechecks_reject_path_replacement() -> None:
    paths = tool_authority.capture_private_state_authority().paths
    with tempfile.TemporaryDirectory(dir=paths.candidates) as raw_directory:
        directory = Path(raw_directory)
        target = directory / "target"
        target.write_bytes(b"#!/bin/sh\nexit 0\n")
        target.chmod(0o700)
        invocation = directory / "tool"
        invocation.symlink_to("target")
        record, _encoded = tool_authority.capture_tool_file(invocation, "tool", allow_symlink=True, executable=True)
        replacement = directory / "replacement"
        replacement.symlink_to("target")
        original_hash = tool_authority._hash_descriptor

        def replace_after_hash(fd: int, identity: os.stat_result, *, capture: bool) -> tuple[str, bytes | None]:
            result = original_hash(fd, identity, capture=capture)
            os.replace(replacement, invocation)
            return result

        try:
            tool_authority._hash_descriptor = replace_after_hash
            with pytest.raises(tool_authority.VerifiedToolFileAuthorityError) as rejected:
                tool_authority.open_verified_executable(record)
            assert isinstance(rejected.value.__cause__, tool_authority.ToolFileAuthorityError)
            assert "drifted" in str(rejected.value.__cause__)
        finally:
            tool_authority._hash_descriptor = original_hash


def test_dependency_merkle_is_bounded_before_sort_and_rejects_escaping_links(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "dependencies"
    root.mkdir()
    (root / "nested").mkdir()
    (root / "nested/a").write_text("a", encoding="utf-8")
    (root / "b").write_text("b", encoding="utf-8")
    monkeypatch.setattr(dependency_authority, "MAX_DEPENDENCY_ENTRIES", 1)
    monkeypatch.setattr(dependency_authority.os, "listdir", lambda *_args: pytest.fail("unbounded listdir used"))

    with pytest.raises(dependency_authority.DependencyAuthorityError, match="entry limit"):
        dependency_authority.capture_dependency_tree(root)

    monkeypatch.setattr(dependency_authority, "MAX_DEPENDENCY_ENTRIES", 50_000)
    (root / "outside").symlink_to("/etc/passwd")
    with pytest.raises(dependency_authority.DependencyAuthorityError, match="escapes"):
        dependency_authority.capture_dependency_tree(root)
    (root / "outside").unlink()

    captured = dependency_authority.capture_dependency_tree(root)
    target = root / "nested/a"
    original_mtime = target.stat().st_mtime_ns
    replacement = root / "nested/replacement"
    replacement.write_text("a", encoding="utf-8")
    os.replace(replacement, target)
    os.utime(target, ns=(original_mtime, original_mtime))
    observed = dependency_authority.capture_dependency_tree(root)

    assert {key: value for key, value in observed.items() if key != "generation_sha256"} == {
        key: value for key, value in captured.items() if key != "generation_sha256"
    }
    assert observed["generation_sha256"] != captured["generation_sha256"]
    with pytest.raises(dependency_authority.DependencyAuthorityError, match="generation authority drifted"):
        dependency_authority.require_dependency_generation_current(captured)
    malformed = dict(captured)
    malformed.pop("generation_sha256")
    with pytest.raises(dependency_authority.DependencyAuthorityError, match="generation evidence is invalid"):
        dependency_authority.require_dependency_generation_current(malformed)  # type: ignore[arg-type]


def test_docker_daemon_evidence_hashes_identity_without_persisting_raw_values(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = os.stat("/run/docker.sock", follow_symlinks=False)
    monkeypatch.setattr(docker_authority, "_socket_identity", lambda: identity)
    payloads = {
        "/version": {"Version": "v", "ApiVersion": "a", "MinAPIVersion": "m"},
        "/info": {"ID": "private-id", "Name": "private-host", "ServerVersion": "v"},
    }
    monkeypatch.setattr(docker_authority, "_read_daemon_payload", payloads.__getitem__)

    evidence = docker_authority.capture_docker_daemon_authority()

    assert set(evidence) == {"socket_authority_sha256", "daemon_identity_sha256"}
    assert all(len(value) == 64 for value in evidence.values())
    assert "private" not in json.dumps(evidence)
