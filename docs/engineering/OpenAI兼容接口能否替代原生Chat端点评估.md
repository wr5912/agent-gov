# OpenAI 兼容接口能否替代原生 Chat 端点（评估与决策）

> 工程决策文档。回答一个具体问题：`/v1/chat/completions`（OpenAI 兼容）能否**完全替代** `/api/chat` 与 `/api/chat/stream`，从而把这两个原生端点删除。
>
> 契约真相源是 OpenAPI。本文结论以当前代码为准：`app/routers/chat.py`、`app/routers/openai.py`、`app/runtime/schemas.py`、`app/runtime/claude_runtime_stream.py`、`app/routers/settings.py`。人类确认（HITL）机制的权威设计见 [Claude 原生业务Agent人类确认机制整改实现方案](./Claude原生业务Agent人类确认机制整改实现方案.md)。

## 背景与问题

平台当前有三个「跑一次 Claude Agent」的入口：

- `/api/chat`（`chat.py`）：整包同步返回，`runtime.run`。
- `/api/chat/stream`（`chat.py`）：SSE 流式，`runtime.stream`，是前端 Playground 的实时对话入口。
- `/v1/chat/completions`（`openai.py`）：OpenAI 兼容出口，供外部按 OpenAI 协议接入。

问题：既然 `/v1/chat/completions` 是 OpenAI 兼容接口、且 OpenAI 官方 `/v1/chat/completions` 支持流式（`stream=true`），把它做成**完整的官方 OpenAI 接口**后，能否完全替代前两个原生端点并删除它们？

一句话结论：**不能完全替代**。卡点**不在流式**（流式可加），而在 `/api/chat/stream` 承载的 **HITL 人在环工具授权** 与 **富结构化事件流**——这两者在 OpenAI `chat/completions` 协议之外，做多全也装不下。`/api/chat`（同步）在原理上可被覆盖，但 `/api/chat/stream` 必须保留。

## 一、三端点现状（能力矩阵）

| 能力 | `/api/chat` | `/api/chat/stream` | `/v1/chat/completions`（现状 shim） |
| --- | --- | --- | --- |
| 返回形态 | 整包 `ChatResponse` | SSE：`session`/`message`/`result`/`error`/`done` 事件 | 整包 `OpenAIChatCompletionResponse`（`stream` 字段当前为兼容保留、返回非流式） |
| HITL 人在环工具授权 | 无 | **有**（`web_hitl`、`create_and_wait`、`can_use_tool`、独立 decision 端点） | 无 |
| per-request 选 Agent | **必填 `agent_id`**，可路由任意 Agent | **必填 `agent_id`**，可路由任意 Agent | **无 agent_id**，锁定到单个运营者配置的出口 Agent（`openai-compat-agent`，默认 main） |
| 消息模型 | 单 `message` + `session_id` 续接 | 同左 | 把 `messages[]` 拍平成一条 `"role: content"` 字符串 prompt |
| 请求上下文字段 | `session_id`/`alert_id`/`case_id`/`system_append`/`model`/`max_turns`/`metadata` | 同左 | 仅 `messages`/`model`/`stream`/`max_turns`/`metadata` |
| 富结构化事件（raw SDK message / 工具 / thinking） | 不适用（整包） | **有**（`message` 事件带 `raw` SDK 投影） | 无 |
| 定位 | 原生同步跑指定 Agent | **前端 Playground 实时流 + 交互治理** | 外部 OpenAI 客户端接一个固定出口 Agent |

三者共用同一执行核心：`/api/chat` 与 `/v1/chat/completions` 都调 `runtime.run`，`/api/chat/stream` 调 `runtime.stream`。**`/v1` 是建在 `runtime.run` 之上的更窄外部壳，不是原生端点的超集。**

## 二、按「官方 OpenAI 接口为准」重评

把「当前 shim 没实现」与「协议根本表达不了」分开，逐项对齐官方 OpenAI `chat/completions`：

| 原生能力 | 官方 OpenAI 能否承载 | 性质 |
| --- | --- | --- |
| 流式文本 | ✅ 能（`stream=true`，`choices[].delta.content` 分块） | **实现缺口**（可加，非协议限制） |
| per-request 选 Agent | ✅ 能（用 `model` 字段编码路由，如 `model:"soc-ops"`，是网关惯例） | 约定可解 |
| SOC/system 上下文 | ✅ 基本能（`metadata` + system message 映射 `alert/case/system_append`） | 约定可解 |
| 整包同步（`/api/chat`） | ✅ 能 | 覆盖成立 |
| **HITL 人在环工具授权** | ❌ **协议根本表达不了** | **决定性阻断** |
| **富结构化事件流**（raw SDK message / 工具时间线 / thinking） | ❌ 严重降级 | 次级阻断 |
| 服务端 `session_id` 续接 / transcript | ⚠️ 语义反转（`chat/completions` 无状态，客户端持全历史） | 语义变更 |

即：**流式与选 Agent 都不是阻断点**——OpenAI 支持流式，选 Agent 可用 `model` 编码。上一版评估把「流式」当成硬阻断是错的，此处纠正。真正过不去的是 HITL 与富事件。

## 三、决定性阻断：HITL 人在环工具授权

OpenAI `chat/completions` 是**请求 → 响应**；即使流式，也是**单向** server→client 的 delta，直到 `[DONE]`。协议里**没有**「流中途暂停、向客户端索要一个工具授权决定、客户端把决定回传、再 resume 同一个服务端执行」的双向中途交互。

有人会提议用 OpenAI function-calling 顶替，但**语义不同**：

