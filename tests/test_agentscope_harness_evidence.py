from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.message import UserMsg
from agentscope.tool import ToolChunk
from agentscope_runtime.harness_evidence_middleware import GovernedHarnessEvidenceMiddleware


def _workspace(tmp_path: Path, agent_id: str = "soc-ops") -> tuple[Path, str]:
    business_root = tmp_path / "business-agents"
    workspace = business_root / agent_id / "workspace"
    (workspace / "skills" / "triage").mkdir(parents=True)
    (workspace / "AGENT.md").write_text("# SOC\n", encoding="utf-8")
    (workspace / "agent.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (workspace / "skills" / "triage" / "SKILL.md").write_text("# Triage\n", encoding="utf-8")
    (workspace / ".env").write_text("SECRET=never\n", encoding="utf-8")
    return business_root, f"/business-agents/{agent_id}/workspace"


async def _invoke_inside_reply(
    middleware: GovernedHarnessEvidenceMiddleware,
    logical_root: str | None,
) -> tuple[dict[str, object], dict[str, object]]:
    tools = {tool.name: tool for tool in await middleware.list_tools()}
    captured: list[tuple[dict[str, object], dict[str, object]]] = []
    metadata = {"agentgov_governed_evidence_root": logical_root} if logical_root else {}
    message = UserMsg("user", "govern", metadata=metadata)

    async def next_handler(**_kwargs):
        listed = await tools["HarnessList"]()
        assert isinstance(listed, ToolChunk)
        list_payload = json.loads(listed.content[0].text)
        read = await tools["HarnessRead"](path="AGENT.md")
        assert isinstance(read, ToolChunk)
        captured.append((list_payload, json.loads(read.content[0].text)))
        yield "done"

    values = [
        item
        async for item in middleware.on_reply(
            SimpleNamespace(),
            {"inputs": [message]},
            next_handler,
        )
    ]
    assert values == ["done"]
    return captured[0]


def test_tools_read_only_the_run_bound_non_sensitive_harness(tmp_path: Path) -> None:
    business_root, logical_root = _workspace(tmp_path)
    middleware = GovernedHarnessEvidenceMiddleware(business_root)

    listed, read = asyncio.run(_invoke_inside_reply(middleware, logical_root))

    assert listed == {
        "root": logical_root,
        "files": [
            f"{logical_root}/AGENT.md",
            f"{logical_root}/agent.yaml",
            f"{logical_root}/skills/triage/SKILL.md",
        ],
    }
    assert read["path"] == f"{logical_root}/AGENT.md"
    assert read["content"] == "# SOC\n"
    assert isinstance(read["sha256"], str) and len(read["sha256"]) == 64


def test_tools_fail_closed_without_binding_and_after_reply(tmp_path: Path) -> None:
    business_root, logical_root = _workspace(tmp_path)
    middleware = GovernedHarnessEvidenceMiddleware(business_root)
    tools = {tool.name: tool for tool in asyncio.run(middleware.list_tools())}

    with pytest.raises(ValueError, match="no governed Harness"):
        asyncio.run(tools["HarnessList"]())

    asyncio.run(_invoke_inside_reply(middleware, logical_root))
    with pytest.raises(ValueError, match="no governed Harness"):
        asyncio.run(tools["HarnessRead"](path="AGENT.md"))


@pytest.mark.parametrize(
    "path",
    [
        "../other/workspace/AGENT.md",
        "/business-agents/other/workspace/AGENT.md",
        ".env",
        "credentials.json",
    ],
)
def test_harness_read_rejects_escape_and_sensitive_paths(tmp_path: Path, path: str) -> None:
    business_root, logical_root = _workspace(tmp_path)
    middleware = GovernedHarnessEvidenceMiddleware(business_root)
    tools = {tool.name: tool for tool in asyncio.run(middleware.list_tools())}

    async def next_handler(**_kwargs):
        await tools["HarnessRead"](path=path)
        yield "unreachable"

    async def exercise() -> None:
        async for _ in middleware.on_reply(
            SimpleNamespace(),
            {"inputs": {"metadata": {"agentgov_governed_evidence_root": logical_root}}},
            next_handler,
        ):
            pass

    with pytest.raises(ValueError):
        asyncio.run(exercise())


def test_harness_list_rejects_any_symlink_in_source(tmp_path: Path) -> None:
    business_root, logical_root = _workspace(tmp_path)
    workspace = business_root / "soc-ops" / "workspace"
    (workspace / "skills" / "linked.md").symlink_to(workspace / "AGENT.md")
    middleware = GovernedHarnessEvidenceMiddleware(business_root)
    tools = {tool.name: tool for tool in asyncio.run(middleware.list_tools())}

    async def next_handler(**_kwargs):
        await tools["HarnessList"]()
        yield "unreachable"

    async def exercise() -> None:
        async for _ in middleware.on_reply(
            SimpleNamespace(),
            {"inputs": {"metadata": {"agentgov_governed_evidence_root": logical_root}}},
            next_handler,
        ):
            pass

    with pytest.raises(ValueError, match="symbolic links"):
        asyncio.run(exercise())
