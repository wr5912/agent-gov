"""将版本化 AgentGov Harness 复制为 Runtime 可写工作区。"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from agentgov_harness_digest import harness_content_digest
from agentscope.app import SubAgentTemplate
from agentscope.app.storage import StorageBase
from agentscope.app.workspace_manager import WorkspaceManagerBase
from agentscope.mcp import HttpMCPConfig, MCPClient
from agentscope.skill import LocalSkillLoader, Skill
from agentscope.workspace import BubblewrapWorkspace

from .mcp_resource_middleware import MCPResourcePolicy, parse_mcp_resource_policy
from .offline_gateway import WorkspacePreparation, offline_gateway_env, validate_runtime_state_links
from .subagent_templates import load_subagent_templates
from .types import JsonObject

_SAFE_AGENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}")
_HARNESS_DIGEST = re.compile(r"[0-9a-f]{64}")
_SESSION_WORKSPACE_TOKEN = re.compile(
    r"session-intent-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
)
_MARKER = ".agentgov-runtime-workspace.json"
_REPORT = "conversion-report.json"
_PUBLISHED_SNAPSHOT_MARKER = "snapshot.json"
_STATE_DIR = ".agentgov-runtime-state"
_CACHE_DIR = ".agentgov-runtime-cache"
_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_REFERENCE_PATH = re.compile(r"mcp_config(?:\.[A-Za-z0-9_-]+)+")
_MCP_TOOL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,255}")
_HTTP_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_FORBIDDEN_MCP_HEADERS = {
    "connection",
    "content-length",
    "forwarded",
    "host",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "via",
    "x-original-url",
    "x-rewrite-url",
}
_RESERVED_RESOURCE_TOOL_NAMES = {
    "resources_list",
    "resource_templates_list",
    "resource_read",
}


def _mcp_env_prefix(server_name: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", "_", server_name.upper()).strip("_")
    return f"{normalized}_MCP_"


def _forbidden_mcp_header(name: str) -> bool:
    normalized = name.casefold()
    return normalized in _FORBIDDEN_MCP_HEADERS or normalized.startswith("x-forwarded-")


def _visible_ascii(value: str) -> bool:
    return bool(value) and all(0x20 <= ord(character) <= 0x7E for character in value)


def harness_digest(workspace: Path) -> str:
    """按 AgentGov 控制面的同一全树算法计算运行版本摘要。"""

    return harness_content_digest(workspace)


class AgentGovLocalWorkspace(BubblewrapWorkspace):
    """Bubblewrap 隔离的可写状态；Harness 只由可信 Runtime 进程读取。"""

    def __init__(
        self,
        *,
        harness_root: Path,
        expected_digest: str,
        sandbox_env: dict[str, str] | None = None,
        default_mcps: list[MCPClient] | None = None,
        mcp_resource_policies: tuple[MCPResourcePolicy, ...] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(
            skill_paths=[],
            default_mcps=default_mcps,
            share_net=True,
            gateway_port=None,
            env={
                **(sandbox_env or {}),
                **offline_gateway_env(),
            },
            extra_pip=["agentscope==2.0.8"],
            **kwargs,
        )
        self.harness_root = str(harness_root)
        self._expected_digest = expected_digest
        self.mcp_resource_policies = mcp_resource_policies
        self._skill_loader = LocalSkillLoader(str(harness_root / "skills"), scan_subdir=True)

    async def list_skills(self, *, agent_id: str | None = None) -> list[Skill]:
        """Load governed skills from the read-only source, never a writable seed copy."""

        del agent_id
        self._validate_source_digest()
        skills = await self._skill_loader.list_skills()
        self._validate_source_digest()
        return skills

    def _validate_source_digest(self) -> None:
        if harness_digest(Path(self.harness_root)) != self._expected_digest:
            raise ValueError("Harness tree digest changed after workspace binding")


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
        self._bound_storage: StorageBase | None = None
        self._lock = asyncio.Lock()

    def bind_storage(self, storage: StorageBase) -> None:
        """Bind AgentScope storage for durable Workspace reference checks."""

        super().bind_storage(storage)
        self._bound_storage = storage

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
        async with self._lock:
            preparation = WorkspacePreparation()
            harness_root, state_root = await asyncio.to_thread(self._materialize, parsed_id)
            cached = self._cache.get(parsed_id)
            created = cached is None
            if cached is None:
                default_mcps, resource_policies = self._load_mcp_bindings(harness_root)
                cached = AgentGovLocalWorkspace(
                    workspace_id=parsed_id,
                    host_workdir=str(state_root / _STATE_DIR),
                    host_cache_dir=str(state_root / _CACHE_DIR),
                    harness_root=harness_root,
                    expected_digest=digest,
                    sandbox_env=self._sandbox_proxy_env(),
                    default_mcps=default_mcps,
                    mcp_resource_policies=resource_policies,
                )
                await preparation.initialize(cached)
            elif Path(cached.harness_root) != harness_root:
                raise ValueError("Harness source changed after workspace binding")
            harness_root = Path(cached.harness_root)
            try:
                self._register_subagent_templates(harness_root, digest)
                binding = (parsed_id, agent_id, session_id)
                if binding not in self._validated_mcp_bindings:
                    await preparation.validate_mcp(
                        self._validate_live_mcp_tools(cached, agent_id=agent_id, session_id=session_id),
                    )
                    self._validated_mcp_bindings.add(binding)
            except BaseException:
                if created:
                    await cached.close()
                raise
            if created:
                self._cache[parsed_id] = cached
            self._session_workspaces[(user_id, agent_id, session_id)] = parsed_id
            return cached

    def _sandbox_proxy_env(self) -> dict[str, str]:
        proxies: dict[str, str] = {}
        for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "no_proxy"):
            value = self._environ.get(name)
            if not value:
                continue
            if name.lower() in {"http_proxy", "https_proxy"}:
                parsed = urlsplit(value)
                if parsed.username is not None or parsed.password is not None:
                    raise ValueError("Credential-bearing sandbox proxy URLs are forbidden")
            proxies[name] = value
        return proxies

    def _register_subagent_templates(self, workspace: Path, digest: str) -> None:
        incoming = load_subagent_templates(workspace, digest)
        for template_type, template in incoming.items():
            existing = self._subagent_templates.get(template_type)
            if existing is None:
                raise RuntimeError(
                    f"Subagent template {template_type!r} was published after Runtime startup; restart Runtime",
                )
            if existing != template:
                raise ValueError(f"Subagent template changed after Runtime startup: {template_type}")

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

    async def resolve_session_workspace_id(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> str | None:
        """Resolve a Session binding from cache, then durable public storage."""

        async with self._lock:
            binding = (user_id, agent_id, session_id)
            workspace_id = self._session_workspaces.get(binding)
            if workspace_id is not None:
                return workspace_id
            storage = self._bound_storage
            if storage is None:
                return None
            for session in await storage.list_sessions(user_id, agent_id):
                if session.id == session_id and session.config.workspace_id:
                    return session.config.workspace_id
            return None

    async def release_session_workspace_if_unreferenced(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        *,
        workspace_id: str | None = None,
    ) -> bool:
        """Close a cached Workspace only after durable zero-reference proof.

        The persistent AgentScope Session records are authoritative. Local
        Session mappings are only candidates, so stale team-cascade entries
        can never keep a Workspace alive.
        """

        async with self._lock:
            binding = (user_id, agent_id, session_id)
            candidate = workspace_id or self._session_workspaces.get(binding)
            if candidate is None:
                return False
            storage = self._bound_storage
            if storage is None:
                return False

            referenced_workspace_ids: set[str] = set()
            for agent in await storage.list_agents(user_id):
                for session in await storage.list_sessions(user_id, agent.id):
                    if session.config.workspace_id:
                        referenced_workspace_ids.add(session.config.workspace_id)

            if candidate in referenced_workspace_ids:
                self._session_workspaces.pop(binding, None)
                return False

            workspace = self._cache.get(candidate)
            if workspace is not None:
                await workspace.close()
                self._cache.pop(candidate, None)
            self._session_workspaces = {
                cached_binding: bound_workspace_id for cached_binding, bound_workspace_id in self._session_workspaces.items() if bound_workspace_id != candidate
            }
            self._validated_mcp_bindings = {validated_binding for validated_binding in self._validated_mcp_bindings if validated_binding[0] != candidate}
            return workspace is not None

    @staticmethod
    async def _validate_live_mcp_tools(
        workspace: AgentGovLocalWorkspace,
        *,
        agent_id: str,
        session_id: str,
    ) -> None:
        clients = await workspace.list_mcps(
            agent_id=agent_id,
            session_id=session_id,
        )
        expected_servers = {client.name for client in workspace.default_mcps}
        if {client.name for client in clients} != expected_servers:
            raise RuntimeError("MCP server roster does not match the versioned Harness")
        for client in clients:
            expected_tools = client.enable_tools
            if expected_tools is None:
                raise RuntimeError("MCP exact tool allowlist is missing")
            raw_tools = await client.list_raw_tools()
            actual = [tool.name for tool in raw_tools]
            if len(actual) != len(set(actual)) or set(actual) != set(expected_tools):
                raise RuntimeError("MCP tool roster does not match the versioned Harness")

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
        allowed_hosts = self._load_network_hosts(workspace)
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

    def _load_network_hosts(self, workspace: Path) -> frozenset[str]:
        manifest = workspace / "agent.yaml"
        try:
            payload = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError("agent.yaml network policy is unreadable") from exc
        policy = payload.get("workspace_policy") if isinstance(payload, dict) else None
        declarations = policy.get("allowed_network_domains") if isinstance(policy, dict) else None
        if not isinstance(declarations, list) or any(not isinstance(item, str) or not item for item in declarations):
            raise ValueError("workspace_policy.allowed_network_domains must be a string list")
        hosts: set[str] = set()
        for declaration in declarations:
            resolved = declaration
            if declaration.startswith("${") and declaration.endswith("}"):
                resolved = self._environ.get(declaration[2:-1], "")
                if not resolved:
                    raise ValueError(f"Network policy environment is missing: {declaration[2:-1]}")
            parsed = urlsplit(resolved if "://" in resolved else f"https://{resolved}")
            if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not parsed.hostname:
                raise ValueError("workspace_policy.allowed_network_domains contains an unsafe target")
            if any(character in parsed.hostname for character in "*?[]"):
                raise ValueError("workspace_policy.allowed_network_domains cannot contain wildcards")
            hosts.add(parsed.hostname.lower())
        return frozenset(hosts)

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
        env_prefix = _mcp_env_prefix(server_name)
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
            if _HTTP_HEADER_NAME.fullmatch(name) is None or _forbidden_mcp_header(name) or normalized in seen_headers:
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
            parsed_config = HttpMCPConfig.model_validate(config)
            parsed_url = urlsplit(parsed_config.url)
            if not _visible_ascii(parsed_config.url):
                raise ValueError("HTTP MCP URL must contain visible ASCII only")
            try:
                address = ipaddress.ip_address(parsed_url.hostname or "")
            except ValueError:
                pass
            else:
                if not (address.is_global or address.is_loopback):
                    raise ValueError("HTTP MCP URL uses a forbidden IP address")
                if require_container_route and address.is_loopback:
                    raise ValueError("Container Runtime MCP URL cannot use a loopback address")
            headers = parsed_config.headers or {}
            if any(not _visible_ascii(name) or not _visible_ascii(value) for name, value in headers.items()):
                raise ValueError("HTTP MCP headers must contain visible ASCII only")
            if (
                parsed_url.scheme not in {"http", "https"}
                or parsed_url.username is not None
                or parsed_url.password is not None
                or parsed_url.hostname is None
                or parsed_url.hostname.lower() not in allowed_hosts
                or parsed_url.query
                or parsed_url.fragment
            ):
                raise ValueError("HTTP MCP URL is outside workspace_policy.allowed_network_domains")
            if parsed_url.path.rstrip("/") != "/mcp":
                raise ValueError("AgentGov MCP resource facade requires the streamable HTTP /mcp endpoint")
            if require_container_route and parsed_url.scheme == "http" and parsed_url.hostname.casefold() != "host.docker.internal":
                raise ValueError("Container Runtime permits plaintext MCP only through host.docker.internal")
            if require_container_route and parsed_url.hostname.casefold() == "localhost":
                raise ValueError("Container Runtime MCP URL cannot use localhost")
            default_stateful = False
        elif config_type == "stdio_mcp":
            raise ValueError("stdio_mcp is forbidden by AgentGov Runtime policy")
        else:
            raise ValueError("MCP config type must be http_mcp")
        is_stateful = record.get("is_stateful", default_stateful)
        if not isinstance(is_stateful, bool):
            raise ValueError("MCP is_stateful must be a boolean")
        enable_tools = record.get("enable_tools")
        if (
            not isinstance(enable_tools, list)
            or any(
                not isinstance(tool_name, str) or _MCP_TOOL_NAME.fullmatch(tool_name) is None or tool_name in _RESERVED_RESOURCE_TOOL_NAMES
                for tool_name in enable_tools
            )
            or len(enable_tools) != len(set(enable_tools))
        ):
            raise ValueError("MCP enable_tools must be an explicit unique tool-name list")
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
        version_binding = workspace_id
        if "--s-" in workspace_id:
            version_binding, token = workspace_id.rsplit("--s-", 1)
            if _SESSION_WORKSPACE_TOKEN.fullmatch(token) is None:
                raise ValueError("workspace_id contains an invalid Session creation token")
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
            return source, target
        self._create_state_atomically(target, workspace_id, digest)
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
    ) -> None:
        staging_root = Path(
            tempfile.mkdtemp(prefix=f".{workspace_id}.", dir=self._workspaces_root),
        )
        temporary = staging_root / "workspace"
        try:
            temporary.mkdir(mode=0o700)
            (temporary / _STATE_DIR).mkdir(mode=0o700)
            (temporary / _CACHE_DIR).mkdir(mode=0o700)
            self._write_marker(temporary, workspace_id, digest)
            self._fsync_directory(temporary)
            try:
                os.rename(temporary, target)
                self._fsync_directory(self._workspaces_root)
            except OSError:
                if not target.exists():
                    raise
                self._validate_existing_target(target, workspace_id, digest)
        finally:
            if staging_root.exists() or staging_root.is_symlink():
                shutil.rmtree(staging_root, ignore_errors=True)

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
