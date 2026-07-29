# AgentGov 集成指南

> 文档角色：面向**上层业务系统**集成 AgentGov 的权威集成参考（接口与集成层）。AgentGov 是 agent 运行治理底座，通常被上层业务系统（如 SOC 平台、客服平台、运维平台）集成，对外只暴露 HTTP API。
>
> 契约单一真相源：**OpenAPI**（运行容器的 `/openapi.json` 与 `/docs`）。本指南**不复制** request/response schema，只讲 OpenAPI 给不了的东西：集成旅程、认证、错误语义、边界归属、稳定性与反模式。涉及具体字段时请以 OpenAPI 为准。
>
> 术语以 [AgentGov术语与版本边界](./AgentGov术语与版本边界.md) 为准；产品定位以 [项目目标愿景使命](./项目目标愿景使命.md) 为准。

## 1. AgentGov 是什么 / 不是什么

AgentGov 是**智能体治理平台**，对外作为 agent 运行治理底座，负责被治理 Agent 的运行（Runtime）、反馈闭环（Feedback Loop）和版本治理（Version Governance），并把运行、反馈、归因、优化、评估、发布沉淀为数据资产、方法论资产和执行资产。

| 维度 | AgentGov 底座负责 | 上层业务系统负责 |
| --- | --- | --- |
| 运行 | 跑所有注册业务 Agent（含 `main-agent`），产出 run、session、trace | 决定何时跑、传入业务上下文、承载业务工作流 |
| 会话 | 持有会话事实（SDK session transcript 为权威源）、按 `conversation_id` 投影历史 | 展示对话、回放气泡、组织业务级会话 |
| 反馈闭环 | 归因、优化、评估、回归（治理 Agent + DSPy，底座内部） | 采集用户反馈并提交、在确认门上做业务决策 |
| 版本治理 | change set / release / 回滚 / 审计 | 触发发布、按业务规则决定是否发布 |
| 审批 | 记录 operator/reason/审计事件 | **高风险动作的人审批**（划归外部业务系统，见 §6） |
| 资产 | 沉淀与跨 Agent 复用 | 消费资产、按场景复用 |

AgentGov **不**提供通用协作看板、不替代协作平台、不承载上层的领域 UI 与审批；治理 Agent 是底座内部工具，**不**对上层暴露为可编排对象。

## 2. 集成前提与约定

- **部署形态**：Docker 容器对外提供 HTTP API，供 Web UI、业务系统、Agent 平台控制面调用。**Base URL 由部署方提供**：外部 / 同主机默认 `http://<host>:58080`（宿主暴露端口 `HOST_PORT`；默认遵循 `50000 + 容器端口`；本机调试常见 `http://localhost:58080`），同 Docker 网络内的服务用 `http://claude-agent-api:8080`；容器内 app 端口是 `8080`，生产可能在反向代理 / TLS 之后。
- **认证**：所有 `/api/*` 与 `/v1/*` 走 `Authorization: Bearer <API_KEY>`。`API_KEY` 为空表示不鉴权（仅限可信内网）；配置后缺失/错误 token 返回 `401`。
- **错误语义**：`400/422` 入参非法；`401` 未鉴权；`403` 权限拒绝；`404` 资源不存在；`409` 状态冲突（如重复创建、并发发布）；`413/415` payload 大小或编码不符合要求；`500` 服务端/数据完整性异常；`501` 当前宿主平台不支持请求的原生 Runtime 捕获；`503` 运行时、模型或配置的出口 Agent 暂不可用。路由主动抛出的 HTTP / 领域错误统一返回 `{detail, error_code}`，领域错误可能带额外诊断字段；FastAPI 请求体验证失败的 `422` 仍可能是标准 validation error 形态。失败即报错，不静默降级为 offline/raw 结果。
- **离线不变量**：底座不依赖公网远程服务；模型经 `MODEL_PROVIDER_BACKEND` 显式选择的本地/内网网关接入。集成方不应假设任何公网回调。
- **契约真相源**：以容器 `/openapi.json`、`/docs` 为准；前端/客户端类型应由 OpenAPI 生成，不要绕过 OpenAPI 自造 schema（见 §6）。OpenAPI `info.version` 即 AgentGov 的发布版本（与 git release tag、docker 镜像 tag 同源于仓库根 `VERSION`），可据此判断对接的是哪个 release。

## 3. 概念模型与所有权

```
业务Agent ──运行──▶ session / run ──反馈──▶ feedback-case ──归因/优化/评估──▶ change set / release ──沉淀──▶ asset registry
```

- **Agent**：集成方编排的是**业务 Agent**（被治理的长期对象，经 `/api/agent-registry` 查询）。`security-operations-expert` 是当前唯一内置、默认且受保护的业务 Agent，这三个属性彼此独立；`main-agent` 只是普通历史示例。治理 Agent `governor` 治理所有业务 Agent，不对集成方暴露为可编排对象。
- **conversation**：新集成使用 `conversation_id`（`conv_*`）作为对外会话标识，通过 `/v1/conversations` 创建、查询和恢复。过渡 Responses control 响应中的 `agentgov.session_id` 是 AgentGov 内部关联 ID，`sdk_session_id` 是更底层的 SDK resume id；两者都不应替代 `conversation_id` 作为新集成的会话 URL 参数。
- **run**：一次运行，带 `run_id`，是反馈与归因的归属锚点。
- **会话正文**：权威源是 agent 自己的 SDK session transcript；底座按需投影，**不另存副本**。集成方也不应缓存一份并行会话存储（会双轨漂移）。
- **所有权（重要）**：
  - `agent_id`（会话归属哪个 Agent）是 **backend-owned**：新会话首次运行时由底座原子绑定，绑定后不可变。`conversation` / `previous_response_id` 只能由同一业务 Agent 续接，跨 Agent 请求返回 `409`。**集成方读历史时不传 `agent_id`**，只凭 `conversation_id`（见 §4.3、§6）。
  - 反馈/评估/版本仍可使用 Responses control 扩展返回的 `run_id` / `session_id` / `agent_id` 关联到对应 Agent 与 version。

## 4. 集成旅程（任务式）

> 每个旅程给“目标 + 最短路径（调用哪些 operation）+ 边界提示”。具体字段、状态码、schema 以 OpenAPI 对应 tag 为准。

