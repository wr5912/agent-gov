# AgentGov 下一阶段 P3 扩展组合准入框架

> 文档状态：评审稿。
>
> 工程阶段说明：P3 是“平台基础准入维度 + 独立扩展线”的组合框架，不是一次性并行交付清单，也不是把 P1/P2 未完成内容统一延后的桶。
>
> 前置入口：[AgentGov 下一阶段实施方案索引](../AgentGov下一阶段实施方案索引.md)。

## 1. 定位与目标

P3 的目标是让平台能按真实需求组合扩展，同时不把当前旗舰业务 Agent、唯一 Runtime 或某个外部产品固化为长期平台边界。网络安全是当前旗舰垂域，用于验证完整纵向闭环；它不是 Runtime、EvalOps、资产治理或通用集成的永久全局启动门。

```text
P1/P2 可验证证据
  -> 选择真实扩展线
  -> 评审该线适用的平台基础维度
  -> 通过才建立实施里程碑
  -> 真实容器、业务、人工或外部证据
  -> 更新当前实现基线与 AGV 状态
```

P3 不默认启动任何扩展。每条扩展线独立启动、验收和退出，不要求“P3 全部完成”。

## 2. 治理对象与闭环预检

| 维度 | P3 裁决 |
| --- | --- |
| 被治理对象 | 精确 `BusinessAgentVersion`、`EvaluationBenchmark` 及其 `EvaluationProtocolRevision`、能力/场景包修订、Runtime binding、精确 `GovernorCapabilityVersion` build 及其作用域 |
| 治理执行者 | 后端确定性规则、独立评测方、授权人工评审者、外部身份/审批系统的组合 |
| 资产分类 | 一级仍是数据/证据、方法论和执行资产；版本、provenance、审计、scope 和生命周期是横切治理维度 |
| 生命周期 | 评测基准、能力包和 Governor 能力均通过新的不可变 revision/build 演进，旧 revision/build 不可原地修改；评估、启用、废弃、回退和审计分别留痕 |
| 反馈归属 | 运行、反馈、评测、发布和线上结果均归属到 Agent、version、scenario/protocol 和 project/resource scope |
| 本轮上游方案边界 | 按评审稿，P1 是单安全 Agent 的窄评测纵切片，P2A 是 Runtime 边界提取，P2B 只生成并评估 shadow candidate；这些是计划交付边界，不代表当前运行态已实现，也不证明平台化扩展已完成 |
| 目标能力边界 | 平台能在不牺牲评测独立性、资产归属、身份作用域、数据治理和可回退性的前提下增加垂域、Runtime、能力包和集成 |

P3 继续遵循统一闭环：

```text
对象 -> 运行 -> 反馈 -> 归因 -> 优化 -> 评测 -> 版本/发布 -> 资产 Registry
                                                   -> 线上结果 -> 观察/回退
```

## 3. 通用组合启动门

任一扩展线开始前必须满足：

- 它所依赖的来源阶段已通过退出门：安全完整 MVP 依赖 P1，Runtime 公共迁移依赖 P2A，Governor 启用依赖 P2B；不相关的扩展线不互相充当全局门；
- 绑定明确 AGV 用例、真实用户任务、精确被治理对象和业务 owner；
- 对本线适用的 EvalOps、资产关系、身份/scope、数据治理、集成可靠性和 SLO/成本维度逐项给出 `applicable` 或有证据的 `not-applicable`；
- 公开 API、DB、OpenAPI、前端、env、Workspace 和历史数据有删除/迁移/保留清单；
- 有身份授权、幂等、部分失败、停止条件、回滚点和真实容器验收设计；
- 不把目标文档、mock、local-debug 结果或某个旗舰 Agent 的通过证据当作通用平台能力；
- 发布点由用户确认，不因进入 P3 自动 bump `VERSION` 或创建 tag。

P0 的[模拟 MCP 平台回执](./AgentGov下一阶段P0模拟MCP平台验收实施方案.md)只证明隔离合成夹具中的工具发现、调用和证据采集，不证明动态安全场景、MCP 认证、生产网络或高风险审批已就绪。

## 4. 平台基础准入维度

下列是每条扩展线的评审维度，不要求一次性全部实施。当扩展会触及某维度时，必须在该里程碑中实现并验收，不得以“留到后续”绕过。

### 4.1 EvalOps 与平台发布评测

