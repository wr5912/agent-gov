# AgentGov 集成指南

> 文档角色：面向上层业务系统集成 AgentGov 的当前权威参考。
>
> 字段、状态码和请求示例的唯一真相源是运行实例的 `/openapi.json` 与 `/docs`；本文只说明
> 跨接口旅程、对象关系、所有权和安全边界。术语以
> [AgentGov 术语与版本边界](./AgentGov术语与版本边界.md) 为准。

## 1. 当前架构与接入边界

AgentGov 已原子切换到 AgentScope Runtime，当前只有一条生产执行路径：

| 服务 | 职责 | 外部可见性 |
| --- | --- | --- |
| `agent-gov-api` | Agent 身份、版本、会话/run 映射、反馈、改进和发布治理 | 唯一业务 API 入口 |
| `agent-gov-ui` | Playground 与治理工作台 | 只调用 AgentGov API |
| `agentscope-runtime` | AgentScope 会话、消息、工具与 Harness 执行 | 仅 Compose 内网 |

这里描述代码中的执行路径。具体部署是否完成切换，须按
[Runtime 替换实施基线与验收](./engineering/AgentGov_AgentScope_Runtime替换实施基线与验收.md)
核验该构建的真实运行证据。

浏览器和外部系统不得直连 AgentScope Runtime 管理面。模型与 MCP 凭据只注入
`agentscope-runtime`；AgentGov API 只持有 Runtime 共享密钥和可选的 Langfuse 查询凭据。
Runtime 通过 OTLP 写入 Langfuse，前端只经 AgentGov API 读取受控 trace 引用与完整性状态。

除健康检查外，公共 API 使用：

```http
Authorization: Bearer <API_KEY>
```

缺失或错误 token 返回 `401`。不要把前端开发用 token 当作面向不可信用户的认证方案；公网部署
应在 AgentGov 前增加 TLS、身份认证和访问控制。

当前 Compose 契约仅支持单租户 operator 控制面，不提供跨用户数据隔离或横向授权。
API、UI 和 Langfuse 宿主端口默认绑定 loopback；部署脚本和受支持的 Make 启动入口对非 loopback 绑定 fail closed，
必须对目标服务显式设置对应的 `*_ALLOW_PUBLIC_BIND=1`。该开关只是风险确认，不替代
TLS、身份认证、访问控制或真正的多租户边界。

## 2. 核心对象与标识符

```text
business Agent + immutable version/Harness
                    │
                    ▼
                 session ──多轮──▶ run ──▶ reply
                                      │
                                      └────▶ OTel trace
```

| 标识 | 所有者 | 生命周期 | 主要用途 |
| --- | --- | --- | --- |
| `agent_id` | AgentGov | 长期 | 被治理业务 Agent 身份 |
| `agent_version_id` | AgentGov | 不可变版本 | 固定一次会话使用的 Harness 版本 |
| `session_id` | AgentScope | 多轮 | canonical messages 与会话状态 |
| `run_id` | AgentGov | 一次触发 | 取消、反馈、审计和运行证据锚点 |
| `reply_id` | AgentScope | 一次回复/暂停点 | 回复、人工确认或外部执行关联 |
| `trace_id` | OTel | 一次运行 | Langfuse 语义轨迹查询 |

这些值用途不同，不要求相同，也不能互相推导。`GET /api/agent-runs/{run_id}` 返回当前已知的
`session_id`、`reply_ids`、`trace_id`、Agent 版本、Harness digest 和运行终态；
`GET /api/agent-runs/{run_id}/trace` 进一步解析同一运行的 Langfuse trace。

会话在创建时绑定 Agent 版本和 Harness digest，后续发布不会热改已有会话；新版本只影响新会话。

## 3. 最短运行旅程

### 3.1 选择业务 Agent

调用 `GET /api/agent-registry` 获取业务 Agent。原生 schema 表单和 Workspace 包是两种输入，
但都只创建同一种 Git 候选：

- `GET /api/runtime/agent-schema`
- `GET /api/agent-registry/{agent_id}/native-candidate-source`
- `POST /api/agent-registry/{agent_id}/native-candidate`
- `POST /api/agent-registry/{agent_id}/workspace/import`
- `POST /api/agent-registry/{agent_id}/workspace/export`
- `POST /api/agent-registry/{agent_id}/workspace/restore`

