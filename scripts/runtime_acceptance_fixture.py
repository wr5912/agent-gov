"""仅为显式隔离验收创建并回收无外部能力依赖的临时业务 Agent。"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import tarfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import yaml
from app.runtime.agent_governance_schemas import AgentDeleteResponse
from app.runtime.agent_paths import validate_agent_id
from app.runtime.agent_workspace_package_schemas import WorkspaceImportResponse
from app.runtime.config_mapping import RUNTIME_CONTRACT
from app.runtime.json_types import JsonObject
from pydantic import ValidationError

from scripts.run_container_acceptance import ACTIVE_ENV, PROFILE_ENV, RUN_ID_ENV

FIXTURE_SCOPE = "generic-runtime"
FIXTURE_EXCLUDED_CLAIMS = (
    "security-business-capability",
    "mcp",
    "subagents",
    "hitl",
    "session-resume",
    "runtime-restart-recovery",
    "model-effect-improvement",
)


class FixtureAgentError(RuntimeError):
    """临时 Agent 的公开导入或清理契约失败；消息不得包含响应原文。"""


def build_fixture_package(agent_id: str) -> bytes:
    """构造全新原生 Harness；不导出或裁减任何内置业务 Agent。"""

    validate_agent_id(agent_id)
    manifest: JsonObject = {
        "schema_version": 1,
        "agent": {"id": agent_id, "runtime": "agentscope", "runtime_contract": RUNTIME_CONTRACT, "system_prompt": "AGENT.md"},
        "session": {"permission_mode": "dont_ask", "cwd": ".", "model_profile": "default"},
        "runtime_middlewares": [{"type": "policy_guard", "phase": "before_tool_call", "fail_closed": True}],
        "workspace_policy": {
            "fail_closed": True,
            "immutable_harness": True,
            "allow_for_run": False,
            "allowed_tools": [],
            "denied_tools": [],
            "denied_read_paths": [".env", "**/.env", "**/*credential*"],
            "immutable_paths": ["AGENT.md", "agent.yaml", "skills/**", "mcp/**", "subagents/**"],
            "writable_paths": [],
            "allowed_network_domains": [],
            "sandbox": {"enabled": True, "fail_if_unavailable": True, "allow_unsandboxed_commands": False},
        },
    }
    entries = {
        "AGENT.md": "# 通用 Runtime 验收助手\n\n仅根据当前对话内容简短回答，不调用任何工具。\n".encode(),
        "agent.yaml": yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False).encode(),
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        root = tarfile.TarInfo("workspace")
        root.type, root.mode = tarfile.DIRTYPE, 0o755
        archive.addfile(root)
        for name, content in entries.items():
            entry = tarfile.TarInfo(f"workspace/{name}")
            entry.size, entry.mode = len(content), 0o644
            archive.addfile(entry, io.BytesIO(content))
    return buffer.getvalue()


async def _import_fixture(client: httpx.AsyncClient, agent_id: str) -> WorkspaceImportResponse:
    package = build_fixture_package(agent_id)
    try:
        response = await client.post(
            f"/api/agent-registry/{agent_id}/workspace/import",
            data={"name": "通用 Runtime 临时验收"},
            files={"package": (f"{agent_id}.tar.gz", package, "application/gzip")},
        )
        response.raise_for_status()
        imported = WorkspaceImportResponse.model_validate_json(response.content)
    except (httpx.HTTPError, ValidationError) as exc:
        raise FixtureAgentError(f"临时 Agent 公共导入失败（{type(exc).__name__}）") from None
    if (
        imported.action != "created"
        or imported.agent.agent_id != agent_id
        or imported.agent.category != "business"
        or imported.agent.status != "active"
        or imported.agent.protected
        or imported.agent.builtin
        or not imported.current_commit_sha
        or imported.package_sha256 != hashlib.sha256(package).hexdigest()
        or imported.test_suite_status == "invalid"
    ):
        raise FixtureAgentError("临时 Agent 公共导入未确认全新独立 Harness；隔离 runner 负责残留回收")
    return imported


async def _delete_fixture(client: httpx.AsyncClient, agent_id: str) -> None:
    try:
        response = await client.delete(f"/api/agent-registry/{agent_id}")
        response.raise_for_status()
        deleted = AgentDeleteResponse.model_validate_json(response.content)
    except (httpx.HTTPError, ValidationError) as exc:
        raise FixtureAgentError(f"临时 Agent 公共清理失败（{type(exc).__name__}）") from None
    if (
        not {"cleanup_complete", "workspace_removed"}.issubset(deleted.model_fields_set)
        or deleted.cleanup_complete is not True
        or deleted.workspace_removed is not True
        or deleted.deleted.agent_id != agent_id
    ):
        raise FixtureAgentError("临时 Agent 公共清理未确认 cleanup_complete/workspace_removed")


@asynccontextmanager
async def temporary_runtime_agent(client: httpx.AsyncClient) -> AsyncIterator[WorkspaceImportResponse]:
    """只回收本次已确认创建的身份；清理失败不得覆盖原始验收异常。"""

    if os.environ.get(ACTIVE_ENV) != "1" or not os.environ.get(RUN_ID_ENV) or os.environ.get(PROFILE_ENV) not in {"core", "langfuse"}:
        raise FixtureAgentError("临时 Agent 只允许在公共 Make 隔离验收 runner 内创建")
    agent_id = f"runtime-acceptance-{uuid.uuid4().hex}"
    imported = await _import_fixture(client, agent_id)
    primary_failed = False
    try:
        yield imported
    except BaseException:
        primary_failed = True
        raise
    finally:
        try:
            await _delete_fixture(client, agent_id)
        except Exception as exc:
            if not primary_failed:
                raise FixtureAgentError(f"临时 Agent 清理失败（{type(exc).__name__}）；隔离 runner 仍需回收临时卷") from None
            print(f"AGENTSCOPE_FIXTURE_CLEANUP_FAIL: {type(exc).__name__}; isolated runner cleanup required", file=sys.stderr)
