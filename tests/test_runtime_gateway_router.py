from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from agentscope_runtime.service import create_runtime_app
from agentscope_runtime.settings import RuntimeSettings
from app.routers.error_handlers import register_error_handlers
from app.runtime.agent_workspace_package_schemas import (
    NativeAgentDataInput,
    NativeContextConfig,
    NativeInviteConfig,
    NativeReactConfig,
)
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.published_harness_preparation import prepare_published_harnesses
from app.runtime_gateway.client import (
    AgentScopeRuntimeClient,
    RuntimeJsonResponse,
    RuntimeUpstreamError,
    canonical_session_id_from_view,
)
from app.runtime_gateway.contracts import (
    GOVERNED_EVIDENCE_ROOT_METADATA_KEY,
    RuntimeChatRequest,
    RuntimeSessionCreateRequest,
)
from app.runtime_gateway.native_schema import _project_native_agent_schema
from app.runtime_gateway.router import _project_session_view, _runtime_stream_body, create_runtime_router
from app.runtime_gateway.session_resources import (
    RuntimeSessionRenameRequest,
    _project_session_rename,
    _project_workspace_mcps,
    _project_workspace_skills,
    _project_workspace_status,
)
from app.runtime_gateway.store import RuntimeObjectNotFound, RuntimeRunStore, RuntimeStateConflict, harness_digest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from app_test_utils import load_test_app
from business_agent_test_utils import ORDINARY_TEST_AGENT_ID
from runtime_gateway_test_utils import store_with_agent_version as _store
from runtime_loopback import serve_loopback

SECRET = "runtime-router-real-boundary-secret"
PUBLIC_API_KEY = "runtime-public-read-auth-secret"
_SCALAR_SCHEMA_KEYS = ("type", "anyOf")


def _form_section_schema(model: type[BaseModel]) -> dict[str, object]:
    model_properties = model.model_json_schema()["properties"]
    return {
        "type": "object",
        "title": model.__name__,
        "description": f"{model.__name__} fields",
        "properties": {name: {key: value[key] for key in _SCALAR_SCHEMA_KEYS if key in value} for name, value in model_properties.items()},
    }


def _renderable_native_agent_schema() -> dict[str, object]:
    return {
        "type": "object",
        "title": "AgentData",
        "description": "Agent form fields",
        "required": ["name", "context_config", "react_config"],
        "properties": {
            "name": {"type": "string"},
            "system_prompt": {"type": "string", "format": "textarea"},
            "context_config": _form_section_schema(NativeContextConfig),
            "react_config": _form_section_schema(NativeReactConfig),
            "invite_config": _form_section_schema(NativeInviteConfig),
        },
    }


def _bind_session(store: RuntimeRunStore, *, session_id: str = "session-a") -> None:
    store.bind_session(
        session_id=session_id,
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
    )


class _ExactChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


def test_runtime_stream_starts_with_readiness_comment_then_preserves_upstream_bytes() -> None:
    upstream_chunks = (
        b'data: {"id":"known","type":"TEXT_BLOCK_DELTA"}\n\n',
        b'data: {"id":"future","type":"FUTURE_AGENT_EVENT","value":"\xe4\xb8\xad"}\n\n',
    )

    async def collect() -> tuple[list[bytes], bool]:
        upstream = httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_ExactChunkStream(upstream_chunks),
        )
        chunks = [chunk async for chunk in _runtime_stream_body(upstream)]
        return chunks, upstream.is_closed

    chunks, closed = asyncio.run(collect())

    assert chunks == [b":\n\n", *upstream_chunks]
    assert b"".join(chunks[1:]) == b"".join(upstream_chunks)
    assert closed is True


def _runtime_settings(tmp_path: Path) -> RuntimeSettings:
    data_dir = tmp_path / "runtime-data"
    business_root = tmp_path / "business-agents"
    candidates_root = tmp_path / "candidates"
    workspaces_root = tmp_path / "workspaces"
    for directory in (data_dir, business_root, candidates_root, workspaces_root):
        directory.mkdir(parents=True, exist_ok=True)
    return RuntimeSettings(
        shared_secret=SECRET,
        provider_api_key="runtime-router-provider-key",
        agentgov_api_base_url="http://127.0.0.1:9",
        data_dir=data_dir,
        business_agents_root=business_root,
        candidates_root=candidates_root,
        workspaces_root=workspaces_root,
        database_url=f"sqlite+aiosqlite:///{data_dir / 'agentscope.db'}",
    )


