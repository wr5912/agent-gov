from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
import scripts.agent_test_acceptance_support as acceptance_support
import scripts.container_acceptance_candidate as candidate_authority
import scripts.container_acceptance_candidate_authority as candidate_contract
import scripts.container_acceptance_candidate_cleanup as candidate_cleanup
import scripts.container_acceptance_candidate_projection as candidate_projection
import scripts.container_acceptance_candidate_storage as candidate_storage
import scripts.container_acceptance_dependency_authority as dependency_authority
import scripts.container_acceptance_toolchain as acceptance_toolchain
from app.runtime.agent_git_environment import governed_git_command, governed_git_environment, require_governed_repository


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env={
            **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
            "GIT_AUTHOR_NAME": "AgentGov Test",
            "GIT_AUTHOR_EMAIL": "agentgov-test@example.invalid",
            "GIT_COMMITTER_NAME": "AgentGov Test",
            "GIT_COMMITTER_EMAIL": "agentgov-test@example.invalid",
        },
    )
    return result.stdout.strip()


class _LocalGitAuthority:
    def validate(self) -> None:
        return None

    def run(
        self,
        repository: Path,
        arguments: Sequence[str],
        *,
        index_root: candidate_storage.OpenSnapshotRoot | None = None,
        index_descriptor: int | None = None,
        input_bytes: bytes | None = None,
        max_output_bytes: int = 4096,
    ) -> bytes:
        if index_root is not None and index_descriptor is not None:
            raise AssertionError("ambiguous fake index")
        descriptor = index_root.descriptor if index_root else index_descriptor
        require_governed_repository(repository)
        result = subprocess.run(
            governed_git_command(repository, arguments),
            cwd=repository,
            env=governed_git_environment(
                repository=repository,
                index_file=(
                    Path(f"/proc/self/fd/{index_root.descriptor}/index")
                    if index_root
                    else Path(f"/proc/self/fd/{index_descriptor}")
                    if index_descriptor is not None
                    else None
                ),
                optional_locks=False,
            ),
            input=input_bytes,
            capture_output=True,
            check=False,
            pass_fds=(descriptor,) if descriptor is not None else (),
        )
        if result.returncode or len(result.stdout) > max_output_bytes:
            raise candidate_authority.CandidateSnapshotError("fake Git authority failed")
        return result.stdout


LOCAL_GIT = _LocalGitAuthority()


def _require_toolchain_authority(condition: bool, message: str) -> None:
    if not condition:
        raise acceptance_toolchain.ToolchainAuthorityError(message)


