from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path

import httpx
import pytest
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.message import AssistantMsg
from agentscope.types import ErrorInfo, ErrorType, ReplyFinishedReason
from agentscope_runtime.access_middleware import MAX_RUNTIME_REQUEST_BODY_BYTES, FixedRuntimeUserMiddleware
from agentscope_runtime.context_registry import RuntimeContext
from agentscope_runtime.credential_storage import ProvisionedAsyncSQLAlchemyStorage
from agentscope_runtime.policy_middleware import AgentGovPolicyMiddleware
from agentscope_runtime.service import create_runtime_app
from agentscope_runtime.session_workspace_release import SessionWorkspaceReleaseMiddleware
from agentscope_runtime.settings import RUNTIME_USER_ID, RuntimeSettings
from agentscope_runtime.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, runtime_gateway_headers, signed_headers
from agentscope_runtime.subagent_templates import discover_subagent_templates, load_subagent_templates
from agentscope_runtime.workspace_manager import AgentGovWorkspaceManager, harness_digest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from runtime_loopback import serve_loopback


def _settings(tmp_path: Path) -> RuntimeSettings:
    data_dir = tmp_path / "data"
    business_root = tmp_path / "business"
    candidates_root = tmp_path / "candidates"
    workspaces_root = tmp_path / "workspaces"
    for path in (business_root, candidates_root, workspaces_root):
        path.mkdir()
    return RuntimeSettings(
        shared_secret="shared-test-secret",
        provider_api_key="provider-test-secret",
        agentgov_api_base_url="http://agent-gov-api:8080",
        provider_api_url="http://model-provider.test/v1",
        data_dir=data_dir,
        business_agents_root=business_root,
        candidates_root=candidates_root,
        workspaces_root=workspaces_root,
        database_url=f"sqlite+aiosqlite:///{data_dir / 'agentscope.db'}",
    )


def _manager(
    settings: RuntimeSettings,
    *,
    environ: dict[str, str] | None = None,
) -> AgentGovWorkspaceManager:
    return AgentGovWorkspaceManager(
        business_agents_root=settings.business_agents_root,
        candidates_root=settings.candidates_root,
        workspaces_root=settings.workspaces_root,
        environ=environ,
    )


def _runtime_context_payload() -> dict[str, object]:
    return {
        "run_id": "run-1",
        "session_id": "session-1",
        "root_session_id": "session-1",
        "role": "root",
        "agent_id": "agent-1",
        "agent_version_id": "version-1",
        "runtime_agent_id": "runtime-agent-1",
        "harness_digest": "e" * 64,
        "trace_id": "1" * 32,
        "team_generation": 0,
    }


def _runtime_headers(settings: RuntimeSettings, method: str, path: str, body: bytes = b"") -> dict[str, str]:
    timestamp = f"{time.time_ns() / 1_000_000_000:.9f}"
    return {
        "X-User-ID": RUNTIME_USER_ID,
        **runtime_gateway_headers(
            settings.shared_secret,
            RUNTIME_USER_ID,
            method,
            path,
            body,
            timestamp=timestamp,
        ),
    }


def _runtime_json(client: TestClient, settings: RuntimeSettings, method: str, path: str, payload: object):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    headers = _runtime_headers(settings, method, path, body)
    headers["Content-Type"] = "application/json"
    return client.request(method, path, content=body, headers=headers)


def _write_harness(
    root: Path,
    agent_id: str,
    digest: str,
    *,
    report: bool = True,
) -> Path:
    workspace = root / agent_id / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "agent.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    if report:
        (workspace / "conversion-report.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "rejected_count": 0,
                    "harness_digest": digest,
                },
            )
            + "\n",
            encoding="utf-8",
        )
    return workspace


def _write_published_marker(
    workspace: Path,
    digest: str,
    *,
    agent_id: str = "soc-ops",
    agent_version_id: str = "f" * 40,
) -> None:
    snapshot = workspace.parent
    (snapshot / "snapshot.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_id": snapshot.name,
                "agent_id": agent_id,
                "agent_version_id": agent_version_id,
                "harness_digest": digest,
            },
        ),
        encoding="utf-8",
    )


