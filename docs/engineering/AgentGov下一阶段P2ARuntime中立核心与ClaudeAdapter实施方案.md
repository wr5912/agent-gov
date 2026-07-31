# AgentGov 下一阶段 P2A Runtime 中立核心与 Claude Adapter 实施方案

> 文档状态：评审稿。
>
> 工程阶段说明：P2A 是下一轮研发的 Runtime 内部边界收口，不是多 Runtime 已完成声明。
>
> 前置方案：[P1 网络安全测评纵向闭环实施方案](./AgentGov下一阶段P1网络安全测评纵向闭环实施方案.md)。
>
> 目标架构依据：[多 Runtime 适配、外部 CLI 旁路与 Multica 协作边界方案](./多Runtime适配与外部CLI旁路及Multica协作边界方案.md)。

## 1. 目标与退出结果

在不改变当前 Claude 外部行为、数据库和公开会话字段的前提下，把生产调用收口到 Runtime 中立
小端口与注册表：

```text
settings
  -> RuntimeRegistry
  -> RuntimeGateway
  -> ClaudeCodeAdapterBundle
  -> 现有 ClaudeRuntime / SDK / SessionStore
```

退出时：

- 路由、业务聊天和 Governor job 不再自行选择或直接构造 `ClaudeRuntime`；
- 只注册 `claude-code`，adapter 仅委托现有成熟实现；
- 非流式、流式、取消、恢复、HITL、subagent、Trace 和 Langfuse 行为等价；
- DB、OpenAPI、SSE、前端和 `sdk_session_id` 保持原契约；
- 第二 Runtime、外部 CLI observer 和 Multica 仍未进入生产路径。

## 2. 实际问题与替代方案

### 2.1 事实依据

- 当前 Claude 原生实现已经覆盖会话、消息、工具、人工输入、subagent 和 Trace，是应保留的成熟
  adapter 起点；
- 多个 router 和 `AgentJobRunner` 仍直接依赖 Claude 实现；
- `claude_runtime.py` 与流式实现接近 800 行，不能继续增加 Runtime 分支；
- `sdk_session_id` 已跨 DB、OpenAPI、SSE、前端和测试，局部改名会形成长期双轨；
- 当前 raw event 表面已有 Runtime 中立 protocol，可作为端口和契约测试基础。

### 2.2 未采用方案

| 方案 | 本阶段不采用原因 |
| --- | --- |
| 直接接 Qwen/Kimi/Codex | 会在边界未稳定前复制 Claude 耦合 |
| 先迁移 `sdk_session_id` | breaking 面过大，且没有第二 adapter 证明目标模型 |
| 建一个包含所有能力的巨型 Runtime 接口 | 新 Runtime 会被迫实现不支持能力，形成空方法和分支 |
| 重写 Claude agent loop | 会丢失 SDK 原生权限、hooks、session 和 subagent 事实 |
| 请求体动态选择 Runtime | 破坏部署、Workspace 原生包和会话事实一致性 |

## 3. 治理对象矩阵

| 维度 | 结论 |
| --- | --- |
| 被治理对象 | 受管 Agent run、Runtime session、原生事件和能力覆盖 |
| 治理执行者 | settings、Runtime registry/gateway、adapter contract tests |
| Runtime 事实所有者 | Claude SDK、SessionStore 和原生事件 |
| Backend 所有事实 | agent/run 归属、部署选择、状态、幂等、公开投影 |
| Agent 所有内容 | 回复、分析和业务结构化输出 |
| 当前边界 | Claude 直接集成可用，但调用方依赖具体实现 |
| 本阶段目标 | 调用方依赖中立小端口，Claude 行为仍由现有实现提供 |

## 4. 中立核心接口

新建独立 `runtime_core` 与 `runtime_adapters/claude_code` 子域；具体文件可按当前包结构调整，
但职责必须保持。

### 4.1 `RuntimeDescriptor`

只描述部署选择和 adapter 身份：

- `runtime_kind`：本阶段唯一值 `claude-code`；
- `adapter_version`：AgentGov adapter 版本；
- `native_version`：启动时探测到的 CLI/SDK 版本；
- `capabilities`：typed `RuntimeCapabilities`；
- `diagnostics`：不含秘密的启动诊断。

### 4.2 `RuntimeCapabilities`

使用显式有限值表达：

- managed run、stream、cancel、session resume；
- human input；
- tool events；
- subagent events；
- native session/message read；
- raw native events；
- telemetry；
- observation coverage。

不支持项返回稳定的 typed diagnosis；不得通过伪造空事件或默认成功宣称支持。

### 4.3 小端口

| 端口 | 职责 | 本阶段 Claude 实现 |
| --- | --- | --- |
| `ManagedExecutionDriver` | start、stream、cancel 和 run 终态 | 委托现有 `ClaudeRuntime` |
| `SessionFactReader` | session、message、subagent 原生事实 | 委托 SDK `SessionStore` 能力 |
| `HumanInputBroker` | pending request、resolve、deny | 委托现有 HITL 服务 |
| `RuntimeEventSource` | raw native 与 canonical event source | 委托现有 raw/stream 投影 |
| `RuntimeTelemetry` | Runtime 维度 Trace/Langfuse 属性 | 委托现有 telemetry |
| `RuntimeAdapterBundle` | 聚合同一 Runtime 的上述端口和 descriptor | `ClaudeCodeAdapterBundle` |

`RuntimeGateway` 只做当前部署 adapter 的解析和端口访问，不包含 Runtime 分支业务逻辑。
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

