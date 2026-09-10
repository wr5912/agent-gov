"""为治理 run 提供受边界约束的只读 Harness 证据工具。"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import AsyncGenerator, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from agentscope.middleware import MiddlewareBase
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, ToolBase

_EVIDENCE_ROOT_METADATA = "agentgov_governed_evidence_root"
_LOGICAL_ROOT = re.compile(
    r"/business-agents/(?P<agent_id>[A-Za-z0-9][A-Za-z0-9._-]{0,126})/workspace",
)
_SAFE_TOP_LEVEL = {
    "AGENT.md",
    "README.md",
    "agent.yaml",
    "conversion-report.json",
}
_SAFE_DIRECTORIES = {"mcp", "skills", "subagents", "tests"}
_SAFE_SUFFIXES = {".json", ".md", ".py", ".toml", ".txt", ".yaml", ".yml"}
_SENSITIVE_FRAGMENTS = {"credential", "secret", "token"}
_MAX_SCAN_ENTRIES = 10_000
_MAX_LISTED_FILES = 1_000
_MAX_FILE_BYTES = 256 * 1024


@dataclass(frozen=True)
class _EvidenceBinding:
    logical_root: PurePosixPath
    host_root: Path


class GovernedHarnessEvidenceMiddleware(MiddlewareBase):
    """只在可信 user Message 声明的单一业务 Harness 根内读取证据。"""

    def __init__(self, business_agents_root: Path) -> None:
        self._business_agents_root = business_agents_root
        self._active_binding: ContextVar[_EvidenceBinding | None] = ContextVar(
            f"agentgov_harness_evidence_{id(self)}",
            default=None,
        )

    async def on_reply(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        del agent
        binding = self._binding_from_inputs(input_kwargs.get("inputs"))
        token = self._active_binding.set(binding)
        try:
            async for item in next_handler(**input_kwargs):
                yield item
        finally:
            self._active_binding.reset(token)

    async def list_tools(self) -> list[ToolBase]:
        allow = PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Read-only access to the run-scoped governed Harness evidence root",
            decision_reason="agentgov.governed_harness_evidence",
        )
        return [
            FunctionTool(
                self._list_harness_files,
                name="HarnessList",
                description=("列出本次治理 run 唯一目标业务 Agent 的非敏感 Harness 文件。根目录由 AgentGov 绑定，不接受调用方指定。"),
                is_read_only=True,
                permission=allow,
            ),
            FunctionTool(
                self._read_harness_file,
                name="HarnessRead",
                description=("读取 HarnessList 返回的一个 UTF-8 Harness 文件；path 可为列表中的绝对逻辑路径或相对目标 workspace 的路径。"),
                is_read_only=True,
                permission=allow,
            ),
        ]

    def _list_harness_files(self) -> dict[str, object]:
        binding = self._require_binding()
        files: list[str] = []
        scanned = 0
        for current, directories, names in os.walk(binding.host_root, followlinks=False):
            current_path = Path(current)
            for name in (*directories, *names):
                scanned += 1
                if scanned > _MAX_SCAN_ENTRIES:
                    raise ValueError("Governed Harness contains too many filesystem entries")
                if (current_path / name).is_symlink():
                    raise ValueError("Governed Harness must not contain symbolic links")
            directories[:] = [name for name in directories if not name.startswith(".")]
            for name in names:
                path = current_path / name
                relative = path.relative_to(binding.host_root)
                if not self._is_safe_file(relative, path):
                    continue
                files.append((binding.logical_root / PurePosixPath(relative.as_posix())).as_posix())
                if len(files) > _MAX_LISTED_FILES:
                    raise ValueError("Governed Harness exposes too many readable files")
        return {
            "root": binding.logical_root.as_posix(),
            "files": sorted(files),
        }

    def _read_harness_file(self, path: str) -> dict[str, object]:
        binding = self._require_binding()
        relative = self._relative_path(binding, path)
        host_path = binding.host_root.joinpath(*relative.parts)
        if not self._is_safe_file(Path(relative.as_posix()), host_path):
            raise ValueError("Requested path is not an allowed non-sensitive Harness file")
        raw = host_path.read_bytes()
        if len(raw) > _MAX_FILE_BYTES:
            raise ValueError("Requested Harness file exceeds the read limit")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Requested Harness file is not UTF-8 text") from exc
        logical_path = (binding.logical_root / relative).as_posix()
        return {
            "path": logical_path,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "content": content,
        }

    def _binding_from_inputs(self, inputs: object) -> _EvidenceBinding | None:
        values = inputs if isinstance(inputs, list) else [inputs]
        roots: set[str] = set()
        for value in values:
            metadata = self._metadata(value)
            root = metadata.get(_EVIDENCE_ROOT_METADATA)
            if isinstance(root, str) and root:
                roots.add(root)
        if not roots:
            return None
        if len(roots) != 1:
            raise ValueError("A governed run cannot bind multiple Harness evidence roots")
        logical = roots.pop()
        match = _LOGICAL_ROOT.fullmatch(logical)
        if match is None:
            raise ValueError("Governed Harness evidence root is invalid")
        business_root = self._business_agents_root.resolve(strict=True)
        if self._business_agents_root.is_symlink() or not business_root.is_dir():
            raise ValueError("Business Agent source root is unsafe")
        host_root = business_root / match.group("agent_id") / "workspace"
        if host_root.is_symlink() or not host_root.is_dir():
            raise ValueError("Governed Harness evidence root does not exist safely")
        resolved = host_root.resolve(strict=True)
        if resolved != host_root:
            raise ValueError("Governed Harness evidence root must not traverse links")
        return _EvidenceBinding(PurePosixPath(logical), resolved)

    @staticmethod
    def _metadata(value: object) -> Mapping[str, object]:
        if isinstance(value, Mapping):
            metadata = value.get("metadata")
        else:
            metadata = getattr(value, "metadata", None)
        return metadata if isinstance(metadata, Mapping) else {}

    def _require_binding(self) -> _EvidenceBinding:
        binding = self._active_binding.get()
        if binding is None:
            raise ValueError("This run has no governed Harness evidence binding")
        return binding

    @staticmethod
    def _relative_path(binding: _EvidenceBinding, value: str) -> PurePosixPath:
        if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
            raise ValueError("Harness path is invalid")
        candidate = PurePosixPath(value)
        if candidate.is_absolute():
            try:
                candidate = candidate.relative_to(binding.logical_root)
            except ValueError as exc:
                raise ValueError("Harness path is outside the governed evidence root") from exc
        if not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
            raise ValueError("Harness path is invalid")
        return candidate

    @staticmethod
    def _is_safe_file(relative: Path, path: Path) -> bool:
        parts = relative.parts
        if not parts or path.is_symlink() or not path.is_file():
            return False
        lowered = [part.lower() for part in parts]
        if any(part.startswith(".") for part in parts):
            return False
        if any(fragment in part for part in lowered for fragment in _SENSITIVE_FRAGMENTS):
            return False
        if len(parts) == 1:
            return parts[0] in _SAFE_TOP_LEVEL
        return parts[0] in _SAFE_DIRECTORIES and path.suffix.lower() in _SAFE_SUFFIXES


def governed_evidence_metadata_key() -> str:
    """供 AgentGov Backend 与 Runtime 共享保留字段名。"""

    return _EVIDENCE_ROOT_METADATA
