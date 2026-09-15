"""将版本化 AgentGov Harness 复制为 Runtime 可写工作区。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar

from agentgov_agentscope_contract import version_workspace_id
from agentgov_harness_digest import harness_content_digest as harness_digest
from agentscope.app import SubAgentTemplate
from agentscope.app.storage import StorageBase
from agentscope.app.workspace_manager import WorkspaceManagerBase
from agentscope.mcp import MCPClient
from agentscope.workspace import BubblewrapWorkspace

from .local_workspace import AgentGovLocalWorkspace
from .mcp_config_validation import (
    HTTP_HEADER_NAME,
    explicit_mcp_tools,
    forbidden_mcp_header,
    mcp_env_prefix,
    validated_http_mcp_config,
)
from .mcp_resource_middleware import MCPResourcePolicy, parse_mcp_resource_policy
from .offline_gateway import WorkspacePreparation, validate_runtime_state_links
from .reference_materialization import materialize_runtime_references, remove_private_staging_tree
from .types import JsonObject
from .workspace_reference_fence import (
    NativeSessionWorkspaceReferences,
    SessionWorkspaceReferenceFence,
    WorkspaceQuarantineStateError,
)
from .workspace_runtime_helpers import (
    load_network_hosts,
    register_subagent_templates,
    sandbox_proxy_env,
    validate_live_mcp_tools,
)

_SAFE_AGENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}")
_HARNESS_DIGEST = re.compile(r"[0-9a-f]{64}")
_MARKER = ".agentgov-runtime-workspace.json"
_REPORT = "conversion-report.json"
_PUBLISHED_SNAPSHOT_MARKER = "snapshot.json"
_STATE_DIR = ".agentgov-runtime-state"
_CACHE_DIR = ".agentgov-runtime-cache"
_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_REFERENCE_PATH = re.compile(r"mcp_config(?:\.[A-Za-z0-9_-]+)+")
_TaskResult = TypeVar("_TaskResult")
logger = logging.getLogger(__name__)


class AgentGovWorkspaceManager(WorkspaceManagerBase):
    """Use immutable Harness sources and never run inside the live workspace."""

    def __init__(
        self,
        *,
        business_agents_root: Path,
        candidates_root: Path,
        workspaces_root: Path,
        environ: Mapping[str, str] | None = None,
        require_read_only_sources: bool = False,
        subagent_templates: Mapping[str, SubAgentTemplate] | None = None,
        workspace_reference_fence: SessionWorkspaceReferenceFence | None = None,
    ) -> None:
        super().__init__()
        self._business_agents_root = business_agents_root
        self._candidates_root = candidates_root
        self._workspaces_root = workspaces_root
        self._environ = os.environ if environ is None else environ
        self._require_read_only_sources = require_read_only_sources
        self._cache: dict[str, AgentGovLocalWorkspace] = {}
        self._session_workspaces: dict[tuple[str, str, str], str] = {}
        self._validated_mcp_bindings: set[tuple[str, str, str]] = set()
        self._subagent_templates = dict(subagent_templates or {})
        self._native_session_references = NativeSessionWorkspaceReferences(workspaces_root)
        self._workspace_reference_fence = workspace_reference_fence or SessionWorkspaceReferenceFence()
        self._lock = asyncio.Lock()

    def bind_storage(self, storage: StorageBase) -> None:
        """Bind AgentScope storage for durable Workspace reference checks."""

        super().bind_storage(storage)
        self._native_session_references.bind_storage(storage)
        bind_reservations = getattr(storage, "bind_workspace_reference_reservations", None)
        if callable(bind_reservations):
            bind_reservations(self._native_session_references)

    async def assign_workspace_id(
        self,
        *,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> str:
        """Fail closed: only the gateway knows the versioned Harness binding."""

        del user_id, agent_id, session_id
        raise ValueError("workspace_id is required for AgentGov Runtime sessions")

    async def get_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str | None = None,
    ) -> BubblewrapWorkspace:
        parsed_id, _, digest = self._parse_workspace_binding(workspace_id)
        stage = "reference_fence"
        logger.info("runtime_workspace_setup stage=begin source_kind=%s", "candidate" if parsed_id.startswith("candidate-") else "published")
        async with self._workspace_reference_fence.hold():
            try:
                self._workspace_reference_fence.require_writable(parsed_id)
                async with self._lock:
                    stage = "native_session_binding"
                    await self._native_session_references.require_binding(
                        user_id, agent_id, session_id, parsed_id,
                    )
                    preparation = WorkspacePreparation()
                    stage = "harness_materialization"
                    harness_root, state_root = await asyncio.to_thread(self._materialize, parsed_id)
                    stage = "subagent_templates"
                    register_subagent_templates(harness_root, digest, self._subagent_templates)
                    cached = self._cache.get(parsed_id)
                    created = cached is None
                    if cached is None:
                        stage = "mcp_bindings"
                        default_mcps, resource_policies = self._load_mcp_bindings(harness_root)
                        cached = AgentGovLocalWorkspace(
                            workspace_id=parsed_id,
                            host_workdir=str(state_root / _STATE_DIR),
                            host_cache_dir=str(state_root / _CACHE_DIR),
                            harness_root=harness_root,
                            expected_digest=digest,
                            sandbox_env=sandbox_proxy_env(self._environ),
                            default_mcps=default_mcps,
                            mcp_resource_policies=resource_policies,
                        )
                        stage = "initialize"
                        await preparation.initialize(cached)
                    elif Path(cached.harness_root) != harness_root:
                        raise ValueError("Harness source changed after workspace binding")
                    try:
                        binding = (parsed_id, agent_id, session_id)
                        stage = "mcp_roster"
                        if binding not in self._validated_mcp_bindings:
                            await preparation.validate_mcp(
                                validate_live_mcp_tools(cached, agent_id=agent_id, session_id=session_id),
                            )
                            self._validated_mcp_bindings.add(binding)
                        stage = "native_session_confirm"
                        if version_workspace_id(parsed_id) != parsed_id:
                            await self._native_session_references.confirm_reclaimable_binding(
                                user_id, agent_id, session_id, parsed_id,
                            )
                    except BaseException:
                        if created:
                            await cached.close()
                        raise
                    if created:
                        self._cache[parsed_id] = cached
                    self._session_workspaces[(user_id, agent_id, session_id)] = parsed_id
                    return cached
            except Exception as exc:
                logger.warning("runtime_workspace_setup stage=%s result=failed error_type=%s", stage, type(exc).__name__)
                raise

    async def close(self, workspace_id: str) -> None:
        async with self._lock:
            workspace = self._cache.get(workspace_id)
            if workspace is not None:
                await workspace.close()
                self._cache.pop(workspace_id, None)
            self._session_workspaces = {
                binding: bound_workspace_id for binding, bound_workspace_id in self._session_workspaces.items() if bound_workspace_id != workspace_id
            }
            self._validated_mcp_bindings = {binding for binding in self._validated_mcp_bindings if binding[0] != workspace_id}

    async def close_all(self) -> None:
        async with self._lock:
            workspaces = tuple(self._cache.values())
            self._cache.clear()
            self._session_workspaces.clear()
            self._validated_mcp_bindings.clear()
            await asyncio.gather(*(workspace.close() for workspace in workspaces))

    @property
    def workspaces_root(self) -> Path:
        """Return the configured Runtime state root, never a Harness source."""

        return self._workspaces_root

    async def snapshot_native_session_workspaces(
        self,
        user_id: str,
    ) -> dict[tuple[str, str], str]:
        """Read all publicly discoverable Session-to-Workspace bindings."""

        async with self._workspace_reference_fence.hold():
            async with self._lock:
                return await self._snapshot_native_session_workspaces_locked(user_id)

    async def reclaimable_workspace_ids(self) -> frozenset[str]:
        """Return only Workspaces created under the complete reference ledger."""

        return await self._native_session_references.reclaimable_workspace_ids()

    async def quarantine_session_workspace_if_unreferenced(
        self,
        user_id: str,
        workspace_id: str,
        *,
        ignored_bindings: frozenset[tuple[str, str]],
        quarantine: Callable[[str], Path],
        restore: Callable[[str], None],
    ) -> Path | None:
        """Close and atomically quarantine one proven-unreferenced Workspace.

        The rename callback runs while the manager lock excludes a concurrent
        ``get_workspace`` materialization. Recursive deletion happens later,
        outside this lock and outside AgentScope storage operations.
        """

        async with self._workspace_reference_fence.hold():
            async with self._lock:
                # Session upserts use the same outer fence, so one durable
                # check remains authoritative through close and rename.
                if await self._workspace_is_referenced_locked(user_id, workspace_id, ignored_bindings):
                    return None
                workspace = self._cache.get(workspace_id)
                close_cancelled = False
                if workspace is not None:
                    _, close_cancelled = await self._finish_task_despite_cancellation(
                        asyncio.create_task(workspace.close()),
                    )
                    self._cache.pop(workspace_id, None)
                    self._validated_mcp_bindings = {binding for binding in self._validated_mcp_bindings if binding[0] != workspace_id}
                if close_cancelled:
                    raise asyncio.CancelledError
                try:
                    tombstone, cancelled = await self._finish_task_despite_cancellation(
                        asyncio.create_task(asyncio.to_thread(quarantine, workspace_id)),
                    )
                except WorkspaceQuarantineStateError:
                    await self._restore_or_retire_locked(workspace_id, restore)
                    raise
                # A non-project StorageBase does not participate in the
                # reference fence. Preserve the public extension boundary by
                # checking once more after rename and restoring on a late
                # durable Session. Provisioned storage cannot race here.
                try:
                    referenced_after_quarantine = await self._workspace_is_referenced_locked(
                        user_id,
                        workspace_id,
                        ignored_bindings,
                    )
                except BaseException:
                    restore_cancelled = await self._restore_or_retire_locked(
                        workspace_id,
                        restore,
                    )
                    if restore_cancelled:
                        raise asyncio.CancelledError from None
                    raise
                if referenced_after_quarantine:
                    cancelled = await self._restore_or_retire_locked(workspace_id, restore) or cancelled
                    if cancelled:
                        raise asyncio.CancelledError
                    return None
                self._workspace_reference_fence.retire(workspace_id)
                self._session_workspaces = {
                    binding: bound_workspace_id for binding, bound_workspace_id in self._session_workspaces.items() if bound_workspace_id != workspace_id
                }
                self._validated_mcp_bindings = {binding for binding in self._validated_mcp_bindings if binding[0] != workspace_id}
                if cancelled:
                    raise asyncio.CancelledError
                return tombstone

    async def prepare_quarantined_workspace_finalization(
        self,
        user_id: str,
        workspace_id: str,
        restore: Callable[[str], None],
    ) -> bool:
        """Restore referenced state, otherwise retire it before deletion."""

        async with self._workspace_reference_fence.hold():
            async with self._lock:
                if await self._workspace_is_referenced_locked(user_id, workspace_id, frozenset()):
                    try:
                        _, cancelled = await self._finish_task_despite_cancellation(
                            asyncio.create_task(asyncio.to_thread(restore, workspace_id)),
                        )
                    except Exception:
                        self._workspace_reference_fence.retire(workspace_id)
                        raise
                    self._workspace_reference_fence.activate(workspace_id)
                    if cancelled:
                        raise asyncio.CancelledError
                    return False
                self._workspace_reference_fence.retire(workspace_id)
                return True

    async def clear_restored_record_if_referenced(
        self,
        user_id: str,
        workspace_id: str,
        validate: Callable[[str], None],
    ) -> bool:
        """Validate and reactivate a restored target that still has a Session."""

        async with self._workspace_reference_fence.hold():
            async with self._lock:
                if not await self._workspace_is_referenced_locked(user_id, workspace_id, frozenset()):
                    return False
                try:
                    _, cancelled = await self._finish_task_despite_cancellation(
                        asyncio.create_task(asyncio.to_thread(validate, workspace_id)),
                    )
                except Exception:
                    self._workspace_reference_fence.retire(workspace_id)
                    raise
                self._workspace_reference_fence.activate(workspace_id)
                if cancelled:
                    raise asyncio.CancelledError
                return True

    async def complete_session_workspace_reclamation(self, workspace_id: str) -> None:
        """Forget a transient retirement only after its exact tree is gone."""

        async with self._workspace_reference_fence.hold():
            self._workspace_reference_fence.activate(workspace_id)

    async def block_workspace_identity(self, workspace_id: str | None) -> None:
        async with self._workspace_reference_fence.hold():
            self._workspace_reference_fence.block(workspace_id)

    async def _restore_or_retire_locked(
        self,
        workspace_id: str,
        restore: Callable[[str], None],
    ) -> bool:
        try:
            _, cancelled = await self._finish_task_despite_cancellation(
                asyncio.create_task(asyncio.to_thread(restore, workspace_id)),
            )
        except BaseException:
            self._workspace_reference_fence.retire(workspace_id)
            raise
        self._workspace_reference_fence.activate(workspace_id)
        return cancelled

    @staticmethod
    async def _finish_task_despite_cancellation(
        task: asyncio.Task[_TaskResult],
    ) -> tuple[_TaskResult, bool]:
        """Keep the reference fence held until a filesystem step settles."""

        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        return task.result(), cancelled

    async def _workspace_is_referenced_locked(
        self,
        user_id: str,
        workspace_id: str,
        ignored_bindings: frozenset[tuple[str, str]],
    ) -> bool:
        del ignored_bindings
        if workspace_id not in await self._native_session_references.reclaimable_workspace_ids():
            return True
        durable = await self._snapshot_native_session_workspaces_locked(user_id)
        return workspace_id in durable.values()

    async def _snapshot_native_session_workspaces_locked(
        self,
        user_id: str,
    ) -> dict[tuple[str, str], str]:
        bindings, self._session_workspaces = await self._native_session_references.snapshot(
            user_id,
            self._session_workspaces,
        )
        return bindings

    def _load_mcp_bindings(
        self,
        workspace: Path,
    ) -> tuple[list[MCPClient], tuple[MCPResourcePolicy, ...]]:
        declaration_root = workspace / "mcp"
        if not declaration_root.exists():
            return [], ()
        self._require_real_directory(declaration_root, "MCP declaration root")
        clients: list[MCPClient] = []
        resource_policies: list[MCPResourcePolicy] = []
        names: set[str] = set()
        allowed_hosts = load_network_hosts(workspace, self._environ)
        for path in sorted(declaration_root.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                raise ValueError("MCP declaration must be a regular JSON file")
            record = self._load_mcp_record(path)
            name = record.get("name")
            if not isinstance(name, str) or name in names:
                raise ValueError("MCP declaration names must be unique strings")
            names.add(name)
            config = record.get("mcp_config")
            references = record.get("credential_refs", [])
            if not isinstance(config, dict) or not isinstance(references, list):
                raise ValueError("MCP declaration config and credential_refs are required")
            if config.get("type") == "http_mcp":
                self._require_runtime_bound_mcp_config(name, config, references)
            resolved = self._resolve_credential_refs(config, references)
            clients.append(
                self._build_mcp_client(
                    name,
                    record,
                    resolved,
                    allowed_hosts,
                    require_container_route=self._require_read_only_sources,
                ),
            )
            resource_policies.append(parse_mcp_resource_policy(name, record))
        return clients, tuple(resource_policies)

    def _load_mcp_clients(self, workspace: Path) -> list[MCPClient]:
        """为离线准入和测试返回已解析 client；生产构造同时读取 resource policy。"""

        return self._load_mcp_bindings(workspace)[0]

    @staticmethod
    def _load_mcp_record(path: Path) -> JsonObject:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid MCP declaration: {path.name}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("MCP declaration schema_version must be 1")
        return value

    @staticmethod
    def _require_runtime_bound_mcp_config(server_name: str, config: JsonObject, references: list[Any]) -> None:
        env_prefix = mcp_env_prefix(server_name)
        reference_paths = {item.get("path") for item in references if isinstance(item, dict) and isinstance(item.get("path"), str)}
        url = config.get("url")
        if not isinstance(url, str) or url != "${" + env_prefix + "URL}" or "mcp_config.url" not in reference_paths:
            raise ValueError("MCP endpoint must be bound from a declared Runtime environment reference")
        if any(not isinstance(item, dict) or not isinstance(item.get("env"), str) or not item["env"].startswith(env_prefix) for item in references):
            raise ValueError("MCP credential environment must be scoped to its server")
        headers = config.get("headers", {})
        if not isinstance(headers, dict):
            raise ValueError("MCP headers must be an object")
        seen_headers: set[str] = set()
        for name, value in headers.items():
            normalized = name.casefold()
            if HTTP_HEADER_NAME.fullmatch(name) is None or forbidden_mcp_header(name) or normalized in seen_headers:
                raise ValueError("MCP header name is invalid or forbidden")
            seen_headers.add(normalized)
            target = f"mcp_config.headers.{name}"
            if not isinstance(value, str) or not _ENV_PLACEHOLDER.search(value) or target not in reference_paths:
                raise ValueError("MCP headers must be bound from declared Runtime environment references")

    def _resolve_credential_refs(
        self,
        config: JsonObject,
        references: list[Any],
    ) -> JsonObject:
        resolved = json.loads(json.dumps(config))
        seen: set[tuple[str, str]] = set()
        for item in references:
            if not isinstance(item, dict) or set(item) != {"env", "path"}:
                raise ValueError("MCP credential_ref must contain only env and path")
            env_name = item.get("env")
            path = item.get("path")
            if not isinstance(env_name, str) or _ENV_NAME.fullmatch(env_name) is None:
                raise ValueError("MCP credential_ref env is invalid")
            if not isinstance(path, str) or _REFERENCE_PATH.fullmatch(path) is None:
                raise ValueError("MCP credential_ref path is invalid")
            if (env_name, path) in seen:
                raise ValueError("MCP credential_ref is duplicated")
            seen.add((env_name, path))
            value = self._environ.get(env_name)
            if value is None or not value:
                raise ValueError(f"Required MCP credential environment is missing: {env_name}")
            target = self._reference_target(resolved, path)
            placeholder = "${" + env_name + "}"
            if not isinstance(target[0][target[1]], str) or placeholder not in target[0][target[1]]:
                raise ValueError("MCP credential_ref does not point to its placeholder")
            target[0][target[1]] = target[0][target[1]].replace(placeholder, value)
        leftovers = self._collect_placeholders(resolved)
        if leftovers:
            raise ValueError(
                "MCP configuration has unresolved environment placeholders: " + ", ".join(sorted(leftovers)),
            )
        return resolved

    @staticmethod
    def _reference_target(
        config: JsonObject,
        path: str,
    ) -> tuple[JsonObject, str]:
        parts = path.split(".")[1:]
        current = config
        for part in parts[:-1]:
            child = current.get(part)
            if not isinstance(child, dict):
                raise ValueError("MCP credential_ref path does not exist")
            current = child
        if parts[-1] not in current:
            raise ValueError("MCP credential_ref path does not exist")
        return current, parts[-1]

    @staticmethod
    def _collect_placeholders(value: Any) -> set[str]:
        if isinstance(value, str):
            return set(_ENV_PLACEHOLDER.findall(value))
        if isinstance(value, list):
            return set().union(
                *(AgentGovWorkspaceManager._collect_placeholders(item) for item in value),
            )
        if isinstance(value, dict):
            return set().union(
                *(AgentGovWorkspaceManager._collect_placeholders(item) for item in value.values()),
            )
        return set()

    @staticmethod
    def _build_mcp_client(
        name: str,
        record: JsonObject,
        config: JsonObject,
        allowed_hosts: frozenset[str],
        *,
        require_container_route: bool,
    ) -> MCPClient:
        config_type = config.get("type")
        if config_type == "http_mcp":
            parsed_config = validated_http_mcp_config(
                config,
                allowed_hosts,
                require_container_route=require_container_route,
            )
        elif config_type == "stdio_mcp":
            raise ValueError("stdio_mcp is forbidden by AgentGov Runtime policy")
        else:
            raise ValueError("MCP config type must be http_mcp")
        is_stateful = record.get("is_stateful", False)
        if not isinstance(is_stateful, bool):
            raise ValueError("MCP is_stateful must be a boolean")
        enable_tools = explicit_mcp_tools(record)
        if record.get("disable_tools") not in (None, []):
            raise ValueError("MCP disable_tools is forbidden when exact enable_tools is required")
        return MCPClient(
            name=name,
            is_stateful=is_stateful,
            mcp_config=parsed_config,
            enable_tools=enable_tools,
            disable_tools=None,
            execution_timeout=record.get("execution_timeout"),
        )

    @staticmethod
    def _parse_workspace_binding(
        workspace_id: str | None,
    ) -> tuple[str, str, str]:
        if workspace_id is None or "--v-" not in workspace_id:
            raise ValueError(
                "workspace_id must be '<agent_id>--v-<64 lowercase hex digest>'",
            )
        version_binding = version_workspace_id(workspace_id)
        agent_id, digest = version_binding.rsplit("--v-", 1)
        if _SAFE_AGENT_ID.fullmatch(agent_id) is None or _HARNESS_DIGEST.fullmatch(digest) is None:
            raise ValueError(
                "workspace_id must be '<agent_id>--v-<64 lowercase hex digest>'",
            )
        return workspace_id, agent_id, digest

    def _materialize(self, workspace_id: str) -> tuple[Path, Path]:
        _, agent_id, digest = self._parse_workspace_binding(workspace_id)
        if not agent_id.startswith(("candidate-", "published-")):
            raise ValueError("Live business Harness sources are forbidden; provision a published snapshot")
        source_root = self._candidates_root
        agent_root = source_root / agent_id
        source = agent_root / "workspace"
        target = self._workspaces_root / workspace_id
        self._require_real_directory(source_root, "Harness source root")
        if self._require_read_only_sources and not os.statvfs(source_root).f_flag & os.ST_RDONLY:
            raise ValueError("Harness source root must be mounted read-only")
        self._require_real_directory(agent_root, "Harness agent root")
        self._require_real_directory(source, "Harness workspace")
        if agent_id.startswith("published-"):
            self._validate_published_snapshot(agent_root, agent_id, digest)
        self._reject_symlinks(source)
        self._validate_source_report(source)
        self._validate_harness_digest(source, digest)
        if (source / _MARKER).exists() or (source / _MARKER).is_symlink():
            raise ValueError("Harness source must not contain a Runtime marker")
        self._require_real_directory(self._workspaces_root, "Runtime workspace root")
        if target.exists() or target.is_symlink():
            self._validate_existing_target(target, workspace_id, digest)
            self._materialize_references(source, target / _STATE_DIR, digest)
            return source, target
        self._create_state_atomically(target, workspace_id, digest, source)
        return source, target

    @staticmethod
    def _validate_published_snapshot(
        snapshot: Path,
        source_id: str,
        digest: str,
    ) -> None:
        marker = snapshot / _PUBLISHED_SNAPSHOT_MARKER
        if marker.is_symlink() or not marker.is_file():
            raise ValueError("Published Harness snapshot marker is missing or unsafe")
        if {entry.name for entry in snapshot.iterdir()} != {_PUBLISHED_SNAPSHOT_MARKER, "workspace"}:
            raise ValueError("Published Harness snapshot contains unexpected entries")
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Published Harness snapshot marker is invalid") from exc
        expected_fields = {
            "schema_version",
            "source_id",
            "agent_id",
            "agent_version_id",
            "harness_digest",
        }
        if not isinstance(payload, dict) or set(payload) != expected_fields:
            raise ValueError("Published Harness snapshot marker has an invalid schema")
        version_id = payload.get("agent_version_id")
        owner_id = payload.get("agent_id")
        if (
            payload.get("schema_version") != 1
            or payload.get("source_id") != source_id
            or payload.get("harness_digest") != digest
            or not isinstance(version_id, str)
            or not version_id
            or any(character not in "0123456789abcdef" for character in version_id)
            or not isinstance(owner_id, str)
            or _SAFE_AGENT_ID.fullmatch(owner_id) is None
        ):
            raise ValueError("Published Harness snapshot marker does not match the workspace binding")

    @staticmethod
    def _require_real_directory(path: Path, label: str) -> None:
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f"{label} must be an existing non-symlink directory")

    @staticmethod
    def _validate_source_report(source: Path) -> None:
        report = source / _REPORT
        if not report.exists():
            return
        if report.is_symlink() or not report.is_file():
            raise ValueError("conversion-report.json must be a regular file")
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("conversion-report.json is invalid") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("conversion-report.json schema_version must be 1")
        if payload.get("rejected_count", 0) != 0:
            raise ValueError("conversion-report.json contains rejected inputs")

    @staticmethod
    def _validate_harness_digest(workspace: Path, expected: str) -> None:
        if harness_digest(workspace) != expected:
            raise ValueError("Harness tree digest does not match workspace_id")

    def _validate_existing_target(
        self,
        target: Path,
        workspace_id: str,
        digest: str,
    ) -> None:
        self._require_real_directory(target, "Runtime workspace")
        validate_runtime_state_links(target)
        marker = target / _MARKER
        if marker.is_symlink() or not marker.is_file():
            raise ValueError("Runtime workspace marker is missing or unsafe")
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Runtime workspace marker is invalid") from exc
        expected = {"workspace_id": workspace_id, "harness_digest": digest}
        if payload != expected:
            raise ValueError("Runtime workspace marker does not match binding")
        self._require_real_directory(target / _STATE_DIR, "Runtime state root")
        self._require_real_directory(target / _CACHE_DIR, "Runtime cache root")
        if os.path.lexists(target / _STATE_DIR / ".mcp"):
            raise ValueError(
                "Persisted .mcp is forbidden; reprovision the fresh Runtime workspace",
            )
        if {entry.name for entry in target.iterdir()} != {_MARKER, _STATE_DIR, _CACHE_DIR}:
            raise ValueError("Runtime workspace root contains unexpected entries")

    def _create_state_atomically(
        self,
        target: Path,
        workspace_id: str,
        digest: str,
        source: Path,
    ) -> None:
        staging_root = Path(
            tempfile.mkdtemp(prefix=f".{workspace_id}.", dir=self._workspaces_root),
        )
        temporary = staging_root / "workspace"
        try:
            temporary.mkdir(mode=0o700)
            (temporary / _STATE_DIR).mkdir(mode=0o700)
            (temporary / _CACHE_DIR).mkdir(mode=0o700)
            self._materialize_references(source, temporary / _STATE_DIR, digest)
            self._write_marker(temporary, workspace_id, digest)
            self._fsync_directory(temporary)
            try:
                os.rename(temporary, target)
            except OSError:
                if not target.exists():
                    raise
                self._validate_existing_target(target, workspace_id, digest)
                self._materialize_references(source, target / _STATE_DIR, digest)
            else:
                self._fsync_directory(self._workspaces_root)
        finally:
            if staging_root.exists() or staging_root.is_symlink():
                remove_private_staging_tree(staging_root)

    def _materialize_references(self, source: Path, state: Path, digest: str) -> None:
        materialize_runtime_references(source, state, digest)

    @staticmethod
    def _reject_symlinks(root: Path) -> None:
        for current, directories, files in os.walk(root, followlinks=False):
            for name in (*directories, *files):
                if (Path(current) / name).is_symlink():
                    raise ValueError("Harness workspace must not contain symlinks")

    @staticmethod
    def _write_marker(root: Path, workspace_id: str, digest: str) -> None:
        marker = root / _MARKER
        payload = (
            json.dumps(
                {"workspace_id": workspace_id, "harness_digest": digest},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(marker, flags, 0o640)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
