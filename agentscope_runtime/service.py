"""使用 AgentScope 公共 create_app 组装独立单副本 Runtime。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
from agentscope.app import create_app as agentscope_create_app
from agentscope.middleware import MiddlewareBase, TracingMiddleware
from agentscope.workspace import WorkspaceBase
from fastapi import FastAPI
from fastapi.middleware import Middleware

from .access_middleware import FixedRuntimeUserMiddleware
from .credential_storage import ProvisionedAsyncSQLAlchemyStorage
from .harness_evidence_middleware import GovernedHarnessEvidenceMiddleware
from .mcp_resource_middleware import MCPResourceMiddleware
from .observability import OTelRuntimeLifecycleMiddleware, configure_otel_from_env
from .policy_middleware import AgentGovPolicyMiddleware
from .receipt_middleware import AgentGovReceiptMiddleware
from .run_trace import AgentGovRunTraceRegistry
from .session_workspace_release import SessionWorkspaceReleaseMiddleware
from .settings import RUNTIME_USER_ID, RuntimeSettings
from .subagent_templates import discover_subagent_templates
from .team_coordination import (
    AgentGovInMemoryMessageBus,
    RuntimeBootCoordinationMiddleware,
    RuntimeBootCoordinator,
)
from .trace_context_middleware import AgentGovTraceContextMiddleware
from .workspace_manager import AgentGovLocalWorkspace, AgentGovWorkspaceManager

AgentMiddlewareFactory = Callable[
    [str, str, str, WorkspaceBase],
    Awaitable[list[MiddlewareBase]],
]


def _agent_middleware_factory(
    settings: RuntimeSettings,
    trace_registry: AgentGovRunTraceRegistry,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AgentMiddlewareFactory:
    async def factory(
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace: WorkspaceBase,
    ) -> list[MiddlewareBase]:
        del user_id, agent_id, session_id
        if not isinstance(workspace, AgentGovLocalWorkspace):
            raise TypeError("AgentGov Runtime requires an AgentGov workspace")
        # Tracing 放在外层，回执 middleware 在其 active span 中取 trace_id。
        return [
            AgentGovTraceContextMiddleware(
                settings,
                transport=transport,
                trace_registry=trace_registry,
            ),
            TracingMiddleware(),
            AgentGovReceiptMiddleware(settings, transport=transport),
            GovernedHarnessEvidenceMiddleware(settings.business_agents_root),
            MCPResourceMiddleware(
                workspace.default_mcps,
                workspace.mcp_resource_policies,
            ),
            AgentGovPolicyMiddleware(
                workspace.harness_root,
                tool_workdir=workspace.workdir,
            ),
        ]

    return factory


def create_runtime_app(
    settings: RuntimeSettings | None = None,
    *,
    control_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the internal AgentScope service without private framework hooks."""

    resolved = settings or RuntimeSettings.from_env()
    resolved.prepare_writable_directories()
    resolved.validate_source_mounts()
    otel_runtime = configure_otel_from_env()
    trace_registry = AgentGovRunTraceRegistry()
    message_bus = AgentGovInMemoryMessageBus(
        resolved,
        transport=control_transport,
    )
    boot_coordinator = RuntimeBootCoordinator(
        resolved,
        transport=control_transport,
    )

    storage = ProvisionedAsyncSQLAlchemyStorage(
        resolved,
        receipt_transport=control_transport,
        trace_registry=trace_registry,
        message_bus=message_bus,
    )
    subagent_templates = discover_subagent_templates(resolved.candidates_root)
    workspace_manager = AgentGovWorkspaceManager(
        business_agents_root=resolved.business_agents_root,
        candidates_root=resolved.candidates_root,
        workspaces_root=resolved.workspaces_root,
        require_read_only_sources=resolved.require_read_only_source_mounts,
        subagent_templates=subagent_templates,
    )
    http_middlewares = [
        Middleware(
            RuntimeBootCoordinationMiddleware,
            coordinator=boot_coordinator,
        ),
        Middleware(
            SessionWorkspaceReleaseMiddleware,
            workspace_manager=workspace_manager,
        ),
    ]
    if otel_runtime is not None:
        http_middlewares.append(
            Middleware(OTelRuntimeLifecycleMiddleware, runtime=otel_runtime),
        )
    # AgentScope adds supplied middleware by prepending it; append auth last so
    # signature/body checks remain the outermost HTTP boundary.
    http_middlewares.append(
        Middleware(
            FixedRuntimeUserMiddleware,
            expected_user_id=RUNTIME_USER_ID,
            shared_secret=resolved.shared_secret,
        ),
    )
    app = agentscope_create_app(
        storage=storage,
        message_bus=message_bus,
        workspace_manager=workspace_manager,
        knowledge_base_manager=None,
        enable_index_worker=False,
        enable_channel_worker=False,
        enable_scheduler=False,
        channels=[],
        mcp_hubs=[],
        skill_hubs=[],
        custom_subagent_templates=list(subagent_templates.values()),
        extra_agent_middlewares=_agent_middleware_factory(
            resolved,
            trace_registry,
            transport=control_transport,
        ),
        extra_middlewares=http_middlewares,
        title="AgentGov AgentScope Runtime",
    )
    app.state.agentgov_runtime_settings = resolved
    app.state.agentgov_trace_registry = trace_registry
    app.state.agentgov_boot_coordinator = boot_coordinator
    return app
