"""Prompt Suggestion 各层的**确定性**测试模板。

真模型(deepseek 及其他)生成建议是非确定性的、且大多沉默(见 SUGGESTION MODE 指令
"Or nothing / Stay silent"),不能作为测试触发条件。所以这里**一律注入合成建议**、
不碰模型,逐层验证「一条 prompt_suggestion 从 SDK 边界一路正确流到线上契约」。

分层:
- 层① 适配器:claude_prompt_suggestions —— 把 CLI 的 raw `prompt_suggestion` 消息
  解析成 PromptSuggestionMessage。
- 层② 后端 SSE 契约:openai_responses_stream —— 把 `prompt_suggestion` 帧投影成
  `agentgov.prompt_suggestion` 信封;成功轮里可**晚于 done**(迟到帧),失败轮丢弃。
- 端到端:真实 runtime.stream → 真实投影,证明「答案完成→done 提前→建议随后」这条真实
  时序下建议仍能到达前端。这一层此前缺失,正是它让「迟到建议被投影丢弃」的回归漏过。

前端层(hook / 组件)**目前没有自动化单测** —— 仓库未引入 vitest;唯一的前端覆盖是
Playwright `scripts/verify_message_actions_browser.mjs`。此处如实记录,不谎报覆盖。
"""

from __future__ import annotations

import asyncio

from app.runtime import claude_prompt_suggestions as ps
from app.runtime.claude_prompt_suggestions import (
    PromptSuggestionMessage,
    query_with_prompt_suggestions,
)
from app.runtime.openai_responses_stream import iter_responses_sse
from claude_agent_sdk import ClaudeAgentOptions

# ---------------------------------------------------------------- 注入用的假 CLI


class _FakeRawQuery:
    """伪造 CLI 的原始消息流:先给正常 assistant+result,再塞一条建议。"""

    def __init__(self, raw: list[dict]) -> None:
        self._raw = raw

    async def receive_messages(self):
        for message in self._raw:
            yield message


class _FakeClient:
    """替身 ClaudeSDKClient:不起真进程、不碰模型,只回放预置消息。"""

    def __init__(self, options: ClaudeAgentOptions) -> None:
        self.options = options
        self._query = _FakeRawQuery(
            [
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "model": "m", "content": [{"type": "text", "text": "答案"}]},
                    "session_id": "sdk-session",
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "duration_ms": 1,
                    "duration_api_ms": 1,
                    "is_error": False,
                    "num_turns": 1,
                    "session_id": "sdk-session",
                    "result": "答案",
                },
                # ↓↓↓ 这一条就是「注入的建议」——层① 要把它解析出来
                {
                    "type": "prompt_suggestion",
                    "suggestion": "  接下来检查失败路径  ",
                    "uuid": "suggestion-1",
                    "session_id": "sdk-session",
                },
            ]
        )
        self.disconnected = False

    async def connect(self, prompt) -> None:
        self._prompt = prompt

    async def disconnect(self) -> None:
        self.disconnected = True


async def _supported(_options: ClaudeAgentOptions) -> bool:
    return True


# ---------------------------------------------------------------- 层① 适配器


def test_layer1_adapter_parses_injected_suggestion(monkeypatch) -> None:
    """层①:注入一条 raw prompt_suggestion → 适配器产出 PromptSuggestionMessage。

    同时验证 uuid/session_id 透传、首尾空白被 strip。
    """
    monkeypatch.setattr(ps, "ClaudeSDKClient", _FakeClient)
    monkeypatch.setattr(ps, "_prompt_suggestions_supported", _supported)

    async def collect():
        return [m async for m in query_with_prompt_suggestions(prompt="hi", options=ClaudeAgentOptions())]

    messages = asyncio.run(collect())

    suggestions = [m for m in messages if isinstance(m, PromptSuggestionMessage)]
    assert len(suggestions) == 1, f"应恰好解析出 1 条建议,实得 {len(suggestions)}"
    assert suggestions[0] == PromptSuggestionMessage("接下来检查失败路径", "suggestion-1", "sdk-session")


def test_layer1_adapter_skips_malformed_suggestion(monkeypatch) -> None:
    """层① 负向:畸形(空白/非字符串)建议被跳过,不影响主结果。"""

    class _Malformed(_FakeClient):
        def __init__(self, options):
            super().__init__(options)
            self._query = _FakeRawQuery([{"type": "prompt_suggestion", "suggestion": "   "}, {"type": "prompt_suggestion", "suggestion": {"hostile": True}}])

    monkeypatch.setattr(ps, "ClaudeSDKClient", _Malformed)
    monkeypatch.setattr(ps, "_prompt_suggestions_supported", _supported)

    async def collect():
        return [m async for m in query_with_prompt_suggestions(prompt="hi", options=ClaudeAgentOptions())]

    assert asyncio.run(collect()) == []


