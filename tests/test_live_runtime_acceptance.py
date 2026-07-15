"""真·端到端 live 验收（env-gated，默认 skip）。

与 tests/ 下其他离线测试的根本区别：本文件不 mock、不 stub、不 monkeypatch 模型层，
而是用真实模型凭据驱动真实运行时，验证「离线 fake 永远证明不了」的那一环——
真实模型输出能否被结构化契约消费、闭环归因那一步是否真的成立。

离线产品不变量不受影响：缺少 `docker/.env` 或其中未配可用模型后端时整文件 skip，
因此 `make test` 在 CI/无模型环境保持全绿；只有部署到真实容器环境并提供 live 后端时才真打网络。

凭据来源：私有、gitignored 的 `docker/.env`（容器部署 env 文件），按白名单只取 provider 变量。
关键约束：导入时**只读入一个本地 dict，绝不改写全局 `os.environ`**（否则 collection 阶段会污染
同进程其他测试）；凭据仅在每个 live 用例内经 `monkeypatch` 临时注入、用完即还原。

运行方式：必须在 Docker Compose API 容器等真实容器测试环境中执行，使用 `docker/.env`
经 Compose 注入的运行时环境；`docker/.env.local-debug` 只用于本机调试专项测试，不用于本文件。
宿主机直接执行会 skip，不伪装成 local-debug。

chat 用例额外要求已由 API 启动协调器或 `make runtime-bootstrap` 准备并验证的容器运行卷。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

import pytest
from app.runtime.feedback_schemas import AttributionFormatterOutput
from app.runtime.model_provider import LOCAL_PROVIDER_DUMMY_API_KEY, ModelProviderRouter
from app.runtime.output_formatter import DSPyOutputFormatter
from app.runtime.schemas import ChatRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import get_settings

_LIVE_ENV_FILE = Path(__file__).resolve().parents[1] / "docker" / ".env"
_LIVE_PROVIDER_KEYS = (
    "MODEL_PROVIDER_API_KEY",
    "MODEL_PROVIDER_API_URL",
    "MODEL_PROVIDER_BACKEND",
    "MODEL_PROVIDER_VLLM_SIDECAR_THRESHOLD",
    "MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS",
    "MODEL_PROVIDER_WARNING_TTL_SECONDS",
    "AGENT_MODEL",
)
# 业务 Agent（含预制 main-agent）统一住 /data 下；断言真实运行布局，不再用已删的 /main-workspace、
# /claude-roots/main（B 整改后这两个目录已不创建，旧断言会因空死目录假通过）。
_CONTAINER_REQUIRED_PATHS = (
    Path("/data"),
    Path("/data/business-agents/main-agent/workspace"),
    Path("/data/business-agents/main-agent/claude-root"),
)
_TRUTHY = {"1", "true", "yes", "on", "container"}
_STRICT_LIVE_RUNTIME = os.environ.get("REQUIRE_LIVE_RUNTIME", "").strip().lower() in _TRUTHY


def _read_live_creds() -> dict[str, str]:
    """从容器部署 env 来源读取白名单 provider 变量到本地 dict，不触碰全局环境。"""
    creds: dict[str, str] = {}
    if _LIVE_ENV_FILE.exists():
        for raw in _LIVE_ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key in _LIVE_PROVIDER_KEYS:
                creds[key] = value.strip().strip('"').strip("'")
    if os.environ.get("RUNTIME_CONTAINER", "").strip().lower() in _TRUTHY:
        for key in _LIVE_PROVIDER_KEYS:
            if os.environ.get(key):
                creds[key] = os.environ[key]
    return creds


_LIVE_CREDS = _read_live_creds()


def _container_acceptance_skip_reason() -> str | None:
    if os.environ.get("RUNTIME_CONTAINER", "").strip().lower() not in _TRUTHY:
        return "live 验收必须在 Docker Compose API 容器等真实容器环境中运行（RUNTIME_CONTAINER=1）"
    missing = [path.as_posix() for path in _CONTAINER_REQUIRED_PATHS if not path.exists()]
    if missing:
        return f"live 验收缺少容器运行态挂载: {', '.join(missing)}"
    if not os.access("/data", os.W_OK):
        return "live 验收需要可写容器 DATA_DIR=/data"
    return None


_CONTAINER_ACCEPTANCE_SKIP_REASON = _container_acceptance_skip_reason()


def _live_provider_skip_reason() -> str | None:
    backend = (_LIVE_CREDS.get("MODEL_PROVIDER_BACKEND") or "anthropic_compatible").strip()
    if backend == "vllm" and not _LIVE_CREDS.get("MODEL_PROVIDER_API_URL"):
        return "vLLM live 验收需 docker/.env 配置 MODEL_PROVIDER_API_URL"
    if backend == "anthropic_compatible" and not _LIVE_CREDS.get("MODEL_PROVIDER_API_KEY"):
        return "Anthropic-compatible live 验收需 docker/.env 配置 MODEL_PROVIDER_API_KEY"
    if not _LIVE_CREDS.get("AGENT_MODEL"):
        return "live 验收需 docker/.env 配置 AGENT_MODEL"
    return None


_LIVE_PROVIDER_SKIP_REASON = _live_provider_skip_reason()

if _STRICT_LIVE_RUNTIME:
    strict_failures = [
        reason
        for reason in (_CONTAINER_ACCEPTANCE_SKIP_REASON, _LIVE_PROVIDER_SKIP_REASON)
        if reason is not None
    ]
    if strict_failures:
        raise RuntimeError("严格 live 验收前置条件不满足: " + "; ".join(strict_failures))

pytestmark = [
    pytest.mark.skipif(
        not _STRICT_LIVE_RUNTIME and _LIVE_PROVIDER_SKIP_REASON is not None,
        reason=_LIVE_PROVIDER_SKIP_REASON or "",
    ),
    pytest.mark.skipif(
        not _STRICT_LIVE_RUNTIME and _CONTAINER_ACCEPTANCE_SKIP_REASON is not None,
        reason=_CONTAINER_ACCEPTANCE_SKIP_REASON or "",
    ),
]


@pytest.fixture
def live_settings(monkeypatch):
    """在单个 live 用例作用域内临时注入 provider 凭据并刷新 settings 缓存。

    用 monkeypatch.setenv 注入（用例结束自动还原），并在前后 `cache_clear()`，
    既保证本用例读到带凭据的 settings，又确保凭据不泄漏到后续测试。
    """
    for key, value in _LIVE_CREDS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RUNTIME_CONTAINER", "1")
    get_settings.cache_clear()
    try:
        yield get_settings()
    finally:
        get_settings.cache_clear()


def _post_json(endpoint: str, payload: dict, *, api_key: str, timeout: float = 60):
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(1024 * 1024)
        if "text/event-stream" in response.headers.get("content-type", ""):
            return {"_sse": raw.decode("utf-8", errors="replace")}
        return json.loads(raw.decode("utf-8")) if raw else {}


def _live_vllm_router(settings):
    if settings.model_provider_backend != "vllm":
        pytest.skip("vLLM 专项 live probe 只在 MODEL_PROVIDER_BACKEND=vllm 时运行")
    return ModelProviderRouter(settings)


def test_live_vllm_s1_provider_route_ready(live_settings):
    """S1：版本/sidecar/models 管道可达，失败时给出独立 probe 归因。"""
    router = _live_vllm_router(live_settings)

    router.ensure_agent_runtime_ready()


def test_live_vllm_s2_litellm_accepts_anthropic_messages_and_streaming(live_settings):
    """S2：LiteLLM sidecar 接受 Claude Code 所需 Anthropic Messages 形态和 streaming。"""
    settings = live_settings
    router = _live_vllm_router(settings)
    route = router.route()
    api_key = settings.provider_api_key or LOCAL_PROVIDER_DUMMY_API_KEY
    base_url = route.claude_base_url.rstrip("/")
    model = settings.agent_model or "agent-gov-model"

    tool_message = _post_json(
        f"{base_url}/v1/messages",
        {
            "model": model,
            "max_tokens": 128,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "Call the agent_gov_probe tool with value ok."}]}],
            "tools": [
                {
                    "name": "agent_gov_probe",
                    "description": "Return a probe value.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                }
            ],
            "tool_choice": {"type": "tool", "name": "agent_gov_probe"},
        },
        api_key=api_key,
    )
    content_blocks = tool_message.get("content") or []
    assert any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content_blocks), (
        "Anthropic Messages tool probe 未返回 tool_use，不能准入 Claude Code tool 循环"
    )

    non_stream = _post_json(
        f"{base_url}/v1/messages",
        {
            "model": model,
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "Reply with OK."}],
        },
        api_key=api_key,
    )
    assert non_stream, "Anthropic Messages 非流式响应不能为空"

    stream = _post_json(
        f"{base_url}/v1/messages",
        {
            "model": model,
            "max_tokens": 32,
            "stream": True,
            "messages": [{"role": "user", "content": "Reply with OK."}],
        },
        api_key=api_key,
    )
    assert "event:" in stream.get("_sse", ""), "Anthropic Messages streaming 应返回 SSE event"


def test_live_vllm_c_model_tool_and_schema_preflight(live_settings):
    """C：目标模型必须能完成强制 tool calling 与 schema-exact JSON，避免端到端阶段才混合暴露。"""
    settings = live_settings
    router = _live_vllm_router(settings)
    route = router.route()
    api_key = settings.provider_api_key or LOCAL_PROVIDER_DUMMY_API_KEY
    base_url = route.formatter_api_base.rstrip("/")
    model = settings.agent_model or "agent-gov-model"

    tool_response = _post_json(
        f"{base_url}/v1/chat/completions",
        {
            "model": model,
            "messages": [{"role": "user", "content": "Call the probe tool with value ok."}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "agent_gov_probe",
                        "description": "Return a probe value.",
                        "parameters": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "agent_gov_probe"}},
            "max_tokens": 128,
        },
        api_key=api_key,
    )
    message = (((tool_response.get("choices") or [{}])[0]).get("message") or {})
    assert message.get("tool_calls"), "模型未返回 tool_calls，不能准入 Claude Code 多工具循环"
    first_tool_call = message["tool_calls"][0]
    tool_call_id = first_tool_call.get("id") or "call_1"
    function = first_tool_call.get("function") or {}
    tool_name = function.get("name") or "agent_gov_probe"

    loop_response = _post_json(
        f"{base_url}/v1/chat/completions",
        {
            "model": model,
            "messages": [
                {"role": "user", "content": "Call the agent_gov_probe tool with value ok, then answer DONE after the tool result."},
                {"role": "assistant", "content": None, "tool_calls": [first_tool_call]},
                {"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": '{"value":"ok"}'},
            ],
            "max_tokens": 64,
        },
        api_key=api_key,
    )
    loop_content = ((((loop_response.get("choices") or [{}])[0]).get("message") or {}).get("content") or "").strip()
    assert loop_content, "模型拿到 tool_result 后未继续输出，不能准入 Claude Code 多工具循环"

    schema_response = _post_json(
        f"{base_url}/v1/chat/completions",
        {
            "model": model,
            "messages": [{"role": "user", "content": 'Return exactly this JSON object and nothing else: {"ok": true}'}],
            "response_format": {"type": "json_object"},
            "max_tokens": 64,
        },
        api_key=api_key,
    )
    content = ((((schema_response.get("choices") or [{}])[0]).get("message") or {}).get("content") or "").strip()
    assert json.loads(content) == {"ok": True}


def test_live_dspy_formatter_produces_typed_attribution_against_live_model(live_settings):
    """闭环最关键一环：真实模型输出经 DSPy formatter 转为合法 typed 归因结果。

    这是离线闭环测试用 fake `_run_profile_json` 替换掉、从未真实验证的契约。
    """
    settings = live_settings
    if settings.model_provider_backend == "anthropic_compatible":
        assert settings.provider_api_key, "Anthropic-compatible live 验收要求 provider_api_key 已配置"
    if settings.model_provider_backend == "vllm":
        assert settings.provider_api_url, "vLLM live 验收要求 provider_api_url 已配置"
    assert settings.runtime_volume_mode == "container"
    assert settings.settings_env_file == Path("docker/.env")
    assert settings.data_dir == Path("/data")
    assert settings.enable_dspy_output_formatter, "DSPy formatter 必须启用才能验证结构化契约"

    formatter = DSPyOutputFormatter(settings)
    raw_text = (
        "归因分析：用户反馈日报缺少高危事件汇总。根因是 prompt 未要求按严重度排序，"
        "证据为最近3次运行均遗漏 critical 级别。建议在 prompt 中增加严重度分组与置顶要求。"
    )
    result = formatter.format(
        job_type="attribution",
        raw_text=raw_text,
        job_input={"feedback_case_id": "fc-live-acceptance", "attribution_job_id": "aj-live-acceptance"},
    )

    output = result.output
    # 能拿到已通过 pydantic 校验的 typed 实例，即证明真实模型输出满足结构化契约。
    assert isinstance(output, AttributionFormatterOutput)
    # backend-owned 上下文字段不应被模型回填到业务输出里（字段所有权边界）。
    dumped = output.model_dump()
    assert "feedback_case_id" not in dumped
    assert "attribution_job_id" not in dumped
    # 关键业务语义字段非空，证明这是真实归因而非空壳。
    assert output.problem_type
    assert output.recommended_next_step
    assert output.rationale and output.rationale.strip()


def test_live_runtime_chat_executes_against_live_model(live_settings):
    """完整运行时路径（profile -> claude_agent_sdk -> live model -> ChatResponse）真实可用。"""
    import anyio

    settings = live_settings
    if not (settings.main_workspace_dir / "CLAUDE.md").exists():
        pytest.skip("chat live 验收需先部署并 bootstrap 容器运行卷")

    runtime = __import__("app.runtime.claude_runtime", fromlist=["ClaudeRuntime"]).ClaudeRuntime(
        settings, LocalSessionStore(settings.session_dir)
    )

    async def _run():
        return await runtime.run(ChatRequest(message="只回答一个数字：2+3 等于几？", max_turns=2))

    response = anyio.run(_run)
    assert response.errors == [], f"live chat 不应有错误: {response.errors}"
    assert response.answer and response.answer.strip(), "live chat 应返回非空 answer"
    assert "5" in response.answer