def test_session_projection_keeps_agentgov_active_run_outside_agentscope_session() -> None:
    upstream = {
        "session": {"id": "session-a", "agent_id": "runtime-a"},
        "is_running": False,
        "status": "idle",
    }

    projected = _project_session_view(
        upstream,
        {"session-a": "run-active"},
        session_id=canonical_session_id_from_view(upstream),
    )

    assert projected == {**upstream, "active_run_id": "run-active"}
    assert "active_run_id" not in upstream["session"]


@pytest.mark.parametrize(
    "invalid_view",
    [
        {"session_id": "legacy-top-level"},
        {"session": {"id": ""}, "session_id": "legacy-top-level"},
        {"session": {}},
        {},
        None,
    ],
    ids=("top-level-only", "empty-canonical", "missing-id", "missing-session", "not-object"),
)
def test_session_identity_rejects_every_noncanonical_shape(invalid_view: object) -> None:
    with pytest.raises(RuntimeUpstreamError) as rejected:
        canonical_session_id_from_view(invalid_view)

    assert rejected.value.status_code == 502
    assert rejected.value.body == b'{"detail":"Runtime returned invalid Session entry"}'


class _SessionListClient:
    def __init__(self, body: object) -> None:
        self.body = body

    async def request_json(self, method: str, path: str, **_kwargs) -> RuntimeJsonResponse:
        assert (method, path) == ("GET", "/sessions/")
        return RuntimeJsonResponse(200, {"content-type": "application/json"}, self.body)


class _SessionListProvisioner:
    def __init__(self, store: RuntimeRunStore) -> None:
        self.store = store

    def require_session(self, session_id: str, runtime_agent_id: str):
        return self.store.get_session(
            session_id,
            runtime_agent_id=runtime_agent_id,
        )


def test_session_list_route_fails_closed_on_top_level_session_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _bind_session(store)
    api = FastAPI()
    register_error_handlers(api)
    api.include_router(
        create_runtime_router(
            client=_SessionListClient(
                {"sessions": [{"session_id": "session-a"}], "total": 1},
            ),  # type: ignore[arg-type]
            store=store,
            provisioner=_SessionListProvisioner(store),  # type: ignore[arg-type]
            model_type="openai_credential",
            credential_id="provider",
            model_name="model",
            model_parameters={},
            require_api_key=lambda: None,
        ),
    )

    with TestClient(api) as client:
        response = client.get(
            "/api/runtime/sessions/",
            params={"governance_agent_id": "agent-a"},
        )

    assert response.status_code == 502
    assert response.json() == {
        "detail": "AgentScope Runtime request failed",
        "error_code": "RUNTIME_UPSTREAM_ERROR",
    }


def _bind_published_agent(module, *, agent_id: str, runtime_agent_id: str) -> tuple[str, str]:
    record = module.agent_registry_store.get_agent(agent_id)
    assert record is not None
    versions = module.agent_governance._store_for(agent_id)
    version_id = versions.current_commit_sha()
    assert version_id is not None
    digest = harness_digest(Path(record.workspace_dir))
    snapshot = module.harness_snapshots.require_existing(
        agent_id=agent_id,
        agent_version_id=version_id,
        expected_digest=digest,
    )
    module.run_store.bind_agent_version(
        agent_id=agent_id,
        agent_version_id=version_id,
        digest=digest,
        runtime_agent_id=runtime_agent_id,
        source_id=snapshot.source_id,
    )
    return version_id, digest


