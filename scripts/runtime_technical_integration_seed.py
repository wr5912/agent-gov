"""通过公共 API 创建并回收 Runtime 技术集成用的最小 Harness。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

import httpx
import yaml
from agentgov_agentscope_contract import AGENTSCOPE_RUNTIME_CONTRACT
from app.agent_testing.schemas import AgentTestRunResponse
from app.runtime.agent_governance_schemas import AgentDeleteResponse
from app.runtime.agent_paths import validate_agent_id
from app.runtime.agent_workspace_package_schemas import WorkspaceImportResponse
from app.runtime.json_types import JsonObject
from app.runtime.response_schemas.agent_governance_response_schemas import (
    AgentChangeSetResponse,
    AgentGitDiffResponse,
    AgentGitFileDiffResponse,
    AgentReleaseResponse,
)
from pydantic import ValidationError

from scripts.agentscope_live_acceptance_scenarios import (
    MCP_TECHNICAL_COMPLETION_TEXT,
    MCP_TECHNICAL_RAW_TOOL_NAME,
    MCP_TECHNICAL_RESOURCE_TEMPLATE,
    MCP_TECHNICAL_RESOURCE_URI,
    MCP_TECHNICAL_SERVER_NAME,
    MCP_TECHNICAL_TOOL_NAMES,
)
from scripts.run_container_acceptance import ACTIVE_ENV, PROFILE_ENV, RUN_ID_ENV

TECHNICAL_INTEGRATION_SCOPE = "runtime-technical-integration-package"
MCP_TECHNICAL_INTEGRATION_SCOPE = "runtime-mcp-technical-integration-package"
TECHNICAL_INTEGRATION_EXCLUDED_CLAIMS = (
    "security-business-capability",
    "mcp",
    "subagents",
    "hitl",
    "session-resume",
    "runtime-restart-recovery",
    "model-effect-improvement",
)
MCP_TECHNICAL_INTEGRATION_EXCLUDED_CLAIMS = (
    "security-business-capability",
    "authenticated-mcp",
    "mcp-write",
    "subagents",
    "hitl",
    "session-resume",
    "runtime-restart-recovery",
    "model-effect-improvement",
)


class TechnicalIntegrationSeedError(RuntimeError):
    """技术集成 Agent 的公开导入或清理失败；消息不得包含响应原文。"""


@dataclass(frozen=True)
class TechnicalIntegrationAgent:
    agent_id: str
    agent_version_id: str
    runtime_agent_id: str
    harness_digest: str


@dataclass(frozen=True)
class CandidateReviewClaims:
    diff_digest: str
    reviewed_files: tuple[JsonObject, ...]


def _canonical_json_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_seed_package(agent_id: str) -> bytes:
    """构造全新原生最小 Harness；不导出或裁减任何内置业务 Agent。"""

    validate_agent_id(agent_id)
    manifest: JsonObject = {
        "schema_version": 1,
        "agent": {
            "id": agent_id,
            "runtime": "agentscope",
            "runtime_contract": AGENTSCOPE_RUNTIME_CONTRACT,
            "system_prompt": "AGENT.md",
        },
        "session": {"permission_mode": "dont_ask", "cwd": ".", "model_profile": "default"},
        "runtime_middlewares": [{"type": "policy_guard", "phase": "before_tool_call", "fail_closed": True}],
        "workspace_policy": {
            "fail_closed": True,
            "immutable_harness": True,
            "allow_for_run": False,
            "allowed_tools": [],
            "ask_tools": [],
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
        "tests/README.md": b"# Technical integration Harness tests\n",
        "tests/test_runtime_harness.py": (
            "from pathlib import Path\n\n"
            "WORKSPACE = Path(__file__).resolve().parents[1]\n\n"
            "def test_minimal_runtime_harness_is_complete(agent):\n"
            "    manifest = (WORKSPACE / 'agent.yaml').read_text(encoding='utf-8')\n"
            "    instructions = (WORKSPACE / 'AGENT.md').read_text(encoding='utf-8')\n"
            "    assert 'runtime: agentscope' in manifest\n"
            "    assert 'immutable_harness: true' in manifest\n"
            "    assert '不调用任何工具' in instructions\n"
            "    result = agent.run('请只输出七加七的十进制计算结果，不使用工具。')\n"
            "    assert not result.errors\n"
            "    assert result.text.strip() == '14'\n"
            "    assert result.raw['agent_activity']['tool_calls'] == []\n"
        ).encode(),
    }
    return _package_bytes(entries)


def build_mcp_seed_package(agent_id: str) -> bytes:
    """构造只允许一个真实只读 MCP 工具和三个 resource facade 的 Harness。"""

    validate_agent_id(agent_id)
    manifest = _mcp_manifest(agent_id)
    mcp_declaration = {
        "schema_version": 1,
        "name": MCP_TECHNICAL_SERVER_NAME,
        "credential_refs": [{"env": "SEC_OPS_MCP_URL", "path": "mcp_config.url"}],
        "mcp_config": {"type": "http_mcp", "url": "${SEC_OPS_MCP_URL}", "timeout": 30.0},
        "enable_tools": [MCP_TECHNICAL_RAW_TOOL_NAME],
        "enable_resources": [MCP_TECHNICAL_RESOURCE_URI],
        "enable_resource_templates": [MCP_TECHNICAL_RESOURCE_TEMPLATE],
    }
    instructions = f"""# 平台 MCP 只读验收助手

