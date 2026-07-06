# OpenAI 兼容接口能否替代原生 Chat 端点（评估、择优目标契约与路线图）

> 工程决策文档。回答三个问题：
>
> 1. 纯官方标准 `/v1/chat/completions` 能否完全替代 `/api/chat` 与 `/api/chat/stream`？
> 2. 允许在 `/v1/chat/completions` 之上加 AgentGov 私有扩展时，能否设计一条完全替代路径（能力基线）？
> 3. 落地口径如何定位这三个端点？
>
> **定位（不变量）**：`/v1/chat/completions` 是 **canonical（规范）Chat 接口**，所有对外文档、集成指南、示例与前端主推入口**以它为准**。`/api/chat`、`/api/chat/stream` **保留为兼容面、不删除**——直到 `/v1` 完全替代它们**且**由使用方主动要求删除时才另行评估；本文档**不安排、不承诺删除**原生端点。
>
> 契约真相源是 OpenAPI。现状事实以 `app/routers/chat.py`、`app/routers/openai.py`、`app/routers/claude_user_input.py`、`app/runtime/schemas.py`、`app/runtime/claude_user_input_schemas.py`、`app/runtime/claude_runtime_stream.py`、`app/runtime/claude_user_input_service.py`、`app/routers/settings.py` 为准。HITL 权威设计见 [Claude 原生业务Agent人类确认机制整改实现方案](./Claude原生业务Agent人类确认机制整改实现方案.md)。

## 结论

- **纯 OpenAI 官方标准层**：不能完全替代。`chat/completions` 支持流式和 `tool_calls`，但它表达的是模型响应、标准流式 chunk 和模型生成的工具调用描述；它不表达 `/api/chat/stream` 的服务端 Claude Code 进程内工具审批、授权卡、原地 resume、raw SDK 事件和治理观测链路。
- **AgentGov 扩展层**：可以作为完整替代的**能力基线**——把 `/v1/chat/completions`（strict）与 `/v1/agentgov/chat/completions`（control）建成完整控制面协议后，即具备替代原生端点的全部能力。这不是「把私有能力伪装成纯标准 OpenAI」，而是「以官方 Chat Completions 外形做公共入口，以 AgentGov 扩展协议承载治理控制面」。

**方案 D（推荐目标）**：双入口，职责隔离——

- `openai-compatible`（strict）= `/v1/chat/completions`：严格兼容外部 OpenAI 客户端，不含任何 AgentGov 私有字段/事件。
- `agentgov-control`（control）= `/v1/agentgov/chat/completions`：承载 Agent 选择、服务端 session、HITL confirmation、规整工具时间线、raw SDK 事件和反馈治理字段——作为 `/api/chat`、`/api/chat/stream` 的**能力对等目标**。

> 本文给出的 control 契约是**择优目标契约**：经对抗式设计审查，既不盲从当前代码实现（其中若干设计有可优化处，下文逐一标注），也纠正早期草案里丢失安全字段/违反 OpenAI 规范的写法。原生端点在 `/v1` 达到能力对等前保留不变。

## 当前三端点现状

| 能力 | `/api/chat` | `/api/chat/stream` | `/v1/chat/completions` 当前 shim |
| --- | --- | --- | --- |
| 返回形态 | 整包 `ChatResponse` | SSE 8 事件（下） | 整包 `OpenAIChatCompletionResponse` |
| 流式 | 无 | 有 | `stream` 字段兼容保留，当前返回非流式 |
| HITL 人在环工具授权 | 无 | 有：`can_use_tool` + confirmation request + decision API | 无 |
| per-request 选 Agent | 必填 `agent_id`（缺失 422，不静默跑 main） | 同左 | 无 `agent_id`，走运营者配置 `openai-compat-agent`（未配置→main，失效→503） |
| 消息模型 | 单 `message` + `session_id` | 同左 | `messages[]` 全量拍平成 `"role: content"` 单 prompt |
| 服务端 session | 有 | 有 | 未暴露 AgentGov session 语义（无状态） |
| SDK 消息投影 | 非流式整包 `messages[]`（SDK 消息投影） | 流式 raw envelope + 规整时间线 | 无 |
| 定位 | 原生同步跑指定 Agent | Playground 实时交互与确认卡 | 外部 OpenAI 客户端入口 |

