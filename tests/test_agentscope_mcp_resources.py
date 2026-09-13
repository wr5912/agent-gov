from __future__ import annotations

import asyncio

import pytest
from agentscope.mcp import HttpMCPConfig, MCPClient
from agentscope_runtime.mcp_resource_middleware import (
    MCPResourceMiddleware,
    MCPResourcePolicy,
    parse_mcp_resource_policy,
)


def _middleware() -> MCPResourceMiddleware:
    client = MCPClient(
        name="sec-ops",
        is_stateful=False,
        mcp_config=HttpMCPConfig(
            url="http://127.0.0.1:1/mcp",
            headers={"Authorization": "Bearer test-only-runtime-token"},
        ),
        enable_tools=[],
    )
    policy = MCPResourcePolicy(
        server_name="sec-ops",
        resources=frozenset({"openapi://soc_api/resp/action-defs"}),
        resource_templates=("openapi://soc_api/resp/playbooks/{playbook_id}",),
    )
    return MCPResourceMiddleware([client], (policy,))


def test_resource_facade_denies_unversioned_uri_before_network_access() -> None:
    middleware = _middleware()
    with pytest.raises(ValueError, match="outside the versioned Harness allowlist"):
        asyncio.run(middleware._read_resource("sec-ops", "openapi://soc_api/admin/secrets"))


def test_valid_resource_policy_preserves_exact_allowlists() -> None:
    policy = parse_mcp_resource_policy(
        "sec-ops",
        {
            "enable_resources": ["openapi://soc_api/resp/action-defs"],
            "enable_resource_templates": ["openapi://soc_api/resp/playbooks/{playbook_id}"],
        },
    )

    assert policy == MCPResourcePolicy(
        server_name="sec-ops",
        resources=frozenset({"openapi://soc_api/resp/action-defs"}),
        resource_templates=("openapi://soc_api/resp/playbooks/{playbook_id}",),
    )


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