def _candidate_repository(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    repository.joinpath(".gitignore").write_text("private/\nartifacts/\n.venv/\nnode_modules/\n", encoding="utf-8")
    repository.joinpath("tracked.txt").write_text("base\n", encoding="utf-8")
    repository.joinpath("docker/runtime-bootstrap").mkdir(parents=True)
    repository.joinpath("docker/runtime-bootstrap/README.md").write_text("bootstrap\n", encoding="utf-8")
    repository.joinpath("frontend").mkdir()
    repository.joinpath("frontend/package.json").write_text('{"name":"candidate"}\n', encoding="utf-8")
    repository.joinpath("scripts").mkdir()
    repository.joinpath("scripts/run_container_acceptance.py").write_text("print('candidate')\n", encoding="utf-8")
    repository.joinpath("authority/hooks").mkdir(parents=True)
    repository.joinpath("authority/hooks/guard.py").write_text("ENABLED = True\n", encoding="utf-8")
    repository.joinpath("authority/hooks.json").write_text("{}\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "base")
    repository.joinpath("tracked.txt").write_text("candidate\n", encoding="utf-8")
    repository.joinpath("included.txt").write_text("included\n", encoding="utf-8")
    repository.joinpath(".obsidian").mkdir()
    repository.joinpath(".obsidian/preferences.json").write_text("{}\n", encoding="utf-8")
    repository.joinpath("private").mkdir()
    env_file = repository / "private/selected.env"
    env_file.write_text("PRIVATE_VALUE=first\n", encoding="utf-8")
    env_file.chmod(0o600)
    repository.joinpath("private/runtime.sqlite3").write_bytes(b"runtime")
    repository.joinpath(".venv").mkdir()
    repository.joinpath(".venv/ignored.py").write_text("ignored\n", encoding="utf-8")
    repository.joinpath("frontend/node_modules").mkdir(parents=True)
    repository.joinpath("frontend/node_modules/ignored.js").write_text("ignored\n", encoding="utf-8")
    return repository, env_file


@dataclass(frozen=True, slots=True)
class _CandidateRequirements:
    parent: acceptance_toolchain.CandidateSnapshotParentRequirement
    dependencies: Path
    captured: dependency_authority.DependencyTreeAuthority
    frontend: acceptance_toolchain.FrontendDependencyProjectionRequirement
    python: acceptance_toolchain.PythonDependencySnapshotRequirement
    pnpm: acceptance_toolchain.PnpmDependencySnapshotRequirement
    node_source: Path
    node: acceptance_toolchain.NodeExecutableSnapshotRequirement


def _candidate_requirements(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    parent = _candidate_parent_requirement(tmp_path)
    node_source, node = _candidate_node_requirement(tmp_path)
    dependencies, captured, frontend, python, pnpm = _candidate_dependency_requirements(tmp_path)
    requirements = _CandidateRequirements(parent, dependencies, captured, frontend, python, pnpm, node_source, node)
    _install_candidate_requirements(monkeypatch, requirements)
    return dependencies


def _candidate_parent_requirement(tmp_path: Path) -> acceptance_toolchain.CandidateSnapshotParentRequirement:
    parent = tmp_path / "candidates"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    identity = parent.stat(follow_symlinks=False)
    return acceptance_toolchain.CandidateSnapshotParentRequirement(
        parent,
        identity.st_dev,
        identity.st_ino,
        identity.st_mode & 0o777,
        identity.st_uid,
        identity.st_gid,
    )


def _candidate_dependency_requirements(
    tmp_path: Path,
) -> tuple[
    Path,
    dependency_authority.DependencyTreeAuthority,
    acceptance_toolchain.FrontendDependencyProjectionRequirement,
    acceptance_toolchain.PythonDependencySnapshotRequirement,
    acceptance_toolchain.PnpmDependencySnapshotRequirement,
]:
    dependencies = tmp_path / "dependencies"
    dependencies.mkdir()
    dependencies.joinpath("package.json").write_text('{"name":"dependencies"}\n', encoding="utf-8")
    dependencies.joinpath("packages/example").mkdir(parents=True)
    dependencies.joinpath("packages/example/index.js").write_text("export default 1\n", encoding="utf-8")
    dependencies.joinpath("bin").mkdir()
    dependencies.joinpath("bin/pnpm.cjs").write_text("console.log('pnpm')\n", encoding="utf-8")
    dependencies.joinpath("relative-package").symlink_to("packages/example", target_is_directory=True)
    dependencies.joinpath("absolute-package").symlink_to(
        dependencies / "packages/example",
        target_is_directory=True,
    )
    captured = dependency_authority.capture_dependency_tree(dependencies)
    projection = acceptance_toolchain.FrontendDependencyProjectionRequirement(
        Path(captured["root"]),
        captured["device"],
        captured["inode"],
        captured["mode"],
        captured["uid"],
        captured["gid"],
        captured["mtime_ns"],
        captured["ctime_ns"],
        captured["entries"],
        captured["regular_bytes"],
        captured["sha256"],
        captured["projection_sha256"],
    )
    python = acceptance_toolchain.PythonDependencySnapshotRequirement(
        projection.target_root,
        projection.device,
        projection.inode,
        projection.mode,
        projection.uid,
        projection.gid,
        projection.mtime_ns,
        projection.ctime_ns,
        projection.entries,
        projection.regular_bytes,
        projection.sha256,
        projection.projection_sha256,
    )
    pnpm = acceptance_toolchain.PnpmDependencySnapshotRequirement(
        projection.target_root,
        projection.device,
        projection.inode,
        projection.mode,
        projection.uid,
        projection.gid,
        projection.mtime_ns,
        projection.ctime_ns,
        projection.entries,
        projection.regular_bytes,
        projection.sha256,
        projection.projection_sha256,
    )
    return dependencies, captured, projection, python, pnpm


def _candidate_node_requirement(tmp_path: Path) -> tuple[Path, acceptance_toolchain.NodeExecutableSnapshotRequirement]:
    node_source = tmp_path / "node-source"
    node_source.write_bytes(b"fake-node-executable\n")
    node_source.chmod(0o500)
    node_identity = node_source.stat(follow_symlinks=False)
    return node_source, acceptance_toolchain.NodeExecutableSnapshotRequirement(
        node_source,
        node_identity.st_dev,
        node_identity.st_ino,
        node_identity.st_mode & 0o777,
        node_identity.st_uid,
        node_identity.st_gid,
        node_identity.st_size,
        node_identity.st_mtime_ns,
        node_identity.st_ctime_ns,
        hashlib.sha256(node_source.read_bytes()).hexdigest(),
    )


def _install_candidate_requirements(monkeypatch: pytest.MonkeyPatch, requirements: _CandidateRequirements) -> None:
    parent_requirement = requirements.parent
    dependencies = requirements.dependencies
    captured = requirements.captured
    projection = requirements.frontend
    python = requirements.python
    pnpm = requirements.pnpm
    node_source = requirements.node_source
    node = requirements.node

    def validate_parent(observed: acceptance_toolchain.CandidateSnapshotParentRequirement) -> None:
        _require_toolchain_authority(observed == parent_requirement, "test parent drifted")

    def validate_projection(observed: acceptance_toolchain.FrontendDependencyProjectionRequirement) -> None:
        _require_toolchain_authority(
            observed == projection and dependency_authority.capture_dependency_tree(dependencies) == captured,
            "test dependency drifted",
        )

    def validate_python(observed: acceptance_toolchain.PythonDependencySnapshotRequirement) -> None:
        _require_toolchain_authority(
            observed == python and dependency_authority.capture_dependency_tree(dependencies) == captured,
            "test Python dependency drifted",
        )

    def validate_pnpm(observed: acceptance_toolchain.PnpmDependencySnapshotRequirement) -> None:
        _require_toolchain_authority(
            observed == pnpm and dependency_authority.capture_dependency_tree(dependencies) == captured,
            "test pnpm dependency drifted",
        )

    def validate_node(observed: acceptance_toolchain.NodeExecutableSnapshotRequirement) -> None:
        current = node_source.stat(follow_symlinks=False)
        current_sha256 = hashlib.sha256(node_source.read_bytes()).hexdigest()
        _require_toolchain_authority(
            observed == node and (current.st_mtime_ns, current.st_ctime_ns, current_sha256) == (node.mtime_ns, node.ctime_ns, node.sha256),
            "test Node executable drifted",
        )

    monkeypatch.setattr(acceptance_toolchain, "candidate_snapshot_parent_requirement", lambda: parent_requirement)
    monkeypatch.setattr(acceptance_toolchain, "validate_candidate_snapshot_parent", validate_parent)
    monkeypatch.setattr(acceptance_toolchain, "frontend_dependency_projection_requirement", lambda: projection)
    monkeypatch.setattr(acceptance_toolchain, "validate_frontend_dependency_projection", validate_projection)
    monkeypatch.setattr(acceptance_toolchain, "python_dependency_snapshot_requirement", lambda: python)
    monkeypatch.setattr(acceptance_toolchain, "validate_python_dependency_snapshot", validate_python)
    monkeypatch.setattr(acceptance_toolchain, "pnpm_dependency_snapshot_requirement", lambda: pnpm)
    monkeypatch.setattr(acceptance_toolchain, "validate_pnpm_dependency_snapshot", validate_pnpm)
    monkeypatch.setattr(acceptance_toolchain, "node_executable_snapshot_requirement", lambda: node)
    monkeypatch.setattr(acceptance_toolchain, "validate_node_executable_snapshot", validate_node)


def _loaded_sources(repository: Path) -> tuple[acceptance_support.LoadedSourceIdentity, ...]:
    relative = "scripts/run_container_acceptance.py"
    digest = hashlib.sha256(repository.joinpath(relative).read_bytes()).hexdigest()
    return (acceptance_support.LoadedSourceIdentity(relative, digest),)


def _prepare_candidate(
    repository: Path,
    env_file: Path,
    *,
    run_id: str,
) -> acceptance_support.PreparedCandidateAuthority:
    reservation = acceptance_support.reserve_candidate_snapshot(
        repository,
        env_file,
        run_id=run_id,
        profile="agent-test",
    )
    restored = acceptance_support.CandidateSnapshotReservation.from_json(reservation.to_json())
    assert restored == reservation
    return acceptance_support.prepare_candidate_snapshot(
        reservation,
        reserved_receipt_sha256="d" * 64,
        loaded_sources=_loaded_sources(repository),
        git_authority=LOCAL_GIT,
    )


def test_candidate_tree_matches_future_staged_and_commit_identity_without_touching_real_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, env_file = _candidate_repository(tmp_path)
    _candidate_requirements(monkeypatch, tmp_path)
    prepared = _prepare_candidate(repository, env_file, run_id="1700000000-a1b2c3d4e5f6")
    witness = candidate_authority.freeze_candidate_source_current(prepared, git_authority=LOCAL_GIT)
    candidate = acceptance_support.AcceptanceCandidateIdentity(
        prepared.source.git_tree_sha,
        prepared.source.selected_env_sha256,
    )
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "foreign-git-dir"))
    assert _git(repository, "diff", "--cached", "--quiet") == ""
    acceptance_support.cleanup_candidate_snapshot(prepared)
    candidate_authority.require_frozen_candidate_source_current(prepared, witness, git_authority=LOCAL_GIT)
    tracked = repository / "included.txt"
    before = tracked.stat()
    tracked.write_bytes(tracked.read_bytes())
    os.utime(tracked, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(candidate_contract.CandidateSnapshotError, match="generation changed"):
        candidate_authority.require_frozen_candidate_source_current(prepared, witness, git_authority=LOCAL_GIT)
    _git(repository, "add", "-A", "--", ".", ":(exclude).obsidian", ":(exclude).obsidian/**")
    assert acceptance_support.staged_tree_sha(repository, git_authority=LOCAL_GIT) == candidate.git_tree_sha
    _git(repository, "commit", "-qm", "candidate")
    assert acceptance_support.revision_tree_sha(repository, git_authority=LOCAL_GIT) == candidate.git_tree_sha
    witness.close()


def test_candidate_ignores_private_assets_but_tracks_source_and_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repository, env_file = _candidate_repository(tmp_path)
    _candidate_requirements(monkeypatch, tmp_path)
    first = _prepare_candidate(repository, env_file, run_id="1700000000-a1b2c3d4e5f6")
    initial = (first.source.git_tree_sha, first.source.selected_env_sha256)
    acceptance_support.cleanup_candidate_snapshot(first)
    repository.joinpath(".obsidian/preferences.json").write_text('{"theme":"dark"}\n', encoding="utf-8")
    repository.joinpath("private/runtime.sqlite3").write_bytes(b"changed runtime")
    ignored = _prepare_candidate(repository, env_file, run_id="1700000001-a1b2c3d4e5f6")
    assert (ignored.source.git_tree_sha, ignored.source.selected_env_sha256) == initial
    acceptance_support.cleanup_candidate_snapshot(ignored)
    repository.joinpath("included.txt").write_text("changed candidate\n", encoding="utf-8")
    source = _prepare_candidate(repository, env_file, run_id="1700000002-a1b2c3d4e5f6")
    assert source.source.git_tree_sha != initial[0] and source.source.selected_env_sha256 == initial[1]
    acceptance_support.cleanup_candidate_snapshot(source)
    env_file.write_text("PRIVATE_VALUE=second\n", encoding="utf-8")
    env = _prepare_candidate(repository, env_file, run_id="1700000003-a1b2c3d4e5f6")
    assert env.source.git_tree_sha == source.source.git_tree_sha
    assert env.source.selected_env_sha256 != source.source.selected_env_sha256
    acceptance_support.cleanup_candidate_snapshot(env)
    original_env = env_file.with_suffix(".original")
    env_file.rename(original_env)
    env_file.symlink_to(original_env)
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="safely"):
        _prepare_candidate(repository, env_file, run_id="1700000004-a1b2c3d4e5f6")
    env_file.unlink()
    original_env.rename(env_file)
    _git(repository, "add", "-f", ".venv/ignored.py")
    _git(repository, "commit", "-qm", "tracked private")
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="private runtime"):
        _prepare_candidate(repository, env_file, run_id="1700000005-a1b2c3d4e5f6")