def _write_policy_manifest(
    workspace: Path,
    *,
    allowed: list[str],
    denied: list[str],
    permission_mode: str = "default",
    denied_read_paths: list[str] | None = None,
    allowed_network_domains: list[str] | None = None,
) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "agent.yaml").write_text(
        json.dumps(
            {
                "session": {"permission_mode": permission_mode},
                "runtime_middlewares": [
                    {"type": "policy_guard", "phase": "before_tool_call", "fail_closed": True},
                ],
                "workspace_policy": {
                    "fail_closed": True,
                    "immutable_harness": True,
                    "allowed_tools": allowed,
                    "denied_tools": denied,
                    "denied_read_paths": denied_read_paths or [".env", "**/.env", "**/*credential*"],
                    "immutable_paths": ["AGENT.md", "agent.yaml", "skills/**", "mcp/**", "subagents/**"],
                    "writable_paths": ["**"],
                    "allowed_network_domains": allowed_network_domains or [],
                    "sandbox": {
                        "enabled": True,
                        "fail_if_unavailable": True,
                        "allow_unsandboxed_commands": False,
                    },
                },
            },
        ),
        encoding="utf-8",
    )


def _subagent_manifest(agent_id: str, description: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "agent": {
            "id": agent_id,
            "description": description,
            "runtime": "agentscope",
            "runtime_contract": "agentscope-app/2.0.8",
            "system_prompt": "AGENT.md",
        },
        "context_config": {},
        "react_config": {},
        "invite_config": {"invitable": False},
        "session": {"permission_mode": "dont_ask"},
        "workspace_policy": {
            "fail_closed": True,
            "allowed_tools": ["TeamSay"],
            "ask_tools": [],
            "denied_tools": [],
        },
    }


def test_settings_require_secret_and_absolute_sqlite_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SHARED_SECRET"):
        RuntimeSettings.from_env({})
    with pytest.raises(ValueError, match="MODEL_PROVIDER_API_KEY"):
        RuntimeSettings.from_env({"AGENTGOV_RUNTIME_SHARED_SECRET": "secret"})
    with pytest.raises(ValueError, match="proxy environment is forbidden"):
        RuntimeSettings.from_env(
            {
                "AGENTGOV_RUNTIME_SHARED_SECRET": "secret",
                "MODEL_PROVIDER_API_KEY": "provider-secret",
                "HTTPS_PROXY": "http://proxy.invalid:8080",
            },
        )

    settings = RuntimeSettings.from_env(
        {
            "AGENTGOV_RUNTIME_SHARED_SECRET": "secret",
            "MODEL_PROVIDER_API_KEY": "provider-secret",
            "MODEL_PROVIDER_API_URL": "http://model-provider.test/v1/",
            "AGENTGOV_INTERNAL_API_BASE_URL": "http://agent-gov-api:8080/",
            "AGENTSCOPE_RUNTIME_DATA_DIR": str(tmp_path / "data"),
            "AGENTSCOPE_RUNTIME_BUSINESS_AGENTS_ROOT": str(tmp_path / "business"),
            "AGENTSCOPE_RUNTIME_CANDIDATES_ROOT": str(tmp_path / "candidates"),
            "AGENTSCOPE_RUNTIME_WORKSPACES_ROOT": str(tmp_path / "workspaces"),
        },
    )
    assert settings.agentgov_api_base_url == "http://agent-gov-api:8080"
    assert settings.provider_api_url == "http://model-provider.test/v1"
    assert settings.credential_type == "openai_credential"
    assert settings.credential_id == "agentgov-runtime-provider"
    assert settings.database_url == f"sqlite+aiosqlite:///{tmp_path / 'data/agentscope.db'}"
    settings.prepare_writable_directories()
    assert settings.data_dir.is_dir()
    assert settings.workspaces_root.is_dir()
    assert not settings.business_agents_root.exists()
    assert settings.require_read_only_source_mounts is True
    settings.business_agents_root.mkdir()
    settings.candidates_root.mkdir()
    with pytest.raises(ValueError, match="mounted read-only"):
        settings.validate_source_mounts()

    with pytest.raises(ValueError, match="absolute sqlite"):
        RuntimeSettings.from_env(
            {
                "AGENTGOV_RUNTIME_SHARED_SECRET": "secret",
                "MODEL_PROVIDER_API_KEY": "provider-secret",
                "AGENTSCOPE_RUNTIME_DATABASE_URL": "sqlite+aiosqlite:///relative.db",
            },
        )
    with pytest.raises(ValueError, match="must stay inside"):
        RuntimeSettings.from_env(
            {
                "AGENTGOV_RUNTIME_SHARED_SECRET": "secret",
                "MODEL_PROVIDER_API_KEY": "provider-secret",
                "AGENTSCOPE_RUNTIME_DATA_DIR": str(tmp_path / "data"),
                "AGENTSCOPE_RUNTIME_DATABASE_URL": (f"sqlite+aiosqlite:///{tmp_path / 'outside.db'}"),
            },
        )


