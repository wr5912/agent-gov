# Agent 运行时语义事件与 Speech Summary 整改方案

> 状态：Accepted / 实施依据；决策日期：2026-07-28；目标版本：3.0.3；公开契约真相源：OpenAPI
> 上位架构依据：[OpenAI 兼容接口能否替代原生 Chat 端点评估](./OpenAI兼容接口能否替代原生Chat端点评估.md)

## 1. 结论先行

本轮不删除 `/api/chat`、`/api/chat/stream`、`/v1/chat/completions`。三者继续作为兼容入口，
在 OpenAPI 和文档中统一标记 deprecated，但不承诺删除日期；新集成仍使用
`/v1/responses`，第一方 Playground live turn 仍使用 `/api/agent-runtime/sdk-events`。

本轮新增的 Speech Summary 是一次受控、可选、非持久化的 LLM 派生能力：

- `/api/agent-runtime/sdk-events` 通过 `with_speech_summary` 显式开启。
- `/api/chat/stream` 通过 `with_speech_summary` 显式开启，`raw` 与 `semantic` 两种模式都输出
  同一个 canonical `agentgov.speech_summary` 事件。
- `/v1/responses` 仅在 control mode、`stream=true` 且
  `agentgov.with_speech_summary=true` 时开启。
- `/api/chat`、`/v1/chat/completions`、strict Responses 和 raw Runtime 接口不支持 Speech
  Summary；显式传入相关字段必须返回 `422`，不得静默忽略。
- `/api/debug/agent-runtime/raw-events` 完全退出 Speech Summary 范围，继续只返回 byte-exact
  Runtime stdout。

Speech Summary 不替代原生 SDK 消息、标准 Responses reasoning、最终正文、Prompt Suggestion
或音频。它只把已完成的顶层思考块或完整顶层模型响应压缩成 10–50 字、适合 TTS 的用户可见播报。

## 2. 实际问题与整改目标

### 2.1 当前问题

当前实现存在四类跨接口问题：

1. `RuntimeQueryState.answer_parts` 和 Responses 流投影没有统一过滤
   `parent_tool_use_id is not None` 的子 Agent 文本，子 Agent 证据可能污染主回答或标准 reasoning。
2. 整改前的终态顺序是 `result -> done -> prompt_suggestion`；Responses 又会输出
   `response.completed -> agentgov.done`。可选派生事件出现在终态之后，客户端不能用一个稳定终态收口。
3. Responses 顶层请求默认忽略未知字段、`input` 使用弱 JSON；strict 非流式响应仍可能带
   `agentgov`。兼容 Chat Completions 也会静默忽略 `stream=true`。
4. raw 接口虽然 body 来自原始 tee，但后台仍依赖 Chat 兼容 facade 排空；HITL 所需运行缺少
   可用的轮询 token 投影，非流式 HITL 也没有统一 fail-fast 契约。

### 2.2 本轮目标

- 以 Claude Agent SDK 消息为唯一事实源，统一主 Agent 作用域、完成边界和终态时序。
- 在三个流式语义接口输出同一 Speech Summary 业务事件。
- 保留三个兼容接口的主要 wire 形状，同时修复静默忽略、错误伪成功、HITL 和子 Agent 污染。
- 收紧 `/v1/responses` strict/control、请求 allowlist、retrieve 模式和标准终态。
- 保持 raw body byte-exact，不解析、不重序列化、不混入任何 AgentGov body 事件。
- 不新增持久化 job、消息副本、SQLite schema、Docker volume 或音频链路。

### 2.3 本期不做

- 不生成、缓存、存储、重放或播放音频。
- 不把摘要写入 SDK session message、AgentRun、SQLite 或 `GET /v1/responses/{id}`。
- 不在 raw body、响应旁路或响应头中增加 Speech Summary。
- 不让客户端通过 Chat Completions 或 strict Responses 使用 AgentGov 控制面。
- 不承诺兼容接口的删除日期。

## 3. 边界与单一真相来源

