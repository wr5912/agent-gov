# AgentGov 下一阶段 P2A Runtime 边界提取与 Claude Adapter 实施方案

> 文档状态：评审稿。
>
> 工程阶段说明：P2A 是下一轮研发中的 Runtime 内部边界提取，不是
> “Runtime 中立已被证明”或多 Runtime 已完成声明。Claude 委托与 fake driver 只能证明
> 边界机械可用；边界是否存在 Claude-shaped 假设，必须用第二类真实协议 spike 证伪。
>
> 并行关联：[P1 网络安全测评纵向闭环实施方案](./AgentGov下一阶段P1网络安全测评纵向闭环实施方案.md)。
> P1 与 P2A 共同以 [P0 准入收口实施方案](./AgentGov下一阶段P0准入收口实施方案.md) 为上游，
> 可独立退出。P2A 使用现有代表性 managed business-Agent 与 Governor 主流程证明 gateway 等价；
> P1 完成后再增加发布评测闭环的穿越证据，不反向成为 P2A 启动或退出门。
>
> 目标架构依据：[多 Runtime 适配、外部 CLI 旁路与 Multica 协作边界方案](./多Runtime适配与外部CLI旁路及Multica协作边界方案.md)。

## 1. 目标与退出结果

在不改变当前 Claude 外部行为、数据库和公开会话字段的前提下，把生产调用收口到
一组可被第二类真实协议检验的候选 Runtime 边界：

```text
backend-owned BusinessAgentVersion binding
  -> RuntimeBindingResolver
  -> RuntimeRegistry
  -> RuntimeGateway
  -> ClaudeCodeAdapterBundle
  -> 现有 ClaudeRuntime / SDK / SessionStore
```

退出时：

- 路由、业务聊天和 Governor job 不再自行选择或直接构造 `ClaudeRuntime`；
- 生产只注册 `claude-code`，adapter 仅委托现有成熟实现；
- 内部 `RuntimeSessionRef` / `RuntimeRun` / `RuntimeEvent` 及 `RuntimeProvenance`
  形成单一 typed 契约，core 不出现 `sdk_session_id` 等 Claude 专属字段；
- 能力清单不再是布尔自证，每项能力保留 support level、constraints、coverage 与
  evidence provenance；
- 使用真实 Codex app server `thread/turn` 协议完成一次非生产 spike，形成
  “可复用边界 / Claude-shaped 假设 / 不支持能力”报告；fake 不能代替该退出证据；
- 非流式、流式、取消、恢复、HITL、subagent、Trace 和 Langfuse 行为等价；
- DB、OpenAPI、SSE、前端和 `sdk_session_id` 保持原契约；
- 第二 Runtime、外部 CLI observer 和 Multica 仍未进入生产路径；
- 本期一个部署只启用一个 Runtime，但这只是过渡期运行限制。长期由后端将
  `BusinessAgentVersion` 绑定到 Runtime，同一部署可治理不同 Runtime 的 BusinessAgentVersion；
  每次 run 只能解析出一个 Runtime，客户端始终不得选择或覆盖。

## 2. 实际问题与替代方案

### 2.1 事实依据

- 当前 Claude 原生实现已经覆盖会话、消息、工具、人工输入、subagent 和 Trace，是应保留的成熟
  adapter 起点；
- 多个 router 和 `AgentJobRunner` 仍直接依赖 Claude 实现；
- `claude_runtime.py` 与流式实现接近 800 行，不能继续增加 Runtime 分支；
- `sdk_session_id` 已跨 DB、OpenAPI、SSE、前端和测试，局部改名会形成长期双轨；
- 当前 raw event 表面有一组较少包含 Claude 命名的 protocol，可作为候选端口
  输入，但不能未经第二协议就宣称中立。

### 2.2 未采用方案

