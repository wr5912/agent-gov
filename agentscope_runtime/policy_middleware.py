"""执行 AgentGov Harness 的工具权限策略。"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import yaml
from agentgov_harness_digest import harness_content_digest
from agentscope.middleware import MiddlewareBase
from agentscope.permission import PermissionBehavior, PermissionDecision, PermissionMode
from agentscope.tool import ToolBase

from .bash_policy import parse_safe_bash
from .receipt_middleware import CURRENT_RUNTIME_CONTEXT
from .types import MiddlewareInput

_PROTECTED_FILES = {".mcp", "AGENT.md", "agent.yaml"}
_PROTECTED_DIRECTORIES = {"skills", "mcp", "subagents"}
_READ_TOOL_PATH_KEYS = {"Read": "file_path", "Grep": "path", "Glob": "path"}
_MAX_PERMISSION_SCAN_ENTRIES = 10_000
_BASH_METADATA_MUTATOR = re.compile(r"(?:^|[;&|()\s])(chmod|chown|chgrp|chattr)(?:$|\s)")
_RUN_RULE_SOURCE_PREFIX = "agentgov-run:"
_REQUIRED_IMMUTABLE_PATHS = {"AGENT.md", "agent.yaml", "skills/**", "mcp/**", "subagents/**"}
_SUPPORTED_RUNTIME_MIDDLEWARES = {"policy_guard", "tool_audit", "system_prompt_context"}
_SUBAGENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,126}")


@dataclass(frozen=True)
class _HarnessRule:
    tool_name: str
    rule_content: str | None


@dataclass(frozen=True)
class _PolicyConfig:
    allow_rules: tuple[_HarnessRule, ...]
    deny_rules: tuple[_HarnessRule, ...]
    denied_read_paths: tuple[str, ...]
    writable_paths: tuple[str, ...]
    allowed_network_hosts: frozenset[str]
    allowed_subagent_types: frozenset[str]


class AgentGovPolicyMiddleware(MiddlewareBase):
    """DENY 优先执行 Harness 规则，未被显式允许的工具一律拒绝。"""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        tool_workdir: str | Path | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root).resolve()
        self._tool_workdir = Path(tool_workdir or workspace_root).resolve()
        self._environ = os.environ if environ is None else environ
        policy = self._load_policy()
        self._allow_rules = policy.allow_rules
        self._deny_rules = policy.deny_rules
        self._denied_read_paths = policy.denied_read_paths
        self._writable_paths = policy.writable_paths
        self._allowed_network_hosts = policy.allowed_network_hosts
        self._allowed_subagent_types = policy.allowed_subagent_types

    async def on_reply(
        self,
        agent: Any,
        input_kwargs: MiddlewareInput,
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        try:
            async for item in next_handler(**input_kwargs):
                yield item
        finally:
            # 一个 AgentGov run 可以跨越多次 AgentScope HITL reply。当前 run
            # 的规则必须保留到后续 continuation；授权判断仍会在
            # ``on_check_permission`` 中按 active run_id 精确匹配。下一次 run
            # 第一次检查时会删除这些旧规则，因此持久化状态不会造成跨 run
            # 放权。
            runtime_context = CURRENT_RUNTIME_CONTEXT.get()
            self._prune_run_rules(
                agent,
                active_run_id=(runtime_context.run_id if runtime_context is not None else None),
            )

    async def on_check_permission(
        self,
        agent: Any,
        input_kwargs: MiddlewareInput,
        next_handler: Callable[..., Awaitable[PermissionDecision]],
    ) -> PermissionDecision:
        runtime_context = CURRENT_RUNTIME_CONTEXT.get()
        self._prune_run_rules(
            agent,
            active_run_id=runtime_context.run_id if runtime_context is not None else None,
        )
        tool = input_kwargs.get("tool")
        tool_input = input_kwargs.get("tool_input")
        if not isinstance(tool, ToolBase) or not isinstance(tool_input, dict):
            return self._deny("AgentGov policy received an invalid permission request")
        if self._is_protected_write(tool.name, tool_input):
            return self._deny("AgentGov Harness assets are immutable")
        if self._is_protected_bash(tool.name, tool_input):
            return self._deny("AgentGov Harness assets cannot be mutated through Bash")
        if self._is_unsafe_bash(tool.name, tool_input):
            return self._deny("Bash command is outside the AgentGov safe command subset")
        if self._reads_denied_path(tool.name, tool_input):
            return self._deny("Read path is denied by AgentGov Harness policy")
        if self._writes_outside_allowed_path(tool.name, tool_input):
            return self._deny("Write path is outside workspace_policy.writable_paths")
        if self._uses_disallowed_network(tool.name, tool_input):
            return self._deny("Network target is outside workspace_policy.allowed_network_domains")
        if self._uses_disallowed_subagent(tool.name, tool_input):
            return self._deny("Subagent type is outside the current AgentGov Harness version")
        if self._permission_mode(agent) is PermissionMode.BYPASS:
            return self._deny("AgentGov Runtime forbids bypass permission mode")
        subagent_decision = await self._subagent_boundary(agent, tool, tool_input)
        if subagent_decision is not None:
            return subagent_decision
        if await self._matches(self._deny_rules, tool, tool_input):
            return self._deny("Denied by AgentGov Harness policy")
        if runtime_context is not None and await self._matches_current_run_rule(
            agent,
            tool,
            tool_input,
            runtime_context.run_id,
        ):
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message="Allowed by AgentGov run-scoped confirmation",
                decision_reason="agentgov.allow_for_run",
            )
        if await self._matches(self._allow_rules, tool, tool_input):
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message="Allowed by AgentGov Harness policy",
                decision_reason="workspace_policy.allowed_tools",
            )
        del next_handler
        return self._deny("Tool invocation is not explicitly allowed by AgentGov Harness policy")

    async def _subagent_boundary(
        self,
        agent: Any,
        tool: ToolBase,
        tool_input: MiddlewareInput,
    ) -> PermissionDecision | None:
        context = getattr(getattr(agent, "state", None), "permission_context", None)
        allow_rules = self._scoped_subagent_rules(getattr(context, "allow_rules", None))
        deny_rules = self._scoped_subagent_rules(getattr(context, "deny_rules", None))
        if not allow_rules and not deny_rules:
            return None
        if await self._matches_permission_rules(deny_rules, tool, tool_input):
            return self._deny("Denied by the current AgentGov subagent policy")
        if not await self._matches_permission_rules(allow_rules, tool, tool_input):
            return self._deny("Tool is not explicitly allowed for the current AgentGov subagent")
        return None

    @staticmethod
    def _scoped_subagent_rules(value: Any) -> tuple[Any, ...]:
        if not isinstance(value, dict):
            return ()
        return tuple(
            rule for rules in value.values() if isinstance(rules, list) for rule in rules if str(getattr(rule, "source", "")).startswith("agentgov-subagent:")
        )

    @staticmethod
    async def _matches_permission_rules(
        rules: tuple[Any, ...],
        tool: ToolBase,
        tool_input: MiddlewareInput,
    ) -> bool:
        for rule in rules:
            if getattr(rule, "tool_name", None) != tool.name:
                continue
            if await tool.match_rule(getattr(rule, "rule_content", None), tool_input):
                return True
        return False

    @staticmethod
    def _prune_run_rules(agent: Any, *, active_run_id: str | None) -> None:
        permission_context = getattr(getattr(agent, "state", None), "permission_context", None)
        allow_rules = getattr(permission_context, "allow_rules", None)
        if not isinstance(allow_rules, dict):
            return
        active_source = f"{_RUN_RULE_SOURCE_PREFIX}{active_run_id}" if active_run_id else None
        for tool_name, rules in list(allow_rules.items()):
            kept = [
                rule
                for rule in rules
                if not str(getattr(rule, "source", "")).startswith(_RUN_RULE_SOURCE_PREFIX) or getattr(rule, "source", None) == active_source
            ]
            if kept:
                allow_rules[tool_name] = kept
            else:
                allow_rules.pop(tool_name, None)

    def _load_policy(
        self,
    ) -> _PolicyConfig:
        manifest = self._workspace_root / "agent.yaml"
        if manifest.is_symlink() or not manifest.is_file():
            raise ValueError("AgentGov Runtime workspace agent.yaml is missing or unsafe")
        try:
            payload = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError("AgentGov Runtime workspace agent.yaml is invalid") from exc
        if not isinstance(payload, dict):
            raise ValueError("AgentGov Runtime workspace agent.yaml must be an object")
        session = payload.get("session", {})
        if isinstance(session, dict) and session.get("permission_mode") == "bypass":
            raise ValueError("AgentGov Runtime forbids bypass permission mode")
        policy = payload.get("workspace_policy")
        if not isinstance(policy, dict):
            raise ValueError("agent.yaml workspace_policy is required")
        if policy.get("fail_closed") is not True or policy.get("immutable_harness") is not True:
            raise ValueError("workspace_policy must be fail_closed and immutable_harness")
        sandbox = policy.get("sandbox")
        if not isinstance(sandbox, dict) or sandbox != {
            "enabled": True,
            "fail_if_unavailable": True,
            "allow_unsandboxed_commands": False,
        }:
            raise ValueError("workspace_policy.sandbox must require the Runtime sandbox")
        immutable_paths = self._parse_path_list(policy.get("immutable_paths"), "immutable_paths", allow_empty=False)
        if not _REQUIRED_IMMUTABLE_PATHS.issubset(immutable_paths):
            raise ValueError("workspace_policy.immutable_paths does not protect the complete Harness")
        self._validate_runtime_middlewares(payload.get("runtime_middlewares"))
        return _PolicyConfig(
            allow_rules=self._parse_rules(policy.get("allowed_tools"), "allowed_tools"),
            deny_rules=self._parse_rules(policy.get("denied_tools"), "denied_tools"),
            denied_read_paths=tuple(self._parse_path_list(policy.get("denied_read_paths"), "denied_read_paths", allow_empty=False)),
            writable_paths=tuple(self._parse_path_list(policy.get("writable_paths"), "writable_paths", allow_empty=True)),
            allowed_network_hosts=self._parse_network_hosts(policy.get("allowed_network_domains")),
            allowed_subagent_types=self._load_allowed_subagent_types(),
        )

    def _load_allowed_subagent_types(self) -> frozenset[str]:
        root = self._workspace_root / "subagents"
        if not root.exists():
            return frozenset()
        if root.is_symlink() or not root.is_dir():
            raise ValueError("subagents must be a non-symlink directory")
        digest = harness_content_digest(self._workspace_root)
        allowed: set[str] = set()
        for entry in sorted(root.iterdir()):
            if entry.is_symlink() or not entry.is_dir() or _SUBAGENT_NAME.fullmatch(entry.name) is None:
                raise ValueError("subagents contains an unsafe entry")
            if not (entry / "agent.yaml").is_file() or not (entry / "AGENT.md").is_file():
                raise ValueError("subagent requires agent.yaml and AGENT.md")
            allowed.add(f"agentgov-{digest}-{entry.name}")
        return frozenset(allowed)

    @staticmethod
    def _parse_path_list(value: Any, field: str, *, allow_empty: bool) -> set[str]:
        if (
            not isinstance(value, list)
            or (not allow_empty and not value)
            or any(not isinstance(item, str) or not item.strip() or "\0" in item for item in value)
        ):
            raise ValueError(f"workspace_policy.{field} must be a valid string list")
        return {item.strip().replace("\\", "/") for item in value}

    def _parse_network_hosts(self, value: Any) -> frozenset[str]:
        if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
            raise ValueError("workspace_policy.allowed_network_domains must be a string list")
        hosts: set[str] = set()
        for item in value:
            resolved = item
            if item.startswith("${") and item.endswith("}"):
                resolved = self._environ.get(item[2:-1], "")
                if not resolved:
                    raise ValueError(f"Network policy environment is missing: {item[2:-1]}")
            parsed = urlsplit(resolved if "://" in resolved else f"https://{resolved}")
            if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not parsed.hostname:
                raise ValueError("workspace_policy.allowed_network_domains contains an unsafe target")
            if any(character in parsed.hostname for character in "*?[]"):
                raise ValueError("workspace_policy.allowed_network_domains cannot contain wildcards")
            hosts.add(parsed.hostname.lower())
        return frozenset(hosts)

    @staticmethod
    def _validate_runtime_middlewares(value: Any) -> None:
        if not isinstance(value, list):
            raise ValueError("runtime_middlewares must be a list")
        names = [item.get("type") for item in value if isinstance(item, dict)]
        if len(names) != len(value) or any(name not in _SUPPORTED_RUNTIME_MIDDLEWARES for name in names):
            raise ValueError("runtime_middlewares contains an unsupported declaration")
        if "policy_guard" not in names:
            raise ValueError("runtime_middlewares must declare policy_guard")

    @staticmethod
    def _parse_rules(value: Any, field: str) -> tuple[_HarnessRule, ...]:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"workspace_policy.{field} must be a string list")
        rules: list[_HarnessRule] = []
        for item in value:
            tool_name, separator, content = item.partition("(")
            if field == "allowed_tools" and tool_name.startswith("mcp__") and any(character in tool_name for character in "*?["):
                raise ValueError("workspace_policy.allowed_tools cannot wildcard MCP tools")
            if separator:
                if not item.endswith(")") or not tool_name or not content[:-1]:
                    raise ValueError(f"workspace_policy.{field} contains an invalid rule")
                rules.append(_HarnessRule(tool_name, content[:-1]))
            elif item:
                rules.append(_HarnessRule(item, None))
            else:
                raise ValueError(f"workspace_policy.{field} contains an empty rule")
        return tuple(rules)

    async def _matches(
        self,
        rules: tuple[_HarnessRule, ...],
        tool: ToolBase,
        tool_input: MiddlewareInput,
    ) -> bool:
        for rule in rules:
            if not self._tool_name_matches(rule.tool_name, tool.name):
                continue
            if await tool.match_rule(rule.rule_content, tool_input):
                return True
        return False

    @staticmethod
    async def _matches_current_run_rule(
        agent: Any,
        tool: ToolBase,
        tool_input: MiddlewareInput,
        run_id: str,
    ) -> bool:
        context = getattr(getattr(agent, "state", None), "permission_context", None)
        rules = getattr(context, "allow_rules", {}).get(tool.name, [])
        source = f"{_RUN_RULE_SOURCE_PREFIX}{run_id}"
        for rule in rules:
            if getattr(rule, "source", None) == source and await tool.match_rule(
                getattr(rule, "rule_content", None),
                tool_input,
            ):
                return True
        return False

    @staticmethod
    def _tool_name_matches(pattern: str, tool_name: str) -> bool:
        if pattern.startswith("mcp__") and any(char in pattern for char in "*?["):
            return fnmatch.fnmatchcase(tool_name, pattern)
        return pattern == tool_name

    def _is_protected_write(self, tool_name: str, tool_input: MiddlewareInput) -> bool:
        if tool_name not in {"Write", "Edit"}:
            return False
        file_path = tool_input.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return True
        candidate = self._resolve_tool_path(file_path)
        for root in (self._workspace_root, self._tool_workdir):
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                continue
            if not relative.parts:
                return True
            if relative.as_posix() in _PROTECTED_FILES or relative.parts[0] in _PROTECTED_DIRECTORIES:
                return True
        return False

    def _is_protected_bash(self, tool_name: str, tool_input: MiddlewareInput) -> bool:
        if tool_name != "Bash":
            return False
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            return True
        lowered = command.lower()
        if _BASH_METADATA_MUTATOR.search(lowered):
            return True
        protected = (".mcp", "agent.yaml", "agent.md", "skills/", "mcp/", "subagents/")
        return any(item in lowered for item in protected) or str(self._workspace_root).lower() in lowered

    def _is_unsafe_bash(self, tool_name: str, tool_input: MiddlewareInput) -> bool:
        if tool_name != "Bash":
            return False
        command = parse_safe_bash(tool_input.get("command"))
        if command is None:
            return True
        return any(not self._path_is_writable(self._resolve_tool_path(path)) for path in command.directory_paths)

    def _writes_outside_allowed_path(self, tool_name: str, tool_input: MiddlewareInput) -> bool:
        if tool_name not in {"Write", "Edit", "NotebookEdit"}:
            return False
        file_path = tool_input.get("file_path")
        return not isinstance(file_path, str) or not self._path_is_writable(self._resolve_tool_path(file_path))

    def _path_is_writable(self, target: Path) -> bool:
        try:
            relative = target.relative_to(self._tool_workdir).as_posix()
        except ValueError:
            return False
        candidates = {relative, f"./{relative}", target.as_posix()}
        return any(
            candidate == rule.rstrip("/")
            or candidate.startswith(rule.rstrip("/") + "/")
            or fnmatch.fnmatchcase(candidate, rule)
            or PurePosixPath(candidate).match(rule)
            for rule in self._writable_paths
            for candidate in candidates
        )

    def _uses_disallowed_network(self, tool_name: str, tool_input: MiddlewareInput) -> bool:
        if tool_name != "WebFetch":
            return False
        raw_url = tool_input.get("url")
        if not isinstance(raw_url, str):
            return True
        parsed = urlsplit(raw_url)
        return (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.hostname is None
            or parsed.hostname.lower() not in self._allowed_network_hosts
        )

    def _uses_disallowed_subagent(self, tool_name: str, tool_input: MiddlewareInput) -> bool:
        if tool_name != "AgentCreate":
            return False
        subagent_type = tool_input.get("subagent_type")
        return not isinstance(subagent_type, str) or subagent_type not in self._allowed_subagent_types

    def _reads_denied_path(self, tool_name: str, tool_input: MiddlewareInput) -> bool:
        path_key = _READ_TOOL_PATH_KEYS.get(tool_name)
        if path_key is None:
            return False
        raw_path = tool_input.get(path_key)
        if raw_path is None and tool_name != "Read":
            raw_path = "."
        if not isinstance(raw_path, str) or not raw_path.strip():
            return True
        target = self._resolve_tool_path(raw_path)
        try:
            target.relative_to(self._tool_workdir)
        except ValueError:
            return True
        if target == self._tool_workdir / ".mcp":
            return True
        target_is_denied = self._path_is_denied(target)
        if target_is_denied or tool_name == "Read":
            return target_is_denied
        filter_pattern = tool_input.get("pattern" if tool_name == "Glob" else "glob")
        if filter_pattern is not None and not isinstance(filter_pattern, str):
            return True
        if isinstance(filter_pattern, str) and self._pattern_is_explicitly_denied(filter_pattern):
            return True
        return self._search_scope_contains_denied(
            target,
            filter_pattern if tool_name == "Glob" else None,
        )

    def _resolve_tool_path(self, value: str) -> Path:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self._tool_workdir / candidate
        return candidate.resolve(strict=False)

    def _path_is_denied(self, path: Path) -> bool:
        absolute = path.as_posix()
        candidates = {absolute, path.name}
        for root in (self._workspace_root, self._tool_workdir):
            with suppress(ValueError):
                relative = path.relative_to(root)
                if ".mcp" in relative.parts:
                    return True
                candidates.add(relative.as_posix())
        for rule in self._denied_read_paths:
            normalized = rule.removeprefix("./")
            scoped = {absolute} if rule.startswith("/") else candidates
            for candidate in scoped:
                if candidate == normalized or candidate.startswith(normalized.rstrip("/") + "/"):
                    return True
                if fnmatch.fnmatchcase(candidate, normalized) or PurePosixPath(candidate).match(normalized):
                    return True
        return False

    def _pattern_is_explicitly_denied(self, pattern: str) -> bool:
        normalized = pattern.strip().removeprefix("./").replace("\\", "/")
        if ".mcp" in PurePosixPath(normalized).parts:
            return True
        return any(normalized == rule.removeprefix("./") for rule in self._denied_read_paths)

    def _search_scope_contains_denied(self, root: Path, glob_pattern: str | None) -> bool:
        try:
            if root.is_symlink():
                return True
            if not root.exists() or root.is_file():
                return self._path_is_denied(root)
            iterator = root.glob(glob_pattern) if glob_pattern else root.rglob("*")
            for count, candidate in enumerate(iterator, start=1):
                if count > _MAX_PERMISSION_SCAN_ENTRIES or candidate.is_symlink():
                    return True
                if self._path_is_denied(candidate.resolve(strict=False)):
                    return True
        except (OSError, RuntimeError, ValueError):
            return True
        return False

    @staticmethod
    def _permission_mode(agent: Any) -> PermissionMode | None:
        state = getattr(agent, "state", None)
        context = getattr(state, "permission_context", None)
        return getattr(context, "mode", None)

    @staticmethod
    def _deny(reason: str) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.DENY,
            message=reason,
            decision_reason=reason,
        )