def test_candidate_snapshot_serializes_allows_runtime_and_cleans_after_dependency_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, env_file = _candidate_repository(tmp_path)
    dependencies = _candidate_requirements(monkeypatch, tmp_path)
    index_before = repository.joinpath(".git/index").read_bytes()
    prepared = _prepare_candidate(repository, env_file, run_id="1700000000-a1b2c3d4e5f6")
    restored = acceptance_support.PreparedCandidateAuthority.from_json(prepared.to_json())
    assert restored == prepared
    assert acceptance_support.CandidateRecoveryAuthority.from_json(prepared.recovery.to_json()) == prepared.recovery
    assert prepared.source_repository_root == repository and prepared.snapshot_repository_root != repository
    acceptance_support.require_snapshot_loaded_file(restored, restored.snapshot_runner_path, Path("scripts/run_container_acceptance.py"))
    acceptance_support.require_snapshot_loaded_sources(
        restored,
        (restored.snapshot_runner_path,),
        required_relative_paths=(Path("scripts/run_container_acceptance.py"),),
    )
    repository_fd = os.open(restored.snapshot_repository_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fd_runner = Path(f"/proc/self/fd/{repository_fd}/scripts/run_container_acceptance.py")
        acceptance_support.require_snapshot_loaded_file(restored, fd_runner, Path("scripts/run_container_acceptance.py"))
        acceptance_support.require_snapshot_loaded_sources(
            restored,
            (fd_runner,),
            required_relative_paths=(Path("scripts/run_container_acceptance.py"),),
        )
    finally:
        os.close(repository_fd)
    linked_repository = tmp_path / "linked-candidate-repository"
    linked_repository.symlink_to(restored.snapshot_repository_root, target_is_directory=True)
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="candidate snapshot"):
        acceptance_support.require_snapshot_loaded_file(
            restored,
            linked_repository / "scripts/run_container_acceptance.py",
            Path("scripts/run_container_acceptance.py"),
        )
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="not authorized"):
        acceptance_support.require_snapshot_loaded_sources(
            restored,
            (restored.snapshot_repository_root / "tracked.txt",),
        )
    assert prepared.frontend_dependency_root.is_dir() and not prepared.frontend_dependency_root.is_symlink()
    assert prepared.python_site_packages.is_dir() and not prepared.python_site_packages.is_symlink()
    assert prepared.pnpm_dependency_root.is_dir() and prepared.pnpm_executable.read_text(encoding="utf-8") == "console.log('pnpm')\n"
    assert prepared.node_executable.read_bytes() == b"fake-node-executable\n" and not prepared.node_executable.is_symlink()
    assert prepared.frontend_dependency_root.joinpath("absolute-package").resolve() == prepared.frontend_dependency_root / "packages/example"
    assert prepared.python_site_packages.joinpath("absolute-package").resolve() == prepared.python_site_packages / "packages/example"
    assert not any(prepared.snapshot_repository_root.joinpath(name).exists() for name in (".git", ".obsidian", ".venv", "private"))
    prepared.runtime_root.joinpath("artifact.txt").write_text("runtime\n", encoding="utf-8")
    acceptance_support.verify_candidate_snapshot(prepared)
    recovered = _prepare_candidate(repository, env_file, run_id="1700000001-a1b2c3d4e5f6")
    acceptance_support.recover_and_cleanup_candidate_snapshot(recovered.recovery)
    acceptance_support.recover_and_cleanup_candidate_snapshot(recovered.recovery)
    copied = prepared.frontend_dependency_root.joinpath("package.json").read_bytes()
    dependencies.joinpath("package.json").write_text("source drift\n", encoding="utf-8")
    acceptance_support.verify_candidate_snapshot(prepared)
    assert prepared.frontend_dependency_root.joinpath("package.json").read_bytes() == copied
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="dependency projection authority"):
        acceptance_support.require_candidate_source_current(prepared, git_authority=LOCAL_GIT)
    copied_file = prepared.frontend_dependency_root.joinpath("package.json")
    copied_file.chmod(0o600)
    copied_file.write_text("snapshot drift\n", encoding="utf-8")
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="snapshot"):
        acceptance_support.verify_candidate_snapshot(prepared)
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="snapshot"):
        acceptance_support.require_candidate_source_current(prepared, git_authority=LOCAL_GIT)
    acceptance_support.cleanup_candidate_snapshot(prepared)
    assert not prepared.snapshot.root.exists() and not recovered.snapshot.root.exists()
    assert repository.joinpath(".git/index").read_bytes() == index_before