导入包必须只有一个顶层 `workspace/`，且 `agent.yaml` 中的 Agent ID 与路径参数逐字一致。
平台拒绝路径逃逸、symlink、`.git`、特殊 tar 成员、资源超限和已知无效配置。
创建或导入成功只表示“候选已保存、尚未发布”；回执包含 `change_set_id`、基准提交和候选提交。
原生表单读取 source 时，有未完成候选就返回该候选的安全投影，`current_commit_sha` 表示候选提交；
否则读取当前发布版本并令该字段表示 live 提交。首次编辑只提交
`expected_current_commit_sha`；继续编辑同一候选则必须成对提交 `change_set_id` 与
`expected_candidate_commit_sha`，且不得再提交 live SHA。候选已存在、SHA 过期、归属不符或已进入终态时
分别以 `409` 显式拒绝，不新建第二个候选，也不覆盖他人的变化。
调用方随后对该精确候选运行平台测试，按敏感路径完成审批，再调用
`POST /api/agent-change-sets/{change_set_id}/publish`。发布命令负责创建并验证原生 Agent、提交版本绑定
和活动 Git 指针；不存在独立 Runtime 启用步骤。

Workspace 导出只读取已发布活动仓库的 Git HEAD：请求开始时固定完整 commit SHA，再从 Git
对象库打包；不快照 live 文件、不暂存或提交修改，也不包含未发布候选、dirty 或 untracked 文件。
未发布的 `draft` Agent 返回 `409 WORKSPACE_NOT_PUBLISHED`，不得用导出来隐式完成首次发布。
响应头 `X-Agent-Commit-SHA`、`X-Workspace-Package-SHA256` 和 `X-Workspace-Tree-SHA256`
分别标识精确提交、下载包字节及 Workspace 内容树，调用方应保留这些版本证据。

### 3.2 创建版本固定的会话

先调用 `GET /api/runtime/agents/{agent_id}/current` 查询业务 Agent 的当前发布绑定，并要求
`provisioned=true` 后取得 `runtime_agent_id`。若未激活，应重试同一个发布命令或处理其明确的恢复状态，
不能由客户端另行创建 Runtime Agent。该查询路径中的 `agent_id` 是业务 Agent ID；下述会话和 chat
请求的同名字段则使用返回的 Runtime ID。

```http
POST /api/runtime/sessions/
Idempotency-Key: <由调用方生成且仅用于本 Agent 的稳定键>
Content-Type: application/json

{"agent_id":"<runtime_agent_id>","name":"业务复核"}
```

成功返回 `session_id`，并在 `X-AgentGov-Session-Id` 响应头再次给出。重试相同
`Idempotency-Key` 会复用同一创建意图；同一个 key 不得跨 Agent 使用。

调用方不能在此请求中指定 model、credential、Workspace、权限规则或任意 Harness 内容。这些值由
AgentGov 根据已发布版本解析并传给 Runtime，额外字段会在请求边界被拒绝。

### 3.3 订阅事件并触发一次 run

先订阅会话事件流：

```http
GET /api/runtime/sessions/{session_id}/stream?agent_id={runtime_agent_id}
Accept: text/event-stream
```

然后触发运行：

```http
POST /api/runtime/chat/
Content-Type: application/json

{
  "agent_id":"<runtime_agent_id>",
  "session_id":"<session_id>",
  "input":{
    "id":"<本次用户动作生成的稳定原生消息ID>",
    "name":"user",
    "role":"user",
    "content":[{"type":"text","text":"请复核已提供的业务材料并说明依据"}]
  }
}
```

chat JSON 仅有原生 `agent_id`、`session_id`、`input` 三个字段；业务关联通过反馈接口的
`entities` 提交，不向 chat body 增加 AgentGov 治理字段。`input` 使用 AgentScope 原生消息、
消息列表或事件结构；客户端在一次用户动作建立时生成显式 `id`，同一动作重试复用这些 ID。
新的用户动作必须使用新的 ID，即使文本相同也不能按文本去重。

响应头 `X-AgentGov-Run-Id` 是本次运行的权威 `run_id`；
`X-AgentGov-Session-Id` 必须与请求的根会话一致。响应 body 保留 AgentScope 原生结构与额外字段，
其中 `session_id` 在子执行恢复时可以是 worker Session，不能改写成根 Session，也不能据此判断归属错误。
Gateway 建连后先发送一个无业务语义的
SSE comment `:\n\n`，使浏览器在上游空闲心跳前即可确认事件流已经建立；其后才是 AgentScope
`AgentEvent` 的原始字节代理。除这一前导 readiness comment 外，Gateway 不插入、重命名或重编码事件：
调用方应按 `type` 做前向兼容分派，保留事件顺序和未知事件，业务展示可以跳过不认识的类型，
原始事件面板仍应可查看；不能把未知事件当成成功终态。最终消息事实以 messages API 为准，
不应从浏览器气泡另建一份权威 transcript。

