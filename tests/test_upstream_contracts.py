"""上游依赖契约硬验证：claude-agent-sdk 与 langfuse 都在持续迭代，本文件用【真实类型】
（非 mock / 非合成 dict）钉住我们实际依赖的字段与 API 签名，一旦 `uv sync` 升级导致上游
改名/改签名，这些测试立刻红灯暴露，而不是让 Langfuse 观测静默变空、系统悄悄退化。

分三层：
1. SDK message/block 形态契约 —— 真实 dataclass → to_plain → 我们的投影，断言 input/结果/usage 仍流通。
2. Langfuse observation API 签名契约 —— 断言我们调用的 kwargs / 方法仍存在。
3. Langfuse OTLP 摄取假设 —— 记录「logs 信号不被接收」这一已知事实的守卫点（在线，标记跳过）。
"""

from __future__ import annotations

import dataclasses
import inspect

import claude_agent_sdk as sdk
import pytest
from app.runtime.message_utils import to_plain
from app.runtime.runtime_activity import RuntimeActivityExtractor

# ---- 层 1：SDK message/block 形态契约（用真实 dataclass）----

def test_sdk_block_fields_we_depend_on_still_exist():
    """我们的投影依赖这些字段名；上游改名即在此红灯。"""
    tu = {f.name for f in dataclasses.fields(sdk.ToolUseBlock)}
    tr = {f.name for f in dataclasses.fields(sdk.ToolResultBlock)}
    am = {f.name for f in dataclasses.fields(sdk.AssistantMessage)}
    rm = {f.name for f in dataclasses.fields(sdk.ResultMessage)}
    assert {"id", "name", "input"} <= tu, f"ToolUseBlock drift: {tu}"
    assert {"tool_use_id", "content", "is_error"} <= tr, f"ToolResultBlock drift: {tr}"
    assert {"content", "model", "usage"} <= am, f"AssistantMessage drift: {am}"
    assert {"usage", "total_cost_usd"} <= rm, f"ResultMessage drift: {rm}"


def test_real_sdk_messages_project_to_child_observations():
    """端到端契约：真实 SDK dataclass → to_plain（同 _track_query_message）→ 投影出带 I/O 的子观测。"""
    tool_use = sdk.ToolUseBlock(id="call_1", name="Glob", input={"pattern": "*.py"})
    assistant = sdk.AssistantMessage(
        content=[sdk.TextBlock(text="先查文件"), tool_use],
        model="deepseek-v4-flash",
        usage={"input_tokens": 100, "output_tokens": 20},
    )
    tool_result = sdk.ToolResultBlock(tool_use_id="call_1", content="a.py\nb.py", is_error=False)
    user = sdk.UserMessage(content=[tool_result])

    # 复刻 _track_query_message 的 to_plain 收口
    messages = [to_plain(assistant), to_plain(user)]
    children = RuntimeActivityExtractor().sdk_child_observations(messages)

    tool = next(c for c in children if c["kind"] == "tool")
    assert tool["name"] == "sdk.tool.Glob"
    assert tool["input"] == {"pattern": "*.py"}        # 真实 ToolUseBlock.input 仍被抽到
    assert tool["output"] == "a.py\nb.py"              # 真实 ToolResultBlock.content 仍被抽到

    gen = next(c for c in children if c["kind"] == "generation")
    assert gen["model"] == "deepseek-v4-flash"
    assert gen["usage_details"] == {"input_tokens": 100, "output_tokens": 20}  # 真实 usage 仍被解析


def test_real_result_message_usage_and_cost_shape():
    """ResultMessage 聚合 usage/cost 字段仍是我们 usage_details/cost_details 能吃的形态。"""
    rm = sdk.ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="s", total_cost_usd=0.01, usage={"input_tokens": 10, "output_tokens": 5},
    )
    plain = to_plain(rm)
    extractor = RuntimeActivityExtractor()
    assert extractor.usage_details(plain.get("usage")) == {"input_tokens": 10, "output_tokens": 5}
    assert extractor.cost_details(plain.get("total_cost_usd")) == {"total_cost_usd": 0.01}


# ---- 层 2：Langfuse observation API 签名契约 ----

def test_langfuse_start_observation_signature_contract():
    """我们给 start_observation 传的 kwargs 必须仍被 langfuse 接受；升级改签名即红灯。"""
    from langfuse._client.span import LangfuseSpan

    params = set(inspect.signature(LangfuseSpan.start_observation).parameters)
    required = {"name", "as_type", "input", "output", "metadata", "model", "usage_details", "cost_details", "level"}
    missing = required - params
    assert not missing, f"langfuse start_observation 签名漂移，缺: {missing}（现有: {sorted(params)}）"
    assert callable(getattr(LangfuseSpan, "end", None)), "LangfuseSpan.end 缺失（我们的子观测靠它收尾）"
    end_params = set(inspect.signature(LangfuseSpan.end).parameters)
    assert "end_time" in end_params, f"LangfuseSpan.end 不再接受 end_time: {end_params}"


def test_langfuse_client_start_as_current_observation_exists():
    """RuntimeLangfuseClient.start_observation 依赖 client.start_as_current_observation。"""
    from langfuse import Langfuse

    assert callable(getattr(Langfuse, "start_as_current_observation", None))
    assert callable(getattr(Langfuse, "start_observation", None))  # 子观测非 current 工厂


# ---- 层 3：Langfuse OTLP 摄取假设（在线守卫，默认跳过）----

@pytest.mark.skip(reason="需在线 Langfuse；记录『/v1/logs 无接收端』这一已知事实的守卫点，见 langfuse_smoke")
def test_langfuse_otlp_logs_endpoint_still_absent():
    """占位：若 Langfuse 未来新增 /v1/logs（不再 404），说明可重新启用 logs 信号，应在此提示。

    落地时在 make langfuse-smoke 里带真实鉴权 POST /api/public/otel/v1/{traces,logs}，
    断言 traces!=404 且 logs==404；一旦 logs!=404 即提醒重新评估 LANGFUSE_OTEL_SIGNALS。
    """
