# AgentScope 与 Langfuse 观测契约及验收

> 核对日期：2026-09-11。本文依据 AgentGov 4.0.1 当前整改候选，记录观测边界与验收要求；既有 v4.0.0 tag 不代表本轮整改代码。
> 代码和测试存在不代表真实部署已经验收；本文不构成生产上线、故障恢复或性能通过报告。
> 来源材料：`agentscope-langfuse-integration-requirements.md`，原稿面向 AgentScope
> `examples/agent_service` / `examples/web_ui`，基线为 AgentScope 2.0.8、Langfuse v4.32.0 草案。

本文供 Runtime、控制面和验收维护者阅读。调用方先读
[AgentGov 集成指南](../AgentGov集成指南.md)，Runtime 替换范围与全局验收门见
[AgentScope Runtime 替换实施基线与验收](./AgentGov_AgentScope_Runtime替换实施基线与验收.md)。
公开字段以当前 OpenAPI 为准；本文不新增 API、配置项或数据存储。

## 1. 来源采纳与边界裁决

原稿针对另一仓库的示例服务，直接复制会引入与 AgentGov 不同的身份、正文、安全和部署契约。
本次保留可复用的公共扩展与验收原则，按当前代码改写实现描述，不导入原稿全文。

| 原稿要求 | 本仓库处理 | 理由或当前边界 |
| --- | --- | --- |
| 公共 Middleware 与标准 OTel，无 Core patch | 采纳 | Runtime 通过 AgentScope 公共入口组合 middleware；升级仍须检查公共能力 |
| 一次逻辑 reply 对应一个 trace | 改写 | 一个 AgentGov `run_id` 对应一个 `trace_id`；一个 run 可包含多个 `reply_id` |
| 每个 segment 自成根，不跨人工等待持有 span | 改写 | 当前只有一个 `agentgov.run` 根，initial、HITL、外部执行为 stage，根在 terminal 确认后结束 |
| 保存完整 prompt、输出、工具参数和结果 | 不纳入 | Runtime 出口仅保留允许的语义属性、正文 UTF-8 字节长度与 SHA-256 |
| Collector 持久队列前过滤秘密 | 改写 | 当前在 Runtime exporter 前过滤；仓库没有该草案的 Collector 栈或持久队列 |
| Redis 双向 TraceIndex | 不纳入 | AgentGov run 与持久化控制回执已经提供关联与对账事实，不另建一份索引 |
| Feedback Outbox 与 Langfuse Score 改评 | 不纳入 | 反馈进入 AgentGov 治理对象；当前未实现该草案的 Score 投影队列 |
| 30 天保留、异步删除与索引 TTL | 不纳入 | 当前未实现该草案的 retention 清理服务，不能声称自动按 30 天删除 |
| v4.32.0、Observations v2、Scores v3、无 SDK | 不纳入当前基线 | Compose 默认锁定 Langfuse `3.225.7` 的 web/worker manifest digest，当前通过 Python SDK 查询；v4 兼容尚未由本文验证 |
| `/observability/v1`、专属开关与 TraceSheet | 不纳入 | 复用 AgentGov run/trace 入口，不复制原稿 API、环境变量或前端布局约定 |
| 基于 owner 的多用户隔离 | 改写 | 当前是单租户 operator 控制面，不提供原稿的多租户授权承诺 |
| 原始 AgentEvent/SSE 与语义 trace 分离 | 采纳 | SSE 未知事件继续原样透传；Langfuse 不承担会话恢复或逐 delta replay |
| 100% sampling 与调用完整性 | 保留为验收要求 | 当前 Provider 未显式固定 sampler，不能仅凭默认配置声明全环境 100% 采样 |
| fresh runtime、安全 canary、故障与性能证据 | 采纳原则 | 具体运行范围和门槛以本仓库验收入口与替换基线为准 |

本次选择避免重复事实源和额外秘密落盘。若未来确需 v4、正文调试存储、Collector 或 Score 同步，
应单独确认必要性、所有权和删除周期，再提交实现与验收；不能把原稿的建议直接解释为已支持。
变更 trace 身份、内容边界、查询协议或 Langfuse 大版本时，必须重新核对此表。