def test_candidate_prepare_failure_cleans_exact_root_and_parent_swap_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, env_file = _candidate_repository(tmp_path)
    _candidate_requirements(monkeypatch, tmp_path)
    original_materialize = candidate_storage.materialize_git_tree

    def mutate_after_materialize(*args: object, **kwargs: object) -> candidate_storage.MaterializedTree:
        result = original_materialize(*args, **kwargs)  # type: ignore[arg-type]
        repository.joinpath("included.txt").write_text("transient\n", encoding="utf-8")
        return result

    monkeypatch.setattr(candidate_storage, "materialize_git_tree", mutate_after_materialize)
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="source changed"):
        _prepare_candidate(repository, env_file, run_id="1700000000-a1b2c3d4e5f6")
    assert not any((tmp_path / "candidates").iterdir())
    monkeypatch.setattr(candidate_storage, "materialize_git_tree", original_materialize)
    original_write = candidate_cleanup.write_reservation_marker

    def fail_marker(*_args: object, **_kwargs: object) -> acceptance_support.CandidatePathIdentity:
        raise candidate_storage.CandidateStorageError("marker write failed")

    reservation = acceptance_support.reserve_candidate_snapshot(repository, env_file, run_id="1700000001-a1b2c3d4e5f6", profile="agent-test")
    monkeypatch.setattr(candidate_cleanup, "write_reservation_marker", fail_marker)
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="marker write failed"):
        acceptance_support.prepare_candidate_snapshot(
            reservation,
            reserved_receipt_sha256="d" * 64,
            loaded_sources=_loaded_sources(repository),
            git_authority=LOCAL_GIT,
        )
    assert not reservation.root.exists()
    assert not any((tmp_path / "candidates").iterdir())
    monkeypatch.setattr(candidate_cleanup, "write_reservation_marker", original_write)
    reservation = acceptance_support.reserve_candidate_snapshot(
        repository,
        env_file,
        run_id="1700000002-a1b2c3d4e5f6",
        profile="agent-test",
    )
    candidates = tmp_path / "candidates"
    candidates.rename(tmp_path / "displaced-candidates")
    candidates.mkdir(mode=0o700)
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="parent|private state"):
        acceptance_support.prepare_candidate_snapshot(
            reservation,
            reserved_receipt_sha256="d" * 64,
            loaded_sources=_loaded_sources(repository),
            git_authority=LOCAL_GIT,
        )


