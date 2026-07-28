"""Prompt Suggestion 在终态前按配置做有界排空。

`--prompt-suggestions` 可能在 ResultMessage 之后才返回建议，而交互模式下 CLI 输出流
不会自行关闭。Runtime 必须同时满足两点：给建议一个短暂机会，又不能无限等待。因此这组
测试钉住生产默认 3 秒 drain 上限和终态最后性；配置窗口是明确 UX 预算，不是开放式尾随。

契约：Result 后最多等待配置的 drain 窗口；建议在窗口内到达则先发送建议再发送 done，
没有建议或超时则取消派生工作并发送 done。终态之后不再有业务帧。

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
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.schemas import ChatRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings

from business_agent_test_utils import create_test_business_agent_workspace
from claude_runtime_test_utils import default_profile_resolver

TRAILING_WINDOW = claude_prompt_suggestions._TRAILING_TIMEOUT_SECONDS


def test_the_production_trailing_window_is_still_what_we_think_it_is() -> None:
    """原生适配默认值应与部署级 Prompt drain 默认值保持一致。"""
    settings = AppSettings(_env_file=None)
    assert settings.prompt_suggestion_terminal_drain_seconds == TRAILING_WINDOW


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
    create_test_business_agent_workspace(
        workspace,
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
        name="Security Operations Expert",
    )
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
def test_terminal_wait_for_optional_suggestion_is_bounded(
    tmp_path,
    monkeypatch,
    suggestion_delay,
) -> None:
    """Result 到 done 的等待不得超过配置窗口；窗口内建议必须先于 done。"""
    _install_live_cli(monkeypatch, suggestion_delay=suggestion_delay)
    runtime = _runtime(tmp_path)

    events, result_at, done_at = asyncio.run(asyncio.wait_for(_stream_until_done(runtime), timeout=TRAILING_WINDOW + 10))

    assert "result" in events and events[-1] == "done"
    tax = done_at - result_at
    assert tax <= TRAILING_WINDOW + 1
    if suggestion_delay is None:
        assert tax >= TRAILING_WINDOW * 0.8
        assert "prompt_suggestion" not in events
    else:
        assert "prompt_suggestion" in events
        assert events.index("prompt_suggestion") < events.index("done")


def test_suggestion_within_drain_reaches_client_before_terminal(
    tmp_path,
    monkeypatch,
) -> None:
    """窗口内建议送达且 done 始终最后。"""
    _install_live_cli(monkeypatch, suggestion_delay=0.2)
    runtime = _runtime(tmp_path)

    async def collect() -> list[str]:
        events: list[str] = []
        async for frame in runtime.stream(ChatRequest(message="hi")):
            events.append(frame["event"])
            if frame["event"] == "done":
                break
        return events

    events = asyncio.run(asyncio.wait_for(collect(), timeout=TRAILING_WINDOW + 10))

    assert "prompt_suggestion" in events
    assert events.index("prompt_suggestion") < events.index("done")
    assert events[-1] == "done"
