# OpenAI 兼容接口能否替代原生 Chat 端点（Responses-first ADR 与已落地契约）

> 状态：**Accepted / Implemented**。截至 `2026-07-10`，Responses-first 主路径、Conversations 会话投影和 canonical HITL decision API 已落地。
>
> 当前结论：AgentGov 已采用 OpenAI **Responses API 系列接口**作为主路径，首推 `POST /v1/responses` + `/v1/conversations`；`/v1/chat/completions`、`/api/chat`、`/api/chat/stream` 只保留兼容。
>
> **口径变更声明**：本文的 canonical 由早期版本的 `/v1/chat/completions` 改为 **Responses-first**（`/v1/responses` + `/v1/conversations`），已经使用方确认；理由见「标准接口取舍」。此前「chat/completions 为 canonical」的口径作废。
>
> 契约真相源是 OpenAPI；当前实现以 `app/routers/responses.py`、`app/routers/conversations.py`、`app/routers/claude_user_input.py`、`app/runtime/openai_responses_schemas.py`、`app/runtime/openai_responses_stream.py`、`app/runtime/claude_user_input_schemas.py` 为准。外部接入权威说明见 [AgentGov 集成指南](../AgentGov集成指南.md)；早期 HITL [实现方案](../archive/design/Claude原生业务Agent人类确认机制整改实现方案.md)与[对抗审查计划](../archive/design/Claude原生业务Agent人类确认机制对抗审查整改计划.md)仅保留历史审计价值。

## 结论

- **主推入口**：`POST /v1/responses`。它承载一次 AgentGov 业务 Agent 运行，覆盖非流式、流式、HITL 人工确认、工具时间线、Trace、`run_id`、`session_id` 与反馈治理上下文。
- **会话入口**：`/v1/conversations` 系列。它承载会话创建、恢复、删除与历史读取；前端恢复会话历史应走 `/v1/conversations/{conversation_id}/items`，而不是继续扩展 `/api/sessions*`。
- **兼容入口**：`/v1/chat/completions` 只面向已有 OpenAI Chat Completions 客户端，作为兼容包装；`/api/chat` 与 `/api/chat/stream` 只作为历史兼容面保留。
- **扩展原则**：OpenAI 已有标准字段必须优先使用标准字段；只有业务 Agent 选择、HITL、Claude Code turn cap、raw SDK 调试等 OpenAI 无法表达的能力才进入 `agentgov` 扩展。
- **不删除旧端点**：本文不安排删除 `/api/chat`、`/api/chat/stream` 或 `/v1/chat/completions`。删除只能在新 `/v1` 能力对等、消费者矩阵确认、真实容器端到端验收之后另行评估。

官方依据：

- OpenAI 文档说明 Responses 是 Chat Completions 的演进，Chat Completions 仍支持，但 Responses 推荐用于新项目：<https://developers.openai.com/api/docs/guides/migrate-to-responses>。
- Conversation state 文档说明 Conversations API 与 Responses API 配套，用长期对象持久化会话状态，并存储 messages、tool calls、tool outputs 等 items：<https://developers.openai.com/api/docs/guides/conversation-state>。
- Conversations API 参考提供 create/retrieve/update/delete conversation 与 items create/retrieve/delete/list 等资源：<https://developers.openai.com/api/reference/resources/conversations/methods/create>。
- Chat Completions 的 `GET /chat/completions/{completion_id}/messages` 只读取 stored chat completion 的消息，不等价于本项目前端需要的业务会话历史恢复：<https://developers.openai.com/api/reference/resources/chat/subresources/completions/subresources/messages/methods/list/>。

## 决策前实现基线（历史）

下表记录 Responses-first 落地前的基线，用于解释本 ADR 的取舍；它不是截至 `2026-07-10` 的当前接口清单。

| 能力 | `/api/chat` | `/api/chat/stream` | `/v1/chat/completions` 当前 shim |
| --- | --- | --- | --- |
| 返回形态 | 整包 `ChatResponse` | SSE 事件 | 整包 `OpenAIChatCompletionResponse` |
| 流式 | 无 | 有 | `stream` 字段保留，当前返回非流式 |
| HITL 人在环工具授权 | 无 | 有：`can_use_tool` + confirmation request + decision API | 无 |
| per-request 选 Agent | 必填 `agent_id` | 必填 `agent_id` | 无 `agent_id`，走运营者配置 `openai-compat-agent` |
| 消息输入 | 单 `message` + `session_id` | 同左 | `messages[]` 拍平成一个 prompt |
| 服务端 session | 有 | 有 | 未暴露 AgentGov session 语义 |
| SDK 消息投影 | `messages[]` | raw envelope + activity 投影 | 无 |
| 定位 | 原生同步运行指定 Agent | Playground 实时交互与确认卡 | 外部 OpenAI Chat 客户端兼容 |