def test_runtime_session_create_contract_forbids_client_model_configuration() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RuntimeSessionCreateRequest.model_validate(
            {
                "agent_id": "runtime-a",
                "name": "Session",
                "model": "attacker-model",
            },
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"name": ""},
        {"name": "   "},
        {"name": None},
        {"name": "renamed", "permission_mode": "bypassPermissions"},
        {"name": "renamed", "cwd": "/tmp/attacker"},
    ],
    ids=("empty", "blank", "null", "permission-mode", "cwd"),
)
def test_session_rename_contract_only_accepts_a_nonblank_name(payload: object) -> None:
    with pytest.raises(ValidationError):
        RuntimeSessionRenameRequest.model_validate(payload)


def test_session_rename_response_is_a_minimal_safe_projection() -> None:
    projected = _project_session_rename(
        {
            "id": "session-a",
            "config": {
                "name": "Reviewed title",
                "permission_mode": "dont_ask",
                "chat_model_config": {"credential_id": "private-credential-reference"},
            },
            "state": {"context": [{"content": "private message"}]},
        },
        session_id="session-a",
        expected_name="Reviewed title",
    )

    assert projected.model_dump() == {"session_id": "session-a", "name": "Reviewed title"}


def test_session_rename_sends_only_agent_id_query_to_real_runtime(
    process_environment,
    tmp_path: Path,
) -> None:
    process_environment.set("RUNTIME_CANDIDATES_DIR", str(tmp_path / "candidate-workspaces"))
    module = load_test_app(process_environment, tmp_path)
    assert prepare_published_harnesses(module.settings) == 1
    version_id, digest = _bind_published_agent(
        module,
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
        runtime_agent_id="runtime-rename-query",
    )
    module.run_store.bind_session(
        session_id="session-rename-query",
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
        agent_version_id=version_id,
        runtime_agent_id="runtime-rename-query",
        digest=digest,
    )
    observed_queries: list[tuple[str, bytes]] = []
    runtime_app = create_runtime_app(_runtime_settings(tmp_path / "native-runtime"))

    @runtime_app.middleware("http")
    async def capture_runtime_query(request: Request, call_next):
        if request.method == "PATCH" and request.url.path == "/sessions/session-rename-query":
            observed_queries.append((request.url.path, request.scope["query_string"]))
        return await call_next(request)

    with serve_loopback(runtime_app, lifespan="on") as runtime_url:

        async def exercise() -> httpx.Response:
            runtime_client = AgentScopeRuntimeClient(runtime_url, shared_secret=SECRET)
            api = FastAPI()
            register_error_handlers(api)
            api.include_router(
                create_runtime_router(
                    client=runtime_client,
                    store=module.run_store,
                    provisioner=module.provisioner,
                    model_type="openai_credential",
                    credential_id="provider",
                    model_name="model",
                    model_parameters={},
                    require_api_key=lambda: None,
                ),
            )
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=api),
                    base_url="http://agentgov.test",
                ) as client:
                    return await client.patch(
                        "/api/runtime/sessions/session-rename-query",
                        params={"agent_id": "runtime-rename-query"},
                        json={"name": "Reviewed title"},
                    )
            finally:
                await runtime_client.close()

        response = asyncio.run(exercise())

    assert response.status_code == 404
    assert observed_queries == [
        ("/sessions/session-rename-query", b"agent_id=runtime-rename-query"),
    ]


def test_workspace_resource_projections_remove_paths_configuration_and_skill_bodies() -> None:
    status = _project_workspace_status(
        {
            "workdir": "/private/runtime/workspace-a",
            "cwd": "/private/runtime/workspace-a",
            "git": {"staged": 0, "unstaged": 1, "untracked": 0, "conflicted": 0},
        },
    )
    mcps = _project_workspace_mcps(
        [
            {
                "name": "local-mcp",
                "is_stateful": False,
                "is_healthy": False,
                "error": "Authorization: private-secret",
                "mcp_config": {"headers": {"Authorization": "private-secret"}},
                "tools": [{"name": "lookup", "description": "Lookup a resource", "inputSchema": {}}],
            },
        ],
    )
    skills = _project_workspace_skills(
        [
            {
                "name": "review",
                "description": "Review one input",
                "markdown": "private instructions",
                "skill_dir": "/private/runtime/skills/review",
            },
        ],
    )

    assert status.model_dump() == {
        "available": True,
        "at_workspace_root": True,
        "git_repository": True,
        "git_dirty": True,
    }
    assert [item.model_dump() for item in mcps] == [
        {
            "name": "local-mcp",
            "is_stateful": False,
            "is_healthy": False,
            "error": "connection_failed",
            "tools": [{"name": "lookup", "description": "Lookup a resource"}],
        },
    ]
    assert [item.model_dump() for item in skills] == [
        {"name": "review", "description": "Review one input"},
    ]