| 方案 | 本阶段不采用原因 |
| --- | --- |
| 直接建第二个生产 adapter | 会在边界未经证伪前复制 Claude 耦合；本期仅做真实协议 spike |
| 先迁移 `sdk_session_id` | breaking 面过大，且没有第二 adapter 证明目标模型 |
| 建一个包含所有能力的巨型 Runtime 接口 | 新 Runtime 会被迫实现不支持能力，形成空方法和分支 |
| 重写 Claude agent loop | 会丢失 SDK 原生权限、hooks、session 和 subagent 事实 |
| 请求体动态选择 Runtime | 破坏后端绑定、Workspace 原生包和会话事实一致性 |

## 3. 治理对象矩阵

| 维度 | 结论 |
| --- | --- |
| 被治理对象 | 受管 Agent run、Runtime session、原生事件和能力覆盖 |
| 治理执行者 | 后端 Runtime binding resolver、registry/gateway、adapter contract tests |
| Runtime 事实所有者 | Claude SDK、SessionStore 和原生事件 |
| Backend 所有事实 | BusinessAgentVersion Runtime 绑定、agent/run 归属、状态、幂等、provenance、公开投影 |
| Agent 所有内容 | 回复、分析和业务结构化输出 |
| 当前边界 | Claude 直接集成可用，但调用方依赖具体实现 |
| 本阶段目标 | 调用方依赖候选小端口，Claude 行为仍由现有实现提供，并由真实第二协议暴露假设 |
| 长期目标 | 同一部署按 backend-owned BusinessAgentVersion binding 治理多 Runtime；单次 run 不混跑，客户端无 selector |

### 3.1 AGV 基线与本阶段贡献

P2A 只提供 Runtime 边界和运行事实证据，不以实施计划预授权
[AgentGov 核心功能测试用例](../AgentGov核心功能测试用例.md)状态升级：

| AGV | 当前基线 | P2A 贡献 | 阶段后状态裁决 |
| --- | --- | --- | --- |
| AGV-002 Runtime/Feedback/Version 治理链 | `gap` | 提供 run/session/event/provenance 边界，并用代表性现有 managed business flow 验证 gateway 不破坏闭环；P1 完成后追加发布评测穿越证据 | 保持 `gap`；未产生 Runtime 事实到 feedback、改进、评估和 release 的单条端到端证据 |
| AGV-014 Runtime 运行可复盘 | `gap` | 增加 typed run/event、coverage、raw boundary、hostile provenance 与真实 Claude 等价回执 | 保持 `gap`；未完成真实运行下 UI/API 对 input/output、tool/skill、错误与 Trace 的联合展示证据 |
| AGV-040 离线/内网必需闭环 | `current` | 第二协议 spike 是阶段验收，不进入生产必需路径；Runtime/provider 保持可本地化 | 保持 `current`；不得因 spike 或外部文档变成运行时公网依赖 |
| AGV-050 Responses/Playground SDK-native 主路径 | `current` | Claude adapter 委托与 golden suite 保持当前 OpenAPI/SSE/Playground 行为 | 保持 `current`；P2A 不迁移公开会话字段、不将 Responses 变为 Runtime selector |

## 4. Runtime 边界契约

新建独立 `runtime_core` 与 `runtime_adapters/claude_code` 子域；具体文件可按当前包结构调整，
但职责必须保持。`runtime_core` 是包边界名，不是“已证明对所有 Runtime 中立”的结论。

### 4.1 内部运行事实

P2A 冻结以下内部 typed 语义，不在本期改变公开 API：

- `RuntimeSessionRef`：`runtime_kind + native_session_id + native_project_scope?`，只引用
  Runtime 原生会话事实，不作为 AgentGov run ID；
- `RuntimeRun`：backend-owned `run_id`、BusinessAgentVersion binding、execution origin、session ref、
  集中状态、时间、provenance 与 observation coverage；
- `RuntimeEvent`：backend-owned `run_id/sequence`、typed `event_kind`、时间和有限
  canonical payload；无法无损投影的原生内容只在 boundary-owned raw payload 保留；