旧 `/api/chat/stream` 的事件包括 `session`、`message`、`result`、`error`、`done`、`heartbeat`、`claude_user_input_required`、`claude_user_input_resolved`。canonical `/v1/responses` 已将其投影为 Responses-style SSE + `agentgov.*` 扩展事件；旧入口继续返回原事件与 payload，供存量调用方兼容。

`/v1/chat/completions` 仍是 `runtime.run` 外的一层非流式 shim，不是 `/api/chat`、`/api/chat/stream` 或 `/v1/responses` 的超集。这正是它不再作为 canonical、仅保留兼容定位的原因。

## 标准接口取舍

| 接口 | 适合作为什么 | 不适合作为什么 |
| --- | --- | --- |
| `/v1/responses` | AgentGov 主运行入口；非流式/流式统一；承载模型输出、工具过程、HITL 扩展、run/session/trace 投影 | 纯 OpenAI Chat Completions 兼容壳 |
| `/v1/conversations` | 长期会话对象、跨刷新/跨设备恢复、items 历史读取 | 单次运行结果包装 |
| `/v1/chat/completions` | 兼容已有 OpenAI Chat 客户端；把 `messages[]` 映射到 AgentGov 运行 | 新增 AgentGov 控制面、HITL、会话治理字段 |
| `/api/chat` | 历史非流式兼容 | 新能力主入口 |
| `/api/chat/stream` | 历史流式兼容与旧 Playground 过渡 | 新事件模型主入口 |

关键判断：

- `model` 仍只表示 LLM 模型，不重载为业务 Agent 路由。
- 标准字段优先：`input`、`instructions`、`conversation`、`previous_response_id`、`metadata`、`stream`、`store` 等 OpenAI 已有字段不得在 `agentgov` 中平行定义。
- 业务 Agent 选择、HITL、Claude Code turn cap、raw SDK 调试开关等非标准控制面统一放入 `agentgov` 扩展对象。
- **字段所有权决定归属，不是「标准字段名优先」一刀切**：`alert_id`、`case_id` 是后端消费的**反馈闭环路由输入**（backend-owned，见 `app/runtime/schemas.py:17-18`、`app/runtime/runtime_db.py` 的 index 列），放入 `agentgov` 扩展（受 schema 约束、可校验、不与客户端 tag 碰撞）；`source`、`client_run_label` 是后端不路由、仅原样回显的**观测标签**，留在 OpenAI 标准 `metadata`（不透明客户端标签，≤16 对/value≤512）。
- **反馈闭环关联是核心不变量**：无论字段放哪，adapter 都必须把 `alert_id`/`case_id` 回填 `agent_runs` 的 index 列以维持 per-agent 反馈匹配；不能只写进 `payload_json`/`metadata` JSON blob。
- Chat Completions 的 stored messages API 是 completion 维度，不是 long-running conversation 维度；前端会话恢复应以 Conversations Items 为目标契约。
- 领域 API 不强行伪装成 OpenAI API。反馈优化、Agent job、Trace 查询等仍保留产品领域接口，但可在结果中关联 `response_id`、`conversation_id`、`trace_id`。

## 已落地契约

### `POST /v1/responses`

当前请求形态：

```json
{
  "model": "claude-sonnet-5",
  "instructions": "只输出日报正文",
  "input": "帮我生成一份日报",
  "stream": true,
  "store": true,
  "conversation": "conv_sess_...",
  "metadata": {
    "source": "playground",
    "client_run_label": "daily-report"
  },
  "agentgov": {
    "agent_id": "main-agent",
    "alert_id": "alert-001",
    "case_id": "case-001",
    "max_turns": 8,
    "hitl": {"enabled": true},
    "debug": {"sdk_raw": false}
  }
}
```

字段口径：

