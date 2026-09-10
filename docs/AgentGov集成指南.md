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
Runtime 通过 OTLP 写入 Langfuse，前端再经 AgentGov API 读取 trace。

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

调用 `GET /api/agent-registry` 获取可运行的业务 Agent。普通 Agent 由 Workspace 包创建：

- `POST /api/agent-registry/{agent_id}/workspace/import`
- `POST /api/agent-registry/{agent_id}/workspace/export`
- `POST /api/agent-registry/{agent_id}/workspace/restore`

导入包必须只有一个顶层 `workspace/`，且 `agent.yaml` 中的 Agent ID 与路径参数逐字一致。
平台拒绝路径逃逸、symlink、`.git`、特殊 tar 成员、资源超限和已知无效配置。

### 3.2 创建版本固定的会话

先调用 `GET /api/runtime/agents/{agent_id}/current` 查询业务 Agent 的当前发布绑定；
若 `provisioned=false`，调用 `POST /api/runtime/agents/{agent_id}/provision` 幂等创建，取得
`runtime_agent_id`。这两个路径中的 `agent_id` 是业务 Agent ID；下述会话和 chat 请求的
同名字段则使用返回的 Runtime ID。

```http
POST /api/runtime/sessions/
Idempotency-Key: <由调用方生成且仅用于本 Agent 的稳定键>
Content-Type: application/json

{"agent_id":"<runtime_agent_id>","name":"告警复核"}
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
  "client_operation_id":"client-generated-stable-id",
  "input":{
    "name":"user",
    "role":"user",
    "content":[{"type":"text","text":"请核查当前告警并给出处置建议"}]
  },
  "alert_id":"optional-alert-id",
  "case_id":"optional-case-id",
  "metadata":{"source":"soc"}
}
```

响应头 `X-AgentGov-Run-Id` 是本次运行的权威 `run_id`；
`X-AgentGov-Session-Id` 必须与请求会话一致。SSE 是 AgentScope `AgentEvent` 的原始字节代理：
调用方应按 `type` 做前向兼容分派，保留事件顺序和未知事件，业务展示可以跳过不认识的类型，
原始事件面板仍应可查看；不能把未知事件当成成功终态。最终消息事实以 messages API 为准，
不应从浏览器气泡另建一份权威 transcript。

`client_operation_id` 标识一次逻辑提交，网络失败后重试必须复用原值。若初次响应是否送达不确定，
调用 `GET /api/agent-runs/by-client-operation?session_id=...&client_operation_id=...` 定位精确 run，
不得换一个操作 ID 重触发。刷新后通过 `GET /api/agent-runs/{run_id}/pending-actions` 恢复仍在等待的
人工确认或外部执行项。

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

续跑必须提供该次操作的 `client_operation_id` 和当前 `expected_run_id`，重试复用同一操作 ID。
普通批准默认 `confirmation_scope=once`；`run` 仅适用于 `USER_CONFIRM_RESULT`，由后端
管理本次 run 内的临时授权，不能用它替代已发布 Harness 的权限边界。

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

已知 `trace_id` 时也可调用 `GET /api/langfuse/traces/{trace_id}`。前端不得直接持有 Langfuse
secret。标准安全语义 trace 包含精确根名 `agentgov.run`，AgentScope 子 span 名归一化为
`invoke_agent`、`chat` 和按实际调用出现的 `execute_tool`；名称不携带 Agent、模型或正文。
根 observation 携带 run、Agent 版本、Harness 与根 session；reply 关联由 stage 和
`invoke_agent` observation 承载。原始输入输出及 tool/MCP 参数不进入观测持久层，出口保留
受控语义属性、精确 UTF-8 长度与 SHA-256。反馈可先提交，自动改进需等待 run terminal 且
`trace_status=complete`。细节及未纳入的示例应用设计见
[观测契约及验收](./engineering/AgentScope与Langfuse观测契约及验收.md)。

## 5. 反馈、改进与发布闭环

上层系统以 `run_id`、`session_id`、`alert_id` 或 `case_id` 提交反馈来源，随后进入受控改进：

1. `POST /api/feedback-signals` 收集反馈，`POST /api/feedback-cases` 形成处置对象。
2. `POST /api/feedback-cases/{feedback_case_id}/evidence-packages` 固化不可变证据。
3. `POST /api/improvements` 创建改进事项。
4. 依次使用 `/normalized-feedback/generate`、`/attribution/generate`、
   `/optimization-plan/generate`、`/execution/apply`、`/regression-test-design/generate` 生成产物，
   并在对应 `/confirm` 入口完成决策。
5. 对精确 candidate commit 执行 `POST /api/agent-change-sets/{change_set_id}/test-runs`。
6. 审批后调用 `POST /api/agent-change-sets/{change_set_id}/publish`；必要时通过
   `/api/agent-releases/{release_id}/restore` 或 `/rollback` 恢复。

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
导出的 live Workspace 可能包含敏感业务配置，应按敏感资产保管，不得写入公开仓库或日志。

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
