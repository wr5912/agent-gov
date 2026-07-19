from scripts.langfuse_smoke import runtime_trace_observation_errors


def test_governor_trace_accepts_projected_sdk_observations_without_native_duplicates() -> None:
    trace_name = "runtime.governor.attribution"
    errors = runtime_trace_observation_errors(
        trace_id="trace-1",
        trace_name=trace_name,
        names={trace_name, "sdk.tool.Read", "sdk.llm.1", "runtime.output_formatter.attribution"},
    )

    assert errors == []


def test_governor_trace_rejects_missing_projection_and_native_duplicate_observations() -> None:
    trace_name = "runtime.governor.attribution"
    errors = runtime_trace_observation_errors(
        trace_id="trace-2",
        trace_name=trace_name,
        names={trace_name, "claude_code.tool.blocked_on_user"},
    )

    assert any("projected SDK tool" in error for error in errors)
    assert any("projected SDK LLM" in error for error in errors)
    assert any("duplicate Claude Code native" in error for error in errors)


def test_governor_trace_rejects_failed_sdk_tool_observations() -> None:
    trace_name = "runtime.governor.attribution"
    errors = runtime_trace_observation_errors(
        trace_id="trace-3",
        trace_name=trace_name,
        names={trace_name, "sdk.tool.Skill", "sdk.llm.1"},
        error_observation_names={"sdk.tool.Skill"},
    )

    assert errors == ["governor trace trace-3 includes error observations: sdk.tool.Skill"]