def test_native_agent_schema_projection_accepts_only_the_supported_public_field_set() -> None:
    projected = _project_native_agent_schema({"schema": _renderable_native_agent_schema()})
    assert set(projected.schema_["properties"]) == {
        "name",
        "system_prompt",
        "context_config",
        "react_config",
        "invite_config",
    }

    assert set(projected.schema_["properties"]["context_config"]["properties"]) == set(
        NativeContextConfig.model_fields,
    )


def test_native_agent_schema_projection_rejects_unowned_or_unrenderable_drift() -> None:
    top_level_drift = _renderable_native_agent_schema()
    top_level_properties = top_level_drift["properties"]
    assert isinstance(top_level_properties, dict)
    top_level_properties["id"] = {"type": "string"}

    summary_schema_drift = _renderable_native_agent_schema()
    sections = summary_schema_drift["properties"]
    assert isinstance(sections, dict)
    context_schema = sections["context_config"]
    assert isinstance(context_schema, dict)
    context_properties = context_schema["properties"]
    assert isinstance(context_properties, dict)
    context_properties["summary_schema"] = {"type": "object", "properties": {}}

    nested_object_drift = _renderable_native_agent_schema()
    nested_sections = nested_object_drift["properties"]
    assert isinstance(nested_sections, dict)
    nested_context = nested_sections["context_config"]
    assert isinstance(nested_context, dict)
    nested_fields = nested_context["properties"]
    assert isinstance(nested_fields, dict)
    nested_fields["compression_prompt"] = {"type": "object", "properties": {}}

    for unsafe_schema in (top_level_drift, summary_schema_drift, nested_object_drift):
        with pytest.raises(RuntimeUpstreamError) as rejected:
            _project_native_agent_schema({"schema": unsafe_schema})
        assert b"unsupported Agent schema" in rejected.value.body


def test_native_agent_schema_route_reads_the_real_pinned_runtime_contract(tmp_path: Path) -> None:
    settings = _runtime_settings(tmp_path)
    store = _store(tmp_path)

    with serve_loopback(create_runtime_app(settings), lifespan="on") as runtime_url:

        async def exercise() -> httpx.Response:
            runtime_client = AgentScopeRuntimeClient(runtime_url, shared_secret=SECRET)
            api = FastAPI()
            api.include_router(
                create_runtime_router(
                    client=runtime_client,
                    store=store,
                    provisioner=object(),  # type: ignore[arg-type]
                    model_type="openai_credential",
                    credential_id="provider",
                    model_name="model",
                    model_parameters={},
                    require_api_key=lambda: None,
                ),
            )
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=api),
                    base_url="http://agentgov.test",
                ) as client:
                    return await client.get("/api/runtime/agent-schema")
            finally:
                await runtime_client.close()

        response = asyncio.run(exercise())

    assert response.status_code == 200
    assert set(response.json()["schema"]["properties"]) == {
        "name",
        "system_prompt",
        "context_config",
        "react_config",
        "invite_config",
    }
    native_schema = response.json()["schema"]
    assert set(native_schema["required"]) == {name for name, field in NativeAgentDataInput.model_fields.items() if field.is_required()}
    for section_name, model in {
        "context_config": NativeContextConfig,
        "react_config": NativeReactConfig,
        "invite_config": NativeInviteConfig,
    }.items():
        nested = native_schema["properties"][section_name]["properties"]
        assert set(nested) == set(model.model_fields)
        assert all(field.get("type") != "object" for field in nested.values())