- 业务 Agent Workspace 的 pytest 是 Agent-owned 可见单元/回归测试，正文以 Workspace Git 为真源；
- 评测基准是 evaluator-owned 的版本化测量合同，至少固定能力维度、样本范围、Ground Truth/评分规程、安全否决、采样和环境约束；
- 平台发布评测绑定精确 BusinessAgentVersion、评测基准修订和环境指纹，生成 baseline/candidate comparison、safety gate、人工复核与发布裁决证据；
- `AgentTestRun` 可作为 Workspace pytest 的首个执行适配器，但动态环境、隐藏集、人工评审和跨 Agent campaign 不得被压缩为 pytest report 或单个 test run 生命周期；
- 不同基准修订、Runtime、模型或环境的分数默认不可直接比较，不提供脱离协议的“Agent 总能力分”。

长期 UI 分工固定为：

| 入口 | 主要任务 | 不应承担 |
| --- | --- | --- |
| 业务 Agent 详情 → 测评 | 查看单个 Agent 的协议、版本比较、发布门、线上结果和回退证据，是单 Agent 主入口 | 管理全平台 campaign 或复制测试正文 |
| 独立测评中心 | 管理评测基准、隐藏集、批次/campaign、跨 Agent 对比和人工评审队列 | 代替 Agent 版本页或将隐藏正文暴露给被测 Agent |
| 资产复利 → 测试资产 | 浏览 Workspace 测试源码、修订、适用范围和执行证据 drill-down，正文修改仍回到 Workspace Git | 充当平台发布基准、Agent 能力分或评测 campaign 权威入口 |

页面可分阶段交付，但路由、查询和评分组件不得把长期信息架构绑死在测试资产页。

### 4.2 资产关系与能力/场景包

- Asset Registry 记录 Agent、protocol、run、feedback、method、`AgentChangeSet`、release 和 online outcome 的版本关系，不复制 Workspace、外部系统或隐藏集正文；
- 能力/场景包是带修订、适用范围、风险和版本策略的组合关系，它引用 prompt、skill、SOP、eval 和 release policy，不另建它们的文本副本；
- 将能力包应用到目标 Agent 只能生成待审查的 `AgentChangeSet`/待发布版本，不能直接改写 active Workspace；
- 来源 Agent 的评测结果不能被目标 Agent 继承；每个目标 Agent 都必须在自己的版本、Runtime binding 和作用域中独立评测；
- P2B Governor shadow learning 只产生 Governor 方法候选和评估证据，不等于跨业务 Agent 能力包复用，不能据此将 AGV-045 升级为 `current`。

### 4.3 单组织控制面与 scope

P3 采用“单组织先行”，不在本阶段建设完整多租户产品。但单组织不等于无身份、无作用域：

- principal 必须由服务端认证与映射，请求体中的 operator、owner 或 scope 不是权威身份；
- 治理资源至少保留 project/resource scope，Agent、protocol、holdout、capability build、activation 和审计查询均校验 scope；
- 评测基准编辑者、候选生成者、独立评测者和启用者必须可做职责分离；
- 外部业务系统继续拥有业务用户、生产审批和最终执行责任；AgentGov 只授权自身评测、发布、资产和 Governor 治理动作。

### 4.4 数据治理

- run、trace、feedback、evaluation、holdout、人工评审和发布证据必须声明来源、授权、分类、保留期和删除/销毁责任；
- 隐藏集的正文、Ground Truth 和评分细则与被测 Agent Workspace 物理分离，只向授权 evaluator 暴露；
- Registry 与运行数据库保存引用、digest、分类和证据摘要，不因查询便利复制敏感正文；
- 脱敏、导出、删除、保留到期和 legal hold 必须可审计，失败时明确投影部分完成范围；
- 真实凭据、私有路径和未脱敏业务数据不进入项目源码仓、公开文档或对外评测摘要。

### 4.5 通用集成可靠性

- 外部 API、webhook、observer 和调度平台先进入通用 integration contract，不为单一产品建立平行主流程；
- 输入绑定稳定外部标识、scope 和幂等键，输出包含可追踪的处理结果和错误；
- 重试、重复、乱序、超时、断网、撤销授权和部分失败均有状态与对账证据；
- 只读 observer 不得写配置、批准工具、取消、恢复或控制外部进程；需要这些动作时必须进入受管 adapter 或外部审批边界。

### 4.6 SLO、容量与经济性

- 扩展线开始前声明用户可感知的可用性、延迟、队列等待、吞吐或完整性目标；
- 评测和 Runtime 需记录任务量、模型/工具调用、耗时、重试、存储增长与单次成本的可观测摘要；
- 容量和预算超限必须有限流、排队、停止或降级策略，不允许静默丢失证据或绕过安全门；
- 没有达到已宣告观察窗和样本量时，不得声称扩展线已满足规模化生产 SLO 或具备可接受单位经济性。

