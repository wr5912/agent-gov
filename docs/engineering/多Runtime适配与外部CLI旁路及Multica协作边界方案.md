# AgentGov 多 Runtime 适配、外部 CLI 旁路与 Multica 协作边界方案

> 文档角色：产品能力目标方案 + 跨层工程架构方案
>
> 评审基准：2026-07-22
>
> 当前状态：待评审，尚未实现
>
> 适用范围：AgentGov 受管执行、外部 CLI 观测、多 Runtime 演进及与 Multica 的系统边界

本文定义 AgentGov 从“Claude 原生受管运行平台”演进为“多 Runtime Agent 持续改进平台”的目标架构。它描述的是未来能力和迁移顺序，**不表示当前 OpenAPI、数据库、Playground 或运行容器已经支持多 Runtime、外部 CLI 旁路或 Multica 集成**。

当前产品边界仍以 [项目目标愿景使命](../项目目标愿景使命.md)、[AgentGov 术语与版本边界](../AgentGov术语与版本边界.md)、当前 OpenAPI 和代码为准。本文获批并分阶段落地后，才能按实际完成范围更新上述当前实现文档。

## 1. 核心结论

AgentGov 长期应形成一个治理内核、两种运行入口、多个原生 Runtime、一个可选上层协作系统：

1. **治理内核只有一套**：统一接收运行事实，形成反馈、评测、改进建议、测试资产和版本决策。
2. **运行入口有两种**：AgentGov 发起并控制的受管执行，以及外部 CLI 自主运行、AgentGov 只读观测的旁路模式。
3. **Runtime 保持原生**：Claude Code、Qwen Code、Codex、Kimi、CodeWhale 分别使用自己的会话、权限、hooks、配置和工具语义，不做相互转换。
4. **一个部署只绑定一个 Runtime**：所有注册业务 Agent（含 main-agent）和治理 Agent 使用该部署选定的同一种 Runtime；请求或单个 Agent 不得临时切换 Runtime。
5. **Multica 是可选的上层协作系统**：它负责任务、成员、队列和多 Agent 协作；AgentGov 负责运行证据、质量评测和持续改进。二者不复制领域对象，也不共同控制同一次执行。
6. **模型网关不是 Agent Runtime**：LiteLLM/vLLM Sidecar 只处理模型协议与路由，不能承担 CLI 会话、工具、权限或协作职责。

目标关系可概括为：

```text
                    可选上层协作
               +-------------------+
               |      Multica      |
               | 任务/成员/队列/协作 |
               +---------+---------+
                         |
             通用 API 调用 | 或启动外部 CLI
                         v
+------------------------ AgentGov -------------------------+
|                                                            |
|  受管入口                         外部 CLI 旁路入口          |
|  API / Playground                 agentgov-observer         |
|       |                                  |                  |
|       v                                  v                  |
|  ManagedExecutionDriver          ObservationIngress        |
|       |                                  |                  |
|       +----------> 统一运行事实 <---------+                  |
|                         |                                  |
|                         v                                  |
|            反馈 -> 评测 -> 建议 -> 验证 -> 版本             |
|                                                            |
+------------------------------------------------------------+
        |                                        ^
        v                                        |
  系统内受管 CLI                         用户/Multica 启动的外部 CLI
```

## 2. 第一性原理与治理对象

### 2.1 AgentGov 要解决的问题

AgentGov 的核心问题不是“替用户再造一个通用 Agent CLI”，也不是“替协作平台管理任务”。它要回答的是：

- 某个 Agent 在什么配置和 Runtime 下完成了什么；
- 哪些行为事实说明它做得好或不好；
- 反馈如何转化为可复现的评测与测试资产；
- 配置改动是否改善了结果，是否具备发布条件；
- 运行中断、工具调用、人工输入和子 Agent 行为如何被完整追溯。

因此，必须先保留各 Runtime 的原生事实，再在其上建立稳定、有限的治理投影。若先追求“所有 CLI 看起来完全一样”，会丢失真正决定质量的权限、工具、会话和事件语义。

### 2.2 治理对象矩阵

| 对象 | 当前归属 | 未来归属 | 不应承担的职责 |
| --- | --- | --- | --- |
| 所有注册业务 Agent（含 main-agent） | Claude 原生 Workspace 与 AgentGov 业务资产 | 部署所选 Runtime 的原生业务 Agent 包 | 不决定跨 Agent 任务编排 |
| 治理 Agent | Claude 原生 Workspace | 与业务 Agent 相同 Runtime 的独立原生包 | 不伪装成业务 Agent，不接管外部 CLI |
| AgentGov 治理内核 | API、运行投影、反馈闭环、评测与版本 | Runtime 中立的治理事实与业务流程 | 不重写原生 agent loop |
| 系统内 CLI | Claude Agent SDK 启动的 Claude Code | 所选 Runtime 的受管执行端 | 不与 Multica 争夺任务所有权 |
| 外部 CLI | 当前不受支持 | 用户或第三方自行启动，AgentGov 只读观测 | AgentGov 不代写其配置、不控制进程 |
| `agentgov-observer` | 当前不存在 | 本机只读采集与可靠转发组件 | 不执行 Agent，不修改配置，不做模型代理 |
| Multica | 当前不接入 | 可选上层任务协作系统 | 不成为 AgentGov 的治理真相源 |
| LiteLLM/vLLM Sidecar | 模型协议与路由 | 继续作为模型提供方适配层 | 不冒充 Runtime、observer 或协作层 |

