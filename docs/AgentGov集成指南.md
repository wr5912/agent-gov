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
| 会话 | 持有会话事实（SDK session transcript 为权威源）、按 `session_id` 投影历史 | 展示对话、回放气泡、组织业务级会话 |
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
- **session**：产品对话标识是 `session_id`（由 API 创建或客户端传入）。响应里的 `sdk_session_id` 是内部 SDK resume id，**不是**产品会话 id，集成方一律用 `session_id`。
- **run**：一次运行，带 `run_id`，是反馈与归因的归属锚点。
- **会话正文**：权威源是 agent 自己的 SDK session transcript；底座按需投影，**不另存副本**。集成方也不应缓存一份并行会话存储（会双轨漂移）。
- **所有权（重要）**：
  - `agent_id`（会话归属哪个 Agent）是 **backend-owned**：由底座在运行时落库。**集成方读历史时不传 `agent_id`**，只凭 `session_id`（见 §6）。
  - 反馈/评估/版本按 `run_id` / `session_id` / `agent_id` 归属到对应 Agent 与 version。

## 4. 集成旅程（任务式）

> 每个旅程给“目标 + 最短路径（调用哪些 operation）+ 边界提示”。具体字段、状态码、schema 以 OpenAPI 对应 tag 为准。

### 4.1 选择 / 创建业务 Agent — OpenAPI tag `agents`
- 目标：拿到要运行的业务 Agent。
- 最短路径：`GET /api/agent-registry` 列出；`GET /api/agent-registry/templates` 查创建模板 catalog；`POST /api/agent-registry` 创建（可选 `template_id` 选模板，默认 `general`，未知值 → 422；非法 `agent_id` 如含路径分隔符/穿越 → 422）；`POST /api/agent-registry/{agent_id}/lifecycle` 切换生命周期；`GET /api/agents`、`GET /api/skills` 查能力目录。
- 边界：`main-agent` 是预制业务 Agent，开箱即用、无需创建；但 `/api/chat`、`/api/chat/stream` 仍要求显式传 `agent_id`（含 `main-agent`），不再支持省略跑 main（缺失 → 422，见 §4.2）。

### 4.2 运行一次对话 — OpenAPI tag `chat` / `openai-compatible`
- 目标：让 Agent 处理一条消息/任务。
- 最短路径：
  - 非流式：`POST /api/chat`，体含 `message`（必填）、可选 `session_id`（省略则 API 创建）、`agent_id` **必填有效**（`main-agent` 或已注册业务 Agent；缺失 → 422，未知 → 404）。响应含 `run_id`、`session_id`、`answer`、`messages`、`agent_activity`、`usage`。
  - 流式：`POST /api/chat/stream`（`text/event-stream` Server-Sent Events），`agent_id` 同样**必填有效**（与 `/api/chat` 口径一致，不静默跑 main）。
  - OpenAI 兼容：`POST /v1/chat/completions`（便于复用现成 OpenAI 客户端）。OpenAI 标准请求无 `agent_id` 字段，故该入口运行**运营者配置的出口 Agent**（经 `GET/PUT/DELETE /api/settings/openai-compat-agent` 配置或前端设置 UI 选择；**未配置时默认走 main**，与显式选 `main-agent` 是不同状态）；要按请求指定 Agent 用上面的 `/api/chat`、`/api/chat/stream` 或 `/v1/responses` control mode。
- 边界：工具权限、MCP、skills、subagents 以 Claude Code 官方配置为准；`skills_mode`/`allowed_tools`/`disallowed_tools` 对 SDK 执行已废弃，不要依赖。续聊用同一 `session_id`。

#### 4.2.1 流式 Web HITL 人工确认卡

`ENABLE_CLAUDE_WEB_HITL=true` 时，Web 人工确认随流式业务 Agent 运行生效；非流式 `/api/chat` 不承载确认卡，并对所有实际权限询问 fail-closed。集成方如果要在自己的聊天界面展示“人工确认卡”，必须把 `/api/chat/stream` 当成带暂停点的 SSE 状态机，而不是普通文本流。

