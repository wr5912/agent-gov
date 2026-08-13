from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Protocol


class _AcceptanceProfile(Protocol):
    build_services: tuple[str, ...]


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def image_evidence(runner: ModuleType, profile: _AcceptanceProfile) -> tuple[object, ...]:
    return tuple(
        runner.acceptance_support.LocalImageEvidence(
            service=service,
            image_id=f"sha256:{index:064x}",
            kind="external-runtime" if service in getattr(profile, "external_image_services", ()) else "candidate",
        )
        for index, service in enumerate((*profile.build_services, *getattr(profile, "external_image_services", ())), start=1)
    )


def candidate_authority(
    module: ModuleType,
    *,
    run_id: str = "1700000000-a1b2c3d4e5f6",
    profile: str = "agent-test",
    git_tree_sha: str = "a" * 40,
    selected_env_sha256: str = "b" * 64,
    reservation: object | None = None,
    reserved_receipt_sha256: str = "e" * 64,
) -> object:
    selected = reservation or candidate_reservation(module, run_id=run_id, profile=profile)
    return _candidate_authority(
        module,
        selected,
        reserved_receipt_sha256=reserved_receipt_sha256,
        git_tree_sha=git_tree_sha,
        selected_env_sha256=selected_env_sha256,
    )


def candidate_reservation(
    module: ModuleType,
    *,
    run_id: str = "1700000000-a1b2c3d4e5f6",
    profile: str = "agent-test",
    parent: Path = Path("/candidate-parent"),
) -> object:
    authority = module.candidate_authority
    identity_type = authority.CandidatePathIdentity
    directory = identity_type(1, 10, stat.S_IFDIR | 0o700, 1, 0, os.geteuid(), os.getegid(), 1, 1)
    nonce = "1" * 32
    root = parent / authority.reservation_root_name(run_id, nonce)
    reservation = authority.CandidateSnapshotReservation(
        run_id=run_id,
        profile=profile,
        parent=parent,
        parent_identity=directory,
        root=root,
        repository_root=Path("/source"),
        selected_env_file=Path("/source/docker/.env"),
        allow_public_env_read=False,
        nonce=nonce,
        marker_sha256="0" * 64,
        reserve_runtime=True,
    )
    return replace(reservation, marker_sha256=authority.reservation_marker_sha256(reservation))


def _candidate_authority(
    module: ModuleType,
    reservation: object,
    *,
    reserved_receipt_sha256: str,
    git_tree_sha: str,
    selected_env_sha256: str,
) -> object:
    authority = module.candidate_authority
    identity_type = authority.CandidatePathIdentity
    directory = identity_type(1, 10, stat.S_IFDIR | 0o500, 1, 0, os.geteuid(), os.getegid(), 1, 1)
    regular = identity_type(1, 11, stat.S_IFREG | 0o400, 1, 1, os.geteuid(), os.getegid(), 1, 1)
    dependency_type = authority.CandidateDependencySnapshotIdentity
    source = authority.CandidateSourceIdentity(
        repository_root=reservation.repository_root,
        repository=directory,
        selected_env_file=reservation.selected_env_file,
        selected_env=regular,
        git_tree_sha=git_tree_sha,
        selected_env_sha256=selected_env_sha256,
        allow_public_env_read=reservation.allow_public_env_read,
    )
    loaded = (authority.LoadedSourceIdentity("scripts/run_container_acceptance.py", "8" * 64),)
    repository_root = reservation.root / authority.SNAPSHOT_REPOSITORY
    frontend_dependency = dependency_type(
        root=repository_root / "frontend/node_modules",
        root_identity=directory,
        source_sha256="9" * 64,
        source_projection_sha256="6" * 64,
        sha256="6" * 64,
        entries=1,
        regular_bytes=1,
    )
    python_dependency = dependency_type(
        root=reservation.root / "dependencies/python-site-packages",
        root_identity=directory,
        source_sha256="5" * 64,
        source_projection_sha256="4" * 64,
        sha256="4" * 64,
        entries=1,
        regular_bytes=1,
    )
    pnpm_dependency = dependency_type(
        root=reservation.root / "dependencies/pnpm",
        root_identity=directory,
        source_sha256="3" * 64,
        source_projection_sha256="2" * 64,
        sha256="2" * 64,
        entries=1,
        regular_bytes=1,
    )
    copied_node = identity_type(1, 12, stat.S_IFREG | 0o500, 1, 1, os.geteuid(), os.getegid(), 1, 1)
    source_node = identity_type(1, 13, stat.S_IFREG | 0o555, 1, 1, os.geteuid(), os.getegid(), 1, 1)
    node_executable = authority.CandidateExecutableSnapshotIdentity(
        path=reservation.root / "dependencies/node/bin/node",
        identity=copied_node,
        source_path=Path("/toolchain/node"),
        source_identity=source_node,
        source_sha256="1" * 64,
        sha256="1" * 64,
    )
    snapshot = authority.CandidateSnapshotIdentity(
        run_id=reservation.run_id,
        profile=reservation.profile,
        reserved_receipt_sha256=reserved_receipt_sha256,
        parent=reservation.parent,
        parent_identity=reservation.parent_identity,
        root=reservation.root,
        root_identity=directory,
        repository_root=repository_root,
        repository_identity=directory,
        env_file=reservation.root / authority.SNAPSHOT_ENV,
        env_identity=regular,
        marker_file=reservation.root / authority.SNAPSHOT_MARKER,
        marker_identity=regular,
        reservation_nonce=reservation.nonce,
        reservation_sha256=reservation.marker_sha256,
        marker_sha256="7" * 64,
        runtime_root=reservation.root / authority.SNAPSHOT_RUNTIME,
        runtime_identity=directory,
        runtime_bootstrap=repository_root.joinpath(*authority.RUNTIME_BOOTSTRAP_PARTS),
        runtime_bootstrap_identity=directory,
        frontend_dependencies=frontend_dependency,
        python_dependencies=python_dependency,
        pnpm_dependencies=pnpm_dependency,
        node_executable=node_executable,
        loaded_sources=loaded,
        loaded_sources_sha256=authority.loaded_sources_digest(loaded),
        git_tree_sha=git_tree_sha,
        repository_sha256="d" * 64,
        selected_env_sha256=selected_env_sha256,
        file_count=1,
        total_bytes=1,
        dependencies_identity=directory,
    )
    prepared = authority.PreparedCandidateAuthority(source=source, snapshot=snapshot)
    authority.validate_prepared_layout(prepared)
    return prepared


def compose_config(*, project_name: str, volume_name: str) -> str:
    return json.dumps(
        {
            "name": project_name,
            "volumes": {"agent-test-runs": {"name": volume_name, "driver": "local"}},
            "networks": {"default": {"name": f"{project_name}_default", "driver": "bridge"}},
        }
    )


def volume_inspect(*, project_name: str, volume_name: str, mountpoint: str) -> str:
    return json.dumps(
        [
            {
                "Name": volume_name,
                "Driver": "local",
                "Scope": "local",
                "Options": None,
                "Mountpoint": mountpoint,
                "Labels": {
                    "com.docker.compose.project": project_name,
                    "com.docker.compose.volume": "agent-test-runs",
                },
            }
        ]
    )