**`/api/chat/stream` 当前真实 SSE 事件（8 个）**：`session`（`claude_runtime_stream.py:71`）、`message`（:146）、`result`（:173）、`error`（:211/:218）、`done`（:221）、`heartbeat`（:266，空闲保活）、`claude_user_input_required`（`claude_user_input_service.py:158`）、`claude_user_input_resolved`（:169/:208/:226，HITL 确认卡事件，前端 `App.tsx:460/483` 按字面名分支）。

三者共用执行核心：`/api/chat` 与 `/v1/chat/completions` 调 `runtime.run`，`/api/chat/stream` 调 `runtime.stream`。当前 `/v1` 是建在 `runtime.run` 之上的更窄外部壳，**不是原生端点的超集**——这正是要通过方案 D 把它建成 canonical 全能力接口的原因。

## 官方标准能承载什么

| 能力 | 纯标准 Chat Completions | 说明 |
| --- | --- | --- |
| 文本整包 / 流式返回 | 能 | 标准 `chat.completion` / `stream=true` 的 `chat.completion.chunk` |
| 模型生成工具调用 | 能 | 标准 `tool_calls` 表达模型建议调用的函数工具 |
| per-request 选 Agent | 需扩展 | OpenAI 无 `agent_id`；见下文 Agent 选择（用扩展字段，不重载 `model`） |
| SOC/system 上下文 | 需扩展 | 见下文控制面字段 |
| 服务端 session 续接 | 不能无损 | `chat/completions` 无状态、客户端持全 messages；AgentGov 权威 session 在服务端 |
| HITL 工具审批 | 不能无损 | 标准协议没有「服务端执行前暂停、让人审批、同一运行 resume」的双向中途握手 |
| raw SDK 事件与工具 I/O 时间线 | 不能无损 | 标准 chunk 无 AgentGov raw Claude SDK event envelope |

> **`model` 不重载为 Agent 路由**：OpenAI `model` 是 LLM 模型标识，把它兼作业务 Agent 句柄（如 `agentgov/{id}`）会造成双语义、OpenAPI 无法干净建模并误导客户端。Agent 选择一律走扩展字段（见下）。

外部依据：OpenAI Chat Completions 官方参考 `https://developers.openai.com/api/reference/resources/chat`。官方 Responses API（conversation/background/previous_response）不在本决策范围；若未来选用需另立评估。

## 决定性差异：工具调用与 HITL 不是同一个拓扑

OpenAI `tool_calls` 是模型输出：模型描述「应调用某函数」，**客户端**执行工具、再把结果作为后续上下文回传。AgentGov 的 HITL 是**服务端** Claude Code 运行时的权限回调：

1. Claude Code 准备调用 `Read`/`Bash`/`Edit` 等服务端工具。
2. 后端 `can_use_tool` 回调触发。
3. 后端创建 confirmation request，经 SSE 推给前端。
4. 用户在确认卡上选允许/拒绝/本次运行允许，或回答 `AskUserQuestion`。
5. 后端把 decision 回传给仍在运行的 query。
6. 同一运行继续执行，保留同一 `run_id`/`session_id`/Langfuse trace/工具 I/O。

「服务端执行前挂起、等人审批、resume 同一运行」是纯标准 Chat Completions 没有的协议动作——这是必须用 control 扩展承载、而非纯标准能覆盖的根因。

## 能力基线：`/v1/agentgov/chat/completions`（control）目标契约

### 设计原则

1. **双入口，职责隔离**：strict = `/v1/chat/completions`（纯 OpenAI）、control = `/v1/agentgov/chat/completions`（扩展）。两套 schema 各挂一个 OpenAPI operation，契约完整、类型生成不残缺、天然抗网关剥离/注入。
   *备选（若产品坚持单路径）*：同路径 + `X-AgentGov-Mode: control` header；header 缺失必须 **fail-safe 到 strict** + mode 决策来源可观测 + 响应回带 effective mode 供 control 客户端检测降级。本文推荐独立路径。
