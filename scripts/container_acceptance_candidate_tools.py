"""候选快照的受管依赖 requirement、实体副本与 freshness 复验。"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass

from scripts import container_acceptance_candidate_authority as candidate_authority
from scripts import container_acceptance_candidate_projection as candidate_projection
from scripts import container_acceptance_candidate_storage as candidate_storage
from scripts import container_acceptance_toolchain as acceptance_toolchain
from scripts.container_acceptance_candidate_authority import (
    CandidateDependencySnapshotIdentity,
    CandidateExecutableSnapshotIdentity,
    CandidatePathIdentity,
    CandidateSnapshotError,
    CandidateSnapshotIdentity,
)

ProjectionRequirement = acceptance_toolchain.FrontendDependencyProjectionRequirement
PythonRequirement = acceptance_toolchain.PythonDependencySnapshotRequirement
PnpmRequirement = acceptance_toolchain.PnpmDependencySnapshotRequirement
NodeRequirement = acceptance_toolchain.NodeExecutableSnapshotRequirement
ProjectionValidator = Callable[[ProjectionRequirement], None]
PythonValidator = Callable[[PythonRequirement], None]
PnpmValidator = Callable[[PnpmRequirement], None]
NodeValidator = Callable[[NodeRequirement], None]

_READ_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
_MAX_NODE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DependencyRequirements:
    frontend: ProjectionRequirement
    python: PythonRequirement
    pnpm: PnpmRequirement
    node: NodeRequirement


@dataclass(frozen=True, slots=True)
class DependencySnapshots:
    frontend: candidate_projection.DependencyTreeSnapshot
    python: candidate_projection.DependencyTreeSnapshot
    pnpm: candidate_projection.DependencyTreeSnapshot
    node: CandidateExecutableSnapshotIdentity


def select_dependency_requirements(
    frontend: ProjectionRequirement | None,
    python: PythonRequirement | None,
    pnpm: PnpmRequirement | None,
    node: NodeRequirement | None,
    *,
    frontend_validator: ProjectionValidator | None,
    python_validator: PythonValidator | None,
    pnpm_validator: PnpmValidator | None,
    node_validator: NodeValidator | None,
) -> DependencyRequirements:
    requirements = DependencyRequirements(
        frontend or _active_frontend_requirement(),
        python or _active_python_requirement(),
        pnpm or _active_pnpm_requirement(),
        node or _active_node_requirement(),
    )
    _validate_frontend(requirements.frontend, frontend_validator)
    _validate_python(requirements.python, python_validator)
    _validate_pnpm(requirements.pnpm, pnpm_validator)
    _validate_node(requirements.node, node_validator)
    return requirements


def materialize_dependency_snapshots(
    root: candidate_storage.OpenSnapshotRoot,
    requirements: DependencyRequirements,
) -> DependencySnapshots:
    frontend = candidate_projection.materialize_dependency_snapshot(
        root,
        (candidate_authority.SNAPSHOT_REPOSITORY, "frontend", "node_modules"),
        requirements.frontend,
    )
    _validate_frontend(requirements.frontend, None)
    candidate_storage.create_private_child(root, "dependencies")
    python = candidate_projection.materialize_dependency_snapshot(
        root,
        ("dependencies", "python-site-packages"),
        requirements.python,
    )
    _validate_python(requirements.python, None)
    pnpm = candidate_projection.materialize_dependency_snapshot(
        root,
        ("dependencies", "pnpm"),
        requirements.pnpm,
    )
    _validate_pnpm(requirements.pnpm, None)
    node = _materialize_node_executable(root, requirements.node)
    _validate_node(requirements.node, None)
    candidate_projection.freeze_parent(root, ("dependencies", "node", "bin"))
    candidate_projection.freeze_parent(root, ("dependencies", "node"))
    candidate_projection.freeze_parent(root, ("dependencies",))
    return DependencySnapshots(frontend, python, pnpm, node)


def require_dependency_sources_current(
    snapshot: CandidateSnapshotIdentity,
    *,
    frontend_validator: ProjectionValidator | None,
    python_validator: PythonValidator | None,
    pnpm_validator: PnpmValidator | None,
    node_validator: NodeValidator | None,
) -> None:
    current = select_dependency_requirements(
        None,
        None,
        None,
        None,
        frontend_validator=frontend_validator,
        python_validator=python_validator,
        pnpm_validator=pnpm_validator,
        node_validator=node_validator,
    )
    _require_source_matches(snapshot.frontend_dependencies, current.frontend)
    _require_source_matches(snapshot.python_dependencies, current.python)
    _require_source_matches(snapshot.pnpm_dependencies, current.pnpm)
    _require_node_source_matches(snapshot.node_executable, current.node)


def verify_dependency_snapshots(snapshot: CandidateSnapshotIdentity) -> None:
    for identity in (snapshot.frontend_dependencies, snapshot.python_dependencies, snapshot.pnpm_dependencies):
        candidate_projection.verify_dependency_snapshot(dependency_evidence(identity))
    verify_node_executable(snapshot.node_executable)


def dependency_identity(
    evidence: candidate_projection.DependencyTreeSnapshot,
    requirement: candidate_projection.DependencyRequirement,
) -> CandidateDependencySnapshotIdentity:
    return CandidateDependencySnapshotIdentity(
        evidence.root,
        evidence.identity,
        requirement.sha256,
        requirement.projection_sha256,
        evidence.sha256,
        evidence.entries,
        evidence.regular_bytes,
    )


def dependency_evidence(identity: CandidateDependencySnapshotIdentity) -> candidate_projection.DependencyTreeSnapshot:
    return candidate_projection.DependencyTreeSnapshot(
        identity.root,
        identity.root_identity,
        identity.sha256,
        identity.entries,
        identity.regular_bytes,
    )


def verify_node_executable(evidence: CandidateExecutableSnapshotIdentity) -> None:
    parent = candidate_storage._open_real_directory(evidence.path.parent)
    descriptor: int | None = None
    try:
        linked = CandidatePathIdentity.from_stat(os.stat(evidence.path.name, dir_fd=parent.descriptor, follow_symlinks=False))
        descriptor = os.open(evidence.path.name, _READ_FLAGS, dir_fd=parent.descriptor)
        opened = CandidatePathIdentity.from_stat(os.fstat(descriptor))
        if linked != evidence.identity or opened != evidence.identity:
            raise candidate_storage.CandidateStorageError("candidate Node executable authority changed")
        digest, size = _hash_file(descriptor, _MAX_NODE_BYTES)
        after = CandidatePathIdentity.from_stat(os.fstat(descriptor))
        current = CandidatePathIdentity.from_stat(os.stat(evidence.path.name, dir_fd=parent.descriptor, follow_symlinks=False))
        if after != opened or current != opened or digest != evidence.sha256 or size != evidence.identity.size:
            raise candidate_storage.CandidateStorageError("candidate Node executable content changed")
    except OSError as exc:
        raise candidate_storage.CandidateStorageError("candidate Node executable is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent.descriptor)


def _materialize_node_executable(
    root: candidate_storage.OpenSnapshotRoot,
    requirement: NodeRequirement,
) -> CandidateExecutableSnapshotIdentity:
    source_parent = candidate_storage._open_real_directory(requirement.source_path.parent)
    source_fd: int | None = None
    destination_parent: int | None = None
    destination_fd: int | None = None
    try:
        source_identity = CandidatePathIdentity.from_stat(os.stat(requirement.source_path.name, dir_fd=source_parent.descriptor, follow_symlinks=False))
        _require_node_source_identity(source_identity, requirement)
        source_fd = os.open(requirement.source_path.name, _READ_FLAGS, dir_fd=source_parent.descriptor)
        if CandidatePathIdentity.from_stat(os.fstat(source_fd)) != source_identity:
            raise candidate_storage.CandidateStorageError("Node executable source was replaced")
        destination_parent = _create_node_parent(root)
        destination_fd = os.open("node", _WRITE_FLAGS, 0o500, dir_fd=destination_parent)
        digest, size = _copy_file(source_fd, destination_fd, requirement.size)
        os.fchmod(destination_fd, 0o500)
        identity = CandidatePathIdentity.from_stat(os.fstat(destination_fd))
        _require_node_source_linked(source_parent.descriptor, source_fd, requirement.source_path.name, source_identity)
    except OSError as exc:
        raise candidate_storage.CandidateStorageError("candidate Node executable could not be materialized") from exc
    finally:
        for descriptor in (destination_fd, destination_parent, source_fd, source_parent.descriptor):
            if descriptor is not None:
                os.close(descriptor)
    if digest != requirement.sha256 or size != requirement.size or identity.size != size:
        raise candidate_storage.CandidateStorageError("candidate Node executable does not match its source")
    evidence = CandidateExecutableSnapshotIdentity(
        root.authority.root / "dependencies/node/bin/node",
        identity,
        requirement.source_path,
        source_identity,
        requirement.sha256,
        digest,
    )
    verify_node_executable(evidence)
    return evidence


def _create_node_parent(root: candidate_storage.OpenSnapshotRoot) -> int:
    descriptor = candidate_projection._open_relative_directory(root.descriptor, ("dependencies",))
    try:
        for part in ("node", "bin"):
            os.mkdir(part, 0o700, dir_fd=descriptor)
            identity = CandidatePathIdentity.from_stat(os.stat(part, dir_fd=descriptor, follow_symlinks=False))
            candidate_storage.require_private_directory_authority(identity)
            child = candidate_storage._open_child_directory(descriptor, part, identity)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _require_node_source_identity(identity: CandidatePathIdentity, requirement: NodeRequirement) -> None:
    observed = (
        identity.device,
        identity.inode,
        stat.S_IMODE(identity.mode),
        identity.uid,
        identity.gid,
        identity.size,
        identity.modified_ns,
        identity.changed_ns,
    )
    expected = (
        requirement.device,
        requirement.inode,
        requirement.mode,
        requirement.uid,
        requirement.gid,
        requirement.size,
        requirement.mtime_ns,
        requirement.ctime_ns,
    )
    if observed != expected or not stat.S_ISREG(identity.mode) or identity.links != 1 or not identity.mode & 0o111 or not 0 < identity.size <= _MAX_NODE_BYTES:
        raise candidate_storage.CandidateStorageError("Node executable source authority changed")


def _require_node_source_linked(parent_fd: int, descriptor: int, name: str, expected: CandidatePathIdentity) -> None:
    opened = CandidatePathIdentity.from_stat(os.fstat(descriptor))
    linked = CandidatePathIdentity.from_stat(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
    if opened != expected or linked != expected:
        raise candidate_storage.CandidateStorageError("Node executable source changed while copied")


def _copy_file(source_fd: int, destination_fd: int, expected_size: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := os.read(source_fd, 1024 * 1024):
        size += len(chunk)
        if size > expected_size or size > _MAX_NODE_BYTES:
            raise candidate_storage.CandidateStorageError("Node executable source exceeds its bounded authority")
        digest.update(chunk)
        offset = 0
        while offset < len(chunk):
            offset += os.write(destination_fd, chunk[offset:])
    return digest.hexdigest(), size


def _hash_file(descriptor: int, maximum: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := os.read(descriptor, 1024 * 1024):
        size += len(chunk)
        if size > maximum:
            raise candidate_storage.CandidateStorageError("candidate Node executable exceeds its bounded authority")
        digest.update(chunk)
    return digest.hexdigest(), size


def _require_source_matches(
    expected: CandidateDependencySnapshotIdentity,
    current: candidate_projection.DependencyRequirement,
) -> None:
    authority = (current.sha256, current.projection_sha256, current.entries, current.regular_bytes)
    frozen = (expected.source_sha256, expected.source_projection_sha256, expected.entries, expected.regular_bytes)
    if authority != frozen or expected.sha256 != expected.source_projection_sha256:
        raise CandidateSnapshotError("candidate dependency source changed after snapshot preparation")


def _require_node_source_matches(expected: CandidateExecutableSnapshotIdentity, current: NodeRequirement) -> None:
    source = expected.source_identity
    frozen = (
        expected.source_path,
        source.device,
        source.inode,
        stat.S_IMODE(source.mode),
        source.uid,
        source.gid,
        source.size,
        source.modified_ns,
        source.changed_ns,
        expected.source_sha256,
    )
    authority = (
        current.source_path,
        current.device,
        current.inode,
        current.mode,
        current.uid,
        current.gid,
        current.size,
        current.mtime_ns,
        current.ctime_ns,
        current.sha256,
    )
    if authority != frozen or expected.sha256 != expected.source_sha256:
        raise CandidateSnapshotError("candidate Node executable source changed after snapshot preparation")


def _active_frontend_requirement() -> ProjectionRequirement:
    try:
        return acceptance_toolchain.frontend_dependency_projection_requirement()
    except acceptance_toolchain.ToolchainAuthorityError as exc:
        raise CandidateSnapshotError("frontend dependency projection authority is unavailable") from exc


def _validate_frontend(requirement: ProjectionRequirement, validator: ProjectionValidator | None) -> None:
    try:
        (validator or acceptance_toolchain.validate_frontend_dependency_projection)(requirement)
    except acceptance_toolchain.ToolchainAuthorityError as exc:
        raise CandidateSnapshotError("frontend dependency projection authority drifted") from exc


def _active_python_requirement() -> PythonRequirement:
    try:
        return acceptance_toolchain.python_dependency_snapshot_requirement()
    except acceptance_toolchain.ToolchainAuthorityError as exc:
        raise CandidateSnapshotError("Python dependency snapshot authority is unavailable") from exc


def _validate_python(requirement: PythonRequirement, validator: PythonValidator | None) -> None:
    try:
        (validator or acceptance_toolchain.validate_python_dependency_snapshot)(requirement)
    except acceptance_toolchain.ToolchainAuthorityError as exc:
        raise CandidateSnapshotError("Python dependency snapshot authority drifted") from exc


def _active_pnpm_requirement() -> PnpmRequirement:
    try:
        return acceptance_toolchain.pnpm_dependency_snapshot_requirement()
    except acceptance_toolchain.ToolchainAuthorityError as exc:
        raise CandidateSnapshotError("pnpm dependency snapshot authority is unavailable") from exc


def _validate_pnpm(requirement: PnpmRequirement, validator: PnpmValidator | None) -> None:
    try:
        (validator or acceptance_toolchain.validate_pnpm_dependency_snapshot)(requirement)
    except acceptance_toolchain.ToolchainAuthorityError as exc:
        raise CandidateSnapshotError("pnpm dependency snapshot authority drifted") from exc


def _active_node_requirement() -> NodeRequirement:
    try:
        return acceptance_toolchain.node_executable_snapshot_requirement()
    except acceptance_toolchain.ToolchainAuthorityError as exc:
        raise CandidateSnapshotError("Node executable snapshot authority is unavailable") from exc


def _validate_node(requirement: NodeRequirement, validator: NodeValidator | None) -> None:
    try:
        (validator or acceptance_toolchain.validate_node_executable_snapshot)(requirement)
    except acceptance_toolchain.ToolchainAuthorityError as exc:
        raise CandidateSnapshotError("Node executable snapshot authority drifted") from exc
