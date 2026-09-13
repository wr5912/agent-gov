"""Canonical deployable-source digest used by normal builds and deployments."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

try:
    from scripts.agentscope_atomic_cutover_images import selected_env_child_env
except ModuleNotFoundError:
    from agentscope_atomic_cutover_images import selected_env_child_env

SOURCE_ARTIFACT_LABEL = "io.agentgov.source-artifact-sha256"

__all__ = (
    "SOURCE_ARTIFACT_LABEL",
    "freeze_deployable_source",
    "selected_env_child_env",
    "source_artifact_sha256",
)


def _source_candidates(repo_root: Path) -> tuple[Path, ...]:
    return (
        *(repo_root / name for name in ("app", "agentscope_runtime", "scripts", "frontend", "config")),
        repo_root / "packages/agentgov-testkit",
        repo_root / "docker/api-gate",
        repo_root / "docker/runtime-bootstrap",
        *(
            repo_root / name
            for name in (
                "agentgov_agentscope_contract.py",
                "agentgov_harness_digest.py",
                "agentgov_run_permission.py",
                "agentgov_subagent_manifest_policy.py",
                "Makefile",
                "VERSION",
                "pyproject.toml",
                "requirements.txt",
                "requirements-api.txt",
            )
        ),
        *(repo_root / f"docker/{name}" for name in ("docker-compose.yml", "docker-compose.langfuse.yml")),
        *(repo_root / f"docker/{name}" for name in ("Dockerfile", "Dockerfile.dockerignore")),
        *(repo_root / f"docker/{name}" for name in ("frontend.Dockerfile", "frontend.Dockerfile.dockerignore")),
        *(repo_root / f"docker/{name}" for name in ("agentscope-runtime.Dockerfile", "agentscope-runtime.Dockerfile.dockerignore")),
    )


def _excluded_source(path: Path, repo_root: Path) -> bool:
    relative = path.relative_to(repo_root)
    parts = relative.parts
    if "__pycache__" in parts or path.suffix in {".pyc", ".pyo"}:
        return True
    if len(parts) >= 2 and parts[0] == "frontend" and parts[1] in {"node_modules", "dist"}:
        return True
    if path.name in {".env", ".env.local", ".env.local-debug"} or path.name.startswith(".env.bak"):
        return True
    return relative.as_posix() == "frontend/tsconfig.tsbuildinfo"


def _collect_source(path: Path, repo_root: Path, entries: dict[str, tuple[Path, os.stat_result]]) -> None:
    if _excluded_source(path, repo_root):
        return
    relative = path.relative_to(repo_root).as_posix()
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
        raise ValueError(f"deployable source 含 symlink/special entry: {relative}")
    entries[relative] = (path, metadata)
    if stat.S_ISDIR(metadata.st_mode):
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            _collect_source(child, repo_root, entries)


def _hash_file(digest: Any, path: Path, initial: os.stat_result, relative: str) -> None:
    digest.update(initial.st_size.to_bytes(8, "big"))
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size) != (
            initial.st_dev,
            initial.st_ino,
            initial.st_mode,
            initial.st_size,
        ):
            raise ValueError(f"deployable source 在摘要期间发生变化: {relative}")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        final = os.fstat(descriptor)
        opened_identity = (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
        final_identity = (final.st_dev, final.st_ino, final.st_mode, final.st_size, final.st_mtime_ns, final.st_ctime_ns)
        if opened_identity != final_identity:
            raise ValueError(f"deployable source 在摘要期间发生变化: {relative}")
    finally:
        os.close(descriptor)


def source_artifact_sha256(repo_root: Path) -> str:
    """Hash deployable source metadata and bytes without reading deployment secrets."""

    root = repo_root.resolve(strict=True)
    entries: dict[str, tuple[Path, os.stat_result]] = {}
    for candidate in _source_candidates(root):
        if not candidate.exists() and not candidate.is_symlink():
            raise ValueError(f"deployable source candidate 缺失: {candidate.relative_to(root)}")
        _collect_source(candidate, root, entries)
    digest = hashlib.sha256()
    for relative, (path, metadata) in sorted(entries.items()):
        encoded = relative.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(b"d" if stat.S_ISDIR(metadata.st_mode) else b"f")
        digest.update(stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"))
        if stat.S_ISREG(metadata.st_mode):
            _hash_file(digest, path, metadata, relative)
    return digest.hexdigest()


def _copy_file(source: Path, destination: Path, initial: os.stat_result, relative: str) -> None:
    source_fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    target_fd = -1
    try:
        opened = os.fstat(source_fd)
        expected = (initial.st_dev, initial.st_ino, initial.st_mode, initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns)
        current = (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
        if current != expected:
            raise ValueError(f"deployable source 在冻结前发生变化: {relative}")
        target_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        while chunk := os.read(source_fd, 1024 * 1024):
            view = memoryview(chunk)
            while view:
                view = view[os.write(target_fd, view) :]
        os.fchmod(target_fd, stat.S_IMODE(initial.st_mode))
        os.fsync(target_fd)
        final = os.fstat(source_fd)
        observed = (final.st_dev, final.st_ino, final.st_mode, final.st_size, final.st_mtime_ns, final.st_ctime_ns)
        if observed != expected:
            raise ValueError(f"deployable source 在冻结期间发生变化: {relative}")
    finally:
        if target_fd >= 0:
            os.close(target_fd)
        os.close(source_fd)


def freeze_deployable_source(repo_root: Path, destination: Path) -> str:
    """Create a private immutable-by-location build input whose digest becomes the image label."""

    root = repo_root.resolve(strict=True)
    destination.mkdir(mode=0o700)
    metadata = destination.lstat()
    if (
        destination.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or any(destination.iterdir())
    ):
        raise ValueError("deployable source snapshot 必须是当前用户持有的空 0700 真实目录")
    entries: dict[str, tuple[Path, os.stat_result]] = {}
    for candidate in _source_candidates(root):
        if not candidate.exists() and not candidate.is_symlink():
            raise ValueError(f"deployable source candidate 缺失: {candidate.relative_to(root)}")
        _collect_source(candidate, root, entries)
    directories: list[tuple[Path, int]] = []
    for relative, (source, initial) in sorted(entries.items()):
        target = destination / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stat.S_ISDIR(initial.st_mode):
            target.mkdir(mode=0o700, exist_ok=True)
            directories.append((target, stat.S_IMODE(initial.st_mode)))
        else:
            _copy_file(source, target, initial, relative)
    for target, mode in reversed(directories):
        target.chmod(mode)
    snapshot_digest = source_artifact_sha256(destination)
    if source_artifact_sha256(root) != snapshot_digest:
        raise ValueError("deployable source 在冻结事务期间发生变化")
    return snapshot_digest
