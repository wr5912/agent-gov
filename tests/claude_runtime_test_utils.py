from __future__ import annotations

import asyncio
from typing import Any


def main_profile_resolver(settings: Any) -> Any:
    """最小 business profile resolver，把任意 agent_id 解析到 main workspace。

    runtime 不再持有预制 main profile——main 是可删除的普通业务 Agent，profile 一律由 resolver
    从注册表解析（生产装配见 main.py）。用例测的是 SDK/session/HITL 语义，profile 只是载体，
    因此这里直接构造，不引入注册表依赖。
    """

    from app.runtime.agent_profiles import build_business_agent_profile

    return lambda agent_id: build_business_agent_profile(
        settings,
        agent_id=agent_id or "main-agent",
        workspace_dir=settings.main_workspace_dir,
    )


def route_interactive_client_through_query(monkeypatch: Any) -> None:
    """Let runtime tests reuse their fake SDK query through the interactive client."""

    import claude_agent_sdk
    from app.runtime import claude_prompt_suggestions

    async def query_with_prompt_suggestions(*, prompt: Any, options: Any):
        async for message in claude_agent_sdk.query(prompt=prompt, options=options):
            yield message

    class QueryBackedClaudeSDKClient:
        def __init__(self, *, options: Any, transport: Any = None) -> None:
            self.options = options
            self.prompt: Any = None
            self.control_task: asyncio.Task[None] | None = None

        async def connect(self, control_stream: Any) -> None:
            async def consume_control_stream() -> None:
                async for _ in control_stream:
                    pass

            self.control_task = asyncio.create_task(consume_control_stream())
            await asyncio.sleep(0)

        async def query(self, prompt: Any, session_id: str = "default") -> None:
            self.prompt = prompt

        async def receive_response(self):
            async for message in claude_prompt_suggestions.query_with_prompt_suggestions(
                prompt=self.prompt,
                options=self.options,
            ):
                yield message

        async def disconnect(self) -> None:
            if self.control_task is not None:
                await self.control_task

    monkeypatch.setattr(claude_prompt_suggestions, "query_with_prompt_suggestions", query_with_prompt_suggestions)
    monkeypatch.setattr(claude_prompt_suggestions, "PromptSuggestionClaudeClient", QueryBackedClaudeSDKClient)
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", QueryBackedClaudeSDKClient)