收到用户消息后，必须且只能各调用一次以下四个工具；不得用自然语言声称已经调用：

1. `mcp__sec-ops__resources_list`，不传 cursor。
2. `mcp__sec-ops__resource_templates_list`，不传 cursor。
3. `mcp__sec-ops__resource_read`，uri 必须是 `openapi://soc_api/health`。
4. `mcp__sec-ops__soc_api__dashboard_summary_api_v1_dashboard_summary_get`，不传参数。

全部工具成功后只回复 `{MCP_TECHNICAL_COMPLETION_TEXT}`，不得复述任何工具参数或返回正文。
"""
    test_source = f"""import json
from pathlib import Path

import yaml

WORKSPACE = Path(__file__).resolve().parents[1]


def test_mcp_readonly_harness_contract(agent):
    manifest = yaml.safe_load((WORKSPACE / "agent.yaml").read_text(encoding="utf-8"))
    declaration = json.loads((WORKSPACE / "mcp/sec-ops.json").read_text(encoding="utf-8"))
    assert manifest["workspace_policy"]["allowed_tools"] == {list(MCP_TECHNICAL_TOOL_NAMES)!r}
    assert manifest["workspace_policy"]["allowed_network_domains"] == ["${{SEC_OPS_MCP_URL}}"]
    assert declaration["credential_refs"] == [{{"env": "SEC_OPS_MCP_URL", "path": "mcp_config.url"}}]
    assert declaration["mcp_config"]["url"] == "${{SEC_OPS_MCP_URL}}"
    assert "headers" not in declaration["mcp_config"]
    assert declaration["enable_tools"] == [{MCP_TECHNICAL_RAW_TOOL_NAME!r}]
    assert declaration["enable_resources"] == [{MCP_TECHNICAL_RESOURCE_URI!r}]
    assert declaration["enable_resource_templates"] == [{MCP_TECHNICAL_RESOURCE_TEMPLATE!r}]
    result = agent.run("请执行声明的真实 MCP 只读技术验收，并仅在全部调用成功后返回规定完成标记。")
    assert not result.errors
    assert result.text.strip() == {MCP_TECHNICAL_COMPLETION_TEXT!r}
    assert result.raw["agent_activity"]["tool_calls"]