### 4.1 选择 / 创建业务 Agent — OpenAPI tag `agents`
- 目标：拿到要运行的业务 Agent。
- 最短路径：`GET /api/agent-registry` 列出。创建普通业务 Agent 时，先准备包含单个顶层 `workspace/` 的 `.tar.gz`，再以 `multipart/form-data` 调用 `POST /api/agent-registry/{agent_id}/workspace/import`；新 ID 必须同时提供非空 `name`。平台不提供直接创建 API、模板列表或来源选择字段。`POST /api/agent-registry/{agent_id}/lifecycle` 切换生命周期；`GET /api/agents`、`GET /api/skills` 查能力目录。
- 创建一致性：Workspace 包只有在安全解包、Workspace 落盘、Git 初始化并完成注册表 finalize 后才返回成功；内部 `provisioning` reservation 不会被 list/get/chat 看见。文件或 finalize 失败会补偿注册表并只清理本次创建的文件；路径穿越、symlink、特殊 tar 成员、重复冲突、`.git`、资源超限与已知 JSON 语法错误均 fail closed。reservation 使用 heartbeat 租约；重启恢复只处理已过期任务，不抢占仍在落盘的创建。
- 边界：`security-operations-expert` 在空运行卷中由运行卷初始化源提供，无需在线创建。SDK-native 请求通过顶层 `agent_id` 选择任一注册业务 Agent；过渡 `/v1/responses` control mode 通过 `agentgov.agent_id` 选择，缺失返回 `422`。未知或不可运行 Agent 返回对应领域错误。

### 4.2 运行一次对话 — OpenAPI tag `claude-sdk-events` / `openai-responses`
- 目标：让 Agent 处理一条消息/任务。
- 受管运行事实源：`POST /api/agent-runtime/sdk-events`。第一方 Playground 已直接消费该入口；后续独立 OpenAI 适配服务也应从这条 SDK-native 流转换，避免把平台内的过渡 projector 当成长期协议核心。
- 过渡 OpenAI 风格投影：`POST /v1/responses`。同一个 endpoint 通过 `stream` 选择非流式 JSON 或流式 SSE，但 operation 已以 `x-agentgov-contract-status: transitional` 标记，不能宣称是完整 OpenAI Responses drop-in replacement：
  - **control mode（AgentGov 集成首选）**：请求包含 `agentgov`，且 `agentgov.agent_id` 必填；可同时传标准字段 `conversation`，以及 `agentgov.alert_id`、`agentgov.case_id`、`agentgov.max_turns` 等 OpenAPI 已声明扩展字段。
  - **strict mode（标准 OpenAI 客户端）**：请求不含 `agentgov`，运行运营者配置的 OpenAI-compatible 出口 Agent；不下发 `agentgov.*` 私有 SSE 事件。由于 AgentGov 的 `instructions` 是 append-only 而非 OpenAI replace/swap 语义，strict mode 传 `instructions` 返回 `422`。
- Swagger UI 的 operation 顶部若显示 `Parameters → No parameters`，只表示该接口没有
  path/query/header/cookie 参数；JSON 字段仍完整位于 `Request body`。Responses 的 operation
  description 会扁平展开 22 个递归字段，请求体下拉框提供
  `agentgov_control_stream`、`agentgov_control_structured`、`strict_openai`、
  `continue_with_conversation`、`continue_with_previous_response_id` 五个具名示例，分别覆盖
  全 control 开关（含 `with_speech_summary`/debug）、两种结构化 content、strict 和两种推荐续聊形式。
  字段/参数示例不再使用 Pydantic 自动生成的 `null`、`string`、随机串或 `additionalProp*`。
- 最小 control 请求以 OpenAPI 为准，典型形态如下：

```json
{
  "input": "请核查当前告警并给出处置建议",
  "stream": true,
  "agentgov": {
    "agent_id": "your-business-agent",
    "include_trace": true
  }
}
```

- 新会话应省略 `conversation`；续聊时只填写由 `POST /v1/conversations`、会话列表或上一轮
  响应实际返回的 `conversation_id`，或只传上一轮实际返回的 `previous_response_id`，不要复制
  文档中的示例 ID。官方 OpenAI 契约不允许同时传两者；当前 AgentGov 仅在二者解析到同一会话时
  暂时接受，否则返回 `409`，该行为已列入 `x-agentgov-known-deviations`，新集成不要依赖此偏差。
- `stream=false` 返回 Responses 对象；权威文本位于 `output[].content[].text`，运行关联位于 `agentgov.run_id`、`agentgov.conversation_id`、`agentgov.session_id`、`agentgov.trace_id` 等扩展字段。默认 `store=true` 时可通过 `GET /v1/responses/{response_id}` 取回已完成响应；`store=false` 只关闭公开取回，不关闭内部治理审计。
- `stream=true` 返回 Responses-style SSE。完整事件名、payload schema、出现条件、phase 与 terminal 标记以该 operation 的 `x-agentgov-sse-events` 为准；其中取消会输出 `agentgov.cancelled`（control mode）并以 `response.incomplete` 保留部分 output。服务端工具事件只用于观察已经由 agent loop 执行的工具，绝不要求客户端重复执行标准 `function_call`。heartbeat 使用 SSE comment 保活，不应写入业务时间线。
- 当前过渡偏差由 OpenAPI `x-agentgov-known-deviations` 机器声明：即时非流式的 `trace_id` 可能与 retrieve 不一致；`response.completed.response.metadata` 当前不会回显请求 metadata；session 前源异常可能产生 `response.created.response.id=null` 后再失败。受管流在发送响应前完成 turn admission；若源在 identity 建立前失败，则不伪造 run/session headers，而在 HTTP `200` 后投影失败终态。客户端必须把没有声明终态的 EOF 当作失败，不能从 HTTP 状态本身推导运行成功。
- 正常进入投影终态的流以 `response.completed`、`response.failed` 或 `response.incomplete` 恰好一次收口；在偏差修复前，不承诺流式完成对象、即时非流式响应与 retrieve 的所有字段完全同构。
- 流式、非流式与 retrieve 的 `output[]` 都使用同一稳定顺序和 ID：存在 ThinkingBlock 时先是 `reasoning`（`rs_<run_id>`），随后是 assistant `message`（`msg_<run_id>`）。
- 产品内置 Playground 是有意的例外：live turn 直接消费下节的 SDK-native 入口，不经 `/v1/responses` 或 `/api/chat/stream`；会话列表和历史恢复仍使用 `/v1/conversations*`。
- 边界：工具权限、MCP、skills、subagents、hooks 和 sandbox 以业务 Agent workspace 的 Claude Code 项目配置为准；Runtime 只选择 project discovery，`can_use_tool` 只桥接原生 `ask`。旧 Chat 字段 `agent`、`skills`、`skills_mode`、`allowed_tools`、`disallowed_tools`、`permission_mode` 已删除，传入返回 `422`。续聊复用同一 `conversation_id`，或使用 `previous_response_id` 让底座解析其所属会话；两种方式都会校验所选业务 Agent 与既有会话 owner 一致，不允许把 Agent A 的 SDK transcript 交给 Agent B 续接。若 `previous_response_id` 对应 run 没有 `session_id`，或其 conversation mapping 已被删除，底座返回 `409`，不会把“续接”静默降级成新会话。