2. **控制面字段进请求体顶层 `agentgov:{}`（强类型子模型，`extra=forbid`），不进 `metadata`**：OpenAI `metadata` 是扁平 `map[string,string]`（≤16 对、value≤512 字符），装不下嵌套对象、强类型 SDK 会序列化失败；且与既有扁平 `metadata.agent_id`（`claude_runtime.py:488-508` 当观测属性读）双轨漂移。`metadata` 保持 OpenAI 纯观测语义。请求/响应对称（响应侧也用顶层 `agentgov`）。
3. **标准客户端零污染**：strict 入口不接受 `agentgov` 字段、不发 `agentgov.*` 事件、不回显可续接 `session_id`；外部 OpenAI SDK 只连 strict 入口。
4. **服务端 session 为权威**：`session_id`/`sdk_session_id`/`run_id`/Langfuse trace/反馈闭环是事实源；`messages[]` 只是输入外形。

### 请求体（control）

```json
{
  "model": "claude-sonnet-5",
  "stream": true,
  "messages": [{"role": "user", "content": "帮我生成一份日报"}],
  "agentgov": {
    "agent_id": "main-agent",
    "session_id": "sess_...",
    "alert_id": "alert-001",
    "case_id": "case-001",
    "system_append": "只输出日报正文",
    "max_turns": 8,
    "hitl": {"enabled": true, "allow_for_run": false},
    "trace": {"client_run_label": "playground"}
  }
}
```

字段口径：

| 字段 | 位置 | 用途 |
| --- | --- | --- |
| `model` | 标准 | **仅** per-request LLM override（`schemas.py:26`），永不承载 Agent 路由 |
| `messages` | 标准 | 输入外形；服务端转 Claude Code prompt。最新 user message 为本次输入 |
| `stream` | 标准 | `false` 对等 `/api/chat`，`true` 对等 `/api/chat/stream` |
| `agentgov.agent_id` | 扩展（必填） | 业务 Agent；control 唯一 Agent 来源；缺失→硬 422 |
| `agentgov.session_id` | 扩展 | 服务端 session 续接依据 |
| `agentgov.system_append` | 扩展 | 对应 `ChatRequest.system_append` |
| `agentgov.alert_id`/`case_id` | 扩展 | 反馈闭环上下文 |
| `agentgov.max_turns` | 扩展 | 对应 `ChatRequest.max_turns` |
| `agentgov.hitl` | 扩展 | HITL 开关与运行级授权策略 |

### Agent 选择与 session 契约

- **Agent 来源**：control 唯一来源 `agentgov.agent_id`；缺失→**硬 422**（继承 `chat.py:16-22` 的「不静默跑 main」不变量，取消早期草案「由产品决定」的后门）；未找到/失效→fail-loud（404/503，沿用 `openai.py:52-56`）。strict 无 per-request Agent 入口，用运营者配置 `openai-compat-agent`（未配置→main，为运营者显式预选、非静默）。两模式非对称在 OpenAPI/文档显式说明。
- **session**：服务端 `session_id` 为权威续接源；**strict 保持无状态**（不接受/不回显 `session_id`，避免污染 OpenAI 无状态语义误导标准客户端）；control 为显式有状态扩展——`agentgov.session_id` 存在则服务端 resume，`messages[]` 只作输入外形，`run_id`/`sdk_session_id`/trace/反馈上下文由服务端生成投影。

### 非流式对等 `/api/chat`

`stream=false` 时返回标准 `chat.completion` 主干 + 顶层 `agentgov` 扩展，对齐真实 `ChatResponse`（`schemas.py:43-57`：`run_id/session_id/sdk_session_id/agent_version_id/answer/messages/agent_activity/usage/total_cost_usd/stop_reason/errors`；无 `agent_id`，`agent_id` 是 backend-owned 投影）：

```json
{
  "id": "chatcmpl_...",
  "object": "chat.completion",
  "model": "claude-sonnet-5",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "日报正文..."}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
  "agentgov": {
    "run_id": "run_...", "session_id": "sess_...", "sdk_session_id": "sdk_...",
    "agent_id": "main-agent", "agent_version_id": "ver_...",
    "messages": [], "agent_activity": {},
    "usage": {}, "total_cost_usd": 0.0, "stop_reason": "end_turn", "errors": []
  }
}
```

### 流式对等 `/api/chat/stream`：control 事件模型

control 流用**统一控制面信封** `{v, type, run_id, ts, seq?, payload}`：SSE `event:` 行 = `type`（闭合 enum）、`data:` 行 = 信封 JSON、可选 `id:` = `seq`（排序/去重；断线 replay buffer 暂 defer，离线单机需求不明、不为不确定需求过度设计）。openai-compatible 入口不套此信封、原样标准 chunk + `[DONE]`。