### 2.3 两条闭环的能力边界

受管模式可以形成完整发布闭环：

```text
AgentGov 发起运行
  -> 原生 Runtime 执行
  -> 运行证据
  -> 反馈与归因
  -> 配置改进
  -> Agent 自测与平台评测
  -> 版本发布
  -> 下一轮受管运行
```

外部 CLI 模式只能形成只读改进闭环：

```text
外部 CLI 自主运行
  -> observer 采集事件和配置快照
  -> 运行证据
  -> 反馈与归因
  -> 配置修改建议及 Diff
  -> 用户在外部手工应用
  -> observer 采集新快照
  -> 判断已采纳/部分采纳/未匹配
```

外部 CLI 若要获得 AgentGov 的自动应用、测试和发布能力，必须由用户显式导入一个 **Runtime 原生 Workspace 包**，转入受管模式。observer 不得自行升级权限。

## 3. 当前时刻：真实实现与约束

### 3.1 当前架构

当前 AgentGov 是明确的 Claude 原生受管实现：

```text
Playground / 上层系统
        |
        v
/v1/responses、/v1/conversations、兼容接口
        |
        v
ClaudeRuntime / AgentJobRunner
        |
        v
claude-agent-sdk + 捆绑的 Claude Code agent
        |
        +--> Claude 原生 SessionStore / tools / hooks / subagents
        |
        +--> AgentGov 运行投影、反馈闭环、Langfuse
```

这里的“系统内 CLI”是由 AgentGov 通过 `claude-agent-sdk` 发起和管理的 Claude Code agent。它与用户在宿主机、IDE 或远程开发机上单独安装并运行的 Claude Code 即使使用相同二进制，也因**进程所有权和生命周期所有权不同**而属于不同运行入口。

### 3.2 当前代码耦合证据

| 位置 | 当前事实 | 对多 Runtime 的影响 |
| --- | --- | --- |
| `app/main.py`、多个 router | 直接构造或依赖 `ClaudeRuntime` | 路由层无法在不加分支的情况下切换实现 |
| `app/runtime/claude_runtime*.py` | 同时承担请求准备、SDK options、会话、流式、持久化和遥测 | 继续堆 Runtime 分支会扩大单体职责 |
| `app/runtime/agent_job_runner.py` | 直接构造 `ClaudeAgentOptions` | 治理 Agent 执行路径同样绑定 Claude |
| `app/runtime/session_store.py`、`sdk_session_store.py` | 以 Claude SDK `SessionStore` 为原生会话事实 | 需要保留 Claude 原生能力，同时抽出中立引用 |
| schema、DB、前端类型 | 广泛暴露 `sdk_session_id` | 公共契约缺少 Runtime 身份与原生会话命名空间 |
| `app/runtime/model_provider.py` | 通过 `claude_env()` 生成运行环境 | 模型提供方配置泄漏到 Runtime 命名 |
| `runtime_activity.py`、`runtime_langfuse.py` | 解析 Claude 消息并设置 Claude 专属遥测环境 | 其他 Runtime 不能直接复用 |
| Agent Workspace 包 | `CLAUDE.md`、`.claude/`、`.mcp.json` 等为原生资产 | 不能将其误称为通用 Agent 配置 |

这不是当前实现的错误。Claude 原生能力是一等事实，现阶段直接集成保证了会话、hooks、权限、工具和子 Agent 语义没有被削弱。问题在于：如果未来直接在这些文件里添加 `if runtime == ...`，Claude 绑定会从正确的单 Runtime 实现变成不可维护的多 Runtime 分支网。

### 3.3 当前已经具备且必须保留的基础

- `/v1/responses` 与 `/v1/conversations` 作为受管调用的公开入口；
- 业务 Agent Workspace 包、Git 版本、反馈闭环、评测用例和发布条件；
- Claude SDK 原生 SessionStore 作为 Claude 会话事实源；
- 所有注册业务 Agent（含 main-agent）与治理 Agent 的明确边界；
- Langfuse 作为开发观测面，而非运行事实的替代存储；
- Claude 原生权限、hooks、skills、subagents 和人工输入机制优先于平台重造。

### 3.4 当前缺口

- 没有部署级 `runtime_kind` 和能力注册表；
- 没有 Runtime 中立的执行、会话、原生包和事件契约；
- 没有 `execution_origin`，无法区分系统受管运行和外部 CLI 运行；
- 没有外部 CLI 配对、事件接入、离线缓冲和配置快照；
- 没有“原生事实 + 中立投影 + 原始载荷”的统一记录模型；
- 没有多 Runtime 版本兼容矩阵和真实 CLI 验收门；
- 当前不接入 Multica，也没有必要提前建立 Multica 专用领域对象。

## 4. 未来目标：2 至 3 年后的稳定形态

未来目标不是支持尽可能多的 CLI 名称，而是让新增 Runtime 的成本稳定、能力缺失可见、治理结论可比较。

目标完成时应满足：