#### 4.2.1 Trace 语义事件与刷新

- `agentgov.trace_event.payload` 是一条完整 SDK 事实，包含后端生成的 `event_id`、`run_id`、
  `sequence`、原始消息/块位置、`kind`、`source_event`、`scope`、父工具调用和业务 payload。
  `kind` 覆盖 `thinking`、`text`、`tool_use`、`tool_result`、`hook`、`task`、`system`、
  `result` 等；一个 SDK content block 对应一条事件，因此同一消息中的多个工具调用不会丢失。
- `StreamEvent`、heartbeat、SSE 信封、session/done 控制帧和
  `SystemMessage:thinking_tokens` 计数快照只是传输/计量事实，不进入语义 Trace。Thinking 只从完整
  ThinkingBlock 生成，opaque signature 不下发；这不影响 `response.output_text.delta` 文本流。
- `agentgov.tool_step` 为存量 control 客户端暂时保留，现由同一投影器为每个工具 block 生成；
  新客户端应消费 `agentgov.trace_event`。`agentgov.debug.sdk_raw=true` 仍按完整 SDK message
  提供原始审计，包括带文本的 AssistantMessage；不得把 raw 事件直接渲染成业务时间线。
- 运行完成后调用 `GET /api/agent-runs/{run_id}/trace`。该接口从 AgentRun 中的原始
  `messages` 当场投影，不另存第二份语义 Trace，返回 `turn_status`、`turn_error`、`errors`、
  `completeness` 和事件列表。`succeeded`、`failed`、`cancelled`、`interrupted` 均可刷新重放；
  没有历史原始消息的旧 run 返回 `completeness=unavailable`，不伪造事件。
- HITL 请求/决策仍由确认卡契约承载，不混入 Trace 事件；Langfuse 是更深的开发观测入口，
  也不替代上面的运行证据投影。

流式 Prompt Suggestion 是可选的下一轮输入辅助：

- `/api/chat/stream` 使用 `event: prompt_suggestion`，data 为 `{suggestion, suggestions, run_id, session_id}`。`suggestions` 是完整候选列表（每轮至多 N 条，默认 3）；`suggestion` 恒等于 `suggestions[0]`，为向后兼容保留。
- `/api/agent-runtime/sdk-events` 使用 `event: agentgov.prompt_suggestion`，data 直接为 `{suggestion, suggestions, run_id, session_id}`；它不是 Responses 信封。
- `/v1/responses` control 模式使用 `event: agentgov.prompt_suggestion` 和既有 `{v,type,run_id,ts,seq,payload}` 信封，payload 为 `{suggestion, suggestions, session_id}`；strict 模式不输出该扩展事件。整批候选在**一帧**内下发，不会分多帧。
- 官方容器与本机调试 env 示例均以 `ENABLE_BACKEND_PROMPT_SUGGESTION=true` 显式选择后端派生路径；`AppSettings` 默认仍关闭该受控特例。关闭时回退 Claude Code 原生 `--prompt-suggestions`，该原生能力可能被上游 feature gate 或 cache 状态抑制。启动日志通过 `prompt_suggestion_source=backend|claude_native` 暴露当前来源；建议生成失败只记录结构化 warning，不改变主 Run 成功状态。
- Claude Code 可能因缓存或模型条件不生成建议，缺失不表示本轮失败。客户端收到后应只提供“填入输入框”动作，不自动发起下一轮请求。
- Suggestion 是临时 UI 辅助，不属于 Prompt 治理资产，也不进入正式会话消息、SQLite run、response retrieve 或 SDK transcript；刷新后无需恢复。
- Prompt Suggestion 在各接口终态前进行有界排空；超时或失败时跳过，不允许在
  `agentgov.done`、`done`、`response.completed`、`response.failed` 或
  `response.incomplete` 后迟到。

流式 Speech Summary 是另一项独立、显式 opt-in 的 TTS 文本辅助：

- `/api/agent-runtime/sdk-events` 和 `/api/chat/stream` 在各自请求体传
  `with_speech_summary=true`；后者的 `raw` 与 `semantic` 模式都直接输出 canonical
  `event: agentgov.speech_summary`。
- `/v1/responses` 仅在 control mode、`stream=true` 时接受
  `agentgov.with_speech_summary=true`；非流式开启返回 `422`，strict mode 不接受且不输出该
  AgentGov 扩展。
- `source_kind=thinking` 来自一个完整顶层 ThinkingBlock 的原生
  `content_block_stop`；`source_kind=assistant_response` 来自完整顶层
  AssistantMessage 中全部非空 TextBlock 的合并正文。ResultMessage 只表示整个 Agent Loop
  终止，不是一次回答摘要来源。
- 只处理主 Agent，工具、子 Agent、空块、纯工具调用消息跳过。摘要是 10–50 字的临时文本，
  不包含音频，不写入 SDK session、SQLite、response retrieve，也不混入 Claude SDK
  usage/cost。超时、模型错误或安全过滤失败均静默丢弃。
- 实际启用边界由 `SPEECH_SUMMARY_BOUNDARIES` 决定；请求开关与环境边界必须同时开启。官方
  容器和本机调试 env 示例保持相同边界，默认超时/终态排空为 15/5 秒。

#### 4.2.2 Claude SDK-native 受管事件 — OpenAPI tag `claude-sdk-events`

