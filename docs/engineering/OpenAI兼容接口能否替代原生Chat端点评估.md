# OpenAI 兼容接口能否替代原生 Chat 端点（评估、扩展替代方案与迁移计划）

> 工程决策文档。回答两个问题：
>
> 1. 纯官方标准 `/v1/chat/completions` 能否完全替代 `/api/chat` 与 `/api/chat/stream`？
> 2. 如果允许在 `/v1/chat/completions` 之上增加 AgentGov 私有扩展，能否设计一条完全替代路径？
>
> 契约真相源是 OpenAPI。当前实现以 `app/routers/chat.py`、`app/routers/openai.py`、`app/runtime/schemas.py`、`app/runtime/claude_runtime_stream.py`、`app/routers/settings.py` 为准。人类确认（HITL）机制的权威设计见 [Claude 原生业务Agent人类确认机制整改实现方案](./Claude原生业务Agent人类确认机制整改实现方案.md)。

## 结论

分两层判断：

- **纯 OpenAI 官方标准层**：不能完全替代。`chat/completions` 支持流式和 `tool_calls`，但它表达的是模型响应、标准流式 chunk 和模型生成的工具调用描述；它不表达 AgentGov 当前 `/api/chat/stream` 的服务端 Claude Code 进程内工具审批、授权卡、原地 resume、raw SDK 事件和治理观测链路。
- **AgentGov 扩展层**：可以作为一条完整替代方案，但必须明确这是**基于 OpenAI Chat Completions 外形的 AgentGov 私有控制面协议**。标准客户端仍可按 OpenAI 兼容模式使用；Playground 和上层系统若要替代 `/api/chat`、`/api/chat/stream`，必须 opt in 到 AgentGov 扩展模式并实现确认卡事件与 decision 回调。

因此，推荐的长期方案不是把所有能力伪装成纯标准 OpenAI，而是把 `/v1/chat/completions` 设计成**一个路径、两种模式**：

- `openai-compatible`：严格兼容外部 OpenAI 客户端，不泄露 AgentGov 私有事件。
- `agentgov-control`：在同一路径上启用 AgentGov 私有扩展，完整承载 Playground、HITL、服务端 session、raw SDK 事件和反馈治理字段，作为替代 `/api/chat` 与 `/api/chat/stream` 的目标协议。

## 当前三端点现状

| 能力 | `/api/chat` | `/api/chat/stream` | `/v1/chat/completions` 当前 shim |
| --- | --- | --- | --- |
| 返回形态 | 整包 `ChatResponse` | SSE：`session`/`message`/`result`/`error`/`done` | 整包 `OpenAIChatCompletionResponse` |
| 流式 | 无 | 有 | `stream` 字段保留，当前返回非流式 |
| HITL 人在环工具授权 | 无 | 有：`can_use_tool` + confirmation request + decision API | 无 |
| per-request 选 Agent | 必填 `agent_id` | 必填 `agent_id` | 无 `agent_id`，走运营者配置的 `openai-compat-agent` |
| 消息模型 | 单 `message` + `session_id` | 同左 | `messages[]` 拍平成 prompt |
| 服务端 session | 有 | 有 | 未暴露 AgentGov session 语义 |
| raw SDK 事件 | 不适用 | 有 | 无 |
| 定位 | 原生同步跑指定 Agent | Playground 实时交互与确认卡 | 外部 OpenAI 客户端入口 |

三者共用执行核心：`/api/chat` 与 `/v1/chat/completions` 调 `runtime.run`，`/api/chat/stream` 调 `runtime.stream`。当前 `/v1` 是建在 `runtime.run` 之上的更窄外部壳，不是原生端点的超集。

## 官方标准能承载什么

按官方 OpenAI Chat Completions 语义重评：

| 能力 | 纯标准 Chat Completions 能否承载 | 说明 |
| --- | --- | --- |
| 文本整包返回 | 能 | 标准 `chat.completion` 响应即可承载 |
| 文本流式返回 | 能 | `stream=true` 返回 `chat.completion.chunk` |
| 模型生成工具调用 | 能 | 标准 `tool_calls` 表达模型建议调用的函数工具 |
| per-request 选 Agent | 可约定 | 可用 `model` alias 或标准外的 metadata 约定，但这已经是网关语义 |
| SOC/system 上下文 | 可约定 | 可映射为 `system/developer` message 或 metadata |
| 服务端 session 续接 | 不能无损 | Chat Completions 请求模型主要由客户端提交 messages；AgentGov 的权威 session 在服务端 |
| HITL 工具审批 | 不能无损 | 标准协议没有“服务端执行前暂停、让人审批、同一运行 resume”的双向中途握手 |
| raw SDK 事件与工具 I/O 时间线 | 不能无损 | 标准 chunk 没有 AgentGov raw Claude SDK event envelope |