# ---------------------------------------------------------------- 层② 后端 SSE 契约


def _sse_events(frames: list[dict]) -> list[tuple[str, dict]]:
    """把帧序列过一遍 Responses 投影,返回 (事件名, data) 列表。"""

    async def _aiter():
        for f in frames:
            yield f

    async def go() -> str:
        chunks = []
        async for chunk in iter_responses_sse(_aiter(), model="m", effective_agent_id="soc-ops", control=True):
            chunks.append(chunk)
        return "".join(chunks)

    import json

    text = asyncio.run(go())
    out: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        name = None
        data = {}
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                raw = line[len("data:") :].strip()
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = {"_raw": raw}
        if name:
            out.append((name, data))
    return out


_SESSION = {
    "event": "session",
    "data": {"run_id": "run-9", "session_id": "sess-9", "sdk_session_id": "sdk-9", "agent_version_id": "ver-9", "agent_id": "soc-ops"},
}
_RESULT = {
    "event": "result",
    "data": {"run_id": "run-9", "session_id": "sess-9", "sdk_session_id": "sdk-9", "usage": {}, "stop_reason": "end_turn", "errors": [], "agent_activity": {}},
}
_DONE = {"event": "done", "data": "[DONE]"}


def test_layer2_projects_suggestion_to_agentgov_envelope_before_done() -> None:
    """层②:建议在 done **之前**到达时,被投影成 `agentgov.prompt_suggestion` 信封。

    这是「早到」路径(原始设计);「晚到」路径见
    test_layer2_delivers_late_suggestion_after_done_on_success。两条一起覆盖修复后的契约:
    成功轮无论建议早于还是晚于 done 都送达,只有失败轮丢弃。
    """
    suggestion_frame = {
        "event": "prompt_suggestion",
        "data": {"suggestion": "  接下来检查失败路径  ", "run_id": "run-9", "session_id": "sess-9"},
    }
    events = _sse_events([_SESSION, _RESULT, suggestion_frame, _DONE])
    names = [n for n, _ in events]

    assert "agentgov.prompt_suggestion" in names, "建议未被投影成 SSE 信封"
    payload = dict(events)["agentgov.prompt_suggestion"]
    body = payload.get("payload", payload)
    assert body["suggestion"] == "接下来检查失败路径", "应 strip 首尾空白"
    assert body["session_id"] == "sess-9"
    assert names.index("agentgov.prompt_suggestion") > names.index("agentgov.result")
    assert names.index("agentgov.prompt_suggestion") < names.index("agentgov.done")


def test_layer2_delivers_late_suggestion_after_done_on_success() -> None:
    """层② **回归**:成功轮里 done 之后到达的建议必须仍被投影(迟到帧)。

    这正是曾经断掉的场景:批5 让 runtime 在答案完成时即发 done、建议随后才到,而投影
    `_project_prompt_suggestion` 原来带 `done_emitted / terminal_status is not None` 守卫,
    把成功轮 done 之后的建议丢掉 → 前端永远收不到。修复后只在**失败**时丢弃。
    """
    late = {"event": "prompt_suggestion", "data": {"suggestion": "接下来检查失败路径", "session_id": "sess-9"}}
    events = _sse_events([_SESSION, _RESULT, _DONE, late])
    names = [n for n, _ in events]
    assert "agentgov.prompt_suggestion" in names, "成功轮 done 之后的迟到建议必须送达(修复点)"
    body = dict(events)["agentgov.prompt_suggestion"]
    assert body.get("payload", body)["suggestion"] == "接下来检查失败路径"


def test_layer2_drops_late_suggestion_after_failure() -> None:
    """层② 负向:失败轮不应给建议(即便建议帧到达)。"""
    result_err = {"event": "result", "data": {**_RESULT["data"], "errors": ["boom"]}}
    late = {"event": "prompt_suggestion", "data": {"suggestion": "别在失败时建议", "session_id": "sess-9"}}
    events = _sse_events([_SESSION, result_err, _DONE, late])
    names = [n for n, _ in events]
    assert "agentgov.prompt_suggestion" not in names, "失败轮不应投影建议"


# ---------------------------------------------------------------- 端到端(runtime → 投影)