| 字段 | 归属 | 说明 |
| --- | --- | --- |
| `model` | OpenAI 标准 | 只作模型 override，不承载 Agent 路由 |
| `input` | OpenAI 标准 | 字符串或 items；服务端转为 Claude Code 本轮 prompt |
| `instructions` | OpenAI 标准字段名·语义非一致 | **本项目为 append-only 追加指令**（映射 `ChatRequest.system_append`，不覆盖 workspace `CLAUDE.md`/preset），**区别于官方 replace/swap 语义**；strict 模式处置见「模式口径」 |
| `stream` | OpenAI 标准 | `false` 对等兼容 `/api/chat`，`true` 对等兼容 `/api/chat/stream` |
| `store` | OpenAI 标准 | 默认 `true`，允许公开 retrieve；`false` 只关闭公开 `GET /v1/responses/{id}`，不关闭内部治理审计 |
| `conversation` | OpenAI 标准方向 | 指向 `conv_*` 会话；服务端映射到 AgentGov `session_id` / SDK resume id |
| `previous_response_id` | OpenAI 标准 | 用于推导上一轮所属 conversation；若与显式 `conversation` 不一致则 409 |
| `metadata` | OpenAI 标准 | 扁平**观测标签**（后端不解释、原样回显）；只承载 `source`、`client_run_label` 等 |
| `agentgov.agent_id` | AgentGov 扩展 | 控制模式必填；缺失硬 422，不静默跑 main |
| `agentgov.alert_id` / `case_id` | AgentGov 扩展 | 反馈闭环路由输入（backend-owned）；adapter 须回填 `agent_runs` index 列 |
| `agentgov.max_turns` | AgentGov 扩展 | Claude Code turn cap，对应现有 `ChatRequest.max_turns`，不等同于 OpenAI output token limit |
| `agentgov.hitl` | AgentGov 扩展 | schema 保留的运行级策略字段；当前在线 HITL 仍由 `ENABLE_CLAUDE_WEB_HITL` 与业务 Agent 权限规则触发，新集成不得依赖该字段自行开启确认 |
| `agentgov.debug` | AgentGov 扩展 | raw SDK envelope 等开发调试开关 |

> `input` 为 items 数组时，服务端取其文本/内容映射为本轮 Claude Code prompt。`instructions` 使用 OpenAI 标准字段名，但 AgentGov 的 system 身份以 Claude Code preset + workspace `CLAUDE.md` 为治理态单一真相源；因此 `instructions` 在本项目中只作为 **append-only** 追加指令，不具备替换受治理 prompt 的能力，**语义区别于官方 replace/swap**（strict 模式处置见「模式口径」）。不再新增 `agentgov.system_append` 这类平行字段。

模式口径：

- **strict mode**：请求不含 `agentgov`。该模式**标准字段形状兼容** OpenAI Responses，使用运营者配置的 OpenAI-compat Agent，不发 `agentgov.*` 私有事件。**但不是纯 OpenAI 语义子集**：AgentGov 的 system 身份以 Claude Code preset + workspace `CLAUDE.md` 为单一真相源，`instructions` 为 **append-only**、不实现官方 replace/swap 语义，因此 strict mode 传 `instructions` 固定返回 `422`，避免纯 OpenAI 客户端误以为能替换或清空系统身份。
- **control mode**：请求含 `agentgov`。该模式启用 AgentGov 控制面，`agentgov.agent_id` 必填，可使用 HITL、工具时间线、raw SDK 调试和反馈治理投影；`instructions` 同样只作 append-only 追加。
- 其余标准字段在两种模式下形状与含义一致；`agentgov` 不得复制、改名或覆盖已有 OpenAI 标准字段。

当前响应形态：

```json
{
  "id": "resp_run_...",
  "object": "response",
  "status": "completed",
  "model": "claude-sonnet-5",
  "output": [
    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "日报正文..."}]}
  ],
  "usage": {},
  "metadata": {},
  "agentgov": {
    "run_id": "run_...",
    "conversation_id": "conv_sess_...",
    "session_id": "sess_...",
    "sdk_session_id": "sdk_...",
    "agent_id": "main-agent",
    "agent_version_id": "ver_...",
    "trace_id": "trace_...",
    "output_text": "日报正文...",
    "agent_activity": {},
    "stop_reason": "end_turn",
    "errors": []
  }
}
```

口径要点：