最短集成流程：

1. `POST /api/chat/stream`，保持 SSE 连接直到 `done`，并按 `request_id` 幂等处理事件。
2. 正常渲染 `session`、`message`、`result`、`done`；忽略但保留连接活性的 `heartbeat`。
3. 收到 `claude_user_input_required` 时，渲染内联确认卡；不要关闭 SSE 连接，因为 Claude SDK 正在等待该决策。
4. 用户决策后，调用 `POST /api/claude-user-input-requests/{request_id}/decision`，请求体带 OpenAPI 定义的上下文校验字段与 `decision_token`。
5. 继续读取原 SSE 流；收到 `claude_user_input_resolved` 后，将同一张卡片更新为已处理。

确认类型：

- `tool_permission`：工具授权卡。动作是 `allow_once`、`allow_for_run`、`deny`。`allow_for_run` 只对已判定的低风险类别生效，绑定 `business_agent_id + run_id + low-risk category`，不跨类别、不跨 run、不写入永久权限配置；高风险或未分类请求不接受该动作。
- `ask_user_question`：Claude 主动向用户澄清。动作是 `answer_question`，可提交结构化选项，也可提交自然语言 `response`。

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

### 4.3 回放会话历史 — OpenAPI tag `sessions`
- 目标：刷新/重开旧会话时重建对话气泡。
- 最短路径：`GET /api/sessions` 列会话；`GET /api/sessions/{session_id}/messages` 取该会话全部消息（SDK transcript 投影）。返回 `messages[]`（每条含 `uuid`/`role`/`parent_tool_use_id`/`blocks`，块为 `thinking`/`text`/`tool_use`/`tool_result`，`tool_use.id` 与 `tool_result.tool_use_id` 可配对）与 `subagents[]`；支持 `?limit=&offset=` 分页。
- 边界：**只传 `session_id`**，不传 `agent_id`（归属由底座解析）；会话尚无 transcript 返回空 `messages`，未知会话 `404`。

### 4.4 提交反馈并驱动闭环 — OpenAPI tag `feedback` / `improvements`
- 目标：把用户/系统反馈喂回闭环，产出归因、优化、执行候选和回归用例。
- 最短路径：`POST /api/feedback-cases` 提交反馈案例（可挂 `run_id`/`session_id`/`alert_id`/`case_id`）；`POST /api/feedback-cases/{id}/evidence-packages` 补证据；随后创建或选择改进事项 `POST /api/improvements`，把 `source_feedback_refs` 指向反馈记录。四阶段产物通过 `/api/improvements/{improvement_id}/attribution/generate`、`/optimization-plan/generate`、`/execution/apply`、`/regression-assessment/generate` 生成，集成方在对应确认门上决策。
- Trace 集成：四阶段生成结果会返回 `generation_trace_id` / `generation_trace_url`；需要在上层系统展示详情时调用 `GET /api/langfuse/traces/{trace_id}`，不要让前端直接持有 Langfuse secret。
- 边界：归因/优化/执行/回归生成的内部机制（governor、DSPy formatter、Langfuse enrich）是底座内部，集成方只**提交反馈 + 管理改进事项 + 在确认/门禁上决策**，不直接编排治理 Agent。

### 4.5 评估与回归 — OpenAPI tag `agents` / `automation`
- 目标：用例化评估候选改动、固化回归资产。
- 最短路径：`/api/eval-cases`、`/api/eval-runs`（含 `impact-analysis`）；`/api/regression-assets/...`（`promote`/`archive`/`mark-flaky`/`supersede`/`revisions`/`governance-events`）。
- 边界：回归门禁结果由底座产出；集成方可读取并据业务规则决定是否放行。

