"""从受治理 Git commit 物化 AgentScope 可读的不可变 Harness 快照。"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.agent_paths import validate_agent_id

from .store import RuntimeStateConflict, harness_digest

_SOURCE_PREFIX = "published-"
_CANDIDATE_PREFIX = "candidate-"
_SOURCE_ID = re.compile(r"(?:published|candidate)-[0-9a-f]{48}")
_MARKER = "snapshot.json"
_MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 20_000


@dataclass(frozen=True)
class PublishedHarnessSnapshot:
    """一个 Git 版本对应的只读 Runtime source。"""

    source_id: str
    workspace_id: str
    workspace: Path
    agent_id: str
    agent_version_id: str
    harness_digest: str


class PublishedHarnessSnapshotStore:
    """在 API 可写、Runtime 只读的共享根下管理发布快照。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def materialize(
        self,
        *,
        version_store: GitAgentVersionStore,
        agent_id: str,
        agent_version_id: str,
        expected_digest: str,
    ) -> PublishedHarnessSnapshot:
        safe_agent_id = validate_agent_id(agent_id)
        resolved_version = version_store.resolve_commit_sha(agent_version_id)
        if resolved_version != agent_version_id:
            raise RuntimeStateConflict("Harness version must be a fully resolved Git commit")
        identity = self._snapshot_identity(
            safe_agent_id,
            resolved_version,
            expected_digest,
        )
        return self._materialize_identity(
            version_store=version_store,
            resolved_version=resolved_version,
            identity=identity,
        )

    def materialize_candidate(
        self,
        *,
        version_store: GitAgentVersionStore,
        agent_id: str,
        agent_version_id: str,
        expected_digest: str,
        isolation_key: str,
    ) -> PublishedHarnessSnapshot:
        """从精确 candidate commit 物化隔离的只读测试 source。"""

        safe_agent_id = validate_agent_id(agent_id)
        resolved_version = version_store.resolve_commit_sha(agent_version_id)
        if resolved_version != agent_version_id:
            raise RuntimeStateConflict("Candidate version must be a fully resolved Git commit")
        if not isolation_key:
            raise RuntimeStateConflict("Candidate snapshot isolation key is required")
        identity = self._snapshot_identity(
            safe_agent_id,
            resolved_version,
            expected_digest,
            source_prefix=_CANDIDATE_PREFIX,
            identity_salt=isolation_key,
        )
        return self._materialize_identity(
            version_store=version_store,
            resolved_version=resolved_version,
            identity=identity,
        )

    def _materialize_identity(
        self,
        *,
        version_store: GitAgentVersionStore,
        resolved_version: str,
        identity: PublishedHarnessSnapshot,
    ) -> PublishedHarnessSnapshot:
        self._ensure_root()
        target = self.root / identity.source_id
        if target.exists() or target.is_symlink():
            return self._validate_existing(target, identity)

        staging = Path(tempfile.mkdtemp(prefix=f".{identity.source_id}.", dir=self.root))
        try:
            workspace = staging / "workspace"
            workspace.mkdir(mode=0o700)
            archive = self._git_archive(version_store.repository_dir, resolved_version)
            self._extract_archive(archive, workspace)
            actual_digest = harness_digest(workspace)
            if actual_digest != identity.harness_digest:
                raise RuntimeStateConflict(
                    "Git commit Harness digest does not match the governed publication tuple",
                )
            self._write_marker(staging, identity)
            self._make_read_only(staging)
            self._fsync_tree(staging)
            try:
                os.rename(staging, target)
                self._fsync_directory(self.root)
            except OSError as exc:
                # POSIX filesystems report a concurrent non-empty directory winner
                # as EEXIST or ENOTEMPTY.  Only those exact races are recoverable;
                # permission/I/O/cross-device failures remain hard failures.
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                return self._validate_existing(target, identity)
            return self._validate_existing(target, identity)
        finally:
            if staging.exists() and not staging.is_symlink():
                self._remove_tree(staging)

    def require_existing(
        self,
        *,
        agent_id: str,
        agent_version_id: str,
        expected_digest: str,
    ) -> PublishedHarnessSnapshot:
        """纯读取并校验已 provision 的 tuple；不存在时绝不补建。"""

        identity = self._snapshot_identity(
            validate_agent_id(agent_id),
            agent_version_id,
            expected_digest,
        )
        target = self.root / identity.source_id
        if not target.exists() and not target.is_symlink():
            raise RuntimeStateConflict("Published Harness snapshot is missing for a provisioned Runtime Agent")
        return self._validate_existing(target, identity)

    def remove(
        self,
        *,
        agent_id: str,
        agent_version_id: str,
        expected_digest: str,
    ) -> bool:
        """删除一个精确 tuple 的快照；不扫描或猜测其他目录。"""

        identity = self._snapshot_identity(
            validate_agent_id(agent_id),
            agent_version_id,
            expected_digest,
        )
        target = self.root / identity.source_id
        if not target.exists() and not target.is_symlink():
            return True
        self._validate_existing(target, identity)
        self._remove_tree(target)
        return not target.exists() and not target.is_symlink()

    def remove_candidate(
        self,
        *,
        agent_id: str,
        agent_version_id: str,
        expected_digest: str,
        isolation_key: str,
    ) -> bool:
        identity = self._snapshot_identity(
            validate_agent_id(agent_id),
            agent_version_id,
            expected_digest,
            source_prefix=_CANDIDATE_PREFIX,
            identity_salt=isolation_key,
        )
        target = self.root / identity.source_id
        if not target.exists() and not target.is_symlink():
            return True
        self._validate_existing(target, identity)
        self._remove_tree(target)
        return not target.exists() and not target.is_symlink()

    def remove_exact_source(
        self,
        *,
        source_id: str,
        agent_id: str,
        agent_version_id: str,
        expected_digest: str,
    ) -> bool:
        """按账本保存的精确 source_id 清理 published/candidate 快照。"""

        if _SOURCE_ID.fullmatch(source_id) is None:
            raise RuntimeStateConflict("Harness snapshot source kind is not removable")
        identity = PublishedHarnessSnapshot(
            source_id=source_id,
            workspace_id=f"{source_id}--v-{expected_digest}",
            workspace=Path(),
            agent_id=validate_agent_id(agent_id),
            agent_version_id=agent_version_id,
            harness_digest=expected_digest,
        )
        # Reuse the strict tuple validator before resolving any path.
        self._snapshot_identity(identity.agent_id, agent_version_id, expected_digest)
        if self.root.is_symlink():
            raise RuntimeStateConflict("Published Harness root must not be a symlink")
        root = self.root.resolve()
        target = self.root / source_id
        if target.parent.resolve() != root:
            raise RuntimeStateConflict("Harness snapshot source escapes its governed root")
        if not target.exists() and not target.is_symlink():
            return True
        self._validate_existing(target, identity)
        self._remove_tree(target)
        return not target.exists() and not target.is_symlink()

    def candidate_identity(
        self,
        *,
        agent_id: str,
        agent_version_id: str,
        expected_digest: str,
        isolation_key: str,
    ) -> PublishedHarnessSnapshot:
        """纯计算 candidate 账本定位符，不创建目录。"""

        if not isolation_key:
            raise RuntimeStateConflict("Candidate snapshot isolation key is required")
        return self._snapshot_identity(
            validate_agent_id(agent_id),
            agent_version_id,
            expected_digest,
            source_prefix=_CANDIDATE_PREFIX,
            identity_salt=isolation_key,
        )

    @staticmethod
    def _snapshot_identity(
        agent_id: str,
        agent_version_id: str,
        expected_digest: str,
        *,
        source_prefix: str = _SOURCE_PREFIX,
        identity_salt: str = "",
    ) -> PublishedHarnessSnapshot:
        if not agent_version_id or any(character not in "0123456789abcdef" for character in agent_version_id):
            raise RuntimeStateConflict("Harness version must be a lowercase hexadecimal Git commit")
        if len(expected_digest) != 64 or any(character not in "0123456789abcdef" for character in expected_digest):
            raise RuntimeStateConflict("Harness digest must be 64 lowercase hexadecimal characters")
        token = hashlib.sha256(
            f"{identity_salt}\n{agent_id}\n{agent_version_id}\n{expected_digest}".encode(),
        ).hexdigest()[:48]
        source_id = f"{source_prefix}{token}"
        return PublishedHarnessSnapshot(
            source_id=source_id,
            workspace_id=f"{source_id}--v-{expected_digest}",
            workspace=Path(),
            agent_id=agent_id,
            agent_version_id=agent_version_id,
            harness_digest=expected_digest,
        )

    def _ensure_root(self) -> None:
        if self.root.is_symlink():
            raise RuntimeStateConflict("Published Harness root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise RuntimeStateConflict("Published Harness root must be a directory")

    @staticmethod
    def _git_archive(repository: Path, commit: str) -> bytes:
        try:
            result = subprocess.run(
                ["git", "-C", str(repository), "archive", "--format=tar", commit],
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise AgentGitError("Failed to materialize the governed Git commit") from exc
        if len(result.stdout) > _MAX_ARCHIVE_BYTES:
            raise RuntimeStateConflict("Governed Harness archive exceeds the Runtime size limit")
        return result.stdout

    @staticmethod
    def _extract_archive(archive: bytes, destination: Path) -> None:
        total_size = 0
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
            members = source.getmembers()
            if len(members) > _MAX_ARCHIVE_ENTRIES:
                raise RuntimeStateConflict("Governed Harness archive contains too many entries")
            for member in members:
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                    raise RuntimeStateConflict("Governed Harness archive contains an unsafe path")
                target = destination.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise RuntimeStateConflict("Governed Harness archive contains a non-regular entry")
                total_size += member.size
                if total_size > _MAX_ARCHIVE_BYTES:
                    raise RuntimeStateConflict("Governed Harness content exceeds the Runtime size limit")
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                extracted = source.extractfile(member)
                if extracted is None:
                    raise RuntimeStateConflict("Governed Harness archive entry is unreadable")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(target, flags, 0o600)
                try:
                    remaining = member.size
                    while remaining:
                        chunk = extracted.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise RuntimeStateConflict("Governed Harness archive entry is truncated")
                        os.write(descriptor, chunk)
                        remaining -= len(chunk)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

    @staticmethod
    def _write_marker(root: Path, identity: PublishedHarnessSnapshot) -> None:
        marker = root / _MARKER
        payload = {
            "schema_version": 1,
            "source_id": identity.source_id,
            "agent_id": identity.agent_id,
            "agent_version_id": identity.agent_version_id,
            "harness_digest": identity.harness_digest,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        descriptor = os.open(
            marker,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _validate_existing(
        self,
        target: Path,
        identity: PublishedHarnessSnapshot,
    ) -> PublishedHarnessSnapshot:
        if target.is_symlink() or not target.is_dir():
            raise RuntimeStateConflict("Published Harness snapshot root is unsafe")
        marker = target / _MARKER
        workspace = target / "workspace"
        if marker.is_symlink() or not marker.is_file() or workspace.is_symlink() or not workspace.is_dir():
            raise RuntimeStateConflict("Published Harness snapshot is incomplete or unsafe")
        if {entry.name for entry in target.iterdir()} != {_MARKER, "workspace"}:
            raise RuntimeStateConflict("Published Harness snapshot contains unexpected entries")
        self._require_read_only_tree(target)
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeStateConflict("Published Harness snapshot marker is invalid") from exc
        expected = {
            "schema_version": 1,
            "source_id": identity.source_id,
            "agent_id": identity.agent_id,
            "agent_version_id": identity.agent_version_id,
            "harness_digest": identity.harness_digest,
        }
        if payload != expected or harness_digest(workspace) != identity.harness_digest:
            raise RuntimeStateConflict("Published Harness snapshot does not match its immutable tuple")
        return PublishedHarnessSnapshot(
            source_id=identity.source_id,
            workspace_id=identity.workspace_id,
            workspace=workspace,
            agent_id=identity.agent_id,
            agent_version_id=identity.agent_version_id,
            harness_digest=identity.harness_digest,
        )

    @staticmethod
    def _require_read_only_tree(root: Path) -> None:
        for path in (root, *root.rglob("*")):
            mode = path.lstat().st_mode
            if mode & 0o222:
                raise RuntimeStateConflict("Published Harness snapshot is not read-only")

    @staticmethod
    def _make_read_only(root: Path) -> None:
        for current, directories, files in os.walk(root, topdown=False, followlinks=False):
            for name in files:
                path = Path(current) / name
                mode = path.stat(follow_symlinks=False).st_mode
                os.chmod(path, 0o555 if mode & stat.S_IXUSR else 0o444, follow_symlinks=False)
            for name in directories:
                os.chmod(Path(current) / name, 0o555, follow_symlinks=False)
        os.chmod(root, 0o555, follow_symlinks=False)

    @staticmethod
    def _fsync_tree(root: Path) -> None:
        for current, _directories, files in os.walk(root, topdown=False, followlinks=False):
            for name in files:
                descriptor = os.open(Path(current) / name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            PublishedHarnessSnapshotStore._fsync_directory(Path(current))

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _remove_tree(path: Path) -> None:
        if path.is_symlink():
            raise RuntimeStateConflict("Refusing to remove a symlinked Harness snapshot")
        for current, directories, files in os.walk(path, topdown=False, followlinks=False):
            for name in files:
                os.chmod(Path(current) / name, 0o600, follow_symlinks=False)
            for name in directories:
                os.chmod(Path(current) / name, 0o700, follow_symlinks=False)
        os.chmod(path, 0o700, follow_symlinks=False)
        shutil.rmtree(path)