| `type` | 取代当前事件 | 作用 |
| --- | --- | --- |
| `agentgov.session` | `session` | 最先下发 `run_id/session_id/sdk_session_id/agent_id/agent_version_id/alert_id/case_id`，并下发 `heartbeat_interval_s`（+可选 `hitl_decision_timeout_s`）供客户端派生 idle |
| `agentgov.delta` | `message`（文本部分） | 仅有 assistant 文本增量时发，`payload={text}`——取代当前把稳定文本、SDK 类名、raw 混塞进一个 `message` 事件、前端靠 `startsWith("AssistantMessage")` hack 区分（`runtime.ts:452-456`）的脆弱设计 |
| `agentgov.tool_step` | `message`（工具部分） | 仅工具 use/result 消息发，后端把 SDK 消息投影成规整时间线 `{tool_name,input,result,duration,risk}`（**复用现有 activity_extractor 投影，单一来源**，勿另造第二份工具语义）——稳定 UI 层 |
| `agentgov.sdk_raw` | `message.raw` | 整包 raw Claude SDK envelope，**仅 verbose/dev 开关下发**，默认关，不进生产/标准载荷——调试层 |
| `agentgov.confirmation.requested` | `claude_user_input_required` | 要求前端展示人工确认卡（含 `request_id` 与本次 `decision_token`，见 HITL） |
| `agentgov.confirmation.resolved` | `claude_user_input_resolved` | 回显 allow/deny/allow_for_run/answer 结果（不回带 `decision_token`） |
| `agentgov.result` | `result` | 最终 `ChatResponse` 等价扩展结果 |
| `agentgov.error` | `error` | 结构化错误 |
| `agentgov.done` | `done` | 结束流 |
| `agentgov.heartbeat` | `heartbeat` | 保活；建议实现为 SSE comment 行（`: keepalive`），不进业务时间线 |

**保活契约（硬约束）**：唯一不变量是 `heartbeat_interval < client_idle`（现状 15s < 180s，`claude_runtime_stream.py:258` / `runtime.ts:45`）。HITL 决策超时 300s（`claude_user_input_service.py:20`）远大于前端 idle，长确认等待**全靠 heartbeat 维持连接**——client 应按 `heartbeat_interval_s * N + margin` 从 `agentgov.session` 声明派生 idle，**不硬编码 180**，否则慢确认会误超时断流。

### HITL decision 契约（control）

当前真实端点：`POST /api/claude-user-input-requests/{request_id}/decision`（`claude_user_input.py:65`，+ 隐藏 legacy alias `/api/claude-hitl-requests/{request_id}/decision`）。control 迁入 `/v1` 命名空间：

```http
POST /v1/agentgov/confirmation-requests/{request_id}/decision
```

目标 body（`extra=forbid`）：

```json
{
  "action": "allow_once",           // allow_once | allow_for_run | deny | answer_question
  "decision_token": "<per-request token，唯一授权因子，必带>",
  "message": "<可选，deny 说明>",
  "answer": null                     // 可选，仅 answer_question；单一字段，形状镜像 SDK AskUserQuestion 答案 schema
}
```

设计要点（相对当前代码 + 早期草案的择优）：

