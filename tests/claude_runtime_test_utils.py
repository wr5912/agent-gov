from __future__ import annotations

import asyncio
from typing import Any


def route_interactive_client_through_query(monkeypatch: Any) -> None:
    """Let runtime tests reuse their fake SDK query through the interactive client."""

    import claude_agent_sdk

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
            async for message in claude_agent_sdk.query(prompt=self.prompt, options=self.options):
                yield message

        async def disconnect(self) -> None:
            if self.control_task is not None:
                await self.control_task

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", QueryBackedClaudeSDKClient)