1. Claude Code、Qwen Code、Codex、Kimi、CodeWhale 可按明确优先级接入；每个 Runtime 有版本化能力清单和真实验收结果。
2. 一个 AgentGov 部署通过 `AGENT_RUNTIME_KIND` 绑定一个 Runtime，启动时完成版本与能力探测；不接受请求级 Runtime 覆盖。
3. 所有注册业务 Agent（含 main-agent）和治理 Agent 均使用该 Runtime 的原生包，不做 Claude 配置到其他 Runtime 的自动翻译。
4. 受管执行和外部 CLI 旁路共享治理内核，但分别保留自己的事实源、可靠性边界和权限模型。
5. 外部 CLI 即使断网或 AgentGov 重启，也能通过本地 durable spool 和幂等回放补齐已采集事件。
6. UI、API、数据库和 Langfuse 可以明确回答：这是哪个 Runtime、哪种执行来源、哪个原生会话、哪份配置、观测是否完整。
7. Multica 可以在上层分派工作，但 AgentGov 不复制其 workspace、issue、member、queue 或协作状态。

### 4.1 目标架构

```text
                           +----------------------+
                           | Multica（可选）       |
                           | 协作、任务、队列、成员 |
                           +----------+-----------+
                                      |
                 +--------------------+--------------------+
                 |                                         |
          调用公开 API                              启动外部 CLI
                 |                                         |
                 v                                         v
+---------------------------- AgentGov --------------------------------+
|                                                                       |
|  Managed Ingress                       Observation Ingress             |
|  API / Playground                      batch events / snapshots        |
|          |                                      |                     |
|          v                                      v                     |
|  RuntimeGateway                         ObservationNormalizer          |
|          |                                      |                     |
|          v                                      |                     |
|  RuntimeAdapterBundle                          |                     |
|  + ManagedExecutionDriver                      |                     |
|  + NativeSessionDriver                         |                     |
|  + NativePackageDriver                         |                     |
|  + NativeHumanInputDriver                      |                     |
|  + NativeProviderBinder                        |                     |
|  + NativeTelemetryBinder                       |                     |
|  + ObservationNormalizer <---------------------+                     |
|          |                                                            |
|          +------------> Canonical Runtime Facts                       |
|                                   |                                   |
|             +---------------------+--------------------+              |
|             v                     v                    v              |
|        运行投影与会话          反馈/评测/建议          Langfuse         |
|                                                                       |
+-----------------------------------------------------------------------+
         |                                           ^
         v                                           |
  部署选定的系统内 Runtime CLI             agentgov-observer（只读）
  Claude/Qwen/Codex/Kimi/CodeWhale         + Runtime 原生 hooks/OTel/API
```

## 5. 系统、CLI、Multica 的关系

### 5.1 责任边界

| 参与方 | 拥有什么 | 控制什么 | 向 AgentGov 提供什么 |
| --- | --- | --- | --- |
| AgentGov | Agent 配置版本、运行投影、反馈、评测、改进建议 | 受管运行、受管测试与发布 | 统一治理视图和公开 API |
| 系统内 CLI | Runtime 原生会话、工具、权限和事件 | 由 AgentGov 启动的一次运行 | 完整原生运行事实 |
| 外部 CLI | 自己的会话、配置和进程 | 用户、IDE、脚本或 Multica 启停 | observer 能取得的只读证据 |
| `agentgov-observer` | 本地配对、待发送事件、快照 digest | 采集、缓存、重放 | 有覆盖声明的事件和配置快照 |
| Multica | workspace、issue、member、task queue、协作状态 | 任务分派、Agent daemon、上层重试 | 可选择只做普通 API 客户端，或启动外部 CLI |
| 模型提供方/Sidecar | 模型 endpoint、协议、路由 | 推理请求 | 模型响应，不提供 Agent 会话治理 |

