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
- **错误语义**：`400/422` 入参非法；`401` 未鉴权；`403` 权限拒绝；`404` 资源不存在；`409` 状态冲突（如重复创建、并发发布）；`413/415` 可编辑配置文件大小或编码不符合要求；`500` 服务端/数据完整性异常；`503` 运行时、模型或配置的出口 Agent 暂不可用。路由主动抛出的 HTTP / 领域错误统一返回 `{detail, error_code}`，领域错误可能带额外诊断字段；FastAPI 请求体验证失败的 `422` 仍可能是标准 validation error 形态。失败即报错，不静默降级为 offline/raw 结果。
- **离线不变量**：底座不依赖公网远程服务；模型经 `MODEL_PROVIDER_BACKEND` 显式选择的本地/内网网关接入。集成方不应假设任何公网回调。
- **契约真相源**：以容器 `/openapi.json`、`/docs` 为准；前端/客户端类型应由 OpenAPI 生成，不要绕过 OpenAPI 自造 schema（见 §6）。OpenAPI `info.version` 即 AgentGov 的发布版本（与 git release tag、docker 镜像 tag 同源于仓库根 `VERSION`），可据此判断对接的是哪个 release。

## 3. 概念模型与所有权

```
业务Agent ──运行──▶ session / run ──反馈──▶ feedback-case ──归因/优化/评估──▶ change set / release ──沉淀──▶ asset registry
```

- **Agent**：集成方编排的是**业务 Agent**（被治理的长期对象，经 `/api/agent-registry` 查询）。`security-operations-expert` 是当前唯一内置、默认且受保护的业务 Agent，这三个属性彼此独立；`main-agent` 只是普通历史示例。治理 Agent `governor` 治理所有业务 Agent，不对集成方暴露为可编排对象。
- **conversation**：新集成使用 `conversation_id`（`conv_*`）作为对外会话标识，通过 `/v1/conversations` 创建、查询和恢复。Responses control 响应中的 `agentgov.session_id` 是 AgentGov 内部关联 ID，`sdk_session_id` 是更底层的 SDK resume id；两者都不应替代 `conversation_id` 作为新集成的会话 URL 参数。
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
- 边界：`security-operations-expert` 在空运行卷中由运行卷初始化源提供，无需在线创建。新集成通过 `/v1/responses` control mode 的 `agentgov.agent_id` 显式选择任一注册业务 Agent（含 `main-agent`）；缺失返回 `422`，未知或不可运行 Agent 返回对应领域错误。

### 4.2 运行一次对话 — OpenAPI tag `openai-responses`
- 目标：让 Agent 处理一条消息/任务。
- 新集成主路径：`POST /v1/responses`。同一个 endpoint 通过 `stream` 统一非流式 JSON 与流式 SSE：
  - **control mode（AgentGov 集成首选）**：请求包含 `agentgov`，且 `agentgov.agent_id` 必填；可同时传标准字段 `conversation`，以及 `agentgov.alert_id`、`agentgov.case_id`、`agentgov.max_turns` 等 OpenAPI 已声明扩展字段。
  - **strict mode（标准 OpenAI 客户端）**：请求不含 `agentgov`，运行运营者配置的 OpenAI-compatible 出口 Agent；不下发 `agentgov.*` 私有 SSE 事件。由于 AgentGov 的 `instructions` 是 append-only 而非 OpenAI replace/swap 语义，strict mode 传 `instructions` 返回 `422`。
- 最小 control 请求以 OpenAPI 为准，典型形态如下：

```json
{
  "input": "请核查当前告警并给出处置建议",
  "stream": true,
  "conversation": "conv_sess_...",
  "agentgov": {
    "agent_id": "main-agent"
  }
}
```