- `POST /api/agent-runtime/sdk-events` 是正式的 managed turn 交互入口，`agent_id` 必填并受统一 API key 保护；Playground 的 live turn 只调用该入口。
- 每个官方 `claude-agent-sdk` yield 原序输出一帧 `claude.sdk.<ClassName>`。`data` 是递归 dataclass → JSON 的机械序列化：不筛选、不改名、不合并、不调和快照，也没有 `str()` fallback。`StreamEvent.event`、ThinkingBlock signature、tool I/O、`SystemMessage:thinking_tokens` 和未来未知 SDK class 都保留。
- AgentGov-owned 控制面使用 `agentgov.session`、`agentgov.confirmation.requested/resolved`、`agentgov.prompt_suggestion`、可选 `agentgov.speech_summary`、`agentgov.result/error/done`；heartbeat 使用 SSE comment。`agentgov.result` 只投影 SDK `ResultMessage` 已到达的进度事实，之后仍可能排空 transcript mirror 和派生任务；只有 `agentgov.done` 才表示 managed turn 已持久化并收尾，客户端不能把两者当作可互换终态。
- 浏览器只把顶层 `text_delta` 放入回答；subagent text、thinking、工具 input delta/result 和未知消息进入运行证据。`thinking_tokens` 只显示为指标。block identity 使用 `parent_tool_use_id + message_start.message.id + index`，没有 message id 时才使用本地 message epoch，不能仅依赖每帧不同的 `StreamEvent.uuid`。
- 正常进入 managed 收口的流会在终态前完成或有界排空 Prompt Suggestion 与 Speech
  Summary，并以 `agentgov.done` 作为最后一个业务事件。UI 收到后可解除“运行中”，并继续
  读到 EOF 以正常关闭连接，但不得期待迟到业务
  事件。后续异步写入必须绑定本轮 assistant message id/run token，不能按“最后一条消息”
  写入。完成后的 Trace 只有 `completeness=complete` 才替换 live evidence；不可用或请求失败时
  保留原生 live evidence。
- 该接口跟随仓库锁定的 SDK 版本，不是 UI schema，也不是 CLI stdout byte stream。需要长期 OpenAI 兼容时，由独立适配服务消费该流并完成 Responses 转换；当前内置 `/v1/responses` 仅供过渡。
- session turn admission 与 backend-owned run/session 句柄在 SSE headers 发送前完成。源若在 identity 建立前失败，不会伪造句柄，而会在 HTTP `200` 后输出受管错误终态；只有收到 `agentgov.done` 才表示 managed turn 已完成持久化和收尾。

#### 4.2.2.1 精确运行取消

- `/api/agent-runtime/sdk-events`、`/api/chat/stream` 和流式 `/v1/responses` 在发送 body
  前通过 `X-AgentGov-Run-Id`、`X-AgentGov-Session-Id` 返回 backend-owned 句柄。调用方不得
  自造 run id，也不得只依赖首个 SSE frame 才建立停止控制面。
- 主动停止调用 `POST /api/agent-runs/{run_id}/cancel`，请求无 body、使用统一 Bearer API
  key。`200` 表示目标 run 已持久化为终态且不再持有该 session fence；重复调用返回同一终态。
  `404` 表示 run 不存在，`409` 表示持久化运行仍未结束但当前单 API 进程找不到其 owner，
  `504` 表示在服务端等待预算内未确认终态。后两者都不能被客户端解释为“已停止”。
- 客户端点击停止后必须继续锁住同会话发送、会话/Agent 切换和 HITL 决策。若响应头尚未到达，
  状态应为“等待运行句柄”；取消请求进行中为“停止中”；`409`、`504` 或网络结果不明时为
  “状态待核对”，只允许对同一 run 重试取消。只有取消 API `200`，或流明确给出已持久化的
  `agentgov.cancelled` + `agentgov.done`，才能解锁下一轮。
- 取消保留已收到的部分文本和 Trace 证据，但不追加“运行失败”。Responses projector 以
  `response.incomplete` 收口，不输出 `response.failed`。浏览器断开连接也会显式关闭嵌套
  stream source 并取消 owner；非流式请求 owner 使用同一协调器。进程重启无法恢复旧
  asyncio owner，新进程启动时会立即把上一进程遗留的 running intent 标为 `interrupted`
  并释放 fence。

#### 4.2.3 Runtime 原始事件调试 — OpenAPI tag `debug`

- `POST /api/debug/agent-runtime/raw-events` 是 Runtime 中立的特权诊断入口，`agent_id` 必填；
  body 中 `stream=false` 缓冲响应，`stream=true` 流式刷新。两种模式消费同一个原始字节源。
- 该调用仍是正常 managed Agent turn：业务 Agent profile、SessionStore、session lease、
  权限/HITL、hooks、MCP、Langfuse 和 run 持久化均不绕过。响应体只改变对外投影方式。
- Claude Code driver 使用透明 CLI tee，在 `claude-agent-sdk` 解码/解析之前复制 stdout。
  响应为 `application/octet-stream`，不包含 SSE 信封、AgentGov heartbeat/done/error，
  不做 JSON parse、重序列化、脱敏或未知事件过滤。HTTP chunk 只代表传输切片；集成方必须按序
  拼接字节，不能把 chunk 当事件。
- `X-AgentGov-Run-Id`、`X-AgentGov-Session-Id`、`X-AgentGov-Agent-Id`、
  `X-AgentGov-Runtime-Kind`、`X-AgentGov-Execution-Origin`、
  `X-AgentGov-Native-Protocol`、`X-AgentGov-Runtime-Version` 与
  `X-AgentGov-Raw-Fidelity` 是 backend-owned 来源证明，请求 payload/metadata 不能覆盖。
- 安全边界：原始流可能携带完整 prompt、tool I/O、控制帧和 transcript mirror，不能在
  byte-exact 前提下脱敏。接口默认关闭；只有同时设置非空 `API_KEY` 和
  `ENABLE_AGENT_RUNTIME_RAW_EVENTS=true` 才能启动为可用状态。单次上限由
  `AGENT_RUNTIME_RAW_EVENTS_MAX_BYTES` 控制，默认 64 MiB。
- 当前实现的 Runtime kind 是 `claude-code`。Claude Code 后面的模型出口即使是 Qwen/Kimi，
  Runtime kind 也不变；未来直接接 Qwen Code/Kimi CLI 时新增 driver 并继续使用同一路径。
  此接口不是 Anthropic-compatible provider HTTP wire。