"""
    return _package_bytes(
        {
            "AGENT.md": instructions.encode(),
            "agent.yaml": yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False).encode(),
            "mcp/sec-ops.json": json.dumps(mcp_declaration, ensure_ascii=False, indent=2, sort_keys=True).encode(),
            "tests/README.md": b"# Real MCP technical acceptance Harness tests\n",
            "tests/test_mcp_readonly_harness.py": test_source.encode(),
        },
    )


def _mcp_manifest(agent_id: str) -> JsonObject:
    return {
        "schema_version": 1,
        "agent": {
            "id": agent_id,
            "runtime": "agentscope",
            "runtime_contract": AGENTSCOPE_RUNTIME_CONTRACT,
            "system_prompt": "AGENT.md",
        },
        "react_config": {"max_iters": 12},
        "session": {"permission_mode": "dont_ask", "cwd": ".", "model_profile": "default"},
        "runtime_middlewares": [
            {"type": "policy_guard", "phase": "before_tool_call", "fail_closed": True},
            {"type": "system_prompt_context", "source": "AGENT.md"},
        ],
        "workspace_policy": {
            "fail_closed": True,
            "immutable_harness": True,
            "allow_for_run": False,
            "allowed_tools": list(MCP_TECHNICAL_TOOL_NAMES),
            "ask_tools": [],
            "denied_tools": [],
            "denied_read_paths": [".env", "**/.env", "**/*credential*"],
            "immutable_paths": ["AGENT.md", "agent.yaml", "skills/**", "mcp/**", "subagents/**"],
            "writable_paths": [],
            "allowed_network_domains": ["${SEC_OPS_MCP_URL}"],
            "sandbox": {"enabled": True, "fail_if_unavailable": True, "allow_unsandboxed_commands": False},
        },
    }


def _package_bytes(entries: dict[str, bytes]) -> bytes:
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


async def _import_seed(
    client: httpx.AsyncClient,
    agent_id: str,
    *,
    package: bytes,
    display_name: str,
) -> WorkspaceImportResponse:
    try:
        response = await client.post(
            f"/api/agent-registry/{agent_id}/workspace/import",
            data={"name": display_name},
            files={"package": (f"{agent_id}.tar.gz", package, "application/gzip")},
        )
        response.raise_for_status()
        imported = WorkspaceImportResponse.model_validate_json(response.content)
    except (httpx.HTTPError, ValidationError, ValueError) as exc:
        raise TechnicalIntegrationSeedError(f"技术集成 Agent 公共导入失败（{type(exc).__name__}）") from None
    if (
        imported.action != "created"
        or imported.agent.agent_id != agent_id
        or imported.agent.category != "business"
        or imported.agent.status != "draft"
        or imported.agent.protected
        or imported.agent.builtin
        or imported.published is not False
        or not imported.change_set_id
        or not imported.candidate_commit_sha
        or imported.candidate_commit_sha == imported.base_commit_sha
        or imported.package_sha256 != hashlib.sha256(package).hexdigest()
        or imported.test_suite_status == "invalid"
    ):
        raise TechnicalIntegrationSeedError("技术集成 Agent 公共导入未确认全新独立 Harness；隔离 runner 负责残留回收")
    return imported


async def _pass_candidate_test_gate(client: httpx.AsyncClient, imported: WorkspaceImportResponse) -> AgentTestRunResponse:
    try:
        response = await client.post(f"/api/agent-change-sets/{imported.change_set_id}/test-runs")
        response.raise_for_status()
        test_run = AgentTestRunResponse.model_validate_json(response.content)
        deadline = asyncio.get_running_loop().time() + 120.0
        while test_run.status in {"queued", "running"} and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.25)
            response = await client.get(f"/api/agent-test-runs/{test_run.test_run_id}")
            response.raise_for_status()
            test_run = AgentTestRunResponse.model_validate_json(response.content)
    except (httpx.HTTPError, ValidationError) as exc:
        raise TechnicalIntegrationSeedError(f"技术集成 Agent 候选测试失败（{type(exc).__name__}）") from None
    if (
        test_run.status != "passed"
        or test_run.agent_id != imported.agent.agent_id
        or test_run.change_set_id != imported.change_set_id
        or test_run.commit_sha != imported.candidate_commit_sha
        or not test_run.suite_digest
    ):
        raise TechnicalIntegrationSeedError(
            _candidate_test_failure(
                test_run,
                agent_id=imported.agent.agent_id,
                change_set_id=imported.change_set_id,
                commit_sha=imported.candidate_commit_sha,
            )
        )
    return test_run


def _candidate_test_failure(test_run: AgentTestRunResponse, *, agent_id: str, change_set_id: str, commit_sha: str) -> str:
    """只投影失败元数据；不把 pytest 原文或参数化 nodeid 带入验收日志。"""

    item = next((item for item in test_run.items if item.outcome != "passed"), None)
    known_nodes = {
        "tests/test_runtime_harness.py::test_minimal_runtime_harness_is_complete",
        "tests/test_mcp_readonly_harness.py::test_mcp_readonly_harness_contract",
    }
    error_code = test_run.error.get("error_code")
    payload = {
        "status": test_run.status,
        "failure_reason": "pending_timeout" if test_run.status in {"queued", "running"} else "gate_rejected",
        "exit_code": test_run.exit_code,
        "suite_digest_present": bool(test_run.suite_digest),
        "agent_match": test_run.agent_id == agent_id,
        "change_set_match": test_run.change_set_id == change_set_id,
        "commit_match": test_run.commit_sha == commit_sha,
        "error_code": error_code if isinstance(error_code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", error_code) else "unclassified",
        "http_status": _pytest_http_status(item.detail or "") if item is not None else None,
        "http_error_code": _pytest_http_error_code(item.detail or "") if item is not None else None,
        "first_nonpass_item": (
            {
                "nodeid": item.nodeid if item.nodeid in known_nodes else "unclassified",
                "outcome": item.outcome if item.outcome in {"failed", "skipped", "incomplete", "error"} else "unclassified",
                "phase": item.phase if item.phase in {"setup", "call", "teardown"} else "unclassified",
                "failure_kind": _pytest_failure_kind(item.detail or ""),
            }
            if item is not None
            else None
        ),
    }
    return "技术集成 Agent 候选未通过精确 commit 平台测试门禁: " + json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _pytest_failure_kind(detail: str) -> str:
    for kind in ("AssertionError", "HTTPStatusError", "RuntimeError"):
        if re.search(rf"(?m)^E\s+(?:[\w.]+\.)?{kind}(?::|\s|$)", detail):
            return kind
    return "AssertionError" if re.search(r"(?m)^E\s+assert(?:\s|$)", detail) else "other"


def _pytest_http_status(detail: str) -> int | None:
    match = re.search(r"(?m)^E\s+.*AgentGov test invocation failed: HTTP ([45][0-9]{2}) error_code=", detail)
    return int(match.group(1)) if match else None


def _pytest_http_error_code(detail: str) -> str | None:
    match = re.search(r"(?m)^E\s+.*AgentGov test invocation failed: HTTP [45][0-9]{2} error_code=([A-Z][A-Z0-9_]{0,127})(?:\s|$)", detail)
    return match.group(1) if match else None


async def _verify_candidate_diff(
    client: httpx.AsyncClient,
    imported: WorkspaceImportResponse,
    *,
    mcp_readonly: bool,
) -> CandidateReviewClaims:
    try:
        response = await client.get(f"/api/agent-change-sets/{imported.change_set_id}/diff")
        response.raise_for_status()
        diff_payload = response.json()
        diff = AgentGitDiffResponse.model_validate(diff_payload)
    except (httpx.HTTPError, ValidationError, ValueError) as exc:
        raise TechnicalIntegrationSeedError(f"技术集成 Agent 敏感 Diff 查询失败（{type(exc).__name__}）") from None
    added_paths = {entry.path for entry in diff.added}
    if (
        diff.from_version_id != imported.base_commit_sha
        or diff.to_version_id != imported.candidate_commit_sha
        or set(imported.changed_paths) != added_paths
        or diff.modified
        or diff.deleted
    ):
        raise TechnicalIntegrationSeedError("技术集成 Agent 敏感 Diff 未精确绑定导入候选")
    required = {"AGENT.md", "agent.yaml", "mcp/sec-ops.json", "tests/test_mcp_readonly_harness.py"}
    if mcp_readonly and required - added_paths:
        raise TechnicalIntegrationSeedError("技术集成 Agent MCP 敏感 Diff 缺少必须文件")
    reviewed: list[JsonObject] = []
    mcp_file_diff: AgentGitFileDiffResponse | None = None
    for path in sorted(added_paths):
        try:
            file_response = await client.get(
                f"/api/agent-change-sets/{imported.change_set_id}/file-diff",
                params={"path": path},
            )
            file_response.raise_for_status()
            detail_payload = file_response.json()
            detail = AgentGitFileDiffResponse.model_validate(detail_payload)
        except (httpx.HTTPError, ValidationError, ValueError) as exc:
            raise TechnicalIntegrationSeedError(f"技术集成 Agent 文件 Diff 查询失败（{type(exc).__name__}）") from None
        changed = any(line.startswith(("+", "-")) and not line.startswith(("+++", "---")) for line in detail.unified_diff.splitlines())
        if (
            detail.path != path
            or detail.from_version_id != imported.base_commit_sha
            or detail.to_version_id != imported.candidate_commit_sha
            or detail.status != "added"
            or not detail.is_text
            or detail.truncated
            or not changed
        ):
            raise TechnicalIntegrationSeedError("技术集成 Agent 文件 Diff 未完整展开并绑定候选")
        if path == "mcp/sec-ops.json":
            mcp_file_diff = detail
        reviewed.append({"path": path, "detail_sha256": _canonical_json_digest(detail_payload)})
    if mcp_readonly and (
        mcp_file_diff is None
        or '"${SEC_OPS_MCP_URL}"' not in mcp_file_diff.unified_diff
        or "http://" in mcp_file_diff.unified_diff
        or "https://" in mcp_file_diff.unified_diff
    ):
        raise TechnicalIntegrationSeedError("技术集成 Agent MCP 文件 Diff 未确认 URL credential_ref 边界")
    diff_digest = _canonical_json_digest(diff_payload)
    return CandidateReviewClaims(diff_digest, tuple(reviewed))


async def _approve_and_publish_candidate(
    client: httpx.AsyncClient,
    imported: WorkspaceImportResponse,
    test_run: AgentTestRunResponse,
    *,
    require_sensitive_diff: bool,
) -> TechnicalIntegrationAgent:
    try:
        if require_sensitive_diff and imported.change_set_status != "pending_approval":
            raise TechnicalIntegrationSeedError("MCP Harness 敏感变更未进入强制人工审批状态")
        claims = await _verify_candidate_diff(client, imported, mcp_readonly=require_sensitive_diff)
        if imported.change_set_status == "pending_approval":
            approval = await client.post(
                f"/api/agent-change-sets/{imported.change_set_id}/approve",
                json={
                    "operator": "technical-live-acceptance",
                    "note": "Approve isolated technical acceptance Harness",
                    "candidate_commit_sha": imported.candidate_commit_sha,
                    "diff_digest": claims.diff_digest,
                    "test_run_id": test_run.test_run_id,
                    "suite_digest": test_run.suite_digest,
                    "reviewed_files": list(claims.reviewed_files),
                },
            )
            approval.raise_for_status()
            approved = AgentChangeSetResponse.model_validate_json(approval.content)
            evidence = approved.approval_evidence
            if (
                approved.status != "approved"
                or approved.candidate_commit_sha != imported.candidate_commit_sha
                or evidence is None
                or evidence.candidate_commit_sha != imported.candidate_commit_sha
                or evidence.test_run_id != test_run.test_run_id
                or evidence.suite_digest != test_run.suite_digest
                or evidence.diff_digest != claims.diff_digest
                or evidence.reviewed_file_count != len(claims.reviewed_files)
                or len(evidence.review_digest) != 64
            ):
                raise TechnicalIntegrationSeedError("技术集成 Agent 审批未绑定精确 Diff 与 pytest 证据")
        elif imported.change_set_status != "candidate_committed":
            raise TechnicalIntegrationSeedError("技术集成 Agent 候选未进入可审批状态")
        published_response = await client.post(
            f"/api/agent-change-sets/{imported.change_set_id}/publish",
            json={
                "operator": "technical-live-acceptance",
                "force": False,
                "expected_candidate_commit_sha": imported.candidate_commit_sha,
                "expected_diff_digest": claims.diff_digest,
                "expected_test_run_id": test_run.test_run_id,
                "expected_suite_digest": test_run.suite_digest,
            },
        )
        published_response.raise_for_status()
        release = AgentReleaseResponse.model_validate_json(published_response.content)
        current_response = await client.get(f"/api/runtime/agents/{imported.agent.agent_id}/current")
        current_response.raise_for_status()
        current = current_response.json()
    except TechnicalIntegrationSeedError:
        raise
    except (httpx.HTTPError, ValidationError, ValueError) as exc:
        raise TechnicalIntegrationSeedError(f"技术集成 Agent 发布激活失败（{type(exc).__name__}）") from None
    runtime_agent_id = current.get("runtime_agent_id") if isinstance(current, dict) else None
    harness_digest = current.get("harness_digest") if isinstance(current, dict) else None
    if (
        release.agent_id != imported.agent.agent_id
        or release.commit_sha != imported.candidate_commit_sha
        or release.runtime_agent_id != runtime_agent_id
        or not isinstance(runtime_agent_id, str)
        or not isinstance(harness_digest, str)
        or current.get("agent_version_id") != release.commit_sha
        or current.get("provisioned") is not True
    ):
        raise TechnicalIntegrationSeedError("技术集成 Agent 发布回执与当前 Runtime 绑定不一致")
    return TechnicalIntegrationAgent(
        agent_id=release.agent_id,
        agent_version_id=release.commit_sha,
        runtime_agent_id=runtime_agent_id,
        harness_digest=harness_digest,
    )


async def _delete_seed(client: httpx.AsyncClient, agent_id: str) -> None:
    try:
        response = await client.delete(f"/api/agent-registry/{agent_id}")
        response.raise_for_status()
        deleted = AgentDeleteResponse.model_validate_json(response.content)
    except (httpx.HTTPError, ValidationError) as exc:
        raise TechnicalIntegrationSeedError(f"技术集成 Agent 公共清理失败（{type(exc).__name__}）") from None
    if (
        not {"cleanup_complete", "workspace_removed"}.issubset(deleted.model_fields_set)
        or deleted.cleanup_complete is not True
        or deleted.workspace_removed is not True
        or deleted.deleted.agent_id != agent_id
    ):
        raise TechnicalIntegrationSeedError("技术集成 Agent 公共清理未确认 cleanup_complete/workspace_removed")


async def _abandon_unpublished_candidate(client: httpx.AsyncClient, change_set_id: str) -> None:
    try:
        response = await client.get(f"/api/agent-change-sets/{change_set_id}")
        response.raise_for_status()
        change_set = AgentChangeSetResponse.model_validate_json(response.content)
        if change_set.status == "published":
            return
        if change_set.status != "abandoned":
            response = await client.post(
                f"/api/agent-change-sets/{change_set_id}/abandon",
                json={"operator": "technical-live-acceptance", "note": "Clean isolated technical acceptance candidate"},
            )
            response.raise_for_status()
            change_set = AgentChangeSetResponse.model_validate_json(response.content)
        if change_set.status != "abandoned":
            raise TechnicalIntegrationSeedError("技术集成 Agent 候选未确认 abandoned")
    except TechnicalIntegrationSeedError:
        raise
    except (httpx.HTTPError, ValidationError) as exc:
        raise TechnicalIntegrationSeedError(f"技术集成 Agent 候选清理失败（{type(exc).__name__}）") from None


def _cleanup_failure(stage: Literal["abandon", "delete"], exc: Exception, *, primary_failed: bool) -> None:
    detail = f"stage={stage}; error_type={type(exc).__name__}; isolated runner cleanup required"
    if not primary_failed:
        raise TechnicalIntegrationSeedError(f"技术集成 Agent 清理失败: {detail}") from None
    print(f"AGENTSCOPE_TECHNICAL_SEED_CLEANUP_FAIL: {detail}", file=sys.stderr)


@asynccontextmanager
async def temporary_technical_integration_agent(
    client: httpx.AsyncClient,
    *,
    mcp_readonly: bool = False,
) -> AsyncIterator[TechnicalIntegrationAgent]:
    """只回收本次已确认创建的身份；清理失败不得覆盖原始验收异常。"""

    if os.environ.get(ACTIVE_ENV) != "1" or not os.environ.get(RUN_ID_ENV) or os.environ.get(PROFILE_ENV) not in {"core", "langfuse"}:
        raise TechnicalIntegrationSeedError("技术集成 Agent 只允许在公共 Make 隔离容器 runner 内创建")
    prefix = "runtime-mcp-technical-integration" if mcp_readonly else "runtime-technical-integration"
    agent_id = f"{prefix}-{uuid.uuid4().hex}"
    package = build_mcp_seed_package(agent_id) if mcp_readonly else build_seed_package(agent_id)
    display_name = "平台 MCP 只读临时验收" if mcp_readonly else "通用 Runtime 临时验收"
    imported: WorkspaceImportResponse | None = None
    activated: TechnicalIntegrationAgent | None = None
    primary_failed = False
    try:
        imported = await _import_seed(client, agent_id, package=package, display_name=display_name)
        test_run = await _pass_candidate_test_gate(client, imported)
        activated = await _approve_and_publish_candidate(
            client,
            imported,
            test_run,
            require_sensitive_diff=mcp_readonly,
        )
        yield activated
    except BaseException:
        primary_failed = True
        raise
    finally:
        cleanup_stage: Literal["abandon", "delete"] = "abandon"
        try:
            if imported is not None and activated is None:
                await _abandon_unpublished_candidate(client, imported.change_set_id)
            cleanup_stage = "delete"
            await _delete_seed(client, agent_id)
        except Exception as exc:
            _cleanup_failure(cleanup_stage, exc, primary_failed=primary_failed)