若初次响应是否送达不确定，先通过原生输入身份查询：

```http
GET /api/agent-runs/by-input-identity?agent_id={runtime_agent_id}&session_id={root_session_id}&operation_kind=initial&input_id={native_message_id}
```

身份由 Runtime Agent、根 Session、操作种类和有序原生输入 ID 共同确定；消息列表以重复的
`input_id` 查询参数保持原顺序。已找回 run 时只恢复该 run 的查询和 SSE 监控，不重发初始输入。
尚未找到时，只有全部输入都有显式 ID 才能使用完全相同的请求重试；同一身份携带不同内容会被拒绝。
没有显式 ID 的原生输入可以单次提交，但不能自动重试，也没有上述身份查询的恢复保障。
网络、超时、解码或 5xx 结果不能直接判失败或另起运行；必须保持待核对。刷新后通过
`GET /api/agent-runs/{run_id}/pending-actions` 恢复仍在等待的人工确认或外部执行项。

### 3.4 读取消息、状态和运行终态

- `GET /api/runtime/sessions/{session_id}/messages?agent_id={runtime_agent_id}`：分页读取 canonical messages。
- `GET /api/runtime/sessions/{session_id}/status?agent_id={runtime_agent_id}`：读取 AgentScope 会话状态。
- `GET /api/agent-runs/{run_id}`：读取 AgentGov run 状态与标识映射。
- `GET /api/runtime/sessions/?governance_agent_id={agent_id}`：按业务 Agent 列出各发布版本的受管会话。

run 可能依次处于 `queued`、`running`、`waiting_human`、`waiting_external`、`finalizing`，最终进入
`succeeded`、`failed`、`cancelled` 或 `interrupted`。只有 terminal 状态才能作为闭环证据；
`finalizing` 表示 Runtime 已产出回复但 canonical message/receipt 尚未完成持久化，不能提前判成功。
`REPLY_END` 只表示一条 reply 结束；多 reply、Session 持久化和 team 子执行仍可能在处理中，
必须以精确 `run_id` 的终态查询为准。

### 3.5 受控确认与外部执行恢复

AgentScope 需要人工确认或外部执行时，会在同一 SSE 中发出原生暂停事件，run 分别进入
`waiting_human` 或 `waiting_external`。调用方把事件给出的 `reply_id` 和逐项结果作为 AgentScope
原生 `USER_CONFIRM_RESULT` 或 `EXTERNAL_EXECUTION_RESULT` 输入，再次提交到
`POST /api/runtime/chat/`，继续同一个 run。

续跑仍只提交上述三个原生字段。根暂停事件使用根 Runtime Agent 和根 Session；
worker 暂停事件必须使用 pending action 返回且经后端绑定校验的
`runtime_agent_id` 与 `session_id`，不得回退到 leader 或由客户端猜测。原生事件的 `id`
标识本次决策，`reply_id` 和逐项 action/tool ID 由暂停事件提供。该操作的身份查询
也使用同一执行 Session/Runtime Agent，种类分别是 `user_confirmation` 或
`external_execution`；响应不确定时复用原事件 ID 查询，不能另造一个决策。

单次允许和拒绝都不附带权限范围头。只有用户明确选择“本次运行允许”时，才对
`USER_CONFIRM_RESULT` 发送 `X-AgentGov-Confirmation-Scope: run`。后端管理本次 run 内的临时授权，
其他输入不得携带该头；它不能替代已发布 Harness 的权限边界。

浏览器只能提交本次决策，不得附带或修改持久化 permission rules。允许的工具、MCP 和授权策略由
已发布 Harness 控制；AgentGov 不维护 Claude SDK/HITL 兼容入口。

### 3.6 取消与清理

- 优先使用 `POST /api/agent-runs/{run_id}/cancel` 精确取消一次 run。
- `POST /api/runtime/sessions/{session_id}/interrupt?agent_id={runtime_agent_id}` 用于中断会话当前运行。
- run 到达 terminal 后，可调用
  `DELETE /api/runtime/sessions/{session_id}?agent_id={runtime_agent_id}` 删除会话。

客户端网络断开不等于后端取消。取消后应继续查询精确 `run_id`，直到看到持久化 terminal 状态，
再允许同一会话发起下一次普通输入。

## 4. Trace 与 Langfuse

`GET /api/agent-runs/{run_id}/trace` 是从运行到轨迹的首选入口。返回的 `trace_status`：

- `pending`：尚未取得并完成对账的轨迹；查询失败也保留此状态，继续轮询。
- `complete`：run 已 terminal，唯一 `agentgov.run` 根和完整轨迹图已结束，并与持久化的
  run/version/Harness、reply 集合、team、tool、HITL/外部执行、模型属性及内容指纹逐项一致。