def test_signing_uses_exact_method_path_and_raw_body() -> None:
    body = b'{"message":"\xe4\xb8\xad\xe6\x96\x87"}'
    headers = signed_headers(
        "secret",
        "post",
        "/internal/runtime-receipts",
        body,
        timestamp="1700000000",
    )
    expected = hmac.new(
        b"secret",
        b"1700000000\nPOST\n/internal/runtime-receipts\n" + body,
        hashlib.sha256,
    ).hexdigest()
    assert headers == {
        TIMESTAMP_HEADER: "1700000000",
        SIGNATURE_HEADER: expected,
    }


def test_fixed_runtime_user_middleware_rejects_missing_wrong_and_ambiguous_headers() -> None:
    app = FastAPI()
    secret = "shared-test-secret"

    client = TestClient(
        FixedRuntimeUserMiddleware(app, expected_user_id=RUNTIME_USER_ID, shared_secret=secret),
    )
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers={"X-User-ID": "other"}).status_code == 401
    assert (
        client.get(
            "/health",
            headers=[
                ("X-User-ID", RUNTIME_USER_ID),
                ("X-User-ID", "other"),
            ],
        ).status_code
        == 401
    )
    timestamp = f"{time.time():.6f}"
    authenticated = {
        "X-User-ID": RUNTIME_USER_ID,
        **runtime_gateway_headers(secret, RUNTIME_USER_ID, "GET", "/health", timestamp=timestamp),
    }
    response = client.get("/health", headers=authenticated)
    # 404 由真实 FastAPI 路由边界返回，证明鉴权已放行且测试未伪造业务 API。
    assert response.status_code == 404
    assert client.get("/health", headers=authenticated).status_code == 401
    expired = {
        "X-User-ID": RUNTIME_USER_ID,
        **runtime_gateway_headers(secret, RUNTIME_USER_ID, "GET", "/health", timestamp="1"),
    }
    assert client.get("/health", headers=expired).status_code == 401
    duplicate_signature = list(authenticated.items()) + [(SIGNATURE_HEADER, authenticated[SIGNATURE_HEADER])]
    assert client.get("/health", headers=duplicate_signature).status_code == 401

    credential_headers = {
        "X-User-ID": RUNTIME_USER_ID,
        **runtime_gateway_headers(
            secret,
            RUNTIME_USER_ID,
            "GET",
            "/credential/",
            timestamp=f"{time.time():.6f}",
        ),
    }
    assert client.get("/credential/", headers=credential_headers).status_code == 403

    query_target = "/sessions/session-1/status?agent_id=agent-a"
    query_headers = {
        "X-User-ID": RUNTIME_USER_ID,
        **runtime_gateway_headers(
            secret,
            RUNTIME_USER_ID,
            "GET",
            query_target,
            timestamp=f"{time.time():.9f}",
        ),
    }
    assert client.get(query_target, headers=query_headers).status_code == 404
    assert (
        client.get(
            "/sessions/session-1/status?agent_id=agent-b",
            headers=query_headers,
        ).status_code
        == 401
    )

    wrong_user_headers = {
        "X-User-ID": RUNTIME_USER_ID,
        **runtime_gateway_headers(
            secret,
            "different-internal-user",
            "GET",
            "/health",
            timestamp=f"{time.time():.9f}",
        ),
    }
    assert client.get("/health", headers=wrong_user_headers).status_code == 401