- `RuntimeProvenance`：runtime kind、adapter/native version、binding source、Agent package
  commit/digest、model/provider 非秘密引用、capability report digest 和执行环境摘要。

长期公开会话引用使用 backend-owned opaque `platform_session_id`；内部
`RuntimeSessionRef` 保留原生会话引用和 provenance。P2A 仅在现有 API/DB boundary 将
`sdk_session_id` 单向映射到内部 ref，core 契约不出现该 Claude 专属名称，也不建立
第二套 transcript。

### 4.2 `RuntimeDescriptor`

只描述 adapter 身份和实测能力：

- `runtime_kind`：本阶段唯一值 `claude-code`；
- `adapter_version`：AgentGov adapter 版本；
- `native_version`：启动时探测到的 CLI/SDK 版本；
- `capability_support`：typed `CapabilitySupport` 集合；
- `diagnostics`：不含秘密的启动诊断。

### 4.3 `CapabilitySupport`

每个能力项使用显式结构表达：

- `support_level`：`supported | partial | unsupported`；
- `constraints`：可测的版本、模式、并发、权限或恢复条件，不用自由文本替代门禁；
- `coverage`：`full | partial | summary_only | unknown`，表达可观测范围，不代替支持级别；
- `evidence_ref`：真实版本、协议 spike 或验收回执的 backend-owned 引用。

首批能力键使用显式有限集合：

- managed run、stream、cancel、session resume；
- human input；
- tool events；
- subagent events；
- native session/message read；
- raw native events；
- telemetry；
- observation coverage。

不支持项返回稳定的 typed diagnosis；不得通过伪造空事件、单个布尔值或
默认成功宣称支持。

### 4.4 小端口

| 端口 | 职责 | 本阶段 Claude 实现 |
| --- | --- | --- |
| `ManagedExecutionDriver` | start、stream、cancel 和 run 终态 | 委托现有 `ClaudeRuntime` |
| `SessionFactReader` | session、message、subagent 原生事实 | 委托 SDK `SessionStore` 能力 |
| `HumanInputBroker` | pending request、resolve、deny | 委托现有 HITL 服务 |
| `RuntimeEventSource` | raw native 与 canonical event source | 委托现有 raw/stream 投影 |
| `RuntimeTelemetry` | Runtime 维度 Trace/Langfuse 属性 | 委托现有 telemetry |
| `RuntimeAdapterBundle` | 聚合同一 Runtime 的上述端口和 descriptor | `ClaudeCodeAdapterBundle` |

`RuntimeGateway` 只根据 backend-owned binding 解析 adapter 并提供端口，不包含 Runtime
分支业务逻辑。P2A 的 `DeploymentDefaultRuntimeBindingResolver` 会把当前全部可运行
BusinessAgentVersion 解析到 `AGENT_RUNTIME_KIND=claude-code`；该实现不得把“部署默认”写进
`RuntimeRun` 或 gateway 的长期契约。
`RuntimeRegistry` 是代码内一方注册表；本阶段不设计第三方插件发现、签名或兼容 SDK。

## 5. 生命周期裁决

沿用 P0 已批准语义：

```text
running -> completed | failed | cancelled | interrupted
```

- 单次 run 的所有终态不可变；
- 服务重启、owner 丢失或结果不确定时写入 `interrupted`；
- Runtime 支持原生 session 恢复时，创建新 `run_id` 并复用同一原生 session；
- `ObservationCoverage` 与 run status 分离，允许 `completed + partial`；
- adapter 不得把原生 session ID 当 AgentGov run ID；
- 原生无法无损映射的字段保留 raw payload，canonical 字段留空或降低 coverage。

本阶段不新增新的持久化状态值；现有状态机如需术语同步，必须通过集中转移表和非法转移测试完成。

## 6. 实施工作包

### 6.1 P2A-W1：Contract-first