| 边界 | 单一真相来源 | 本轮处理 |
| --- | --- | --- |
| SDK 消息、session、subagent、tool、Result | `claude-agent-sdk` / Claude Code | 原样保留；后端只做确定性作用域和完成态投影 |
| 主回答与 reasoning | 顶层 SDK `AssistantMessage` / `StreamEvent` | 标准输出只消费 `parent_tool_use_id is None` |
| Speech Summary 文本 | DSPy typed `SpeechSummaryOutput` | 只拥有 `text` |
| Speech provenance | Runtime 上下文和 SDK message/block | 后端生成，模型不得复述 |
| SSE envelope、`ts`、`seq`、JSON | 各 HTTP 边界 | 边界生成，不进入 DSPy |
| Prompt Suggestion | 现有 backend/native suggestion 路径 | 改为终态前有界排空，不持久化 |
| Responses retrieve 模式 | AgentRun 现有 metadata 保留键 | 不改 DB schema；历史缺标记按 control 富化 |
| raw body | Runtime stdout tee | byte-exact，不解析、不重序列化 |
| HITL 决策 token | API 进程 pending memory | 只对精确 waiting run 轮询临时暴露，不持久化 |

## 4. 公开 API 契约

### 4.1 接口矩阵

| 接口 | Speech Summary 参数 | 有效条件 | 输出与处置 |
| --- | --- | --- | --- |
| `POST /api/agent-runtime/sdk-events` | `with_speech_summary: bool=false` | SSE 接口；请求开关与 env 边界同时开启 | `claude.sdk.*`、既有 `agentgov.*`、`agentgov.speech_summary` |
| `POST /api/chat/stream` | `with_speech_summary: bool=false` | `event_mode=raw/semantic` 均可 | 保留旧 Chat SSE；摘要单独输出 canonical `agentgov.speech_summary` |
| `POST /v1/responses` control | `agentgov.with_speech_summary: bool=false` | 仅 `stream=true` | 标准 `response.*` 和 control `agentgov.*` |
| `POST /v1/responses` strict | 不支持 | 不适用 | 不接受/不输出 AgentGov Speech Summary |
| `POST /api/chat` | 不支持 | 不适用 | 显式字段由 `extra=forbid` 返回 `422` |
| `POST /v1/chat/completions` | 不支持 | 不适用 | 显式字段或未知扩展返回 `422` |
| `POST /api/debug/agent-runtime/raw-events` | 不支持 | 不适用 | 只输出原始字节；显式字段返回 `422` |

额外约束：

- control Responses 中 `with_speech_summary=true` 且 `stream=false` 返回 `422`，不得静默忽略。
- env 边界为空时，请求开启仍正常运行，但不调用 DSPy、不输出摘要。
- Speech Summary 字段不进入共享 `ChatRequest`。SDK SSE 和 Chat Stream 各使用专用请求模型；
  Responses 字段只进入 `AgentGovRequestExtension`。
- 三个兼容接口在 OpenAPI 中 `deprecated: true`，文档明确“兼容保留、无删除日期”。

### 4.2 兼容接口同步整改

#### `/api/chat`

- 保持非流式 `ChatRequest -> ChatResponse` 主形状。
- `agent_id` 必填且必须指向可运行的注册业务 Agent。
- `message` 必须为非空文本。
- `answer` 只由顶层 AssistantMessage 与必要的顶层 Result fallback 组成；`messages` 仍保留完整 SDK 事实。
- 目标 Agent 需要 Web HITL 时，在启动 Agent 前返回 `422`；非流式接口不进入等待态。
- 不支持 Speech Summary。

#### `/api/chat/stream`

- 保留 `session/message/prompt_suggestion/result/error/done/heartbeat` 等旧事件和主要 payload。
- `event_mode=raw` 仍返回完整已解析 SDK 兼容事实，可附加 `scope` /
  `parent_tool_use_id`，不得删旧字段。
- `event_mode=semantic` 的回答文本只来自主 Agent；子 Agent 内容只进入完整 Trace 事实。
- HITL 使用既有兼容事件；目标 Agent 需要 HITL 但全局服务关闭时，在发送 SSE header 前返回 `503`。
- `done` 是最后一个业务 data frame；旧 `heartbeat` 仍是兼容 data frame，不改成 comment。
- Speech Summary 不包装进旧 `message` 事件，直接使用 `event: agentgov.speech_summary`。

