from __future__ import annotations

import asyncio
import json
import time
import uuid
import venv
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from agentgov_agentscope_contract import session_workspace_id
from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app.storage import (
    AgentData,
    AgentRecord,
    AsyncSQLAlchemyStorage,
    SessionConfig,
    SessionRecord,
)
from agentscope_runtime.credential_storage import ProvisionedAsyncSQLAlchemyStorage
from agentscope_runtime.receipt_middleware import AgentGovReceiptDispatcher
from agentscope_runtime.run_trace import AgentGovRunTraceRegistry
from agentscope_runtime.session_workspace_release import SessionWorkspaceReclaimer
from agentscope_runtime.settings import RUNTIME_USER_ID, RuntimeSettings
from agentscope_runtime.signing import runtime_gateway_headers
from agentscope_runtime.workspace_manager import AgentGovWorkspaceManager
from agentscope_runtime.workspace_reference_fence import SessionWorkspaceReferenceFence
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway.store import RuntimeRunStore
from fastapi.testclient import TestClient
from scripts.runtime_workspace_gc import plan_workspaces, read_governance_references
from scripts.runtime_workspace_gc_inventory import collect_native_inventory
from starlette.types import ASGIApp, Receive, Scope, Send

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "a" * 64
USER = "agentgov-runtime"


class _FailAfterAgentDelete:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._app(scope, receive, send)
        if scope["type"] == "http" and scope["method"] == "DELETE" and scope["path"].startswith("/agent/"):
            raise RuntimeError("injected downstream response failure")


async def _create_native(database: Path, workspace_id: str, *, delete: bool) -> str:
    async with AsyncSQLAlchemyStorage(f"sqlite+aiosqlite:///{database}") as storage:
        agent = AgentRecord(user_id=USER, data=AgentData(name="GC contract agent", context_config=ContextConfig(), react_config=ReActConfig()))
        await storage.upsert_agent(USER, agent)
        await storage.upsert_session(USER, agent.id, SessionConfig(workspace_id=workspace_id))
        if delete:
            assert await storage.delete_agent(USER, agent.id)
        return agent.id


def _workspace(root: Path, workspace_id: str) -> Path:
    target = root / workspace_id
    target.mkdir(parents=True)
    (target / ".agentgov-runtime-workspace.json").write_text(json.dumps({"workspace_id": workspace_id, "harness_digest": DIGEST}), encoding="utf-8")
    (target / ".agentgov-runtime-state").mkdir()
    (target / ".agentgov-runtime-cache").mkdir()
    return target


