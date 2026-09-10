# AgentGov AgentScope Runtime 替换实施基线与验收

> 文档角色：Runtime 替换的工程决策、实现索引与剩余验收依据。
> 核对日期：2026-09-11。状态：4.0.0 基础运行验收已有证据，完整业务验收仍受阻，详见 §6.3。
> 来源：`AgentGov_AgentScope_Runtime替换实施方案.md`；原方案基于 AgentGov
> `b12372f` 与 AgentScope `2.0.8` / `ff8697ec4d59ee01f3766176e70cb24ee894d6c6`。
> 本文核对的是 AgentGov 4.0.0 源码基线，不把原方案 commit 当成已部署版本。

## 1. 文档归属与采纳范围

[README](../../README.md) 保持架构、启动和部署命令入口；
[集成指南](../AgentGov集成指南.md) 维护调用旅程，字段和状态码以 OpenAPI 为准。
本文保留替换原因、原方案与实现的差异、迁移边界和验收门槛；观测细节集中到
[AgentScope 与 Langfuse 观测契约及验收](./AgentScope与Langfuse观测契约及验收.md)。

| 原方案内容 | 处理 | 本文或现有入口 |
| --- | --- | --- |
| 单 Runtime、薄网关、事实源与独立 ID | 保留 | 第 2 节；README 与集成指南提供使用入口 |
| API、Middleware、前端终态与切换时序 | 按实现修订 | 第 3 节；不把方案中的接口草图当现行契约 |
| Harness 转换、不可变版本、旧数据退出 | 保留并限定作用域 | 第 4、5 节 |
| 50-run、浏览器、故障与性能指标 | 保留为验收要求 | 第 6 节；存在代码或检查器不等于通过 |
| Plan Mode、临时交付路径、旧耦合文件数量 | 不纳入 | 属一次性编写上下文，不能证明当前状态 |
| 覆盖率数值、部署命令全文 | 引用既有权威来源 | `tests/quality_policy.json`、README，避免重复维护 |

原方案中的 Claude 名称在本文只表示迁移来源或删除范围；旧设计已在
[归档索引](../archive/README.md) 登记，不恢复为并行生产方案。

## 2. 替换目的与事实所有权

要解决的问题是 AgentGov 控制面同时承担治理与 Runtime 细节，导致会话、消息、恢复和
执行协议存在重复实现。当前裁决是：所有受管执行交给固定版本 AgentScope，AgentGov
负责身份、发布绑定、准入、运行关联和反馈治理，Langfuse 提供派生观测。

| 事实或对象 | 所有者 | 边界 |
| --- | --- | --- |
| 业务 Agent 身份、版本绑定、反馈、审批、改进 | AgentGov | 以业务 Agent 和精确版本组织治理 |
| Harness、测试、候选与发布内容 | Git | Runtime Workspace 物化副本不成为发布事实源 |
| Session、Message、AgentState、最终历史 | AgentScope | AgentGov 不另存一份权威消息正文 |
| `run_id`、active-run fence、治理回执 | AgentGov | 一次顶层受管执行，覆盖人工确认与外部执行续跑 |
| `session_id`、`reply_id` | AgentScope | Session 多轮复用；一个 run 可以关联多个 reply |
| `trace_id` 与语义轨迹 | 控制面分配关联 ID，Runtime 创建 OTel span，Langfuse 保存 | 一 run 一 Trace；不以轨迹重建会话事实 |

业务 Agent、Governor 和候选测试都使用同一 Runtime 路径。项目开发者的 Codex/Claude
配置属于离线开发工具；不得因删除业务 Runtime 的 Claude 资产而删除这些项目配置。
本次替换不新建 `main` 样板或模板 catalog，内置 Workspace 仍按既有运行卷初始化规则管理。

替代方案未采用的原因：保留 Claude fallback 或消息双写会继续制造两套事实源；把
AgentScope 嵌入 API 进程会扩大模型凭据与执行权限边界；直接开放 AgentScope 管理面会
绕过已发布版本和准入。当前只通过公共 API、公共 Middleware、Workspace 和 Storage
扩展点组合；不可用的公共能力必须报告缺口，不能以核心补丁或私有导入补齐。