#### `/v1/chat/completions`

- 保持非流式 OpenAI Chat Completions 响应形状和运营者配置的出口 Agent。
- 请求模型 `extra=forbid`，`messages` 非空，role 为支持的标准枚举，正文为非空文本。
- `stream=true` 返回 `422`，不再静默按非流式执行。
- 完整 messages 顺序映射为一次 prompt；不把业务 Agent ID 塞入 `model`。
- 响应 `model` 使用实际生效的请求/profile/settings 模型。
- Agent 运行失败返回 `502` OpenAI 风格 error，不再把带 `errors` 的结果包装成成功 choice。
- 配置的出口 Agent 缺失或不可运行仍返回 `503`。
- 目标 Agent 需要 HITL 时返回 `422`；不支持 Speech Summary。

## 5. Speech Summary 事件契约

### 5.1 SSE 事件

统一事件名：

```text
event: agentgov.speech_summary
```

ThinkingBlock 完成示例：

```json
{
  "v": 1,
  "type": "agentgov.speech_summary",
  "run_id": "run_xxx",
  "ts": 0,
  "seq": 23,
  "payload": {
    "summary_id": "speech:msg_123:thinking:0",
    "source_kind": "thinking",
    "message_id": "msg_123",
    "block_index": 0,
    "scope": "main",
    "text": "正在核对告警证据和攻击链路",
    "char_count": 13
  }
}
```

完整 AssistantMessage 示例：

```json
{
  "v": 1,
  "type": "agentgov.speech_summary",
  "run_id": "run_xxx",
  "ts": 0,
  "seq": 31,
  "payload": {
    "summary_id": "speech:msg_123:assistant_response",
    "source_kind": "assistant_response",
    "message_id": "msg_123",
    "scope": "main",
    "text": "已完成告警研判并给出处置优先级",
    "char_count": 16
  }
}
```

契约约束：

- `v=1` 复用公开 AgentGov envelope，不新增 `schema_version`。
- `source_kind=thinking` 时 `block_index` 必填且为原生
  `content_block_stop.index`；`source_kind=assistant_response` 时字段不存在。
- `scope` 本期固定为 `main`。
- `char_count=len(text)`，文本先做 Unicode strip；不把示例中的人工计数当权威。
- `ts` 为边界生成的 Unix 秒；`seq` 为当前连接 data frame 序号。SDK SSE 与 Responses 的
  heartbeat comment 不计数；Chat Stream 的旧 `heartbeat` data frame 继续计数。
- SDK SSE、Chat Stream、Responses control 分别维护本连接序号，不承诺跨连接 replay。

### 5.2 完成边界

`SPEECH_SUMMARY_BOUNDARIES` 支持：

- `thinking_block_completed`：只处理顶层 `StreamEvent` 中 thinking block 的原生
  `content_block_stop`；按 `(scope, message_id, block_index)` 聚合此前 `thinking_delta`。
- `assistant_response_completed`：完整顶层 `AssistantMessage` 到达后，合并该消息全部非空
  `TextBlock`，每条消息生成一次摘要。

`ResultMessage` 只表示 Agent Loop 结束，不能作为一次模型响应或摘要来源。

### 5.3 作用域与跳过规则

- 只处理 `parent_tool_use_id is None` 的主 Agent。
- 子 Agent、工具输入输出、工具专用消息不生成摘要。
- 空 ThinkingBlock、空正文、纯工具调用 AssistantMessage 直接跳过。
- 同一 `(message_id, source_kind, block_index)` 只生成一次。
- 对应最终顶层正文完成时，取消或丢弃尚未完成的过时 thinking 摘要。

## 6. DSPy typed output 与安全边界

### 6.1 Typed output

```python
class SpeechSummaryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=10, max_length=50)
```

字段所有权：