- `stream=false` 返回 Responses 对象；权威文本位于 `output[].content[].text`，运行关联位于 `agentgov.run_id`、`agentgov.conversation_id`、`agentgov.session_id`、`agentgov.trace_id` 等扩展字段。默认 `store=true` 时可通过 `GET /v1/responses/{response_id}` 取回已完成响应；`store=false` 只关闭公开取回，不关闭内部治理审计。
- `stream=true` 返回 Responses-style SSE：标准事件包括 `response.created`、`response.output_text.delta`、`response.completed`、`response.failed`；control mode 另有 `agentgov.session`、`agentgov.tool_step`、`agentgov.confirmation.*`、`agentgov.result`、`agentgov.error`、`agentgov.done`。heartbeat 使用 SSE comment 保活，不应写入业务时间线。
- 边界：工具权限、MCP、skills、subagents、hooks 和 sandbox 以业务 Agent workspace 的 Claude Code 项目配置为准；Runtime 只选择 project discovery，`can_use_tool` 只桥接原生 `ask`。旧 Chat 字段 `agent`、`skills`、`skills_mode`、`allowed_tools`、`disallowed_tools`、`permission_mode` 已删除，传入返回 `422`。续聊复用同一 `conversation_id`，或使用 `previous_response_id` 让底座解析其所属会话；两种方式都会校验所选业务 Agent 与既有会话 owner 一致，不允许把 Agent A 的 SDK transcript 交给 Agent B 续接。若 `previous_response_id` 对应 run 没有 `session_id`，或其 conversation mapping 已被删除，底座返回 `409`，不会把“续接”静默降级成新会话。

流式 Prompt Suggestion 是可选的下一轮输入辅助：

- `/api/chat/stream` 使用 `event: prompt_suggestion`，data 为 `{suggestion, suggestions, run_id, session_id}`。`suggestions` 是完整候选列表（每轮至多 N 条，默认 3）；`suggestion` 恒等于 `suggestions[0]`，为向后兼容保留。
- `/v1/responses` control 模式使用 `event: agentgov.prompt_suggestion` 和既有 `{v,type,run_id,ts,seq,payload}` 信封，payload 为 `{suggestion, suggestions, session_id}`；strict 模式不输出该扩展事件。整批候选在**一帧**内下发，不会分多帧。
- 官方容器与本机调试 env 示例均以 `ENABLE_BACKEND_PROMPT_SUGGESTION=true` 显式选择后端派生路径；`AppSettings` 默认仍关闭该受控特例。关闭时回退 Claude Code 原生 `--prompt-suggestions`，该原生能力可能被上游 feature gate 或 cache 状态抑制。启动日志通过 `prompt_suggestion_source=backend|claude_native` 暴露当前来源；建议生成失败只记录结构化 warning，不改变主 Run 成功状态。
- Claude Code 可能因缓存或模型条件不生成建议，缺失不表示本轮失败。客户端收到后应只提供“填入输入框”动作，不自动发起下一轮请求。
- Suggestion 是临时 UI 辅助，不属于 Prompt 治理资产，也不进入正式会话消息、SQLite run、response retrieve 或 SDK transcript；刷新后无需恢复。

#### 4.2.1 流式 Web HITL 人工确认卡

`ENABLE_CLAUDE_WEB_HITL=true` 且目标业务 Agent 的 Claude Code 权限规则触发 `ask` 时，Web 人工确认通过 `/v1/responses` control mode 的流式 SSE 暴露。非流式 Responses 不承载在线确认卡。集成方必须把该 SSE 连接当成带暂停点的状态机，而不是普通文本流。

最短集成流程：

1. `POST /v1/responses`，传 `stream=true` 和 `agentgov.agent_id`，保持 SSE 连接直到 `response.completed` / `response.failed` 以及 `agentgov.done`。
2. 渲染标准 Responses 事件；control mode 同时处理 `agentgov.session`、`agentgov.tool_step` 和可选的 `agentgov.prompt_suggestion` 等事件。收到 SSE comment heartbeat 时只刷新连接存活时间。
3. 收到 `agentgov.confirmation.requested` 时，从其 `payload` 读取 `request_id`、`decision_token`、请求类型、工具或问题信息并渲染内联确认卡；不要关闭原 SSE 连接。
4. 用户决策后调用 `POST /v1/agentgov/confirmation-requests/{request_id}/decision`。请求体只使用 OpenAPI 的 `action`、`decision_token`、`answer`、`message`；不得回传 `run_id`、`session_id`、`business_agent_id`，也不得使用顶层 `answers`、`response`、`updated_input` 或 `allow_modified`。
5. 继续读取原 SSE 流；收到 `agentgov.confirmation.resolved` 后更新同一张卡片，最终按标准 Responses 完成或失败事件收口。