当前依赖由 [Runtime requirements](../../agentscope_runtime/requirements.txt) 固定为
`agentscope[service,storage-sql]==2.0.8`。升级时同时复核 Harness contract、OpenAPI、
原生事件、故障测试与浏览器流程；不能把升级简化为改一个数字。首版采用单 Runtime
副本、mounted SQLite 和进程内 MessageBus；多副本、HA、KB、Channel、Scheduler
不在本次交付中。公共扩展或部署拓扑变化时，重新评审本节边界。

## 3. 已存在的实现与原方案修订

以下是代码证据索引，不是容器或生产验收报告。

| 面向调用者的行为 | 当前实现 | 证据 |
| --- | --- | --- |
| 获取当前发布绑定 | `GET /api/runtime/agents/{agent_id}/current` 只读；`POST .../provision` 幂等创建；发布版本可尚未 provision | [router.py](../../app/runtime_gateway/router.py)、[provisioning.py](../../app/runtime_gateway/provisioning.py) |
| 创建 Session | body 为 `agent_id`、可选 `name`；这里 `agent_id` 的值是 `runtime_agent_id`；额外字段一律拒绝 | [contracts.py](../../app/runtime_gateway/contracts.py) |
| 列出 Session | `GET /api/runtime/sessions/?governance_agent_id=...` 按业务 Agent 聚合多个版本，并过滤受管 Session | [router.py](../../app/runtime_gateway/router.py) |
| 提交 chat 与恢复重试 | 必须传 `client_operation_id`；响应不确定时按同一操作 ID 查询 run；HITL 续跑还要传 `expected_run_id` | [contracts.py](../../app/runtime_gateway/contracts.py)、[操作入口](../../app/runtime_gateway/_router_operations.py) |
| 原生 SSE | 使用 `aiter_raw()` 转发，保留 frame 内容、顺序及未知事件；HTTP/TCP chunk 边界不属于等价承诺 | [router.py](../../app/runtime_gateway/router.py)、[前端解析](../../frontend/src/api/agentScopeStream.ts) |
| 精确运行终态 | `REPLY_END` 只是 reply 结束；前端按 `run_id` 查询终态，后端还核对 Message、Session 持久化和 team 子执行 | [终态存储](../../app/runtime_gateway/_store_runs.py)、[前端终态判断](../../frontend/src/playgroundRunTerminal.ts) |
| 生命周期回执 | 实际为 TraceContext、Tracing、Receipt 等 Middleware 组合；内部还有 boot、child-session、team-inbox 协调 | [service.py](../../agentscope_runtime/service.py)、[router.py](../../app/runtime_gateway/router.py) |
| 新数据库 epoch | 仅允许空库初始化或精确匹配 `agentscope-runtime-v1`；拒绝旧库与未知 schema，不在线迁移旧 Claude 数据 | [runtime_db.py](../../app/runtime/runtime_db.py) |

Session 创建的 `Idempotency-Key`、chat 的 `client_operation_id` 和治理 `run_id`
分别解决不同步骤的幂等与关联问题，不能互换。Session 一旦绑定发布版本，后续发布只影响
新 Session；已有 Session 的 model、权限、Workspace 和 Harness 不随聊天请求变更。

run 状态集合为 `queued`、`running`、`waiting_human`、`waiting_external`、`finalizing`
及终态 `succeeded`、`failed`、`cancelled`、`interrupted`。转移真相源为
[ALLOWED_RUN_TRANSITIONS](../../app/runtime_gateway/contracts.py)；它不是只能向右推进的
线性链，多 reply 协调期间 `finalizing` 可以回到执行或等待状态。上游返回 `started`、
浏览器断连、收到单个 `REPLY_END` 都不能自行决定成功或取消。

HITL 以 Session、run、reply 和 tool call 的持久身份核对原调用，拒绝修改工具名、参数或
注入持久 permission rules。产品上的单次/本 run 授权对应 `confirmation_scope=once|run`；
run 结束后临时授权失效。外部执行续跑也必须对应真实 pending action，不能靠浏览器自行
构造身份。具体请求见集成指南与 OpenAPI，规则实现在
[hitl.py](../../app/runtime_gateway/hitl.py)。

断线后的最终历史从 `/messages` 与 `/status` 恢复，精确执行状态从 run API 恢复；不承诺
`Last-Event-ID` 重放全部 transient delta。Runtime 重启不能恢复 model/tool 调用的指令中点，
必须通过回执与恢复协调得到明确终态，不能猜测成功。

## 4. Harness、旧设计退出与配置边界