- 建立上述 Protocol/Pydantic 类型、`RuntimeBindingResolver` 和完整运行状态转移表；
- 使用 fake driver 验证 registry、gateway、绑定解析、能力诊断和未知 Runtime
  fail-fast；该结果只证明 contract plumbing，不作为 Runtime 中立性证据；
- 固定当前 Claude golden behavior：非流式、SSE、cancel/resume、HITL、subagent、Trace；
- 不移动 Claude 业务逻辑。

### 6.2 P2A-W2：真实第二协议 spike

- 使用受支持的真实 Codex CLI/app server 版本运行最小 `thread/turn`、stream、cancel、
  approval 与 event 观测实验；
- 用同一组 `RuntimeSessionRef` / `RuntimeRun` / `RuntimeEvent` / `CapabilitySupport`
  候选契约记录映射结果，但不接入生产 registry、API、DB 或 Workspace 导入；
- 对每个端口标记 `reusable`、`claude-shaped`、`candidate-specific` 或 `unsupported`，
  并记录版本、constraints、coverage 和可重复的本地验收命令；
- 只有报告中的 `claude-shaped` 项已从 core 移至 Claude adapter，或明确作为原生
  extension 隔离后，P2A 才能退出；
- 本工作包不得使用 fake、手写 fixture 或仅阅读官方文档代替真实协议回执。

### 6.3 P2A-W3：阶段性部署默认

- settings 增加 `AGENT_RUNTIME_KIND`，默认 `claude-code`；
- 启动时解析 registry；未知值在应用启动阶段失败；
- 请求体、query、header 和 Vite env 均不能覆盖部署选择；
- P2A 不新增持久化 `BusinessAgentVersionRuntimeBinding`；当前 resolver 将已准入 BusinessAgentVersion
  绑定到部署默认，并且必须校验 Agent 原生包声明与解析结果一致；
- 后续多 Runtime 阶段以 backend-owned BusinessAgentVersion binding 替换 resolver 实现，不改变
  gateway、run 或客户端契约；
- 启动日志只输出 runtime kind、版本和 capability 摘要，不输出 env 值或私有路径；
- 同步 `docker/.env.example`、`docker/.env.local-debug.example`、README、settings/env policy 和
  文档契约；示例值只使用 `claude-code`，真实私有 env 由部署者自行选择是否显式填写；
- `frontend/.env.example` 不增加 Runtime selector；
- 当前 Agent Workspace 中 `runtime: claude-code` 继续作为原生包声明，本阶段不改变导入准入语义。

### 6.4 P2A-W4：Claude 委托 adapter

- 由 composition root 创建现有 `ClaudeRuntime` 和相关服务；
- `ClaudeCodeAdapterBundle` 将小端口委托给现有对象；
- adapter 保留 Claude 原生类型和 raw payload，不把 Claude 配置翻译成通用配置；
- provider/LiteLLM 仍是模型调用层，不进入 Runtime registry；
- 不在事务中执行远程调用或文件删除。

### 6.5 P2A-W5：调用方收口

- router 依赖 `RuntimeGateway` 或明确小端口，不直接 import/构造 `ClaudeRuntime`；
- router 传入 backend-owned BusinessAgentVersion/run context，不传入客户端提供的
  `runtime_kind`、native session 或 provenance；
- Responses、会话、HITL、raw events 和 Agent job 逐项切换；
- `AgentJobRunner` 与业务聊天共享当前部署的 `ManagedExecutionDriver`，但保留各自 profile 和
  typed output 契约；
- 所有生产入口切换完成前不得删除现有实现；
- 阶段退出时，直接 `ClaudeRuntime` 构造只允许出现在 Claude adapter、composition root 和测试。

### 6.6 P2A-W6：清理与等价证明

- 删除 router 内 Runtime 类型判断和重复构造；
- 清理不再使用的 facade，仅保留明确属于 Claude adapter 的实现；
- 文本检索确认生产调用方没有新增 `if runtime_kind == ...` 分支；
- 文本检索确认 core 没有 `sdk_session_id`、Claude options 或 Claude event type；
- OpenAPI 和生成类型保持零差异。