def test_candidate_post_freeze_verification_failure_cleans_the_open_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, env_file = _candidate_repository(tmp_path)
    _candidate_requirements(monkeypatch, tmp_path)

    def reject_prepared(*_args: object, **_kwargs: object) -> None:
        raise candidate_authority.CandidateSnapshotError("post-freeze verification failed")

    monkeypatch.setattr(candidate_authority, "verify_candidate_snapshot", reject_prepared)
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="post-freeze verification failed"):
        _prepare_candidate(repository, env_file, run_id="1700000009-a1b2c3d4e5f6")
    assert not any((tmp_path / "candidates").iterdir())


def test_candidate_source_freshness_uses_runtime_index_and_does_not_change_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, env_file = _candidate_repository(tmp_path)
    _candidate_requirements(monkeypatch, tmp_path)
    prepared = _prepare_candidate(repository, env_file, run_id="1700000010-a1b2c3d4e5f6")
    acceptance_support.require_candidate_source_current(prepared, git_authority=LOCAL_GIT)
    assert not prepared.runtime_root.joinpath("index").exists()
    repository.joinpath("included.txt").write_text("source changed\n", encoding="utf-8")
    acceptance_support.verify_candidate_snapshot(prepared)
    with pytest.raises(candidate_authority.CandidateGitFreshnessError, match="source changed"):
        candidate_authority.require_candidate_source_current(prepared, git_authority=LOCAL_GIT)
    assert not prepared.runtime_root.joinpath("index").exists()
    acceptance_support.cleanup_candidate_snapshot(prepared)