| 字段 | 所有者 |
| --- | --- |
| `text` | DSPy / Agent-owned |
| `run_id`、`message_id`、`block_index`、`scope` | Backend-owned |
| `summary_id`、`char_count` | Backend-owned |
| `v`、`type`、`ts`、`seq`、SSE/JSON | Boundary-owned |

DSPy Signature 只要求生成 `text`。Backend-owned 字段可以作为观测 metadata，但不得作为
模型输出字段或让模型复述后再采信。

### 6.2 调用策略

- 使用 DSPy 原生 async `Predict.acall`，不以 `to_thread` 模拟异步。
- LM 使用 `cache=False`、`num_retries=0`、显式 timeout、受限 max tokens。
- `ChatAdapter(use_json_adapter_fallback=False)`，禁用隐式 adapter fallback。
- typed 解析/长度失败最多进行一次明确修复重试；provider timeout、provider error、安全过滤失败不重试。
- 不在句子中间机械截断输出；过短或过长只能通过一次模型修复后接受，否则丢弃。
- 每轮串行摘要、进程级有界并发；输入只取完成边界所需文本并设确定性上限。
- 调用成本进入独立 Langfuse Speech Summary generation/span；不写入 Claude SDK usage 或
  `total_cost_usd`。

### 6.3 安全策略

系统提示要求生成“面向用户的进度播报”，不得复述：

- chain-of-thought 或逐步内部推理；
- system/developer prompt；
- API key、token、password、secret；
- URL、文件路径；
- 原始工具参数、JSON 或代码。

模型输出后再执行确定性过滤。命中 URL、路径、密钥模式、代码围栏、JSON 结构、控制字符、
明显系统提示/思维链复述时静默丢弃。过滤失败不发送 `agentgov.error`，主 Agent Loop 继续成功。

## 7. 异步、排空与终态

### 7.1 时间预算

| 派生能力 | 单任务总超时 | Agent 终态排空 |
| --- | --- | --- |
| Speech Summary | 15 秒 | 5 秒 |
| Prompt Suggestion | 15 秒 | 3 秒 |

Speech Summary 在完成边界出现后立即异步启动，不阻塞后续原生 token。Prompt Suggestion 在最终
Result 可用后启动。终态阶段并发排空两个能力，各自使用独立预算；超时任务取消并静默丢弃。

客户端断开时：

- 取消未完成的 DSPy task；
- 取消未完成的 Prompt Suggestion task；
- 取消本轮 HITL wait；
- 不留下向 SSE queue 写入的孤儿任务。

### 7.2 统一终态顺序

managed Runtime 的逻辑顺序：

```text
SDK native events
-> SDK ResultMessage / agentgov.result
-> SDK 原生消息源结束（含 Result 后 transcript mirror flush）
-> 原子提交 session / run / turn intent
-> 已完成或有界排空的 prompt_suggestion / speech_summary
-> agentgov.done
```

`agentgov.result` 是 Agent Loop 已产出 SDK `ResultMessage` 的进度事实，不等于公开完成信号。
SDK 可能在该消息之后才批量写入官方 `SessionStore`；此时 turn intent 必须继续保持
`running`。只有原生消息源排空后才能原子提交并关闭写栅栏，否则晚到的 transcript 会被
fencing 拒绝并产生 `MirrorErrorMessage`。持久化失败可在 `agentgov.result` 之后输出
`agentgov.error`，Responses projector 必须等 `done` 才决定唯一的标准成功或失败终态。

各接口投影：

- SDK SSE：`agentgov.done` 是最后一个业务 data frame。
- Chat Stream：兼容 `done` 是最后一个业务 data frame。
- Responses strict：`response.completed`、`response.failed` 或取消态
  `response.incomplete` 恰好一次并且最后。
- Responses control：先输出 `agentgov.done`，再输出恰好一次
  `response.completed`、`response.failed` 或 `response.incomplete`；取消时在
  `agentgov.done` 前输出 `agentgov.cancelled`，标准终态始终是连接最后一个业务事件。

任何接口都不得在上述终态后再输出 Prompt Suggestion、Speech Summary、error、result 或 SDK 消息。