新 Harness 使用 `agent.yaml`、`AGENT.md`、`skills/`、`mcp/`、`subagents/` 和 `tests/`。
身份、展示、`agentscope-app/2.0.8` contract、权限、Workspace 策略与资产 digest 进入
manifest；发布以精确 Git 内容为依据，Runtime Agent 绑定按需创建且不可原地改写。

| 处理 | 资产 | 约束与证据 |
| --- | --- | --- |
| 迁移 | `CLAUDE.md`、rules、skills、commands、subagents、MCP 和已评审 hook | [离线转换器](../../scripts/convert_claude_harness.py) 生成新 Harness；未知或不等价映射必须拒绝 |
| 删除活动入口 | 旧 Chat/Responses/Conversation、SDK events、Claude trust/resume、Runtime selector、sidecar 与专属 env | [静态切换检查](../../scripts/check_agentscope_cutover.py) 与 OpenAPI 检查；旧路径只留在归档、转换及负向测试 |
| 退出旧运行数据 | 旧 Session、run、Trace、反馈、改进、测试运行和旧 DB | 只在明确的 cutover 流程中处理；普通部署不得清空 |
| 保留 | 转换后的 Harness、业务测试与发布资产；项目开发者工具配置 | 不以保留开发工具为由恢复旧生产 Runtime |

`conversion-report.json` 必须逐项记录源/目标 hash、映射规则、
`mapped|retired|rejected` 与确认状态。转换覆盖率要求 100%、`rejected=0`、重复执行 digest
一致；不允许静默漏文件。转换器只用于离线迁移，不进入生产启动链。仓库内置初始化源还须通过
`runtime-bootstrap` 准入；live Workspace 中的私有配置不能随转换回流项目源码。

| Consumer | Mode / env 来源 | 数据与秘密边界 |
| --- | --- | --- |
| AgentGov API | container / `COMPOSE_ENV_FILE` 选择的一份完整 env，默认 `docker/.env` | 持有治理 DB、共享密钥及可选查询凭据，不持有模型/MCP 密钥 |
| AgentScope Runtime | container / Compose 仅注入本服务所需键 | provider/MCP 凭据只进入 Runtime；会话库与执行 Workspace 在独立运行目录 |
| Host API / Runtime | local-debug / API 选择 `docker/.env.local-debug`，Runtime 使用独立私有环境 | 默认 `/tmp/local-debug-volume-agent-gov`，宿主机结果不证明容器已验收 |
| Vite / Langfuse | 前端选 `frontend/.env.local`；Langfuse 为可选 Compose profile | 浏览器不接收观测或模型 secret；Langfuse 仅保存受控语义轨迹 |

容器持久化根为 `${HOME}/volume-agent-gov`，Runtime 使用其
`agentscope-runtime/{data,workspaces,candidates}`；`docker/volume/` 仅作历史迁移来源。
env 是按环境选择，不是叠加覆盖。配置完整说明以 README、env 示例和
[Runtime/env skill](../../.codex/skills/runtime-env-governance/SKILL.md) 为准。

## 5. 原子切换与恢复边界