- raw 请求模型不包含 `with_speech_summary`，显式传入返回 `422`；响应体、响应头和旁路接口都
  不承载 Speech Summary。
- `/api/chat/stream?event_mode=raw` 和 `agentgov.debug.sdk_raw` 是历史的已解析 SDK 投影，
  不是 byte-exact CLI stdout；业务对话/语义 Trace 继续使用它们或 `/v1/responses`，不要把
  原始调试流直接渲染为业务时间线。

#### 4.2.4 流式 Web HITL 人工确认卡

`ENABLE_CLAUDE_WEB_HITL=true` 且目标业务 Agent 的 Claude Code 权限规则触发 `ask` 时，Web 人工确认通过 `/v1/responses` control mode 的流式 SSE 暴露。非流式 Responses 不承载在线确认卡。集成方必须把该 SSE 连接当成带暂停点的状态机，而不是普通文本流。

最短集成流程：

1. 过渡期调用 `POST /v1/responses`，传 `stream=true` 和 `agentgov.agent_id`，保持 SSE
   连接直到 `response.completed`、`response.failed` 或 `response.incomplete`；若 EOF 前未收到任一标准终态，按流失败处理。
2. 渲染标准 Responses 事件；control mode 同时处理 `agentgov.session`、`agentgov.tool_step`
   和可选的 `agentgov.prompt_suggestion`、`agentgov.speech_summary` 等事件。收到 SSE comment
   heartbeat 时只刷新连接存活时间。
3. 收到 `agentgov.confirmation.requested` 时，从其 `payload` 读取 `request_id`、`decision_token`、请求类型、工具或问题信息并渲染内联确认卡；不要关闭原 SSE 连接。
4. 用户决策后调用 `POST /v1/agentgov/confirmation-requests/{request_id}/decision`。请求体只使用 OpenAPI 的 `action`、`decision_token`、`answer`、`message`；不得回传 `run_id`、`session_id`、`business_agent_id`，也不得使用顶层 `answers`、`response`、`updated_input` 或 `allow_modified`。
5. 继续读取原 SSE 流；收到 `agentgov.confirmation.resolved` 后更新同一张卡片，最终按标准 Responses 完成或失败事件收口。

HITL 查询只保留 `GET /api/claude-user-input-requests`。只有按精确 `run_id` 且
`status=waiting` 查询时才可能返回一次性 `decision_token`；宽泛列表不会返回 token。查询与
decision 都要求 Bearer API key，`decision_token` 是 API key 之外的单请求授权因子。历史
`/api/claude-hitl-requests*` 以及
`POST /api/claude-user-input-requests/{request_id}/decision` 已删除，不得探测或回退使用。

决策请求示例：

```json
{
  "action": "answer_question",
  "decision_token": "<one-time-token>",
  "answer": {
    "response": "只处理当前告警资产"
  }
}
```

Swagger UI 同时提供 `deny`、`allow_once`、`allow_for_run`、`answer_question` 四个具名示例。
`answer` 是唯一回答对象：选项答案放在其 `answers` 键，自由文本放在其 `response` 键；二者都不是顶层字段。`message` 仅用于拒绝原因或补充说明，不使用时应省略而不是发送 `null`。

确认类型：

- `tool_permission`：工具授权卡。动作是 `allow_once`、`allow_for_run`、`deny`。`allow_for_run` 只对已判定的低风险类别生效，绑定 `business_agent_id + run_id + low-risk category`，不跨类别、不跨 run、不写入永久权限配置；高风险或未分类请求不接受该动作。
- `ask_user_question`：Claude 主动向用户澄清。动作只能是 `answer_question`，结构化选项或自然语言 `response` 统一放入 `answer` 对象。

安全与体验边界：

- `decision_token` 是一次性敏感能力 token，只放前端内存；不要写入 localStorage、服务端日志、埋点或会话持久层。
- 确认卡建议挂在当前 assistant 消息内，显示工具名、风险等级、参数摘要和三类工具动作；避免全局弹窗让用户丢失上下文。
- 页面刷新或客户端丢失 `decision_token` 后，不应伪造决策；提示用户重新运行当前任务。
- 用户断开 SSE 时，底座会取消当前 run 的等待请求；上层系统应把卡片标为已中断或失效。

#### 4.2.4 Workspace 权限与 Agent 专属流程

所有注册业务 Agent 使用同一套运行、会话和治理接口。平台不按 Agent ID 注入工具、权限或业务流程，也不为某个 Agent 建立专用授权分支。

- 权限事实只来自目标 Workspace 的 `.claude/settings.json`；`can_use_tool` 只桥接 Claude 原生 `ask` 决策。
- 集成方按流式事件契约处理确认请求，不根据 Agent ID 猜测其工具名、风险级别或确认步骤。
- 角色、工具、业务流程和专属测试由该 Agent 的 `workspace/README.md`、`CLAUDE.md`、`agent.yaml`、`.mcp.json`、`.claude/` 与 `tests/` 共同定义。
- Workspace 包根目录 `agent.yaml.agent.id` 必须有效，并与 URL 路由中的目标 `agent_id` 逐字一致；
  平台不改写该字段。成功导入后，新注册对象立即使用平台通用机制运行和治理。

Playground 可调用 `GET /api/agent-registry/{agent_id}/presentation` 获取结构化 Welcome Card。`agent_id` 和展示名称来自平台注册表；版本、语言、Runtime、能力标识以及可选的 `presentation.summary`、`welcome_message`、`composer_placeholder`、`starter_prompts` 从该 Agent 的 `agent.yaml` 白名单投影。开场内容只用于会话前静态展示，不创建 session、不写入消息历史；建议任务只填入输入框，不自动发送。Agent 在真实会话中的动态澄清继续使用 Claude 原生 `AskUserQuestion`。

仓库内 `docker/runtime-bootstrap/` 只初始化整体缺失的内置 Workspace，不覆盖现有 live Workspace。升级现有实例时，先导出候选，人工把包内 `agent.id` 设置为测试 ID 后以新 ID 导入测试；完成回归并导出候选后，再人工把包内 ID 设置回目标 ID，携带目标预期当前提交版本覆盖。测试和发布是显式治理门，不是隐藏的运行准入锁。

