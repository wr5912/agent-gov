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
| 运行 | 跑被治理的业务 Agent / main agent，产出 run、session、trace | 决定何时跑、传入业务上下文、承载业务工作流 |
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

- **Agent**：集成方编排的是**业务 Agent**（被治理的长期对象，经 `/api/agent-registry` 注册/查询）。其中 `main-agent` 是**预制业务 Agent**（开箱即用样板，同样在注册表中），运营/集成方可再注册更多业务 Agent；唯一特殊的是治理 Agent `governor`（治理所有业务 Agent，不对集成方暴露为可编排对象）。
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
- 最短路径：`GET /api/agent-registry` 列出；`GET /api/agent-registry/templates` 查 generic template 与声明 seed catalog；`POST /api/agent-registry` 创建，可选互斥的 `template_id` 或 `source_seed_id`，两者同时提供返回 `422`。`source_seed_id` 从声明 seed 原样跨 ID 复制，平台不重写源 workspace 内的身份表述；两者都不提供时，目标 ID 有同名声明 seed 则使用它，否则使用 `general`。非法 `agent_id`（路径分隔符/穿越等）或未知来源返回 `422`。`POST /api/agent-registry/{agent_id}/lifecycle` 切换生命周期；`GET /api/agents`、`GET /api/skills` 查能力目录。
- 创建一致性：`POST /api/agent-registry` 只有在来源目录安全复制、workspace 落盘、Git 初始化并完成注册表 finalize 后才返回 `201`；内部 `provisioning` reservation 不会被 list/get/chat 看见。文件或 finalize 失败会补偿注册表并只清理本次创建的文件，预存 workspace 不覆盖、不整目录删除；来源中的 symlink、路径穿越、`__pycache__`/字节码缓存，以及 workspace 目标路径中的任一 symlink 均 fail closed。reservation 使用 15 分钟 heartbeat 租约，新 API 进程不会恢复仍在落盘的创建；过期任务由启动检查和每分钟 reconciliation 恢复旧 tombstone，或转为不可见 tombstone 后允许安全重试。
- 边界：`main-agent` 是预制业务 Agent，开箱即用、无需创建；新集成通过 `/v1/responses` control mode 的 `agentgov.agent_id` 显式选择业务 Agent（含 `main-agent`），缺失返回 `422`，未知或不可运行 Agent 返回对应领域错误。

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

- `/api/chat/stream` 使用 `event: prompt_suggestion`，data 为 `{suggestion, run_id, session_id}`。
- `/v1/responses` control 模式使用 `event: agentgov.prompt_suggestion` 和既有 `{v,type,run_id,ts,seq,payload}` 信封，payload 为 `{suggestion, session_id}`；strict 模式不输出该扩展事件。
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

#### 4.2.2 RO 两阶段响应处置

响应编排服务（RO）通过 `/v1/responses` control mode 调用 `security-operations-expert`，并使用独立的服务间 Bearer token `RESPONSE_ORCHESTRATOR_API_KEY`。该 token 必须与普通 `API_KEY` 不同，不得交给浏览器或写入 `VITE_*` 配置。

- `agentgov.phase=proposal`：只生成整本处置剧本提案，要求非空 `agentgov.case_id`，不接受审批/执行绑定，不产生 SOC 写副作用。
- `agentgov.phase=approved_execution`：要求 `stream=true`、Web HITL 可用，并同时提供非空 `approval_request_id`、64 位小写 SHA-256 `playbook_digest` 和唯一 `execution_run_id`。相同审批或执行 ID 只能消费一次，重放返回 `409`。
- 执行阶段只有精确的 `mcp__sec-ops__soc_api__create` 与 `mcp__sec-ops__soc_api__manual` 会形成 RO 内部 HITL 请求。RO 必须逐次核对工具名和完整参数，再用同一专用凭据提交 `allow_once + updated_input`；普通 API key 无权决策，run 级授权无效。
- 临时剧本是 `create -> manual`；已发布剧本可直接 `manual`。claim 只有在 `manual` 被授权且流成功结束后才完成；`soc_api__execute`、`AskUserQuestion` 与其他 mutation 均拒绝。
- RO token 只允许上述受控创建、受保护决策，以及按需 `POST /v1/conversations` 创建会话映射；不能读取/列举/删除响应或会话，也不能调用其他普通 API。普通 token 不能伪造 `phase` 或 backend-owned metadata。