## 2. 当前事实源与运行关系

| 对象 | 事实所有者 | 观测侧用途 |
| --- | --- | --- |
| Session、Message、AgentState | AgentScope | 会话恢复与消息查询；不从 trace 重建权威消息 |
| Agent 版本和 Harness | Git 与不可变 Runtime Agent 绑定 | 确认本次运行实际使用的发布版本 |
| run、回执、pending action、Team ledger | AgentGov | 提供受治理运行身份、终态与观测对账要求 |
| OTel span / Langfuse observation | Runtime 导出、Langfuse 保存 | 调用级语义轨迹，属于派生观测 |

`session_id` 可跨多轮；`run_id` 标识一次受管顶层执行；`reply_id` 标识 AgentScope 回复；
`trace_id` 标识该 run 的语义轨迹。HITL continuation 复用 run，不能把这些 ID 相互冒充。
控制面预分配 trace ID，Runtime 用它创建真实根 span；不能仅把关联 ID 写入 metadata 就称为同一 trace。

```text
AgentGov run / trace_id
└─ agentgov.run（一个根）
   ├─ agentgov.run.stage（initial / HITL / external continuation）
   │  └─ invoke_agent → chat / execute_tool
   └─ agentgov.run.stage（同一 run 下的 Team 子会话执行）
      └─ invoke_agent → chat / execute_tool
```

图示表示主要观测层级，不规定每次运行都必须发生工具或子 Agent 调用。
真实场景应发生哪些调用，由回执、action 与 Team ledger 决定，不能让 trace 自报清单证明自身完整。

代码入口：[run root 与 stage](../../agentscope_runtime/run_trace.py)、
[TraceContext Middleware](../../agentscope_runtime/trace_context_middleware.py)、
[Middleware 装配](../../agentscope_runtime/service.py)。

Middleware 在每次 `anext` 前 attach context，随后 detach 再 yield，避免异步事件串线。
stage 可先结束；根等待控制面 terminal 确认后结束，重复确认幂等。Runtime 退出会关闭仍活动的根；
这不代表可以从崩溃的 model/tool 指令中点继续，也不等于强杀场景已验证零丢失。

Runtime 的普通控制回执调度器是进程内队列，不是 durable outbox。它用稳定
`receipt_id` / `event_id` 和控制面幂等写入对抗重放；调用协程取消后，已调度任务仍由
tracked task 继续投递。优雅关闭时依次关闭 Storage、有界刷新控制回执、收口 trace、
关闭 provider；刷新超时或永久错误会使 lifespan 失败并记录不含正文和凭据的错误。

`SIGKILL`、进程崩溃、容器或主机突然丢失不执行上述 flush，未 ACK 的内存回执可能丢失；
不得把协程取消保护表述为进程崩溃后耐久。此时依赖 AgentGov 控制面的 durable run/fence：
Runtime boot reconciliation 先对 active run 请求中断，再对所有绑定 Session 执行连续两次真实
idle 观测后 fail-closed 收敛。该恢复链只确保 run 不永久假挂和 fence 不提前释放，
不承诺重建崩溃窗口中未持久的 Message、回执或 OTel span。

## 3. 内容、凭据与传输

当前 [OTel 出口](../../agentscope_runtime/observability.py) 使用 `RedactingSpanProcessor`
处理已结束 span，再送入 `BatchSpanProcessor` 与标准 OTLP HTTP exporter。
只有 `LANGFUSE_ENABLED=true` 才初始化该出口；开启但摄取配置缺失时启动失败，关闭时即使保留
OTLP endpoint/header 也不初始化 exporter。Provider 的 flush/shutdown 由 Runtime 生命周期管理。

出口规则如下：

- span 名归一化为 `agentgov.run`、`agentgov.run.stage`、`invoke_agent`、`chat`、`execute_tool`
  等受控名称，名称不拼接 Agent、模型或业务正文。
- 允许属性包括 run/session/reply/版本关联、模型与 provider 标识、usage、工具调用 ID 和终态等。
- prompt、模型输出、工具定义、参数和结果原文不导出，只生成对应 UTF-8 字节长度和 SHA-256。
- 未知属性、span events、links 和错误描述不进入该安全副本；Resource 也按允许列表过滤。