### 4.3 创建与回放会话 — OpenAPI tag `openai-conversations`
- 目标：刷新/重开旧会话时重建对话气泡。
- 最短路径：`POST /v1/conversations` 可预创建会话；`GET /v1/conversations` 列出会话；`GET /v1/conversations/{conversation_id}` 读取元数据；`DELETE /v1/conversations/{conversation_id}` 删除会话映射；`GET /v1/conversations/{conversation_id}/items` 从 SDK transcript 投影历史。
- items 返回 `data[]`，每项包含 `id`、`role`、`parent_tool_use_id`、`content`；内容块保留 `thinking`、`text`、`tool_use`、`tool_result` 等 SDK 事实。若 transcript message UUID 能通过已提交的 SessionStore entry 确定性关联 `AgentRun`，该 item 还包含可选的 `agentgov` 扩展：`run_id`、`sdk_session_id`、`agent_version_id`、`langfuse_trace_id`、`langfuse_trace_url`。分页使用 cursor 风格 `after`、`limit`、`order`、`include`，不使用 offset。
- 边界：只传 `conversation_id`，不传 `agent_id`；归属由底座解析。会话尚无 transcript 时 `data` 为空；读取未知会话或其 items 返回 `404`，删除未知映射返回 `deleted=false`。历史数据若 `agent_id` 为空但已经存在 `turns` 或 `sdk_session_id`，底座无法从该映射唯一证明 owner，会 fail-closed 返回 `409`；集成方应新建会话，不得指定 Agent 抢占，也不得依赖底座猜测或静默迁移。item 无 `agentgov` 表示旧 transcript 无法确定性关联 run；扩展存在但 Trace 字段为空表示该 run 未记录 Langfuse Trace。底座不按文本或时间猜测，也不伪造 Trace URL。会话正文继续来自 SDK transcript；失败、取消或中断 run 的气泡/状态可由 `GET /api/agent-runs?session_id=...` 补齐，Trace 一律按 `run_id` 调专用 Trace API，不能从 conversation content block 猜测。
- 旧 `/api/sessions*` 已在 OpenAPI 标记 deprecated，只为存量 offset 客户端保留；完成消费者迁移后，将在下一次确认的破坏性版本中删除。

### 4.4 提交反馈并驱动闭环 — OpenAPI tag `feedback` / `improvements`
- 目标：把用户/系统反馈喂回闭环，产出归因、优化、执行改动和回归测试设计。
- 最短路径：先创建或识别已有的 `signal`、`soc_event` 或已解析的 `pending_correlation`，再调用 `POST /api/feedback-cases`。请求体必须包含至少一个 typed 引用，且所有来源必须归属同一业务 Agent，例如 `{"source_refs":[{"source_kind":"signal","source_id":"sig-..."}],"title":"...","priority":"medium"}`。统一反馈来源的 path 参数也只接受这三个规范值；历史内部别名 `feedback_signal`、`event`、`pending` 不再接受。`run_id`/`session_id`/`alert_id`/`case_id` 由底座从被引用来源投影，不是该请求的顶层字段；旧 `source_ids`、空 `source_refs` 和空白 `source_id` 均会被拒绝。随后通过 `POST /api/feedback-cases/{id}/evidence-packages` 补证据，创建或选择改进事项 `POST /api/improvements`，并把 `source_feedback_refs` 指向反馈记录。四阶段产物通过 `/api/improvements/{improvement_id}/attribution/generate`、`/optimization-plan/generate`、`/execution/apply`、`/regression-test-design/generate` 生成，集成方在对应确认门上决策。
- Trace 集成：四阶段生成结果会返回 `generation_trace_id` / `generation_trace_url`；需要在上层系统展示详情时调用 `GET /api/langfuse/traces/{trace_id}`，不要让前端直接持有 Langfuse secret。
- 边界：归因/优化/执行/回归生成的内部机制（governor、DSPy formatter、Langfuse enrich）是底座内部，集成方只**提交反馈 + 管理改进事项 + 在确认/门禁上决策**，不直接编排治理 Agent。业务产物成功持久化后由对应端点推进事项阶段；`/lifecycle` 仅用于合法返工，不能绕过产物直接前推。

### 4.5 Workspace 测试与平台运行 — OpenAPI tag `improvements` / `agent-testing`
- 目标：把已确认的 `RegressionTestDesign` 物化为待发布版本中的 pytest 文件，并在精确 Git 提交上执行平台测试。
- 最短路径：调用 `POST /api/improvements/{improvement_id}/regression-test-design/confirm`。底座会在同一未发布 change set 的 worktree 中新增 `tests/test_feedback_*.py` 并提交更新后的待发布版本，但不隐式创建或运行 `AgentTestRun`。使用 `GET /api/agent-test-assets` 查询各业务 Agent 的当前测试资产摘要，使用 `GET /api/agent-registry/{agent_id}/test-suite?commit_sha=<sha>` 检查指定提交，使用 `/test-suite/file` 读取 suite 内单个源码文件；手工运行调用 `POST /api/agent-test-runs`，待发布变更运行调用 `POST /api/agent-change-sets/{change_set_id}/test-runs`；轻量分页历史使用 `GET /api/agent-test-runs/history`，单次详情使用 `GET /api/agent-test-runs/{test_run_id}`，取消使用 `POST /api/agent-test-runs/{test_run_id}/cancel`。
- 定时策略：`GET/PUT /api/agent-registry/{agent_id}/test-schedule` 读取或保存每 Agent 唯一五字段 Cron + IANA 时区策略；`GET /api/agent-registry/{agent_id}/test-schedule/events` 查询触发审计。保存不立即运行。触发时只固定当前有效 commit，运行来源为 `scheduled` 且不绑定 `change_set_id`；策略不得推进、发布或回滚 change set。
- 固定执行：底座只执行 `python -m pytest -q -p agentgov_testkit.pytest_plugin tests`，即完整执行该提交的 `workspace/tests/`，不会按本次 Diff 只选新增或修改的用例。创建运行可省略 `commit_sha`，但底座会在请求内固定当时版本，不会在稍后执行时重新取“最新”。
- 边界：`workspace/tests/` 是测试内容唯一真相源。集成方不能提交命令、工作目录、测试状态、报告、任意文件路径或 backend-owned 提交绑定；源码读取路径必须属于指定 suite。`commit_sha` 是被测版本权威标识；`suite_digest` 是派生摘要，`change_set_id` 是业务关联。服务重启后 running 记录变为 `interrupted`，queued 重新入队，未完成调度事件幂等恢复，临时测试 session 返回明确不可用错误；运行超时以 `error/AGENT_TEST_RUN_TIMEOUT` 记录。