### 4.3 创建与回放会话 — OpenAPI tag `openai-conversations`
- 目标：刷新/重开旧会话时重建对话气泡。
- 最短路径：`POST /v1/conversations` 可预创建会话；`GET /v1/conversations` 列出会话；`GET /v1/conversations/{conversation_id}` 读取元数据；`DELETE /v1/conversations/{conversation_id}` 删除会话映射；`GET /v1/conversations/{conversation_id}/items` 从 SDK transcript 投影历史。
- items 返回 `data[]`，每项包含 `id`、`role`、`parent_tool_use_id`、`content`；内容块保留 `thinking`、`text`、`tool_use`、`tool_result` 等 SDK 事实。分页使用 cursor 风格 `after`、`limit`、`order`、`include`，不使用 offset。
- 边界：只传 `conversation_id`，不传 `agent_id`；归属由底座解析。会话尚无 transcript 时 `data` 为空；读取未知会话或其 items 返回 `404`，删除未知映射返回 `deleted=false`。历史数据若 `agent_id` 为空但已经存在 `turns` 或 `sdk_session_id`，底座无法从该映射唯一证明 owner，会 fail-closed 返回 `409`；集成方应新建会话，不得指定 Agent 抢占，也不得依赖底座猜测或静默迁移。会话正文继续来自 SDK transcript，后端不另建消息副本。

### 4.4 提交反馈并驱动闭环 — OpenAPI tag `feedback` / `improvements`
- 目标：把用户/系统反馈喂回闭环，产出归因、优化、执行候选和回归用例。
- 最短路径：先创建或识别已有的 `signal`、`soc_event` 或已解析的 `pending_correlation`，再调用 `POST /api/feedback-cases`。请求体必须包含至少一个 typed 引用，且所有来源必须归属同一业务 Agent，例如 `{"source_refs":[{"source_kind":"signal","source_id":"sig-..."}],"title":"...","priority":"medium"}`。`run_id`/`session_id`/`alert_id`/`case_id` 由底座从被引用来源投影，不是该请求的顶层字段；旧 `source_ids`、空 `source_refs` 和空白 `source_id` 均会被拒绝。随后通过 `POST /api/feedback-cases/{id}/evidence-packages` 补证据，创建或选择改进事项 `POST /api/improvements`，并把 `source_feedback_refs` 指向反馈记录。四阶段产物通过 `/api/improvements/{improvement_id}/attribution/generate`、`/optimization-plan/generate`、`/execution/apply`、`/regression-assessment/generate` 生成，集成方在对应确认门上决策。
- Trace 集成：四阶段生成结果会返回 `generation_trace_id` / `generation_trace_url`；需要在上层系统展示详情时调用 `GET /api/langfuse/traces/{trace_id}`，不要让前端直接持有 Langfuse secret。
- 边界：归因/优化/执行/回归生成的内部机制（governor、DSPy formatter、Langfuse enrich）是底座内部，集成方只**提交反馈 + 管理改进事项 + 在确认/门禁上决策**，不直接编排治理 Agent。业务产物成功持久化后由对应端点推进事项阶段；`/lifecycle` 仅用于合法返工，不能绕过产物直接前推。