def test_node_executable_source_and_candidate_copy_have_independent_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, env_file = _candidate_repository(tmp_path)
    _candidate_requirements(monkeypatch, tmp_path)
    prepared = _prepare_candidate(repository, env_file, run_id="1700000011-a1b2c3d4e5f6")
    source = prepared.snapshot.node_executable.source_path
    copied = prepared.node_executable.read_bytes()
    source.chmod(0o700)
    source.write_bytes(b"changed-node-source\n")
    source.chmod(0o500)
    acceptance_support.verify_candidate_snapshot(prepared)
    assert prepared.node_executable.read_bytes() == copied
    with pytest.raises(candidate_authority.CandidateDependencyFreshnessError, match="Node executable snapshot authority"):
        candidate_authority.require_candidate_source_current(prepared, git_authority=LOCAL_GIT)
    prepared.node_executable.chmod(0o700)
    prepared.node_executable.write_bytes(b"changed-node-copy\n")
    prepared.node_executable.chmod(0o500)
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="snapshot"):
        acceptance_support.verify_candidate_snapshot(prepared)
    acceptance_support.cleanup_candidate_snapshot(prepared)


def test_dependency_copy_rejects_content_that_matches_postscan_but_not_reserved_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, env_file = _candidate_repository(tmp_path)
    dependencies = _candidate_requirements(monkeypatch, tmp_path)
    source_file = dependencies / "package.json"
    original_content = source_file.read_bytes()
    transient_content = b"x" * len(original_content)
    original_copy = candidate_projection._copy_directory
    original_scan = candidate_projection._scan_source_directory
    changed = False

    def copy_transient(*args: object, **kwargs: object) -> None:
        nonlocal changed
        prefix = args[4]
        if prefix == () and not changed:
            source_file.write_bytes(transient_content)
            changed = True
        original_copy(*args, **kwargs)  # type: ignore[arg-type]

    def scan_then_restore(*args: object, **kwargs: object) -> None:
        original_scan(*args, **kwargs)  # type: ignore[arg-type]
        if args[3] == ():
            source_file.write_bytes(original_content)

    monkeypatch.setattr(candidate_projection, "_copy_directory", copy_transient)
    monkeypatch.setattr(candidate_projection, "_scan_source_directory", scan_then_restore)
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="materialized safely"):
        _prepare_candidate(repository, env_file, run_id="1700000012-a1b2c3d4e5f6")
    assert source_file.read_bytes() == original_content
    assert not any((tmp_path / "candidates").iterdir())