## 8. `/v1/responses` 专项整改

### 8.1 请求 allowlist 与 typed input

顶层只接受当前明确支持的字段：

`model`、`input`、`instructions`、`stream`、`store`、`conversation`、
`previous_response_id`、`metadata`、`agentgov`。

其余字段，包括请求侧 `reasoning`、`temperature`、`tools` 等，返回 `422`；不再因
“可能来自真实 OpenAI 客户端”而静默忽略。

`input` 只接受：

- 非空字符串；或
- 非空 typed message items，支持已声明的 role 和文本 content block。

空列表、空文本、只有 system/developer 而没有本轮用户输入、未知 item/block 或无法提取用户文本，
均返回 `422`。

### 8.2 strict/control 隔离

- strict 请求没有 `agentgov`；非流式、流式和 retrieve 都不输出 `agentgov`。
- control 请求包含 `agentgov` 且 `agentgov.agent_id` 必填。
- strict 继续输出标准 reasoning 生命周期，但不接受 request-side `reasoning` 配置。
- strict 选中的出口 Agent 若要求 HITL，流式和非流式均在运行前返回 `422`。
- control 非流式选中的 Agent 若要求 HITL，运行前返回 `422`。
- control 流式选中的 Agent 若要求 HITL但全局服务关闭，运行前返回 `503`。

### 8.3 retrieve 模式一致性

请求运行时在现有 AgentRun metadata 写 backend-only 模式标记，不改 SQLite schema：

- strict 创建的 response retrieve 仍为 strict，不含 `agentgov`。
- control 创建的 response retrieve 保留 AgentGov 富化。
- 历史 run 没有模式标记时按 control/enriched 返回，维持既有历史读取兼容。
- 保留键不会通过公开 `metadata` 回显，客户端不能伪造或覆盖。

## 9. raw Runtime 与 HITL

### 9.1 byte-exact 不变量

`/api/debug/agent-runtime/raw-events` 继续：

- `Content-Type: application/octet-stream`；
- `X-AgentGov-Raw-Fidelity: byte-exact`；
- body 是未解析、未重新序列化、未 SSE framing 的 Runtime stdout；
- 保留现有 API key、启用开关和最大字节限制；
- 不增加 Speech Summary 参数、事件、缓存、重放、旁路接口或摘要响应头。

raw 后台改为直接排空 typed `runtime.stream_events()`，不再依赖 Chat wire facade；该内部迁移不能
改变捕获 body 的任何字节。

流式 raw 在 HTTP header 已发送后发生 Runtime 错误或超限时，HTTP status 无法改写，连接只能以
已发送的 exact-prefix 结束。这一传输限制必须在文档中明确：`byte-exact` 不等于
`complete`。需要权威完整捕获时使用 `stream=false`；流式客户端同时验证原生 EOF/Result。

### 9.2 raw HITL 轮询

- `stream=false` 且目标 Agent 需要 HITL：运行前返回 `422`。
- `stream=true` 且目标 Agent 需要 HITL：保持 raw body，不增加控制旁路；客户端从
  `X-AgentGov-Run-Id` 获取 run id，再轮询既有
  `GET /api/claude-user-input-requests?run_id=<id>&status=waiting`。
- 全局 HITL 服务关闭时在返回 raw header 前返回 `503`。
- `decision_token` 只保存在 API 进程的 pending memory；只对已鉴权、同时精确指定
  `run_id` 和 `status=waiting` 的列表请求临时返回。
- token 不进入 SQLite、日志、resolved/cancelled 列表、resolved event 或历史响应。
- 决策仍使用 canonical
  `POST /v1/agentgov/confirmation-requests/{request_id}/decision`。

## 10. Runtime / Env 配置

请求开关是 per-run opt-in；env 边界是部署级能力开关。只有两者同时允许才生成摘要。

两份官方示例必须原样、连续保留以下注释：