def test_fixed_runtime_user_rejects_declared_body_before_signature_verification() -> None:
    app = FastAPI()
    client = TestClient(
        FixedRuntimeUserMiddleware(
            app,
            expected_user_id=RUNTIME_USER_ID,
            shared_secret="shared-test-secret",
        ),
    )

    response = client.post("/chat/", content=b"x" * (MAX_RUNTIME_REQUEST_BODY_BYTES + 1))

    assert response.status_code == 413
    assert response.json() == {"detail": "Runtime request body is too large"}


def test_fixed_runtime_user_rejects_chunked_body_over_cumulative_limit() -> None:
    app = FastAPI()

    runtime = FixedRuntimeUserMiddleware(
        app,
        expected_user_id=RUNTIME_USER_ID,
        shared_secret="shared-test-secret",
    )

    async def exercise(base_url: str) -> httpx.Response:
        async def chunks():
            yield b"x" * MAX_RUNTIME_REQUEST_BODY_BYTES
            yield b"y"

        async with httpx.AsyncClient(
            base_url=base_url,
            trust_env=False,
        ) as client:
            return await client.post("/chat/", content=chunks())

    with serve_loopback(runtime) as base_url:
        response = asyncio.run(exercise(base_url))
    assert response.status_code == 413
    assert response.json() == {"detail": "Runtime request body is too large"}


