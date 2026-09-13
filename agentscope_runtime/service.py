"""使用 AgentScope 公共 create_app 组装独立单副本 Runtime。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from agentgov_agentscope_contract import RUNTIME_TEMPLATE_RESTART_REQUIRED, RuntimeTemplateRestartRequired
from agentscope.app import create_app as agentscope_create_app
from agentscope.middleware import MiddlewareBase, TracingMiddleware
from agentscope.workspace import WorkspaceBase
from fastapi import FastAPI, Request
from fastapi.middleware import Middleware
from fastapi.responses import JSONResponse

from .access_middleware import FixedRuntimeUserMiddleware
from .credential_storage import ProvisionedAsyncSQLAlchemyStorage
from .harness_evidence_middleware import GovernedHarnessEvidenceMiddleware
from .mcp_resource_middleware import MCPResourceMiddleware
from .observability import configure_otel_from_env
from .policy_middleware import AgentGovPolicyMiddleware
from .receipt_middleware import (
    AgentGovReceiptDispatcher,
    AgentGovReceiptLifespanMiddleware,
    AgentGovReceiptMiddleware,
)
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


async def _template_restart_response(_request: Request, _error: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error_code": RUNTIME_TEMPLATE_RESTART_REQUIRED,
            "detail": str(RuntimeTemplateRestartRequired()),
        },
    )


def _configure_runtime_app(
    app: FastAPI,
    settings: RuntimeSettings,
    trace_registry: AgentGovRunTraceRegistry,
    receipt_dispatcher: AgentGovReceiptDispatcher,
    boot_coordinator: RuntimeBootCoordinator,
) -> FastAPI:
    """在公共 FastAPI 实例上装配 AgentGov 依赖引用和稳定错误边界。"""
    app.state.agentgov_runtime_settings = settings
    app.state.agentgov_trace_registry = trace_registry
    app.state.agentgov_receipt_dispatcher = receipt_dispatcher
    app.state.agentgov_boot_coordinator = boot_coordinator
    app.add_exception_handler(RuntimeTemplateRestartRequired, _template_restart_response)
    return app


def _agent_middleware_factory(
    settings: RuntimeSettings,
    trace_registry: AgentGovRunTraceRegistry,
    receipt_dispatcher: AgentGovReceiptDispatcher,
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
                trace_registry=trace_registry,
            ),
            TracingMiddleware(),
            AgentGovReceiptMiddleware(
                settings,
                receipt_dispatcher=receipt_dispatcher,
            ),
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
) -> FastAPI:
    """Build the internal AgentScope service without private framework hooks."""

    resolved = settings or RuntimeSettings.from_env()
    resolved.prepare_writable_directories()
    resolved.validate_source_mounts()
    otel_runtime = configure_otel_from_env()
    trace_registry = AgentGovRunTraceRegistry()
    receipt_dispatcher = AgentGovReceiptDispatcher(
        resolved,
        trace_registry=trace_registry,
    )
    message_bus = AgentGovInMemoryMessageBus(resolved)
    boot_coordinator = RuntimeBootCoordinator(resolved)

    storage = ProvisionedAsyncSQLAlchemyStorage(
        resolved,
        receipt_dispatcher=receipt_dispatcher,
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
        Middleware(
            AgentGovReceiptLifespanMiddleware,
            dispatcher=receipt_dispatcher,
            trace_registry=trace_registry,
            provider_shutdown=(otel_runtime.shutdown if otel_runtime is not None else None),
        ),
    ]
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
            receipt_dispatcher,
        ),
        extra_middlewares=http_middlewares,
        title="AgentGov AgentScope Runtime",
    )
    return _configure_runtime_app(app, resolved, trace_registry, receipt_dispatcher, boot_coordinator)