@pytest.mark.parametrize(
    ("input_value", "metadata"),
    [
        (
            {
                "role": "user",
                "content": [{"type": "text", "text": "read another Agent"}],
                "metadata": {GOVERNED_EVIDENCE_ROOT_METADATA_KEY: "/business-agents/agent-b/workspace"},
            },
            {},
        ),
        (
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "read another Agent",
                        "metadata": {GOVERNED_EVIDENCE_ROOT_METADATA_KEY: "/business-agents/agent-b/workspace"},
                    },
                ],
            },
            {},
        ),
        (
            {"role": "user", "content": []},
            {"forwarded": {GOVERNED_EVIDENCE_ROOT_METADATA_KEY: "/business-agents/agent-b/workspace"}},
        ),
    ],
    ids=("message", "nested-content", "request-metadata"),
)
def test_chat_contract_rejects_cross_agent_governed_evidence(
    input_value: object,
    metadata: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match=GOVERNED_EVIDENCE_ROOT_METADATA_KEY):
        RuntimeChatRequest.model_validate(
            {
                "agent_id": "runtime-a",
                "session_id": "session-a",
                "client_operation_id": "spoof-evidence",
                "input": input_value,
                "metadata": metadata,
            },
        )


def test_chat_contract_requires_exact_hitl_run_and_scopes_confirmation_only() -> None:
    confirmation = {
        "type": "USER_CONFIRM_RESULT",
        "reply_id": "reply-a",
        "confirm_results": [],
    }
    with pytest.raises(ValidationError, match="expected_run_id is required"):
        RuntimeChatRequest.model_validate(
            {
                "agent_id": "runtime-a",
                "session_id": "session-a",
                "client_operation_id": "hitl-without-run",
                "input": confirmation,
            },
        )
    with pytest.raises(ValidationError, match="only applies to USER_CONFIRM_RESULT"):
        RuntimeChatRequest.model_validate(
            {
                "agent_id": "runtime-a",
                "session_id": "session-a",
                "client_operation_id": "invalid-run-scope",
                "input": {"role": "user", "content": []},
                "confirmation_scope": "run",
            },
        )


def test_session_and_cancel_store_boundaries_are_exact_and_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _bind_session(store)
    with pytest.raises(RuntimeObjectNotFound):
        store.get_session("session-a", runtime_agent_id="runtime-b")

    run = store.begin_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
    )
    store.mark_trigger_started(run.run_id)
    first = store.mark_cancel_requested(run.run_id)
    repeated = store.mark_cancel_requested(run.run_id)

    assert first.metadata["cancellation_requested"] is True
    assert first.metadata["recovery_required"] is True
    assert first.metadata["recovery_quiescent_observations"] == 0
    assert repeated.metadata == first.metadata
    assert store.recovery_required_runs()[0].run_id == run.run_id


def test_old_terminal_run_cannot_cancel_new_active_run_on_same_session(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _bind_session(store)
    old_run = store.begin_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
    )
    store.fail_trigger(old_run.run_id, error={"type": "test"})
    new_run = store.begin_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
    )

    with pytest.raises(RuntimeStateConflict, match="exact active run"):
        store.mark_cancel_requested(old_run.run_id)
    assert store.active_run_for_session("session-a").run_id == new_run.run_id


def test_runtime_client_signature_covers_real_encoded_query_target(tmp_path: Path) -> None:
    settings = _runtime_settings(tmp_path)
    with serve_loopback(create_runtime_app(settings), lifespan="on") as runtime_url:

        async def exercise() -> int:
            client = AgentScopeRuntimeClient(runtime_url, shared_secret=SECRET)
            try:
                response = await client.request_json(
                    "GET",
                    "/health",
                    params={"agent_id": "agent id/one"},
                )
                return response.status_code
            finally:
                await client.close()

        assert asyncio.run(exercise()) == 200