决策请求示例：

```json
{
  "action": "answer_question",
  "decision_token": "<one-time-token>",
  "answer": {
    "response": "只处理当前告警资产"
  },
  "message": null
}
```

`answer` 是唯一回答对象：选项答案放在其 `answers` 键，自由文本放在其 `response` 键；二者都不是顶层字段。`message` 仅用于拒绝原因或补充说明。

确认类型：

- `tool_permission`：工具授权卡。动作是 `allow_once`、`allow_for_run`、`deny`。`allow_for_run` 只对已判定的低风险类别生效，绑定 `business_agent_id + run_id + low-risk category`，不跨类别、不跨 run、不写入永久权限配置；高风险或未分类请求不接受该动作。
- `ask_user_question`：Claude 主动向用户澄清。动作只能是 `answer_question`，结构化选项或自然语言 `response` 统一放入 `answer` 对象。

安全与体验边界：

- `decision_token` 是一次性敏感能力 token，只放前端内存；不要写入 localStorage、服务端日志、埋点或会话持久层。
- 确认卡建议挂在当前 assistant 消息内，显示工具名、风险等级、参数摘要和三类工具动作；避免全局弹窗让用户丢失上下文。
- 页面刷新或客户端丢失 `decision_token` 后，不应伪造决策；提示用户重新运行当前任务。
- 用户断开 SSE 时，底座会取消当前 run 的等待请求；上层系统应把卡片标为已中断或失效。

#### 4.2.2 内置网络安全业务 Agent 与原生确认

`security-operations-expert` 是当前唯一内置、默认且受保护的网络安全业务 Agent。它可导出、跨 ID 导入并继续迭代；导出的 Workspace 包是可修改起点，不是平台模板。持有 `API_KEY` 的用户成功导入后，新 registry Agent 即为可运行实例；平台不按来源或处置阶段设置第二套准入白名单。包内 `agent.id` / profile 是来源声明，不覆盖路由与 registry 身份。

- 权限事实只来自导入 workspace 的 `.claude/settings.json`。精确的 `mcp__sec-ops__soc_api__create` 与 `mcp__sec-ops__soc_api__manual` 触发原生工具确认卡；`soc_api__execute`、`AskUserQuestion` 与其他 mutation 由 workspace 拒绝。
- 临时剧本按 `create -> manual` 形成两次独立确认；已发布剧本只确认 `manual`。每张卡只接受当次 `allow_once` 或 `deny`，高风险请求不显示也不接受 `allow_for_run`。
- 决策 API 不接收工具参数变更。确认对象就是卡片展示的完整输入，客户端不得提交 `updated_input`、`allow_modified` 或重写后的参数。
- `manual` 返回非空 `instanceId` 后，本次提交结束；提交回执不是执行完成或效果达成，查询、验证和关闭处置单属于后续流程。
- 旧 `agentgov.phase`、`approval_request_id`、`playbook_digest`、`execution_run_id` 字段已从公开契约删除，传入会返回 `422`；不再存在专用响应编排凭据或 claim 状态。

仓库内 `docker/runtime-bootstrap/` 只初始化整体缺失的内置 Workspace，不覆盖现有 live Workspace。升级现有实例时，先导出候选并以新 ID 导入测试，完成回归后导出该候选，再携带目标当前 commit 进行 CAS 覆盖；测试和发布是显式治理门，不是隐藏的运行准入锁。

