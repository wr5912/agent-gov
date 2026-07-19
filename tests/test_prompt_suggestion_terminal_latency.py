"""终态不得被可选的 Prompt Suggestion 扣住。

`--prompt-suggestions` 让 CLI 在每轮之后再吐一条「下一句可以问什么」。适配器为此在
ResultMessage 之后保留了 3 秒尾随窗口——但交互模式下 CLI 进程还活着、输出流不会关闭，
**没有建议就必定等满 3 秒**。答案早就写完了，`done` 却迟到 3 秒；前端 `onDone` 里
`setStreaming(false)` 才解除「停止」按钮，于是每个业务 Agent、每一轮对话都白等 3 秒，
这 3 秒里发不出下一句。讽刺的是，这功能本来就是为了加快「下一句」。

这里断言的是**生产默认值（3.0）下的终态时延**——不像
`test_interactive_trailing_timeout_does_not_fail_completed_result` 传 0.01 把问题绕过去：
那条证明的是「超时不会把成功 Run 弄失败」，而不是「用户不用等」。

契约：答案完成即收尾；建议若稍后到达，作为迟到帧从仍打开的流送出（Responses 投影层
早已把 prompt_suggestion 豁免于 done 守卫，正是为此预留）。

这些用例走**真实的 runtime.stream**，真实的 query_with_prompt_suggestions、真实的尾随
生成器、真实的 3.0 默认值；只把最底层的 SDK 客户端换成一个「像活着的 CLI 那样不关闭
输出流」的假客户端。
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from app.runtime import claude_prompt_suggestions
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.schemas import ChatRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings

from business_agent_test_utils import create_test_business_agent_workspace
from claude_runtime_test_utils import default_profile_resolver

TRAILING_WINDOW = claude_prompt_suggestions._TRAILING_TIMEOUT_SECONDS


def test_the_production_trailing_window_is_still_what_we_think_it_is() -> None:
    """若有人把默认窗口调小，下面的时延断言就不再证明原问题——先钉住前提。"""
    assert TRAILING_WINDOW >= 1.0, "尾随窗口已被调小；请重新评估本文件的时延断言是否仍在证明「终态被扣住」"


def _result_raw(session_id: str) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": session_id,
        "total_cost_usd": 0.0,
        "result": "answer",
    }


class _LiveCliQuery:
    """真实交互模式：ResultMessage 之后 CLI 进程仍在，输出流不关闭。

    这正是「必定等满 3 秒」的成因——非交互模式下流会关闭（StopAsyncIteration），
    尾随窗口立刻结束；税只压在交互模式，也就是所有 permissions.ask 非空的业务 Agent。
    """

    def __init__(self, options, session_id: str, *, suggestion_delay: float | None = None) -> None:
        self._options = options
        # 必须回显 options 里的 session id：runtime 会校验 ResultMessage 的 session,
        # 对不上就直接报错——那样本文件又会「因错误的原因变红」。
        self._session_id = session_id
        self._suggestion_delay = suggestion_delay

    async def receive_messages(self):
        # 真实 CLI 会把本轮写进 SDK session transcript；不写的话 runtime 落库会失败，
        # 本文件就又会「因错误的原因变红」。
        await self._options.session_store.append(
            {
                "project_key": self._options.session_store.binding.project_key,
                "session_id": self._session_id,
            },
            [{"type": "user", "uuid": "prompt-suggestion-latency-entry"}],
        )
        yield {
            "type": "assistant",
            "message": {"role": "assistant", "model": "fake-model", "content": [{"type": "text", "text": "answer"}]},
            "session_id": self._session_id,
        }
        yield _result_raw(self._session_id)
        if self._suggestion_delay is not None:
            await asyncio.sleep(self._suggestion_delay)
            yield {
                "type": "prompt_suggestion",
                "suggestion": "接下来可以问失败路径",
                "uuid": "suggestion-1",
                "session_id": self._session_id,
            }
        await asyncio.Event().wait()  # CLI 还活着，输出流永不关闭


def _install_live_cli(monkeypatch, *, suggestion_delay: float | None = None) -> None:
    """只换掉「进程 + 传输」，其余全走真实代码。

    刻意继承真实的 PromptSuggestionClaudeClient：``receive_response`` 不覆盖，于是跑的
    仍是真实的 ``_receive_messages_with_trailing_suggestion`` 与真实的 3.0 默认窗口。
    换掉的只有 connect（不拉起真 CLI 进程）和底层 _query（换成不关闭输出流的假 CLI）。

    必须打在交互客户端上：业务 Agent 的 workspace 有 permissions.ask，runtime 会走
    query_with_interactive_client，而不是 query_with_prompt_suggestions —— 税也正是
    只压在这条路径上。
    """

    class FakeInteractiveClient(claude_prompt_suggestions.PromptSuggestionClaudeClient):
        def __init__(self, *, options=None, transport=None) -> None:
            self.options = options
            self._prompt_suggestions_enabled = True
            session_id = getattr(options, "resume", None) or getattr(options, "session_id", None) or "sdk-session"
            self._query = _LiveCliQuery(options, session_id, suggestion_delay=suggestion_delay)
            self._control_task: asyncio.Task | None = None

        async def connect(self, prompt=None) -> None:
            if prompt is not None and hasattr(prompt, "__aiter__"):

                async def drain() -> None:
                    async for _ in prompt:
                        pass

                self._control_task = asyncio.create_task(drain())
                await asyncio.sleep(0)

        async def query(self, prompt, session_id: str = "default") -> None:
            return None

        async def disconnect(self) -> None:
            if self._control_task is not None:
                self._control_task.cancel()

    async def supported(_options) -> bool:
        return True

    monkeypatch.setattr(claude_prompt_suggestions, "_prompt_suggestions_supported", supported)
    monkeypatch.setattr(claude_prompt_suggestions, "PromptSuggestionClaudeClient", FakeInteractiveClient)
    monkeypatch.setattr(claude_prompt_suggestions, "ClaudeSDKClient", FakeInteractiveClient)


def _runtime(tmp_path) -> ClaudeRuntime:
    settings = AppSettings(
        _env_file=None,
        DATA_DIR=tmp_path / "docker" / "volume" / "data",
        GOVERNOR_CLAUDE_ROOT=tmp_path / "docker" / "volume" / "claude-roots" / "governor",
        RUNTIME_VOLUME_MODE="local-debug",
    )
    workspace = settings.default_workspace_dir
    create_test_business_agent_workspace(workspace, agent_id="main-agent", name="Main Agent")
    # 使用真实 endpoint fixture，覆盖 Claude Runtime 对 live workspace 原样配置的读取。
    (workspace / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"sec-ops-data": {"type": "http", "url": "http://localhost:58001/mcp"}}}, indent=2) + "\n",
        encoding="utf-8",
    )
    return ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), business_profile_resolver=default_profile_resolver(settings))


async def _stream_until_done(runtime: ClaudeRuntime) -> tuple[list[str], float, float]:
    """消费 runtime.stream 直到 done；返回事件序列、result 时延、done 时延。"""
    started = time.monotonic()
    events: list[str] = []
    result_at = 0.0
    source = runtime.stream(ChatRequest(message="hi"))
    async for frame in source:
        event = frame["event"]
        events.append(event)
        if event == "result":
            result_at = time.monotonic() - started
        if event == "done":
            return events, result_at, time.monotonic() - started
    raise AssertionError(f"流在 done 之前就结束了：{events}")


@pytest.mark.parametrize("suggestion_delay", [None, TRAILING_WINDOW / 2])
def test_terminal_does_not_wait_for_the_optional_suggestion(tmp_path, monkeypatch, suggestion_delay) -> None:
    """**核心用例**：答案完成后，done 必须立刻发出，不等尾随窗口。

    修复前：无建议时 done 迟到满 3 秒——每个业务 Agent、每一轮对话都白等，
    这 3 秒里「停止」按钮还挂着、发不出下一句。
    """
    _install_live_cli(monkeypatch, suggestion_delay=suggestion_delay)
    runtime = _runtime(tmp_path)

    events, result_at, done_at = asyncio.run(asyncio.wait_for(_stream_until_done(runtime), timeout=TRAILING_WINDOW + 10))

    assert "result" in events
    # 断言的是「答案到终态」的净差，而不是绝对时延——后者含 runtime 启动开销，
    # 会随环境浮动，测不准这笔税。
    tax = done_at - result_at
    assert tax < TRAILING_WINDOW / 3, (
        f"答案完成后又白等了 {tax:.2f}s 才收尾（尾随窗口 {TRAILING_WINDOW}s）：这段时间「停止」按钮还挂着、发不出下一句，而每个业务 Agent 每一轮都要交这笔税"
    )


def test_a_late_suggestion_still_reaches_the_client_after_the_terminal(tmp_path, monkeypatch) -> None:
    """建议迟到不该被丢掉：终态先发，建议作为迟到帧从仍打开的流送出。

    前端按 session 存建议（usePromptSuggestion 的 suggestionsBySession），与 run/流
    生命周期解耦，天然能接受晚到的帧；Responses 投影层也已把 prompt_suggestion
    豁免于 done 守卫。所以「不等」不等于「丢弃」。
    """
    _install_live_cli(monkeypatch, suggestion_delay=0.2)
    runtime = _runtime(tmp_path)

    async def collect() -> list[str]:
        events: list[str] = []
        async for frame in runtime.stream(ChatRequest(message="hi")):
            events.append(frame["event"])
            if frame["event"] == "prompt_suggestion":
                break
        return events

    events = asyncio.run(asyncio.wait_for(collect(), timeout=TRAILING_WINDOW + 10))

    assert "prompt_suggestion" in events, "建议被丢了——修时延不能把功能一起修没"
    assert events.index("done") < events.index("prompt_suggestion"), "终态应先于迟到的建议：这正是「不让可选增强扣住终态」的形状"
