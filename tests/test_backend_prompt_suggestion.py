"""后端稳定生成 Prompt Suggestion(受控特例)的确定性测试。

生成器本身要调 LLM —— 测试里**一律 mock litellm.completion / generate**,不碰真模型,
保证确定性。真模型的稳定性是人工/部署验收的事,不进单测。

覆盖:
- 生成器:多行 → 至多 N 条(清洗/剥序号/去重/保序/**绝不补齐**);content 空 → [];
  异常 → [](best-effort);坏 count 在使用点 clamp 不崩。
- 运行时集成:开关开启时,runtime.stream 在答案完成(done)后 emit **恰好一帧**载整批候选
  (走 SDK 原生 query、无 CLI 尾随窗口);N=1 时 payload 向后兼容;开关关闭时不生成。
- 两通道一致性:后端生成与原生 CLI 两个 emitter 必须同形状,不留 schema 双轨。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from app.runtime import claude_runtime_stream as crs
from app.runtime.business_agent_workspace import seed_business_agent_workspace
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.prompt_suggestion_generator import PromptSuggestionGenerator, _clean, _clean_many
from app.runtime.schemas import ChatRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from claude_runtime_test_utils import main_profile_resolver


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


def test_generator_returns_clean_suggestions(tmp_path, monkeypatch) -> None:
    """模型多行输出 → 至多 N 条、逐条清洗、保序。"""
    gen = PromptSuggestionGenerator(_settings(tmp_path))
    monkeypatch.setattr(
        "app.runtime.prompt_suggestion_generator.litellm.completion",
        _fake_completion("  「跑测试」  \n看日志\n提交代码"),
    )
    assert gen.generate("修好 bug 再跑测试", "已修复空指针") == ["跑测试", "看日志", "提交代码"]


def test_generator_never_pads_to_reach_count(tmp_path, monkeypatch) -> None:
    """**绝不补齐**:模型只给 2 条就出 2 条 —— 凑数的建议比没有更糟。"""
    gen = PromptSuggestionGenerator(_settings(tmp_path, BACKEND_PROMPT_SUGGESTION_COUNT=3))
    monkeypatch.setattr(
        "app.runtime.prompt_suggestion_generator.litellm.completion",
        _fake_completion("跑测试\n看日志"),
    )
    assert gen.generate("修 bug", "已修复") == ["跑测试", "看日志"]


def test_generator_truncates_to_configured_count(tmp_path, monkeypatch) -> None:
    gen = PromptSuggestionGenerator(_settings(tmp_path, BACKEND_PROMPT_SUGGESTION_COUNT=2))
    monkeypatch.setattr(
        "app.runtime.prompt_suggestion_generator.litellm.completion",
        _fake_completion("a\nb\nc\nd"),
    )
    assert gen.generate("x", "y") == ["a", "b"]


@pytest.mark.parametrize("configured", [0, -5, 99])
def test_generator_clamps_bad_count_instead_of_crashing(tmp_path, monkeypatch, configured) -> None:
    """配置写错值不得崩:在使用点 clamp 到 1..5(本模块信条:失败不影响主 Run)。"""
    gen = PromptSuggestionGenerator(_settings(tmp_path, BACKEND_PROMPT_SUGGESTION_COUNT=configured))
    monkeypatch.setattr(
        "app.runtime.prompt_suggestion_generator.litellm.completion",
        _fake_completion("\n".join(f"s{i}" for i in range(10))),
    )
    got = gen.generate("x", "y")
    assert 1 <= len(got) <= 5


def test_generator_returns_empty_when_model_content_empty(tmp_path, monkeypatch) -> None:
    """推理模型思考吃光 token / 明确沉默 → content 空 → [](不 emit)。"""
    gen = PromptSuggestionGenerator(_settings(tmp_path))
    monkeypatch.setattr(
        "app.runtime.prompt_suggestion_generator.litellm.completion",
        _fake_completion(None),
    )
    assert gen.generate("完成型请求", "已交付") == []


def test_generator_swallows_errors(tmp_path, monkeypatch) -> None:
    """硬边界:LLM 调用抛异常也绝不冒泡,返回 []。"""
    gen = PromptSuggestionGenerator(_settings(tmp_path))

    def _boom(**_kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("app.runtime.prompt_suggestion_generator.litellm.completion", _boom)
    assert gen.generate("x", "y") == []


def test_generator_skips_empty_conversation(tmp_path, monkeypatch) -> None:
    gen = PromptSuggestionGenerator(_settings(tmp_path))
    monkeypatch.setattr(
        "app.runtime.prompt_suggestion_generator.litellm.completion",
        _fake_completion("不该被调用"),
    )
    assert gen.generate("", "") == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"跑测试"', "跑测试"),
        ("建议：提交代码", "提交代码"),
        # 前缀与引号并存:必须先剥前缀再去引号,否则左引号被前缀挡住、留下 `"跑测试`
        ('建议："跑测试"', "跑测试"),
        ("第一行\n第二行", "第一行"),
        ("   ", None),
        ("x" * 200, "x" * 60),
    ],
)
def test_clean_normalizes_output(raw, expected) -> None:
    assert _clean(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("跑测试\n看日志\n提交", ["跑测试", "看日志", "提交"]),
        # 模型偶尔自己加序号/项目符,要剥掉
        ("1. 跑测试\n2) 看日志\n3、提交", ["跑测试", "看日志", "提交"]),
        ("- 跑测试\n* 看日志\n• 提交", ["跑测试", "看日志", "提交"]),
        # 去重按归一化 key(忽略标点差异),保序
        ("跑测试\n跑测试。\n看日志", ["跑测试", "看日志"]),
        # 空行不占名额
        ("跑测试\n\n\n看日志", ["跑测试", "看日志"]),
        ("   \n\n  ", []),
        ("", []),
    ],
)
def test_clean_many_parses_lines(raw, expected) -> None:
    assert _clean_many(raw, 3) == expected


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
    return ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), business_profile_resolver=main_profile_resolver(settings))


def test_runtime_emits_backend_generated_suggestion_after_done(tmp_path, monkeypatch) -> None:
    """开关开启:runtime.stream 在 done 之后 emit **恰好一帧**、载完整候选批次。

    一帧载整批(而非每条一帧)是刻意的:投影层不透传 run_id,客户端拿不到 batch key;
    done 之后到建议之间用户可以发下一轮,多帧会让两轮候选混在一起且不收敛。
    """
    runtime = _runtime(tmp_path, monkeypatch, enabled=True)
    captured = {}

    def fake_generate(user_message, agent_answer):
        captured["user"] = user_message
        captured["answer"] = agent_answer
        return ["跑测试", "看日志", "提交代码"]

    monkeypatch.setattr(runtime.prompt_suggestion_generator, "generate", fake_generate)

    async def collect():
        return [e async for e in runtime.stream(ChatRequest(message="修好 bug 再跑测试"))]

    events = asyncio.run(asyncio.wait_for(collect(), timeout=30))
    names = [e["event"] for e in events]

    assert names.count("prompt_suggestion") == 1, f"必须恰好一帧载整批:{names}"
    suggestion = next(e for e in events if e["event"] == "prompt_suggestion")
    assert suggestion["data"]["suggestions"] == ["跑测试", "看日志", "提交代码"]
    # 附加式:`suggestion` 保留且恒等首条(老客户端零改动)
    assert suggestion["data"]["suggestion"] == "跑测试"
    # 迟到帧:建议在 done 之后(答案完成即收尾,生成不扣终态)
    assert names.index("prompt_suggestion") > names.index("done")
    # grounding:拿到了用户输入与助手回答
    assert captured["user"] == "修好 bug 再跑测试"
    assert "答案正文" in captured["answer"]


def test_runtime_single_suggestion_payload_is_backward_identical(tmp_path, monkeypatch) -> None:
    """**N=1 回归钉**:只有一条时,payload 与多候选之前的形状保持兼容。

    `suggestion` 仍是那条字符串(第三方按 README/集成指南只读它即可),`suggestions`
    是新增的单元素列表。这条钉住「附加而非破坏」的承诺。
    """
    runtime = _runtime(tmp_path, monkeypatch, enabled=True)
    monkeypatch.setattr(runtime.prompt_suggestion_generator, "generate", lambda *_a: ["跑测试"])

    async def collect():
        return [e async for e in runtime.stream(ChatRequest(message="hi"))]

    events = asyncio.run(asyncio.wait_for(collect(), timeout=30))
    suggestion = next(e for e in events if e["event"] == "prompt_suggestion")
    assert suggestion["data"]["suggestion"] == "跑测试"
    assert suggestion["data"]["suggestions"] == ["跑测试"]


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


def test_runtime_skips_emit_when_generator_returns_empty(tmp_path, monkeypatch) -> None:
    """生成器返回 [](无意义/失败)时不 emit,不留空建议。"""
    runtime = _runtime(tmp_path, monkeypatch, enabled=True)
    monkeypatch.setattr(runtime.prompt_suggestion_generator, "generate", lambda *_a: [])

    async def collect():
        return [e async for e in runtime.stream(ChatRequest(message="hi"))]

    events = asyncio.run(asyncio.wait_for(collect(), timeout=30))
    assert "prompt_suggestion" not in [e["event"] for e in events]


# ---------------------------------------------------------------- 两通道一致性


def test_both_emitters_produce_the_same_frame_shape(tmp_path, monkeypatch) -> None:
    """后端生成与原生 CLI 两个 emitter 必须出**同一形状** —— 否则就是 schema 双轨。

    `/api/chat/stream` 直接透传 runtime 帧,`/v1/responses` 从同一帧投影,所以帧形状漂了
    就会让两条公开通道各说各话。而 OpenAPI 硬门只查 media type、**看不见 SSE payload 形状**
    (audit_openapi_contract.py),CI 不会拦——这条测试就是补那个盲区。
    """
    from app.runtime.claude_prompt_suggestions import PromptSuggestionMessage

    # 原生 CLI 路径:适配器 yield PromptSuggestionMessage,runtime 转成帧
    cli_runtime = _runtime(tmp_path / "cli", monkeypatch, enabled=False)

    async def cli_query(*, prompt, options):
        async for _ in prompt:
            pass
        sid = options.resume or options.session_id
        await options.session_store.append(
            {"project_key": options.session_store.binding.project_key, "session_id": sid},
            [{"type": "user", "uuid": "cli-entry"}],
        )
        yield AssistantMessage(content=[TextBlock(text="答案正文")], model="m", session_id=sid)
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=0, is_error=False,
                            num_turns=1, session_id=sid, result="答案正文")
        yield PromptSuggestionMessage("跑测试", "u1", sid)

    from app.runtime import claude_prompt_suggestions

    monkeypatch.setattr(claude_prompt_suggestions, "query_with_prompt_suggestions", cli_query)

    async def collect(rt):
        return [e async for e in rt.stream(ChatRequest(message="hi"))]

    cli_events = asyncio.run(asyncio.wait_for(collect(cli_runtime), timeout=30))
    cli_frame = next(e for e in cli_events if e["event"] == "prompt_suggestion")

    # 后端生成路径
    be_runtime = _runtime(tmp_path / "be", monkeypatch, enabled=True)
    monkeypatch.setattr(be_runtime.prompt_suggestion_generator, "generate", lambda *_a: ["跑测试"])
    be_events = asyncio.run(asyncio.wait_for(collect(be_runtime), timeout=30))
    be_frame = next(e for e in be_events if e["event"] == "prompt_suggestion")

    assert set(cli_frame["data"]) == set(be_frame["data"]), (
        f"两个 emitter 的帧字段漂了 —— schema 双轨:\n"
        f"  CLI:  {sorted(cli_frame['data'])}\n  后端: {sorted(be_frame['data'])}"
    )
    for frame in (cli_frame, be_frame):
        assert frame["data"]["suggestions"] == ["跑测试"]
        assert frame["data"]["suggestion"] == "跑测试"