### 4.7 AGV 状态贡献

| 平台维度 | AGV | 当前状态 | P3 状态规则 |
| --- | --- | --- | --- |
| EvalOps 与平台发布评测 | AGV-051 | `gap` | 单 benchmark 的 P1 证据不自动升级；只有基准独立治理、三类入口分工、人工决定、Release 追溯和 OnlineOutcome 闭环均有真实证据后才复核 |
| 单组织身份与 scope | AGV-052 | `gap` | backend-owned principal、越权负向测试和职责分离通过后逐项复核 |
| 数据全生命周期 | AGV-053 | `gap` | holdout/trace/evaluation/release 的保留、导出、删除、legal hold 与部分失败形成闭环后复核 |
| 通用集成可靠性 | AGV-054 | `gap` | 真实外部集成的幂等、重放、撤销、对账和未配置负向证据通过后复核 |
| SLO、容量与经济性 | AGV-055 | `future` | 达到声明观察窗、样本量、容量故障与单位成本证据后才可进入 `gap/current` 评审 |

任何扩展线只更新其实际覆盖的 AGV；P3 框架文档本身不改变状态。

## 5. 扩展组合 A：网络安全旗舰垂域 MVP

### 5.1 启动条件

- P1 的 8 case 静态纵切片已稳定，且能区分 Workspace 回归测试与平台发布评测；
- 已批准从 8 case 扩展到完整 MVP 的数据计划、能力分层、专家资源和质量标准；
- 已指定与候选 Agent 开发侧分离的评测基准 owner，完成开发集/隐藏集隔离与数据治理设计；
- 只有进入动态工具步骤时，才要求隔离环境、身份授权、工具 allowlist、审批和停止条件就绪。

“已形成 50+ 高质量案例”是完整 MVP 的退出证据，不是从 8 case 扩展的启动条件。

### 5.2 扩展顺序

1. 在不改写 P1 基准证据的前提下，将可见静态回归案例扩展到 30 个左右，并逐步建立 evaluator-owned 隐藏发布集；
2. 增加至少两名独立安全专家的质量复核、`EvaluationReviewDecision` 和争议处理；
3. 增加告警研判、多跳调查等场景，使版本化高质量案例总数达到 50 个以上；
4. 接入只读或模拟工具，并采集 Runtime 原生 tool trace；
5. 增加高风险动作审批、安全沙箱和失败注入；
6. 最后评审 L3 多智能体工作流和 L4 线上运营闭环。

### 5.3 退出门

- 至少 50 个高质量、版本化案例，核心案例经至少两名独立安全专家复核；
- 可见回归集与隐藏发布集分离，候选 Agent 无法读取或改写隐藏集、Ground Truth、评分器和发布阈值；
- 同协议、同 Runtime/模型/工具环境下完成 baseline/candidate 多次执行比较，确定性安全失败可直接否决；
- 数据来源、授权、脱敏、保留和销毁可审计；动态工具测试有隔离环境、审批和停止证据；
- 空态、成功态、评分失败、人工复核、工具错误和安全阻断均有 UI 场景证据；
- 线上业务指标、误报/漏报、延迟、成本和回退阈值已定义，不以离线分数代替发布后结果。

P0-MCP 只可复用“OpenAPI → MCP → Runtime 原生 facts”技术认识。安全扩展必须新建领域协议、认证/授权边界、场景 Ground Truth、失败注入和独立评分；若仍只需要平台 smoke，本扩展线保持未启动。

## 6. 扩展组合 B：Runtime 公共契约迁移与第二 Runtime

### 6.1 启动条件

- P2A Claude gateway 与真实容器行为等价，且至少一条代表性真实 managed business-Agent flow
  已完整穿越 gateway；该证据可以来自 P1，也可以来自具备等价 run/session/HITL/Trace 边界的其他
  已批准业务流，不把 Runtime 演进永久绑定网络安全垂域；
- 至少一个非 Claude 真实协议 spike 已用于证伪端口设计，不以 fake driver 单独声称 Runtime 中立；
- 历史 SQLite 中 Claude session 数据完成只读兼容分析；
- OpenAPI、SSE、前端和上层客户端 breaking change 已获批准。

### 6.2 目标契约

公共 API 只暴露不透明的 `platform_session_id`。内部 `RuntimeSessionRef` 保留不可替代的 Runtime provenance：

```text
PlatformSessionRef:
  platform_session_id

RuntimeSessionRef (internal):
  platform_session_id
  runtime_kind
  runtime_instance_key?
  native_session_id
  native_project_key?
  project_scope
```

