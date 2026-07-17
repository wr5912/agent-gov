"""后端稳定生成 Prompt Suggestion(受控特例)的确定性测试。

生成器本身要调 LLM —— 测试里**一律 mock litellm.completion / generate**,不碰真模型,
保证确定性。真模型的稳定性是人工/部署验收的事,不进单测。

覆盖:
- 生成器:正常返回清洗后的建议;推理模型 content 为空 → None;异常 → None(best-effort);
  _clean 去引号/前缀/多行。
- 运行时集成:开关开启时,runtime.stream 在答案完成(done)后 emit 一条后端生成的
  prompt_suggestion(走 SDK 原生 query、无 CLI 尾随窗口);开关关闭时不生成。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from app.runtime import claude_runtime_stream as crs
from app.runtime.business_agent_workspace import seed_business_agent_workspace
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.prompt_suggestion_generator import PromptSuggestionGenerator, _clean
from app.runtime.schemas import ChatRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock


def _settings(tmp_path, **overrides) -> AppSettings:
    return AppSettings(
        _env_file=None,
        DATA_DIR=tmp_path / "docker" / "volume" / "data",
        GOVERNOR_CLAUDE_ROOT=tmp_path / "docker" / "volume" / "claude-roots" / "governor",
        RUNTIME_VOLUME_MODE="local-debug",
        AGENT_MODEL="deepseek-v4-flash",
        MODEL_PROVIDER_API_URL="https://api.example.test/anthropic",
        MODEL_PROVIDER_API_KEY="k",
        **overrides,
    )


# ---------------------------------------------------------------- 生成器单测


def _fake_completion(content):
    def _call(**_kwargs):
        return {"choices": [{"message": {"content": content}}]}

    return _call


def test_generator_returns_clean_suggestion(tmp_path, monkeypatch) -> None:
    gen = PromptSuggestionGenerator(_settings(tmp_path))
    monkeypatch.setattr(
        "app.runtime.prompt_suggestion_generator.litellm.completion",
        _fake_completion("  「跑测试」  "),
    )
    assert gen.generate("修好 bug 再跑测试", "已修复空指针") == "跑测试"


def test_generator_returns_none_when_model_content_empty(tmp_path, monkeypatch) -> None:
    """推理模型思考吃光 token / 明确沉默 → content 空 → None(不 emit)。"""
    gen = PromptSuggestionGenerator(_settings(tmp_path))
    monkeypatch.setattr(
        "app.runtime.prompt_suggestion_generator.litellm.completion",
        _fake_completion(None),
    )
    assert gen.generate("完成型请求", "已交付") is None


def test_generator_swallows_errors(tmp_path, monkeypatch) -> None:
    """硬边界:LLM 调用抛异常也绝不冒泡,返回 None。"""
    gen = PromptSuggestionGenerator(_settings(tmp_path))

    def _boom(**_kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("app.runtime.prompt_suggestion_generator.litellm.completion", _boom)
    assert gen.generate("x", "y") is None


def test_generator_skips_empty_conversation(tmp_path, monkeypatch) -> None:
    gen = PromptSuggestionGenerator(_settings(tmp_path))
    monkeypatch.setattr(
        "app.runtime.prompt_suggestion_generator.litellm.completion",
        _fake_completion("不该被调用"),
    )
    assert gen.generate("", "") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"跑测试"', "跑测试"),
        ("建议：提交代码", "提交代码"),
        ("第一行\n第二行", "第一行"),
        ("   ", None),
        ("x" * 200, "x" * 60),
    ],
)
def test_clean_normalizes_output(raw, expected) -> None:
    assert _clean(raw) == expected


# ---------------------------------------------------------------- 运行时集成


def _runtime(tmp_path, monkeypatch, *, enabled: bool):
    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        sid = options.resume or options.session_id
        await options.session_store.append(
            {"project_key": options.session_store.binding.project_key, "session_id": sid},
            [{"type": "user", "uuid": "be-entry"}],
        )
        yield AssistantMessage(content=[TextBlock(text="答案正文")], model="m", session_id=sid)
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=0, is_error=False,
                            num_turns=1, session_id=sid, result="答案正文")

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    monkeypatch.setattr(crs, "read_requires_web_hitl", lambda _w: False)
    # 关闭态走 CLI 路径:让它探测「不支持」从而干净回退到上面 patch 的 fake query,
    # 否则会去起真 CLI 进程挂住。开启态走 SDK 原生 query,不受影响。
    from app.runtime import claude_prompt_suggestions

    async def _unsupported(_options):
        return False

    monkeypatch.setattr(claude_prompt_suggestions, "_prompt_suggestions_supported", _unsupported)

    settings = _settings(tmp_path, ENABLE_BACKEND_PROMPT_SUGGESTION=enabled)
    workspace = settings.main_workspace_dir
    seed_business_agent_workspace(workspace, agent_id="main-agent", name="Main Agent")
    (workspace / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"sec-ops-data": {"type": "http", "url": "http://localhost:58001/mcp"}}}) + "\n",
        encoding="utf-8",
    )
    return ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))


def test_runtime_emits_backend_generated_suggestion_after_done(tmp_path, monkeypatch) -> None:
    """开关开启:runtime.stream 在 done 之后 emit 一条后端生成的建议。"""
    runtime = _runtime(tmp_path, monkeypatch, enabled=True)
    captured = {}

    def fake_generate(user_message, agent_answer):
        captured["user"] = user_message
        captured["answer"] = agent_answer
        return "跑测试"

    monkeypatch.setattr(runtime.prompt_suggestion_generator, "generate", fake_generate)

    async def collect():
        return [e async for e in runtime.stream(ChatRequest(message="修好 bug 再跑测试"))]

    events = asyncio.run(asyncio.wait_for(collect(), timeout=30))
    names = [e["event"] for e in events]

    assert "prompt_suggestion" in names, f"未 emit 后端生成的建议:{names}"
    suggestion = next(e for e in events if e["event"] == "prompt_suggestion")
    assert suggestion["data"]["suggestion"] == "跑测试"
    # 迟到帧:建议在 done 之后(答案完成即收尾,生成不扣终态)
    assert names.index("prompt_suggestion") > names.index("done")
    # grounding:拿到了用户输入与助手回答
    assert captured["user"] == "修好 bug 再跑测试"
    assert "答案正文" in captured["answer"]


def test_runtime_does_not_generate_when_disabled(tmp_path, monkeypatch) -> None:
    """开关关闭:不走后端生成(回退 CLI 路径),不 emit 后端建议。"""
    runtime = _runtime(tmp_path, monkeypatch, enabled=False)

    def _boom(*_a, **_k):
        raise AssertionError("关闭时不应调用后端生成器")

    monkeypatch.setattr(runtime.prompt_suggestion_generator, "generate", _boom)

    async def collect():
        return [e async for e in runtime.stream(ChatRequest(message="hi"))]

    events = asyncio.run(asyncio.wait_for(collect(), timeout=30))
    assert "prompt_suggestion" not in [e["event"] for e in events]


def test_runtime_skips_emit_when_generator_returns_none(tmp_path, monkeypatch) -> None:
    """生成器返回 None(无意义/失败)时不 emit,不留空建议。"""
    runtime = _runtime(tmp_path, monkeypatch, enabled=True)
    monkeypatch.setattr(runtime.prompt_suggestion_generator, "generate", lambda *_a: None)

    async def collect():
        return [e async for e in runtime.stream(ChatRequest(message="hi"))]

    events = asyncio.run(asyncio.wait_for(collect(), timeout=30))
    assert "prompt_suggestion" not in [e["event"] for e in events]
