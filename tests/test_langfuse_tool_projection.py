"""后端自建 Langfuse 子观测：把 SDK 原生 message 流投影成逐工具/逐轮 I/O 观测。

背景：Claude Code 原生 OTEL 的 claude_code.tool / claude_code.llm_request span 的
Input/Output 为空（工具入参/结果/报文走 logs 信号被 Langfuse 404 丢弃）。本模块测试
后端从 claude-agent-sdk message 流（ToolUseBlock.input / ToolResultBlock.content /
AssistantMessage 每轮 content+usage）投影出的 sdk.tool.* / sdk.llm.* 子观测，携带完整 I/O。
"""

from __future__ import annotations

from app.runtime.integrations.runtime_langfuse import RuntimeLangfuseClient
from app.runtime.runtime_activity import RuntimeActivityExtractor
from app.runtime.settings import AppSettings


def _settings(tmp_path) -> AppSettings:
    workspace = tmp_path / "docker" / "volume" / "main-workspace"
    data = tmp_path / "docker" / "volume" / "data"
    claude_root = tmp_path / "docker" / "volume" / "claude-roots" / "main"
    claude_home = claude_root / ".claude"
    workspace.mkdir(parents=True, exist_ok=True)
    claude_home.mkdir(parents=True, exist_ok=True)
    return AppSettings(
        _env_file=None,
        WORKSPACE_DIR=workspace,
        MAIN_WORKSPACE_DIR=workspace,
        DATA_DIR=data,
        CLAUDE_ROOT=claude_root,
        MAIN_CLAUDE_ROOT=claude_root,
        CLAUDE_HOME=claude_home,
    )


# to_plain 之后的 SDK message 形态：AssistantMessage.content 里的 block 是扁平 dict
# （ToolUseBlock -> {"id","name","input"}；ToolResultBlock -> {"tool_use_id","content","is_error"}）。
def _messages():
    return [
        {"content": "分析这个反馈", "event": "UserMessage"},
        {
            "content": [
                {"text": "我先查一下工作区文件"},
                {"id": "call_1", "name": "Glob", "input": {"pattern": "*.py"}},
            ],
            "model": "deepseek-v4-flash",
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "event": "AssistantMessage",
        },
        {
            "content": [{"tool_use_id": "call_1", "content": "a.py\nb.py", "is_error": False}],
            "event": "UserMessage",
        },
        {
            "content": [{"text": "结论是 X"}],
            "model": "deepseek-v4-flash",
            "usage": {"input_tokens": 200, "output_tokens": 50},
            "event": "AssistantMessage",
        },
        {"result": "结论是 X", "total_cost_usd": 0.01, "event": "ResultMessage:success"},
    ]


class FakeChild:
    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.ended = False

    def end(self, **_):
        self.ended = True


class FakeParent:
    def __init__(self):
        self.children: list[FakeChild] = []

    def start_observation(self, **kwargs):
        child = FakeChild(kwargs)
        self.children.append(child)
        return child


def _kind(children):
    return [(c["kind"], c["name"]) for c in children]


def test_projects_tool_and_generation_children(tmp_path):
    children = RuntimeActivityExtractor(_settings(tmp_path)).sdk_child_observations(_messages())

    tools = [c for c in children if c["kind"] == "tool"]
    gens = [c for c in children if c["kind"] == "generation"]

    assert [c["name"] for c in tools] == ["sdk.tool.Glob"]
    assert tools[0]["input"] == {"pattern": "*.py"}          # 工具入参完整
    assert tools[0]["output"] == "a.py\nb.py"                # 工具结果完整
    assert tools[0]["level"] == "DEFAULT"
    assert tools[0]["metadata"]["tool_use_id"] == "call_1"

    assert [c["name"] for c in gens] == ["sdk.llm.1", "sdk.llm.2"]
    assert gens[0]["model"] == "deepseek-v4-flash"
    assert gens[0]["usage_details"] == {"input_tokens": 100, "output_tokens": 20}
    # 逐轮 output = 该轮 assistant content（含文本与 tool_use）
    assert any(isinstance(b, dict) and b.get("name") == "Glob" for b in gens[0]["output"])
    # 第二轮 input = 上一轮以来的增量（tool_result），不重复全量历史
    assert gens[1]["usage_details"] == {"input_tokens": 200, "output_tokens": 50}


def test_missing_result_yields_null_output(tmp_path):
    messages = [
        {
            "content": [{"id": "call_x", "name": "Bash", "input": {"command": "ls"}}],
            "model": "m",
            "event": "AssistantMessage",
        },
    ]
    children = RuntimeActivityExtractor(_settings(tmp_path)).sdk_child_observations(messages)
    tool = [c for c in children if c["kind"] == "tool"][0]
    assert tool["input"] == {"command": "ls"}
    assert tool.get("output") is None       # 无结果 → output 缺省/空


