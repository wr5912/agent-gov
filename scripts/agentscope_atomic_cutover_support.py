"""Destructive/restore support for the AgentScope atomic cutover CLI.

The read-only ``inspect`` path intentionally does not depend on this module so
the deployment preflight can continue to copy and execute one self-contained
file before replacing the remote source tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NoReturn, cast
from urllib.request import Request, urlopen

try:
    from scripts.agentscope_atomic_cutover_types import (
        REQUIRED_MANIFEST_STRING_FIELDS,
        CoreImageIds,
        CutoverManifest,
        EnvValues,
        FinalEvidence,
        GateState,
        JsonObject,
        ProductionDrain,
        ProductionDrainArtifacts,
        RollbackBundle,
        RollbackImage,
        TreeEntry,
        is_rollback_image_list,
        is_tree_entry_list,
    )
except ModuleNotFoundError:
    from agentscope_atomic_cutover_types import (
        REQUIRED_MANIFEST_STRING_FIELDS,
        CoreImageIds,
        CutoverManifest,
        EnvValues,
        FinalEvidence,
        GateState,
        JsonObject,
        ProductionDrain,
        ProductionDrainArtifacts,
        RollbackBundle,
        RollbackImage,
        TreeEntry,
        is_rollback_image_list,
        is_tree_entry_list,
    )


class CutoverSupport:
    """Bound operations unavailable to the standalone read-only preflight."""

    def __init__(
        self,
        *,
        error_type: type[RuntimeError],
        repo_root: Path,
        compose_file: Path,
        schema_epoch: str,
        utc_now: Callable[[], str],
        sha256_file: Callable[[Path], str],
        source_artifact_sha256: Callable[[], str],
        load_env_file: Callable[[Path], EnvValues],
        resolve_runtime_root: Callable[..., Path],
        compose_base: Callable[[Path, Path], list[str]],
        classify_runtime_epoch: Callable[[Path], Mapping[str, object]],
        database_path: Callable[[Path, Path], Path],
    ) -> None:
        self._error_type = error_type
        self._repo_root = repo_root
        self._compose_file = compose_file
        self._schema_epoch = schema_epoch
        self._utc_now = utc_now
        self._sha256_file = sha256_file
        self._source_artifact_sha256 = source_artifact_sha256
        self._load_env_file = load_env_file
        self._resolve_runtime_root = resolve_runtime_root
        self._compose_base = compose_base
        self._classify_runtime_epoch = classify_runtime_epoch
        self._database_path = database_path

    def _fail(self, message: str) -> NoReturn:
        raise self._error_type(message)

    def tree_manifest(self, root: Path) -> list[TreeEntry]:
        entries: list[TreeEntry] = []
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
            metadata = path.lstat()
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(metadata.st_mode) or not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
                self._fail(f"Runtime root 含不支持的 link/special entry: {relative}")
            entry = TreeEntry(
                path=relative,
                type="dir" if stat.S_ISDIR(metadata.st_mode) else "file",
                mode=stat.S_IMODE(metadata.st_mode),
                uid=metadata.st_uid,
                gid=metadata.st_gid,
                size=metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0,
            )
            if stat.S_ISREG(metadata.st_mode):
                entry["sha256"] = self._sha256_file(path)
            entries.append(entry)
        return entries

    @staticmethod
    def tree_content_projection(entries: list[TreeEntry]) -> list[TreeEntry]:
        projected: list[TreeEntry] = []
        for entry in entries:
            item = dict(entry)
            item.pop("uid", None)
            item.pop("gid", None)
            projected.append(cast(TreeEntry, item))
        return projected

    def safe_extract_regular_archive(self, archive: Path, destination: Path) -> None:
        if destination.exists():
            if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
                self._fail("restore 目标必须是不存在或已清空的真实目录")
        else:
            destination.mkdir(parents=True, exist_ok=False)
        directories: list[tuple[Path, tarfile.TarInfo]] = []
        with tarfile.open(archive, "r") as stream:
            for member in stream.getmembers():
                target = self._validated_archive_target(member, destination)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    directories.append((target, member))
                    continue
                source = stream.extractfile(member)
                if source is None:
                    self._fail("快照普通文件无法读取")
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                self._restore_metadata(target, member)
            for target, member in reversed(directories):
                self._restore_metadata(target, member)

    def _validated_archive_target(self, member: tarfile.TarInfo, destination: Path) -> Path:
        member_path = Path(member.name)
        if member_path.is_absolute() or ".." in member_path.parts:
            self._fail("快照 restore drill 检测到路径逃逸")
        target = (destination / member_path).resolve()
        if not target.is_relative_to(destination.resolve()):
            self._fail("快照 restore drill 检测到路径逃逸")
        if not member.isdir() and not member.isfile():
            self._fail("快照只允许普通文件与目录")
        return target

    @staticmethod
    def _restore_metadata(target: Path, member: tarfile.TarInfo) -> None:
        os.chmod(target, member.mode)
        if os.geteuid() == 0:
            os.chown(target, member.uid, member.gid)

    def create_snapshot_with_restore_drill(
        self,
        runtime_root: Path,
        env_file: Path,
        backup_root: Path,
    ) -> tuple[Path, str, list[TreeEntry]]:
        backup = backup_root.expanduser().resolve()
        if backup == runtime_root or backup.is_relative_to(runtime_root):
            self._fail("快照目录必须位于活动 Runtime root 之外")
        backup.mkdir(parents=True, exist_ok=True, mode=0o700)
        if backup.is_symlink():
            self._fail("快照目录不得是符号链接")
        tree = self.tree_manifest(runtime_root)
        archive = backup / "runtime-root.tar"
        with tarfile.open(archive, "w") as stream:
            for entry in tree:
                source = runtime_root / entry["path"]
                stream.add(source, arcname=entry["path"], recursive=False)
        archive.chmod(0o600)
        archive_sha = self._sha256_file(archive)
        with tempfile.TemporaryDirectory(prefix="restore-drill-", dir=backup) as drill_name:
            drill_root = Path(drill_name) / "runtime-root"
            self.safe_extract_regular_archive(archive, drill_root)
            restored = self.tree_manifest(drill_root)
            if self.tree_content_projection(restored) != self.tree_content_projection(tree):
                self._fail("快照 restore drill 的内容或 mode 与活动 root 不一致")
        env_snapshot = backup / "compose.env.snapshot"
        shutil.copy2(env_file, env_snapshot)
        env_snapshot.chmod(0o600)
        return archive, archive_sha, tree

    @staticmethod
    def write_json(path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()

    def atomic_write_gate_state(
        self,
        path: Path,
        *,
        state: str,
        cutover_id: str,
        irreversible_at: str | None = None,
    ) -> GateState:
        if state not in {"acceptance", "drain", "open"}:
            self._fail(f"拒绝写入未知 API gate state: {state}")
        if state == "open" and not irreversible_at:
            self._fail("open API gate 必须同时记录 irreversible_at")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink():
            self._fail("API gate 目录不得是符号链接")
        payload = GateState(schema_version=1, state=state, cutover_id=cutover_id, updated_at=self._utc_now())
        if irreversible_at:
            payload["irreversible_at"] = irreversible_at
        data = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode()
        temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()
        return payload

    def read_gate_state(self, manifest: CutoverManifest) -> GateState | None:
        raw_path = manifest.get("api_gate_state_file")
        if raw_path is None:
            return None
        path = Path(str(raw_path))
        expected_parent = Path(str(manifest.get("snapshot_archive"))).parent / "api-gate"
        if path != expected_parent / "api-gate-state.json" or path.is_symlink() or not path.is_file():
            self._fail("API gate-state 文件不在 manifest 专用外置目录")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise self._error_type("API gate-state 文件无法读取") from exc
        if not self._valid_gate_payload(payload, manifest):
            self._fail("API gate-state 与 manifest 不一致")
        return cast(GateState, payload)

    @staticmethod
    def _valid_gate_payload(payload: object, manifest: CutoverManifest) -> bool:
        if not isinstance(payload, dict):
            return False
        state = payload.get("state")
        expected_keys = {"schema_version", "state", "cutover_id", "updated_at"}
        if state == "open":
            expected_keys.add("irreversible_at")
        return (
            set(payload) == expected_keys
            and payload.get("schema_version") == 1
            and payload.get("cutover_id") == manifest.get("cutover_id")
            and state in {"acceptance", "drain", "open"}
            and isinstance(payload.get("updated_at"), str)
            and (state != "open" or isinstance(payload.get("irreversible_at"), str))
        )

    def require_restore_allowed(self, manifest: CutoverManifest) -> None:
        gate_state = self.read_gate_state(manifest)
        if gate_state is not None and (gate_state.get("state") == "open" or gate_state.get("irreversible_at")):
            self._fail("API gate-state 已原子 open；旧快照恢复永久禁止")
        reversible_states = {
            "prepared",
            "acceptance_starting",
            "acceptance_failed",
            "acceptance_only",
            "production_drain_starting",
            "production_drain_failed",
            "production_drain_ready",
            "deletion_intent_ready",
            "restore_start_failed",
        }
        if manifest.get("state") not in reversible_states or manifest.get("irreversible") is not False:
            self._fail("已到不可逆点或 manifest 状态不允许恢复旧快照")

    def load_manifest(self, path: Path) -> CutoverManifest:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise self._error_type("cutover manifest 无法读取") from exc
        if not isinstance(payload, dict) or not self._valid_manifest_core(payload):
            self._fail("cutover manifest schema 不受支持")
        return cast(CutoverManifest, payload)

    @staticmethod
    def _valid_manifest_core(payload: Mapping[object, object]) -> bool:
        strings_valid = all(isinstance(payload.get(key), str) and bool(payload.get(key)) for key in REQUIRED_MANIFEST_STRING_FIELDS)
        integers_valid = all(type(payload.get(key)) is int for key in ("runtime_root_device", "runtime_root_inode"))
        return (
            payload.get("schema_version") == 1
            and strings_valid
            and integers_valid
            and isinstance(payload.get("irreversible"), bool)
            and isinstance(payload.get("active_counts"), dict)
            and is_tree_entry_list(payload.get("snapshot_entries"))
            and is_rollback_image_list(payload.get("rollback_images"))
        )

    def verify_token(self, manifest: CutoverManifest, field: str, token: str) -> None:
        actual = hashlib.sha256(token.encode()).hexdigest()
        if not secrets.compare_digest(str(manifest.get(field) or ""), actual):
            self._fail("cutover confirmation token 不匹配")

    def verify_manifest_target(
        self,
        manifest: CutoverManifest,
        runtime_root: Path,
        env_file: Path,
        *,
        verify_current_source: bool = True,
    ) -> None:
        if manifest.get("runtime_root") != runtime_root.as_posix():
            self._fail("manifest Runtime root 与当前目标不一致")
        metadata = runtime_root.stat()
        if manifest.get("runtime_root_device") != metadata.st_dev or manifest.get("runtime_root_inode") != metadata.st_ino:
            self._fail("Runtime root inode/device 已变化，拒绝清空")
        if manifest.get("source_env_sha256") != self._sha256_file(env_file):
            self._fail("源 env 在 prepare 后发生变化")
        if verify_current_source and manifest.get("source_artifact_sha256") != self._source_artifact_sha256():
            self._fail("代码制品在 prepare 后发生变化")
        archive = Path(str(manifest.get("snapshot_archive") or ""))
        if not archive.is_file() or self._sha256_file(archive) != manifest.get("snapshot_sha256"):
            self._fail("外置快照或 SHA-256 已变化")
        env_snapshot = Path(str(manifest.get("env_snapshot") or ""))
        if not env_snapshot.is_file() or env_snapshot.parent != archive.parent:
            self._fail("外置 env snapshot 或 SHA-256 已变化")
        if self._sha256_file(env_snapshot) != manifest.get("env_snapshot_sha256"):
            self._fail("外置 env snapshot 或 SHA-256 已变化")
        self.verify_rollback_bundle(manifest)

    def clear_runtime_root(self, runtime_root: Path) -> None:
        if runtime_root.is_symlink() or not runtime_root.is_dir():
            self._fail("精确清空前 Runtime root 必须仍是真实目录")
        for entry in list(runtime_root.iterdir()):
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
            elif entry.is_dir():
                shutil.rmtree(entry)
            else:
                self._fail(f"拒绝删除特殊 Runtime entry: {entry.name}")
        if any(runtime_root.iterdir()):
            self._fail("Runtime root 未被精确清空")

    def capture_rollback_bundle(
        self,
        env_file: Path,
        backup_root: Path,
        rollback_compose_file: Path,
        *,
        run_command: Callable[..., str],
    ) -> RollbackBundle:
        """Persist the exact stopped stack config and images required by restore."""

        if rollback_compose_file.is_symlink() or not rollback_compose_file.is_file():
            self._fail("--rollback-compose-file 必须是仍在运行的旧栈 Compose 普通文件")
        rollback_compose_file = rollback_compose_file.resolve()
        resolved_compose = backup_root / "rollback-compose.resolved.yml"
        resolved_compose.write_text(
            run_command(
                [*self._compose_base(env_file, rollback_compose_file), "config"],
                "旧栈 Compose 解析",
                capture=True,
            ),
            encoding="utf-8",
        )
        resolved_compose.chmod(0o600)
        raw_images = run_command(
            [*self._compose_base(env_file, rollback_compose_file), "config", "--images"],
            "旧栈 image 引用解析",
            capture=True,
        )
        image_references = tuple(dict.fromkeys(line.strip() for line in raw_images.splitlines() if line.strip()))
        if not image_references:
            self._fail("旧栈 Compose 未解析出任何可恢复 image")
        images = [self._inspect_rollback_image(reference, run_command) for reference in image_references]
        inventory = backup_root / "rollback-images.json"
        self.write_json(inventory, {"schema_version": 1, "images": images})
        image_archive = backup_root / "rollback-images.tar"
        run_command(["docker", "image", "save", "--output", str(image_archive), *image_references], "旧 image 导出")
        self._verify_image_archive(image_archive)
        return RollbackBundle(
            rollback_compose_source=rollback_compose_file.as_posix(),
            rollback_compose_source_sha256=self._sha256_file(rollback_compose_file),
            rollback_compose=resolved_compose.as_posix(),
            rollback_compose_sha256=self._sha256_file(resolved_compose),
            rollback_image_inventory=inventory.as_posix(),
            rollback_image_inventory_sha256=self._sha256_file(inventory),
            rollback_image_archive=image_archive.as_posix(),
            rollback_image_archive_sha256=self._sha256_file(image_archive),
            rollback_image_restore_drill="passed",
            rollback_images=images,
        )

    def _inspect_rollback_image(self, reference: str, run_command: Callable[..., str]) -> RollbackImage:
        try:
            inspected = json.loads(run_command(["docker", "image", "inspect", reference], f"旧 image 不可用: {reference}", capture=True))
        except json.JSONDecodeError as exc:
            raise self._error_type(f"旧 image inspect 不是 JSON: {reference}") from exc
        if not isinstance(inspected, list) or not inspected or not isinstance(inspected[0], dict):
            self._fail(f"旧 image inspect 结构无效: {reference}")
        image_id = inspected[0].get("Id")
        if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
            self._fail(f"旧 image 缺少精确 content digest: {reference}")
        return RollbackImage(
            reference=reference,
            id=image_id,
            repo_digests=inspected[0].get("RepoDigests") or [],
        )

    def _verify_image_archive(self, image_archive: Path) -> None:
        if not image_archive.is_file() or image_archive.stat().st_size == 0:
            self._fail("旧 image archive 未生成")
        image_archive.chmod(0o600)
        with tarfile.open(image_archive, "r") as stream:
            members = stream.getmembers()
            if not members or any(Path(item.name).is_absolute() or ".." in Path(item.name).parts for item in members):
                self._fail("旧 image archive restore drill 失败")

    def verify_rollback_bundle(self, manifest: CutoverManifest) -> None:
        for path_key, digest_key in (
            ("rollback_compose", "rollback_compose_sha256"),
            ("rollback_image_inventory", "rollback_image_inventory_sha256"),
            ("rollback_image_archive", "rollback_image_archive_sha256"),
        ):
            path = Path(str(manifest.get(path_key) or ""))
            if not path.is_file() or path.parent != Path(str(manifest.get("snapshot_archive"))).parent:
                self._fail(f"rollback bundle 缺少外置文件: {path_key}")
            if self._sha256_file(path) != manifest.get(digest_key):
                self._fail(f"rollback bundle SHA-256 已变化: {path_key}")

    @staticmethod
    def append_env_overrides(
        source_env: Path,
        destination: Path,
        *,
        label: str,
        overrides: Mapping[str, str],
    ) -> None:
        source_text = source_env.read_text(encoding="utf-8")
        rendered = "\n".join(f"{key}={value}" for key, value in overrides.items())
        destination.write_text(source_text.rstrip() + f"\n\n# AgentScope cutover {label} overrides\n{rendered}\n", encoding="utf-8")
        destination.chmod(0o600)

    def append_acceptance_overrides(
        self,
        source_env: Path,
        destination: Path,
        *,
        identity: str,
        gate_state_file: Path,
    ) -> str:
        env = self._load_env_file(source_env)
        api_key = secrets.token_urlsafe(36)
        host_port = env.get("HOST_PORT", "50400").rsplit(":", 1)[-1]
        if not host_port.isdecimal():
            self._fail("HOST_PORT 必须可转换为本机验收端口")
        overrides = {
            "API_KEY": api_key,
            "FRONTEND_RUNTIME_API_KEY": api_key,
            "API_BIND_IP": "127.0.0.1",
            "HOST_PORT": host_port,
            "FRONTEND_BIND_IP": "127.0.0.1",
            "LANGFUSE_BIND_IP": "127.0.0.1",
            "AGENTGOV_CUTOVER_ACCEPTANCE_ONLY": "1",
            "AGENTGOV_API_MODE": "acceptance",
            "AGENTGOV_ACCEPTANCE_IDENTITY": identity,
            "AGENTGOV_ACCEPTANCE_API_KEY": api_key,
            "AGENTGOV_API_GATE_STATE_DIR_HOST": gate_state_file.parent.as_posix(),
            "AGENTGOV_API_GATE_STATE_FILE": "/run/agentgov/api-gate/api-gate-state.json",
        }
        self.append_env_overrides(source_env, destination, label="acceptance-only", overrides=overrides)
        return api_key

    def write_production_drain_env(
        self,
        source_env: Path,
        destination: Path,
        *,
        gate_state_file: Path,
        cutover_id: str,
    ) -> None:
        self.append_env_overrides(
            source_env,
            destination,
            label="production-drain",
            overrides={
                "AGENTGOV_API_MODE": "drain",
                "AGENTGOV_ACCEPTANCE_IDENTITY": cutover_id,
                "AGENTGOV_ACCEPTANCE_API_KEY": "",
                "AGENTGOV_CUTOVER_ACCEPTANCE_ONLY": "0",
                "AGENTGOV_API_GATE_STATE_DIR_HOST": gate_state_file.parent.as_posix(),
                "AGENTGOV_API_GATE_STATE_FILE": "/run/agentgov/api-gate/api-gate-state.json",
            },
        )

    def run(self, command: list[str], label: str, *, capture: bool = False) -> str:
        try:
            result = subprocess.run(
                command,
                cwd=self._repo_root,
                check=True,
                capture_output=capture,
                text=capture,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise self._error_type(f"{label}失败") from exc
        return result.stdout if capture else ""

    def bootstrap_and_force_recreate(self, runtime_root: Path, env_file: Path) -> None:
        self.run(
            [
                sys.executable,
                str(self._repo_root / "scripts/bootstrap_runtime_volume.py"),
                "--env-file",
                str(env_file),
                "--runtime-root",
                str(runtime_root),
                "--quiet",
            ],
            "空卷 bootstrap",
        )
        self.run(
            [
                "make",
                "--no-print-directory",
                "all-up",
                f"COMPOSE_ENV_FILE={env_file}",
                "COMPOSE_UP_FLAGS=--force-recreate --no-build --pull never",
                f"PYTHON_RUN={sys.executable}",
            ],
            "AgentScope 新栈 force-recreate",
        )

    def stop_compose_project(self, env_file: Path) -> None:
        self.run(
            [*self._compose_base(env_file, self._compose_file), "--profile", "langfuse", "down", "--remove-orphans"],
            "切换项目停机",
        )

    def restore_rollback_images_and_start(self, manifest: CutoverManifest) -> None:
        image_archive = Path(str(manifest["rollback_image_archive"]))
        self.run(["docker", "image", "load", "--input", str(image_archive)], "旧 image 恢复")
        images = manifest.get("rollback_images")
        if not isinstance(images, list) or not images:
            self._fail("manifest 缺少旧 image digest 清单")
        for item in images:
            if not isinstance(item, dict):
                self._fail("旧 image digest 清单结构无效")
            reference = item.get("reference")
            expected_id = item.get("id")
            if not isinstance(reference, str) or not isinstance(expected_id, str):
                self._fail("旧 image reference/digest 无效")
            inspected = self.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
                f"旧 image digest 恢复校验: {reference}",
                capture=True,
            ).strip()
            if inspected != expected_id:
                self._fail(f"旧 image digest 恢复后不匹配: {reference}")
        env_snapshot = Path(str(manifest["env_snapshot"]))
        rollback_compose = Path(str(manifest["rollback_compose"]))
        self.run(
            [
                *self._compose_base(env_snapshot, rollback_compose),
                "--profile",
                "langfuse",
                "up",
                "-d",
                "--wait",
                "--remove-orphans",
                "--no-build",
                "--pull",
                "never",
            ],
            "旧栈精确 image 重启",
        )
        self.wait_ready(env_snapshot)

    def acceptance_artifacts(
        self,
        runtime_root: Path,
        base_url: str,
        api_key: str,
        acceptance_env: Path,
        manifest: CutoverManifest,
    ) -> JsonObject:
        tree_json = json.dumps(self.tree_content_projection(self.tree_manifest(runtime_root)), sort_keys=True).encode()
        return {
            "schema_epoch": self._schema_epoch,
            "openapi_sha256": self.openapi_sha(base_url, api_key),
            "harness_sha256": hashlib.sha256(tree_json).hexdigest(),
            "agentscope_version": self.agent_scope_version(),
            "source_artifact_sha256": manifest["source_artifact_sha256"],
            "snapshot_sha256": manifest["snapshot_sha256"],
            "rollback_image_inventory_sha256": manifest["rollback_image_inventory_sha256"],
            "rollback_image_archive_sha256": manifest["rollback_image_archive_sha256"],
            "rollback_compose_sha256": manifest["rollback_compose_sha256"],
            "acceptance_env_sha256": self._sha256_file(acceptance_env),
            "acceptance_identity": manifest["cutover_id"],
            "image_ids": self.core_image_ids(acceptance_env),
        }

    def core_image_ids(self, env_file: Path) -> CoreImageIds:
        """Return exact immutable image IDs for the three production services."""

        image_ids: dict[str, str] = {}
        for service in ("agentscope-runtime", "agent-gov-api", "agent-gov-ui"):
            container_id = self.run(
                [*self._compose_base(env_file, self._compose_file), "ps", "-q", service],
                f"定位验收容器: {service}",
                capture=True,
            ).strip()
            if not container_id or "\n" in container_id:
                self._fail(f"验收服务容器身份不唯一: {service}")
            image_id = self.run(
                ["docker", "container", "inspect", "--format", "{{.Image}}", container_id],
                f"读取验收 image ID: {service}",
                capture=True,
            ).strip()
            if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
                self._fail(f"验收服务 image ID 无效: {service}")
            image_ids[service] = image_id
        return cast(CoreImageIds, image_ids)

    def start_production_drain(
        self,
        *,
        manifest_path: Path,
        manifest: CutoverManifest,
        env_file: Path,
        runtime_root: Path,
        evidence: FinalEvidence,
        evidence_sha: str,
    ) -> ProductionDrain:
        production_drain_env = manifest_path.parent / "production-drain.env"
        gate_state = self.read_gate_state(manifest)
        if gate_state is None or gate_state.get("state") not in {"acceptance", "drain"} or gate_state.get("irreversible_at"):
            self._fail("finalize 前 API gate 必须仍为可恢复 acceptance/drain")
        raw_gate_state_file = manifest.get("api_gate_state_file")
        if not isinstance(raw_gate_state_file, str) or not raw_gate_state_file:
            self._fail("finalize manifest 缺少 API gate-state 路径")
        gate_state_file = Path(raw_gate_state_file)
        self.atomic_write_gate_state(gate_state_file, state="drain", cutover_id=str(manifest["cutover_id"]))
        self.write_production_drain_env(
            env_file,
            production_drain_env,
            gate_state_file=gate_state_file,
            cutover_id=str(manifest["cutover_id"]),
        )
        manifest.update(
            state="production_drain_starting",
            production_drain_env=production_drain_env.as_posix(),
            production_drain_env_sha256=self._sha256_file(production_drain_env),
            irreversible=False,
        )
        self.write_json(manifest_path, manifest)
        try:
            self.bootstrap_and_force_recreate(runtime_root, production_drain_env)
            base_url, api_key = self.wait_ready(production_drain_env)
        except self._error_type as exc:
            manifest.update(state="production_drain_failed", production_drain_failed_at=self._utc_now(), irreversible=False)
            self.write_json(manifest_path, manifest)
            raise self._error_type("生产 drain 栈启动失败；尚未打开 mutation gate，可使用 restore token 自动恢复旧栈") from exc
        openapi_sha256 = self.openapi_sha(base_url, api_key)
        image_ids = self.core_image_ids(production_drain_env)
        acceptance = manifest.get("acceptance_artifacts")
        if not isinstance(acceptance, dict):
            self._fail("manifest 缺少 acceptance artifacts")
        if acceptance.get("openapi_sha256") != openapi_sha256:
            self._fail("production drain OpenAPI 与 acceptance-only 不一致")
        if acceptance.get("image_ids") != image_ids:
            self._fail("production drain image IDs 与 acceptance-only 不一致")
        artifacts = ProductionDrainArtifacts(
            evidence_sha256=evidence_sha,
            evidence=evidence,
            openapi_sha256=openapi_sha256,
            image_ids=image_ids,
            api_mode="drain",
        )
        db_path = self._database_path(runtime_root, env_file)
        self.record_ledger(
            db_path,
            cutover_id=f"{manifest['cutover_id']}:production-drain",
            phase="production_drain",
            status="ready_to_open",
            detail="Production bind and key are ready, but the global mutation gate remains drain and rollback is allowed.",
            artifacts=artifacts,
        )
        manifest.update(
            state="production_drain_ready",
            production_drain_ready_at=self._utc_now(),
            production_drain_artifacts=cast(JsonObject, artifacts),
            irreversible=False,
        )
        self.write_json(manifest_path, manifest)
        return ProductionDrain(db_path, gate_state_file, evidence_sha, artifacts)

    def wait_ready(self, env_file: Path) -> tuple[str, str]:
        env = self._load_env_file(env_file)
        port = env.get("HOST_PORT", "50400").rsplit(":", 1)[-1]
        api_key = env.get("API_KEY", "")
        base_url = f"http://127.0.0.1:{port}"
        error: Exception | None = None
        for _ in range(60):
            try:
                request = Request(f"{base_url}/health/ready", headers={"Authorization": f"Bearer {api_key}"})
                with urlopen(request, timeout=3) as response:
                    if response.status == 200:
                        return base_url, api_key
            except Exception as exc:  # noqa: BLE001 - 返回最后一次本地 readiness 失败。
                error = exc
            time.sleep(1)
        self._fail(f"AgentScope 新栈 readiness 未通过: {error!r}")

    @staticmethod
    def openapi_sha(base_url: str, api_key: str) -> str:
        request = Request(f"{base_url}/openapi.json", headers={"Authorization": f"Bearer {api_key}"})
        with urlopen(request, timeout=10) as response:
            return hashlib.sha256(response.read()).hexdigest()

    def agent_scope_version(self) -> str:
        requirements = (self._repo_root / "agentscope_runtime/requirements.txt").read_text(encoding="utf-8")
        match = re.search(r"^agentscope(?:\[[^]]+\])?==([^\s#]+)", requirements, flags=re.MULTILINE)
        return match.group(1) if match else "unknown"

    def record_ledger(
        self,
        db_path: Path,
        *,
        cutover_id: str,
        phase: str,
        status: str,
        detail: str,
        artifacts: Mapping[str, object],
    ) -> None:
        epoch = self._classify_runtime_epoch(db_path)
        if epoch["classification"] != "agentscope":
            self._fail("新栈未创建精确 AgentScope schema epoch，不能记录 ledger")
        serialized = json.dumps(artifacts, sort_keys=True)
        expected = (phase, status, detail, serialized)
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO runtime_cutover_ledger (cutover_id, phase, status, detail, artifacts_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (cutover_id, *expected, self._utc_now()),
            )
            existing = connection.execute(
                "SELECT phase, status, detail, artifacts_json FROM runtime_cutover_ledger WHERE cutover_id = ?",
                (cutover_id,),
            ).fetchone()
            if existing is None or tuple(existing) != expected:
                self._fail("cutover ledger idempotency key 与既有内容冲突")
            connection.commit()