外部依据：

- OpenAI Chat Completions 官方参考：`https://developers.openai.com/api/reference/resources/chat`
- OpenAI Responses API 官方参考：`https://platform.openai.com/docs/api-reference/responses`

本文只评估 `/v1/chat/completions`。官方 Responses API 有 conversation、background、previous response 等能力，但当前 AgentGov 已把历史 `/v1/responses` 排除在产品契约外；若未来选择 Responses API，需要另立迁移评估，而不是混入本决策。

## 决定性差异：工具调用与 HITL 不是同一个拓扑

OpenAI Chat Completions 的 `tool_calls` 是模型输出的一部分：模型描述“应该调用某个函数及参数”，客户端或上层编排器随后处理，再把工具结果作为后续上下文交给模型。

AgentGov 的 HITL 是服务端 Claude Code agent 正在运行时的权限回调：

1. Claude Code 准备调用 `Read`、`Bash`、`Edit` 等服务端工具。
2. 后端 `can_use_tool` 回调被触发。
3. 后端创建 confirmation request，并通过 SSE 推给前端。
4. 用户在确认卡上选择允许、拒绝、本次运行允许，或回答 `AskUserQuestion`。
5. 后端把 decision 回传给仍在运行的 Claude Code query。
6. 同一个运行继续执行，并保留同一 `run_id`、`session_id`、Langfuse trace、工具 I/O 和治理链路。

这不是字段多少的问题，而是交互拓扑不同。纯标准 Chat Completions 没有“服务端工具执行前挂起并等待外部审批，然后 resume 同一运行”的协议动作。

## 完全替代方案：`/v1/chat/completions` + AgentGov 扩展模式

如果产品目标是最终删除 `/api/chat` 与 `/api/chat/stream`，可采用方案 D：

> 以 `/v1/chat/completions` 作为唯一 Chat 入口。默认保持标准 OpenAI 兼容；当客户端显式 opt in 到 AgentGov 扩展模式时，同一路径额外承载 Agent 选择、服务端 session、HITL confirmation、raw SDK 事件、run metadata 和治理观测字段。

### 设计原则

1. **同一路径，两种模式**
   默认请求走 `openai-compatible`，只返回标准 OpenAI 响应。显式启用扩展后走 `agentgov-control`，允许私有字段和私有 SSE event。

2. **标准字段优先，私有字段命名空间隔离**
   `messages`、`model`、`stream`、`tools`、`tool_calls`、`usage`、`finish_reason` 尽量遵守 OpenAI 形态。AgentGov 字段放入 `metadata.agentgov` 或响应中的 `agentgov` 命名空间，不散落在顶层。

3. **标准客户端不被私有事件破坏**
   未 opt in 的客户端不能收到 `agentgov.*` SSE event，也不能被要求处理 confirmation card。外部 OpenAI SDK 可继续调用 strict 模式。

4. **Playground 使用扩展客户端**
   Playground 不再把自己伪装成普通 OpenAI 客户端，而是使用 `/v1/chat/completions` 的 `agentgov-control` 模式，理解私有事件、decision API 和 raw event envelope。

5. **迁移以服务端 session 为权威**
   AgentGov 仍以 Claude SDK session、`session_id`、`run_id`、Langfuse trace 和反馈闭环为事实源；`messages[]` 只是请求输入外形，不成为治理事实的唯一来源。

### 模式选择

推荐使用请求头显式选择扩展模式：

```http
POST /v1/chat/completions
X-AgentGov-Mode: control
Accept: text/event-stream
Content-Type: application/json
```

请求体同时保留 OpenAI 外形：