长度/hash 只用于对账，不能恢复正文，也不能替代 AgentScope canonical messages。
原始内容仍可能被 AgentScope 在进程内构造；本文不声明它从未进入内存。
任何“秘密不进入日志、队列、Langfuse 或浏览器构建”的结论还需要逐边界 canary 实测。

| 消费方 | 当前凭据边界 | 配置与验证入口 |
| --- | --- | --- |
| AgentScope Runtime | 持有模型/MCP 凭据及 OTLP 摄取配置；摄取与 API 查询复用同一对项目 key，不表示权限隔离；不接收登录、盐、加密或存储密码 | Runtime settings、OTLP 导出与脱敏测试 |
| AgentGov API | 仅把可选 Langfuse 项目凭据用于查询、不执行写入，但该 key 同时供 Runtime 摄取，凭据本身没有只读降权；不接收模型/MCP secret | API settings、trace 查询与错误投影 |
| 浏览器 | 经 AgentGov API 读取受控 trace 引用与完整性状态，不持有 Langfuse secret | API 响应、DOM 与构建产物检查 |
| Langfuse profile | 保存脱敏语义 observation，其基础设施凭据保持私有 | Compose 渲染摘要与真实 trace smoke |

容器部署通过 `COMPOSE_ENV_FILE` 选择一份完整 env；宿主调试、容器和前端环境各有边界，
不能把本机调试结果当作容器验收。现有键和挂载以
[核心 Compose](../../docker/docker-compose.yml)、[可选 Langfuse Compose](../../docker/docker-compose.langfuse.yml)
与 [README](../../README.md#langfuse-与-otel) 为准。普通参数用默认值，项目初始化 key 和 Runtime Basic
认证由同一对私有 key 派生；首次生成与无损精简分别使用 `make langfuse-env` 和
`scripts/initialize_langfuse_env.py --compact`，不得借配置精简轮换已有数据的凭据。
不要复制原稿 `AGENTSCOPE_OBSERVABILITY_*`、端口、独立 Redis 或 Collector 配置。

当前未显式向 `TracerProvider` 传入 sampler。验收要求受治理场景不漏采，但实际采样行为仍须结合
OTel SDK、运行环境和真实调用数量核实。当前也没有本地 Collector 持久队列；flush 只代表尝试发送，
不能据此承诺 Langfuse 已可读或 Runtime 强杀后零丢失。

## 4. 查询、完整性与反馈门

唯一公开查询入口是 `GET /api/agent-runs/{run_id}/trace`。控制面先读取并授权该 AgentGov run，
不提供仅凭 `trace_id` 读取 Langfuse payload 的通用路由；前端不直接访问 Langfuse secret API。
公开响应只包含 run/trace 关联、受控链接和完整性状态，不返回 observation、input、output、event、
body 或上游 metadata。该查询路径不新增原稿的 `/observability/v1`、reply 查询、retention 或
Score 协议。

[查询客户端](../../app/runtime/integrations/runtime_langfuse.py) 当前使用 Langfuse SDK 的
`trace.get`，只请求 `core,observations`，随后按受控 trace/observation 字段和精确语义属性
allowlist 做第二次正向投影；因此不能把上游 `fields` 参数当成隐私边界。投影后的瞬时内部视图
只供完整性校验和受控证据摘要使用，公开 API 不复用该 payload。查询客户端忽略宿主机代理环境，
避免本地 Langfuse Basic 查询凭据被意外发送给外部代理。
查询异常返回脱敏的失败状态与错误类型。Compose 将 Langfuse web/worker 成对锁定为
`3.225.7` 的多架构 manifest digest，并锁定 Postgres、ClickHouse、Redis 与 MinIO 的本轮
验证 digest；私有 web/worker 覆盖必须成对且显式版本一致。验收仍须记录本机解析后的平台镜像 digest，不能由
原稿 v4 文档推导本实例的 API 兼容性。

`trace_status` 与 run 业务状态分开：

| 状态 | 当前语义 |
| --- | --- |
| `pending` | 尚未完成观测对账；run terminal 也可处于此状态 |
| `complete` | terminal run 的 Langfuse 轨迹通过与控制面持久事实的完整性校验 |
| `incomplete` | 触发/取消结果不确定、持久化/恢复或有界对账已判定证据不完整；此时 run 也可能尚未 terminal |

[完整性校验](../../app/runtime_gateway/trace_validation.py) 当前至少要求：

1. trace ID、run ID、session 与控制面 expectations 一致，expectations 自身完整。
2. 恰好一个已结束的 `agentgov.run` 根；全部 observation 已结束，父子图连通且没有跨 trace 混入。
3. 根的 Agent、发布版本、Harness digest、Runtime 版本、session 与 terminal reason 匹配。
4. 根会话 stage 的 reply 集合与持久化 `reply_ids` 精确一致，不能把 worker reply 混入根回复。
5. 每个 durable Team 子会话有对应 stage 与 invoke；每个 durable 工具终态有匹配的工具 span。
6. HITL/外部执行 action 有对应 request 身份；已 resolved action 还必须有续跑决策证据。
7. 存在根 invoke、模型调用及 model/provider 标识，并有合法 input/output 长度与 SHA-256 指纹。

这些要求按实际控制事实校验，不要求没有工具调用的场景凭空生成工具 span。
仅有根 span、成功 flush、Trace URL 或 HTTP 200 都不足以使 `trace_status=complete`。
expectations 由 [run 查询存储](../../app/runtime_gateway/_store_run_queries.py) 从回执、action
和 Team ledger 派生；[回执 Middleware](../../agentscope_runtime/receipt_middleware.py) 提供控制身份。

[后台 reconciliation](../../app/runtime_gateway/trace_reconciliation.py) 默认使用终态时间起
60 秒窗口。未取得结果（`None`）或取得但内容不全时，期限前保持 pending，到期标 incomplete；
网络异常或显式 `fetch_status=failed` 只计查询失败并保持 pending，不套用这个不完整截止判定。
各 run 查询互相隔离。后台只扫描 pending terminal run；
[run trace 路由](../../app/runtime_gateway/router.py) 也使用同一校验器处理查询结果。

用户提交反馈无需等待 trace complete；自动改进分析要求来源 run 已 terminal 且 trace complete。
这一门由 [Governor 服务](../../app/services/improvement_governor_service.py) 执行。
反馈的当前事实属于 AgentGov，不代表已同步成 Langfuse Score。

## 5. 测试入口与验收要求

下表列出当前测试资产及其用途；它证明存在可运行的检查入口，不记录本次通过数。

| 验证主题 | 当前资产 |
| --- | --- |
| 同 run 根、HITL stage、终态幂等与 trace 冲突 | [test_agentscope_run_trace.py](../../tests/test_agentscope_run_trace.py) |
| 无正文回执、工具终态、HITL/external 身份 | [test_agentscope_trace_receipts.py](../../tests/test_agentscope_trace_receipts.py) |
| 从持久事实生成观测期望 | [test_runtime_trace_expectation_store.py](../../tests/test_runtime_trace_expectation_store.py) |
| 根图、reply、Team、tool、action 和指纹负向验证 | [test_runtime_trace_validation.py](../../tests/test_runtime_trace_validation.py) |
| 查询失败脱敏、公开投影与访问边界 | [test_langfuse_query_client.py](../../tests/test_langfuse_query_client.py) |
| Runtime 生命周期、正文脱敏与持久化回执 | [test_agentscope_runtime_service.py](../../tests/test_agentscope_runtime_service.py) |
| 回执重试、取消、响应丢失重放与有界关闭 | [test_runtime_control_receipt_delivery.py](../../tests/test_runtime_control_receipt_delivery.py) |
| smoke 绑定本轮 run 和语义关联 | [test_langfuse_smoke_contract.py](../../tests/test_langfuse_smoke_contract.py) |

真实观测验收走公共入口
`REQUIRE_LIVE_RUNTIME=1 REAL_ACCEPTANCE_AGENT_ID=security-operations-expert
REAL_SCENARIO_FILE=/outside/reviewed-scenarios.json make langfuse-smoke COMPOSE_ENV_FILE=docker/.env`。
该入口从仓库外复核文件选择一个 `success` 场景触发真实 AgentScope run，需要可用的模型与 OTLP 私有配置；
[smoke 脚本](../../scripts/langfuse_smoke.py) 校验本轮 run 的语义 trace，而非任选历史记录。
公开 AgentGov API 不返回 observation payload；脚本只在隔离验收进程中使用私有凭据查询 Langfuse，
并在校验前复用控制面的正向字段投影。
容器构建、force-recreate、临时卷隔离与清理遵循根 README 的验收 runner 契约。

| 场景 | 应保存的成功或失败证据 |
| --- | --- |
| 普通回复、多 reply、HITL/外部执行 | 本轮 run/session/reply/trace 关系、根与 stage、action 请求及续跑身份 |
| model/tool/Team | 持久事实与实际 observation 一一核对；缺失、串线、重复身份不能判 complete |
| Langfuse 延迟与不可用 | query failure、pending 和 incomplete 的区别，以及恢复后真实可读结果 |
| Runtime 退出或强杀 | 已提交消息、回执、root 和 exporter 的时间线；如有损失，明确受影响窗口 |
| 未知 SSE 事件 | 数据面原样透传且不误判 terminal；观测 allowlist 不代表可过滤 SSE |
| 浏览器 trace 展示 | 加载、完整、失败/不完整状态；不得把失败回显成无 trace 或完整成功 |
| 安全 canary | 伪造秘密在 exporter 出口、日志、Langfuse、API、DOM/构建产物中均无泄漏 |
| 采样与性能 | 同一场景开关观测的原始样本、调用数、耗时与资源数据，不引用另一主机旧统计 |

安全 canary 应覆盖 prompt、工具输入输出、MCP header 和异常；只使用测试字符串。
测试中只保留匹配位置、计数、长度/hash 和脱敏结果，不把真实秘密或业务正文带入交付证据。
当前单元测试的 canary 不能代替跨进程、真实 Langfuse 和浏览器验证。

50 个实质不同输入、Chromium 与 Firefox 各连续 3 次等全局门统一见
[Runtime 替换验收基线](./AgentGov_AgentScope_Runtime替换实施基线与验收.md)，本文不另定一套阈值。
原稿的 100 Session × 10 reply、v4 Score 跨日改评和 retention 删除测试不自动成为本期通过声明。

每次验收至少记录代码与工作树版本、AgentScope/OTel 依赖版本、实际镜像 digest、脱敏配置摘要、
命令与时间、fresh run 标识、对账结果和失败原因。证据要区分“已有检查入口”“本轮执行通过”与
“尚无真实运行证据”；缺模型配置、版本不兼容或资源不足时保留明确失败/受阻结论。

## 6. 本基线的已知边界

- 本轮运行验收范围见 [Runtime 替换验收基线 §6.3](./AgentGov_AgentScope_Runtime替换实施基线与验收.md#63-当前能力边界)。
  AgentGov `v4.0.0` tag 和当前 4.0.1 整改候选均不代表 Langfuse v4；Langfuse v4 API、生产故障恢复、保留期与吞吐目标尚未验证。
- `all-up`、容器 healthy 或 Web 登录成功都不等于真实 OTLP 摄取与查询可用。任何 Langfuse 恢复通过声明必须在
  仓库外冷备上共同演练 PostgreSQL、ClickHouse、MinIO 与 Redis，并保留原 `LANGFUSE_ENCRYPTION_KEY`
  的可恢复性；只恢复数据库或更换加密 key 不能视为完整恢复。当前尚无这组生产恢复证据。
- 自助注册关闭等身份面设置必须先按当前锁定的 Langfuse `3.225.7` 实际配置核验，不能照搬另一大版本的变量名；
  私有身份、salt、认证和存储加密值只保留在所选私有 env／备份中，不进入源码、日志或本文。
- Collector、Redis TraceIndex、Score Outbox、自动清理和多租户授权均非本文声明的现有能力。
- Langfuse 不接管 AgentScope 消息事实，也不反向驱动运行状态；观测不完整会阻止自动改进取证。
- 若依赖升级改变公共 Hook、采样、属性、查询结构或异步可见时间，应重跑对应契约与真实 smoke，
  并修订当前说明；不能添加私有 API、核心补丁或静默降级来维持表面成功。