- **权威输出在 `output[]`**（`message` → `content[].output_text.text`）。OpenAI 的顶层 `output_text` 是 **SDK-only 便利属性**（聚合 `output[]` 的文本），不在 HTTP wire 响应体里；若 AgentGov 为便利额外下发顶层 `output_text`，须标注为 AgentGov 便利投影、勿当作 OpenAI 标准字段——否则合规 OpenAI SDK 会从 `output[]` 聚合、忽略顶层，读到空文本。
- **`response.id` 由 `run_id` 稳定派生**，形如 `resp_<run_id>`；默认 `store=true` 时支持 `GET /v1/responses/{response_id}` 的最小 retrieve，从现有 `agent_runs` 与 SDK/session 投影重建响应，不新增并行 transcript 存储。
- **最小 retrieve 的保真边界（`agent_runs` 无显式 status 列、run 仅完成时落库）**：只覆盖**已完成 run**；`status` 由 `errors`/`stop_reason` 派生（`completed`/`failed`），**不承诺** `in_progress`/`incomplete`/`cancelled`；回显最小 OpenAI 字段集 `id`/`object`/`created_at`/`status`/`model`/`output`/`usage`/`metadata`（`instructions` 尽力回显，精确 `usage` 因薄投影可能非 1:1，作为已知非一致点纳入契约测试）；`output_text` 权威来源是 `payload_json` 的 `messages[]` 投影，**非**截断的 `answer_summary`。
- **`store=false` 只影响公开 retrieve**：公开 `GET /v1/responses/{id}` 返回 404 或等价不可取回错误，但内部 `agent_runs`、反馈闭环、Langfuse 和审计记录仍照常保留。
- **`previous_response_id` 最小支持**：用于解析上一轮响应所属 conversation；若客户端同时传 `conversation` 且二者不一致，返回 409；若找不到上一轮响应，返回 404。
- **`agentgov` 是顶层私有扩展字段**：只承载 OpenAI 标准字段无法表达的 AgentGov 控制面。无 `agentgov` 的 `/v1/responses` 是 strict mode；已有 OpenAI Chat 客户端也可继续走 `/v1/chat/completions` 备选兼容入口。

### `POST /v1/responses` 流式事件

当前流式语义是 Responses-style SSE，加 AgentGov 私有事件：

| 事件 | 来源/用途 |
| --- | --- |
| `response.created` | 标准事件，返回 `response_id`、`conversation`（保持 wire 纯净，不塞 `run_id` 等私有字段） |
| `response.output_text.delta` | assistant 文本增量 |
| `response.completed` | 最终完成 |
| `response.failed` | 结构化失败 |
| `agentgov.session` | 下发 `run_id/session_id/sdk_session_id/agent_id/agent_version_id` |
| `agentgov.tool_step` | 工具调用/工具结果的规整时间线 |
| `agentgov.confirmation.requested` | HITL 人工确认卡 |
| `agentgov.confirmation.resolved` | HITL 决策结果 |
| `agentgov.result` / `agentgov.error` / `agentgov.done` | control mode 的运行收口事件 |
| `agentgov.sdk_raw` | 仅 verbose/dev 模式下发的 raw SDK envelope |
| SSE comment `: keepalive` | 长运行和长确认等待的连接保活；不进入业务时间线 |

实现契约：

- **当前标准事件最小集**：`response.created`、`response.output_text.delta`、`response.completed`、`response.failed`；strict mode 只接收标准事件，不接 `agentgov.*`。
- **`agentgov.*` 事件使用统一信封** `{v, type, run_id, ts, seq, payload}`：`event:` 行=`type`、`data:` 行=信封 JSON、`id:`=`seq`，并由契约测试覆盖排序和字段隔离；当前不承诺断线 replay。
- **`agentgov.tool_step` 从 runtime message 的 SDK content block 做薄投影**，不另存工具时间线副本。
- **`agentgov.confirmation.requested` 最小 payload**：必须包含 `request_id`、`decision_token`、`request_type`、`run_id`、`conversation_id`、`agent_id`；工具权限请求还必须包含 `tool_name`、`tool_input`、`risk_reason`，AskUserQuestion 请求还必须包含 `question`、`options`。`decision_token` 只在 requested 事件下发，不进入 resolved 事件、历史列表或日志投影。
  - **这些是对外投影字段名，需从现有 record 重命名映射**（`claude_user_input_records.py` public_payload）：`agent_id`←`business_agent_id`、`tool_input`←`input`、`risk_reason`←`risk`(JsonObject)、`conversation_id`←`session_id` 映射，AskUserQuestion 的 `question`/`options`←从 `input` 提取；不是现有字段透传。
