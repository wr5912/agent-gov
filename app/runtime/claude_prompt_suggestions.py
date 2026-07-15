"""Python SDK 尚未暴露 Prompt Suggestion 时的窄适配边界。

除 ``prompt_suggestion`` 外，所有原始消息仍交给 claude-agent-sdk 官方 parser；
本模块不解析 transcript、不复制 transport，也不改变 Claude Code agent loop。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path

import claude_agent_sdk
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from claude_agent_sdk.types import Message

from app.runtime.async_iterators import close_async_iterator
from app.runtime.json_types import JsonObject

_PROMPT_SUGGESTIONS_FLAG = "prompt-suggestions"
_CLI_HELP_TIMEOUT_SECONDS = 10
_TRAILING_TIMEOUT_SECONDS = 3.0
_CLI_SUPPORT_CACHE: dict[tuple[str, int, int], bool] = {}

logger = logging.getLogger(__name__)

PromptInput = str | AsyncIterable[JsonObject]


@dataclass(frozen=True, slots=True)
class PromptSuggestionMessage:
    """经最小校验的上游 Prompt Suggestion；产品 ID 由 Runtime 另行注入。"""

    suggestion: str
    uuid: str | None = None
    session_id: str | None = None


def parse_prompt_suggestion(data: object) -> PromptSuggestionMessage | None:
    """解析单条上游建议；畸形建议只跳过，不影响本轮正式结果。"""
    if not isinstance(data, dict) or data.get("type") != "prompt_suggestion":
        return None
    value = data.get("suggestion")
    if not isinstance(value, str) or not value.strip():
        logger.warning("Skipping malformed Claude prompt_suggestion message")
        return None
    uuid = data.get("uuid")
    session_id = data.get("session_id")
    return PromptSuggestionMessage(
        suggestion=value.strip(),
        uuid=uuid if isinstance(uuid, str) else None,
        session_id=session_id if isinstance(session_id, str) else None,
    )


def _options_with_prompt_suggestions(options: ClaudeAgentOptions) -> ClaudeAgentOptions:
    extra_args = {_PROMPT_SUGGESTIONS_FLAG: None, **options.extra_args}
    return replace(options, extra_args=extra_args)


def _options_without_prompt_suggestions(options: ClaudeAgentOptions) -> ClaudeAgentOptions:
    extra_args = {key: value for key, value in options.extra_args.items() if key != _PROMPT_SUGGESTIONS_FLAG}
    return replace(options, extra_args=extra_args)


async def _resolve_cli_path(options: ClaudeAgentOptions) -> str:
    if options.cli_path is not None:
        return str(options.cli_path)
    # Python SDK 暂无公开的 bundled CLI 定位 API；把该私有 seam 隔离并由契约测试钉住。
    transport = SubprocessCLITransport(prompt="", options=options)
    return await asyncio.to_thread(transport._find_cli)


async def _read_cli_help(cli_path: str) -> str:
    process = await asyncio.create_subprocess_exec(
        cli_path,
        "--help",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=_CLI_HELP_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return stdout.decode("utf-8", errors="replace") if stdout else ""


async def _prompt_suggestions_supported(options: ClaudeAgentOptions) -> bool:
    try:
        cli_path = await _resolve_cli_path(options)
        stat = await asyncio.to_thread(Path(cli_path).stat)
        key = (cli_path, stat.st_mtime_ns, stat.st_size)
        cached = _CLI_SUPPORT_CACHE.get(key)
        if cached is not None:
            return cached
        supported = "--prompt-suggestions" in await _read_cli_help(cli_path)
        _CLI_SUPPORT_CACHE[key] = supported
        if not supported:
            logger.warning("Claude Code CLI does not support --prompt-suggestions; using the ordinary SDK query path")
        return supported
    except Exception as exc:
        logger.warning("Unable to verify Claude Code prompt suggestion support; using the ordinary SDK query path: %s", exc)
        return False


async def _receive_messages_with_trailing_suggestion(
    client: ClaudeSDKClient,
    *,
    trailing_timeout_seconds: float = _TRAILING_TIMEOUT_SECONDS,
) -> AsyncIterator[Message | PromptSuggestionMessage]:
    """读取正式结果，并为可能晚于 Result 的建议保留有限尾随窗口。"""
    raw_query = client._query
    if raw_query is None:
        raise RuntimeError("Claude SDK client connected without an internal query stream")

    raw_stream = raw_query.receive_messages()
    result_seen = False
    suggestion_seen = False
    trailing_deadline: float | None = None
    try:
        while True:
            try:
                if trailing_deadline is None:
                    data = await anext(raw_stream)
                else:
                    remaining = trailing_deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        return
                    data = await asyncio.wait_for(anext(raw_stream), timeout=remaining)
            except (StopAsyncIteration, TimeoutError):
                # Prompt Suggestion 是增强能力；缺失或超时不改变主 Run 的成功状态。
                return

            suggestion = parse_prompt_suggestion(data)
            if suggestion is not None:
                suggestion_seen = True
                yield suggestion
            elif data.get("type") != "prompt_suggestion":
                message = parse_message(data)
                if message is not None:
                    yield message
                    if isinstance(message, ResultMessage):
                        result_seen = True
                        trailing_deadline = asyncio.get_running_loop().time() + trailing_timeout_seconds

            if result_seen and suggestion_seen:
                return
    finally:
        await close_async_iterator(raw_stream)


class PromptSuggestionClaudeClient(ClaudeSDKClient):
    """为 SDK 交互客户端补充 Prompt Suggestion，不改变控制通道生命周期。"""

    def __init__(self, options: ClaudeAgentOptions | None = None) -> None:
        super().__init__(options=options)
        self._prompt_suggestions_enabled = False

    async def connect(self, prompt: PromptInput | None = None) -> None:
        self._prompt_suggestions_enabled = await _prompt_suggestions_supported(self.options)
        self.options = _options_with_prompt_suggestions(self.options) if self._prompt_suggestions_enabled else _options_without_prompt_suggestions(self.options)
        await super().connect(prompt)

    async def receive_response(self) -> AsyncIterator[Message | PromptSuggestionMessage]:
        if not self._prompt_suggestions_enabled:
            async for message in super().receive_response():
                yield message
            return
        async for message in _receive_messages_with_trailing_suggestion(self):
            yield message


async def query_with_prompt_suggestions(
    *,
    prompt: PromptInput,
    options: ClaudeAgentOptions,
) -> AsyncIterator[Message | PromptSuggestionMessage]:
    """执行一次 SDK query，并在不截断正式消息流的前提下暴露 Prompt Suggestion。"""
    if not await _prompt_suggestions_supported(options):
        fallback_options = _options_without_prompt_suggestions(options)
        async for message in claude_agent_sdk.query(prompt=prompt, options=fallback_options):
            yield message
        return

    client = ClaudeSDKClient(options=_options_with_prompt_suggestions(options))
    try:
        await client.connect(prompt)
        async for message in _receive_messages_with_trailing_suggestion(client):
            yield message
    finally:
        await client.disconnect()