## 7. Consumer × Mode × Boundary

| Consumer | Mode | Env source | Runtime root | Secret boundary | Verification |
| --- | --- | --- | --- | --- | --- |
| API container | container | 默认选择完整 `docker/.env`；自动化可由 `COMPOSE_ENV_FILE` 选择一份完整 env | `${HOME}/volume-agent-gov` | 私有 env，不向示例写真实值 | settings、Compose sanitized config、startup log |
| Host Python/PyCharm | local-debug | 选择 `docker/.env.local-debug` | `/tmp/local-debug-volume-agent-gov` 默认 | 私有本机调试 env | settings、bootstrap、startup log |
| Vite frontend | frontend-local | `frontend/.env.local` | 无 | 不增加 `VITE_RUNTIME_KIND` | build/browser smoke |
| Governor job | 与 API 相同 | 复用 API 已选择的 Runtime | 与 API 相同 | 私有 `MODEL_PROVIDER_API_KEY` | main-flow、typed output |
| Langfuse | container profile | 容器服务地址；浏览器使用 host 地址 | `${HOME}/volume-agent-gov/langfuse` | keys 保持私有 | trace link、health |
| 真实容器验收 | container | 公共 Make 入口选择完整 env | core 使用持久根，隔离验收使用临时根 | 只输出摘要 | build、force-recreate、freshness、live smoke |

术语统一使用“选择 env 文件”，不描述为 layered override。P2A 不改变卷布局，也不在
`docker/.env.local-debug` 下宣称容器验收通过。

## 8. 公开契约与兼容

### 8.1 本阶段新增

- 内部 Runtime 边界类型、binding resolver、registry、gateway 和 Claude adapter；
- 服务端配置 `AGENT_RUNTIME_KIND=claude-code`；
- 不含秘密的 runtime capability 启动诊断。
- 真实 Codex app server 第二协议 spike 报告和可重复回执，但不新增 Codex
  生产 adapter、配置或对外支持声明。

### 8.2 本阶段保持

- `/v1/responses`、SSE、会话、HITL 和 raw event 的公开行为；
- `sdk_session_id`、当前 DB columns、OpenAPI 和前端生成类型；
- Claude SessionStore 作为会话/message/subagent 事实源；
- 业务 Agent Workspace 包与 Governor Workspace；
- `${HOME}/volume-agent-gov` 和 local-debug 临时根。

### 8.3 本阶段禁止

- `sdk_session_id`/`runtime_session_id` 双写或 alias；
- 公开未稳定的内部 `RuntimeSessionRef` 或把 native session ID 当作长期平台主键；
- 请求级 Runtime selector；
- 跨 Runtime Workspace 配置转换；
- observer 写回、批准、取消或恢复外部 CLI；
- 新建并行 session/message 数据库副本。

## 9. 字段所有权

| 所有者 | 字段 |
| --- | --- |
| Runtime-native | native session/event、tool/permission、usage、stop reason、subagent identity |
| Backend-owned | runtime kind、Agent/run 归属、run ID、状态、幂等键、coverage、公开 trace link |
| Agent-owned | 回复内容、分析、建议和业务结构化输出 |
| Boundary-owned | DB、HTTP、SSE、Langfuse、raw payload JSON |

Hostile adapter/native payload 包含伪造 Agent ID、run ID、status、principal 或 provenance 时，后端
必须覆盖或拒绝。

## 10. 架构阈值与替换清单

| 动作 | 内容 |
| --- | --- |
| 保留 | 现有 Claude 原生实现、SessionStore、Responses-first、HITL、Trace、Langfuse |
| 抽取 | 调用端口、registry、gateway、capabilities 和 composition |
| 替换 | router/service 对 `ClaudeRuntime` 的直接依赖 |
| 删除 | router 内重复构造、Runtime 类型分支和失去调用方的 facade |
| 延后 | 公共会话字段迁移、Claude 逻辑物理搬迁、第二 adapter、observer |

