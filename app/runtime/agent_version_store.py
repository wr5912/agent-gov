from __future__ import annotations

import difflib
import fcntl
import fnmatch
import hashlib
import json
import os
import shutil
import tarfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from app.runtime.errors import AgentVersionIntegrityError


SNAPSHOT_POLICY_VERSION = "main-workspace-managed-config-v2"
MAX_FILE_DIFF_BYTES = 200_000
WORKSPACE_EXCLUDED_NAMES = {
    ".cache",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "dist",
    "node_modules",
}
WORKSPACE_EXCLUDED_PATTERNS = ("*.pyc", "*.pyo")

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentVersionStore:
    """Version managed agent configuration without depending on Git."""

    def __init__(self, *, versions_dir: Path, workspace_dir: Path, claude_root: Path) -> None:
        self.versions_dir = versions_dir
        self.workspace_dir = workspace_dir
        self.claude_root = claude_root
        self.bundles_dir = self.versions_dir / "bundles"
        self.manifests_dir = self.versions_dir / "manifests"
        self.tmp_dir = self.versions_dir / "tmp"
        self._lock = threading.RLock()
        self._maintenance = False
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.bundles_dir.mkdir(parents=True, exist_ok=True)
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    @property
    def versions_path(self) -> Path:
        return self.versions_dir / "versions.jsonl"

    @property
    def current_path(self) -> Path:
        return self.versions_dir / "current.json"

    def is_maintenance_active(self) -> bool:
        return self._maintenance

    def ensure_bootstrap(self) -> dict[str, Any]:
        current = self.current_version()
        if current:
            return current
        return self.create_snapshot(reason="bootstrap", note="初始化 Agent 版本基线。")

    def current_version_id(self) -> Optional[str]:
        return self.ensure_bootstrap().get("agent_version_id")

    def current_version(self) -> Optional[dict[str, Any]]:
        if not self.current_path.exists():
            return None
        try:
            loaded = json.loads(self.current_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(loaded, dict):
            return None
        version_id = loaded.get("agent_version_id")
        return self.get_version(str(version_id)) if version_id else None

    def list_versions(self, limit: int = 100) -> list[dict[str, Any]]:
        versions = self._read_jsonl(self.versions_path)
        return list(reversed(versions))[:limit]

    def get_version(self, version_id: str) -> Optional[dict[str, Any]]:
        for record in reversed(self._read_jsonl(self.versions_path)):
            if record.get("agent_version_id") == version_id:
                return record
        return None

    def get_manifest(self, version_id: str) -> Optional[dict[str, Any]]:
        path = self.manifests_dir / f"{version_id}.json"
        if not path.exists():
            return None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return loaded if isinstance(loaded, dict) else None

    def create_snapshot(
        self,
        *,
        reason: str = "manual_snapshot",
        source_proposal_ids: Optional[list[str]] = None,
        note: Optional[str] = None,
        parent_version_id: Optional[str] = None,
        rollback_of_version_id: Optional[str] = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._ensure_dirs()
            current = self.current_version()
            parent_id = parent_version_id if parent_version_id is not None else self._string(current, "agent_version_id")
            version_id = self._new_version_id()
            bundle_path = self.bundles_dir / f"{version_id}.tar.gz"

            entries, skipped = self._collect_entries()
            with tarfile.open(bundle_path, "w:gz", dereference=False) as tar:
                for entry in entries:
                    source_path = entry.get("_source_path")
                    archive_path = entry.get("path")
                    if source_path and archive_path:
                        tar.add(source_path, arcname=archive_path, recursive=False)

            bundle_sha = self._sha256_file(bundle_path)
            manifest = {
                "agent_version_id": version_id,
                "parent_version_id": parent_id,
                "created_at": utc_now(),
                "reason": reason,
                "rollback_of_version_id": rollback_of_version_id,
                "source_proposal_ids": source_proposal_ids or [],
                "note": note,
                "agent_yaml_version": self._agent_yaml_version(),
                "snapshot_policy_version": SNAPSHOT_POLICY_VERSION,
                "included_roots": [
                    {"name": "main-workspace", "path": str(self.workspace_dir), "mode": "managed_full_with_excludes"},
                ],
                "excluded_paths": self._excluded_policy(),
                "skipped_paths": skipped,
                "bundle_path": str(bundle_path),
                "bundle_sha256": bundle_sha,
                "file_count": sum(1 for entry in entries if entry.get("type") == "file"),
                "entry_count": len(entries),
                "total_bytes": sum(int(entry.get("size") or 0) for entry in entries if entry.get("type") == "file"),
                "files": [self._public_entry(entry) for entry in entries],
                "related_data": {
                    "data_dir": str(self.versions_dir.parent),
                    "runtime_db_path": str(self.versions_dir.parent / "runtime.sqlite3"),
                },
            }
            manifest_path = self.manifests_dir / f"{version_id}.json"
            self._write_json(manifest_path, manifest)

            summary = {
                "agent_version_id": version_id,
                "parent_version_id": parent_id,
                "created_at": manifest["created_at"],
                "reason": reason,
                "rollback_of_version_id": rollback_of_version_id,
                "source_proposal_ids": source_proposal_ids or [],
                "note": note,
                "agent_yaml_version": manifest["agent_yaml_version"],
                "snapshot_policy_version": SNAPSHOT_POLICY_VERSION,
                "bundle_sha256": bundle_sha,
                "bundle_path": str(bundle_path),
                "manifest_path": str(manifest_path),
                "file_count": manifest["file_count"],
                "entry_count": manifest["entry_count"],
                "total_bytes": manifest["total_bytes"],
            }
            self._append_jsonl(self.versions_path, summary)
            self._write_json(self.current_path, {"agent_version_id": version_id, "updated_at": utc_now()})
            return summary

    def restore_version(self, version_id: str, *, note: Optional[str] = None) -> Optional[dict[str, Any]]:
        with self._lock:
            target = self.get_version(version_id)
            manifest = self.get_manifest(version_id)
            if not target or not manifest:
                return None

            self._maintenance = True
            extract_dir = self.tmp_dir / f"restore-{version_id}-{uuid.uuid4().hex[:8]}"
            try:
                pre_restore = self.create_snapshot(
                    reason="pre_restore",
                    note=f"恢复 {version_id} 前自动保存当前受管配置。",
                )
                bundle_path = Path(str(manifest.get("bundle_path") or self.bundles_dir / f"{version_id}.tar.gz"))
                expected_sha = str(manifest.get("bundle_sha256") or "")
                if not bundle_path.exists() or self._sha256_file(bundle_path) != expected_sha:
                    raise AgentVersionIntegrityError("Agent version bundle hash mismatch")

                extract_dir.mkdir(parents=True, exist_ok=True)
                self._safe_extract(bundle_path, extract_dir)
                self._restore_manifest_files(manifest, extract_dir)
                restored = self.create_snapshot(
                    reason="rollback",
                    note=note or f"恢复到 {version_id} 的受管配置。",
                    parent_version_id=pre_restore.get("agent_version_id"),
                    rollback_of_version_id=version_id,
                )
                return {
                    "restored_from_version": target,
                    "pre_restore_version": pre_restore,
                    "current_version": restored,
                    "requires_runtime_restart": True,
                }
            finally:
                shutil.rmtree(extract_dir, ignore_errors=True)
                self._maintenance = False

    def diff_versions(self, from_version_id: str, to_version_id: str) -> Optional[dict[str, Any]]:
        left = self.get_manifest(from_version_id)
        right = self.get_manifest(to_version_id)
        if not left or not right:
            return None
        left_files = {entry["path"]: entry for entry in left.get("files", []) if isinstance(entry, dict)}
        right_files = {entry["path"]: entry for entry in right.get("files", []) if isinstance(entry, dict)}
        added: list[dict[str, Any]] = []
        deleted: list[dict[str, Any]] = []
        modified: list[dict[str, Any]] = []
        unchanged: list[dict[str, Any]] = []

        for path in sorted(set(left_files) | set(right_files)):
            before = left_files.get(path)
            after = right_files.get(path)
            if before is None and after is not None:
                added.append(after)
            elif before is not None and after is None:
                deleted.append(before)
            elif before and after and self._entry_fingerprint(before) != self._entry_fingerprint(after):
                modified.append({"path": path, "before": before, "after": after})
            elif after:
                unchanged.append(after)

        return {
            "from_version_id": from_version_id,
            "to_version_id": to_version_id,
            "added": added,
            "modified": modified,
            "deleted": deleted,
            "unchanged_count": len(unchanged),
        }

    def diff_version_file(self, from_version_id: str, to_version_id: str, path: str) -> Optional[dict[str, Any]]:
        archive_path = self._archive_path_for_user_path(path)
        if not archive_path:
            return None
        left = self.get_manifest(from_version_id)
        right = self.get_manifest(to_version_id)
        if not left or not right:
            return None
        left_entry = self._manifest_entry(left, archive_path)
        right_entry = self._manifest_entry(right, archive_path)
        status = self._file_diff_status(left_entry, right_entry)
        result: dict[str, Any] = {
            "from_version_id": from_version_id,
            "to_version_id": to_version_id,
            "path": path,
            "archive_path": archive_path,
            "status": status,
            "before": left_entry,
            "after": right_entry,
            "unified_diff": "",
            "is_text": False,
            "truncated": False,
            "reason": None,
        }
        if status == "missing":
            result["reason"] = "文件未出现在两个版本快照中。"
            return result
        if status == "unchanged":
            result["reason"] = "文件内容未变化。"
            return result
        for entry in (left_entry, right_entry):
            if entry and entry.get("type") != "file":
                result["reason"] = "目标不是普通文本文件，无法展示内容级 diff。"
                return result
            if entry and int(entry.get("size") or 0) > MAX_FILE_DIFF_BYTES:
                result["status"] = "binary_or_too_large"
                result["truncated"] = True
                result["reason"] = f"文件超过 {MAX_FILE_DIFF_BYTES} bytes，未展开内容。"
                return result
        before_bytes = self._read_version_file_bytes(left, archive_path) if left_entry else b""
        after_bytes = self._read_version_file_bytes(right, archive_path) if right_entry else b""
        if before_bytes is None or after_bytes is None:
            result["reason"] = "版本包中缺少对应文件内容。"
            return result
        if b"\x00" in before_bytes or b"\x00" in after_bytes:
            result["status"] = "binary_or_too_large"
            result["reason"] = "文件包含二进制内容，未展开内容。"
            return result
        try:
            before_text = before_bytes.decode("utf-8")
            after_text = after_bytes.decode("utf-8")
        except UnicodeDecodeError:
            result["status"] = "binary_or_too_large"
            result["reason"] = "文件不是 UTF-8 文本，未展开内容。"
            return result
        before_lines = before_text.splitlines(keepends=True)
        after_lines = after_text.splitlines(keepends=True)
        result["is_text"] = True
        result["unified_diff"] = "".join(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"{from_version_id}:{archive_path}",
                tofile=f"{to_version_id}:{archive_path}",
                lineterm="\n",
            )
        )
        return result

    def _collect_entries(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        entries: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        self._collect_workspace_entries(entries, skipped)
        entries.sort(key=lambda entry: str(entry.get("path") or ""))
        return entries, skipped

    def _archive_path_for_user_path(self, path: str) -> Optional[str]:
        if not path:
            return None
        raw = str(path).strip().replace("\\", "/")
        parts = Path(raw).parts
        if Path(raw).is_absolute() or ".." in parts:
            return None
        if raw == "workspace" or raw.startswith("workspace/"):
            return raw
        return f"workspace/{raw}"

    def _manifest_entry(self, manifest: dict[str, Any], archive_path: str) -> Optional[dict[str, Any]]:
        for entry in manifest.get("files", []) or []:
            if isinstance(entry, dict) and entry.get("path") == archive_path:
                return entry
        return None

    def _file_diff_status(self, before: Optional[dict[str, Any]], after: Optional[dict[str, Any]]) -> str:
        if before is None and after is None:
            return "missing"
        if before is None:
            return "added"
        if after is None:
            return "deleted"
        if self._entry_fingerprint(before) == self._entry_fingerprint(after):
            return "unchanged"
        return "modified"

    def _read_version_file_bytes(self, manifest: dict[str, Any], archive_path: str) -> Optional[bytes]:
        bundle_path = Path(str(manifest.get("bundle_path") or self.bundles_dir / f"{manifest.get('agent_version_id')}.tar.gz"))
        expected_sha = str(manifest.get("bundle_sha256") or "")
        if not bundle_path.exists():
            return None
        if expected_sha and self._sha256_file(bundle_path) != expected_sha:
            return None
        try:
            with tarfile.open(bundle_path, "r:gz") as tar:
                member = tar.getmember(archive_path)
                if not member.isfile():
                    return None
                handle = tar.extractfile(member)
                return handle.read(MAX_FILE_DIFF_BYTES + 1) if handle else None
        except (KeyError, OSError, tarfile.TarError):
            return None

    def _collect_workspace_entries(self, entries: list[dict[str, Any]], skipped: list[dict[str, Any]]) -> None:
        if not self.workspace_dir.exists():
            skipped.append({"path": "workspace", "reason": "missing"})
            return
        for root, dirnames, filenames in os.walk(self.workspace_dir, topdown=True, followlinks=False):
            root_path = Path(root)
            rel_root = root_path.relative_to(self.workspace_dir)
            kept_dirs = []
            for dirname in dirnames:
                rel = rel_root / dirname
                if self._workspace_excluded(rel):
                    skipped.append({"path": f"workspace/{rel.as_posix()}", "reason": "workspace_exclude"})
                elif (root_path / dirname).is_symlink():
                    self._append_entry(entries, root_path / dirname, f"workspace/{rel.as_posix()}", skipped)
                else:
                    kept_dirs.append(dirname)
            dirnames[:] = kept_dirs

            if rel_root != Path("."):
                self._append_entry(entries, root_path, f"workspace/{rel_root.as_posix()}", skipped)
            for filename in filenames:
                rel = rel_root / filename
                if self._workspace_excluded(rel):
                    skipped.append({"path": f"workspace/{rel.as_posix()}", "reason": "workspace_exclude"})
                    continue
                self._append_entry(entries, root_path / filename, f"workspace/{rel.as_posix()}", skipped)

    def _append_entry(self, entries: list[dict[str, Any]], source: Path, archive_path: str, skipped: list[dict[str, Any]]) -> None:
        try:
            stat = source.lstat()
        except OSError as exc:
            skipped.append({"path": archive_path, "reason": f"stat_failed:{exc.__class__.__name__}"})
            return
        if source.is_symlink():
            entry_type = "symlink"
            size = len(os.readlink(source))
            sha = None
            link_target = os.readlink(source)
        elif source.is_file():
            entry_type = "file"
            size = stat.st_size
            sha = self._sha256_file(source)
            link_target = None
        elif source.is_dir():
            entry_type = "dir"
            size = 0
            sha = None
            link_target = None
        else:
            skipped.append({"path": archive_path, "reason": "special_file"})
            return
        entries.append(
            {
                "path": archive_path,
                "type": entry_type,
                "sha256": sha,
                "size": size,
                "mode": stat.st_mode,
                "mtime": int(stat.st_mtime),
                "link_target": link_target,
                "_source_path": str(source),
            }
        )

    def _restore_manifest_files(self, manifest: dict[str, Any], extract_dir: Path) -> None:
        target_entries = [entry for entry in manifest.get("files", []) if isinstance(entry, dict)]
        target_paths = {str(entry.get("path")) for entry in target_entries}
        current_entries, _ = self._collect_entries()
        for entry in sorted(current_entries, key=lambda item: str(item.get("path") or "").count("/"), reverse=True):
            archive_path = str(entry.get("path") or "")
            if archive_path in target_paths:
                continue
            dest = self._destination_for_archive_path(archive_path)
            if not dest or not dest.exists() and not dest.is_symlink():
                continue
            if entry.get("type") in {"file", "symlink"}:
                dest.unlink(missing_ok=True)

        for entry in sorted(target_entries, key=lambda item: str(item.get("path") or "").count("/")):
            archive_path = str(entry.get("path") or "")
            source = extract_dir / archive_path
            dest = self._destination_for_archive_path(archive_path)
            if not dest:
                continue
            entry_type = entry.get("type")
            if entry_type == "dir":
                dest.mkdir(parents=True, exist_ok=True)
            elif entry_type == "file":
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.is_dir() and not dest.is_symlink():
                    shutil.rmtree(dest)
                elif dest.exists() or dest.is_symlink():
                    dest.unlink()
                shutil.copy2(source, dest)
            elif entry_type == "symlink":
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.is_dir() and not dest.is_symlink():
                    shutil.rmtree(dest)
                elif dest.exists() or dest.is_symlink():
                    dest.unlink()
                os.symlink(str(entry.get("link_target") or ""), dest)

        for entry in sorted(current_entries, key=lambda item: str(item.get("path") or "").count("/"), reverse=True):
            archive_path = str(entry.get("path") or "")
            if archive_path in target_paths or entry.get("type") != "dir":
                continue
            dest = self._destination_for_archive_path(archive_path)
            if dest and dest.exists():
                try:
                    dest.rmdir()
                except OSError:
                    pass

    def _destination_for_archive_path(self, archive_path: str) -> Optional[Path]:
        if archive_path == "workspace":
            return self.workspace_dir
        if archive_path.startswith("workspace/"):
            return self.workspace_dir / archive_path.removeprefix("workspace/")
        return None

    def _safe_extract(self, bundle_path: Path, extract_dir: Path) -> None:
        with tarfile.open(bundle_path, "r:gz") as tar:
            for member in tar.getmembers():
                name = member.name
                if Path(name).is_absolute() or ".." in Path(name).parts:
                    raise AgentVersionIntegrityError(f"Unsafe archive path: {name}")
                if not (name == "workspace" or name.startswith("workspace/")):
                    raise AgentVersionIntegrityError(f"Unexpected archive path: {name}")
            tar.extractall(extract_dir)

    def _workspace_excluded(self, rel: Path) -> bool:
        parts = rel.parts
        if any(part in WORKSPACE_EXCLUDED_NAMES for part in parts):
            return True
        name = rel.name
        return any(fnmatch.fnmatch(name, pattern) for pattern in WORKSPACE_EXCLUDED_PATTERNS)

    def _excluded_policy(self) -> list[dict[str, str]]:
        return [
            {"path": "/data", "reason": "runtime_data_excluded"},
            {"path": "/claude-roots", "reason": "claude_runtime_state_excluded"},
            *({"path": f"workspace/**/{name}", "reason": "workspace_cache_or_build_artifact"} for name in sorted(WORKSPACE_EXCLUDED_NAMES)),
            *({"path": f"workspace/**/{pattern}", "reason": "workspace_cache_or_build_artifact"} for pattern in WORKSPACE_EXCLUDED_PATTERNS),
        ]

    def _agent_yaml_version(self) -> Optional[str]:
        path = self.workspace_dir / "agent.yaml"
        if not path.exists():
            return None
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return None
        if not isinstance(loaded, dict):
            return None
        agent = loaded.get("agent")
        if not isinstance(agent, dict):
            return None
        version = agent.get("version")
        return str(version) if version is not None else None

    def _new_version_id(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"agent-version-{stamp}-{uuid.uuid4().hex[:8]}"

    def _entry_fingerprint(self, entry: dict[str, Any]) -> tuple[Any, ...]:
        return (entry.get("type"), entry.get("sha256"), entry.get("size"), entry.get("link_target"))

    def _public_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in entry.items() if not key.startswith("_")}

    def _sha256_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _string(self, record: Optional[dict[str, Any]], key: str) -> Optional[str]:
        if not record:
            return None
        value = record.get(key)
        return value if isinstance(value, str) and value else None

    def _ensure_dirs(self) -> None:
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.bundles_dir.mkdir(parents=True, exist_ok=True)
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)

    def _append_jsonl(self, path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._file_lock(path):
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str))
                fh.write("\n")

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self._file_lock(path):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    loaded = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(loaded, dict):
                    records.append(loaded)
        return records

    @contextmanager
    def _file_lock(self, path: Path):
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
