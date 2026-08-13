from __future__ import annotations

import hashlib
import importlib
import json
import os
import py_compile
import sys
import tempfile
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any, cast

import pytest

from runtime_container_acceptance_test_support import load_module

REPO_ROOT = Path(__file__).resolve().parents[1]
scripts_namespace: Any = sys.modules.setdefault("scripts", ModuleType("scripts"))
scripts_namespace.__path__ = [str(REPO_ROOT / "scripts")]
tool_authority = load_module("scripts.container_acceptance_tool_authority", REPO_ROOT / "scripts/container_acceptance_tool_authority.py")
scripts_namespace.container_acceptance_tool_authority = tool_authority
bootstrap = load_module("scripts.container_acceptance_bootstrap", REPO_ROOT / "scripts/container_acceptance_bootstrap.py")
import_authority = load_module("scripts.container_acceptance_import_authority", REPO_ROOT / "scripts/container_acceptance_import_authority.py")
snapshot_authority = load_module("scripts.container_acceptance_snapshot_authority", REPO_ROOT / "scripts/container_acceptance_snapshot_authority.py")
snapshot_exec = load_module("scripts.container_acceptance_snapshot_exec", REPO_ROOT / "scripts/container_acceptance_snapshot_exec.py")


def _frozen_registry(
    repository: Path,
    expected: dict[str, str],
) -> tuple[Any, Any]:
    descriptor = os.open(repository, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        reused_fd = os.dup(descriptor)
        os.close(reused_fd)
        import_root = f"/proc/self/fd/{reused_fd}"
        adjacent = f"{import_root}0"
        sentinel = object()
        sys.path_importer_cache[import_root] = None
        sys.path_importer_cache[f"{import_root}/app"] = None
        sys.path_importer_cache[adjacent] = sentinel
        anchor = next(iter(expected.items()))
        registry = snapshot_authority._FrozenSourceRegistry(repository, descriptor, expected, {anchor[0]: anchor[1]})
    finally:
        os.close(descriptor)
    assert registry.root_fd == reused_fd
    assert import_root not in sys.path_importer_cache
    assert f"{import_root}/app" not in sys.path_importer_cache
    assert sys.path_importer_cache.pop(adjacent) is sentinel
    finder = snapshot_authority._FrozenSourceFinder(registry)
    return registry, finder


def test_pre_registry_repository_ancestor_swap_is_rejected_before_replacement_source_executes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = tool_authority.capture_private_state_authority().paths.candidates
    with tempfile.TemporaryDirectory(dir=candidates) as raw_parent:
        parent = Path(raw_parent)
        repository = parent / "repository"
        repository.mkdir()
        descriptor = os.open(repository, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
        identity = bootstrap._stat_identity(os.fstat(descriptor))
        displaced = parent / "displaced"
        marker = parent / "replacement-executed"
        repository.rename(displaced)
        repository.mkdir()
        (repository / "replacement.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
        monkeypatch.setattr(bootstrap, "REPO_ROOT", repository)
        monkeypatch.setattr(bootstrap, "_INJECTED_REPOSITORY_FD", descriptor)
        monkeypatch.setattr(bootstrap, "_INJECTED_REPOSITORY_IDENTITY", identity)
        try:
            with pytest.raises(bootstrap.BootstrapAuthorityError, match="drifted"):
                bootstrap._initial_repository_descriptor()
        finally:
            os.close(descriptor)

        assert not marker.exists()


def test_snapshot_runner_leaf_drift_is_rejected_before_sentinel_executes(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    anchor = repository / "anchor.py"
    runner = repository / "runner.py"
    marker = tmp_path / "runner-executed"
    anchor.write_text("VALUE = 1\n", encoding="utf-8")
    runner.write_text("VALUE = 29\n", encoding="utf-8")
    expected = {
        "anchor.py": hashlib.sha256(anchor.read_bytes()).hexdigest(),
        "runner.py": hashlib.sha256(runner.read_bytes()).hexdigest(),
    }
    registry, _finder = _frozen_registry(repository, expected)
    runner.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
    try:
        with pytest.raises(snapshot_authority.SnapshotAuthorityError, match="drifted"):
            registry.capture(registry.import_root / "runner.py", package=False)
    finally:
        registry.close()

    assert not marker.exists()


def test_snapshot_unregistered_repository_import_is_rejected_before_sentinel_executes(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    anchor = repository / "anchor.py"
    surprise = repository / "surprise.py"
    marker = tmp_path / "surprise-executed"
    anchor.write_text("VALUE = 1\n", encoding="utf-8")
    surprise.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
    registry, finder = _frozen_registry(repository, {"anchor.py": hashlib.sha256(anchor.read_bytes()).hexdigest()})
    sys.meta_path.insert(0, finder)
    sys.path.insert(0, str(registry.import_root))
    try:
        with pytest.raises(snapshot_authority.SnapshotAuthorityError, match="not frozen"):
            importlib.import_module("surprise")
    finally:
        sys.modules.pop("surprise", None)
        sys.path.remove(str(registry.import_root))
        sys.meta_path.remove(finder)
        registry.close()

    assert not marker.exists()


def test_snapshot_repository_pyc_is_rejected_before_sentinel_executes(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    anchor = repository / "anchor.py"
    package = repository / "sourceless"
    package.mkdir()
    source = package / "__init__.py"
    bytecode = package / "__init__.pyc"
    marker = tmp_path / "pyc-executed"
    anchor.write_text("VALUE = 1\n", encoding="utf-8")
    source.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
    py_compile.compile(str(source), cfile=str(bytecode), doraise=True)
    source.unlink()
    registry, finder = _frozen_registry(repository, {"anchor.py": hashlib.sha256(anchor.read_bytes()).hexdigest()})
    sys.meta_path.insert(0, finder)
    sys.path.insert(0, str(registry.import_root))
    try:
        with pytest.raises(snapshot_authority.SnapshotAuthorityError, match="source-only"):
            importlib.import_module("sourceless")
    finally:
        sys.modules.pop("sourceless", None)
        sys.path.remove(str(registry.import_root))
        sys.meta_path.remove(finder)
        registry.close()

    assert not marker.exists()


def test_snapshot_agentgov_testkit_cannot_fall_back_to_an_external_same_name_package(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    external = tmp_path / "external/agentgov_testkit"
    repository.mkdir()
    external.mkdir(parents=True)
    anchor = repository / "anchor.py"
    marker = tmp_path / "external-package-executed"
    anchor.write_text("VALUE = 1\n", encoding="utf-8")
    (external / "__init__.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
    registry, finder = _frozen_registry(repository, {"anchor.py": hashlib.sha256(anchor.read_bytes()).hexdigest()})
    sys.meta_path.insert(0, finder)
    sys.path[:0] = [str(registry.import_root / "packages/agentgov-testkit/src"), str(external.parent)]
    previous = sys.modules.pop("agentgov_testkit", None)
    try:
        with pytest.raises(snapshot_authority.SnapshotAuthorityError, match="escaped"):
            importlib.import_module("agentgov_testkit")
    finally:
        sys.modules.pop("agentgov_testkit", None)
        if previous is not None:
            sys.modules["agentgov_testkit"] = previous
        del sys.path[:2]
        sys.meta_path.remove(finder)
        registry.close()

    assert not marker.exists()


def test_snapshot_authority_second_read_drift_is_rejected_before_sentinel_executes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    source = scripts / "container_acceptance_snapshot_authority.py"
    marker = tmp_path / "authority-executed"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    replacement = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n".encode()
    repository_fd = os.open(repository, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)

    def drift(_path: Path, _repository_fd: int | None = None) -> tuple[int, str]:
        descriptor = os.open(source, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        os.pwrite(descriptor, replacement, 0)
        os.ftruncate(descriptor, len(replacement))
        return descriptor, expected

    monkeypatch.setattr(bootstrap, "REPO_ROOT", repository)
    monkeypatch.setattr(bootstrap, "_open_bootstrap_source", drift)
    try:
        with pytest.raises(bootstrap.BootstrapAuthorityError, match="actual bytes drifted"):
            bootstrap._load_snapshot_authority(
                repository_fd,
                {"scripts/container_acceptance_snapshot_authority.py": expected},
                "b" * 64,
            )
    finally:
        os.close(repository_fd)

    assert not marker.exists()


def test_snapshot_authority_cold_load_needs_no_repository_package_and_restores_failed_module(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    source = scripts / "container_acceptance_snapshot_authority.py"
    source.write_bytes(Path(snapshot_authority.__file__).read_bytes())
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    repository_fd = os.open(repository, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    module_name = "scripts.container_acceptance_snapshot_authority"
    previous_scripts = sys.modules.pop("scripts", None)
    previous_module = sys.modules.pop(module_name, None)
    monkeypatch.setattr(bootstrap, "REPO_ROOT", repository)
    try:
        module, digest = bootstrap._load_snapshot_authority(
            repository_fd,
            {"scripts/container_acceptance_snapshot_authority.py": expected},
            "b" * 64,
        )
        assert digest == expected
        assert sys.modules[module_name] is module
        assert "scripts" not in sys.modules

        source.write_text("raise RuntimeError('cold-load-sentinel')\n", encoding="utf-8")
        failed_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        with pytest.raises(RuntimeError, match="cold-load-sentinel"):
            bootstrap._load_snapshot_authority(
                repository_fd,
                {"scripts/container_acceptance_snapshot_authority.py": failed_digest},
                "b" * 64,
            )
        assert sys.modules[module_name] is module

        cast(dict[str, Any], sys.modules)[module_name] = None
        with pytest.raises(RuntimeError, match="cold-load-sentinel"):
            bootstrap._load_snapshot_authority(
                repository_fd,
                {"scripts/container_acceptance_snapshot_authority.py": failed_digest},
                "b" * 64,
            )
        assert module_name in sys.modules
        assert sys.modules[module_name] is None
    finally:
        os.close(repository_fd)
        sys.modules.pop(module_name, None)
        if previous_module is not None:
            sys.modules[module_name] = previous_module
        if previous_scripts is not None:
            sys.modules["scripts"] = previous_scripts


def test_snapshot_bootstrap_drift_is_rejected_before_toolchain_executes_sentinel() -> None:
    candidates = tool_authority.capture_private_state_authority().paths.candidates
    with tempfile.TemporaryDirectory(dir=candidates) as raw_root:
        root = Path(raw_root)
        scripts = root / "repository/scripts"
        scripts.mkdir(parents=True)
        marker = root / "bootstrap-executed"
        entrypoint = scripts / "container_acceptance_bootstrap.py"
        entrypoint.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
        runner = scripts / "run_container_acceptance.py"
        snapshot: list[object] = [None] * 32
        snapshot[5] = str(root)
        snapshot[7] = str(root / "repository")
        snapshot[24] = [["scripts/container_acceptance_bootstrap.py", "a" * 64]]
        environment = {
            "AGENT_GOV_ACCEPTANCE_SNAPSHOT_ROOT": str(root),
            "AGENT_GOV_PREPARED_CANDIDATE_AUTHORITY": json.dumps(
                {"contract": "agentgov.container-acceptance-candidate.v5", "source": [], "snapshot": snapshot}
            ),
        }
        python_record, _encoded = tool_authority.capture_tool_file(
            Path("/usr/bin/python3.10"),
            "python",
            executable=True,
        )

        with pytest.raises(snapshot_exec.SnapshotExecAuthorityError, match="Prepared"):
            snapshot_exec.exec_snapshot_python(
                entrypoint,
                ("resume", str(runner)),
                environment,
                python_record=python_record,
            )

        assert not marker.exists()


@pytest.mark.parametrize("registered", (False, True))
def test_initial_source_loader_rejects_world_writable_python_before_execution(
    tmp_path: Path,
    registered: bool,
) -> None:
    source = tmp_path / "writable_probe.py"
    anchor = tmp_path / "anchor.py"
    marker = tmp_path / "writable-source-executed"
    source.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
    anchor.write_text("VALUE = 1\n", encoding="utf-8")
    source.chmod(0o666)
    expected_path = source if registered else anchor
    expected = {expected_path.name: hashlib.sha256(expected_path.read_bytes()).hexdigest()}
    registry = import_authority.ActualLoadedSourceRegistry(tmp_path, expected=expected)
    finder = import_authority._RepositorySourceFinder(registry)
    sys.path_importer_cache.pop(str(registry.import_root), None)
    sys.meta_path.insert(0, finder)
    sys.path.insert(0, str(registry.import_root))
    try:
        with pytest.raises(import_authority.ImportAuthorityError, match="identity"):
            importlib.import_module("writable_probe")
    finally:
        sys.modules.pop("writable_probe", None)
        sys.path.remove(str(registry.import_root))
        sys.meta_path.remove(finder)
        os.close(registry.root_fd)

    assert not marker.exists()


def test_initial_source_loader_accepts_repository_group_writable_mode(tmp_path: Path) -> None:
    source = tmp_path / "group_writable_probe.py"
    source.write_text("VALUE = 29\n", encoding="utf-8")
    source.chmod(0o664)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    descriptors_before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(import_authority.ImportAuthorityError, match="frozen"):
        import_authority.ActualLoadedSourceRegistry(tmp_path, expected={})
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(import_authority.ImportAuthorityError, match="drifted"):
            import_authority.install_frozen_repository_imports(
                tmp_path,
                descriptor,
                {source.name: "0" * 64},
                preloaded={source.name: "0" * 64},
            )
    finally:
        os.close(descriptor)
    assert len(os.listdir("/proc/self/fd")) == descriptors_before
    registry = import_authority.ActualLoadedSourceRegistry(tmp_path, expected={source.name: digest})

    try:
        captured = registry.capture(source, package=False)
    finally:
        os.close(registry.root_fd)

    assert captured.sha256 == digest


def test_initial_source_loader_bounds_complete_application_import_graph(tmp_path: Path) -> None:
    registry = import_authority.ActualLoadedSourceRegistry(tmp_path)
    try:
        for index in range(import_authority._MAX_LOADED_SOURCES):
            registry._record(PurePosixPath(f"app/module_{index}.py"), f"{index:064x}")
        with pytest.raises(import_authority.ImportAuthorityError, match="oversized"):
            registry._record(PurePosixPath("app/one_too_many.py"), "f" * 64)
    finally:
        os.close(registry.root_fd)

    assert import_authority._MAX_LOADED_SOURCES == 512