- **保活契约（硬约束）**：`heartbeat_interval < client_idle`；`agentgov.session` 下发 `heartbeat_interval_s`，runtime heartbeat 被转换为 SSE comment。客户端应据此派生 idle，不应硬编码固定等待时间，否则慢确认会误超时断流。

HITL 的本质不是 OpenAI `tool_calls`。OpenAI `tool_calls` 是模型建议客户端调用工具；AgentGov HITL 是 Claude Code 服务端工具执行前暂停、等待人类审批、再 resume 同一运行。这个拓扑必须用 `agentgov.*` 扩展表达。

### `/v1/conversations`

当前能力：

- `POST /v1/conversations`：创建会话，返回 `conv_<session_id>`。
- `GET /v1/conversations`：作为 AgentGov 扩展列表接口，支撑前端会话侧栏。
- `GET /v1/conversations/{conversation_id}`：读取会话元数据。
- `DELETE /v1/conversations/{conversation_id}`：删除会话映射。
- `GET /v1/conversations/{conversation_id}/items`：从 SDK transcript 投影 messages、tool calls、tool outputs、thinking/text blocks 等 items，查询参数采用 OpenAI 风格 `after`、`limit`、`order`、`include`。

实现原则：

- 不在后端另建消息副本。
- 不手解析 Claude CLI 内部文件格式。
- 优先复用 claude-agent-sdk session API 与当前 `read_session_history`（`app/runtime/session_history.py`）投影能力。
- `conversation_id` 是对外 ID，内部继续保留现有 `session_id` 与 `sdk_session_id` 的边界。
- **投影前先解析 owning agent**：`conversation_id`→`session_id` 后，须按 `session.agent_id` 经 agent registry 解析 owning agent 的 `workspace_dir`/`claude_config_dir`（复用 `app/routers/sessions.py:_resolve_owning_profile`，backend-owned、无静默 fallback）再定位 SDK transcript，否则取不到历史或误取他 Agent transcript。
- **不暴露旧 offset 契约**：旧 `/api/sessions/{id}/messages?offset=&limit=` 继续作为兼容面；新 `/v1/conversations/{id}/items` 使用 cursor 风格 `after`，避免只是换路径的旧 sessions API。

### `/v1/chat/completions`

保留为兼容包装：

- 支持标准 OpenAI Chat Completions 非流式输出。
- 当前不提供 Chat Completions stream chunks；需要流式的新集成使用 `/v1/responses`。
- 当前保留独立兼容 shim，不暴露 `agentgov.*` 控制事件。
- 不新增 AgentGov 私有控制字段，不把它作为 HITL、会话治理、工具时间线的主入口。

### `/api/chat` 与 `/api/chat/stream`

保留为兼容别名：

- `/api/chat` 对应 `/v1/responses` control mode 的历史非流式包装，保持现有 `agent_id` 必填、无 Web HITL 卡片语义。
- `/api/chat/stream` 对应 `/v1/responses` control mode 的历史流式包装，继续给旧前端/旧集成返回原事件名。
- **兼容不仅保原事件名、还须保原 payload 字段集**：现有 `claude_user_input_required` 携带的 `sdk_subagent_id`、`tool_use_id`、`api_session_id`、`context` 等比新 `confirmation.requested`「最小 payload」丰富；若旧流经新裁剪信封再翻译回旧事件会丢字段，破坏旧前端。迁移时须保全旧字段集，或显式列出允许裁剪项。
- 文档、示例和新集成主推 `/v1/responses` + `/v1/conversations`。
- 旧端点删除不在本文范围。

### HITL decision

当前控制面路径：

```http
POST /v1/agentgov/confirmation-requests/{request_id}/decision
```

安全不变量与择优：

- 保留 `decision_token`，作为 API key 之外的 per-request 授权因子；`request_id`（URL）定位 record、`decision_token` 做 constant-time 校验。校验顺序：record 存在性 → `decision_token` → status/pending，归一 404/token-invalid 措辞。
- **已删除客户端回传的 `run_id/session_id/business_agent_id` 三元组**：它们冗余且不构成第二因子；运行定位仅凭 `request_id`、授权仅凭 `decision_token`；防串扰/防重放由 `request_id→pending` 1:1 + `status` 单次 `waiting→resolved` 达成（若需防 store 错乱由服务端内部 assert，不进对外 body）。
- `extra=forbid`，拒绝 `updated_input`、`allow_modified` 等未设计字段。
- 请求体只包含 `action`、`decision_token`、可选 `answer`、可选 `message`；`allow_once`、`allow_for_run`、`deny`、`answer_question` 语义保留。`answer_question` 应答收敛为**单一顶层 `answer` 对象**，结构化选项用其 `answers` 键，自由文本用其 `response` 键。
- AskUserQuestion 允许用户选择 Claude 给出的选项，也允许输入其他自然语言回答；该回答是用户答复，不等于修改工具参数。

