from __future__ import annotations

import asyncio

from app.runtime.schemas import ChatRequest
from app.runtime.sdk_session_store import SqliteSdkSessionStore

from app_test_utils import load_test_app


def test_stream_keeps_turn_running_until_post_result_transcript_flush(
    monkeypatch,
    tmp_path,
) -> None:
    """SDK 允许 transcript batcher 在 ResultMessage 后 flush，公开终态不能抢先关闭写栅栏。"""

    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    post_result_flushed = False
    session_key: dict[str, str] = {}

    async def fake_query(*, prompt, options, transport=None):
        nonlocal post_result_flushed
        async for _ in prompt:
            pass
        sdk_session_id = options.resume or options.session_id
        key = {
            "project_key": options.session_store.binding.project_key,
            "session_id": sdk_session_id,
        }
        session_key.update(key)
        await options.session_store.append(
            key,
            [{"type": "user", "uuid": "before-result"}],
        )
        yield AssistantMessage(
            content=[TextBlock(text="完成")],
            model="<synthetic>",
            session_id=sdk_session_id,
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id=sdk_session_id,
            result="完成",
        )
        await options.session_store.append(
            key,
            [{"type": "assistant", "uuid": "after-result"}],
        )
        post_result_flushed = True

    module = load_test_app(
        monkeypatch,
        tmp_path,
        requires_web_hitl=False,
    )
    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    module.settings.enable_backend_prompt_suggestion = True
    monkeypatch.setattr(module.runtime.prompt_suggestion_generator, "generate", lambda *_args: [])

    async def consume() -> list[dict[str, object]]:
        return [
            event
            async for event in module.runtime.stream(
                ChatRequest(message="flush transcript", session_id="sess-post-result"),
            )
        ]

    events = asyncio.run(consume())
    names = [event["event"] for event in events]
    assert post_result_flushed is True
    assert names[-2:] == ["result", "done"]
    assert "error" not in names

    saved = module.session_store.get("sess-post-result")
    assert saved is not None and saved.turns == 1 and saved.active_run_id is None
    committed = SqliteSdkSessionStore.committed(module.session_store.Session)
    entries = asyncio.run(committed.load(session_key))
    assert entries is not None
    assert [entry["uuid"] for entry in entries] == [
        "before-result",
        "after-result",
    ]