### 4.5 评估与回归 — OpenAPI tag `assets` / `feedback`
- 目标：把已确认的回归评估候选采用为 typed TestDataset，并对候选改动执行不可变数据集快照。
- 最短路径：`POST /api/improvements/{improvement_id}/test-dataset/adopt` 采用后，将数据集审计激活为 `active`；候选 ChangeSet 使用 `POST /api/agent-change-sets/{change_set_id}/regression-runs` 执行精确绑定的回归。结果为 `review_required` 时，使用 `POST /api/agent-change-sets/{change_set_id}/regression-runs/{eval_run_id}/review` 对全部待复核 case 提交逐条决策。查询与审计入口为 `GET /api/test-datasets`、`GET /api/test-datasets/{dataset_id}`、`POST /api/test-datasets/{dataset_id}/lifecycle`、`GET /api/test-datasets/{dataset_id}/revisions`、`GET /api/eval-runs`、`GET /api/eval-runs/{eval_run_id}`；`POST /api/eval-runs` 只用于不参与发布门禁的手工评估。
- 边界：TestDataset 的 Agent 归属、事项、execution、候选版本和有序用例由底座绑定；只有 `active` 数据集可启动，运行期间的 `evaluating` 由底座原子管理。事项返工形成新证据链后重新采用会生成新 dataset 版本，旧快照不改写。review 必须携带唯一 `review_id`、操作人、原因、`scope=current_eval_run` 和完整 case 决策；只允许处理自然语言语义待复核项，不得覆盖 required check 失败。底座在同一事务更新 EvalRun、ChangeSet 和审计事件，保留原始 item 证据。集成方不能提交全局用例 ID、客户端执行结果或 backend-owned change set/run 绑定。

### 4.6 版本发布与回滚 — OpenAPI tag `feedback`
- 目标：把确认的改动发布为新版本，可回滚。
- 最短路径：`/api/agent-change-sets/...`（`diff`/`file-diff`/`approve`/`reject`/`publish`）；`/api/agent-releases/...`（`restore`/`rollback`）；`/api/agent-repository/...`（`snapshot`/`current`/`discard-changes`，可选 `?agent_id=` 指定业务 Agent，默认 `main-agent`）。
- 边界：版本治理按 `agent_id` 落到该业务 Agent 自己的 **per-agent 版本库**（main-agent 与各业务 Agent 相互隔离）；审批/发布的**业务决策**在上层；底座负责执行、记录、审计与原子性。

### 4.7 资产沉淀与跨 Agent 复用 — OpenAPI tag `assets`
- 目标：复用方法论、执行和审计资产；TestDataset 通过专用 typed API 管理。
- 最短路径：`GET/POST /api/assets`（通用类型仅 `methodology`、`execution`、`audit`，可按 `agent_id`/`asset_type` 过滤）；`POST /api/assets/{asset_id}/inherit` 继承到另一个 Agent。旧 `regression` 类型和字符串化 `test_dataset` 均被拒绝。

### 4.8 定制业务 Agent 的行为（workspace / Claude Code 配置）
- 目标：给某个业务 Agent 定制 prompt / 角色边界、skills、subagents、规则、MCP 工具与权限——即它的 Claude Code workspace 配置。
- **在线资产闭环**：`POST /api/agent-registry/{agent_id}/workspace/export` 同步导出当前 Git tree；`POST /api/agent-registry/{agent_id}/workspace/import` 原样新建或覆盖；`POST /api/agent-registry/{agent_id}/workspace/restore` 把历史 tree 恢复为一个新 commit。覆盖使用 expected current commit 做 CAS，活跃 turn 或未终结 change set 会返回 `409`。导入、恢复和 dirty export snapshot 都绑定实际 per-Agent Git commit，并在下一 turn 生效；不创建第二套 import/export operation 状态机。
- **包边界**：workspace 包是单个顶层 `workspace/` 的 `.tar.gz`。普通文件和二进制按字节保留，可包含 `.env`、真实 endpoint、MCP header 或其他私有运行配置；平台只拒绝路径逃逸、`.git`、特殊 tar 成员、重复冲突、资源超限和已知 JSON 语法错误。导出包因此应按敏感资产保管，不得进入公开仓库或日志。
- **配置位置 = 该业务 Agent 的运行卷 workspace**：`${HOST_RUNTIME_VOLUME_ROOT}/data/business-agents/<agent_id>/workspace/`（容器内 `/data/business-agents/<agent_id>/workspace`；`<agent_id>` 即 `/api/agent-registry` 创建时的 id，其 `workspace_dir` 也可由 `GET /api/agent-registry` 查到）。可定制：
  - `CLAUDE.md`（角色/边界/SOP）、`.claude/skills/<skill>/SKILL.md`、`.claude/agents/*.md`、`.claude/rules/*`、`.mcp.json`（工具接入，并同步 `.claude/settings.json` 权限）。
  - 该 `workspace/` 是这个 Agent 的**活配置层**（git 就地版本化的版本源）；与它**并列**在 `<agent_id>/` 下的还有 `claude-root/`（Claude SDK 运行态）与 `version/`（per-agent 版本治理工件：worktrees/releases）——去嵌套后运行态天然不进版本源。优化闭环跑的是候选 worktree、发布归档落该 Agent 自己的 `version/`，都不在 `workspace/` 内。
  - **统一模型**：所有业务 Agent——含**预制的 main-agent**、运营预置的 AAA/BBB、以及动态注册的——都住 `data/business-agents/<id>/workspace/`，同一套 profile 与版本治理。运营**预置在种子里**的业务 Agent（`data/business-agents/<id>/`，目录名即 `agent_id`）会在应用启动时被自动发现并幂等登记进注册表，随即可由 `GET /api/agent-registry` 查到、被 `/v1/responses` control mode 路由，无需逐个再调创建接口。唯一特殊的是治理 Agent `governor`（顶层 `governor-workspace/` + `claude-roots/governor`，治理所有业务 Agent，不对集成方暴露为可编排对象）。定制 / 优化目标都是该 Agent `workspace/` 下的可编辑文件。
