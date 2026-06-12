"""真·端到端 live 验收（env-gated，默认 skip）。

与 tests/ 下其他离线测试的根本区别：本文件不 mock、不 stub、不 monkeypatch 模型层，
而是用真实模型凭据驱动真实运行时，验证「离线 fake 永远证明不了」的那一环——
真实模型输出能否被结构化契约消费、闭环归因那一步是否真的成立。

离线产品不变量不受影响：缺少 MODEL_PROVIDER_API_KEY 时整文件自动 skip，
因此 `make test` 在 CI/离线环境保持全绿；只有显式提供 live 凭据时才真打网络。

凭据来源：私有、gitignored 的 `docker/.env`（容器部署 env 文件），由本测试在导入时
按白名单读取三项 provider 变量注入进程环境，**绝不写入仓库、绝不出现在命令行**。
缺失该文件或文件未配置 key 时整文件 skip，离线 `make test` 不打网络、产品不变量不受影响。

运行方式（凭据已在 `docker/.env` 中，命令行无需任何 secret）::

    .venv/bin/python -m pytest -q tests/test_live_runtime_acceptance.py

chat 用例额外要求已 bootstrap 的 local-debug 运行卷（`make local-debug-bootstrap`）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.runtime.feedback_schemas import AttributionFormatterOutput
from app.runtime.output_formatter import DSPyOutputFormatter
from app.runtime.schemas import ChatRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import get_settings

# live 验收凭据只允许来自私有 env 文件，且只取与模型 provider 相关的白名单键，
# 避免把容器路径 / mode 等无关变量带进 host 测试进程。
_LIVE_ENV_FILE = Path(__file__).resolve().parents[1] / "docker" / ".env"
_LIVE_PROVIDER_KEYS = ("MODEL_PROVIDER_API_KEY", "MODEL_PROVIDER_API_URL", "AGENT_MODEL")


def _load_live_provider_env() -> None:
    if not _LIVE_ENV_FILE.exists():
        return
    for raw in _LIVE_ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key not in _LIVE_PROVIDER_KEYS:
            continue
        # 已显式导出的非空环境变量优先；缺失或空串（导入链路可能预置空 provider 变量）才由 env 文件补齐。
        if os.environ.get(key):
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


_load_live_provider_env()

pytestmark = pytest.mark.skipif(
    not os.environ.get("MODEL_PROVIDER_API_KEY"),
    reason="live 验收需 docker/.env 配置 MODEL_PROVIDER_API_KEY；缺失默认 skip，不破坏 make test 产品不变量",
)


def test_live_dspy_formatter_produces_typed_attribution_against_live_model():
    """闭环最关键一环：真实模型输出经 DSPy formatter 转为合法 typed 归因结果。

    这是离线闭环测试用 fake `_run_profile_json` 替换掉、从未真实验证的契约。
    """
    settings = get_settings()
    assert settings.provider_api_key, "live 验收要求 provider_api_key 已配置"
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


def test_live_runtime_chat_executes_against_live_model():
    """完整运行时路径（profile -> claude_agent_sdk -> live model -> ChatResponse）真实可用。"""
    import anyio

    settings = get_settings()
    if not settings.main_workspace_dir.exists():
        pytest.skip("chat live 验收需先 make local-debug-bootstrap 准备运行卷")

    runtime = __import__("app.runtime.claude_runtime", fromlist=["ClaudeRuntime"]).ClaudeRuntime(
        settings, LocalSessionStore(settings.session_dir)
    )

    async def _run():
        return await runtime.run(ChatRequest(message="只回答一个数字：2+3 等于几？", max_turns=2))

    response = anyio.run(_run)
    assert response.errors == [], f"live chat 不应有错误: {response.errors}"
    assert response.answer and response.answer.strip(), "live chat 应返回非空 answer"
    assert "5" in response.answer