### 4.3 创建与回放会话 — OpenAPI tag `openai-conversations`
- 目标：刷新/重开旧会话时重建对话气泡。
- 最短路径：`POST /v1/conversations` 可预创建会话；`GET /v1/conversations` 列出会话；`GET /v1/conversations/{conversation_id}` 读取元数据；`DELETE /v1/conversations/{conversation_id}` 删除会话映射；`GET /v1/conversations/{conversation_id}/items` 从 SDK transcript 投影历史。
- items 返回 `data[]`，每项包含 `id`、`role`、`parent_tool_use_id`、`content`；内容块保留 `thinking`、`text`、`tool_use`、`tool_result` 等 SDK 事实。分页使用 cursor 风格 `after`、`limit`、`order`、`include`，不使用 offset。
- 边界：只传 `conversation_id`，不传 `agent_id`；归属由底座解析。会话尚无 transcript 时 `data` 为空；读取未知会话或其 items 返回 `404`，删除未知映射返回 `deleted=false`。历史数据若 `agent_id` 为空但已经存在 `turns` 或 `sdk_session_id`，底座无法从该映射唯一证明 owner，会 fail-closed 返回 `409`；集成方应新建会话，不得指定 Agent 抢占，也不得依赖底座猜测或静默迁移。会话正文继续来自 SDK transcript，后端不另建消息副本。

### 4.4 提交反馈并驱动闭环 — OpenAPI tag `feedback` / `improvements`
- 目标：把用户/系统反馈喂回闭环，产出归因、优化、执行改动和回归测试设计。
- 最短路径：先创建或识别已有的 `signal`、`soc_event` 或已解析的 `pending_correlation`，再调用 `POST /api/feedback-cases`。请求体必须包含至少一个 typed 引用，且所有来源必须归属同一业务 Agent，例如 `{"source_refs":[{"source_kind":"signal","source_id":"sig-..."}],"title":"...","priority":"medium"}`。`run_id`/`session_id`/`alert_id`/`case_id` 由底座从被引用来源投影，不是该请求的顶层字段；旧 `source_ids`、空 `source_refs` 和空白 `source_id` 均会被拒绝。随后通过 `POST /api/feedback-cases/{id}/evidence-packages` 补证据，创建或选择改进事项 `POST /api/improvements`，并把 `source_feedback_refs` 指向反馈记录。四阶段产物通过 `/api/improvements/{improvement_id}/attribution/generate`、`/optimization-plan/generate`、`/execution/apply`、`/regression-test-design/generate` 生成，集成方在对应确认门上决策。
- Trace 集成：四阶段生成结果会返回 `generation_trace_id` / `generation_trace_url`；需要在上层系统展示详情时调用 `GET /api/langfuse/traces/{trace_id}`，不要让前端直接持有 Langfuse secret。
- 边界：归因/优化/执行/回归生成的内部机制（governor、DSPy formatter、Langfuse enrich）是底座内部，集成方只**提交反馈 + 管理改进事项 + 在确认/门禁上决策**，不直接编排治理 Agent。业务产物成功持久化后由对应端点推进事项阶段；`/lifecycle` 仅用于合法返工，不能绕过产物直接前推。

