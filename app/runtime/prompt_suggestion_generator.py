"""后端直接生成 Prompt Suggestion —— **受控特例**。

正常原则是「以 claude-agent-sdk / Claude Code 为中心,后端只透传、不自造 agent 能力」。
本模块是对该原则的**受控例外**,理由与边界如下:

- Claude Code CLI 的原生 `--prompt-suggestions`(SUGGESTION MODE)在本部署里事实上失效:
  它被指令刻意压制("Or nothing / 安全话题一律沉默"),且本部署全是 SOC/安全 Agent、
  provider 为 deepseek-v4-flash —— 实测理想场景也几乎永远返回空。"用 SDK 的"在这里等于
  "什么都没有"。
- 因此后端对**本轮对话**做一次 LLM 派生,产出建议。这与 `DSPyOutputFormatter`(后端对
  agent 输出做 dspy 派生)是**同一类**,复用同一套 provider_router / dspy,不新起 LLM 栈。
- 边界(必须守住,否则就不再是"受控"):**不碰 agent loop**(工具、MCP、hooks、skills、
  subagents 一概不动)、**不落库**、**不当 agent 事实**、只作临时 UX 帧;失败一律吞掉返回
  None,绝不影响主 Run。
- 开关:`ENABLE_BACKEND_PROMPT_SUGGESTION`,**默认关**(守常规原则),仅本部署经
  docker/.env 显式开启;关掉即回退到 CLI 原生路径。

不得据此把"后端随意自造 agent 能力"正常化。
"""

from __future__ import annotations

import re
from typing import Any

from .litellm_defaults import configure_litellm_import_defaults

configure_litellm_import_defaults()

import litellm  # noqa: E402

from .model_provider import ModelProviderRouter  # noqa: E402
from .settings import AppSettings  # noqa: E402

_MAX_ANSWER_CHARS = 2000
_MAX_SUGGESTION_CHARS = 60

# 直接调 completion(而非 dspy):建议只是一句短文本,dspy 的结构化输出格式在小 max_tokens
# 下会被截断、解析不出。直接 completion 更简单可靠,且仍是「后端对 agent 输出做 LLM 派生」。
_SYSTEM_PROMPT = (
    "你在预测用户接下来最可能**自己输入**给 AI 助手的下一句话。"
    "从用户视角写(不是助手视角、不是对回答的评价、不要问句形式的反问)。"
    "输出 2-12 个字/词、简短可直接发送的一句话;只有当这一轮明显是终点、确实没有自然的"
    "下一步时才回复空。只回复那句话本身,不要引号、不要解释、不要前缀。"
)


class PromptSuggestionGenerator:
    """对本轮对话做一次 LLM 派生,产出「用户下一句」建议(best-effort)。"""

    def __init__(
        self,
        settings: AppSettings,
        *,
        provider_router: ModelProviderRouter | None = None,
        langfuse: Any | None = None,
    ) -> None:
        self.settings = settings
        self.provider_router = provider_router or ModelProviderRouter(settings)
        self.langfuse = langfuse

    def generate(self, user_message: str, agent_answer: str) -> str | None:
        """返回建议文本;无意义 / 生成失败一律返回 None(绝不抛)。"""
        user_message = (user_message or "").strip()
        agent_answer = (agent_answer or "").strip()
        if not user_message and not agent_answer:
            return None
        try:
            model = self.settings.backend_prompt_suggestion_model or self.settings.agent_model
            if not model:
                return None
            kwargs: dict[str, Any] = dict(self.provider_router.formatter_kwargs())
            content = (
                f"用户本轮输入:\n{user_message[:_MAX_ANSWER_CHARS]}\n\n"
                f"助手本轮回答:\n{agent_answer[:_MAX_ANSWER_CHARS]}\n\n"
                f"用户下一句最可能输入的话是:"
            )
            response = litellm.completion(
                model=self.provider_router.formatter_model_name(model),
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                # 推理模型先吐思考再吐正文,max_tokens 要留够思考预算(见 settings 注释)。
                max_tokens=max(self.settings.backend_prompt_suggestion_max_tokens, 512),
                temperature=0.4,
                **kwargs,
            )
            return _clean(response["choices"][0]["message"]["content"] or "")
        except Exception:
            # 受控特例的硬边界:建议是可选增强,任何失败都不得影响主 Run。
            return None


def _clean(raw: str) -> str | None:
    """规范化模型输出:取单行、去引号、限长;空则 None。"""
    text = raw.strip()
    if not text:
        return None
    text = text.splitlines()[0].strip()
    text = text.strip("\"'“”‘’「」")
    # 去掉常见前缀噪音(模型偶尔会加"建议:"之类)
    text = re.sub(r"^(建议|下一句|next|suggestion)\s*[:：]\s*", "", text, flags=re.IGNORECASE).strip()
    if not text:
        return None
    return text[:_MAX_SUGGESTION_CHARS]