普通部署与破坏性切换是两种操作。普通部署发现旧 Claude/未知 schema 应在停服前拒绝，
不会顺带迁移或清空活动卷。切换命令的参数和操作顺序以
[README 部署章节](../../README.md#部署) 为唯一 runbook。

操作者明确放弃旧数据时，可按 README 的空卷重新初始化路径部署；必须核定本项目卷与外部
依赖边界，不能把该路径写成旧数据迁移、回滚演练或五类原子切换证据通过。下表描述的是保留
恢复能力的切换流程，不要求为明确弃旧的新部署伪造历史恢复证据。

| 阶段 | 必备条件与产物 | 失败处理 |
| --- | --- | --- |
| 隔离演练 | 临时 Runtime root 完成 Harness 转换和验收；不复用真实活动卷 | 保留脱敏证据，关闭隔离项目 |
| `prepare` | 业务停机；精确解析旧 root；外置数据/env/ownership/hash 与旧 Compose/image 快照；完成数据和镜像 restore drill | 不清空活动数据；证据不匹配即拒绝 |
| `execute` | 校验一次性 token、路径/inode/hash、active run/HITL/test/publish 均为 0；建立 fresh epoch 和验收栈 | 未开放写闸时可按 manifest 恢复旧快照和精确镜像 |
| `finalize` 前半段 | 五类机器验收证据齐备；生产 bind/key 下以 `drain` 状态重建并通过 readiness | 写闸仍关闭时可恢复 |
| 不可逆点 | `api-gate-state.json` 经 `fsync + os.replace` 原子切为 `open`，同时记录 `irreversible_at` | 即使尚无真实请求，也禁止回挂旧卷或运行旧 binary |
| 开放后 | 写入 cutover ledger，清除受保护旧快照，持续观察新栈 | 关闭写闸并向前修复 AgentScope-only 栈 |

源方案把“首个非验收请求被接受”作为不可逆点；当前实现以写闸原子开放为准，避免请求
接收与恢复标记之间的空窗。恢复与清理依据
[切换恢复模块](../../scripts/agentscope_atomic_cutover_recovery.py)，最终证据依据
[证据校验器](../../scripts/agentscope_atomic_cutover_evidence.py)。原方案的开放后 24 小时
观察、旧快照在不可逆点后 15 分钟内清理仍是操作验收要求，不能仅凭脚本存在认定已完成。

## 6. 验证分层与完整验收门槛

| 验证层 | 公共入口或证据 | 能证明的范围 |
| --- | --- | --- |
| 文档与静态治理 | `make codex-guard`、文档契约测试 | 索引、契约引用、旧入口与生成物检查，不证明真实运行 |
| 行为与故障契约 | 相关目标测试、`make main-flow-test`、`make typecheck`；提交/发版前串行 `make test` | 后端、前端、状态机、权限与恢复逻辑；覆盖率以 [quality_policy.json](../../tests/quality_policy.json) 为准 |
| 容器 | `make container-core-smoke`、`make container-openapi-check` | 当前工作树 rebuild、force-recreate 后的独立 Compose 验收 |
| 观测 | `make langfuse-smoke` | 当次真实 run 的语义 Trace；需要有效 provider 与 OTLP 配置 |
| 浏览器 | 对应公共 UI smoke 入口和完整旅程证据 | 原生事件、暂停/续跑、取消、历史、反馈与 Trace 的实际交互 |
| 真实模型与稳定性 | `REQUIRE_LIVE_RUNTIME=1 make container-live-test` 及补充故障/soak/性能证据 | 固定场景、身份关联、负载和长时间运行 |

公共容器验收由 [run_container_acceptance.py](../../scripts/run_container_acceptance.py)
生成临时 Runtime 根、唯一 Compose project 和随机回环端口，使用当前工作树重建并
`--force-recreate`，结束后清理该临时项目。它不触碰既有部署或真实持久化根；直接运行
私有 target、宿主机测试或检查旧容器不能替代该路径。

### 6.1 50-run 场景组成

这是本次替换的内部验收门槛，不是通行标准。至少准备 50 条经人工质量复核、实质不同的
输入；当前最终机器回执要求一组恰好 50 个 run、50 个不同输入，额外探索样本另计。
仅换值、机械改写或重复 prompt 不计入有效样本；脚本去重不能代替人工复核。

| 场景 | run 数 |
| --- | ---: |
| 普通单轮 | 12 |
| 多轮，5 Session × 2 turn | 10 |
| Skill/MCP/tool 成功、失败、权限拒绝 | 8 |
| HITL 单次授权、本 run 授权、拒绝 | 6 |
| cancel/disconnect | 4 |
| 进程重启/恢复 | 4 |
| subagent/team | 3 |
| prompt、Skill、MCP 候选改进与发布闭环 | 3 |

至少 10 个 run 以并发 10 执行。另须覆盖 Session 幂等/补偿、重复提交、参数篡改、权限
注入、跨 Agent/Session/version 错绑、不同阶段 interrupt、持久化/flush 失败、未知事件、
Harness 自修改和管理面绕过。当前是单租户 operator，不能把作用域负测写成已实现多租户隔离。

### 6.2 硬指标与证据

- 预期 terminal、Session 身份及 Agent/version/Harness/reply/trace 关联准确率 100%；
  重复终态、终态重开、残留 run/HITL、未释放 fence 和错误作用域授权为 0。
- 10 次反馈正确关联原 run；3 次候选闭环的变更 commit、测试 commit、发布版本一致；
  测试失败不能发布。
- 真实浏览器连续 3 次完成创建 Session、两轮对话、Tool、HITL、Cancel、刷新历史、反馈、
  Trace 流程，3/3 通过且不 mock SSE。2 小时 soak 内非计划重启、未处理异常、残留运行、
  terminal 丢失均为 0。
- 50 个 run 对应 50 个唯一 Trace；场景应有 span 缺失为 0，内容长度/hash 对账通过，
  secret 原文命中为 0；查询 P95 ≤ 30 秒、最大 ≤ 60 秒。完整性规则见专项观测文档。
- 对冻结基线，P95 TTFT 和端到端耗时 ≤ 1.20 倍、P99 ≤ 1.50 倍，非预期错误率增量
  ≤ 0.5 个百分点，OTel 引入的 P95 附加开销 ≤ 10%。报告采样、原始结果和环境，不能只报最好一次。

最终五类机器证据为 `static_gates`、`contract_tests`、`container_acceptance`、
`browser_acceptance`、`live_runtime`，须绑定同一 cutover、当前源码、镜像、验收产物与
SHA-256；不得把人工改写 `passed` 当成测试结果。保留 schema/OpenAPI/Harness/Runtime
版本、恢复演练、场景复核和故障时间线。受保护原始证据放在仓库外，仓库仅收脱敏结论与引用。

### 6.3 当前能力边界

[live runner](../../scripts/run_agentscope_live_acceptance.py) 已能读取场景，运行 Session、
SSE、chat、消息、run/reply/trace 关联和反馈检查；其结果摘要并不自动证明上述场景分配、
三次浏览器、两小时 soak、性能对比、候选发布闭环或五类最终机器回执已经齐备。

2026-09-10 至 2026-09-11 的 4.0.0 发布验证按以下范围记账；发布版本不代表完整业务验收通过：

| 范围 | 本轮结果与边界 |
| --- | --- |
| 全量回归 | `make test`：1587 项通过；`make typecheck`、前端构建通过。3 条 warning 来自第三方弃用提示 |
| 主流程 | 后端 834 项通过，五组 UI 回归通过。首轮端口冲突与页面超时保留失败记录；整改测试启动所有权、mock 环境隔离、清理和诊断后复验通过，新增 9 项边界测试；未把偶发超时唯一归因为已确认的端口问题 |
| 核心容器 | `make container-core-smoke` 通过，包含 API/Runtime readiness、UI HTTP 与 125 个 OpenAPI operation 的契约核对；不等于完整浏览器业务旅程 |
| 真实浏览器首屏 | 在公共容器验收 runner 内访问真实 UI/API，未 mock 请求；Playground 与 Agent 选择器可见，Agent 已选中，观察期间无 pageerror 或 HTTP ≥400。此项不包含业务对话、取消或 HITL |
| 通用真实运行 | 公共 `container-live-test` 显式 fixture 的 2 个不同合成输入通过 Session、原生 SSE、canonical messages、反馈来源关联与 2 个 complete Trace；未验证业务效果 |
| 观测隐私 | 对上述 2 个 Trace、8 个 observation 及 API/Runtime 日志做追加只读检查：合成 canary 与高熵私有值命中为 0，Trace/observation 原始 input/output 为空；不覆盖 Tool/MCP 全场景或浏览器 DOM 隐私 |
| 正式部署 | 当前代码 `make build` 后 `make all-up COMPOSE_UP_FLAGS=--force-recreate`；9 服务运行，304 个源码文件逐哈希核对，所选 Compose env 与容器实际值一致，映射端口均为回环地址上的 50400–50499 |
| 新数据边界 | 旧卷移出活动路径后从空目录初始化，无 legacy tables；旧目录暂存但不复用。这不是旧数据迁移、切换恢复或永久删除演练 |
| 安全业务受阻 | 外部 MCP 尚缺案件修订 RO 工具和 SOC 处置剧本接口/资源；仅刷新注册无效。本轮未修改外部服务，也未降低批准能力来换取通过 |

同一轮中已修复资源创建的并发/取消补偿、签名畸形输入与重放窗口、Bash 参数越权、
Runtime 缺少 `jq`、陈旧验收入口及 UI 测试隔离等本项目问题。原始日志保存在仓库外，不提交凭据或业务正文。

50 个经复核的实质不同场景、10 并发、HITL、多轮/重启恢复、三类候选发布闭环、三次完整浏览器、
两小时 soak 与性能/效果对比尚无完整证据，不声明通过。只有对应验证和同构建证据齐备，
才能升级为“完整验收通过”。
