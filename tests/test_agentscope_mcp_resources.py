from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agentscope.mcp import HttpMCPConfig, MCPClient
from agentscope_runtime.mcp_resource_middleware import (
    MCPResourceMiddleware,
    MCPResourcePolicy,
    parse_mcp_resource_policy,
)
from agentscope_runtime.workspace_manager import AgentGovWorkspaceManager
from mcp.types import (
    ListResourcesResult,
    ListResourceTemplatesResult,
    ReadResourceResult,
    Resource,
    ResourceTemplate,
    TextResourceContents,
)


class _FakeSession:
    async def list_resources(self, cursor: str | None) -> ListResourcesResult:
        assert cursor in {None, "next-page", "after-resources"}
        return ListResourcesResult(
            resources=[
                Resource(name="actions", uri="openapi://soc_api/resp/action-defs"),
                Resource(name="unreviewed", uri="openapi://soc_api/private/secrets"),
            ],
            nextCursor="after-resources" if cursor in {None, "next-page"} else None,
        )

    async def list_resource_templates(self, cursor: str | None) -> ListResourceTemplatesResult:
        assert cursor is None
        return ListResourceTemplatesResult(
            resourceTemplates=[
                ResourceTemplate(
                    name="playbook",
                    uriTemplate="openapi://soc_api/resp/playbooks/{playbook_id}",
                ),
                ResourceTemplate(
                    name="unreviewed",
                    uriTemplate="openapi://soc_api/admin/{operation}",
                ),
            ],
        )

    async def read_resource(self, uri) -> ReadResourceResult:
        rendered = str(uri)
        return ReadResourceResult(
            contents=[
                TextResourceContents(
                    uri=rendered,
                    mimeType="application/json",
                    text='{"playbook_id":"pb-1"}',
                ),
            ],
        )


def _middleware() -> MCPResourceMiddleware:
    client = MCPClient(
        name="sec-ops",
        is_stateful=False,
        mcp_config=HttpMCPConfig(
            url="http://mcp.internal/mcp",
            headers={"Authorization": "Bearer runtime-only-secret"},
        ),
        enable_tools=[],
    )
    policy = MCPResourcePolicy(
        server_name="sec-ops",
        resources=frozenset({"openapi://soc_api/resp/action-defs"}),
        resource_templates=("openapi://soc_api/resp/playbooks/{playbook_id}",),
    )
    return MCPResourceMiddleware([client], (policy,))


def test_resource_facade_registers_only_fixed_read_only_tools_and_filters_server_growth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    middleware = _middleware()

    @asynccontextmanager
    async def fake_session(server_name: str):
        assert server_name == "sec-ops"
        yield _FakeSession()

    monkeypatch.setattr(middleware, "_session", fake_session)

    async def exercise() -> tuple[list[str], dict[str, object], dict[str, object], dict[str, object]]:
        tools = await middleware.list_tools()
        resources = await middleware._list_resources("sec-ops", "next-page")
        templates = await middleware._list_resource_templates("sec-ops", None)
        content = await middleware._read_resource("sec-ops", "openapi://soc_api/resp/playbooks/pb-1")
        return [tool.name for tool in tools], resources, templates, content

    names, resources, templates, content = asyncio.run(exercise())
    assert names == [
        "mcp__sec-ops__resources_list",
        "mcp__sec-ops__resource_templates_list",
        "mcp__sec-ops__resource_read",
    ]
    assert [item["uri"] for item in resources["resources"]] == ["openapi://soc_api/resp/action-defs"]
    assert resources["next_cursor"] == "after-resources"
    assert [item["uri_template"] for item in templates["resource_templates"]] == [
        "openapi://soc_api/resp/playbooks/{playbook_id}",
    ]
    assert content == {
        "contents": [
            {
                "uri": "openapi://soc_api/resp/playbooks/pb-1",
                "mime_type": "application/json",
                "text": '{"playbook_id":"pb-1"}',
            },
        ],
    }


def test_resource_facade_denies_unversioned_uri_before_network_access() -> None:
    middleware = _middleware()
    with pytest.raises(ValueError, match="outside the versioned Harness allowlist"):
        asyncio.run(middleware._read_resource("sec-ops", "openapi://soc_api/admin/secrets"))


@pytest.mark.parametrize(
    "record",
    [
        {},
        {
            "enable_resources": [],
            "enable_resource_templates": ["openapi://soc_api/resp/{+unsafe}"],
        },
        {
            "enable_resources": ["openapi://soc_api/resp/action-defs"] * 2,
            "enable_resource_templates": [],
        },
    ],
)
def test_resource_policy_requires_explicit_unique_simple_allowlists(record: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        parse_mcp_resource_policy("sec-ops", record)


def test_workspace_binding_fails_when_live_mcp_tool_roster_is_missing() -> None:
    class FakeClient:
        name = "sec-ops"
        enable_tools = ["tool_a", "tool_b"]

        async def list_raw_tools(self):
            return [SimpleNamespace(name="tool_a")]

    client = FakeClient()
    workspace = SimpleNamespace(
        default_mcps=[client],
        list_mcps=lambda **_: _async_value([client]),
    )
    with pytest.raises(RuntimeError, match="tool roster"):
        asyncio.run(
            AgentGovWorkspaceManager._validate_live_mcp_tools(
                cast(Any, workspace),
                agent_id="agent",
                session_id="session",
            ),
        )


async def _async_value(value):
    return value