- `incomplete`：触发/取消结果不确定、持久化/恢复发现证据缺失，或终态后的观测对账窗口
  已到期且仍没有完整轨迹。后台对账默认窗口为 60 秒；控制面故障可在 run 尚未 terminal 时
  直接标记不完整，不必等待该窗口。不得用局部事件冒充完整轨迹。

Trace 只能从已授权的 AgentGov run 调用 `GET /api/agent-runs/{run_id}/trace` 查询；不提供仅凭
`trace_id` 读取 Langfuse payload 的通用路由，前端也不得持有 Langfuse secret。公开响应只包含
`run_id`、`trace_id`、`trace_url` 与 `trace_status`，完整性校验使用的 observation 瞬时视图不会返回
浏览器。标准安全语义 trace 包含精确根名 `agentgov.run`，AgentScope 子 span 名归一化为
`invoke_agent`、`chat` 和按实际执行出现的 `execute_tool`；被拒绝的 tool call 不会伪造执行 span。
名称不携带 Agent、模型或正文。工具终态以 AgentGov durable receipt 为准，Trace 使用 session、
tool call ID 和父 `invoke_agent` 的 reply 证明实际执行归属。
根 observation 携带 run、Agent 版本、Harness 与根 session；reply 关联由 stage 和
`invoke_agent` observation 承载。原始输入输出及 tool/MCP 参数不进入观测持久层，出口保留
受控语义属性、精确 UTF-8 长度与 SHA-256。反馈可先提交，自动改进需等待 run terminal 且
`trace_status=complete`。细节及未纳入的示例应用设计见
[观测契约及验收](./engineering/AgentScope与Langfuse观测契约及验收.md)。

## 5. 反馈、改进与发布闭环

上层系统通过 `POST /api/feedback-signals` 提交反馈信号，通过 `POST /api/feedback-events`
提交通用业务事件；事件类型 `event_type` 是非空开放字符串，不是 SOC 固定枚举。
`source_system` 标识业务来源系统，SOC 只是其中一种业务来源，不另设平行事件 API。
事件重试必须复用相同 `event_id` 与规范化后的不可变请求；精确重试返回 `duplicate`。若同一
`event_id` 携带不同内容，API 返回 HTTP 409、`error_code=FEEDBACK_EVENT_ID_CONFLICT`，且原事件
和原待关联记录保持不变。等价 RFC 3339 时区写法及实体顺序不会制造伪冲突。
旧版本已补齐关联字段而尚无请求指纹的事件，只在首次不冲突重试时绑定该请求；后续与新事件一样
执行严格比较。`event_id`、`source_system` 必须含非空白字符，自由 JSON 不接受 `NaN` 或
`Infinity`；这些输入在 HTTP 422 边界被拒绝且不落库。

来源、事件、信号、Run 和反馈 Case 的业务对象引用统一为 `entities: Record<string, string[]>`，
例如 `{"document":["document-1"],"case":["business-case-1"]}`。查询业务对象时成对传入
`entity_type` 与 `entity_id`；`entities.case` 表示业务系统自己的 case，绝不能填治理事项 ID。
`feedback_case_id` 则是 AgentGov 治理反馈 Case 的身份，和业务对象引用分开保存、查询与展示。
事件输入中的 `run_id`、`session_id` 是关联线索；`agent_id`、`matched_run_id` 由后端解析，
同一 Session 有多轮运行时不能猜测“最近一次 run”。

改进事项来源响应中的 `feedback_case_id` 只从已有归属关系派生；`source_events` 是该反馈批次
已有事件的只读列表，每项包含 `event_id`、`source_system`、`event_type`。一个来源可以关联多个事件，
前端应保留全部来源，不把它们压成单个事件，也不让用户或模型伪造这些派生字段。
现有 `source` 字段仍描述反馈来源类别，不替代事件的来源系统与类型。随后进入受控改进：

1. `POST /api/feedback-signals` 收集反馈，`POST /api/feedback-cases` 形成处置对象。
2. `POST /api/feedback-cases/{feedback_case_id}/evidence-packages` 固化不可变证据。
3. `POST /api/improvements` 创建改进事项。
4. 依次使用 `/normalized-feedback/generate`、`/attribution/generate`、
   `/optimization-plan/generate`、`/execution/apply`、`/regression-test-design/generate` 生成产物，
   并在对应 `/confirm` 入口完成决策。