def test_tool_error_maps_to_error_level(tmp_path):
    messages = [
        {
            "content": [{"id": "call_e", "name": "Bash", "input": {"command": "boom"}}],
            "model": "m",
            "event": "AssistantMessage",
        },
        {"content": [{"tool_use_id": "call_e", "content": "boom: not found", "is_error": True}], "event": "UserMessage"},
    ]
    children = RuntimeActivityExtractor(_settings(tmp_path)).sdk_child_observations(messages)
    tool = [c for c in children if c["kind"] == "tool"][0]
    assert tool["level"] == "ERROR"
    assert tool["output"] == "boom: not found"


def test_orphan_result_still_projected(tmp_path):
    messages = [
        {"content": [{"tool_use_id": "ghost", "content": "orphan result", "is_error": False}], "event": "UserMessage"},
    ]
    children = RuntimeActivityExtractor(_settings(tmp_path)).sdk_child_observations(messages)
    tools = [c for c in children if c["kind"] == "tool"]
    assert len(tools) == 1
    assert tools[0].get("input") is None
    assert tools[0]["output"] == "orphan result"


def test_empty_messages(tmp_path):
    assert RuntimeActivityExtractor(_settings(tmp_path)).sdk_child_observations([]) == []


def test_hostile_content_shapes_do_not_crash(tmp_path):
    messages = [
        {"content": None, "event": "UserMessage"},
        {"content": [{"id": "c", "name": "X", "input": [1, 2, {"nested": True}]}], "model": 123, "event": "AssistantMessage"},
        {"content": [{"tool_use_id": "c", "content": [{"type": "text", "text": "ok"}], "is_error": None}], "event": "UserMessage"},
        "not-a-dict",
        {"weird": object()},
    ]
    # 不得抛异常
    children = RuntimeActivityExtractor(_settings(tmp_path)).sdk_child_observations(messages)
    assert any(c["kind"] == "tool" and c["name"] == "sdk.tool.X" for c in children)


def test_emit_uses_parent_start_observation_and_ends(tmp_path):
    children = RuntimeActivityExtractor(_settings(tmp_path)).sdk_child_observations(_messages())
    parent = FakeParent()
    RuntimeLangfuseClient(_settings(tmp_path)).emit_sdk_child_observations(parent, children)

    assert len(parent.children) == len(children)
    assert all(c.ended for c in parent.children)                       # 每条都 end()
    names = [c.kwargs.get("name") for c in parent.children]
    assert "sdk.tool.Glob" in names and "sdk.llm.1" in names
    glob = next(c for c in parent.children if c.kwargs.get("name") == "sdk.tool.Glob")
    assert glob.kwargs["as_type"] == "tool"
    assert glob.kwargs["input"] == {"pattern": "*.py"}
    assert glob.kwargs["output"] == "a.py\nb.py"


def test_emit_no_children_is_noop(tmp_path):
    parent = FakeParent()
    RuntimeLangfuseClient(_settings(tmp_path)).emit_sdk_child_observations(parent, [])
    assert parent.children == []


def test_emit_falls_back_to_client_when_no_parent(tmp_path, monkeypatch):
    captured = FakeParent()  # 复用其 start_observation 作 client 级工厂
    client = RuntimeLangfuseClient(_settings(tmp_path))
    monkeypatch.setattr(client, "get_client", lambda: captured)
    children = RuntimeActivityExtractor(_settings(tmp_path)).sdk_child_observations(_messages())
    client.emit_sdk_child_observations(None, children)
    assert len(captured.children) == len(children)  # None parent → 走 ambient client 工厂


def test_governor_runner_glue_projects_children(tmp_path):
    """治理路径 glue：_emit_sdk_child_observations 从 messages 建 children 并以 parent=None 发射（ambient）。"""
    from types import SimpleNamespace

    from app.runtime.agent_job_runner import AgentJobRunner

    class CapturingLangfuse:
        def emit_sdk_child_observations(self, parent, children):
            self.parent = parent
            self.children = children

    lf = CapturingLangfuse()
    runner = AgentJobRunner(
        settings=_settings(tmp_path),
        profiles={},
        env_builder=lambda profile: {},
        output_formatter=SimpleNamespace(langfuse=lf),
    )
    runner._emit_sdk_child_observations(_messages())

    assert lf.parent is None  # governor → ambient root span
    assert any(c["name"] == "sdk.tool.Glob" for c in lf.children)
    assert any(c["kind"] == "generation" for c in lf.children)