- **不要用 `docker/runtime-volume-seeds/` 定制已存在 Agent**：那是运行卷初始态的**种子**（`governor-workspace/`、预制 `data/business-agents/<id>/workspace/`、`templates/business-agent/` 创建模板；Compose 默认只读挂载当前 checkout，镜像内保留一份兜底），不是某个业务 Agent 的运行配置；改它只影响后续缺失 workspace 或新建 Agent，定制不到已存在的 Agent。定制某个具体 Agent 一律改其运行卷 `workspace/` 或走上述导出/导入闭环。
- **新增预制业务 Agent 不再要求重建镜像**：在已使用当前 Compose 配置 recreate 过容器后，新增 `docker/runtime-volume-seeds/data/business-agents/<agent_id>/workspace/` 后执行 `make up`；API 启动协调器会从只读 seed 原样播种缺失 workspace、初始化该 Agent 的 Git 版本源、写入运行 receipt，并在就绪后启动 API 服务。修改 Dockerfile、Python 代码、bootstrap 复制/校验逻辑或依赖仍需重建镜像。
- **种子 = 出生配置，运行卷 = 活优化态（强制不可覆盖）**：bootstrap 对 `data/business-agents/*` 只做**workspace 存在性对账**——种子里新增的预置 Agent（卷里缺失）会被原样复制并自动登记，已存在 workspace 不逐文件回灌，也不按容器/本机模式重写 settings、MCP、hook 或 endpoint。配套：种子预置的业务 Agent 是**声明式基线**，UI 与 `DELETE /api/agent-registry/{id}` **禁删**；用户创建的非种子 Agent 可 tombstone 删除。
- 边界与注意：
  - **不要碰** 与 workspace **并列**的 `version/`（该 Agent 的版本治理工件）与 `claude-root/`（运行态 Claude 状态）；改它们会破坏版本治理 / 运行态。
  - `workspace/` 同时是 Agent 的 git 版本源——直接改是工作树未提交改动；要固化成版本走版本治理（change set / release，见 4.6）。
  - 权限与仓库边界：generic template 的 `Bash(*)` 基线放在 `ask`，仅按低风险类别授予本次 run 权限；MCP 写入/处置工具如需在线人审，也放入 `ask` 并通过流式运行 + `ENABLE_CLAUDE_WEB_HITL=true` 承载。声明 seed 跨 ID 实例化和 live workspace 导入都保留各自权限，不被 generic 基线覆盖。repo seed/template 不写真实 api_key/token、私有 header、数据库凭据或本机私有路径；live workspace 与其 per-Agent Git 可原样保存业务运行所需私有值，但导出、日志和运维操作必须按敏感资产处理。把 live workspace 纳入 repo builtin 时，先在仓库外保留逐字节候选：明确秘密和本机私有路径必须处理，非秘密 endpoint、内网地址与较宽权限只提示复核，平台不静默改写。
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
- **把 `main-agent` 当长期边界**：它是第一阶段样板，长期治理对象是业务 Agent。
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