```json
{
  "model": "agentgov/main-agent",
  "stream": true,
  "messages": [
    {"role": "user", "content": "帮我生成一份日报"}
  ],
  "metadata": {
    "agentgov": {
      "agent_id": "main-agent",
      "session_id": "sess_...",
      "alert_id": "alert-001",
      "case_id": "case-001",
      "system_append": "只输出日报正文",
      "hitl": {
        "enabled": true,
        "allow_for_run": true
      },
      "trace": {
        "client_run_label": "playground"
      }
    }
  }
}
```

字段口径：

| 字段 | 标准/扩展 | 用途 |
| --- | --- | --- |
| `model` | 标准字段 + AgentGov 约定 | 可继续表示模型，也可采用 `agentgov/{agent_id}` alias 路由业务 Agent |
| `messages` | 标准字段 | 用户输入与上下文外形；服务端仍转换为 Claude Code prompt |
| `stream` | 标准字段 | `false` 替代 `/api/chat`，`true` 替代 `/api/chat/stream` |
| `metadata.agentgov.agent_id` | 私有扩展 | 明确业务 Agent；优先级高于 `model` alias |
| `metadata.agentgov.session_id` | 私有扩展 | 服务端 session 续接 |
| `metadata.agentgov.system_append` | 私有扩展 | 对应当前 `ChatRequest.system_append` |
| `metadata.agentgov.alert_id/case_id` | 私有扩展 | 反馈闭环上下文 |
| `metadata.agentgov.hitl` | 私有扩展 | HITL 开关和运行级授权策略 |

### 非流式替代 `/api/chat`

`stream=false` 且 `X-AgentGov-Mode: control` 时，服务端执行 `runtime.run`，返回兼容 OpenAI 的 `chat.completion`，并在响应中增加 `agentgov` 扩展对象：

```json
{
  "id": "chatcmpl_sess_123",
  "object": "chat.completion",
  "model": "agentgov/main-agent",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "日报正文..."},
      "finish_reason": "stop"
    }
  ],
  "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
  "agentgov": {
    "run_id": "run_...",
    "session_id": "sess_...",
    "sdk_session_id": "sdk_...",
    "agent_id": "main-agent",
    "agent_version_id": "ver_...",
    "agent_activity": {},
    "errors": []
  }
}
```

这样可以覆盖 `/api/chat` 的关键返回字段，同时保留 OpenAI 标准响应主干。

### 流式替代 `/api/chat/stream`

`stream=true` 且 `X-AgentGov-Mode: control` 时，仍使用 SSE，但分两类事件：

1. 标准 OpenAI chunk：事件名可保持默认 `message` 或不写 `event`，`data` 是 `chat.completion.chunk`。
2. AgentGov 私有事件：事件名使用 `agentgov.*`，`data` 放 AgentGov 控制面 payload。

事件建议：

| 事件 | 替代当前事件 | 作用 |
| --- | --- | --- |
| `agentgov.session` | `session` | 下发 `session_id`、`sdk_session_id`、`run_id`、`agent_id` |
| 标准 chunk | `message.text` | 下发 assistant 文本增量 |
| `agentgov.sdk_message` | `message.raw` | 下发 raw Claude SDK 投影，用于工具时间线、thinking、debug |
| `agentgov.tool_timeline` | `message.raw` 的 UI 投影 | 下发已规整的工具使用、工具结果、耗时、风险标签 |
| `agentgov.confirmation.requested` | HITL 卡片 | 要求前端展示人工确认卡 |
| `agentgov.confirmation.resolved` | HITL 结果 | 回显允许、拒绝、本次运行允许、自然语言回答 |
| `agentgov.result` | `result` | 下发最终 `ChatResponse` 等价扩展结果 |
| `agentgov.error` | `error` | 下发结构化错误 |
| `agentgov.done` | `done` | 结束流 |

示例：

```text
event: agentgov.session
data: {"run_id":"run_1","session_id":"sess_1","sdk_session_id":"sdk_1","agent_id":"main-agent"}

data: {"id":"chatcmpl_sess_1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"日报"},"finish_reason":null}]}

event: agentgov.confirmation.requested
data: {"request_id":"uir_1","tool_name":"Bash","input":{"command":"printf hitl"},"actions":["allow_once","allow_for_run","deny"]}

event: agentgov.result
data: {"agentgov":{"run_id":"run_1","session_id":"sess_1","errors":[]}}

event: agentgov.done
data: [DONE]
```