def test_dependency_copy_does_not_fsync_each_temporary_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, env_file = _candidate_repository(tmp_path)
    _candidate_requirements(monkeypatch, tmp_path)
    original_fsync = os.fsync
    projection_fsync_calls = 0

    def record_fsync(descriptor: int) -> None:
        nonlocal projection_fsync_calls
        if Path(sys._getframe(1).f_code.co_filename) == Path(candidate_projection.__file__):
            projection_fsync_calls += 1
        original_fsync(descriptor)

    monkeypatch.setattr(candidate_projection.os, "fsync", record_fsync)
    prepared = _prepare_candidate(repository, env_file, run_id="1700000013-a1b2c3d4e5f6")
    assert projection_fsync_calls == 0
    acceptance_support.cleanup_candidate_snapshot(prepared)


@pytest.mark.parametrize("target", ["missing-target", "../outside"])
def test_dependency_snapshot_rejects_broken_or_escaping_symlink(tmp_path: Path, target: str) -> None:
    dependency = tmp_path / "dependency-copy"
    dependency.mkdir()
    dependency.joinpath("link").symlink_to(target)
    dependency.chmod(0o500)
    identity = candidate_storage.PathIdentity.from_stat(dependency.stat(follow_symlinks=False))
    evidence = candidate_projection.DependencyTreeSnapshot(dependency, identity, "0" * 64, 1, 0)
    with pytest.raises(candidate_storage.CandidateStorageError, match="symlink"):
        candidate_projection.verify_dependency_snapshot(evidence)