### 4.6 版本发布与回滚 — OpenAPI tag `agents`
- 目标：把确认的改动发布为新版本，可回滚。
- 最短路径：`/api/agent-change-sets/...`（`diff`/`file-diff`/`approve`/`reject`/`publish`）；`/api/agent-releases/...`（`restore`/`rollback`）；`/api/agent-repository/...`（`snapshot`/`current`/`discard-changes`，可选 `?agent_id=` 指定业务 Agent，默认 `main-agent`）。
- 边界：版本治理按 `agent_id` 落到该业务 Agent 自己的 **per-agent 版本库**（main-agent 与各业务 Agent 相互隔离）；审批/发布的**业务决策**在上层；底座负责执行、记录、审计与原子性。

### 4.7 资产沉淀与跨 Agent 复用 — OpenAPI tag `assets`
- 目标：复用方法论/执行/数据资产。
- 最短路径：`GET/POST /api/assets`（按 `agent_id`/`asset_type` 过滤）；`POST /api/assets/{asset_id}/inherit` 继承到另一个 Agent。

### 4.8 定制业务 Agent 的行为（workspace / Claude Code 配置）
- 目标：给某个业务 Agent 定制 prompt / 角色边界、skills、subagents、规则、MCP 工具与权限——即它的 Claude Code workspace 配置。
- **配置位置 = 该业务 Agent 的运行卷 workspace**：`${HOST_RUNTIME_VOLUME_ROOT}/data/business-agents/<agent_id>/workspace/`（容器内 `/data/business-agents/<agent_id>/workspace`；`<agent_id>` 即 `/api/agent-registry` 创建时的 id，其 `workspace_dir` 也可由 `GET /api/agent-registry` 查到）。可定制：
  - `CLAUDE.md`（角色/边界/SOP）、`.claude/skills/<skill>/SKILL.md`、`.claude/agents/*.md`、`.claude/rules/*`、`.mcp.json`（工具接入，并同步 `.claude/settings.json` 权限）。
  - 该 `workspace/` 是这个 Agent 的**活配置层**（git 就地版本化的版本源）；与它**并列**在 `<agent_id>/` 下的还有 `claude-root/`（Claude SDK 运行态）与 `version/`（per-agent 版本治理工件：worktrees/releases）——去嵌套后运行态天然不进版本源。优化闭环跑的是候选 worktree、发布归档落该 Agent 自己的 `version/`，都不在 `workspace/` 内。
  - **统一模型**：所有业务 Agent——含**预制的 main-agent**、运营预置的 AAA/BBB、以及动态注册的——都住 `data/business-agents/<id>/workspace/`，同一套 profile 与版本治理。运营**预置在种子里**的业务 Agent（`data/business-agents/<id>/`，目录名即 `agent_id`）会在应用启动时被自动发现并幂等登记进注册表，随即可由 `GET /api/agent-registry` 查到、被 `/api/chat` 路由，无需逐个再调创建接口。唯一特殊的是治理 Agent `governor`（顶层 `governor-workspace/` + `claude-roots/governor`，治理所有业务 Agent，不对集成方暴露为可编排对象）。定制 / 优化目标都是该 Agent `workspace/` 下的可编辑文件。