标准客户端只使用 strict 模式，不会收到这些私有事件。Playground 和上层 AgentGov 集成客户端使用 control 模式，必须处理 `agentgov.confirmation.requested`。

### HITL decision API

为了保持同一运行 resume，decision 仍需要独立回调端点。路径可保留现有端点，也可迁移到 `/v1` 命名空间：

```http
POST /v1/agentgov/confirmation-requests/{request_id}/decision
```

请求体：

```json
{
  "action": "allow_once",
  "message": "允许本次执行",
  "updated_input": null
}
```

`AskUserQuestion` 的自然语言回答仍属于用户选择/回答，不等于让用户修改底层工具参数。工具参数修改只能由后端受控字段表达，并继续受 `allow_modified` 等安全策略约束。

### 服务端 session 与 messages 的关系

替代方案不能把治理事实完全搬到客户端 `messages[]`。推荐规则：

1. `metadata.agentgov.session_id` 存在时，服务端按该 session resume。
2. `messages[]` 中最新 user message 是本次输入；历史 messages 可作为补充上下文，但不是 transcript 权威源。
3. 服务端返回的 `agentgov.session_id` 是后续请求的续接依据。
4. `run_id`、`sdk_session_id`、Langfuse trace id、feedback context 仍由服务端生成和投影。

### Agent 选择

推荐优先级：

1. `metadata.agentgov.agent_id`
2. `model` alias：`agentgov/{agent_id}`
3. 运营者配置的 `openai-compat-agent`
4. 默认 main

strict 模式只允许第 3、4 种，避免普通 OpenAI 客户端误以为 `model` 是底层 LLM 模型却实际路由业务 Agent。control 模式允许第 1、2 种。

### 错误与兼容策略

| 场景 | strict 模式 | control 模式 |
| --- | --- | --- |
| `stream=false` | 返回标准 `chat.completion` | 返回标准主干 + `agentgov` 扩展 |
| `stream=true` | 只返回标准 chunk | 返回标准 chunk + `agentgov.*` 私有事件 |
| 缺少 `agent_id` | 使用配置的出口 Agent | 422，要求显式 agent 或使用配置默认，具体由产品决定 |
| HITL 触发 | 不启用 HITL，按服务端安全策略拒绝或 fail-fast | 发 `agentgov.confirmation.requested` 并等待 decision |
| 客户端不处理 confirmation | 不会收到 | 超时后按拒绝或配置策略失败 |
| unknown 私有字段 | 忽略 | 严格校验 `metadata.agentgov`，未知字段可 422 |

## 迁移计划

### 阶段 0：契约冻结

- 明确 `/v1/chat/completions` 支持 `openai-compatible` 和 `agentgov-control` 两种模式。
- 在 OpenAPI 中表达扩展字段、私有 SSE event 和 decision API。
- 明确 `/v1/responses` 不纳入本方案；如未来启用，另立评估。

验收：

- OpenAPI 包含 `/v1/chat/completions` control mode 扩展 schema。
- strict 模式快照保持兼容，现有 OpenAI 客户端 smoke 不受影响。

### 阶段 1：实现非流式 control mode

- `/v1/chat/completions` 在 `stream=false + X-AgentGov-Mode: control` 下接受 `metadata.agentgov`。
- 映射到当前 `ChatRequest`，复用 `resolve_business_profile` 和 `runtime.run`。
- 响应新增 `agentgov` 扩展，覆盖当前 `ChatResponse` 字段。

验收：

- `/api/chat` 与 `/v1/chat/completions` control non-stream 在同一输入下产生等价 `run_id/session_id/answer/errors`。
- 旧 `/v1` strict 模式仍只返回标准 OpenAI 形态。

### 阶段 2：实现流式 control mode

- `/v1/chat/completions` 在 `stream=true + X-AgentGov-Mode: control` 下走 `runtime.stream`。
- 标准文本增量投影为 `chat.completion.chunk`。
- 当前 `session/message/result/error/done` 事件迁移为 `agentgov.*` 事件。
- raw SDK event 与 UI 工具时间线同时保留：raw 用于调试，timeline 用于稳定 UI。

验收：

- Playground 使用新端点完成普通对话、工具时间线展示、HITL 卡片展示、允许/拒绝/本次运行允许、超时错误展示。
- Langfuse trace、`run_id`、`session_id` 与旧 `/api/chat/stream` 等价。