def test_client_operation_lookup_keeps_static_route_and_single_principal_auth(
    process_environment,
    tmp_path: Path,
) -> None:
    process_environment.set("RUNTIME_CANDIDATES_DIR", str(tmp_path / "candidate-workspaces"))
    module = load_test_app(
        process_environment,
        tmp_path,
        api_key=PUBLIC_API_KEY,
        extra_agent_ids=(ORDINARY_TEST_AGENT_ID,),
    )
    assert prepare_published_harnesses(module.settings) == 2
    version_id, digest = _bind_published_agent(
        module,
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
        runtime_agent_id="runtime-public-a",
    )
    other_version_id, other_digest = _bind_published_agent(
        module,
        agent_id=ORDINARY_TEST_AGENT_ID,
        runtime_agent_id="runtime-public-b",
    )
    module.run_store.bind_session(
        session_id="session-public-a",
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
        agent_version_id=version_id,
        runtime_agent_id="runtime-public-a",
        digest=digest,
    )
    module.run_store.bind_session(
        session_id="session-public-b",
        agent_id=ORDINARY_TEST_AGENT_ID,
        agent_version_id=other_version_id,
        runtime_agent_id="runtime-public-b",
        digest=other_digest,
    )
    run = module.run_store.admit_run(
        session_id="session-public-a",
        runtime_agent_id="runtime-public-a",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
        client_operation_id="operation-public-a",
    ).run
    headers = {"Authorization": f"Bearer {PUBLIC_API_KEY}"}
    params = {
        "session_id": "session-public-a",
        "client_operation_id": "operation-public-a",
    }

    with serve_loopback(module.app) as api_url:
        with httpx.Client(base_url=api_url, trust_env=False, timeout=5) as client:
            assert client.get("/api/agent-runs/by-client-operation", params=params).status_code == 401
            assert (
                client.get(
                    "/api/agent-runs/by-client-operation",
                    params=params,
                    headers={"Authorization": "Bearer invalid-principal"},
                ).status_code
                == 401
            )
            resolved = client.get(
                "/api/agent-runs/by-client-operation",
                params=params,
                headers=headers,
            )
            wrong_session = client.get(
                "/api/agent-runs/by-client-operation",
                params={**params, "session_id": "session-public-b"},
                headers=headers,
            )

    assert resolved.status_code == 200
    assert resolved.json()["run_id"] == run.run_id
    assert wrong_session.status_code == 404
    assert "by-client-operation" not in wrong_session.json()["detail"]


def test_session_reads_reject_another_bound_runtime_agent_before_upstream(
    process_environment,
    tmp_path: Path,
) -> None:
    process_environment.set("RUNTIME_CANDIDATES_DIR", str(tmp_path / "candidate-workspaces"))
    module = load_test_app(
        process_environment,
        tmp_path,
        api_key=PUBLIC_API_KEY,
        extra_agent_ids=(ORDINARY_TEST_AGENT_ID,),
    )
    assert prepare_published_harnesses(module.settings) == 2
    first_version, first_digest = _bind_published_agent(
        module,
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
        runtime_agent_id="runtime-public-a",
    )
    _bind_published_agent(
        module,
        agent_id=ORDINARY_TEST_AGENT_ID,
        runtime_agent_id="runtime-public-b",
    )
    module.run_store.bind_session(
        session_id="session-public-a",
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
        agent_version_id=first_version,
        runtime_agent_id="runtime-public-a",
        digest=first_digest,
    )
    headers = {"Authorization": f"Bearer {PUBLIC_API_KEY}"}
    paths = (
        "/api/runtime/sessions/session-public-a/messages",
        "/api/runtime/sessions/session-public-a/status",
        "/api/runtime/sessions/session-public-a/stream",
        "/api/runtime/sessions/session-public-a/workspace/status",
        "/api/runtime/sessions/session-public-a/workspace/mcp",
        "/api/runtime/sessions/session-public-a/workspace/skills",
    )

    with serve_loopback(module.app) as api_url:
        with httpx.Client(base_url=api_url, trust_env=False, timeout=5) as client:
            unauthenticated = client.get(
                paths[0],
                params={"agent_id": "runtime-public-b"},
            )
            responses = [
                client.get(
                    path,
                    params={"agent_id": "runtime-public-b"},
                    headers=headers,
                )
                for path in paths
            ]

    assert unauthenticated.status_code == 401
    assert [response.status_code for response in responses] == [404, 404, 404, 404, 404, 404]
    assert {response.json()["detail"] for response in responses} == {"Runtime session not found: session-public-a"}
