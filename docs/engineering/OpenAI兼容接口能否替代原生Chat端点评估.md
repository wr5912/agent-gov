# OpenAI 兼容接口能否替代原生 Chat 端点（Responses-first 评估与目标契约）

> 工程决策文档。本文只定义接口演进方向与目标契约，不表示当前代码已经完成实现。
>
> 当前结论：AgentGov 后续应采用 OpenAI **Responses API 系列接口**作为主路径，首推 `POST /v1/responses` + `/v1/conversations`；`/v1/chat/completions`、`/api/chat`、`/api/chat/stream` 只保留兼容。
>
> **口径变更声明**：本文的 canonical 由早期版本的 `/v1/chat/completions` 改为 **Responses-first**（`/v1/responses` + `/v1/conversations`），已经使用方确认；理由见「标准接口取舍」。此前「chat/completions 为 canonical」的口径作废。
>
> 契约真相源是 OpenAPI；现状事实以 `app/routers/chat.py`、`app/routers/openai.py`、`app/routers/sessions.py`、`app/routers/claude_user_input.py`、`app/runtime/schemas.py`、`app/runtime/session_history.py`、`app/runtime/claude_user_input_schemas.py`、`app/runtime/claude_runtime_stream.py` 为准。HITL 权威设计见 [Claude 原生业务Agent人类确认机制整改实现方案](./Claude原生业务Agent人类确认机制整改实现方案.md)。

## 结论

- **主推入口**：`POST /v1/responses`。它承载一次 AgentGov 业务 Agent 运行，覆盖非流式、流式、HITL 人工确认、工具时间线、Trace、`run_id`、`session_id` 与反馈治理上下文。
- **会话入口**：`/v1/conversations` 系列。它承载会话创建、恢复、删除与历史读取；前端恢复会话历史应走 `/v1/conversations/{conversation_id}/items`，而不是继续扩展 `/api/sessions*`。
- **兼容入口**：`/v1/chat/completions` 只面向已有 OpenAI Chat Completions 客户端，作为兼容包装；`/api/chat` 与 `/api/chat/stream` 只作为历史兼容面保留。
- **不删除旧端点**：本文不安排删除 `/api/chat`、`/api/chat/stream` 或 `/v1/chat/completions`。删除只能在新 `/v1` 能力对等、消费者矩阵确认、真实容器端到端验收之后另行评估。

官方依据：

- OpenAI 文档说明 Responses 是 Chat Completions 的演进，Chat Completions 仍支持，但 Responses 推荐用于新项目：<https://developers.openai.com/api/docs/guides/migrate-to-responses>。
- Conversation state 文档说明 Conversations API 与 Responses API 配套，用长期对象持久化会话状态，并存储 messages、tool calls、tool outputs 等 items：<https://developers.openai.com/api/docs/guides/conversation-state>。
- Conversations API 参考提供 create/retrieve/update/delete conversation 与 items create/retrieve/delete/list 等资源：<https://developers.openai.com/api/reference/resources/conversations/methods/create>。
- Chat Completions 的 `GET /chat/completions/{completion_id}/messages` 只读取 stored chat completion 的消息，不等价于本项目前端需要的业务会话历史恢复：<https://developers.openai.com/api/reference/resources/chat/subresources/completions/subresources/messages/methods/list/>。

## 当前实现现状

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

当前 `/api/chat/stream` 的事件包括 `session`、`message`、`result`、`error`、`done`、`heartbeat`、`claude_user_input_required`、`claude_user_input_resolved`。这些事件是 AgentGov 控制面事件，不应继续塞进 Chat Completions 兼容流；应迁移为 Responses-style SSE + `agentgov.*` 扩展事件。

当前 `/v1/chat/completions` 只是 `runtime.run` 外的一层非流式 shim，不是 `/api/chat` 与 `/api/chat/stream` 的超集。因此，继续把它定义成 canonical 会把未来控制面建在错误抽象上。

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
- 业务 Agent 选择、SOC 上下文、HITL、反馈闭环字段统一放入 `agentgov` 扩展对象。
- Chat Completions 的 stored messages API 是 completion 维度，不是 long-running conversation 维度；前端会话恢复应以 Conversations Items 为目标契约。
- 领域 API 不强行伪装成 OpenAI API。反馈优化、Agent job、Trace 查询等仍保留产品领域接口，但可在结果中关联 `response_id`、`conversation_id`、`trace_id`。