### 4.6 版本发布与回滚 — OpenAPI tag `feedback`
- 目标：把确认的改动发布为新版本，可回滚。
- 最短路径：`/api/agent-change-sets/...`（`diff`/`file-diff`/`approve`/`reject`/`publish`）；`/api/agent-releases/...`（`restore`/`rollback`）；`/api/agent-repository/...`（`snapshot`/`current`/`discard-changes`，可选 `?agent_id=` 指定业务 Agent，默认 `security-operations-expert`）。
- 边界：版本治理按 `agent_id` 落到各业务 Agent 自己的 per-Agent 版本库。发布必须存在同一业务 Agent、当前待发布 `commit_sha` 上完整测试集通过的平台运行；旧提交通过不能放行新提交，已有失败和新增失败都必须修复。反馈闭环待发布版本不能强制绕过测试条件，Playground 的反馈发布工作台不提供强制发布入口；未关联反馈、由版本治理 API 手工创建的待发布版本仍可通过受保护 API 强制发布，但必须填写非空原因并持久化原阻塞项和警告。provenance 不完整始终不可绕过。审批/发布的业务决策在上层；底座负责执行、记录、审计与原子性。

### 4.7 资产沉淀与跨 Agent 复用 — OpenAPI tag `assets` / `agent-testing`
- 目标：资产复利中心统一承载测试资产只读投影，以及方法论、执行和审计资产的继承复用；测试文件始终随对应业务 Agent Workspace Git 管理。
- 最短路径：“测试资产”使用 §4.5 的 suite/file/history/schedule 接口，不调用通用资产创建或继承；“治理资产”使用 `GET/POST /api/assets`（通用类型仅 `methodology`、`execution`、`audit`，可按 `agent_id`/`asset_type` 过滤）和 `POST /api/assets/{asset_id}/inherit`。未知 `asset_type` 在 API 契约边界返回 `422`。底座不复制测试正文，也不提供跨 Agent 自动继承测试代码。

### 4.8 定制业务 Agent 的行为（workspace / Claude Code 配置）
- 目标：给某个业务 Agent 定制 prompt / 角色边界、skills、subagents、规则、MCP 工具与权限——即它的 Claude Code workspace 配置。
- **在线资产闭环**：`POST /api/agent-registry/{agent_id}/workspace/export` 同步导出当前 Git tree；`POST /api/agent-registry/{agent_id}/workspace/import` 在包内 ID 与目标 ID 完全一致时原样新建或覆盖；`POST /api/agent-registry/{agent_id}/workspace/restore` 把历史 tree 恢复为一个新 commit。覆盖必须携带预期当前提交版本，活跃 turn、版本已变化或未终结 change set 会返回 `409`。导入、恢复和 dirty export snapshot 都绑定实际 per-Agent Git commit，并在下一 turn 生效；不创建第二套 import/export operation 状态机。
- **包边界**：workspace 包是单个顶层 `workspace/` 的 `.tar.gz`。普通文件和二进制按字节保留，可包含 `.env`、真实 endpoint、MCP header 或其他私有运行配置；根目录 `agent.yaml.agent.id` 缺失、无效或与 URL 目标不一致时，会在目标 Workspace、注册表、Git 和会话状态变更前拒绝，同时保留失败审计。平台还拒绝路径逃逸、`.git`、特殊 tar 成员、重复冲突、资源超限和已知 JSON 语法错误。导出包因此应按敏感资产保管，不得进入公开仓库或日志。
- **配置位置 = 该业务 Agent 的运行卷 Workspace**：`${HOST_RUNTIME_VOLUME_ROOT}/data/business-agents/<agent_id>/workspace/`（容器内 `/data/business-agents/<agent_id>/workspace`；`<agent_id>` 是 Workspace 包导入路径中的 id，其 `workspace_dir` 也可由 `GET /api/agent-registry` 查到）。可定制：
  - `CLAUDE.md`（角色/边界/SOP）、`agent.yaml`（运行元数据与静态 Welcome Card）、`.claude/skills/<skill>/SKILL.md`、`.claude/agents/*.md`、`.claude/rules/*`、`.mcp.json`（工具接入，并同步 `.claude/settings.json` 权限）。
  - 该 `workspace/` 是这个 Agent 的**活配置层**（git 就地版本化的版本源）；与它**并列**在 `<agent_id>/` 下的还有 `claude-root/`（Claude SDK 运行态）与 `version/`（per-agent 版本治理工件：worktrees/releases）——去嵌套后运行态天然不进版本源。优化闭环跑的是候选 worktree、发布归档落该 Agent 自己的 `version/`，都不在 `workspace/` 内。
  - **统一模型**：所有注册业务 Agent（含 `main-agent`）都住 `data/business-agents/<id>/workspace/`，使用同一套 profile、运行和版本治理。API 启动时发现 live Workspace 并幂等登记；`security-operations-expert` 的内置、默认、受保护属性由平台分别派生。治理 Agent `governor` 使用顶层 `governor-workspace/` 与 `claude-roots/governor`，不对集成方暴露为可编排对象。
