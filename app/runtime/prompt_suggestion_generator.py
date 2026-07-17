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
# 数量上限:5 × 60 字已经把输入框上方那一行挤满,再多没有意义。
_MAX_COUNT = 5

# 直接调 completion(而非 dspy):建议只是几句短文本,dspy 的结构化输出格式在小 max_tokens
# 下会被截断、解析不出。直接 completion 更简单可靠,且仍是「后端对 agent 输出做 LLM 派生」。
#
# 同理:**行式输出而非 JSON**。推理模型一旦在数组中途被截断,JSON 解析出 0 条;行式输出
# 截断了前面几行仍然完整可用 —— 优雅降级。
_SYSTEM_PROMPT_TEMPLATE = (
    "你在预测用户接下来最可能**自己输入**给 AI 助手的下一句话。"
    "从用户视角写(不是助手视角、不是对回答的评价、不要问句形式的反问)。"
    "输出至多 {count} 条,每条一行,互不重复且方向不同"
    "(例如:深入细节 / 换个角度 / 下一步动作)。"
    "每条 2-12 个字/词、简短可直接发送。"
    "只有当这一轮明显是终点、确实没有自然的下一步时才回复空。"
    "只回复这些句子本身,每行一条,不要编号、不要引号、不要解释、不要前缀。"
)

# 剥掉模型偶尔加的项目符号 / 序号前缀
_BULLET_RE = re.compile(r"^\s*[-*•·]\s*")
_NUMBERING_RE = re.compile(r"^\s*\d+\s*[.)、:：]\s*")


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

    def generate(self, user_message: str, agent_answer: str) -> list[str]:
        """返回至多 N 条建议(按序,首条最贴切);无意义 / 生成失败一律返回 []（绝不抛）。

        **绝不补齐**:模型只给 2 条就返回 2 条 —— 凑数的建议比没有更糟。
        """
        user_message = (user_message or "").strip()
        agent_answer = (agent_answer or "").strip()
        if not user_message and not agent_answer:
            return []
        count = self._count()
        try:
            model = self.settings.backend_prompt_suggestion_model or self.settings.agent_model
            if not model:
                return []
            kwargs: dict[str, Any] = dict(self.provider_router.formatter_kwargs())
            content = (
                f"用户本轮输入:\n{user_message[:_MAX_ANSWER_CHARS]}\n\n"
                f"助手本轮回答:\n{agent_answer[:_MAX_ANSWER_CHARS]}\n\n"
                f"用户下一句最可能输入的话(至多 {count} 条,每行一条):"
            )
            response = litellm.completion(
                model=self.provider_router.formatter_model_name(model),
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT_TEMPLATE.format(count=count)},
                    {"role": "user", "content": content},
                ],
                # 推理模型先吐思考再吐正文,max_tokens 要留够思考预算(见 settings 注释);
                # 条数越多正文越长,下限随 N 抬。
                max_tokens=max(
                    self.settings.backend_prompt_suggestion_max_tokens, 512 + 64 * count
                ),
                temperature=0.4,
                **kwargs,
            )
            return _clean_many(response["choices"][0]["message"]["content"] or "", count)
        except Exception:
            # 受控特例的硬边界:建议是可选增强,任何失败都不得影响主 Run。
            return []

    def _count(self) -> int:
        """数量在使用点 clamp,不在 settings 用 ge/le。

        本模块信条是「任何失败都不得影响主 Run」——一个装饰性增强的配置写错值,
        不该把进程启动崩掉。
        """
        return max(1, min(_MAX_COUNT, self.settings.backend_prompt_suggestion_count))


def _clean_many(raw: str, count: int) -> list[str]:
    """把模型的多行输出解析成至多 count 条建议(保序、去重)。

    行式解析:逐行剥项目符/序号 → 复用 `_clean` 做引号/前缀/限长 → 归一化去重 → 截到 count。
    **绝不补齐**:模型给几条就是几条,凑数的建议比没有更糟。
    """
    out: list[str] = []
    seen: set[str] = set()
    for line in (raw or "").splitlines():
        stripped = _NUMBERING_RE.sub("", _BULLET_RE.sub("", line))
        cleaned = _clean(stripped)
        if not cleaned:
            continue
        key = _dedupe_key(cleaned)
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= count:
            break
    return out


def _dedupe_key(text: str) -> str:
    """去重键:忽略大小写与标点/空白差异,避免「跑测试」「跑测试。」并存。"""
    return re.sub(r"[\s,，.。!!??;;:：、]+", "", text).casefold()


def _clean(raw: str) -> str | None:
    """规范化**单行**模型输出:取首行、去引号、限长;空则 None。

    仍被 `_clean_many` 逐行复用 —— 它承担引号/前缀/60 字截断这套已被参数化测试钉住的规则。
    """
    text = raw.strip()
    if not text:
        return None
    text = text.splitlines()[0].strip()
    # 顺序要紧:**先去前缀再去引号**。反过来的话,`建议:"跑测试"` 的左引号会被前缀挡住、
    # strip 不到,留下 `"跑测试`。
    text = re.sub(
        r"^(建议|下一句|next|suggestion)\s*[:：]\s*", "", text, flags=re.IGNORECASE
    ).strip()
    text = text.strip("\"'“”‘’「」").strip()
    if not text:
        return None
    return text[:_MAX_SUGGESTION_CHARS]