```dotenv
# Speech Summary 生成边界，多个值使用英文逗号分隔。
#
# 可用边界：
# - thinking_block_completed
#   顶层 ThinkingBlock 完成时触发。内部对应 Claude Agent SDK
#   StreamEvent 中 thinking 类型的原生 content_block_stop，用于播报处理进度。
#
# - assistant_response_completed
#   完整顶层 AssistantMessage 到达且包含非空 TextBlock 时触发；
#   合并该消息全部正文后生成一次结果摘要。
#
# 推荐组合：
# - 进度播报 + 结果摘要（推荐）：
#   thinking_block_completed,assistant_response_completed
# - 仅结果摘要，成本和思维内容风险更低：
#   assistant_response_completed
# - 仅进度播报：
#   thinking_block_completed
# - 留空：禁止生成任何 Speech Summary。
#
# 未知值或重复值应在应用启动时校验失败，不得静默忽略。
SPEECH_SUMMARY_BOUNDARIES=thinking_block_completed,assistant_response_completed
```

同时新增：

```dotenv
SPEECH_SUMMARY_TIMEOUT_SECONDS=15
SPEECH_SUMMARY_TERMINAL_DRAIN_SECONDS=5
PROMPT_SUGGESTION_TIMEOUT_SECONDS=15
PROMPT_SUGGESTION_TERMINAL_DRAIN_SECONDS=3
```

校验规则：

- 未知值、重复值、非整体空字符串中的空 segment 在应用启动时失败。
- 整体留空合法，表示所有 Speech Summary 边界关闭。
- 配置 `thinking_block_completed` 时，`INCLUDE_PARTIAL_MESSAGES=false` 必须启动失败。
- 容器 `docker/.env.example` 与本机 `docker/.env.local-debug.example` 的 Runtime key 同构；
  真实私有 env 只在对应运行模式中选择，不构成 layered override。
- 启动日志输出边界和 timeout，不输出模型凭据。

Docker volume 不受影响：容器仍使用 `${HOME}/volume-agent-gov`，本机调试仍默认
`/tmp/local-debug-volume-agent-gov`；不迁移 `docker/volume/`，不改 volume mount。

## 11. 内部实现边界

新增独立 Speech Summary 模块，职责包括：

```text
SDK message
-> 主 Agent / 完成边界识别
-> per-run SpeechSummaryCoordinator
-> SpeechSummaryService
-> typed SpeechSummaryOutput
-> backend-owned SpeechSummary control data
-> SDK / Chat / Responses SSE envelope
```

模块职责：

- `speech_summary.py`：typed output、DSPy async 调用、安全过滤、去重、thinking 聚合、过时取消和有界排空。
- stream lifecycle/coordinator：只编排 Prompt Suggestion 与 Speech Summary 的任务、取消和终态，不做 DSPy prompt。
- `claude_runtime_stream.py`：保留 SDK query 与 managed event 薄接线。
- `openai_responses_stream.py`：只投影统一事件，不加入 DSPy、聚合器或安全过滤。
- 三个 HTTP 边界：维护本连接 data-frame sequence，生成 canonical envelope。

架构阈值处理：

- 不向现有 706 行 `openai_responses_stream.py` 增加新职责；优先抽出 Responses terminal/lifecycle helper，
  使文件不跨越 800 行。
- 不向 656 行 `claude_runtime_stream.py` 内嵌 Speech service、DSPy 或过滤器。
- 不向 1050 行 `tests/test_responses_stream.py` 增加 Speech 场景，新建独立测试模块。
- 不复制三份超过 40 行的 envelope/序列逻辑；使用一个边界 helper 和一个 typed payload 真相源。

## 12. 替换、迁移与保留清单

### 删除

- `done` 后仍允许 Prompt Suggestion 的运行时与 Responses 特判。
- Responses 顶层未知字段静默忽略。
- Chat Completions `stream=true` 静默降级。
- 主回答/reasoning 混入 subagent 文本的路径。
- raw backend 对 `ClaudeRuntime.stream()` Chat 兼容 facade 的依赖。

### 迁移