def test_endtoend_late_suggestion_reaches_sse_after_early_done(tmp_path, monkeypatch) -> None:
    """**回归护栏**:真实 runtime.stream → 真实投影,迟到建议必须到达前端 SSE。

    复刻真实时序:答案完成 → runtime 提前发 done → 建议随后到(runtime 帧序里建议在
    done 之后)。此前投影会丢弃它,前端永远收不到;这条端到端断言 `agentgov.prompt_suggestion`
    确实出现在投影后的 SSE 里。缺这一层测试,是当初回归漏过的根因。
    """
    import json

    from app.runtime.claude_runtime import ClaudeRuntime
    from app.runtime.schemas import ChatRequest
    from app.runtime.session_store import LocalSessionStore
    from app.runtime.settings import AppSettings
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    from business_agent_test_utils import create_test_business_agent_workspace
    from claude_runtime_test_utils import default_profile_resolver

    async def fake_query(*, prompt, options):
        async for _ in prompt:
            pass
        sid = options.resume or options.session_id
        await options.session_store.append(
            {"project_key": options.session_store.binding.project_key, "session_id": sid},
            [{"type": "user", "uuid": "e2e-entry"}],
        )
        yield AssistantMessage(content=[TextBlock(text="答案")], model="m", session_id=sid)
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=0, is_error=False, num_turns=1, session_id=sid, result="答案")
        # 建议在答案之后才生成 —— 真实时序,到达时 done 已发出
        yield PromptSuggestionMessage("接下来检查失败路径", "u1", sid)

    monkeypatch.setattr(ps, "query_with_prompt_suggestions", fake_query)
    monkeypatch.setattr("app.runtime.claude_runtime_stream.read_requires_web_hitl", lambda _w: False)

    settings = AppSettings(
        _env_file=None,
        DATA_DIR=tmp_path / "docker" / "volume" / "data",
        GOVERNOR_CLAUDE_ROOT=tmp_path / "docker" / "volume" / "claude-roots" / "governor",
        RUNTIME_VOLUME_MODE="local-debug",
    )
    workspace = settings.default_workspace_dir
    create_test_business_agent_workspace(workspace, agent_id="main-agent", name="Main Agent")
    (workspace / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"sec-ops-data": {"type": "http", "url": "http://localhost:58001/mcp"}}}) + "\n",
        encoding="utf-8",
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), business_profile_resolver=default_profile_resolver(settings))

    async def run() -> tuple[list[str], list[str]]:
        raw_frames: list[str] = []

        async def source():
            async for frame in runtime.stream(ChatRequest(message="hi")):
                raw_frames.append(frame["event"])
                yield frame

        sse_events: list[str] = []
        async for chunk in iter_responses_sse(source(), model="m", effective_agent_id="main-agent", control=True):
            for line in chunk.split("\n"):
                if line.startswith("event:"):
                    sse_events.append(line[len("event:") :].strip())
        return raw_frames, sse_events

    raw_frames, sse_events = asyncio.run(asyncio.wait_for(run(), timeout=30))

    # 前提:runtime 里建议确实排在 done 之后(否则这条测不到真问题)
    assert raw_frames.index("prompt_suggestion") > raw_frames.index("done"), f"runtime 帧序应为建议晚于 done,实得 {raw_frames}"
    # 核心:投影后前端能收到建议
    assert "agentgov.prompt_suggestion" in sse_events, f"迟到建议被投影丢弃,前端收不到。SSE={sse_events}"


def test_layer2_projects_full_candidate_list_with_compat_first_item() -> None:
    """层②:多候选一帧投影 —— `suggestions` 是完整列表,`suggestion` 恒等首条。

    附加式形状的核心断言:老客户端只读 `suggestion` 就仍拿到最贴切的那条(不是最差的),
    新客户端读 `suggestions` 拿全部。逐条 strip。
    """
    frame = {
        "event": "prompt_suggestion",
        "data": {
            "suggestion": "  跑测试  ",
            "suggestions": ["  跑测试  ", "看日志", "  提交代码"],
            "session_id": "sess-9",
        },
    }
    events = _sse_events([_SESSION, _RESULT, frame, _DONE])
    body = dict(events)["agentgov.prompt_suggestion"]
    payload = body.get("payload", body)

    assert payload["suggestions"] == ["跑测试", "看日志", "提交代码"]
    assert payload["suggestion"] == "跑测试" == payload["suggestions"][0]


def test_layer2_tolerates_frame_without_suggestions_key() -> None:
    """层② 兼容:只带 `suggestion` 的旧帧(或未同步的 emitter)归一成单元素,不静默丢。"""
    frame = {"event": "prompt_suggestion", "data": {"suggestion": "跑测试", "session_id": "sess-9"}}
    events = _sse_events([_SESSION, _RESULT, frame, _DONE])
    body = dict(events)["agentgov.prompt_suggestion"]
    payload = body.get("payload", body)

    assert payload["suggestion"] == "跑测试"
    assert payload["suggestions"] == ["跑测试"]
