from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from app.runtime.errors import FeedbackStoreError
from app.runtime.execution_targets import WorkspaceExecutionTargetPolicy

# 写入结构化配置文件的安全护栏回调：(target_path, new_bytes, original_bytes) -> None，违规抛错。
ContentGuard = Callable[..., None]


class WorkspaceExecutionApplyError(FeedbackStoreError):
    """Route-safe error raised while applying generated operations to a workspace."""

    def __init__(self, detail: str, *, status_code: int = 409) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.error_code = "EXECUTION_APPLICATION_ERROR" if status_code != 404 else "NOT_FOUND"


class WorkspaceExecutionApplier:
    """Applies execution operations to an explicit workspace path without old task/batch storage."""

    def __init__(self, *, max_write_bytes: int = 500_000) -> None:
        self.max_write_bytes = max_write_bytes

    def apply_execution_operations(
        self,
        operations: list[object],
        *,
        workspace_dir: Path,
        target_policy: WorkspaceExecutionTargetPolicy | None = None,
        content_guard: ContentGuard | None = None,
        allowed_targets: set[str] | None = None,
    ) -> None:
        if not operations:
            raise WorkspaceExecutionApplyError("Execution plan has no operations")
        # allowlist 强制：受治理 apply 只允许写显式可编辑目标集（防写 settings.local.json / hooks / .env / agents 绕过护栏）。
        allow_norm = {Path(t).as_posix() for t in allowed_targets} if allowed_targets is not None else None
        originals: dict[Path, bytes | None] = {}
        writes: list[tuple[Path, bytes]] = []
        for item in operations:
            if not isinstance(item, dict):
                raise WorkspaceExecutionApplyError("Execution operation must be an object")
            op = str(item.get("operation") or "")
            target_path = str(item.get("path") or "")
            if allow_norm is not None and Path(target_path).as_posix() not in allow_norm:
                raise WorkspaceExecutionApplyError(f"Target path is not in the editable allowlist: {target_path}")
            dest = self.safe_workspace_target(target_path, workspace_dir=workspace_dir)
            if target_policy is not None and not target_policy.target_allowed(target_path):
                raise WorkspaceExecutionApplyError(f"Target path is not allowed: {target_path}")
            if op == "noop":
                continue
            if dest not in originals:
                originals[dest] = dest.read_bytes() if dest.exists() else None
            data = self._operation_bytes(item, op=op, target_path=target_path, original=originals[dest])
            if len(data) > self.max_write_bytes:
                raise WorkspaceExecutionApplyError(f"Execution write exceeds {self.max_write_bytes} bytes: {target_path}")
            if content_guard is not None:
                # 结构化配置合法性 + 权限升级防护；违规抛错 → 上层 abandon change set + 回退（尚未落盘）。
                content_guard(target_path=target_path, new_bytes=data, original_bytes=originals[dest])
            writes.append((dest, data))
        if not writes:
            raise WorkspaceExecutionApplyError("Execution plan has no writable operations")
        self._write_with_rollback(writes, originals)

    def _operation_bytes(self, item: dict, *, op: str, target_path: str, original: bytes | None) -> bytes:
        expected_sha = str(item.get("expected_sha256") or "").strip()
        if op in {"append_text", "replace_file"} and not expected_sha:
            raise WorkspaceExecutionApplyError(f"{op} operation requires expected_sha256: {target_path}")
        if expected_sha and original is not None:
            actual_sha = hashlib.sha256(original).hexdigest()
            if actual_sha != expected_sha:
                raise WorkspaceExecutionApplyError(f"Target file changed before apply: {target_path}")
        if op == "append_text":
            append_text = item.get("append_text")
            if not isinstance(append_text, str):
                raise WorkspaceExecutionApplyError(f"append_text operation requires append_text: {target_path}")
            if original is None:
                raise WorkspaceExecutionApplyError(f"append_text target does not exist: {target_path}")
            try:
                return (original.decode("utf-8") + append_text).encode("utf-8")
            except UnicodeDecodeError as exc:
                raise WorkspaceExecutionApplyError(f"append_text target is not UTF-8 text: {target_path}") from exc
        if op in {"replace_file", "create_file"}:
            content = item.get("content")
            if not isinstance(content, str):
                raise WorkspaceExecutionApplyError(f"{op} operation requires content: {target_path}")
            if op == "create_file" and original is not None:
                raise WorkspaceExecutionApplyError(f"create_file target already exists: {target_path}")
            return content.encode("utf-8")
        raise WorkspaceExecutionApplyError(f"Unsupported operation: {op}")

    def _write_with_rollback(self, writes: list[tuple[Path, bytes]], originals: dict[Path, bytes | None]) -> None:
        try:
            for dest, data in writes:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
        except Exception as exc:
            rollback_errors: list[str] = []
            for dest, data in originals.items():
                try:
                    if data is None:
                        dest.unlink(missing_ok=True)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(data)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{dest}: {rollback_exc}")
            suffix = f"; rollback errors: {'; '.join(rollback_errors)}" if rollback_errors else ""
            raise WorkspaceExecutionApplyError(f"Execution apply failed and was rolled back: {exc}{suffix}") from exc

    @staticmethod
    def safe_workspace_target(target_path: str, *, workspace_dir: Path) -> Path:
        if not target_path:
            raise WorkspaceExecutionApplyError("Target path is required")
        rel = Path(target_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise WorkspaceExecutionApplyError(f"Unsafe target path: {target_path}")
        try:
            base = workspace_dir.resolve(strict=True)
            dest = (base / rel).resolve(strict=False)
            dest.relative_to(base)
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspaceExecutionApplyError(f"Target path escapes workspace: {target_path}") from exc
        return dest