- `native_session_id` 不组成公开可寻址主键，也不要求外部客户端识别 Runtime 种类；
- DB、records、API response、SSE、OpenAPI、前端生成类型和 ContextPackage 在同一迁移里程碑原子切换；
- 不保留长期 alias、双写或请求双字段；历史快照通过明确 projection 读取，不放宽当前 response schema；
- 每个 BusinessAgentVersion 由后端绑定一个 Runtime，每次 run 只使用该绑定；客户端不得在请求中选择或覆盖 Runtime；
- 长期同一 AgentGov 部署可治理使用不同 Runtime 的 BusinessAgentVersion，不以“单部署单 Runtime”作为产品永久边界；
- run 仍不可重开，session resume 创建新 `run_id`；重复 migration、历史脏数据、回滚和真实数据 UI 必须验证。

### 6.3 Adapter 演进

公共迁移稳定后，再把 Claude 执行、session、HITL、事件和 telemetry 按 P2A 小端口迁入 `runtime_adapters/claude_code`。每迁一个端口先跑等价 suite，再删除旧 facade；生产路径旧 symbol 最终清零。

第二 Runtime 只在真实业务需求和固定版本 spike 通过后实施：先做 managed adapter，使用原生 Workspace/Governor 包，运行同一 contract suite，对不支持能力 fail-closed；不转换 Claude 配置，不并行承诺多个候选 Runtime。

## 7. 扩展组合 C：Governor 受控启用、观察与回退

### 7.1 启动条件

- P2B 至少一个 candidate 已物化为不可变 `GovernorCapabilityVersion` build，并完成 current-vs-candidate 盲化隔离评估；
- build manifest 精确列出 prompt builder、skill/rule/profile、job registry、formatter/OutputModel、配置与 content digest；
- candidate、安全门、holdout/evaluation pack、评估者版本和 build digest 均可追溯；
- 认证 middleware 提供 backend-owned `AuthenticatedPrincipal`，职责分离和 `ApplicabilityScope` 获批。

### 7.2 身份与动作

- `governor_learning_reviewer` 可查看脱敏证据、提交评审意见；
- `governor_learning_admin` 可对评估通过的精确 build 启动 canary、扩大作用域或回退；
- 候选生成者不能成为该 build 的唯一 evaluator、reviewer 或 activator；
- 开发单 key 模式也必须由服务端映射稳定 principal，不把 key、映射或私有路径写入仓库。
- 启动 canary 必须引用 exact outcome/build/scope 上仍有效的
  `HumanReviewDecision=approved_for_canary`；仅有 `EvaluationOutcome=passed` 不能绕过人工决定。

Governor 能力治理使用独立管理面，不进入四阶段业务 Agent 改进工作台：

| 用户动作 | 业务产物 | 目标 API 语义 | 副作用 |
| --- | --- | --- | --- |
| 提交评审意见 | `HumanReviewDecision` | 对精确 `capability_version_id/build_digest` 评审 | 不切 active |
| 启动 canary | `ActivationRecord` | 绑定 `capability_version_id/build_digest + applicability_scope + rollout_policy + approved_review_decision_id` | 新建 canary activation，不覆写 build |
| 扩大作用域 | `ActivationRecord` | 对已观察 activation 进行 CAS 提升 | 原子更新 scoped active binding |
| 回退 | `ActivationRecord` | 将指定 scope 恢复到旧 evaluated build | 保留历史与回退原因 |

Activation 只接受服务端 principal，使用 idempotency key 和 active binding compare-and-swap。缺少有效
`approved_for_canary` 人工决定、评估/安全失败、build digest 不符、scope 不匹配、证据过期或角色不足
均 fail-closed；不存在从可变 method candidate 或单独 passed outcome 直接切 active 的路径。

### 7.3 Canary、观察与回退

- 启动前固定 scope、新 run 流量/指定 Agent 集、观察窗、最小样本、安全否决、效果、延迟、成本和人工介入阈值；
- 只有观察窗和最小样本同时满足且无安全否决时，才能扩大 scope；离线 eligible 不等于线上有效；
- 命中安全否决、错误率/延迟/成本超阈值或证据不完整时，自动停止扩大并回退到上一个 evaluated build；
- 新 Governor run 绑定该 scope 解析出的精确 build，进行中的 run 保持启动时 build；
- 并发请求最多产生一次 active binding 切换和一次审计事件，旧 build、评估与 activation 历史不可改写。

UI 展示候选/build diff、作用域、反证、盲化评估、canary 指标、安全门和回退条件；它不成为业务 Agent，也不复制四阶段产物编辑入口。