Multica 官方将自身描述为人类与 AI Agent 的任务协作平台；其 server 持有 workspace、issue、member 和 task queue，daemon 在用户机器上驱动实际 coding tool。该定位与 AgentGov 的治理内核互补，但不应被合并为同一领域模型。参见 [Multica 工作原理](https://multica.ai/docs/how-multica-works) 与 [Multica 开源仓库](https://github.com/multica-ai/multica)。

### 5.2 三种合法关系

#### A. AgentGov 独立受管执行

```text
用户/业务系统 -> AgentGov API -> 系统内 CLI -> AgentGov 治理闭环
```

AgentGov 拥有单次运行的发起、取消、恢复、人工确认和结果投影。Multica 不参与。

#### B. Multica 启动外部 CLI，AgentGov 旁路观测

```text
Multica task -> Multica daemon -> 外部 CLI
                                   |
                                   v
                          agentgov-observer
                                   |
                                   v
                          AgentGov 只读治理
```

Multica 拥有任务和 CLI 生命周期；AgentGov 不向该进程发送控制命令，只观测运行并生成配置建议。即使 CLI 类型与 AgentGov 当前部署的 `runtime_kind` 相同，也不能把外部会话冒充为受管会话。

#### C. Multica 作为普通客户端调用 AgentGov

```text
Multica task -> AgentGov 公开 API -> 系统内 CLI
```

此时 Multica 只负责上层任务，AgentGov 负责一次受管运行。双方通过公开 API 交互，不增加 Multica 专用数据库表、状态同步或私有协议。这是普通客户端关系，不应宣传为深度产品集成。

### 5.3 明确禁止的关系

- AgentGov 不导入或镜像 Multica 的 workspace、issue、member、queue；
- Multica 与 AgentGov 不同时拥有同一次 CLI 的取消、重试、恢复和人工确认；
- `agentgov-observer` 不嵌入 Multica daemon，也不由 Multica 私有 API 驱动；
- AgentGov 不在 Compose 中捆绑 Multica，不为其建立专属 connector；
- 不用 Multica 的任务状态代替 AgentGov 的运行状态或改进事项状态；
- 不因 Multica 支持多个 CLI，就在单个 AgentGov 部署中引入请求级 Runtime 混跑。

只有出现可量化的真实需求，例如通用 API 无法完成必要的关联、审计或取消语义，且持续存在至少两个独立使用场景时，才另立方案评审 Multica 专用集成。

## 6. 两种运行入口

### 6.1 受管执行 `managed`

受管执行继续承担当前核心产品能力：

- AgentGov 选择已注册业务 Agent；
- `RuntimeGateway` 通过当前部署的 `RuntimeAdapterBundle` 发起运行；
- Runtime 原生驱动决定 options、权限、hooks、会话恢复、工具和流式协议；
- AgentGov 只保存 API 所需映射、治理投影和审计字段；
- 原生 Runtime 仍是消息、会话、工具调用和子 Agent 行为的事实源；
- 治理 Agent 同样经 `ManagedExecutionDriver` 执行，不保留 Claude 专用旁路 runner。

### 6.2 外部 CLI 旁路 `external_cli`

`agentgov-observer` 是小型本机组件，而不是另一套 Agent Runtime。它应按以下优先级接入原生事实：

1. Runtime 官方结构化事件 API、app server 或 wire protocol；
2. Runtime 官方 hooks；
3. Runtime 官方 OpenTelemetry；
4. 明确版本约束下的结构化日志；
5. 无结构化能力时标记不支持，不能通过猜测终端文本伪造完整事件。

observer 必须：

- 使用一次性、短期配对令牌绑定 `agent_id + runtime_kind + workspace`；
- 只读取适配器声明的配置 allowlist，不读取环境变量、认证存储、缓存和凭据文件；
- 先将事件和快照写入本地 durable spool，再批量发送；
- 接收服务端最后确认序号，网络或进程恢复后幂等重放；
- 上报明确的 `ObservationCoverage`，不能把“未观察到”解释为“未发生”；
- 不写文件、不修改工具输入、不批准权限、不恢复会话、不控制 CLI 进程。

### 6.3 两种入口在哪里汇合

两种入口只在**原生事实被捕获之后**汇合：

```text
managed:      Runtime native facts -> normalizer -> canonical facts
external_cli: observer raw events  -> normalizer -> canonical facts
```

不能让外部事件先伪装成 Claude SDK 消息再进入 `ClaudeRuntime`，也不能为了复用当前代码而给外部运行分配假的 `sdk_session_id`。每次运行必须且只能有一个 `execution_origin`。

## 7. Runtime 适配架构

### 7.1 小端口组合，不建巨型接口

目标架构使用多个职责单一的端口，再由一个 bundle 组合：

| 端口 | 职责 | 典型能力 |
| --- | --- | --- |
| `RuntimeDescriptor` | 身份、版本、能力探测 | runtime kind、版本范围、能力位 |
| `ManagedExecutionDriver` | 发起和控制受管运行 | run、stream、cancel、resume |
| `NativeSessionDriver` | 读取原生会话事实 | messages、session info、subagents |
| `NativePackageDriver` | 校验和发现原生 Agent 包 | manifest、配置文件、测试入口 |
| `NativeHumanInputDriver` | 映射原生人工输入 | tool approval、clarification、defer |
| `ObservationNormalizer` | 将原生事件投影为治理事件 | event mapping、coverage、raw payload |
| `NativeProviderBinder` | 将模型路由转换为 Runtime 原生设置 | env、flags、endpoint |
| `NativeTelemetryBinder` | 配置 Runtime 原生遥测 | hooks、OTel、structured events |

```python
@dataclass(frozen=True)
class RuntimeAdapterBundle:
    descriptor: RuntimeDescriptor
    managed_execution: ManagedExecutionDriver
    sessions: NativeSessionDriver
    packages: NativePackageDriver
    human_input: NativeHumanInputDriver | None
    observation: ObservationNormalizer
    provider: NativeProviderBinder
    telemetry: NativeTelemetryBinder
```

首期采用仓库内的一方注册表，不提前设计第三方插件 SDK。只有两个以上外部团队需要独立发布适配器时，才评审插件发现、签名和兼容协议。

### 7.2 部署级选择

目标配置增加：

```text
AGENT_RUNTIME_KIND=claude-code
```

约束如下：

- 允许值由内置 registry 提供；
- 应用启动时完成 Runtime 版本和能力探测，失败即输出结构化诊断并 fail fast；
- API 请求、Agent manifest 和 observer 配对不得覆盖部署 Runtime；
- 所有注册业务 Agent（含 main-agent）和治理 Agent 必须与部署 Runtime 一致；
- 若需要同时运行 Claude Code 和 Codex，部署两个独立 AgentGov 实例，后续再评审跨实例聚合，而不是在一次请求中混跑。

### 7.3 能力协商

每个 Runtime 适配器必须声明并实测：

```text
managed_run
managed_stream
session_resume
subagent_events
tool_events
human_input
hooks
otel
structured_observation
native_package_validation
```

缺少能力时必须显式降级或拒绝：例如没有可靠会话恢复，就禁用“继续会话”；没有人工输入回调，就不能显示可操作审批；只有 OTel 汇总数据时，外部运行的 coverage 不能标记为完整。禁止用同名空字段掩盖能力缺失。

### 7.4 Runtime 与模型提供方彻底分离

三个组件名称和职责必须始终区分：

| 组件 | 解决的问题 |
| --- | --- |
| Runtime adapter | Agent CLI/SDK 的会话、工具、权限、包和事件 |
| LiteLLM/vLLM Sidecar | Anthropic/OpenAI 等模型协议和模型 endpoint 路由 |
| `agentgov-observer` | 外部 CLI 的只读事件与配置快照采集 |

现有 `claude_env()` 应迁移为各 Runtime 的 `NativeProviderBinder`，但模型路由决策仍由统一 provider 层拥有。不得让 LiteLLM 根据 Agent 会话状态做编排，也不得让 Runtime adapter 重写模型网关协议。

## 8. 统一事实、字段所有权与生命周期

### 8.1 最小中立模型

```text
RuntimeKind = claude-code | qwen-code | codex | kimi | codewhale
ExecutionOrigin = managed | external_cli

RuntimeSessionRef:
  runtime_kind
  native_session_id
  native_project_key?

RuntimeRunRecord:
  run_id
  agent_id
  runtime_kind
  execution_origin
  runtime_session_ref?
  agent_version_id?       # 仅受管运行
  config_snapshot_id?     # 外部运行优先使用
  status
  observation_coverage
  started_at / finished_at

RuntimeEvent:
  run_id
  sequence
  event_kind
  occurred_at
  normalized_payload
  raw_native_payload
```

首批稳定事件只包含治理真正需要的交集：

```text
run.started
message.delta
message.completed
tool.requested
tool.completed
human_input.required
human_input.resolved
subagent.started
subagent.completed
run.completed
run.failed
```

原生事件无法无损映射时，保留 `raw_native_payload`，并将中立字段留空或降低 coverage，不能强行解释。

### 8.2 字段所有权

| 所有者 | 字段 |
| --- | --- |
| AgentGov 后端 | `run_id`、`agent_id`、`runtime_kind`、`execution_origin`、状态、时间戳、配对和快照 ID |
| 原生 Runtime | 原生 session/event、工具与权限语义、usage、stop reason、subagent 标识 |
| Agent 输出 | 回复内容、分析、建议和业务结构化结果 |
| 边界层 | DB、HTTP、SSE、Langfuse、observer batch 的序列化形式 |

公共契约最终使用 `runtime_session_id` 或结构化 `RuntimeSessionRef` 取代 `sdk_session_id`。现有 Claude 值可通过一次性数据库迁移保留为 `native_session_id`；不保留长期双写、长期 alias 或两套 OpenAPI 字段。

### 8.3 运行状态与观测完整性分离

运行状态采用集中转移表：

```text
running -> completed | failed | cancelled | interrupted
interrupted -> running | completed | failed | cancelled
```

- `completed`、`failed`、`cancelled` 是不可变终态；
- 服务或 observer 中断时，未确认终态的运行进入 `interrupted`；
- Runtime 支持恢复时可回到 `running`，只支持事件回放时根据原生终态收敛；
- `ObservationCoverage` 单独表达 `full`、`partial`、`summary_only` 或 `unknown`；
- `completed + partial` 是合法组合，不能因任务完成就宣称证据完整。

### 8.4 重启、断网与重复事件

外部旁路必须把异常路径视为主设计：

1. observer 为每个配对维护本地持久化 spool；
2. 写入 spool 成功后才允许发送，服务端确认后再清理；
3. 幂等键至少包含 observer identity、稳定原生 run key、sequence 或 payload digest；
4. 服务端按配对维护已确认序号并容忍重复 batch；
5. observer 重启、AgentGov 重启、网络抖动均通过重放恢复；
6. 无法确认连续性时标记 gap 和 `partial`，不自动补造事件；
7. 配对撤销后拒绝新事件，但保留已接收证据的审计归属。

受管运行继续优先使用 Runtime 原生会话恢复能力。适配器只投影恢复结果，不在 AgentGov 中另造一份可独立推进的 transcript。

## 9. Agent 包、治理 Agent 与配置建议

### 9.1 原生 Agent 包

每个包的 `agent.yaml` 必须声明唯一 Runtime：

```yaml
agent_id: security-operations-expert
runtime: claude-code
```

`workspace/` 内容由对应 Runtime 原生约定决定。Claude 包可以包含 `CLAUDE.md`、`.claude/`、`.mcp.json`；其他 Runtime 使用各自官方配置。平台只校验 manifest 与适配器定义的原生结构，不自动把一个 Runtime 的 prompt、hook、permission 或 skill 翻译到另一个 Runtime。

同一业务语义若需支持多个 Runtime，应由开发者维护多个原生包并分别测试。它们可以共享外部业务资料，但不能假定配置文件逐项等价。

### 9.2 治理 Agent

- 治理 Agent 不是业务 Agent，不出现在 Playground 业务 Agent 选择语义中；
- 它与所有注册业务 Agent（含 main-agent）使用相同的部署 Runtime；
- 通用治理 prompt、Pydantic 输出和确定性投影保留在治理内核；
- Runtime 特有的 options、permissions、hooks 和 workspace 由对应原生治理 Agent 包承担；
- 治理任务通过同一个 `ManagedExecutionDriver`，删除 Claude 专用 `AgentJobRunner` 旁路。

处理外部 CLI 配置时，只把 manifest、digest 和必要元数据放入治理 prompt；治理 Agent 通过只读检索工具按需读取已授权的快照文件，不能将整个 Workspace 无条件塞入上下文。

### 9.3 受管版本与外部快照不得混淆

- 受管运行绑定 `agent_version_id`；
- 外部运行绑定 `config_snapshot_id`；
- 只有内容 digest 可确定性证明完全相同时，才可额外记录与受管版本的匹配关系；
- 不能因为 `agent_id` 相同，就把外部运行标记为当前受管版本；
- 外部配置建议是 recommendation artifact，不是可自动执行的 change set；
- 用户手工修改后，新快照与建议做 `adopted`、`partially_adopted`、`not_matched` 归因，状态不代表平台执行了修改。

## 10. API、配置与 UI 目标

### 10.1 受管 API

- 保留 `/v1/responses` 与 `/v1/conversations` 的产品语义；
- 运行详情增加 `runtime_kind`、`execution_origin`、`runtime_session_ref` 和 `observation_coverage`；
- 不增加请求级 `runtime_kind`；
- 原生人工确认统一投影为 UI 可消费的 envelope，但 allow/deny/modify/defer 等合法动作以当前 Runtime 能力为准；
- OpenAPI 和前端生成类型同步删除公开 `sdk_session_id`，不建立永久兼容字段。

### 10.2 observer API

建议建立独立入口，避免混入受管 responses：

```text
POST /v1/observer/pairings
POST /v1/observer/pairings/{pairing_id}/exchange
POST /v1/observer/events/batches
POST /v1/observer/config-snapshots
POST /v1/observer/heartbeats
DELETE /v1/observer/pairings/{pairing_id}
```

接口要求：

- 配对令牌短期、单次使用，交换为可撤销的 observer 凭据；
- batch 必须有 sequence、幂等键、Runtime 版本和 coverage；
- 服务端返回已确认序号，不以 HTTP 2xx 代替逐批确认；
- snapshot 内容寻址、不可变，重复 digest 不重复存储；
- observer API 不提供文件写回、CLI control、权限审批或任务分派接口。

路径和 schema 在实现前通过 OpenAPI ADR 最终确认；本节固定的是职责和禁止项，不把示例路径当成已发布契约。

### 10.3 UI

| 页面 | 目标变化 |
| --- | --- |
| Playground | 显示当前部署 Runtime；只列出与该 Runtime 匹配的注册业务 Agent |
| 会话与运行详情 | 显示“受管运行/外部 CLI”、原生会话引用、Runtime 版本和观测完整性 |
| Agent 设置 | 管理原生包；外部 CLI 使用独立“配对与观测”入口 |
| 反馈工作台 | 可筛选 Runtime 和执行来源；同一反馈模型处理两种证据 |
| 配置建议 | 受管 change set 与外部 recommendation 明确分区，按钮副作用不同 |
| Langfuse 跳转 | 使用中立 span 名称和 `runtime.kind`、`execution.origin` 属性 |

不增加 Multica 专用页面、图标或任务状态。若 Multica 只是公开 API 客户端或外部 CLI 发起方，UI 无需知道它的领域对象。

## 11. Runtime 接入顺序与当前可行性

适配器不得仅凭“CLI 能运行”宣称完成。需要分别核查受管协议、会话、结构化事件、工具、人工输入和配置资产。

| 顺序 | Runtime | 现有可用切入点 | 首要验证 |
| --- | --- | --- | --- |
| 1 | Claude Code | Agent SDK sessions、hooks、OTel | 抽取后与当前功能完全等价 |
| 2 | Qwen Code | headless、hooks、telemetry | 工具事件、会话恢复、长任务行为 |
| 3 | Codex | app server、hooks、OTel | thread/turn 映射、审批和流式事件 |
| 4 | Kimi | Wire Mode、hooks、sessions | wire 稳定性、权限和子 Agent 语义 |
| 5 | CodeWhale | 以实际公开接口为准 | 先做能力 spike；不足时仅支持部分观测 |

截至 2026-07-22 的官方能力入口：

- Claude Code：[Agent SDK sessions](https://code.claude.com/docs/en/agent-sdk/sessions)、[hooks](https://code.claude.com/docs/en/hooks)、[monitoring](https://code.claude.com/docs/en/monitoring-usage)；
- Qwen Code：[headless mode](https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/)、[hooks](https://qwenlm.github.io/qwen-code-docs/en/users/features/hooks/)、[telemetry](https://qwenlm.github.io/qwen-code-docs/en/developers/development/telemetry/)；
- Codex：[app server](https://learn.chatgpt.com/docs/app-server)、[hooks](https://developers.openai.com/codex/hooks)、[advanced configuration and telemetry](https://learn.chatgpt.com/docs/config-file/config-advanced)；
- Kimi：[Wire Mode](https://moonshotai.github.io/kimi-cli/en/customization/wire-mode.html)、[hooks](https://moonshotai.github.io/kimi-code/en/customization/hooks)、[sessions](https://moonshotai.github.io/kimi-code/en/guides/sessions.html)；
- CodeWhale：[开源仓库](https://github.com/Hmbown/CodeWhale)。

这些外部能力会演进。每个适配器必须固定已测试版本区间，并用真实 CLI 重新验收；文档链接不是兼容性证明。

## 12. 迁移方案

### 12.1 删除、迁移、保留清单

| 动作 | 内容 |
| --- | --- |
| 保留 | Responses-first API 语义、Claude 原生能力、业务 Agent 包、治理 Agent、反馈/评测/版本闭环、Langfuse |
| 抽取 | `ClaudeRuntime` 中的执行、会话、人工输入、事件、provider 和 telemetry 职责 |
| 迁移 | `sdk_session_id` 到 Runtime 中立会话引用；Claude 旧值一次性迁移为 `native_session_id` |
| 替换 | router 对 `ClaudeRuntime` 的直接依赖，改为 `RuntimeGateway` |
| 收口 | `AgentJobRunner` 与业务聊天共用当前 Runtime 的 `ManagedExecutionDriver` |
| 新增 | Runtime registry、capability manifest、canonical events、observer ingress、pairing、spool、snapshot |
| 删除 | 路由中的 Runtime 分支、公共 Claude 专属字段、长期 alias/双写、跨 Runtime 配置转换、observer 写回能力 |
| 不改职责 | LiteLLM/vLLM Sidecar 继续只处理模型协议与路由 |

### 12.2 目标目录边界

目录名在编码前可随现有拆包重构调整，但职责必须保持：

```text
app/
├── runtime_core/
│   ├── contracts.py
│   ├── capabilities.py
│   ├── registry.py
│   ├── gateway.py
│   ├── events.py
│   └── lifecycle.py
├── runtime_adapters/
│   ├── claude_code/
│   ├── qwen_code/
│   ├── codex/
│   ├── kimi/
│   └── codewhale/
└── observation/
    ├── pairing.py
    ├── ingestion.py
    ├── snapshots.py
    └── replay.py

agentgov-observer/
├── core/
├── adapters/
└── spool/
```

### 12.3 分阶段实施与硬门

#### P0：契约冻结与能力实测

- 建立 ADR：单部署单 Runtime、双入口、Multica 边界、字段所有权；
- 记录当前 Claude 受管主流程的 golden behavior；
- 对候选 CLI 做真实版本 spike，不先写生产适配器；
- 固定 canonical event 最小集合和 capability manifest。

**硬门**：当前 Claude 功能清单、数据库迁移影响、公开 API breaking change 和回滚点全部可审查。

#### P1：中立核心与一次性数据迁移

- 新增 Runtime 中立类型、状态转移表和 registry；
- 将 `sdk_session_id` 一次性迁移为 Runtime 会话引用；
- 更新 OpenAPI、前端生成类型、DB、Langfuse 属性和术语文档；
- 不增加第二 Runtime。

**硬门**：旧 Claude 历史仍可读取；无长期双写；非法状态转移、重复迁移和回滚路径有测试。

#### P2：Claude adapter 抽取与等价验收

- 将现有 Claude 逻辑按小端口迁入 `claude_code` adapter；
- router 只依赖 `RuntimeGateway`；
- 治理 Agent 和业务 Agent 共用受管执行端口；
- 删除生产路径对 `ClaudeRuntime` 的直接构造。

**硬门**：Claude 会话恢复、流式、工具、人工输入、subagents、prompt suggestions、Langfuse、反馈主流程和真实容器 E2E 与迁移前等价。

#### P3：Claude 外部 CLI observer

- 实现配对、spool、batch、幂等、coverage 和配置快照；
- 只使用 Claude 官方 hooks/OTel/结构化能力；
- UI 区分 managed 与 external CLI；
- 形成“建议 -> 用户手工应用 -> 新快照匹配”的只读闭环。

**硬门**：断网、observer 重启、AgentGov 重启、重复 batch、事件缺口、撤销配对和敏感文件排除全部通过。

#### P4：Qwen Code

- 先完成真实能力报告，再实现原生包和 adapter；
- 受管执行通过后，再开放旁路观测；
- 不复用 Claude 配置映射。

**硬门**：与 Claude 共用同一 contract test suite；不支持能力均有明确诊断和 UI 降级。

#### P5：Codex

- 基于 app server/thread/turn 和官方 hooks/OTel 建立 adapter；
- 单独验证审批、恢复、工具事件与流式；
- 完成 Runtime 原生治理 Agent 包。

**硬门**：真实 Codex CLI 版本矩阵和容器/宿主机边界通过，不靠模拟协议宣称可用。

#### P6：Kimi

- 基于 Wire Mode、hooks 和 sessions 完成能力 spike；
- 先解决原生会话与事件身份，再接治理闭环。

**硬门**：长会话、异常中断和权限语义可追溯；能力缺口不被中立 schema 掩盖。

#### P7：CodeWhale 与生态收口

- 以届时公开、稳定且可测试的结构化接口为准；
- 若只具备有限观测能力，只发布 observer partial support；
- 总结两个以上非 Claude adapter 的重复点，再决定是否开放插件 SDK。

**硬门**：没有结构化事实来源时不得通过终端文本解析伪装完整接入。

Multica 不作为以上阶段的前置依赖。只有 AgentGov 多 Runtime 核心稳定、出现真实协作需求且通用 API/旁路模式无法满足时，才单独进入需求评审。

## 13. 验收体系

### 13.1 Contract tests

每个 Runtime adapter 运行同一套行为契约：

- 能力探测与不支持诊断；
- run/stream/cancel/resume；
- session、message、tool、human input、subagent 投影；
- raw payload 保留和 canonical event 映射；
- provider 与 telemetry 绑定不串 Runtime；
- 原生包校验、治理 Agent 包校验；
- hostile 原生字段不能覆盖 backend-owned 字段。

### 13.2 受管真实验收

- 所有注册业务 Agent（含 main-agent）均可选择和运行；
- 治理 Agent 使用同一 Runtime 且不出现在业务选择列表；
- 非流式、流式、会话恢复、人工确认、服务重启和失败诊断完整；
- 反馈 -> 归因 -> 优化 -> 自测/平台评测 -> 发布主流程可完成；
- UI 空态、成功态、失败详情和 Runtime 降级态有截图或浏览器场景证据；
- 使用真实容器、真实 CLI 和支持矩阵中的版本，不以 mock 代替最终验收。

### 13.3 外部 CLI 真实验收

- 配对必须显式绑定既有 Agent，不能靠目录名自动创建；
- CLI 未连接 AgentGov 时仍能独立运行；
- observer 被杀、网络中断、AgentGov 重启后事件可恢复；
- 重复和乱序 batch 不产生重复运行或错误终态；
- 配置 allowlist 外文件、环境变量、认证存储和凭据不会进入快照；
- AgentGov 无法通过 observer 修改文件、批准工具、停止进程或恢复会话；
- 建议只能由用户手工应用，后续快照匹配结果准确；
- coverage 缺口在 API、UI 和 Langfuse 中一致可见。

### 13.4 Multica 边界验收

- 未配置 Multica 时，AgentGov 所有核心能力完整可用；
- Multica 启动外部 CLI 时，AgentGov 只观察，不参与其任务状态或进程控制；
- Multica 调用公开 API 时，与任一普通客户端遵循相同契约；
- AgentGov 数据库和 OpenAPI 不出现 Multica workspace、issue、member、queue 的镜像模型；
- 不存在 Multica 专用 Compose 服务或强依赖。

## 14. 主要风险与防线

| 风险 | 后果 | 防线 |
| --- | --- | --- |
| 过度归一化 | 丢失原生权限、工具和会话语义 | 最小事件交集 + `raw_native_payload` + coverage |
| 巨型 adapter | 新增 Runtime 后修改所有实现 | 小端口 + bundle + contract tests |
| 双重控制 | AgentGov 与 Multica/用户竞争取消、恢复、审批 | `execution_origin` 唯一；外部模式严格只读 |
| 外部 CLI 版本漂移 | hooks 或事件结构失效 | 版本探测、支持矩阵、fail closed、真实验收 |
| 配置泄密 | observer 上传凭据 | adapter allowlist，默认拒绝未知文件，快照前扫描 |
| 事件丢失/重复 | 错误归因和评测 | durable spool、ack sequence、幂等、gap/coverage |
| 历史字段长期双轨 | API、DB、前端语义漂移 | 一次性迁移，删除 `sdk_session_id` 公共契约 |
| 把模型网关当 Runtime | 只解决 HTTP 却丢失 agent loop | provider sidecar 与 Runtime adapter 独立 |
| 把 Multica 当治理内核 | 重复任务、成员和状态模型 | 上层协作边界，通用 API 或只读旁路 |
| 为名义支持降低质量门 | CLI 能启动但治理不可用 | 能力分级发布，不支持项明确报错 |

## 15. 非目标

本方案不包含：

- 建设通用多 Agent 协作、A2A 网络或任务看板；
- 将 Multica 嵌入 AgentGov 或复制其领域数据；
- 在单次请求内动态选择 Runtime；
- 自动翻译 Claude、Qwen、Codex、Kimi、CodeWhale 配置；
- 通过 observer 修改外部 CLI 配置或控制其进程；
- 用 AgentGov 数据库替代 Runtime 原生 session store；
- 用 LiteLLM/vLLM Sidecar 替代 Agent Runtime；
- 为能力不足的 CLI 伪造会话、工具、人工确认或完整观测。

## 16. 审批决策项

本方案提交审批时，应一次性确认以下架构决策：

1. 接受“一个部署一个 Runtime”，拒绝请求级混跑；
2. 接受“受管执行 + 外部 CLI 只读旁路”共享治理内核；
3. 接受 Runtime 原生包并存，拒绝跨 Runtime 配置转换；
4. 接受 `sdk_session_id` 一次性迁移和公共契约清理，不保留永久兼容层；
5. 接受 observer 严格只读，外部自动应用必须通过显式导入转为受管模式；
6. 接受 Multica 仅为可选上层协作系统，不进入当前 AgentGov 领域模型和 Compose；
7. 接受先完成 Claude adapter 等价抽取，再接 Qwen Code、Codex、Kimi、CodeWhale；
8. 接受能力分级和 fail closed，不以“能启动 CLI”替代完整验收。

## 17. 最终目标判断

当前“在 Claude Agent SDK 上包一层”的实现不是应被丢弃的临时方案，而是未来 `claude-code` adapter 的真实、成熟起点。需要替换的是它对路由、公共 schema、治理任务和模型绑定的直接渗透，不是 Claude 原生能力本身。

未来外部 CLI 旁路也不是另一套 AgentGov。它只增加一种证据入口：原生 CLI 保持自主，observer 可靠采集，治理内核继续完成反馈、评测和配置改进。受管模式与旁路模式在事实归一化之后汇合，在此之前各自尊重原生生命周期和真相源。

Multica 位于更上层。它回答“谁在什么任务上协作”，AgentGov 回答“这个 Agent 如何运行、如何被评测、如何持续变好”。保持这一边界，AgentGov 才能既服务自己的 Playground 与 API，也能旁路服务 Claude Code、Qwen Code、Codex、Kimi、CodeWhale，以及未来由 Multica 或其他系统调度的 CLI，而不演变成另一个协作平台或通用 CLI 外壳。