- Runtime 终态迁移到“派生事件并发有界排空后 done”。
- Responses 终态迁移到“agentgov.done 后标准 terminal 最后”。
- Responses retrieve 迁移到 backend mode marker。
- SDK/Chat Stream 请求迁移到各自专用 Speech opt-in schema。
- raw HITL 迁移到既有 waiting request polling + canonical decision API。
- OpenAPI SSE 文档迁移到 typed Speech Summary component +
  `x-agentgov-sse-events`，同时保留 `claude.sdk.*` 开放事件族。

### 保留

- `/api/chat`、`/api/chat/stream`、`/v1/chat/completions` 公开路径和兼容定位。
- Chat Stream 旧事件名和主要 payload 字段。
- `/v1/responses` 标准 reasoning 输出。
- raw 鉴权、开关、大小限制、媒体类型、header 与 stdout 字节。
- AgentRun / session / feedback 的现有持久化事实。
- `${HOME}/volume-agent-gov`、SQLite schema、历史数据和 Docker mounts。

兼容入口只有在未来消费者清单、替代能力、迁移窗口和真实容器证据全部确认后，才另行评估删除。

## 13. 测试同步矩阵

| 行为 | 处置 | 权威测试重点 |
| --- | --- | --- |
| DSPy typed output、长度、一次修复 | GAP | 正常、过短、过长、parse failure、provider failure |
| hostile backend-owned 字段污染 | GAP | extra 字段不能进入最终事件 |
| URL/path/secret/CoT/JSON 安全过滤 | GAP | 静默丢弃，不发 `agentgov.error` |
| thinking/assistant 完成边界 | GAP | 原生 message id/index、去重、空/tool-only/subagent 跳过 |
| 默认关闭和 env 空边界 | GAP | 不调用 DSPy、不输出摘要 |
| stale thinking 与断连取消 | GAP | 取消任务、无迟到事件、无孤儿成本 |
| SDK SSE Speech Summary | REFACTOR | 专用请求 schema、canonical envelope、done 最后 |
| Chat Stream raw/semantic Speech | GAP | 两模式同一 canonical 事件、旧事件兼容 |
| Responses control Speech | GAP | control+stream only、标准 terminal 最后 |
| strict Responses 无 AgentGov | PROMOTE | 非流式/流式/retrieve 一致 |
| Responses allowlist/typed input | REFACTOR | 空、only-system、unknown field/block、hostile metadata |
| Responses subagent scope | GAP | 标准 text/reasoning 不污染，control trace 保留 |
| Responses terminal exactly once/last | PROMOTE | success、failure、source exception、derived timeout |
| Chat/Chat Completions HITL fail-fast | GAP | 运行前 `422` |
| Chat Completions `stream=true` | GAP | `422` 且 OpenAPI 不宣称 stream |
| Chat Completions runtime failure | GAP | `502` OpenAI error，不伪成功 |
| raw 不支持 Speech | KEEP/扩展 | 422、OpenAPI 无字段、body/header byte-exact |
| raw HITL polling token | GAP | 仅 exact waiting run 暴露，终态/宽查询不泄漏 |
| Prompt Suggestion 终态前排空 | REFACTOR | 超时、异常、顺序、断连取消 |
| 三兼容接口 deprecated | GAP | OpenAPI + docs，无 sunset 日期 |

测试应断言公开行为，不绑定 coordinator 私有任务集合或 DSPy 内部调用顺序。新增场景同步
`tests/quality_policy.json` 的 owner、capability、lane 和主流程绑定。

## 14. 分层验证与真实环境验收

### 14.1 本地红绿循环

```bash
.venv/bin/python -m pytest -q \
  tests/test_speech_summary_service.py \
  tests/test_speech_summary_stream.py \
  tests/test_responses_speech_summary.py \
  tests/test_legacy_chat_contract.py \
  tests/test_responses_scope_contract.py \
  tests/test_responses_terminal_contract.py \
  tests/test_claude_sdk_native_stream.py \
  tests/test_claude_user_input.py \
  tests/test_runtime_raw_events.py \
  tests/test_settings.py \
  tests/test_openapi_export.py \
  tests/test_documentation_contracts.py
```

### 14.2 中段架构与契约 checkpoint