`updated_input` / `allow_modified` 不进入最小契约。若未来支持“批准但修改工具参数”，必须作为单独安全子系统设计：按工具白名单限制可改字段、修改后重跑风险分类、强制一次性授权、记录原始/修改双写审计。

## 落地状态（截至 2026-07-10）

1. **Responses canonical 已落地**：`POST /v1/responses` 已统一非流式 JSON 与流式 SSE，strict/control 两种模式和 Agent 选择边界已进入 OpenAPI 与契约测试。
2. **Response retrieve 已落地**：`GET /v1/responses/{id}` 已实现 `store` 与 `previous_response_id` 的最小语义，并从现有 run/session 事实重建响应。
3. **Conversations 已落地**：`/v1/conversations*` 已提供创建、列表、读取、删除映射和 cursor 风格 items 历史投影。
4. **canonical HITL 已落地**：Responses control SSE 已提供 `agentgov.confirmation.requested/resolved`，decision API 已收敛到 `/v1/agentgov/confirmation-requests/{request_id}/decision` 与最小请求体。
5. **前端主路径已切换**：Playground 使用 `/v1/responses` 流式入口，会话侧栏和历史恢复使用 `/v1/conversations*`，decision 调用使用 canonical `/v1/agentgov/*` 路径。
6. **兼容面继续保留**：`/v1/chat/completions`、`/api/chat`、`/api/chat/stream`、`/api/sessions*` 保持存量契约，并有独立回归覆盖；它们不再接收新控制面能力。
7. **文档权威链已收口**：新集成只读 [AgentGov 集成指南](../AgentGov集成指南.md) 与 OpenAPI；早期 HITL 实现方案及对抗审查计划已归档。旧端点 deprecation/sunset 仍需消费者确认后另行决策，不属于本次已完成迁移。

## 持续验收

后续变更必须持续覆盖：

- `/v1/responses` 非流式：strict/control 两模式均可运行；control 指定 `agentgov.agent_id`；生成 `response_id/run_id/conversation_id/session_id`。
- 标准字段优先级：`instructions`、`conversation`、`previous_response_id` 不被 `agentgov` 平行字段替代；请求包含 `agentgov.instructions`、`agentgov.metadata`、`agentgov.conversation` 等字段时应被 schema 拒绝。
- 字段所有权：`agentgov.alert_id/case_id`（backend-owned 路由）经 adapter **回填 `agent_runs` index 列**，per-agent 反馈匹配不退化；`source/client_run_label` 在 `metadata` 中原样回显、后端不路由。
- `instructions` 语义：strict 模式发送 `instructions` 被 422 拒绝或忽略并回显告警（不静默按 append 生效）；control 模式按 append-only 生效、不替换受治理 prompt。
- `/v1/responses` 流式：普通文本增量、工具时间线、heartbeat、错误事件。
- `GET /v1/responses/{id}`：`store=true` 可取回，`store=false` 不公开取回但内部治理审计仍存在。
- HITL：允许一次、拒绝、本次运行允许、AskUserQuestion 自由文本回答。
- `/v1/conversations/{id}/items`：`after/limit/order/include`、刷新页面后恢复同一会话历史，消息来自 SDK transcript 投影。
- `/v1/chat/completions`：标准 OpenAI 客户端 smoke，确认不泄露 `agentgov.*` 私有事件。
- `/api/chat` 与 `/api/chat/stream`：旧契约继续通过。
- 真实容器 E2E：业务 Agent 选择、Playground 聊天、HITL 卡片、Trace、反馈闭环关联。

## 定位与边界

- 本文是已接受并落地的 Responses-first ADR；当前 wire contract 仍以 OpenAPI 为准。
- `/v1/responses` + `/v1/conversations` 是当前主推接口。
- `/v1/chat/completions` 是兼容接口，不是 AgentGov 控制面主接口。
- `/api/chat` 与 `/api/chat/stream` 保留兼容，不新增主能力。
- AgentGov 后端仍是 Claude Code / claude-agent-sdk 的薄投影层；会话、消息、trace、工具事实以 SDK / agent 为单一真相源。