5. 对精确 candidate commit 执行 `POST /api/agent-change-sets/{change_set_id}/test-runs`。
6. 审批后调用 `POST /api/agent-change-sets/{change_set_id}/publish`。发布响应不确定或发生可恢复的
   部分失败时，以同一 `change_set_id` 重入该命令并查询同一 release；不得另行切换活动 Git 指针。
   若要恢复历史行为，应把所需历史内容形成新的候选，重新完成测试、审批和同一发布流程。

治理 Agent 也经同一 AgentScope Runtime 运行。生成产物不能绕过确认、测试和发布门，也不能直接
修改活动 Harness。高风险动作的业务授权由上层系统负责；AgentGov 负责执行、状态机、审计与原子性。

## 6. Harness 与秘密边界

业务 Agent 的活动 Harness 位于其版本化 Workspace，原生资产为：

- `agent.yaml`：身份、模型引用、权限模式和 Workspace 策略。
- `AGENT.md`：系统指令与行为边界。
- `skills/**/SKILL.md`：技能。
- `subagents/<name>/agent.yaml`、`subagents/<name>/AGENT.md`：子智能体。
- `mcp/*.json`：MCP 定义；秘密只通过 `credential_refs` 引用。
- `tests/`：与版本绑定的回归测试。

聊天输入不能修改 model、credential、权限模式、工作目录或活动 Harness。受控改进在隔离候选
worktree 中修改资产，经静态策略、回归、人工确认后原子发布；旧会话继续使用旧 digest。
已发布 Git 版本的导出包仍可能包含敏感业务配置，应按敏感资产保管，不得写入公开仓库或日志；
只读导出不会采集当前 live Workspace 中未提交的内容。

旧 Claude Workspace 只能通过 `scripts/convert_claude_harness.py` 做一次性离线迁移；转换器不在
生产启动或运行链路中调用。MCP 迁移与凭据格式见
[MCP 替换指南](../docker/MCP_REPLACEMENT_GUIDE.md)。

## 7. 健康、错误与恢复

- `GET /health/live` 只证明 AgentGov API 进程存活，不探测 Runtime。
- `GET /health/ready` 验证 AgentScope Runtime 可达；不可达时返回 `503`。
- `GET /health` 返回 API、Runtime、依赖版本和可观测配置摘要，不暴露内部响应正文或秘密。

常见错误语义：`401` 未认证，`404` 对象不存在或跨 Agent 不可见，`409` 状态/并发冲突，
`422` 请求字段非法，`502/503` Runtime 调用失败。失败不会静默回落到离线假结果或旧 Runtime。
Session 创建和 run finalize 都有持久化意图/receipt 恢复；调用方仍应使用 Idempotency-Key 和
`run_id` 查询实现自己的安全重试。

## 8. 契约与部署验收

新集成必须以当前 OpenAPI 生成客户端类型，并对未知 SSE 事件和新增响应字段前向兼容。原子切换后
不再提供以下旧生产入口：

- `/api/agent-runtime/sdk-events`
- `/api/chat`、`/api/chat/stream`
- `/api/sessions*`
- `/v1/responses`、`/v1/conversations*`、`/v1/chat/completions`
- Claude user-input/HITL 路由与 LiteLLM sidecar

推荐验收：

```bash
make runtime-validate
make cutover-check
make container-core-smoke COMPOSE_ENV_FILE=docker/.env
REQUIRE_LIVE_RUNTIME=1 \
REAL_ACCEPTANCE_AGENT_ID=security-operations-expert \
REAL_SCENARIO_FILE=/outside/reviewed-scenarios.json \
make langfuse-smoke COMPOSE_ENV_FILE=docker/.env
make test
make typecheck
```

容器验收会从所选 env 读取 secret，但为每轮生成临时 Runtime 根、唯一 Compose project/容器前缀、
随机回环端口和全部宿主挂载，再基于当前工作树 rebuild 并 `--force-recreate` 三个核心服务。无论
成功失败都会清理临时容器、卷和目录，不触碰既有部署或 `${HOME}/volume-agent-gov`；端口可达或旧
容器仍运行都不能单独作为通过证据。

## 9. 集成反模式

- 直连或向浏览器暴露 AgentScope Runtime 管理面。
- 把 `session_id`、`run_id`、`reply_id`、`trace_id` 当成同一个值。
- 在会话/chat 请求中覆盖 model、credential、权限规则或 Harness。
- 从 SSE 局部文本推断 terminal，或把客户端断连当成后端取消。
- 在上层系统另建一份权威 transcript，与 AgentScope canonical messages 双写。
- 让前端直接持有 Langfuse secret，或让 AgentGov API 持有模型/MCP secret。
- 调用已删除的 Claude SDK、Responses、旧 Chat/Session 或 LiteLLM sidecar 路径。