## 8. 扩展组合 D：通用集成与外部 CLI observer

通用 API/webhook 不足，且出现无法通过受管 Runtime API 获得的真实旁路证据需求时，才启动外部 CLI observer。observer 必须：

- 显式配对已有 Agent 与 project scope，不通过文件路径猜测身份；
- 严格只读，不写配置、批准工具、取消、恢复或控制进程；
- 具有 durable spool、ack、sequence、幂等和 coverage 完整性；
- adapter allowlist 排除 env、认证存储和未知文件；
- 断网、重复、乱序、重启和撤销配对均有真实 CLI 验收。

外部 CLI observer 是通用集成契约的一个适配器，不建立第二套 run、session、trace 或任务状态真相源。

## 9. Multica 与多智能体协作候选边界

Multica 不再作为 P3 的独立产品扩展线或任何阶段的前置依赖。只有同时出现至少两个独立真实场景，且通用 AgentGov API、webhook 和只读 observer 均无法满足必要的关联、审计或取消语义时，才把 Multica 与其他候选方案一并纳入 AGV-054 通用集成评审。

任何候选评审必须保持：

- AgentGov 不复制 workspace、issue、member、queue 或外部任务状态；
- 不增加候选产品专用 Compose 强依赖或平行认证系统；
- 协作平台作为普通客户端或上层调度者，AgentGov 继续只负责 Runtime、反馈闭环、评测、资产和版本治理；
- 未配置任何协作平台时，AgentGov 核心能力仍完整。

## 10. 测试与验收矩阵

| 准入维度/扩展线 | 必需测试 | 必需真实证据 |
| --- | --- | --- |
| EvalOps | 候选不可读/改 holdout、基准修订锁定、同环境比较、安全否决、人工争议 | 精确 protocol/BusinessAgentVersion/environment 与发布裁决 |
| 资产与能力包 | 引用完整性、scope 不匹配、重复应用、跨 Agent 独立评测、回退 | 能力包修订、两个 Agent 的 `AgentChangeSet` 和独立评测报告 |
| 身份/scope | 伪造 principal、越权读写、不同 scope 混淆、职责分离 | 服务端 principal、授权决策和审计事件 |
| 数据治理 | 脱敏、保留到期、删除/导出、holdout 秘密性、部分失败 | 数据登记、销毁/保留证据和无泄露扫描 |
| 通用集成 | 认证、幂等、重试、重复/乱序、超时、撤销、部分失败 | 真实外部 API/CLI、断网与对账记录 |
| SLO/经济性 | 队列压力、限流、超时、预算超限、观测完整性 | 声明窗口内的延迟/错误/吞吐/单次成本报告 |
| 安全旗舰 MVP | 数据授权、隐藏集、baseline/candidate、专家争议、工具失败、安全审批 | 专家复核、隔离环境、真实容器；P0-MCP 回执不替代 |
| Runtime 公共迁移 | fresh/历史 DB、重复/回滚 migration、OpenAPI/type、非 Claude spike、非法状态 | 真实数据列表/详情/UI、Claude live 与第二协议证据 |
| Governor 启用 | 未授权、build 篡改、scope 不匹配、评估失败、并发、幂等、canary 超阈值、回退 | 精确 build/pack/evaluator、principal、canary 观察和 activation/rollback trace |
| observer/协作候选 | pairing、spool、重复/乱序、coverage、秘密排除、未配置负向断言 | 真实 CLI/候选平台、断网和进程重启 |

每条实际进入实施的扩展线均需运行目标测试、`make codex-guard`、`make typecheck`、`make main-flow-test`、串行 `make test`，并通过对应公共真实容器入口。需要真实专家、外部审批、数据销毁或 CLI 的验收不能用 mock 替代。

## 11. 退出、更新与停止条件

- 每条扩展线独立退出，安全垂域、Runtime、Governor、能力包和集成不互相代表对方完成；
- 完成一条线后，只更新其真实实现基线、AGV 状态、README/OpenAPI/前端和质量策略，不连带升级其他 `gap/future` 用例；
- 阶段性限制必须记录当前原因、长期目标、兼容缝隙、触发条件和清理 owner，不得上升为无退出条件的平台不变量；
- 若真实需求消失、评测无法独立、身份/scope 不足、上游接口不稳定或 SLO/预算不可接受，应停止该线而不是降低门槛；
- 若出现秘密泄露、跨 scope/Agent 污染、隐藏集泄露、不可回滚迁移、未评估 build 启用或双重控制，立即回退并重新评审；
- 版本发布仍遵循根 `VERSION` 单一真相和用户确认，不因文档阶段完成自动创建 tag。