- **保留 `decision_token`（必带）**：`secrets.token_urlsafe(32)` 生成、sha256 存库、仅在 `confirmation.requested` 事件下发、`hmac.compare_digest` constant-time 校验（`claude_user_input_service.py:122/88/192`）。它是 API key 之上的 per-request 第二因子——早期草案 `{action, message, updated_input}` 删掉 token 会让 API key 成为唯一门槛（任何 key 持有者可批准任意危险工具），是安全退步，**明确拒绝**。
- **删除客户端回传的 `run_id/session_id/business_agent_id` 三元组**（相对当前代码简化）：`request_id`（URL）已唯一定位 record，三者服务端从 record 全知；三元组经 GET list 公开可读（`claude_user_input_records.py:84-87`），不构成第二因子。防串扰/防重放/匹配等待运行由 `decision_token` + `request_id→pending` 1:1 + `status` 单次 `waiting→resolved`（`service.py:184-194`，`test_claude_user_input.py:484-501` 固化）达成。若需防 store 错乱，由服务端内部 assert record 自身字段，不进对外 body。**校验顺序建议**：record 存在性 → `decision_token`（constant-time）→ status/pending，并归一 404/token-invalid 措辞（简化 + 纵深防御，避免差异化错误让仅持 key 者枚举确认 request 上下文）。
- **`extra=forbid` 保留**，堵未知字段（`test_claude_user_input.py:423-451` 固化拒 `updated_input`/`allow_modified`）。
- **`answer_question`**：把当前 `answers`(JsonObject)+`response`(str) 双字段（`schemas.py:47-48`，可同时出现、无优先级）**收敛为单一 `answer`**，以 SDK/Claude Code 的 AskUserQuestion 答案 schema 为单一真相源（落地前核对 SDK 实际契约，勿臆造字段）。
- **迁移只改路径前缀，不改 body 安全语义**；旧 `/api/...` 路径保留 deprecated alias（现状已有 alias）。

> **`updated_input`/`allow_modified`（受控修改工具参数）暂不进最小契约**。「批准 Bash 但改命令」是有真实价值的 HITL 能力，但裸加一个字段不安全：创建时算好的 `risk_json`/`low_risk_category`（`service.py:126/143`）是按原始 input 算的，裸 `updated_input` 与 `allow_for_run` 组合会用原始风险类别授权修改后的命令，造成风险分类 desync（状态漂移）。**要则必须作为完整受控子系统交付**：独立 `action` + 按工具的可改字段白名单（如仅 `Bash.command`/`Write.content`，禁改 `file_path` 目标）+ 对修改后 input **重跑风险分类** + 强制 `scope=once`（禁 `allow_for_run`）+ 原始/修改双写审计 + 复用同一安全谓词。当前默认禁用，未来单独立项。

## 路线图（把 /v1 建成 canonical，原生端点保留）

目标是让 `/v1` 达到能力对等（上文 control 契约）并成为文档/集成/前端主推入口；**原生端点全程保留、不删**。

1. **契约冻结**：在 OpenAPI 表达 strict（`/v1/chat/completions`）与 control（`/v1/agentgov/chat/completions`）两套 operation、control 请求/响应扩展 schema、SSE 控制面信封的 event envelope schema（今天 `/api/chat/stream` 的 SSE 本就不在 OpenAPI、前端靠手写 `StreamEnvelope`——借此把 event envelope 固化为显式 schema 并纳入契约测试，是既有缺口的收口机会）。strict 快照保持兼容，现有 OpenAI 客户端 smoke 不受影响。
2. **实现非流式 control**：`/v1/agentgov/chat/completions` `stream=false` 接受顶层 `agentgov`，复用 `resolve_business_profile` + `runtime.run`，响应加 `agentgov` 扩展对齐 `ChatResponse`。验收：与 `/api/chat` 同输入产出等价 `run_id/session_id/answer/errors`。
3. **实现流式 control**：`stream=true` 走 `runtime.stream`，标准文本增量 → `agentgov.delta`，8 事件迁移为控制面信封 enum（含 `heartbeat` 保活、`claude_user_input_*`→`confirmation.*`）。验收：Playground 用新端点完成普通对话、工具时间线、HITL 卡片、allow/deny/allow_for_run、AskUserQuestion、断连取消、心跳保活（对齐前端 idle）、超时错误，且 Langfuse trace/`run_id`/`session_id` 与旧端点等价。
4. **文档 / 集成 / 前端主推切到 /v1**：前端 Playground 客户端改调 `/v1/agentgov/chat/completions`；集成指南与对外文档以 `/v1` 为准（strict 给外部 OpenAI 客户端、control 给需 HITL/治理的上层），把 `/api/chat`、`/api/chat/stream` 标注为「仍支持的兼容面」。**原生端点保留**，不设删除阶段。
5. **删除原生端点——不在本文档范围**：仅当 `/v1` 完全替代到位**且**使用方主动要求时才另行评估；届时需真实容器端到端验收（普通对话、HITL、AskUserQuestion、session resume、agent 切换、trace、反馈闭环）、消费者矩阵确认、对外弃用/Sunset 公告窗口。本文档不承诺、不安排删除。

## 消费者与兼容边界