def test_cleanup_rejects_dependency_swap_after_tombstone_move(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, env_file = _candidate_repository(tmp_path)
    _candidate_requirements(monkeypatch, tmp_path)
    prepared = _prepare_candidate(repository, env_file, run_id="1700000011-a1b2c3d4e5f6")
    displaced_dependencies = tmp_path / "displaced-dependencies"
    swapped = False

    def swap_dependencies(phase: str) -> None:
        nonlocal swapped
        if phase != "root-renamed" or swapped:
            return
        swapped = True
        cleaning_root = next(prepared.snapshot.parent.glob(f".{prepared.snapshot.root.name}.cleaning-*"))
        cleaning_root.chmod(0o700)
        dependencies = cleaning_root / "dependencies"
        dependencies.chmod(0o700)
        dependencies.rename(displaced_dependencies)
        displaced_dependencies.chmod(0o500)
        cleaning_root.joinpath("dependencies").mkdir(mode=0o500)
        cleaning_root.chmod(0o500)

    monkeypatch.setattr(candidate_cleanup, "_checkpoint", swap_dependencies)
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="cleaned safely"):
        acceptance_support.cleanup_candidate_snapshot(prepared)
    cleaning_root = next(prepared.snapshot.parent.glob(f".{prepared.snapshot.root.name}.cleaning-*"))
    assert cleaning_root.exists() and displaced_dependencies.exists()
    monkeypatch.setattr(candidate_cleanup, "_checkpoint", lambda _phase: None)
    cleaning_root.chmod(0o700)
    cleaning_root.joinpath("dependencies").rmdir()
    displaced_dependencies.chmod(0o700)
    displaced_dependencies.rename(cleaning_root / "dependencies")
    cleaning_root.joinpath("dependencies").chmod(0o500)
    cleaning_root.chmod(0o500)
    acceptance_support.cleanup_candidate_snapshot(prepared)
    assert not any(prepared.snapshot.parent.iterdir())


@pytest.mark.parametrize("leaf", ["repository", "selected.env", "runtime", "dependencies", "index"])
def test_reserved_partial_candidate_cleanup_accepts_only_known_layout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    leaf: str,
) -> None:
    repository, env_file = _candidate_repository(tmp_path)
    _candidate_requirements(monkeypatch, tmp_path)
    reservation = acceptance_support.reserve_candidate_snapshot(
        repository,
        env_file,
        run_id=f"1700000020-{hashlib.sha256(leaf.encode()).hexdigest()[:12]}",
        profile="agent-test",
    )
    root = candidate_storage.create_planned_snapshot_root(
        reservation.parent,
        reservation.parent_identity,
        reservation.root,
    )
    try:
        marker = candidate_contract.reservation_marker_bytes(reservation, "d" * 64)
        candidate_storage.write_snapshot_env(root.descriptor, marker, name=candidate_contract.SNAPSHOT_MARKER)
        if leaf in {"selected.env", "index"}:
            candidate_storage.write_snapshot_env(root.descriptor, b"temporary\n", name=leaf)
        else:
            candidate_storage.create_private_child(root, leaf)
    finally:
        os.close(root.descriptor)
    acceptance_support.cleanup_reserved_candidate(reservation, "d" * 64)
    assert not reservation.root.exists()
    assert not any((tmp_path / "candidates").iterdir())


@pytest.mark.parametrize("write_marker", [False, True])
def test_reserved_cleanup_recovers_empty_root_but_preserves_unknown_layout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_marker: bool,
) -> None:
    repository, env_file = _candidate_repository(tmp_path)
    _candidate_requirements(monkeypatch, tmp_path)
    reservation = acceptance_support.reserve_candidate_snapshot(
        repository,
        env_file,
        run_id=f"1700000030-{'a' if write_marker else 'b'}123456789ab",
        profile="agent-test",
    )
    root = candidate_storage.create_planned_snapshot_root(
        reservation.parent,
        reservation.parent_identity,
        reservation.root,
    )
    try:
        if write_marker:
            marker = candidate_contract.reservation_marker_bytes(reservation, "d" * 64)
            candidate_storage.write_snapshot_env(root.descriptor, marker, name=candidate_contract.SNAPSHOT_MARKER)
            candidate_storage.create_private_child(root, "unknown")
    finally:
        os.close(root.descriptor)
    if not write_marker:
        acceptance_support.cleanup_reserved_candidate(reservation, "d" * 64)
        assert not reservation.root.exists()
        return
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="cleaned safely"):
        acceptance_support.cleanup_reserved_candidate(reservation, "d" * 64)
    assert reservation.root.exists()
    candidate_storage.cleanup_snapshot_root(root.authority)