- **OpenAI function-calling**：模型产 `tool_call` → **客户端执行工具** → 客户端把结果作为**新请求**（`role:"tool"`）回传 → 模型续。工具在**客户端**跑、多请求、无人类审批语义。
- **AgentGov 的 HITL**（`claude_runtime_stream.py`）：工具在**服务端**沙箱 workspace 跑（Read/Bash/Edit…），**人只是审批**；`can_use_tool` 是**进程内 live 回调**，通过 `service.create_and_wait` 挂起，前端展示授权卡，用户经独立端点 `/api/claude-user-input-requests/{id}/decision` 回传决定，服务端 **resume 同一个正在跑的 query**。

「审批服务端执行、原地 resume」映射不到「客户端执行并回传结果」。这不是字段多少的问题，是**交互拓扑不同**——OpenAI 的 completion 是一次性单向流，装不下一个有状态、双向、跨「授权卡」的中途握手。故 HITL 是协议层阻断。

## 四、次级阻断：富结构化事件流与服务端 session 语义

- **富事件流**：OpenAI stream 分块只能带 `delta.content`（assistant 文本增量）+ `delta.tool_calls`（函数调用增量）+ `finish_reason`。而 `/api/chat/stream` 的 `message` 事件带 `raw` Claude SDK 消息投影（`tool_use`/`tool_result`/thinking 等），前端据此渲染**工具时间线、逐工具 I/O、思考过程**。强行塞进 OpenAI 分块会丢掉这些结构，Playground 的可观测面随之退化。
- **session 语义反转**：`chat/completions` 是无状态的——每次请求由客户端带全 `messages[]` 历史。AgentGov 是**服务端 session**（SDK session transcript + `session_id` 续接/恢复），且反馈闭环、Langfuse trace 投影、版本 stamping 都挂在服务端 `session_id`/`run_id` 上。改成 OpenAI 无状态模型等于把 session 真相从服务端搬到客户端，牵动反馈闭环，不是删端点能顺带完成的。

## 五、消费者与影响面

- **前端 Playground 直接依赖** `/api/chat/stream`：`frontend/src/api/runtime.ts:336` `fetch(makeUrl(config, "/api/chat/stream"))`。删了 Playground 实时对话与 HITL 直接瘫。
- **测试**依赖原生端点与 HITL：`tests/test_claude_runtime_hitl.py`、`tests/test_chat_stream_agent_id.py`、`tests/test_claude_runtime_session_resume.py`、`tests/test_api_error_handlers.py` 等。
- **`/v1/chat/completions`** 供外部 OpenAI 客户端；出口 Agent 经 `/api/settings/openai-compat-agent` 配置（见 `settings.py`）。这是**外部集成面**，与内部控制面互补。

## 六、结论

分开说才准确：

- **`/api/chat`（同步）**：**原理上可被**一个做全的官方 OpenAI 端点覆盖（都是整包 in/out，选 Agent 靠 `model`，不需 HITL）。这一个具备被替代/收敛的条件。
- **`/api/chat/stream`（交互式 HITL + 富事件）**：**不能被替代**。HITL 的服务端执行 + 人在环审批 + resume，以及结构化工具事件，都在 OpenAI `chat/completions` 协议**之外**。

因此「用 `/v1/chat/completions` 完全替代**两个**原生端点」= **不成立**。这本质不是「API 是否等价」，而是**产品取舍**：是否愿意在 Playground 放弃 HITL 交互审批与工具可观测。

## 七、产品取舍与迁移选项

- **方案 A（推荐·默认保留）**：三个端点各司其职。`/v1` 做外部 OpenAI 出口（可后续补 `stream=true` 让外部客户端也能流式），`/api/chat` 做原生同步，`/api/chat/stream` 做 Playground 交互流 + HITL。不删任何端点。
- **方案 B（收敛 `/api/chat` → `/v1`）**：把 `/v1/chat/completions` 做成完整官方接口（含流式 + `model` 编码选 Agent + `metadata`/system 映射），之后**只删 `/api/chat`（同步）**、保留 `/api/chat/stream`。收益有限（少一个薄端点），需迁移 `/api/chat` 的现有调用方与测试，且引入 `model` 路由约定；`/api/chat/stream` 仍不可动。
- **方案 C（放弃 HITL 才能删两个）**：仅当产品决定**不再需要** Playground 的交互式工具授权与结构化工具时间线时，才可能把两者收敛到一个官方 OpenAI 流式端点。这是**砍功能**决策，不是 API 整理，需重构前端 Playground 为纯 OpenAI 流式客户端，代价与风险显著大于收益。

## 八、建议

- 采用**方案 A**：保留三端点。若要精简，最多讨论方案 B（删 `/api/chat` 同步端点这一个独立问题），且需先核查 `/api/chat` 的实际活跃调用方。
- **不要**以「OpenAI 兼容」为由删 `/api/chat/stream`：HITL 与富事件是它的产品价值，也是 OpenAI 协议给不了的。
- 若外部集成需要流式，正确做法是**给 `/v1` 补 OpenAI 官方 `stream=true`**（外部客户端受益），而不是反向拿 `/v1` 替代内部控制面。

## 附录：契约真相源与验证

- 契约真相源：OpenAPI（`scripts/export_openapi.py` 导出；`tests/test_openapi_export.py`、`tests/test_documentation_contracts.py` 守端点存在性）。
- 关键代码锚点：`app/routers/chat.py`（原生两端点）、`app/routers/openai.py`（`/v1` 出口）、`app/runtime/schemas.py`（`ChatRequest` vs `OpenAIChatCompletionRequest`）、`app/runtime/claude_runtime_stream.py`（HITL：`web_hitl`/`create_and_wait`/`can_use_tool` 与事件流）。
- 复核方式：`grep -rn '/api/chat/stream' frontend/src` 确认前端依赖；`OpenAIChatCompletionRequest.stream` 字段说明确认流式当前为兼容保留、未实现。