- **不要用 `docker/runtime-bootstrap/` 定制已存在 Agent**：它是只读运行卷初始化源，只包含 governor 与内置 `security-operations-expert` 的初始 Workspace。它只处理整体缺失目录，不逐文件同步已有实例。定制具体 Agent 一律改其 live Workspace，或走上述导出/导入闭环。
- **普通新 Agent 只通过 Workspace 包创建**：平台没有通用创建模板，也不会扫描仓库目录自动增加普通业务 Agent。可先导出 `security-operations-expert` 作为 Workspace 起点，修改后以新 ID 导入；其内容和权限由导入方负责。
- **运行卷是活配置事实**：初始化逻辑不会按启动模式、receipt 或代码版本重写已有 Workspace 的 settings、MCP、hook 或 endpoint。受保护属性只决定在线删除规则，不改变 Workspace 的运行和版本治理路径。
- 边界与注意：
  - **不要碰** 与 workspace **并列**的 `version/`（该 Agent 的版本治理工件）与 `claude-root/`（运行态 Claude 状态）；改它们会破坏版本治理 / 运行态。
  - `workspace/` 同时是 Agent 的 git 版本源——直接改是工作树未提交改动；要固化成版本走版本治理（change set / release，见 4.6）。
  - 权限与仓库边界：每个 Workspace 的 Claude 原生权限配置独立生效；MCP 写入/处置工具如需在线人审，应放入 `ask` 并通过流式运行 + `ENABLE_CLAUDE_WEB_HITL=true` 承载。Workspace 包导入保留原权限，不套用平台默认值。仓库内运行卷初始化源不得写真实 api_key/token、私有 header、数据库凭据或本机私有路径；live Workspace 与其 per-Agent Git 可原样保存业务运行所需私有值，但导出、日志和运维操作必须按敏感运行资产处理。live Workspace 纳入内置初始化源前，必须先在仓库外形成候选并通过准入扫描。
  - 工具 / MCP / skills / subagents 以 Claude Code 官方配置为准（见 §6），不通过 chat 入参接管；workspace `.mcp.json` 中的 `${VAR}` 由 Claude Code 使用 Runtime 传入的完整环境原生解析。改完在下一 turn 生效，不要求 API 重启或平台重渲染。

### 观测
- `GET /health/live` 只检查 API 进程存活，`GET /health/ready` 返回缓存的模型 provider readiness，`GET /health` 返回运行态字段和同一 provider 摘要。外部 vLLM 超时不应被解释为 API 启动失败；模型请求的结构化错误与 readiness 中的 `error_code`、`probe`、`reason`、`retryable`、`action` 可用于定位根因。
- 运行 trace 经 Langfuse（自托管，内网）观测，集成方可据 `run_id` 关联。

## 5. 契约稳定性与版本

- **受管运行事实源**：`POST /api/agent-runtime/sdk-events`；未来独立 OpenAI 适配服务从这里转换。客户端仍必须遵守“无 `agentgov.done` 的 EOF 即失败”的终态边界。
- **稳定资源路径**：`/v1/conversations*`、`GET /api/claude-user-input-requests`、`POST /v1/agentgov/confirmation-requests/{request_id}/decision`；反馈闭环、Agent 注册、版本治理等领域能力继续使用 OpenAPI 声明的 `/api/*` 资源接口。
- **过渡契约**：`POST /v1/responses` 与 `GET /v1/responses/{response_id}`。消费者必须读取 OpenAPI 的 transitional 标记和已知偏差，不能据路径名推断完整 OpenAI 兼容。
- **演进中**（接入需关注变更）：外置 OpenAI adapter、Responses 三种投影一致性、业务 Agent 多租与隔离、审批外移细化。
- OpenAPI 与生成类型是契约边界；底座变更公开 API 时会同步 OpenAPI 与迁移说明。集成方应以 OpenAPI 版本为对接基线，并对 `4xx/5xx` 做稳健处理。
- `POST /v1/responses` 是过渡单 endpoint 双响应媒体类型：`stream=false` 返回 `application/json`；`stream=true` 返回 `text/event-stream`。
- 运维验收应同时检查运行容器 `/openapi.json` 的 `info.version` 与仓库 `VERSION` / 镜像 tag 一致；版本不一致优先按部署镜像或容器未 recreate 的漂移处理。仓库内的真实 Compose 验收只使用公开 Make 入口；入口基于当前工作树重建本地镜像、加载 `COMPOSE_ENV_FILE` 选择的完整配置、`--force-recreate` 服务并校验本轮 freshness 后才执行检查，不会自动拉取远端代码。纯宿主机测试不需要刷新容器。

## 6. 集成反模式（请不要这么做）

- **读会话历史时传 `agent_id`**：归属是 backend-owned，由底座按 `conversation_id` 解析；传入会与真实归属冲突。
- **在 AgentGov 内做高风险动作的人审批**：审批划归外部业务系统（见愿景与生产化清单）；底座只记录 operator/reason/审计事件。
- **绕过 OpenAPI 自造 schema**：客户端类型应由 OpenAPI 生成，避免 schema 双轨漂移。
- **把 `main-agent` 特殊化**：它只是普通历史示例；默认、内置和受保护属性当前属于 `security-operations-expert`，且必须分别判断。
- **在上层另存一份会话/消息副本**：会话事实的单一真相源是 agent 的 SDK transcript，按 `conversation_id` 向底座取，不要并行存储。
- **用 `session_id` 或 `sdk_session_id` 当新集成的会话 URL id**：二者是 AgentGov/SDK 内部关联值；新集成使用 `conversation_id`。

## 7. 已弃用兼容接口附录

以下接口已在 OpenAPI 标记 `deprecated: true`，但本阶段不删除且未设置 sunset 日期。它们仅为
已有调用方保留，不是新集成入口，也不再承载新增 AgentGov 控制面能力：

| 兼容接口 | 新集成替代路径 | 说明 |
| --- | --- | --- |
| `POST /api/chat` | `POST /api/agent-runtime/sdk-events`，或过渡 `/v1/responses` | 旧非流式 ChatRequest/ChatResponse 包装；不支持 Speech Summary。 |
| `POST /api/chat/stream` | `POST /api/agent-runtime/sdk-events` | 默认 `event_mode=raw` 保旧 SSE；`?event_mode=semantic` 保文本流并输出 `trace_event`；两模式可显式开启 Speech Summary。 |
| `/api/sessions*` | `/v1/conversations*` | 旧 session/offset 历史读取契约。 |
| `POST /v1/chat/completions` | 规划中的外置 OpenAI adapter；过渡期可评估 `/v1/responses` strict mode | 仅支持字符串消息和非流式文本；`stream=true`、tools、multimodal 均不支持。 |

兼容接口当前仍由 OpenAPI 或运行时提供，但新客户端不得以它们建立新的控制面依赖。迁移后使用 `conversation_id`、SDK-native 受管事件和唯一 HITL decision 路径；旧 Chat 接口删除仍需消费者确认与迁移公告，旧 Sessions 则计划在完成迁移后的下一次确认破坏性版本中删除。

## 8. AI 辅助集成（可选）

若上层系统本身用 Claude Code / Codex 开发，可安装 AgentGov 提供的可分发集成 skill（见仓库 `integrations/agentgov-integration/`），让集成方的开发 Agent 直接掌握上面的旅程与边界。该 skill 派生自本指南，本指南为单一真相源。