## 目标契约

### `POST /v1/responses`

目标请求形态：

```json
{
  "model": "claude-sonnet-5",
  "input": "帮我生成一份日报",
  "stream": true,
  "conversation": "conv_sess_...",
  "metadata": {"source": "playground"},
  "agentgov": {
    "agent_id": "main-agent",
    "alert_id": "alert-001",
    "case_id": "case-001",
    "system_append": "只输出日报正文",
    "max_turns": 8,
    "hitl": {"enabled": true}
  }
}
```

字段口径：

| 字段 | 归属 | 说明 |
| --- | --- | --- |
| `model` | OpenAI 标准 | 只作模型 override，不承载 Agent 路由 |
| `input` | OpenAI 标准 | 字符串或 items；服务端转为 Claude Code 本轮 prompt |
| `stream` | OpenAI 标准 | `false` 对等兼容 `/api/chat`，`true` 对等兼容 `/api/chat/stream` |
| `conversation` | OpenAI 标准方向 | 指向 `conv_*` 会话；服务端映射到 AgentGov `session_id` / SDK resume id |
| `metadata` | OpenAI 标准 | 只作扁平观测元数据，不承载控制面 |
| `agentgov.agent_id` | AgentGov 扩展 | 控制模式必填；缺失硬 422，不静默跑 main |
| `agentgov.alert_id` / `case_id` | AgentGov 扩展 | 反馈闭环上下文 |
| `agentgov.system_append` | AgentGov 扩展 | 对应现有 `ChatRequest.system_append` |
| `agentgov.max_turns` | AgentGov 扩展 | 对应现有 `ChatRequest.max_turns` |
| `agentgov.hitl` | AgentGov 扩展 | HITL 开关与运行级策略 |

> `input` 为 items 数组时，服务端取其文本/内容映射为本轮 Claude Code prompt。**刻意不映射 OpenAI 标准 `instructions` 参数**：AgentGov 的 system 身份是 Claude Code preset + workspace CLAUDE.md（治理态单一真相源），`instructions` 的 replace 语义会让客户端覆盖受治理 prompt；故 system 追加只经 `agentgov.system_append`（append 到 preset，见 `app/runtime/claude_runtime.py`）。