### 阶段 3：迁移 Playground 与集成文档

- 前端 `streamChat()` 改为调用 `/v1/chat/completions` control mode。
- 统一确认卡集成文档：上层系统调用 `/v1/chat/completions` control stream 时，监听 `agentgov.confirmation.requested` 并调用 decision API。
- 保留旧 `/api/chat/stream` 为 deprecated alias，返回响应头提示迁移。

验收：

- Playground 主流程只走 `/v1/chat/completions`。
- 浏览器网络面板不再出现 `/api/chat/stream`。
- 集成指南给出 strict 模式和 control 模式两个最小样例。

### 阶段 4：删除原生端点

删除条件：

- 真实容器端到端验收通过：普通对话、HITL、AskUserQuestion、session resume、agent 切换、trace、反馈闭环均通过。
- OpenAPI 和前端生成类型已经删除 `/api/chat`、`/api/chat/stream`。
- `tests/coverage_policy.json` 已把主流程绑定到新端点。
- 运行日志和代码搜索确认无活跃调用方。

删除内容：

- `app/routers/chat.py` 中 `/api/chat`、`/api/chat/stream` 路由。
- 前端旧 `streamChat()` 原生路径分支。
- 旧原生端点测试，迁移为 `/v1/chat/completions` control mode 测试。

验收：

- `make main-flow-test`
- `make test`
- 真实容器功能 + 效果验收
- OpenAPI legacy path 断言：`/api/chat`、`/api/chat/stream` 不存在

## 风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 污染 OpenAI 兼容面 | 普通 OpenAI 客户端收到私有事件后解析失败 | strict/control 模式硬隔离；私有事件只在 opt in 时发送 |
| `model` 路由混淆 | 用户以为选择 LLM model，实际选择业务 Agent | control 模式优先 `metadata.agentgov.agent_id`，`model` alias 只作为显式约定 |
| 前端确认卡耦合协议细节 | 后续事件变更导致 UI 脆弱 | 提供稳定 `agentgov.confirmation.*` 与 `agentgov.tool_timeline`，raw SDK event 仅作调试 |
| session 双轨 | 客户端 messages 与服务端 transcript 不一致 | 服务端 session 为权威，messages 只作为输入外形 |
| 删除过早 | 上层系统仍依赖原生端点 | 先 deprecated alias + telemetry，再删除 |
| OpenAPI 表达 SSE 私有事件困难 | 类型生成不完整 | 使用明确的 event envelope schema，并补手写前端类型或生成扩展类型 |

## 最终推荐

短期仍推荐保留三端点，因为当前 `/v1` 只是外部 shim，强行删除会破坏 Playground 与 HITL。

如果产品目标明确是“统一到 `/v1/chat/completions` 并删除原生端点”，推荐采用方案 D，而不是继续讨论纯标准 OpenAI 是否足够：

- strict 模式保持真正的 OpenAI 兼容出口。
- control 模式作为 AgentGov 私有扩展，完整替代 `/api/chat` 与 `/api/chat/stream`。
- 完成四阶段迁移和真实容器验收后，再删除原生端点。

这条路技术上可行，但它不是“纯官方标准完全替代”，而是“以官方 Chat Completions 外形为公共入口，以 AgentGov 扩展协议承载治理控制面”。

## 附录：契约真相源与验证

- 契约真相源：OpenAPI（`scripts/export_openapi.py` 导出；`tests/test_openapi_export.py`、`tests/test_documentation_contracts.py` 守端点存在性）。
- 关键代码锚点：`app/routers/chat.py`（当前原生两端点）、`app/routers/openai.py`（当前 `/v1` 出口）、`app/runtime/schemas.py`（`ChatRequest` vs `OpenAIChatCompletionRequest`）、`app/runtime/claude_runtime_stream.py`（HITL：`web_hitl`/`create_and_wait`/`can_use_tool` 与事件流）。
- 复核方式：`rg -n '/api/chat/stream' frontend/src app tests docs` 确认旧路径依赖；`rg -n 'OpenAIChatCompletionRequest|OpenAIChatCompletionResponse' app tests frontend/src` 确认 `/v1` 契约；真实容器中用 Playground 验证 control stream、确认卡、trace 与 session resume。