不得继续向接近 800 行的 Claude Runtime 文件添加 registry、配置选择或其他 Runtime 分支。新模块
保持单一职责；小端口之间不能复制超过 40 行的相同投影逻辑。

## 11. 测试同步矩阵

| 行为变化 | 旧测试处置 | 新增测试 | 深度 |
| --- | --- | --- | --- |
| 内部 session/run/event/provenance | 现有 session/run 测试 `REFACTOR` | core 无 Claude 字段、raw 边界、hostile provenance | unit/contract |
| registry/gateway | 无 | fake driver、backend binding、单 run 唯一解析、未知值 fail-fast | unit/contract |
| capability support | 现有功能测试 `KEEP` | support/constraints/coverage/evidence 不可伪造 | contract/negative |
| 第二协议 spike | 无 | 真实 Codex thread/turn/event 回执与边界反证报告 | spike/live |
| Claude 委托 | 现有 Runtime 测试 `KEEP` | adapter delegation 与能力 manifest | integration |
| router 依赖收口 | API 测试原则不改 | 无客户端 Runtime 注入 | contract/security |
| Agent job 共用端口 | Governor 测试 `KEEP` | profile、formatter、hostile 字段不退化 | main flow |
| 状态语义 | 状态机测试 `KEEP` | terminal 不可重开、新 run resume | negative |
| env 选择 | settings/env policy 测试 `REFACTOR` | container/local-debug/unknown runtime | configuration |
| OpenAPI/前端 | 生成契约测试 `KEEP` | 零 diff 断言 | contract |

## 12. 验证与退出门

验证顺序：

1. runtime_core、binding resolver 和 fake driver 目标 pytest；
2. settings、repository env policy、startup diagnosis 测试；
3. Claude 非流式/SSE、cancel/resume、HITL、subagent、raw event、Trace/Langfuse 回归；
4. Governor job typed output 与反馈闭环目标测试；
5. OpenAPI 导出和前端生成类型漂移检查；
6. `make codex-guard`、`make typecheck`、`make main-flow-test`；
7. 阶段提交前串行 `make test`；
8. 公共真实容器入口重建并 force-recreate 后，执行 Responses、resume、HITL、subagent、Trace smoke；
9. 使用已固定版本的真实 Codex app server 执行第二协议 spike，保存版本、命令、
   脱敏原生回执、映射矩阵和未支持诊断。

退出硬门：

- 现有 Claude 行为 golden suite 全部等价；
- 客户端无法选择 Runtime；
- 每次 run 仅能通过 backend binding 解析出一个 Runtime，不接受 manifest 或请求覆盖；
- 未知 Runtime 在启动前 fail-fast；
- router/service 生产路径不再直接构造 `ClaudeRuntime`；
- core 的 session/run/event/provenance/capability 契约无 Claude 专属字段；
- 真实 Codex 协议 spike 已将候选边界分类为可复用、Claude-shaped、特有或不支持，
  且 Claude-shaped 假设不再留在 core；
- OpenAPI、DB 和前端仍使用单一现有公开契约；
- 没有新增卷、Runtime 数据副本或真实凭据。

## 13. 回退与 P3 触发

- `AGENT_RUNTIME_KIND` 默认 Claude，发生问题时可以回退 composition 到原 Claude 实现；回退不得
  引入第二套公开字段。
- P2A 不做数据迁移，因此回退不需要改写历史 SQLite。
- 只有 Claude adapter 等价、代表性 managed business-Agent/Governor 主流程已穿越 gateway、真实
  容器与第二协议证据完整后，才能评审将公开会话主键迁移为 opaque `platform_session_id`；P1
  完成后补充发布评测穿越证据，但不把安全垂域设为公共主键迁移的永久前置。
- 第二个生产 Runtime 必须有真实业务需求、已通过的真实协议 spike 和完整准入方案；
  不能以 fake adapter 或 P2A spike 通过直接宣称已生产支持。
