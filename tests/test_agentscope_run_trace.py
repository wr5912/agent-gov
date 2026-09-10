from __future__ import annotations

from dataclasses import dataclass

import pytest
from agentscope_runtime.run_trace import AgentGovRunTraceRegistry, AgentGovTraceIdGenerator
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@dataclass(frozen=True)
class _Context:
    run_id: str = "run-1"
    session_id: str = "session-1"
    agent_id: str = "business-agent"
    agent_version_id: str = "f" * 40
    runtime_agent_id: str = "runtime-agent"
    harness_digest: str = "a" * 64
    trace_id: str = "1" * 32


def _registry() -> tuple[AgentGovRunTraceRegistry, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(id_generator=AgentGovTraceIdGenerator())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return AgentGovRunTraceRegistry(provider.get_tracer("test")), exporter


def test_initial_and_hitl_stages_share_one_governed_root() -> None:
    registry, exporter = _registry()
    context = _Context()
    initial = registry.start_stage(
        context,
        stage="initial",
        reply_id="pending",
        runtime_version="v1",
        agentscope_version="2.0.8",
    )
    initial.set_reply_id("reply-1")
    initial.end(failed=False)
    resumed = registry.start_stage(
        context,
        stage="hitl_resume",
        reply_id="reply-1",
        runtime_version="v1",
        agentscope_version="2.0.8",
    )
    resumed.end(failed=False)

    assert [span.name for span in exporter.get_finished_spans()] == [
        "agentgov.run.stage",
        "agentgov.run.stage",
    ]
    registry.finish_run(
        context,
        terminal_reason="completed",
        failed=False,
        runtime_version="v1",
        agentscope_version="2.0.8",
    )
    spans = exporter.get_finished_spans()
    roots = [span for span in spans if span.name == "agentgov.run"]
    stages = [span for span in spans if span.name == "agentgov.run.stage"]
    assert len(roots) == 1
    assert roots[0].parent is None
    assert f"{roots[0].context.trace_id:032x}" == context.trace_id
    assert roots[0].attributes["agentgov.run.finished_reason"] == "completed"
    assert all(span.parent.span_id == roots[0].context.span_id for span in stages)
    assert {f"{span.context.trace_id:032x}" for span in spans} == {context.trace_id}


def test_terminal_confirmation_is_idempotent_and_blocks_reopen() -> None:
    registry, exporter = _registry()
    context = _Context()
    registry.finish_run(
        context,
        terminal_reason="completed",
        failed=False,
        runtime_version="v1",
        agentscope_version="2.0.8",
    )
    registry.finish_run(
        context,
        terminal_reason="completed",
        failed=False,
        runtime_version="v1",
        agentscope_version="2.0.8",
    )

    assert len(exporter.get_finished_spans()) == 1
    with pytest.raises(ValueError, match="Terminal"):
        registry.start_stage(
            context,
            stage="late",
            reply_id="late",
            runtime_version="v1",
            agentscope_version="2.0.8",
        )


def test_trace_id_mismatch_fails_closed_without_second_root() -> None:
    registry, exporter = _registry()
    context = _Context()
    stage = registry.start_stage(
        context,
        stage="initial",
        reply_id="pending",
        runtime_version="v1",
        agentscope_version="2.0.8",
    )
    stage.end(failed=False)

    with pytest.raises(ValueError, match="another trace_id"):
        registry.start_stage(
            _Context(trace_id="2" * 32),
            stage="resume",
            reply_id="reply-1",
            runtime_version="v1",
            agentscope_version="2.0.8",
        )
    registry.close_all()
    assert len([span for span in exporter.get_finished_spans() if span.name == "agentgov.run"]) == 1