目标响应形态：

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
    "agent_activity": {},
    "stop_reason": "end_turn",
    "errors": []
  }
}
```

口径要点：

- **权威输出在 `output[]`**（`message` → `content[].output_text.text`）。OpenAI 的顶层 `output_text` 是 **SDK-only 便利属性**（聚合 `output[]` 的文本），不在 HTTP wire 响应体里；若 AgentGov 为便利额外下发顶层 `output_text`，须标注为 AgentGov 便利投影、勿当作 OpenAI 标准字段——否则合规 OpenAI SDK 会从 `output[]` 聚合、忽略顶层，读到空文本。
- **`response.id` 由 `run_id` 稳定派生**，`store=false` 语义（不另建并行存储）；因此 **不支持** `GET /v1/responses/{id}` retrieve 与 `previous_response_id` 链式（这两者依赖 `store=true` 服务端持久化）。上下文延续统一走 `conversation`，会话历史以 Claude SDK / agent transcript 为单一真相源。
- **`agentgov` 是顶层私有扩展字段**：带该字段的 `/v1/responses` 面向 AgentGov control 客户端；纯 OpenAI 客户端应走 `/v1/chat/completions` 兼容入口（strict SDK 可能拒未知顶层字段）。

### `POST /v1/responses` 流式事件

目标流式语义是 Responses-style SSE，加 AgentGov 私有事件：

| 事件 | 来源/用途 |
| --- | --- |
| `response.created` | 创建运行，返回 `response_id`、`conversation_id`、`run_id` |
| `response.output_text.delta` | assistant 文本增量 |
| `response.completed` | 最终完成 |
| `response.failed` | 结构化失败 |
| `agentgov.session` | 下发 `run_id/session_id/sdk_session_id/agent_id/agent_version_id` |
| `agentgov.tool_step` | 工具调用/工具结果的规整时间线 |
| `agentgov.confirmation.requested` | HITL 人工确认卡 |
| `agentgov.confirmation.resolved` | HITL 决策结果 |
| `agentgov.heartbeat` | 长运行和长确认等待的保活 |
| `agentgov.sdk_raw` | 仅 verbose/dev 模式下发的 raw SDK envelope |

实现契约（勿在落地时丢失）：

- **标准 Responses 生命周期事件按官方语义**：除上列外还含 `response.in_progress`、`response.output_item.added/.done` 等；纯 OpenAI 客户端走兼容入口、不接 `agentgov.*`。
- **`agentgov.*` 事件用统一信封** `{v, type, run_id, ts, seq}`：`event:` 行=`type`、`data:` 行=信封 JSON、`id:`=`seq`（排序/去重；断线 replay 暂 defer）。SSE 事件今天不在 OpenAPI、前端靠手写类型——借迁移把 event envelope 固化为显式 schema 并纳入契约测试。
- **`agentgov.tool_step` 复用现有 activity_extractor 投影**（工具时间线单一来源，勿另造第二份工具语义）。
- **保活契约（硬约束）**：`heartbeat_interval < client_idle`（现状 15s < 180s，`claude_runtime_stream.py:258` / `runtime.ts:45`）；HITL 决策超时 300s 远大于前端 idle，长确认等待全靠 heartbeat 维持连接——client 应从 `agentgov.session` 下发的 `heartbeat_interval_s` **派生 idle、不硬编码 180**，否则慢确认会误超时断流。

HITL 的本质不是 OpenAI `tool_calls`。OpenAI `tool_calls` 是模型建议客户端调用工具；AgentGov HITL 是 Claude Code 服务端工具执行前暂停、等待人类审批、再 resume 同一运行。这个拓扑必须用 `agentgov.*` 扩展表达。

### `/v1/conversations`

目标能力：

- `POST /v1/conversations`：创建会话，返回 `conv_<session_id>`。
- `GET /v1/conversations`：作为 AgentGov 扩展列表接口，支撑前端会话侧栏。
- `GET /v1/conversations/{conversation_id}`：读取会话元数据。
- `DELETE /v1/conversations/{conversation_id}`：删除会话映射。
- `GET /v1/conversations/{conversation_id}/items`：从 SDK transcript 投影 messages、tool calls、tool outputs、thinking/text blocks 等 items。

实现原则：

- 不在后端另建消息副本。
- 不手解析 Claude CLI 内部文件格式。
- 优先复用 claude-agent-sdk session API 与当前 `read_session_history`（`app/runtime/session_history.py`）投影能力。
- `conversation_id` 是对外 ID，内部继续保留现有 `session_id` 与 `sdk_session_id` 的边界。
- **投影前先解析 owning agent**：`conversation_id`→`session_id` 后，须按 `session.agent_id` 经 agent registry 解析 owning agent 的 `workspace_dir`/`claude_config_dir`（复用 `app/routers/sessions.py:_resolve_owning_profile`，backend-owned、无静默 fallback）再定位 SDK transcript，否则取不到历史或误取他 Agent transcript。

### `/v1/chat/completions`

保留为兼容包装：

- 支持标准 OpenAI Chat Completions 非流式输出。
- 可补齐标准 chat stream chunks，便于现有 OpenAI SDK 客户端复用。
- 内部可映射到 `/v1/responses` adapter，但不暴露 `agentgov.*` 控制事件。
- 不新增 AgentGov 私有控制字段，不把它作为 HITL、会话治理、工具时间线的主入口。

### `/api/chat` 与 `/api/chat/stream`

保留为兼容别名：

- `/api/chat` 对应 `/v1/responses stream=false` 的历史包装，保持非流式、无 Web HITL 卡片语义。
- `/api/chat/stream` 对应 `/v1/responses stream=true` 的历史包装，继续给旧前端/旧集成返回原事件名。
- 文档、示例和新集成主推 `/v1/responses` + `/v1/conversations`。
- 旧端点删除不在本文范围。

### HITL decision

目标控制面路径：

```http
POST /v1/agentgov/confirmation-requests/{request_id}/decision
```

安全不变量与择优：

- 保留 `decision_token`，作为 API key 之外的 per-request 授权因子；`request_id`（URL）定位 record、`decision_token` 做 constant-time 校验。校验顺序：record 存在性 → `decision_token` → status/pending，归一 404/token-invalid 措辞。
- **删除客户端回传的 `run_id/session_id/business_agent_id` 三元组**（相对当前代码简化）：冗余、可经 GET list 公开读取、不构成第二因子；运行定位仅凭 `request_id`、授权仅凭 `decision_token`；防串扰/防重放由 `request_id→pending` 1:1 + `status` 单次 `waiting→resolved` 达成（若需防 store 错乱由服务端内部 assert，不进对外 body）。
- `extra=forbid`，拒绝 `updated_input`、`allow_modified` 等未设计字段。
- `allow_once`、`allow_for_run`、`deny`、`answer_question` 语义保留；`answer_question` 应答收敛为**单一 `answer` 字段**（镜像 SDK AskUserQuestion 答案 schema），勿保留 `answers`+`response` 双平行字段。
- AskUserQuestion 允许用户选择 Claude 给出的选项，也允许输入其他自然语言回答；该回答是用户答复，不等于修改工具参数。

`updated_input` / `allow_modified` 不进入最小契约。若未来支持“批准但修改工具参数”，必须作为单独安全子系统设计：按工具白名单限制可改字段、修改后重跑风险分类、强制一次性授权、记录原始/修改双写审计。

## 迁移路线图

1. **文档契约冻结**：以本文为准，把目标从 Chat-Completions-canonical 改为 Responses-first；旧文档、集成指南和示例后续逐步同步。
2. **新增 `/v1/responses`**：先实现非流式，再实现流式；复用 `ClaudeRuntime.run/stream`、Agent profile resolver、session store、Langfuse trace 投影。
3. **新增 `/v1/conversations`**：把会话列表、会话删除、历史读取从 `/api/sessions*` 迁移为 Conversations/Items 契约。
4. **前端主路径切换**：Playground 调 `/v1/responses stream=true`，会话侧栏与历史恢复调 `/v1/conversations*`。
5. **兼容层回归**：`/v1/chat/completions`、`/api/chat`、`/api/chat/stream` 全部走新 adapter 或等价逻辑，保证旧调用方不破。
6. **弃用评估另行立项**：只有新主路径真实容器验收通过，并确认外部消费者迁移后，才评估是否给旧端点加 deprecation/sunset 策略。

## 验收建议

文档后续落地实现时，至少覆盖：

- `/v1/responses` 非流式：指定 `agentgov.agent_id`、生成 `response_id/run_id/conversation_id/session_id`。
- `/v1/responses` 流式：普通文本增量、工具时间线、heartbeat、错误事件。
- HITL：允许一次、拒绝、本次运行允许、AskUserQuestion 自由文本回答。
- `/v1/conversations/{id}/items`：刷新页面后恢复同一会话历史，消息来自 SDK transcript 投影。
- `/v1/chat/completions`：标准 OpenAI 客户端 smoke，确认不泄露 `agentgov.*` 私有事件。
- `/api/chat` 与 `/api/chat/stream`：旧契约继续通过。
- 真实容器 E2E：业务 Agent 选择、Playground 聊天、HITL 卡片、Trace、反馈闭环关联。

## 定位与边界

- 本文是目标契约，不代表当前代码已完成。
- `/v1/responses` + `/v1/conversations` 是未来主推接口。
- `/v1/chat/completions` 是兼容接口，不是 AgentGov 控制面主接口。
- `/api/chat` 与 `/api/chat/stream` 保留兼容，不新增主能力。
- AgentGov 后端仍是 Claude Code / claude-agent-sdk 的薄投影层；会话、消息、trace、工具事实以 SDK / agent 为单一真相源。