def _runtime_workspace(root: Path, workspace_id: str) -> tuple[Path, Path]:
    target = _workspace(root, workspace_id)
    journal_path = root / ".agentgov-native-session-references.json"
    if journal_path.exists():
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    else:
        journal = {
            "schema_version": 1,
            "reclaimable_workspace_ids": [],
            "entries": [],
        }
    workspace_ids = set(journal["reclaimable_workspace_ids"])
    workspace_ids.add(workspace_id)
    journal["reclaimable_workspace_ids"] = sorted(workspace_ids)
    journal_path.write_text(
        json.dumps(journal, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    environment = target / ".agentgov-runtime-state/.agentscope/.venv"
    venv.EnvBuilder(with_pip=False, symlinks=False).create(environment)
    payload = environment / "workspace-owned-bytes.bin"
    payload.write_bytes(b"workspace-owned-environment")
    return target, payload


def _reclaim_manager(
    tmp_path: Path,
    storage: AsyncSQLAlchemyStorage,
    *,
    workspace_reference_fence: SessionWorkspaceReferenceFence | None = None,
) -> tuple[AgentGovWorkspaceManager, Path, Path]:
    business = tmp_path / "business"
    candidates = tmp_path / "candidates"
    workspaces = tmp_path / "runtime-workspaces"
    for directory in (business, candidates, workspaces):
        directory.mkdir(exist_ok=True)
    manager = AgentGovWorkspaceManager(
        business_agents_root=business,
        candidates_root=candidates,
        workspaces_root=workspaces,
        workspace_reference_fence=workspace_reference_fence,
    )
    manager.bind_storage(storage)
    return manager, candidates, workspaces


@asynccontextmanager
async def _project_reclaim_runtime(
    tmp_path: Path,
    name: str,
    *,
    settings: RuntimeSettings | None = None,
    bind_deletes: bool = False,
    fence: SessionWorkspaceReferenceFence | None = None,
) -> AsyncIterator[
    tuple[
        ProvisionedAsyncSQLAlchemyStorage,
        AgentGovWorkspaceManager,
        SessionWorkspaceReclaimer,
        Path,
        RuntimeSettings,
    ]
]:
    if settings is None:
        settings = _project_runtime_settings(tmp_path, name)
    fence = fence or SessionWorkspaceReferenceFence()
    dispatcher = AgentGovReceiptDispatcher(settings, trace_registry=AgentGovRunTraceRegistry())
    storage = ProvisionedAsyncSQLAlchemyStorage(
        settings,
        receipt_dispatcher=dispatcher,
        workspace_reference_fence=fence,
    )
    async with storage:
        manager, _, workspaces = _reclaim_manager(
            tmp_path,
            storage,
            workspace_reference_fence=fence,
        )
        reclaimer = SessionWorkspaceReclaimer(manager)
        if bind_deletes:
            storage.bind_workspace_deletion_reconciler(reclaimer)
        yield storage, manager, reclaimer, workspaces, settings


def _project_runtime_settings(tmp_path: Path, name: str) -> RuntimeSettings:
    data = tmp_path / f"{name}-data"
    business = tmp_path / "business"
    candidates = tmp_path / "candidates"
    workspaces = tmp_path / "runtime-workspaces"
    for directory in (data, business, candidates, workspaces):
        directory.mkdir(exist_ok=True)
    return RuntimeSettings(
        shared_secret="workspace-reclaim-test-secret",
        provider_api_key="unused-provider-test-secret",
        agentgov_api_base_url="http://127.0.0.1:9",
        data_dir=data,
        business_agents_root=business,
        candidates_root=candidates,
        workspaces_root=workspaces,
        database_url=f"sqlite+aiosqlite:///{data / 'agentscope.db'}",
    )


async def _native_session(
    storage: AsyncSQLAlchemyStorage,
    workspace_id: str,
    *,
    agent: AgentRecord | None = None,
    source: str = "user",
) -> tuple[AgentRecord, SessionRecord]:
    if agent is None:
        agent = AgentRecord(
            user_id=USER,
            source=source,
            data=AgentData(
                name="Workspace reclaim contract",
                context_config=ContextConfig(),
                react_config=ReActConfig(),
            ),
        )
        await storage.upsert_agent(USER, agent)
    session = await storage.upsert_session(
        USER,
        agent.id,
        SessionConfig(workspace_id=workspace_id),
    )
    return agent, session


def _runtime_request(
    client: TestClient,
    settings: RuntimeSettings,
    method: str,
    path: str,
    payload: object | None = None,
):
    body = b"" if payload is None else json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    timestamp = f"{time.time_ns() / 1_000_000_000:.9f}"
    headers = {
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
    if payload is not None:
        headers["Content-Type"] = "application/json"
    return client.request(method, path, content=body, headers=headers)


def _scenario(tmp_path: Path, *, native_deleted: bool = True, cleanup_complete: bool = True):
    database = tmp_path / "runtime.sqlite3"
    store = RuntimeRunStore(make_session_factory(database))
    native_database = tmp_path / "native.sqlite3"
    workspace_id = session_workspace_id(f"candidate-gc--v-{DIGEST}", uuid.uuid4())
    runtime_agent_id = asyncio.run(_create_native(native_database, workspace_id, delete=native_deleted))
    workspaces = tmp_path / "workspaces"
    _workspace(workspaces, workspace_id)
    store.start_ephemeral_resource(
        cache_key="gc-contract",
        business_agent_id="soc",
        version_owner_id="candidate-gc",
        agent_version_id="b" * 40,
        digest=DIGEST,
        source_id="candidate-gc",
        source_kind="candidate_snapshot",
        workspace_id=workspace_id,
    )
    store.record_ephemeral_agent("gc-contract", runtime_agent_id)
    if cleanup_complete:
        store.mark_ephemeral_cleanup_pending("gc-contract", stage="cleanup", error_type="CleanupRequested")
        store.complete_ephemeral_resource("gc-contract")
    return database, native_database, workspaces, workspace_id, runtime_agent_id, store


def _plan(database: Path, native_database: Path, workspaces: Path):
    references = read_governance_references(database)
    native = asyncio.run(collect_native_inventory(native_database, set(references.agent_ids)))
    return plan_workspaces(workspaces, references, native)
