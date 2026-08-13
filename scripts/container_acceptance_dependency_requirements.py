"""容器验收候选依赖与私有目录的名义 authority 类型。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scripts.container_acceptance_dependency_authority import DependencyTreeAuthority
from scripts.container_acceptance_tool_authority import ToolAuthority


@dataclass(frozen=True, slots=True)
class FrontendDependencyProjectionRequirement:
    target_root: Path
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    mtime_ns: int
    ctime_ns: int
    entries: int
    regular_bytes: int
    sha256: str
    projection_sha256: str


@dataclass(frozen=True, slots=True)
class PythonDependencySnapshotRequirement:
    target_root: Path
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    mtime_ns: int
    ctime_ns: int
    entries: int
    regular_bytes: int
    sha256: str
    projection_sha256: str


@dataclass(frozen=True, slots=True)
class PnpmDependencySnapshotRequirement:
    target_root: Path
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    mtime_ns: int
    ctime_ns: int
    entries: int
    regular_bytes: int
    sha256: str
    projection_sha256: str


@dataclass(frozen=True, slots=True)
class NodeExecutableSnapshotRequirement:
    source_path: Path
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CandidateSnapshotParentRequirement:
    path: Path
    device: int
    inode: int
    mode: int
    uid: int
    gid: int


@dataclass(frozen=True, slots=True)
class ReceiptRootRequirement:
    path: Path
    device: int
    inode: int
    mode: int
    uid: int
    gid: int


def node_requirement(value: ToolAuthority) -> NodeExecutableSnapshotRequirement:
    return NodeExecutableSnapshotRequirement(
        Path(value["resolved_path"]),
        value["device"],
        value["inode"],
        value["mode"],
        value["uid"],
        value["gid"],
        value["size"],
        value["mtime_ns"],
        value["ctime_ns"],
        value["sha256"],
    )


def frontend_requirement(value: DependencyTreeAuthority) -> FrontendDependencyProjectionRequirement:
    return FrontendDependencyProjectionRequirement(
        Path(value["root"]),
        value["device"],
        value["inode"],
        value["mode"],
        value["uid"],
        value["gid"],
        value["mtime_ns"],
        value["ctime_ns"],
        value["entries"],
        value["regular_bytes"],
        value["sha256"],
        value["projection_sha256"],
    )


def python_requirement(value: DependencyTreeAuthority) -> PythonDependencySnapshotRequirement:
    return PythonDependencySnapshotRequirement(
        Path(value["root"]),
        value["device"],
        value["inode"],
        value["mode"],
        value["uid"],
        value["gid"],
        value["mtime_ns"],
        value["ctime_ns"],
        value["entries"],
        value["regular_bytes"],
        value["sha256"],
        value["projection_sha256"],
    )


def pnpm_requirement(value: DependencyTreeAuthority) -> PnpmDependencySnapshotRequirement:
    return PnpmDependencySnapshotRequirement(
        Path(value["root"]),
        value["device"],
        value["inode"],
        value["mode"],
        value["uid"],
        value["gid"],
        value["mtime_ns"],
        value["ctime_ns"],
        value["entries"],
        value["regular_bytes"],
        value["sha256"],
        value["projection_sha256"],
    )
