from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .agent_job_runner import AgentJobRunner
from .hitl_policy import blocks_interactive_question, tool_requires_web_hitl

if TYPE_CHECKING:
    from .agent_profiles import AgentRuntimeProfile
    from .claude_runtime import ClaudeRuntime, RuntimeRequestContext
    from .schemas import ChatRequest, ChatResponse


def _non_stream_hitl_deny_callback(profile_name: str) -> Any:
    """非流式无人审路径只允许无需 HITL 的工具。"""
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    async def deny(tool_name: str, input_data: Any, sdk_context: Any) -> Any:
        if blocks_interactive_question(profile_name, tool_name):
            return PermissionResultDeny(message=f"工具 {tool_name} 已禁用：后台处置流程不得发起临时人工提问。")
        if not tool_requires_web_hitl(profile_name, tool_name):
            return PermissionResultAllow()
        return PermissionResultDeny(
            message=(
                f"工具 {tool_name} 需人工审批，但 ENABLE_CLAUDE_WEB_HITL 未开启且非流式无人审面："
                f"业务 Agent {profile_name} 的响应处置执行请改用流式 + 开启 HITL，或先做 dry-run。"
            )
        )

    return deny


async def run_claimed_claude_runtime(
    runtime: ClaudeRuntime,
    req: ChatRequest,
    *,
    context: RuntimeRequestContext,
    profile: AgentRuntimeProfile,
) -> ChatResponse:
    from claude_agent_sdk import ResultMessage, query

    from .claude_runtime import RuntimeQueryState

    hitl_required = profile.requires_web_hitl
    context.telemetry_input["permission_mode"] = "default" if hitl_required else "bypassPermissions"
    context.telemetry_input["claude_web_hitl_enabled"] = False
    state = RuntimeQueryState(sdk_session_id=context.session.sdk_session_id)
    root_metadata = runtime._runtime_observation_metadata(context, "non_stream", profile=profile)
    propagation = runtime._langfuse_propagation_attributes(req, context, "non_stream", profile=profile)
    with runtime.langfuse.propagate_attributes(**propagation):
        with runtime.langfuse.start_observation(
            as_type="span",
            name=profile.langfuse_observation_name,
            input=context.telemetry_input,
            metadata=root_metadata,
        ) as root_span:
            context.langfuse_trace_id, context.langfuse_trace_url = runtime.langfuse.current_trace_ref()
            runtime.langfuse.set_trace_attributes(root_span, **propagation)
            with runtime.langfuse.start_observation(
                as_type="generation",
                name=f"{profile.langfuse_observation_name}.claude_sdk_query",
                input=runtime._generation_input(req, context),
                model=req.model or runtime.settings.agent_model,
                metadata=root_metadata,
            ) as generation:
                runtime.langfuse.set_trace_attributes(generation, **propagation)
                try:

                    async def execute_query() -> None:
                        runtime.model_provider_router.ensure_agent_runtime_ready()
                        options = runtime._build_options(
                            req,
                            context.session,
                            profile=profile,
                            execution_mode="non_stream_hitl_required" if hitl_required else "non_stream_bypass",
                            can_use_tool=_non_stream_hitl_deny_callback(profile.name) if hitl_required else None,
                        )
                        prompt_stream = AgentJobRunner.single_prompt_stream(context.prompt)
                        async for msg in query(prompt=prompt_stream, options=options):
                            runtime._track_query_message(msg, state, ResultMessage)

                    try:
                        await execute_query()
                    except Exception as exc:
                        if not runtime._should_retry_without_sdk_resume(exc, context, state):
                            raise
                        runtime._clear_stale_sdk_session(context)
                        state = RuntimeQueryState(sdk_session_id=None)
                        await execute_query()
                except Exception as exc:
                    if not runtime._should_suppress_exception(exc, state.errors):
                        state.errors.append(f"{exc.__class__.__name__}: {exc}")

                answer, agent_activity, output = runtime._runtime_output_from_state(req, context, state)
                runtime._update_runtime_observations(root_span, generation, context, state, output, propagation)
    runtime._flush_langfuse()
    runtime._sync_langfuse_trace(context, propagation, output)
    runtime._complete_runtime_request(req, context, state, answer, agent_activity)
    return runtime._run_response(context, state, answer, agent_activity)
