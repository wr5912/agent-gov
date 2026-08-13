"""容器验收候选源码的 reserved -> prepared 不可变快照编排。"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Final

from scripts import container_acceptance_candidate_authority as candidate_authority
from scripts import container_acceptance_candidate_cleanup as candidate_cleanup
from scripts import container_acceptance_candidate_git as candidate_git
from scripts import container_acceptance_candidate_projection as candidate_projection
from scripts import container_acceptance_candidate_storage as candidate_storage
from scripts import container_acceptance_candidate_tools as candidate_tools
from scripts import container_acceptance_toolchain as acceptance_toolchain
from scripts.container_acceptance_candidate_authority import (
    CandidatePathIdentity,
    CandidateRecoveryAuthority,
    CandidateSnapshotError,
    CandidateSnapshotIdentity,
    CandidateSnapshotReservation,
    CandidateSourceIdentity,
    LoadedSourceIdentity,
    PreparedCandidateAuthority,
)
from scripts.container_acceptance_candidate_git import CandidateGitAuthority

_FRONTEND_DEPENDENCY_PARTS: Final = ("frontend", "node_modules")
_RUNTIME_BOOTSTRAP = PurePosixPath(*candidate_authority.RUNTIME_BOOTSTRAP_PARTS)
_ALLOWED_RESERVED_ROOT_NAMES: Final = frozenset(
    {
        candidate_authority.SNAPSHOT_MARKER,
        candidate_authority.SNAPSHOT_REPOSITORY,
        candidate_authority.SNAPSHOT_ENV,
        candidate_authority.SNAPSHOT_RUNTIME,
        "dependencies",
        "index",
        "index.lock",
    }
)

ParentRequirement = acceptance_toolchain.CandidateSnapshotParentRequirement
ProjectionRequirement = candidate_tools.ProjectionRequirement
PythonRequirement = candidate_tools.PythonRequirement
PnpmRequirement = candidate_tools.PnpmRequirement
NodeRequirement = candidate_tools.NodeRequirement
ParentValidator = Callable[[ParentRequirement], None]
ProjectionValidator = candidate_tools.ProjectionValidator
PythonValidator = candidate_tools.PythonValidator
PnpmValidator = candidate_tools.PnpmValidator
NodeValidator = candidate_tools.NodeValidator


class CandidateSnapshotFreshnessError(CandidateSnapshotError):
    """不可变候选快照自身不再满足 prepared authority。"""


class CandidateGitFreshnessError(CandidateSnapshotError):
    """原始 Git tree 或 selected env 不再匹配候选。"""


class CandidateLoadedSourceFreshnessError(CandidateSnapshotError):
    """首阶段实际加载的仓库源码不再匹配候选。"""


class CandidateDependencyFreshnessError(CandidateSnapshotError):
    """固定依赖或执行文件不再匹配候选。"""


def reserve_candidate_snapshot(
    repository: Path,
    selected_env: Path,
    *,
    run_id: str,
    profile: str,
    allow_public_env_read: bool = False,
    parent_requirement: ParentRequirement | None = None,
    parent_validator: ParentValidator | None = None,
) -> CandidateSnapshotReservation:
    requirement = parent_requirement or _active_parent_requirement()
    _validate_parent_requirement(requirement, parent_validator)
    parent = requirement.path.absolute()
    try:
        parent_identity = candidate_storage.private_directory_identity(parent)
    except candidate_storage.CandidateStorageError as exc:
        raise CandidateSnapshotError("candidate snapshot parent authority is invalid") from exc
    _require_parent_matches(requirement, parent_identity)
    nonce = os.urandom(16).hex()
    root = parent / candidate_authority.reservation_root_name(run_id, nonce)
    reservation = CandidateSnapshotReservation(
        run_id,
        profile,
        parent,
        parent_identity,
        root,
        repository.absolute(),
        selected_env.absolute(),
        allow_public_env_read,
        nonce,
        "0" * 64,
        True,
    )
    reservation = replace(reservation, marker_sha256=candidate_authority.reservation_marker_sha256(reservation))
    try:
        candidate_authority.validate_reservation(reservation)
        if not candidate_storage.planned_snapshot_root_absent(parent, parent_identity, root):
            raise CandidateSnapshotError("candidate reserved snapshot leaf already exists")
    except (candidate_authority.CandidateAuthorityError, candidate_storage.CandidateStorageError) as exc:
        raise CandidateSnapshotError("candidate snapshot reservation is invalid") from exc
    return reservation


def plan_candidate_snapshot(
    repository: Path,
    selected_env: Path,
    *,
    run_id: str,
    profile: str,
    allow_public_env_read: bool = False,
    parent_requirement: ParentRequirement | None = None,
    parent_validator: ParentValidator | None = None,
) -> CandidateSnapshotReservation:
    return reserve_candidate_snapshot(
        repository,
        selected_env,
        run_id=run_id,
        profile=profile,
        allow_public_env_read=allow_public_env_read,
        parent_requirement=parent_requirement,
        parent_validator=parent_validator,
    )


def prepare_candidate_snapshot(
    reservation: CandidateSnapshotReservation,
    *,
    reserved_receipt_sha256: str,
    loaded_sources: tuple[LoadedSourceIdentity, ...],
    git_authority: CandidateGitAuthority | None = None,
    dependency_projection: ProjectionRequirement | None = None,
    python_dependencies: PythonRequirement | None = None,
    pnpm_dependencies: PnpmRequirement | None = None,
    node_executable: NodeRequirement | None = None,
    parent_validator: ParentValidator | None = None,
    projection_validator: ProjectionValidator | None = None,
    python_validator: PythonValidator | None = None,
    pnpm_validator: PnpmValidator | None = None,
    node_validator: NodeValidator | None = None,
) -> PreparedCandidateAuthority:
    authority = candidate_git.validated_git_authority(git_authority)
    requirements = candidate_tools.select_dependency_requirements(
        dependency_projection,
        python_dependencies,
        pnpm_dependencies,
        node_executable,
        frontend_validator=projection_validator,
        python_validator=python_validator,
        pnpm_validator=pnpm_validator,
        node_validator=node_validator,
    )
    _validate_live_reservation(reservation, parent_validator)
    marker_content = candidate_authority.reservation_marker_bytes(reservation, reserved_receipt_sha256)
    root = _create_reserved_root(reservation)
    try:
        try:
            marker_identity = candidate_cleanup.write_reservation_marker(root, reservation, marker_content)
        except candidate_storage.CandidateStorageError as exc:
            raise CandidateSnapshotError("candidate reservation marker write failed") from exc
        source = candidate_git.capture_candidate_source(reservation, root, authority)
        prepared = _materialize_snapshot(
            reservation,
            source,
            root,
            marker_content,
            marker_identity,
            reserved_receipt_sha256,
            _normalize_loaded_sources(loaded_sources),
            requirements,
            authority,
        )
        verify_candidate_snapshot(prepared, parent_validator=parent_validator, projection_validator=projection_validator)
        return prepared
    except BaseException as primary:
        try:
            candidate_cleanup.cleanup_reserved_snapshot(
                reservation,
                reserved_receipt_sha256,
                allowed_names=_ALLOWED_RESERVED_ROOT_NAMES,
            )
        except candidate_storage.CandidateStorageError as cleanup_error:
            raise CandidateSnapshotError("candidate snapshot preparation and exact cleanup both failed") from cleanup_error
        raise primary
    finally:
        os.close(root.descriptor)


def cleanup_reserved_candidate(
    reservation: CandidateSnapshotReservation,
    reserved_receipt_sha256: str,
    *,
    parent_validator: ParentValidator | None = None,
) -> None:
    _validate_live_reservation(reservation, parent_validator, allow_existing=True)
    try:
        candidate_cleanup.cleanup_reserved_snapshot(
            reservation,
            reserved_receipt_sha256,
            allowed_names=_ALLOWED_RESERVED_ROOT_NAMES,
        )
    except candidate_storage.CandidateStorageError as exc:
        raise CandidateSnapshotError("reserved candidate snapshot could not be cleaned safely") from exc


def verify_candidate_snapshot(
    authority: PreparedCandidateAuthority,
    *,
    parent_validator: ParentValidator | None = None,
    projection_validator: ProjectionValidator | None = None,
) -> None:
    try:
        candidate_authority.validate_prepared_layout(authority)
    except candidate_authority.CandidateAuthorityError as exc:
        raise CandidateSnapshotError("prepared candidate authority layout is invalid") from exc
    _validate_snapshot_parent(authority.snapshot, parent_validator)
    _verify_snapshot(authority.snapshot, projection_validator)


def require_candidate_source_current(
    authority: PreparedCandidateAuthority,
    *,
    git_authority: CandidateGitAuthority | None = None,
    parent_validator: ParentValidator | None = None,
    projection_validator: ProjectionValidator | None = None,
    python_validator: PythonValidator | None = None,
    pnpm_validator: PnpmValidator | None = None,
    node_validator: NodeValidator | None = None,
) -> None:
    witness = freeze_candidate_source_current(
        authority,
        git_authority=git_authority,
        parent_validator=parent_validator,
        projection_validator=projection_validator,
        python_validator=python_validator,
        pnpm_validator=pnpm_validator,
        node_validator=node_validator,
    )
    witness.close()


def freeze_candidate_source_current(
    authority: PreparedCandidateAuthority,
    *,
    git_authority: CandidateGitAuthority | None = None,
    parent_validator: ParentValidator | None = None,
    projection_validator: ProjectionValidator | None = None,
    python_validator: PythonValidator | None = None,
    pnpm_validator: PnpmValidator | None = None,
    node_validator: NodeValidator | None = None,
) -> candidate_git.FrozenCandidateIndex:
    try:
        verify_candidate_snapshot(
            authority,
            parent_validator=parent_validator,
            projection_validator=projection_validator,
        )
    except CandidateSnapshotError as exc:
        raise CandidateSnapshotFreshnessError(str(exc)) from exc
    reservation = _reservation_from_prepared(authority)
    try:
        selected = candidate_git.validated_git_authority(git_authority)
        with candidate_git.prepared_candidate_index(authority.snapshot) as index_root:
            current = candidate_git.capture_candidate_source(reservation, index_root, selected)
            if current != authority.source:
                raise CandidateSnapshotError("candidate source changed after its immutable snapshot was prepared")
            witness = candidate_git.freeze_candidate_index(authority.source.repository_root, index_root, selected)
    except CandidateSnapshotError as exc:
        raise CandidateGitFreshnessError(str(exc)) from exc
    try:
        _verify_current_loaded_source_set(authority.source_repository_root, authority.snapshot.loaded_sources)
    except CandidateSnapshotError as exc:
        witness.close()
        raise CandidateLoadedSourceFreshnessError(str(exc)) from exc
    try:
        candidate_tools.require_dependency_sources_current(
            authority.snapshot,
            frontend_validator=projection_validator,
            python_validator=python_validator,
            pnpm_validator=pnpm_validator,
            node_validator=node_validator,
        )
    except CandidateSnapshotError as exc:
        witness.close()
        raise CandidateDependencyFreshnessError(str(exc)) from exc
    return witness


def require_frozen_candidate_source_current(
    authority: PreparedCandidateAuthority,
    witness: candidate_git.FrozenCandidateIndex,
    *,
    git_authority: CandidateGitAuthority | None = None,
    projection_validator: ProjectionValidator | None = None,
    python_validator: PythonValidator | None = None,
    pnpm_validator: PnpmValidator | None = None,
    node_validator: NodeValidator | None = None,
) -> None:
    try:
        selected = candidate_git.validated_git_authority(git_authority)
        repository = candidate_storage.real_directory_identity(authority.source.repository_root)
        env = candidate_storage.capture_regular_file(
            authority.source.selected_env_file,
            allow_public_read=authority.source.allow_public_env_read,
        )
        if repository != authority.source.repository or env.identity != authority.source.selected_env or env.sha256 != authority.source.selected_env_sha256:
            raise CandidateSnapshotError("candidate source authority changed during cleanup")
    except (CandidateSnapshotError, candidate_storage.CandidateStorageError) as exc:
        raise CandidateGitFreshnessError(str(exc)) from exc
    try:
        _verify_current_loaded_source_set(authority.source_repository_root, authority.snapshot.loaded_sources)
    except CandidateSnapshotError as exc:
        raise CandidateLoadedSourceFreshnessError(str(exc)) from exc
    try:
        candidate_tools.require_dependency_sources_current(
            authority.snapshot,
            frontend_validator=projection_validator,
            python_validator=python_validator,
            pnpm_validator=pnpm_validator,
            node_validator=node_validator,
        )
    except CandidateSnapshotError as exc:
        raise CandidateDependencyFreshnessError(str(exc)) from exc
    require_frozen_candidate_generation_current(authority, witness, git_authority=selected)


def require_frozen_candidate_generation_current(
    authority: PreparedCandidateAuthority,
    witness: candidate_git.FrozenCandidateIndex,
    *,
    git_authority: CandidateGitAuthority | None = None,
) -> None:
    try:
        selected = candidate_git.validated_git_authority(git_authority)
        candidate_git.require_frozen_candidate_current(authority.source.repository_root, witness, selected)
        repository = candidate_storage.real_directory_identity(authority.source.repository_root)
        env_identity = candidate_storage.lstat_identity(authority.source.selected_env_file)
        if repository != authority.source.repository or env_identity != authority.source.selected_env:
            raise CandidateSnapshotError("candidate source linkage changed during terminal verification")
    except (CandidateSnapshotError, candidate_storage.CandidateStorageError) as exc:
        raise CandidateGitFreshnessError(str(exc)) from exc


def recover_and_cleanup_candidate_snapshot(
    authority: CandidateRecoveryAuthority,
    *,
    parent_validator: ParentValidator | None = None,
    projection_validator: ProjectionValidator | None = None,
) -> None:
    expected = candidate_authority.recovery_digest(authority.snapshot)
    if authority.authority_sha256 != expected:
        raise CandidateSnapshotError("candidate recovery authority digest is invalid")
    try:
        candidate_authority.validate_snapshot_layout(authority.snapshot)
    except candidate_authority.CandidateAuthorityError as exc:
        raise CandidateSnapshotError("candidate recovery authority layout is invalid") from exc
    _validate_snapshot_parent(authority.snapshot, parent_validator)
    _cleanup_snapshot_identity(authority.snapshot)


def cleanup_candidate_snapshot(
    authority: PreparedCandidateAuthority,
    *,
    parent_validator: ParentValidator | None = None,
    projection_validator: ProjectionValidator | None = None,
) -> None:
    del projection_validator
    try:
        candidate_authority.validate_prepared_layout(authority)
    except candidate_authority.CandidateAuthorityError as exc:
        raise CandidateSnapshotError("prepared candidate authority layout is invalid") from exc
    _validate_snapshot_parent(authority.snapshot, parent_validator)
    _cleanup_snapshot_identity(authority.snapshot)


def require_snapshot_loaded_sources(
    authority: PreparedCandidateAuthority,
    loaded_files: Sequence[Path],
    *,
    required_relative_paths: Sequence[PurePosixPath] = (),
) -> None:
    verify_candidate_snapshot(authority)
    expected = {item.relative_path: item.sha256 for item in authority.snapshot.loaded_sources}
    observed: dict[str, str] = {}
    for loaded_file in loaded_files:
        relative = candidate_git.snapshot_loaded_relative(authority.snapshot, loaded_file)
        observed[relative] = _verified_loaded_file(authority.snapshot_repository_root, relative)
    required = {path.as_posix() for path in required_relative_paths}
    valid = (
        bool(observed)
        and len(observed) == len(loaded_files)
        and all(expected.get(path) == digest for path, digest in observed.items())
        and len(required) == len(required_relative_paths)
        and required <= observed.keys()
    )
    if not valid:
        raise CandidateSnapshotError("acceptance loaded source set is not authorized by the candidate snapshot")
    verify_candidate_snapshot(authority)


def require_snapshot_loaded_file(
    authority: PreparedCandidateAuthority,
    loaded_file: Path,
    relative_path: PurePosixPath,
) -> None:
    verify_candidate_snapshot(authority)
    if candidate_git.snapshot_loaded_relative(authority.snapshot, loaded_file) != relative_path.as_posix():
        raise CandidateSnapshotError("acceptance runner was not loaded from the candidate snapshot")
    digest = _verified_loaded_file(authority.snapshot_repository_root, relative_path.as_posix())
    expected = {item.relative_path: item.sha256 for item in authority.snapshot.loaded_sources}.get(relative_path.as_posix())
    if expected != digest:
        raise CandidateSnapshotError("acceptance loaded source is not part of the frozen source set")
    verify_candidate_snapshot(authority)


staged_tree_sha = candidate_git.staged_tree_sha
revision_tree_sha = candidate_git.revision_tree_sha


def _materialize_snapshot(
    reservation: CandidateSnapshotReservation,
    source: CandidateSourceIdentity,
    root: candidate_storage.OpenSnapshotRoot,
    marker_content: bytes,
    marker_identity: CandidatePathIdentity,
    reserved_receipt_sha256: str,
    loaded_sources: tuple[LoadedSourceIdentity, ...],
    requirements: candidate_tools.DependencyRequirements,
    git_authority: CandidateGitAuthority,
) -> PreparedCandidateAuthority:
    repository = root.authority.root / candidate_authority.SNAPSHOT_REPOSITORY
    try:
        materialized = candidate_storage.materialize_git_tree(
            source.repository_root,
            source.git_tree_sha,
            root.descriptor,
            candidate_authority.SNAPSHOT_REPOSITORY,
            git_authority,
        )
        final_source = candidate_git.capture_candidate_source(reservation, root, git_authority)
        if final_source != source:
            raise CandidateSnapshotError("candidate source changed while its immutable snapshot was prepared")
        candidate_storage.remove_candidate_index(root)
        _verify_loaded_source_set(repository, loaded_sources)
        env = candidate_storage.capture_regular_file(
            source.selected_env_file,
            allow_public_read=source.allow_public_env_read,
        )
        if env.identity != source.selected_env or env.sha256 != source.selected_env_sha256:
            raise CandidateSnapshotError("selected env changed before snapshot materialization")
        env_identity = candidate_storage.write_snapshot_env(
            root.descriptor,
            env.content,
            name=candidate_authority.SNAPSHOT_ENV,
        )
        runtime_identity = candidate_storage.create_private_child(root, candidate_authority.SNAPSHOT_RUNTIME)
        dependencies = candidate_tools.materialize_dependency_snapshots(root, requirements)
        dependencies_identity = candidate_storage.real_directory_identity(root.authority.root / "dependencies")
        excluded = candidate_storage.ExcludedDirectory(_FRONTEND_DEPENDENCY_PARTS, dependencies.frontend.identity)
        root_identity, repository_identity = candidate_projection.freeze_repository(
            root,
            candidate_authority.SNAPSHOT_REPOSITORY,
            excluded,
        )
        observed = candidate_storage.snapshot_tree(repository, excluded)
        runtime_bootstrap = repository.joinpath(*_RUNTIME_BOOTSTRAP.parts)
        runtime_bootstrap_identity = candidate_storage.real_directory_identity(runtime_bootstrap)
    except (candidate_storage.CandidateStorageError, acceptance_toolchain.ToolchainAuthorityError) as exc:
        raise CandidateSnapshotError("candidate tree could not be materialized safely") from exc
    _require_materialized_tree_match(observed, materialized)
    evidence = candidate_authority.SnapshotMaterializationEvidence(
        root_identity=root_identity,
        repository_identity=repository_identity,
        env_identity=env_identity,
        runtime_identity=runtime_identity,
        runtime_bootstrap=runtime_bootstrap,
        runtime_bootstrap_identity=runtime_bootstrap_identity,
        frontend_dependencies=candidate_tools.dependency_identity(dependencies.frontend, requirements.frontend),
        python_dependencies=candidate_tools.dependency_identity(dependencies.python, requirements.python),
        pnpm_dependencies=candidate_tools.dependency_identity(dependencies.pnpm, requirements.pnpm),
        node_executable=dependencies.node,
        repository_sha256=materialized.source_sha256,
        selected_env_sha256=env.sha256,
        file_count=materialized.file_count,
        total_bytes=materialized.total_bytes,
        dependencies_identity=dependencies_identity,
    )
    return candidate_authority.prepared_authority_from_materialization(
        reservation=reservation,
        source=source,
        reserved_receipt_sha256=reserved_receipt_sha256,
        marker_content=marker_content,
        marker_identity=marker_identity,
        loaded_sources=loaded_sources,
        evidence=evidence,
    )


def _require_materialized_tree_match(
    observed: candidate_storage.MaterializedTree,
    expected: candidate_storage.MaterializedTree,
) -> None:
    actual_identity = (observed.source_sha256, observed.file_count, observed.total_bytes)
    expected_identity = (expected.source_sha256, expected.file_count, expected.total_bytes)
    if actual_identity != expected_identity:
        raise CandidateSnapshotError("materialized candidate tree does not match its raw Git manifest")


def _reservation_from_prepared(authority: PreparedCandidateAuthority) -> CandidateSnapshotReservation:
    snapshot = authority.snapshot
    reservation = CandidateSnapshotReservation(
        snapshot.run_id,
        snapshot.profile,
        snapshot.parent,
        snapshot.parent_identity,
        snapshot.root,
        authority.source.repository_root,
        authority.source.selected_env_file,
        authority.source.allow_public_env_read,
        snapshot.reservation_nonce,
        snapshot.reservation_sha256,
        True,
    )
    try:
        candidate_authority.validate_reservation(reservation)
    except candidate_authority.CandidateAuthorityError as exc:
        raise CandidateSnapshotError("prepared candidate reservation authority is invalid") from exc
    return reservation


def _verify_snapshot(snapshot: CandidateSnapshotIdentity, projection_validator: ProjectionValidator | None) -> None:
    del projection_validator
    excluded = candidate_storage.ExcludedDirectory(
        _FRONTEND_DEPENDENCY_PARTS,
        snapshot.frontend_dependencies.root_identity,
    )
    try:
        candidate_storage.verify_snapshot_root(_snapshot_root(snapshot))
        if candidate_storage.real_directory_identity(snapshot.repository_root) != snapshot.repository_identity:
            raise CandidateSnapshotError("candidate repository snapshot root changed")
        observed = candidate_storage.snapshot_tree(snapshot.repository_root, excluded)
        candidate_tools.verify_dependency_snapshots(snapshot)
        dependencies = candidate_storage.real_directory_identity(snapshot.root / "dependencies")
        env = candidate_storage.capture_regular_file(snapshot.env_file, allow_public_read=False)
        marker = candidate_storage.capture_regular_file(snapshot.marker_file, allow_public_read=False)
        runtime = candidate_storage.real_directory_identity(snapshot.runtime_root)
        runtime_bootstrap = candidate_storage.real_directory_identity(snapshot.runtime_bootstrap)
    except candidate_storage.CandidateStorageError as exc:
        raise CandidateSnapshotError("candidate snapshot could not be verified") from exc
    if (observed.source_sha256, observed.file_count, observed.total_bytes) != (
        snapshot.repository_sha256,
        snapshot.file_count,
        snapshot.total_bytes,
    ):
        raise CandidateSnapshotError("candidate repository snapshot digest changed")
    if env.identity != snapshot.env_identity or env.sha256 != snapshot.selected_env_sha256:
        raise CandidateSnapshotError("candidate selected env snapshot changed")
    if marker.identity != snapshot.marker_identity or marker.sha256 != snapshot.marker_sha256:
        raise CandidateSnapshotError("candidate snapshot recovery marker changed")
    if marker.content != candidate_authority.snapshot_marker_bytes(snapshot):
        raise CandidateSnapshotError("candidate snapshot recovery marker is invalid")
    if dependencies != snapshot.dependencies_identity:
        raise CandidateSnapshotError("candidate dependency parent authority changed")
    if not _same_runtime_node(runtime, snapshot.runtime_identity):
        raise CandidateSnapshotError("candidate runtime directory authority changed")
    if runtime_bootstrap != snapshot.runtime_bootstrap_identity:
        raise CandidateSnapshotError("candidate runtime bootstrap snapshot changed")
    _verify_loaded_source_set(snapshot.repository_root, snapshot.loaded_sources)


def _normalize_loaded_sources(sources: tuple[LoadedSourceIdentity, ...]) -> tuple[LoadedSourceIdentity, ...]:
    normalized = tuple(sorted(sources, key=lambda item: item.relative_path.encode()))
    if normalized != sources or len({item.relative_path for item in sources}) != len(sources):
        raise CandidateSnapshotError("candidate loaded source set is invalid")
    if not any(item.relative_path == "scripts/run_container_acceptance.py" for item in sources):
        raise CandidateSnapshotError("candidate runner is absent from the loaded source set")
    return normalized


def _verify_loaded_source_set(repository: Path, sources: tuple[LoadedSourceIdentity, ...]) -> None:
    for source in sources:
        if _verified_loaded_file(repository, source.relative_path) != source.sha256:
            raise CandidateSnapshotError("candidate loaded source digest does not match the snapshot")


def _verify_current_loaded_source_set(repository: Path, sources: tuple[LoadedSourceIdentity, ...]) -> None:
    for source in sources:
        if candidate_git.source_loaded_file_sha256(repository, source.relative_path) != source.sha256:
            raise CandidateSnapshotError("candidate loaded source digest changed after snapshot preparation")


def _verified_loaded_file(repository: Path, relative_path: str) -> str:
    path = PurePosixPath(relative_path)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise CandidateSnapshotError("candidate loaded source path is invalid")
    try:
        captured = candidate_storage.capture_regular_file(
            repository.joinpath(*path.parts),
            allow_public_read=True,
        )
    except candidate_storage.CandidateStorageError as exc:
        raise CandidateSnapshotError("candidate loaded source is unavailable") from exc
    return captured.sha256


def _validate_live_reservation(
    reservation: CandidateSnapshotReservation,
    parent_validator: ParentValidator | None,
    *,
    allow_existing: bool = False,
) -> None:
    try:
        candidate_authority.validate_reservation(reservation)
    except candidate_authority.CandidateAuthorityError as exc:
        raise CandidateSnapshotError("candidate snapshot reservation is invalid") from exc
    requirement = _parent_requirement_from(reservation.parent, reservation.parent_identity)
    _validate_parent_requirement(requirement, parent_validator)
    try:
        current = candidate_storage.private_directory_identity(reservation.parent)
        _require_parent_matches(requirement, current)
        absent = candidate_storage.planned_snapshot_root_absent(
            reservation.parent,
            reservation.parent_identity,
            reservation.root,
        )
    except candidate_storage.CandidateStorageError as exc:
        raise CandidateSnapshotError("candidate snapshot reservation authority drifted") from exc
    if not allow_existing and not absent:
        raise CandidateSnapshotError("candidate reserved snapshot leaf already exists")


def _validate_snapshot_parent(snapshot: CandidateSnapshotIdentity, validator: ParentValidator | None) -> None:
    requirement = _parent_requirement_from(snapshot.parent, snapshot.parent_identity)
    _validate_parent_requirement(requirement, validator)
    _require_parent_matches(requirement, snapshot.parent_identity)


def _parent_requirement_from(path: Path, identity: CandidatePathIdentity) -> ParentRequirement:
    return ParentRequirement(
        path,
        identity.device,
        identity.inode,
        stat.S_IMODE(identity.mode),
        identity.uid,
        identity.gid,
    )


def _active_parent_requirement() -> ParentRequirement:
    try:
        return acceptance_toolchain.candidate_snapshot_parent_requirement()
    except acceptance_toolchain.ToolchainAuthorityError as exc:
        raise CandidateSnapshotError("candidate snapshot parent authority is unavailable") from exc


def _validate_parent_requirement(requirement: ParentRequirement, validator: ParentValidator | None) -> None:
    try:
        (validator or acceptance_toolchain.validate_candidate_snapshot_parent)(requirement)
    except acceptance_toolchain.ToolchainAuthorityError as exc:
        raise CandidateSnapshotError("candidate snapshot parent authority drifted") from exc


def _require_parent_matches(requirement: ParentRequirement, identity: CandidatePathIdentity) -> None:
    observed = _parent_requirement_from(requirement.path, identity)
    if observed != requirement or requirement.mode != 0o700 or requirement.uid != os.geteuid():
        raise CandidateSnapshotError("candidate snapshot parent does not match fixed private state")


def _create_reserved_root(reservation: CandidateSnapshotReservation) -> candidate_storage.OpenSnapshotRoot:
    try:
        return candidate_storage.create_planned_snapshot_root(
            reservation.parent,
            reservation.parent_identity,
            reservation.root,
        )
    except candidate_storage.CandidateStorageError as exc:
        raise CandidateSnapshotError("candidate reserved snapshot root could not be created") from exc


def _cleanup_snapshot_identity(snapshot: CandidateSnapshotIdentity) -> None:
    try:
        candidate_cleanup.cleanup_prepared_snapshot(snapshot)
    except candidate_storage.CandidateStorageError as exc:
        raise CandidateSnapshotError("candidate snapshot could not be cleaned safely") from exc


def _snapshot_root(snapshot: CandidateSnapshotIdentity) -> candidate_storage.SnapshotRoot:
    return candidate_storage.SnapshotRoot(
        snapshot.parent,
        snapshot.parent_identity,
        snapshot.root,
        snapshot.root_identity,
    )


def _same_runtime_node(current: CandidatePathIdentity, expected: CandidatePathIdentity) -> bool:
    return candidate_storage.same_node(current, expected) and stat.S_IMODE(current.mode) == stat.S_IMODE(expected.mode) == 0o700