### 4.5 Workspace 测试与平台运行 — OpenAPI tag `improvements` / `agent-testing`
- 目标：把已确认的 `RegressionTestDesign` 物化为待发布版本中的 pytest 文件，并在精确 Git 提交上执行平台测试。
- 最短路径：调用 `POST /api/improvements/{improvement_id}/regression-test-design/confirm`。底座会在同一未发布 change set 的 worktree 中新增 `tests/test_feedback_*.py`、提交更新后的待发布版本并自动创建 `source=feedback_optimization` 的 `AgentTestRun`。使用 `GET /api/agent-registry/{agent_id}/test-suite?commit_sha=<sha>` 检查指定提交的测试资产；手工运行调用 `POST /api/agent-test-runs`，待发布变更运行调用 `POST /api/agent-change-sets/{change_set_id}/test-runs`；使用 `GET /api/agent-test-runs`、`GET /api/agent-test-runs/{test_run_id}` 查询结果，使用 `POST /api/agent-test-runs/{test_run_id}/cancel` 取消。
- 固定执行：底座只执行 `python -m pytest -q -p agentgov_testkit.pytest_plugin tests`。创建运行可省略 `commit_sha`，但底座会在请求内固定当时版本，不会在稍后执行时重新取“最新”。
- 边界：`workspace/tests/` 是测试内容唯一真相源。集成方不能提交命令、工作目录、测试状态、报告、文件路径或 backend-owned 提交绑定。`commit_sha` 是被测版本权威标识；`suite_digest` 是派生摘要，`change_set_id` 是业务关联。服务重启后 running 记录变为 `interrupted`，queued 重新入队，临时测试 session 返回明确不可用错误；运行超时以 `error/AGENT_TEST_RUN_TIMEOUT` 记录。

### 4.6 版本发布与回滚 — OpenAPI tag `feedback`
- 目标：把确认的改动发布为新版本，可回滚。
- 最短路径：`/api/agent-change-sets/...`（`diff`/`file-diff`/`approve`/`reject`/`publish`）；`/api/agent-releases/...`（`restore`/`rollback`）；`/api/agent-repository/...`（`snapshot`/`current`/`discard-changes`，可选 `?agent_id=` 指定业务 Agent，默认 `security-operations-expert`）。
- 边界：版本治理按 `agent_id` 落到各业务 Agent 自己的 per-Agent 版本库。普通发布必须存在同一业务 Agent、当前待发布 `commit_sha` 上通过的平台测试运行；旧提交通过不能放行新提交。强制发布必须填写非空原因并持久化原阻塞项和警告，provenance 不完整不可绕过。审批/发布的业务决策在上层；底座负责执行、记录、审计与原子性。

### 4.7 资产沉淀与跨 Agent 复用 — OpenAPI tag `assets`
- 目标：复用方法论、执行和审计资产；测试文件始终随对应业务 Agent Workspace Git 管理。
- 最短路径：`GET/POST /api/assets`（通用类型仅 `methodology`、`execution`、`audit`，可按 `agent_id`/`asset_type` 过滤）；`POST /api/assets/{asset_id}/inherit` 继承到另一个 Agent。Asset Registry 可以投影测试文件、平台运行、改进事项、变更集和发布之间的关系，但不能复制测试正文或成为第二写入源。

### 4.8 定制业务 Agent 的行为（workspace / Claude Code 配置）
- 目标：给某个业务 Agent 定制 prompt / 角色边界、skills、subagents、规则、MCP 工具与权限——即它的 Claude Code workspace 配置。
- **在线资产闭环**：`POST /api/agent-registry/{agent_id}/workspace/export` 同步导出当前 Git tree；`POST /api/agent-registry/{agent_id}/workspace/import` 原样新建或覆盖；`POST /api/agent-registry/{agent_id}/workspace/restore` 把历史 tree 恢复为一个新 commit。覆盖使用 expected current commit 做 CAS，活跃 turn 或未终结 change set 会返回 `409`。导入、恢复和 dirty export snapshot 都绑定实际 per-Agent Git commit，并在下一 turn 生效；不创建第二套 import/export operation 状态机。
- **包边界**：workspace 包是单个顶层 `workspace/` 的 `.tar.gz`。普通文件和二进制按字节保留，可包含 `.env`、真实 endpoint、MCP header 或其他私有运行配置；平台只拒绝路径逃逸、`.git`、特殊 tar 成员、重复冲突、资源超限和已知 JSON 语法错误。导出包因此应按敏感资产保管，不得进入公开仓库或日志。
- **配置位置 = 该业务 Agent 的运行卷 Workspace**：`${HOST_RUNTIME_VOLUME_ROOT}/data/business-agents/<agent_id>/workspace/`（容器内 `/data/business-agents/<agent_id>/workspace`；`<agent_id>` 是 Workspace 包导入路径中的 id，其 `workspace_dir` 也可由 `GET /api/agent-registry` 查到）。可定制：
  - `CLAUDE.md`（角色/边界/SOP）、`.claude/skills/<skill>/SKILL.md`、`.claude/agents/*.md`、`.claude/rules/*`、`.mcp.json`（工具接入，并同步 `.claude/settings.json` 权限）。
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