- **不要用 `docker/runtime-volume-seeds/` 定制已存在 Agent**：那是 AgentGov 渲染运行卷初始态的**种子**（`governor-workspace/`、预制 `data/business-agents/<id>/workspace/`、`templates/business-agent/` 创建模板；Compose 默认只读挂载当前 checkout，镜像内保留一份兜底），不是某个业务 Agent 的运行配置；改它只影响后续新建/重渲染，定制不到已存在的 Agent。定制某个具体 Agent 一律改其运行卷 `workspace/`。
- **新增预制业务 Agent 不再要求重建镜像**：在已使用当前 Compose 配置 recreate 过容器后，新增 `docker/runtime-volume-seeds/data/business-agents/<agent_id>/workspace/` 后执行 `make up`；API 启动协调器会从只读 seed 播种缺失 workspace、初始化该 Agent 的 Git 版本源并写入运行 receipt，API 健康后 worker 才启动。修改 Dockerfile、Python 代码、bootstrap 渲染逻辑或依赖仍需重建镜像。
- **种子 = 出生配置，运行卷 = 活优化态（强制不可覆盖）**：bootstrap 对 `data/business-agents/*` 只做**workspace 存在性对账**——种子里新增的预置 Agent（卷里缺失）会被渲染补全并自动登记，已存在 workspace 不逐文件回灌。平台受管的 Bash/MCP/sandbox 及专用安全运营契约只通过协调器迁移：工作树必须干净且没有未终结 change set，迁移会生成 per-agent Git 快照，否则阻断启动。配套：种子预置的业务 Agent 是**声明式基线**，UI 与 `DELETE /api/agent-registry/{id}` **禁删**；用户创建的非种子 Agent 可 tombstone 删除。
- 边界与注意：
  - **不要碰** 与 workspace **并列**的 `version/`（该 Agent 的版本治理工件）与 `claude-root/`（运行态 Claude 状态）；改它们会破坏版本治理 / 运行态。
  - `workspace/` 同时是 Agent 的 git 版本源——直接改是工作树未提交改动；要固化成版本走版本治理（change set / release，见 4.6）。
  - 权限：通用业务 Agent 的 `Bash(*)` 基线放在 `ask`，仅按低风险类别授予本次 run 权限；MCP 写入/处置工具如需在线人审，也放入 `ask` 并通过流式运行 + `ENABLE_CLAUDE_WEB_HITL=true` 承载。配置文件里不写 api_key / token / 凭据。
  - 工具 / MCP / skills / subagents 以 Claude Code 官方配置为准（见 §6），不通过 chat 入参接管；改完可能需重启该 Agent 的 Claude Code 或重渲染受管配置才生效。

### 观测
- `GET /health` 健康检查与运行态字段；运行 trace 经 Langfuse（自托管，内网）观测，集成方可据 `run_id` 关联。

## 5. 契约稳定性与版本

- **稳定面**（可放心长期依赖）：`/v1` OpenAI 兼容、`/api/chat`、`/api/chat/stream`、`/api/sessions*`、`/api/feedback-cases`、`/api/agent-registry`、`/health`。
- **演进中**（接入需关注变更）：业务 Agent 多租与隔离、审批外移细化、改进治理工作台的用户主流程术语。
- OpenAPI 与生成类型是契约边界；底座变更公开 API 时会同步 OpenAPI 与迁移说明。集成方应以 OpenAPI 版本为对接基线，并对 `4xx/5xx` 做稳健处理。
- `POST /v1/responses` 是单 endpoint 双响应媒体类型：`stream=false` 返回 `application/json` 的 Responses 对象；`stream=true` 返回 `text/event-stream` 的 Responses-style SSE。
- 运维验收应同时检查运行容器 `/openapi.json` 的 `info.version` 与仓库 `VERSION` / 镜像 tag 一致；版本不一致优先按部署镜像或容器未 recreate 的漂移处理。

## 6. 集成反模式（请不要这么做）

- **读会话历史时传 `agent_id`**：归属是 backend-owned，由底座按 `session_id` 解析；传入会与真实归属冲突。
- **在 AgentGov 内做高风险动作的人审批**：审批划归外部业务系统（见愿景与生产化清单）；底座只记录 operator/reason/审计事件。
- **绕过 OpenAPI 自造 schema**：客户端类型应由 OpenAPI 生成，避免 schema 双轨漂移。
- **把 `main-agent` 当长期边界**：它是第一阶段样板，长期治理对象是业务 Agent。
- **在上层另存一份会话/消息副本**：会话事实的单一真相源是 agent 的 SDK transcript，按 `session_id` 向底座取，不要并行存储。
- **用 `sdk_session_id` 当会话 id**：它是内部 resume id，产品对话 id 一律用 `session_id`。

## 7. AI 辅助集成（可选）

若上层系统本身用 Claude Code / Codex 开发，可安装 AgentGov 提供的可分发集成 skill（见仓库 `integrations/agentgov-integration/`），让集成方的开发 Agent 直接掌握上面的旅程与边界。该 skill 派生自本指南，本指南为单一真相源。