```bash
.venv/bin/python scripts/check_codex_governance.py --mode fail
.venv/bin/python scripts/check_stage_language.py
.venv/bin/python -m ruff check <本轮修改的 Python 文件>
.venv/bin/python -m ruff format --check <本轮修改的 Python 文件>
make typecheck
```

### 14.3 主流程、生成物和发版硬门

```bash
pnpm --dir frontend run generate:api-types
pnpm --dir frontend test:unit
pnpm --dir frontend build
make main-flow-test
.venv/bin/python scripts/check_orphan_tests.py
.venv/bin/python scripts/check_test_quality_policy.py --manifest-only
make codex-guard
make test
```

### 14.4 真实 Compose 验收

必须使用容器选择的私有 `docker/.env` 和现有 Compose 服务，不用 local-debug 冒充：

1. 重建并启动 API/UI/LiteLLM sidecar，确认 `/health/ready`。
2. 对真实注册业务 Agent、真实模型运行 SDK SSE：
   - `with_speech_summary=true`；
   - 至少收到一个合法 10–50 字摘要；
   - SDK 原生源排空后不出现 `MirrorErrorMessage`；
   - 主回答正常，`agentgov.done` 最后。
3. 运行 Chat Stream `raw`、`semantic` 和 Responses control stream：
   - 三者都收到 canonical Speech 事件；
   - Responses 标准 terminal 恰好一次且最后。
4. 运行 `/api/chat` 与 `/v1/chat/completions` 非流式 smoke，确认兼容接口仍可用且不输出 Speech。
5. 运行 strict Responses，确认无 `agentgov.*`；运行 control non-stream +
   Speech flag，确认 `422`。
6. 开启 raw debug 后运行真实 raw：
   - header 声明 byte-exact；
   - body 是可识别的原生 Runtime stdout，不含 `agentgov.speech_summary`；
   - 显式 Speech 参数返回 `422`。
7. 至少执行一次 HITL 所需 Agent 的流式运行，验证 SDK/Chat/Responses SSE 或 raw polling 的
   requested -> decision -> resolved -> terminal 链路；非流式入口 fail-fast。
8. 记录 HTTP 4xx/5xx、浏览器 console error、SSE 最终事件和摘要样本；不得输出私有 key、URL
   query token 或工具敏感参数。

`make container-live-test` 继续验证真实 provider、DSPy typed output 和 Runtime；本轮
Speech Summary 专项验收使用
`COMPOSE_ENV_FILE=<已开启 raw 的完整 Compose env> make container-speech-summary-test`，
不得直接调用其私有 Make target 或验证脚本。只有 fresh-build 真实容器证据与全量硬门都通过，
才能进入 release。

## 15. 版本、提交、推送与 tag

- 根 `VERSION` 更新为 `3.0.3`，通过 `make sync-version` 或等价受控方式同步前端派生版本。
- 不手工硬编码 `app/version.py` 或 Compose image tag。
- 提交前检查 ignored 私有 env、runtime SQLite、日志、dist、cache 和测试 artifact 未进入 staged diff。
- 提交正文包含 `Compatibility` 和 `Verification`，说明兼容接口保留、Speech opt-in、raw 不变及真实验收。
- 先完成 Verify 和阶段收尾审计，再提交并推送 `master`。
- 创建 annotated tag `v3.0.3` 并推送。
- 最后用 `git ls-remote --heads origin master` 与
  `git ls-remote --tags origin refs/tags/v3.0.3` 验证远端 commit/tag，不能只凭本地命令成功。

## 16. 修订与退出条件

以下事实变化时重审本方案：

- Claude Agent SDK 提供原生、稳定、可安全直接播报的 summary 事件；
- 产品决定 Speech Summary 成为默认能力或进入持久化/replay；
- strict OpenAI Responses 需要扩大标准字段支持范围；
- 多 API worker 下 HITL pending state 迁移到可安全共享的权威服务；
- 兼容接口消费者清单归零并确认正式 sunset。

在这些条件发生前，Speech Summary 保持显式 opt-in、非持久化、best-effort；raw 保持
byte-exact；Responses-first 与 SDK-native 第一方入口保持不变。