- **新集成稳定主路径**：`POST /v1/responses`、`GET /v1/responses/{response_id}`、`/v1/conversations*`、`POST /v1/agentgov/confirmation-requests/{request_id}/decision`；反馈闭环、Agent 注册、版本治理等领域能力继续使用 OpenAPI 声明的 `/api/*` 资源接口。
- **演进中**（接入需关注变更）：业务 Agent 多租与隔离、审批外移细化、改进治理工作台的用户主流程术语。
- OpenAPI 与生成类型是契约边界；底座变更公开 API 时会同步 OpenAPI 与迁移说明。集成方应以 OpenAPI 版本为对接基线，并对 `4xx/5xx` 做稳健处理。
- `POST /v1/responses` 是单 endpoint 双响应媒体类型：`stream=false` 返回 `application/json` 的 Responses 对象；`stream=true` 返回 `text/event-stream` 的 Responses-style SSE。
- 运维验收应同时检查运行容器 `/openapi.json` 的 `info.version` 与仓库 `VERSION` / 镜像 tag 一致；版本不一致优先按部署镜像或容器未 recreate 的漂移处理。

## 6. 集成反模式（请不要这么做）

- **读会话历史时传 `agent_id`**：归属是 backend-owned，由底座按 `conversation_id` 解析；传入会与真实归属冲突。
- **在 AgentGov 内做高风险动作的人审批**：审批划归外部业务系统（见愿景与生产化清单）；底座只记录 operator/reason/审计事件。
- **绕过 OpenAPI 自造 schema**：客户端类型应由 OpenAPI 生成，避免 schema 双轨漂移。
- **把 `main-agent` 特殊化**：它只是普通历史示例；默认、内置和受保护属性当前属于 `security-operations-expert`，且必须分别判断。
- **在上层另存一份会话/消息副本**：会话事实的单一真相源是 agent 的 SDK transcript，按 `conversation_id` 向底座取，不要并行存储。
- **用 `session_id` 或 `sdk_session_id` 当新集成的会话 URL id**：二者是 AgentGov/SDK 内部关联值；新集成使用 `conversation_id`。

## 7. 历史兼容附录

以下接口仅为已有调用方保留，不是新集成入口，也不再承载新增 AgentGov 控制面能力：

| 兼容接口 | 新集成替代路径 | 说明 |
| --- | --- | --- |
| `POST /api/chat` | `POST /v1/responses`，`stream=false` | 旧非流式 ChatRequest/ChatResponse 包装。 |
| `POST /api/chat/stream` | `POST /v1/responses`，`stream=true` | 旧 SSE 事件名与 payload 兼容面。 |
| `/api/sessions*` | `/v1/conversations*` | 旧 session/offset 历史读取契约。 |
| `POST /v1/chat/completions` | `POST /v1/responses` strict mode | 仅面向既有 OpenAI Chat Completions 客户端。 |

兼容接口当前仍由 OpenAPI 或运行时提供，但新客户端不得以它们建立新的控制面依赖。迁移后使用 `conversation_id`、Responses 标准事件和 canonical HITL decision 路径；旧接口的删除需另行完成消费者确认与迁移公告。

## 8. AI 辅助集成（可选）

若上层系统本身用 Claude Code / Codex 开发，可安装 AgentGov 提供的可分发集成 skill（见仓库 `integrations/agentgov-integration/`），让集成方的开发 Agent 直接掌握上面的旅程与边界。该 skill 派生自本指南，本指南为单一真相源。