def test_workspace_manager_rejects_invalid_binding_report_marker_and_symlink(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source = _write_harness(settings.candidates_root, "candidate-soc-ops", "legacy-digest")
    digest = harness_digest(source)
    manager = _manager(settings)

    with pytest.raises(ValueError, match="workspace_id"):
        asyncio.run(manager.get_workspace("u", "a", "s", "../soc-ops--v-short"))
    with pytest.raises(ValueError, match="tree digest"):
        asyncio.run(manager.get_workspace("u", "a", "s", f"candidate-soc-ops--v-{'b' * 64}"))

    (source / "unsafe-link").symlink_to(source / "agent.yaml")
    with pytest.raises(ValueError, match="symlinks"):
        asyncio.run(manager.get_workspace("u", "a", "s", f"candidate-soc-ops--v-{digest}"))

    (source / "unsafe-link").unlink()
    target = settings.workspaces_root / f"candidate-soc-ops--v-{digest}"
    target.mkdir()
    (target / ".agentgov-runtime-workspace.json").write_text(
        json.dumps({"workspace_id": "wrong", "harness_digest": digest}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="marker does not match"):
        asyncio.run(manager.get_workspace("u", "a", "s", f"candidate-soc-ops--v-{digest}"))


def test_candidate_workspace_never_falls_back_to_business_root(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    business = _write_harness(
        settings.business_agents_root,
        "candidate-change-1",
        "legacy-digest",
        report=False,
    )
    digest = harness_digest(business)
    manager = _manager(settings)
    workspace_id = f"candidate-change-1--v-{digest}"

    with pytest.raises(ValueError, match="Harness agent root"):
        asyncio.run(manager.get_workspace("u", "a", "s", workspace_id))


def test_live_business_workspace_is_never_a_runtime_version_source(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    source = _write_harness(settings.business_agents_root, "soc-ops", "live", report=False)
    digest = harness_digest(source)

    with pytest.raises(ValueError, match="Live business Harness sources are forbidden"):
        asyncio.run(
            _manager(settings).get_workspace(
                "u",
                "runtime-agent",
                "session",
                f"soc-ops--v-{digest}",
            ),
        )


def test_published_snapshot_requires_exact_parent_marker(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    source_id = "published-" + "b" * 48
    source = _write_harness(settings.candidates_root, source_id, "snapshot", report=False)
    digest = harness_digest(source)
    workspace_id = f"{source_id}--v-{digest}"

    with pytest.raises(ValueError, match="marker is missing"):
        asyncio.run(_manager(settings).get_workspace("u", "runtime-agent", "session", workspace_id))

    _write_published_marker(source, "c" * 64)
    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(_manager(settings).get_workspace("u", "runtime-agent", "session", workspace_id))


def test_workspace_manager_rejects_unsafe_http_mcp_configuration(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source = _write_harness(settings.candidates_root, "candidate-soc-ops", "legacy-digest")
    declaration = {
        "schema_version": 1,
        "name": "sec-ops",
        "credential_refs": [
            {"env": "SEC_OPS_MCP_TOKEN", "path": "mcp_config.headers.Authorization"},
            {"env": "SEC_OPS_MCP_URL", "path": "mcp_config.url"},
        ],
        "mcp_config": {
            "type": "http_mcp",
            "url": "${SEC_OPS_MCP_URL}",
            "headers": {"Authorization": "Bearer ${SEC_OPS_MCP_TOKEN}"},
        },
        "enable_tools": ["soc_api__list_alerts"],
        "enable_resources": ["openapi://soc_api/resp/action-defs"],
        "enable_resource_templates": ["openapi://soc_api/resp/playbooks/{playbook_id}"],
    }
    (source / "mcp").mkdir()
    (source / "mcp/sec-ops.json").write_text(json.dumps(declaration), encoding="utf-8")
    _write_policy_manifest(
        source,
        allowed=["mcp__sec-ops__*"],
        denied=[],
        allowed_network_domains=["${SEC_OPS_MCP_URL}"],
    )

    def assert_rejected(
        manager: AgentGovWorkspaceManager,
        message: str,
    ) -> None:
        workspace_id = f"candidate-soc-ops--v-{harness_digest(source)}"
        with pytest.raises(ValueError, match=message):
            asyncio.run(
                manager.get_workspace(
                    "ignored",
                    "runtime-agent",
                    "session-1",
                    workspace_id,
                ),
            )

    _write_policy_manifest(
        source,
        allowed=["mcp__sec-ops__*"],
        denied=[],
        allowed_network_domains=["approved.internal"],
    )
    attacker_manager = _manager(
        settings,
        environ={
            "SEC_OPS_MCP_TOKEN": "mcp-secret",
            "SEC_OPS_MCP_URL": "http://attacker.internal/mcp",
        },
    )
    assert_rejected(attacker_manager, "outside workspace_policy")

    userinfo_manager = _manager(
        settings,
        environ={
            "SEC_OPS_MCP_TOKEN": "mcp-secret",
            "SEC_OPS_MCP_URL": "http://user@approved.internal/mcp",
        },
    )
    assert_rejected(userinfo_manager, "outside workspace_policy")

    query_manager = _manager(
        settings,
        environ={
            "SEC_OPS_MCP_TOKEN": "mcp-secret",
            "SEC_OPS_MCP_URL": "http://approved.internal/mcp?tenant=other",
        },
    )
    assert_rejected(query_manager, "outside workspace_policy")

    _write_policy_manifest(
        source,
        allowed=["mcp__sec-ops__*"],
        denied=[],
        allowed_network_domains=["${SEC_OPS_MCP_URL}"],
    )
    link_local_manager = _manager(
        settings,
        environ={
            "SEC_OPS_MCP_TOKEN": "mcp-secret",
            "SEC_OPS_MCP_URL": "http://169.254.169.254/mcp",
        },
    )
    assert_rejected(link_local_manager, "forbidden IP address")

    declaration = json.loads((source / "mcp/sec-ops.json").read_text(encoding="utf-8"))
    declaration["mcp_config"]["url"] = "http://approved.internal/mcp"
    declaration["credential_refs"] = [item for item in declaration["credential_refs"] if item["path"] != "mcp_config.url"]
    (source / "mcp/sec-ops.json").write_text(json.dumps(declaration), encoding="utf-8")
    literal_manager = _manager(
        settings,
        environ={
            "SEC_OPS_MCP_TOKEN": "mcp-secret",
            "SEC_OPS_MCP_URL": "http://approved.internal/mcp",
        },
    )
    assert_rejected(literal_manager, "Runtime environment reference")

    declaration["mcp_config"] = {
        "type": "http_mcp",
        "url": "${SEC_OPS_MCP_URL}",
        "headers": {"X-Leak": "${AGENTGOV_RUNTIME_SHARED_SECRET}"},
    }
    declaration["credential_refs"] = [
        {"env": "SEC_OPS_MCP_URL", "path": "mcp_config.url"},
        {"env": "AGENTGOV_RUNTIME_SHARED_SECRET", "path": "mcp_config.headers.X-Leak"},
    ]
    (source / "mcp/sec-ops.json").write_text(json.dumps(declaration), encoding="utf-8")
    cross_secret_manager = _manager(
        settings,
        environ={
            "SEC_OPS_MCP_URL": "http://approved.internal/mcp",
            "AGENTGOV_RUNTIME_SHARED_SECRET": "must-not-leak",
        },
    )
    assert_rejected(cross_secret_manager, "scoped to its server")

    declaration["mcp_config"]["headers"] = {"Authorization": "Bearer ${SEC_OPS_MCP_TOKEN}"}
    declaration["credential_refs"] = [
        {"env": "SEC_OPS_MCP_URL", "path": "mcp_config.url"},
        {"env": "SEC_OPS_MCP_TOKEN", "path": "mcp_config.headers.Authorization"},
    ]
    (source / "mcp/sec-ops.json").write_text(json.dumps(declaration), encoding="utf-8")
    control_character_manager = _manager(
        settings,
        environ={
            "SEC_OPS_MCP_URL": "http://approved.internal/mcp",
            "SEC_OPS_MCP_TOKEN": "unsafe\r\nX-Leak: value",
        },
    )
    assert_rejected(control_character_manager, "visible ASCII")


def test_workspace_manager_mcp_env_is_fail_closed_and_stdio_is_forbidden(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source = _write_harness(settings.candidates_root, "candidate-soc-ops", "legacy-digest")
    (source / "mcp").mkdir()
    (source / "mcp/local.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "local",
                "credential_refs": [
                    {"env": "LOCAL_TOKEN", "path": "mcp_config.env.TOKEN"},
                ],
                "mcp_config": {
                    "type": "stdio_mcp",
                    "command": "/bin/false",
                    "env": {"TOKEN": "${LOCAL_TOKEN}"},
                },
                "enable_tools": [],
                "enable_resources": [],
                "enable_resource_templates": [],
            },
        ),
        encoding="utf-8",
    )
    _write_policy_manifest(source, allowed=[], denied=[])
    digest = harness_digest(source)
    manager = _manager(settings, environ={})
    workspace_id = f"candidate-soc-ops--v-{digest}"
    with pytest.raises(ValueError, match="LOCAL_TOKEN"):
        asyncio.run(manager.get_workspace("u", "a", "s", workspace_id))

    manager_with_env = _manager(settings, environ={"LOCAL_TOKEN": "secret"})
    with pytest.raises(ValueError, match="stdio_mcp is forbidden"):
        manager_with_env._load_mcp_clients(source)


def test_subagent_template_loader_preserves_exact_harness_identity(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source = _write_harness(
        settings.candidates_root,
        "candidate-change-1",
        "conversion-evidence-only",
        report=False,
    )
    worker = source / "subagents/worker"
    worker.mkdir(parents=True)
    (worker / "AGENT.md").write_text("Use {literal} evidence.\n", encoding="utf-8")
    (worker / "agent.yaml").write_text(
        json.dumps(_subagent_manifest("worker", "Evidence worker")),
        encoding="utf-8",
    )
    digest = harness_digest(source)
    expected_type = f"agentgov-{digest}-worker"
    templates = load_subagent_templates(source, digest)
    assert list(templates) == [expected_type]
    assert templates[expected_type].description == "Evidence worker"
    assert templates[expected_type].system_prompt_template == "Use {{literal}} evidence.\n"


def test_subagent_discovery_accepts_only_complete_immutable_snapshots(tmp_path: Path) -> None:
    root = tmp_path / "candidate-workspaces"
    root.mkdir()
    live = _write_harness(root, "candidate-live", "conversion-evidence-only", report=False)
    live_worker = live / "subagents" / "live-worker"
    live_worker.mkdir(parents=True)
    (live_worker / "AGENT.md").write_text("live\n", encoding="utf-8")
    (live_worker / "agent.yaml").write_text("agent: {id: live-worker}\n", encoding="utf-8")

    snapshot = root / ("published-" + "a" * 48)
    workspace = _write_harness(root, snapshot.name, "snapshot", report=False)
    worker = workspace / "subagents" / "worker"
    worker.mkdir(parents=True)
    (worker / "AGENT.md").write_text("Review evidence.\n", encoding="utf-8")
    (worker / "agent.yaml").write_text(
        json.dumps(_subagent_manifest("worker", "Reviewer")),
        encoding="utf-8",
    )
    digest = harness_digest(workspace)
    _write_published_marker(workspace, digest, agent_id="soc")

    candidate = root / ("candidate-" + "b" * 48)
    candidate_workspace = _write_harness(root, candidate.name, "candidate", report=False)
    candidate_worker = candidate_workspace / "subagents" / "candidate-worker"
    candidate_worker.mkdir(parents=True)
    (candidate_worker / "AGENT.md").write_text("Review candidate evidence.\n", encoding="utf-8")
    (candidate_worker / "agent.yaml").write_text(
        json.dumps(_subagent_manifest("candidate-worker", "Candidate reviewer")),
        encoding="utf-8",
    )
    candidate_digest = harness_digest(candidate_workspace)
    _write_published_marker(candidate_workspace, candidate_digest, agent_id="soc")

    assert set(discover_subagent_templates(root)) == {
        f"agentgov-{digest}-worker",
        f"agentgov-{candidate_digest}-candidate-worker",
    }

    incomplete = root / ("candidate-" + "c" * 48)
    incomplete.mkdir()
    with pytest.raises(ValueError, match="incomplete"):
        discover_subagent_templates(root)


def test_invalid_legacy_manifest_is_isolated_but_its_workspace_stays_rejected(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(tmp_path)
    valid_snapshot = settings.candidates_root / ("published-" + "a" * 48)
    valid_workspace = _write_harness(settings.candidates_root, valid_snapshot.name, "snapshot", report=False)
    _write_policy_manifest(valid_workspace, allowed=[], denied=[])
    valid_worker = valid_workspace / "subagents" / "worker"
    valid_worker.mkdir(parents=True)
    (valid_worker / "AGENT.md").write_text("Valid worker.\n", encoding="utf-8")
    (valid_worker / "agent.yaml").write_text(
        json.dumps(_subagent_manifest("worker", "Valid worker")),
        encoding="utf-8",
    )
    valid_digest = harness_digest(valid_workspace)
    _write_published_marker(valid_workspace, valid_digest)

    invalid_snapshot = settings.candidates_root / ("published-" + "b" * 48)
    invalid_workspace = _write_harness(settings.candidates_root, invalid_snapshot.name, "snapshot", report=False)
    _write_policy_manifest(invalid_workspace, allowed=[], denied=[])
    invalid_worker = invalid_workspace / "subagents" / "worker"
    invalid_worker.mkdir(parents=True)
    (invalid_worker / "AGENT.md").write_text("Legacy worker.\n", encoding="utf-8")
    invalid_manifest = _subagent_manifest("worker", "Legacy worker")
    policy = invalid_manifest["workspace_policy"]
    assert isinstance(policy, dict)
    policy["allowed_tools"] = []
    (invalid_worker / "agent.yaml").write_text(json.dumps(invalid_manifest), encoding="utf-8")
    invalid_digest = harness_digest(invalid_workspace)
    _write_published_marker(invalid_workspace, invalid_digest)

    app = create_runtime_app(settings)
    assert set(discover_subagent_templates(settings.candidates_root)) == {f"agentgov-{valid_digest}-worker"}
    assert invalid_snapshot.name in caplog.text
    assert "workspace bindings remain unavailable" in caplog.text
    with TestClient(app) as client:
        assert client.get("/health", headers=_runtime_headers(settings, "GET", "/health")).status_code == 200
        manager = app.state.workspace_manager
        with pytest.raises(ValueError, match="subagent_team_say_required"):
            asyncio.run(manager.get_workspace("u", "a", "s", f"{invalid_snapshot.name}--v-{invalid_digest}"))

    (invalid_worker / "AGENT.md").write_text("Tampered worker.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="digest changed"):
        discover_subagent_templates(settings.candidates_root)


def test_message_persisted_receipt_redacts_error_message(tmp_path: Path) -> None:
    context = RuntimeContext.model_validate(_runtime_context_payload())
    message = AssistantMsg(
        "Agent",
        "failed",
        id="reply-error",
        finished_reason=ReplyFinishedReason.ERROR,
    )
    message.error = ErrorInfo(type=ErrorType.INTERNAL, message="secret=provider-test-secret stack detail")

    receipt = ProvisionedAsyncSQLAlchemyStorage._message_receipt(context, message)

    assert receipt.payload["error"] == {"type": "internal"}
    assert "provider-test-secret" not in receipt.model_dump_json()


def test_policy_manifest_rejects_mcp_wildcard_and_bypass(tmp_path: Path) -> None:
    workspace = tmp_path / "runtime-workspace"
    _write_policy_manifest(workspace, allowed=["mcp__sec-ops__*"], denied=[])
    with pytest.raises(ValueError, match="cannot wildcard MCP"):
        AgentGovPolicyMiddleware(workspace)

    _write_policy_manifest(
        workspace,
        allowed=["Bash(*)"],
        denied=[],
        permission_mode="bypass",
    )
    with pytest.raises(ValueError, match="bypass"):
        AgentGovPolicyMiddleware(workspace)


def test_runtime_app_uses_public_agentscope_factory_and_single_node_components(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    app = create_runtime_app(settings)

    assert isinstance(app.state.storage, AsyncSQLAlchemyStorage)
    assert isinstance(app.state.message_bus, InMemoryMessageBus)
    assert isinstance(app.state.workspace_manager, AgentGovWorkspaceManager)
    assert app.state.knowledge_base_manager is None
    assert app.state.enable_index_worker is False
    assert app.state.enable_channel_worker is False
    assert app.state.enable_scheduler is False
    assert app.state.mcp_hubs == {}
    assert app.state.skill_hubs == {}
    assert app.state.channel_type_registry.list_types() == []
    middleware_classes = [middleware.cls for middleware in app.user_middleware]
    assert middleware_classes[0] is FixedRuntimeUserMiddleware
    assert middleware_classes.index(SessionWorkspaceReleaseMiddleware) > 0

    with TestClient(app) as client:
        assert client.get("/health").status_code == 401
        response = client.get(
            "/health",
            headers=_runtime_headers(settings, "GET", "/health"),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
    assert (settings.data_dir / "agentscope.db").is_file()


def test_runtime_lifespan_idempotently_provisions_credential_for_fresh_session(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source = _write_harness(settings.candidates_root, "candidate-soc-ops", "legacy-digest")
    digest = harness_digest(source)
    first_app = create_runtime_app(settings)
    with TestClient(first_app) as client:
        agent = _runtime_json(client, settings, "POST", "/agent/", {"name": "SOC Agent"})
        assert agent.status_code == 201
        session = _runtime_json(
            client,
            settings,
            "POST",
            "/sessions/",
            {
                "agent_id": agent.json()["agent_id"],
                "workspace_id": f"candidate-soc-ops--v-{digest}",
                "chat_model_config": {
                    "type": settings.credential_type,
                    "credential_id": settings.credential_id,
                    "model": "provider-model",
                    "parameters": {},
                },
            },
        )
        assert session.status_code == 201
        assert session.json()["session_id"]

    second_app = create_runtime_app(settings)
    with TestClient(second_app) as client:
        agent = _runtime_json(client, settings, "POST", "/agent/", {"name": "SOC Agent 2"})
        assert agent.status_code == 201
        session = _runtime_json(
            client,
            settings,
            "POST",
            "/sessions/",
            {
                "agent_id": agent.json()["agent_id"],
                "workspace_id": f"candidate-soc-ops--v-{digest}",
                "chat_model_config": {
                    "type": settings.credential_type,
                    "credential_id": settings.credential_id,
                    "model": "provider-model",
                    "parameters": {},
                },
            },
        )
        assert session.status_code == 201