- 建立上述 Protocol/Pydantic 类型；
- 使用 fake driver 验证 registry、gateway、能力诊断和未知 Runtime fail-fast；
- 固定当前 Claude golden behavior：非流式、SSE、cancel/resume、HITL、subagent、Trace；
- 不移动 Claude 业务逻辑。

### 6.2 P2A-W2：部署级选择

- settings 增加 `AGENT_RUNTIME_KIND`，默认 `claude-code`；
- 启动时解析 registry；未知值在应用启动阶段失败；
- 请求体、query、header 和 Vite env 均不能覆盖部署选择；
- 启动日志只输出 runtime kind、版本和 capability 摘要，不输出 env 值或私有路径；
- 同步 `docker/.env.example`、`docker/.env.local-debug.example`、README、settings/env policy 和
  文档契约；示例值只使用 `claude-code`，真实私有 env 由部署者自行选择是否显式填写；
- `frontend/.env.example` 不增加 Runtime selector；
- 当前 Agent Workspace 中 `runtime: claude-code` 继续作为原生包声明，本阶段不改变导入准入语义。

### 6.3 P2A-W3：Claude 委托 adapter

- 由 composition root 创建现有 `ClaudeRuntime` 和相关服务；
- `ClaudeCodeAdapterBundle` 将小端口委托给现有对象；
- adapter 保留 Claude 原生类型和 raw payload，不把 Claude 配置翻译成通用配置；
- provider/LiteLLM 仍是模型调用层，不进入 Runtime registry；
- 不在事务中执行远程调用或文件删除。

### 6.4 P2A-W4：调用方收口

- router 依赖 `RuntimeGateway` 或明确小端口，不直接 import/构造 `ClaudeRuntime`；
- Responses、会话、HITL、raw events 和 Agent job 逐项切换；
- `AgentJobRunner` 与业务聊天共享当前部署的 `ManagedExecutionDriver`，但保留各自 profile 和
  typed output 契约；
- 所有生产入口切换完成前不得删除现有实现；
- 阶段退出时，直接 `ClaudeRuntime` 构造只允许出现在 Claude adapter、composition root 和测试。

### 6.5 P2A-W5：清理与等价证明

- 删除 router 内 Runtime 类型判断和重复构造；
- 清理不再使用的 facade，仅保留明确属于 Claude adapter 的实现；
- 文本检索确认生产调用方没有新增 `if runtime_kind == ...` 分支；
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

- 内部 Runtime 中立类型、registry、gateway 和 Claude adapter；
- 服务端配置 `AGENT_RUNTIME_KIND=claude-code`；
- 不含秘密的 runtime capability 启动诊断。

### 8.2 本阶段保持

- `/v1/responses`、SSE、会话、HITL 和 raw event 的公开行为；
- `sdk_session_id`、当前 DB columns、OpenAPI 和前端生成类型；
- Claude SessionStore 作为会话/message/subagent 事实源；
- 业务 Agent Workspace 包与 Governor Workspace；
- `${HOME}/volume-agent-gov` 和 local-debug 临时根。

### 8.3 本阶段禁止

- `sdk_session_id`/`runtime_session_id` 双写或 alias；
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
| registry/gateway | 无 | fake driver、唯一选择、未知值 fail-fast | unit/contract |
| Claude 委托 | 现有 Runtime 测试 `KEEP` | adapter delegation 与能力 manifest | integration |
| router 依赖收口 | API 测试原则不改 | 无客户端 Runtime 注入 | contract/security |
| Agent job 共用端口 | Governor 测试 `KEEP` | profile、formatter、hostile 字段不退化 | main flow |
| 状态语义 | 状态机测试 `KEEP` | terminal 不可重开、新 run resume | negative |
| env 选择 | settings/env policy 测试 `REFACTOR` | container/local-debug/unknown runtime | configuration |
| OpenAPI/前端 | 生成契约测试 `KEEP` | 零 diff 断言 | contract |

## 12. 验证与退出门

验证顺序：

1. runtime_core 和 fake driver 目标 pytest；
2. settings、repository env policy、startup diagnosis 测试；
3. Claude 非流式/SSE、cancel/resume、HITL、subagent、raw event、Trace/Langfuse 回归；
4. Governor job typed output 与反馈闭环目标测试；
5. OpenAPI 导出和前端生成类型漂移检查；
6. `make codex-guard`、`make typecheck`、`make main-flow-test`；
7. 阶段提交前串行 `make test`；
8. 公共真实容器入口重建并 force-recreate 后，执行 Responses、resume、HITL、subagent、Trace smoke。

退出硬门：

- 现有 Claude 行为 golden suite 全部等价；
- 客户端无法选择 Runtime；
- 未知 Runtime 在启动前 fail-fast；
- router/service 生产路径不再直接构造 `ClaudeRuntime`；
- OpenAPI、DB 和前端仍使用单一现有公开契约；
- 没有新增卷、Runtime 数据副本或真实凭据。

## 13. 回退与 P3 触发

- `AGENT_RUNTIME_KIND` 默认 Claude，发生问题时可以回退 composition 到原 Claude 实现；回退不得
  引入第二套公开字段。
- P2A 不做数据迁移，因此回退不需要改写历史 SQLite。
- 只有 Claude adapter 等价、P1 闭环在 gateway 上通过、真实容器证据完整后，才能进入 P3
  公共 Runtime 会话迁移。
- 第二 Runtime 必须有真实业务需求和 CLI/SDK capability spike；不能以 fake adapter 通过作为准入。