- **前端 Playground** 依赖 `/api/chat/stream`（`frontend/src/api/runtime.ts:336`）；**测试**依赖原生端点与 HITL（`test_claude_runtime_hitl` / `test_chat_stream_agent_id` / `test_claude_runtime_session_resume` 等）；**集成指南** `docs/AgentGov集成指南.md:132` 已把 `/api/chat`、`/api/chat/stream` 列为对外承诺的「稳定面（可放心长期依赖）」。
- 鉴权为**单一共享 bearer key**（`app/main.py:191-194`，无 caller/tenant 身份），telemetry 无法识别匿名外部调用方——这也是「不凭代码搜索/日志断言无外部调用方就删端点」的原因；原生端点保留即规避此风险。
- **control 模式鉴权**：control 与 strict 是否共用同一 key、是否需独立 scope/密钥限制谁能启用私有控制面，需在契约冻结阶段定；decision 端点在单 key 之上仍由 per-request `decision_token` 二次授权（现状），迁移不得弱化。

## 风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 污染 OpenAI 兼容面 | 标准客户端收到私有事件/字段解析失败 | 独立路径隔离（strict 入口无 `agentgov` 字段/事件）；同路径 fallback 时 header 缺失 fail-safe 到 strict |
| `metadata` 承载控制面 | 违反 OpenAI 扁平 map/≤512 规范、强类型 SDK 崩、与扁平 `metadata.agent_id` 双轨 | 控制面进请求体顶层 `agentgov:{}`，`metadata` 保持纯观测 |
| 删 `decision_token` | 任意 key 持有者可批准危险工具 | 保留 `decision_token` 为唯一授权因子、首验、constant-time |
| 裸 `updated_input` | 与原始风险分类 desync、越权 | 默认禁用；要则以完整受控子系统交付 |
| 事件盘点不全致断流 | 漏 `heartbeat`/HITL 事件，长确认断流或 UI 漂 | 8 事件全量迁移、显式保活契约、client idle 从声明派生 |
| 文档/集成切 /v1 但原生仍在 | 读者误以为原生已弃用 | 明确「原生保留为兼容面、不删」，标注两者关系 |
| SSE 私有事件难进 OpenAPI | 类型生成不完整 | 用显式 event envelope schema 并纳入契约测试（收口既有缺口，非新增风险） |

## 定位与边界（不变量）

- **`/v1/chat/completions` 是 canonical Chat 接口**，所有文档、集成指南、示例、前端主推入口以它为准。
- **`/api/chat`、`/api/chat/stream` 保留为兼容面、不删除**——直到 `/v1` 完全替代**且**使用方主动要求删除时才另行评估；本文档不安排、不承诺删除。
- 目标契约不得违背：防串扰/防重放/HITL 进程内审批、以 SDK 为中心/后端薄投影、离线不变量、OpenAPI 为契约真相源、`/api/chat` 的「不静默跑 main」。
- 本文的 control 契约是**推荐目标**，保留同路径 + header 作为 fallback，不单方废弃。

## 附录：契约真相源与验证

- 契约真相源：OpenAPI（`scripts/export_openapi.py` 导出；`tests/test_openapi_export.py`、`tests/test_documentation_contracts.py` 守端点存在性）。
- 关键代码锚点：
  - 原生端点：`app/routers/chat.py`（`/api/chat`、`/api/chat/stream`）、`app/routers/openai.py`（`/v1` 出口 + `openai-compat-agent`）。
  - 契约/字段：`app/runtime/schemas.py`（`ChatRequest`/`ChatResponse`/`OpenAIChatCompletion*`）。
  - HITL：`app/routers/claude_user_input.py`（decision 路由 + legacy alias）、`app/runtime/claude_user_input_schemas.py`（action 枚举与 decision 契约）、`app/runtime/claude_user_input_service.py`（`create_and_wait:108`、token hmac、上下文匹配、SSE 事件）、`app/runtime/claude_runtime_stream.py`（流式事件与心跳）。
- 复核方式：`rg -n '/api/chat/stream' frontend/src app tests docs` 确认旧路径依赖；`rg -n 'OpenAIChatCompletion(Request|Response)' app tests frontend/src` 确认 `/v1` 契约；真实容器中用 Playground 验证 control stream、确认卡、trace 与 session resume。
