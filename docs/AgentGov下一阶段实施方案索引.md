# AgentGov 下一阶段实施方案索引

> 文档状态：评审稿。
>
> 评审基准：2026-08-05，仓库版本 `3.0.3`。
>
> 文档角色：连接长期产品目标、平台能力地图、单项产品能力目标方案与下一轮工程实施的唯一阶段索引。
> 本文和下列阶段方案不表示对应能力已经进入当前 OpenAPI、数据库、UI 或运行态。

## 1. 阶段结论

下一阶段采用 **Conditional Go**：

- 本轮文档整改先固定平台能力地图、三类资产、评测权威、控制面和临时决策退出条件；
- P0 随后收口业务 Agent Workspace 全量测试和独立 P0-MCP 平台回执，但不把单个 Agent 测试
  并入根静态 collection，也不把 `29` 固化为平台契约；
- P0 通过后，P1 网络安全协议化回归/发布准入与 P2A Runtime 边界提取可以并行；
- P2B Governor 受控学习基础可先建设账本和候选，但必须等待一条 P1 真实证据及 P2A gateway
  后才能退出；
- P3 是扩展组合准入框架；统一 EvalOps、资产关系、单组织控制面、数据治理和运营能力属于平台
  横切基础，不以安全垂域完整 MVP 作为永久前置。

不采用三条路线同时全面铺开的方案，也不把已有三份目标方案误当作平台能力全集。当前底座足以
支撑窄纵切片，但统一评测、资产关系、跨层 Runtime 契约、Governor 二阶治理证据、控制面和运行
运营能力尚未同时达到规模化建设条件。

## 2. 权威关系

| 文档                                                                                          | 角色        | 本索引如何使用                                                      |
| ------------------------------------------------------------------------------------------- | --------- | ------------------------------------------------------------ |
| [项目目标愿景使命](./项目目标愿景使命.md)                                                                   | 长期产品权威    | 固定 `Agent Runtime · Feedback Loop · Version Governance` 核心定位 |
| [AgentGov 核心功能测试用例](./AgentGov核心功能测试用例.md)                                                  | 长期验收锚点    | 每个阶段必须绑定并增强具体 AGV 用例                                         |
| [网络安全智能体测评工程需求](./网络安全智能体测评工程需求文档.md)                                                       | 产品能力目标方案  | 定义安全测评目标、对象、评分、安全门和完整 MVP                                    |
| [Governor 自研究与受控自学习能力需求](./Governor自研究与受控自学习能力需求.md)                                        | 产品能力目标方案  | 定义 Governor 二阶治理闭环与安全边界                                      |
| [多 Runtime 适配、外部 CLI 旁路与 Multica 协作边界方案](./engineering/多Runtime适配与外部CLI旁路及Multica协作边界方案.md) | 目标架构方案    | 定义 Runtime 长期边界和迁移方向                                         |
| [反馈闭环当前实现基线](./反馈闭环当前实现基线.md)                                                               | 当前实现基线    | 解释当前代码、API、存储、四阶段流程和发布门                                      |
| 本索引及下列阶段方案                                                                                  | 当前实施评审入口  | 决定下一轮先做什么、暂不做什么以及如何验收                                        |
| [P0 模拟 MCP 平台验收实施方案](./engineering/AgentGov下一阶段P0模拟MCP平台验收实施方案.md)                          | P0 配套工程方案 | 只定义隔离合成夹具、真实 Runtime 工具闭环和阶段回执                               |

三份单项目标方案继续 `keep`，不合并、不归档；它们分别解释安全测评、Governor 学习和 Runtime，
不构成平台能力全集。平台能力面、资产分类、评测关系和控制面边界以长期产品权威及本索引为准。
阶段方案只把已选切片落实为可执行边界，不复制完整长期正文。已归档的旧《AgentGov 目标达成
分阶段执行计划》继续只承担历史审计价值，不恢复为活跃入口。

## 3. 当前准入证据

截至评审基准日：

- `make codex-guard`、`make typecheck`、`make main-flow-test` 通过；
- 根质量策略能收集 1351 个 pytest leaf；按测试资产权威契约，它不静态收集任何业务 Agent
  Workspace 测试，平台通过通用 per-Agent lane 在精确 commit 上独立执行；
- 该 Workspace 的首轮完整独立测试为 15 通过、14 失败：6 个危险 Bash 未 deny、3 个畸形输入
  契约、1 个审计 fallback 路径错误、4 个原生配置/身份陈测；
- 静态测试之外，当前还没有“真实 Runtime 加载模拟 MCP → Agent 调用两个限定 GET tools →
  AgentGov 投影原生 tool facts → 清理”的可复现阶段回执；
- 核心功能测试用例在 2026-07-30 基线为 28 个 `current`、22 个 `gap`；AGV-002、AGV-043
  对应端到端治理链和内置业务 Agent 闭环，AGV-045 必须由跨业务 Agent 能力包及逐 Agent 评测
  证明，不能由 Governor 自身 shadow 学习替代；
- `ClaudeRuntime`、流式 Runtime 和 Governor 中心服务已接近 800 行架构阈值，新增职责必须进入
  独立子域，不能继续在中心文件中堆叠分支；
- `sdk_session_id` 已跨越 DB、OpenAPI、SSE、前端和测试，当前不具备低风险局部改名条件；
- 当前 Governor 有受控生成、Trace、变更和发布基础，但没有不可变学习证据、能力版本、
  独立评估和可信人工启用链。

这些证据支持“开始阶段工程”，不支持“宣布三项长期能力已经就绪”。

## 4. 平台能力完整性地图

| 平台面 | 当前可用基础 | 下一阶段最小动作 | 长期退出结果 |
| --- | --- | --- | --- |
| 业务 Agent 与资产治理 | Workspace 包、per-Agent Git、change set/release、只读测试资产 | 建立测试/评测/改进/发布的关系投影；不复制正文 | 能力包跨 Agent 复用并逐 Agent 独立评测 |
| Runtime 执行 | Claude 原生受管链、会话、HITL、Trace | 提取中立内部事实和能力边界；验证第二类真实协议差异 | 同部署可治理后端绑定不同 Runtime 的 BusinessAgentVersion |
| Feedback 与改进 | 四阶段事项、归因、优化、执行和发布 | 用 P1 失败证据进入现有闭环 | 线上结果反哺改进并可追溯到精确版本 |
| Evaluation 与 Release | Workspace pytest、`AgentTestRun`、精确发布门 | 分离 Agent 自有回归与独立发布基准，冻结协议中立语义 | 跨 Agent/协议 campaign、隐藏集、人工评审和周期再评估 |
| Policy 与控制 | 当前 API 认证、后端状态与审计基础 | 单组织 principal/resource scope 边界和职责分离 | 独立治理控制面，不接管外部业务权限 |
| Integration | Runtime API、反馈入口、Trace 链接 | 固定通用身份映射、签名、幂等、回放和错误契约 | 特定外部系统仅作为 adapter，不形成平台专用主流程 |
| Data 与 Operations | SQLite/Workspace Git、容器测试、Langfuse | 定义分类分级、保留/删除、SLO、容量和成本基线 | 可持续运行、恢复和成本可解释的平台服务 |

安全运营是当前首个旗舰垂域，用于验证平台通用契约；它不是 Runtime、控制面、数据治理或通用
集成的永久依赖。横切基础可在各自前置证据满足后推进，不以安全完整 MVP 作为统一全局门。

## 5. 治理对象矩阵

| 维度 | 网络安全测评 | Runtime 边界提取 | Governor 受控学习 | 平台横切基础 |
| --- | --- | --- | --- | --- |
| 被治理对象 | `security-operations-expert` 精确 Git commit 及其协议内表现 | Agent 运行、会话、原生事实和能力覆盖 | Governor 可执行能力 build 及实际治理效果 | 评测、资产、scope、数据和运营契约 |
| 治理执行者 | SDK/Agent 事实源、独立评测方、确定性安全门、人工评审、发布门 | Runtime adapter、后端 registry/gateway、真实协议 spike 和契约测试 | Governor 候选生成、盲化 evaluator、后端门禁、授权人员 | 后端策略、平台管理员、领域专家和外部业务系统 |
| 数据/证据资产 | run、trace、case result、scorecard、violation | native event、canonical event、coverage、session ref | 原始产物、人工修订 diff、评估、启用与回退记录 | principal/scope audit、SLO、成本和线上效果 |
| 方法论资产 | 评分规程、安全门槛、评测协议 | capability 语义、降级和生命周期规则 | 归因方法、适用条件、反证和淘汰规则 | 资产适用、评审、保留/删除和运营策略 |
| 执行资产 | Workspace 可见测试、独立发布评测包、fixture、Ground Truth | Runtime 原生包、adapter、contract suite | prompt、skill、job spec、typed contract、候选 build、dev/holdout pack | EvalOps 编排、资产关系和通用集成机制 |
| 横切治理维度 | Agent commit、suite/protocol revision、digest、Release、审计、scope | Runtime kind、adapter/native version、capability digest、provenance | capability key/build、ApplicabilityScope、评审/激活/回退记录 | version、provenance、audit、scope、lifecycle |
| 当前边界 | 测试运行和发布门已存在，独立发布基准未落地 | Claude 原生实现成熟，中立事实与验证缝隙未收口 | 事项级执行已存在，二阶学习闭环未实现 | 只有分散能力，没有统一平台合同 |
| 本轮目标 | 形成首条协议化回归和发布准入证据，不宣称整体能力提升 | 建立 Claude 委托的中立内部边界并用第二协议证伪 | 形成不改变线上行为、评估精确 build 的 shadow 学习证据 | 固定长期 seam、启动门和独立实施顺序 |

P0-MCP 不改变上表的业务治理对象。其被验对象是 AgentGov Runtime/MCP/证据投影，固定 commit 的
`security-operations-expert` 只是测试载体，两个外部仓库只是可丢弃夹具。该回执不属于 Agent
领域能力、P1 Scorecard 或生产 MCP 配置/版本证据；未覆盖的 transport、认证、resources、prompts、
写操作和其他 Runtime adapter 继续保持 GAP。

## 6. 总体闭环与依赖

```text
P0 准入收口
  ├─ 精确 commit 的 Workspace 全量测试零未分类失败（29 仅为当前快照）
  └─ P0-MCP capability slice：Claude / Streamable HTTP / tools / read-only / no-auth
  ↓
  ├─ P1 安全协议化回归/发布准入 → ImprovementItem → candidate → paired evidence → Release
  └─ P2A Runtime 边界提取 + Claude 委托 adapter + 第二类协议 spike
       ↓
P2B Governor evidence → MethodCandidate → immutable capability build → blind shadow evaluation
       ↓
P3 扩展组合准入
  ├─ 平台基础：EvalOps / 资产关系与能力包 / 单组织控制面 / 数据治理 / SLO 与成本
  └─ 可选扩展：安全完整 MVP / Runtime 公共迁移 / Governor 受控激活 / 外部 adapter
```

P1 与 P2A 可在 P0 退出后并行，避免把 Runtime 平台边界永久绑定安全垂域。P2B 的账本、候选和
评估开发可以提前进行，但接入统一 `ManagedExecutionDriver` 和阶段退出必须同时等待 P2A gateway
与至少一条 P1 真实闭环证据。P3 的平台基础和扩展线分别满足自身启动门；安全完整 MVP 不阻断
与其无依赖的身份、数据、运营或通用集成基础。

## 7. 阶段方案

| 阶段 | 方案 | 核心产物 | 退出结果 |
| --- | --- | --- | --- |
| P0 | [准入收口实施方案](./engineering/AgentGov下一阶段P0准入收口实施方案.md) | per-Agent 全量测试、质量策略 lane、Runtime 生命周期裁决 | 形成无单 Agent 平台特例的开发基线 |
| P0-MCP 配套 | [模拟 MCP 平台验收实施方案](./engineering/AgentGov下一阶段P0模拟MCP平台验收实施方案.md) | 固定上游、过滤 OpenAPI、直接协议与 AgentGov live 回执 | 关闭已声明 capability slice 的回执 GAP，不产生领域结论 |
| P1 | [网络安全测评纵向闭环实施方案](./engineering/AgentGov下一阶段P1网络安全测评纵向闭环实施方案.md) | 8 个静态案例、独立发布基准引用、typed Scorecard/Violation、paired evidence、改进事项闭环 | 首条协议化回归和发布准入证据，不宣称整体能力提升 |
| P2A | [Runtime 边界提取与 Claude Adapter 实施方案](./engineering/AgentGov下一阶段P2ARuntime边界提取与ClaudeAdapter实施方案.md) | 中立内部事实、小端口、registry/gateway、Claude 委托、第二协议 spike | Claude 行为等价且抽象经非 Claude 语义证伪 |
| P2B | [Governor 受控学习基础实施方案](./engineering/AgentGov下一阶段P2BGovernor受控学习基础实施方案.md) | 不可变证据、候选 capability build、ApplicabilityScope、盲化 shadow 评估 | 精确 build 可评估但不自动生效 |
| P3 | [扩展组合准入框架](./engineering/AgentGov下一阶段P3扩展准入实施方案.md) | 平台基础与各扩展线的独立启动门、迁移、运营和回退要求 | 形成后续独立里程碑，不把准入评审伪装为已实施 |

## 8. 已选实施裁决

1. **P0 双门独立收口**：精确 commit 的 Workspace 全量测试和 P0-MCP 回执分别阻断；当前
   `29` 只作基线快照，退出标准是声明范围零未分类失败。
2. **业务 Agent 测试独立执行**：质量策略登记通用 per-Agent lane、owner 和资源类别，不把任何
   业务 Agent Workspace 测试加入根静态 collection；真正的平台 runner/隔离契约留在根测试。
3. **模拟 MCP 只验精确 slice**：固定原始上游提交，只覆盖
   `Claude / Streamable HTTP / tools / read-only / no-auth`；认证、其他 transport、resources、
   prompts、写操作和其他 Runtime 继续保持 GAP。
4. **安全是旗舰垂域，不是平台边界**：P1 先证明安全 Agent 的协议化回归—改进—版本闭环；
   Runtime、身份、数据和运营基础按自身依赖推进。
5. **首切片只做静态 L2**：P1 的 8 个高密度案例固定 `allowed_tools=[]`、精确 commit；不得称为
   完整 MVP、通用 benchmark 或整体能力提升。
6. **测试与正式基准分权**：Workspace Git 保存 Agent 自有可见回归测试；evaluator-owned Git
   评测包保存发布协议、评分器和 holdout。数据库只保存受控引用、revision/digest 和结果证据。
7. **`AgentTestRun` 是 sample 执行适配器**：P1 建立最小协议中立
   `EvaluationExecution/Assessment/ComparisonGroup` 聚合和持久化，只引用 1..N 个 sample run，
   不复制测试正文或 Runtime 事实，也不恢复旧 `TestDataset/EvalRun`。
8. **版本比较与稳定性分开证明**：修复前/待发布版本使用兼容协议和环境配对；评分器确定性次数与
   Agent 独立执行采样、均值、波动和失败率分开记录。
9. **安全门独立否决**：高综合分不能抵消工具越界、虚构证据、敏感泄露或高风险动作越权。
10. **一次 run 不重开**：`interrupted` 是该 run 的终态；恢复同一 Runtime session 创建新
    `run_id` 并保留恢复血缘。
11. **Runtime 近期单选、长期后端绑定**：P2A 只启用 `claude-code`，禁止客户端逐请求切换；长期
    同部署可治理绑定不同 Runtime 的 BusinessAgentVersion。公开主键最终使用 opaque platform session ID，
    native session 只作为内部 provenance。
12. **Governor 评测精确 build**：P2B 只产出 shadow 证据；candidate 必须物化为不可变
    capability build，dev/holdout 分离，结果按 ApplicabilityScope 解释，不能用无范围全局指针传播。
13. **三类一级资产**：数据/证据、方法论和执行资产是稳定分类；version、provenance、audit、
    scope、lifecycle 是横切维度，Registry 只保存引用和关系。
14. **UI 分层**：P1 即建立“业务 Agent 详情 → 测评”单 Agent 主入口，测试运行详情只保留
    sample/run 证据 deep-link；独立测评中心在第二 benchmark/protocol、跨 Agent campaign、持续
    隐藏集运营或专家评审队列出现时启用。
15. **单组织先行**：AgentGov 只治理自身 principal/resource scope；外部系统继续拥有业务权限、
    组织和生产审批。联网研究、自修改和全局自动传播默认禁用。
16. **四阶段工作台保持四阶段**：测评失败只通过真实业务动作进入改进事项；测评运营和 Governor
    元治理不成为第五个用户阶段。
17. **Multica 不是前置依赖**：先建设通用集成原语；只有通用 API/observer 仍不能满足两个以上
    真实场景时才另立产品评审。

## 9. 全局字段所有权

| 所有者 | 字段或语义 | 约束 |
| --- | --- | --- |
| Backend-owned | ID、Agent/commit/runtime/session 引用、状态、时间、trace、suite/protocol/build digest、比较组、评分投影、门禁、principal/scope、provenance | 不进入 Agent/Governor 输出要求；外部污染值必须忽略或覆盖 |
| Agent/Governor-owned | 回答内容、分析结论、业务假设、风险说明、方法正文、适用条件和反证 | 必须经 typed OutputModel 或明确的原生文本边界进入后端 |
| Workspace/test-owned | Agent 自有可见回归 case、fixture、工程断言和测试源码 | 随业务 Agent Workspace Git 版本化，不能被运行请求改写，也不进入根静态 collection |
| Evaluation-owned | 正式评测协议、发布/holdout case、Ground Truth、scorer、阈值、采样和环境要求 | 与候选 Agent 独立版本化；候选无权修改或读取隐藏内容，变更需单独评审 |
| Boundary-owned | SQLite JSON、HTTP、SSE、文件报告、日志、Langfuse metadata | 只在边界序列化；内部主流程传递 typed model |

## 10. 全局环境与数据边界

- API 容器继续选择完整的 `docker/.env`；自动化可由 `COMPOSE_ENV_FILE` 选择另一份完整 env，
  不使用 layered override 术语。
- 宿主机 Python/PyCharm 继续选择 `docker/.env.local-debug`；其结果不能声明为容器验收。
- Vite 只使用 `frontend/.env.local`，不增加独立 Runtime 选择器。
- 容器持久化根继续为 `${HOME}/volume-agent-gov`；P0、P1、P2A、P2B 均不改变卷布局。
- 独立评测包以受控 Git 内容为真相源，候选运行权限只能读取本次公开输入；数据库和 Registry 不
  复制 pack 正文，隐藏集访问必须审计并支持轮换。
- P0-MCP 是上述卷规则之外的隔离验收：使用独立 Compose project 和临时 Runtime/MCP 数据，
  明确不得挂载 `${HOME}/volume-agent-gov`，结束后必须清理。
- 必需闭环保持离线可用；外部研究、Multica 和远程服务不得成为通过条件。
- 真实 key、MCP header、私有 endpoint、运行态 SQLite 和敏感样本不得进入实施文档、源码或测试
  fixture。
- append-only 表示证据关系和判定不可被静默覆盖，不表示敏感 payload 永久保存。平台基础必须定义
  分类分级、保留期限、脱敏/删除、legal hold、备份恢复和删除后的 digest/tombstone 审计。

## 11. 文档动作与维护

| 对象 | 动作 | 原因 |
| --- | --- | --- |
| 三份产品能力目标方案 | `keep` + 对齐 | 继续定义单项长期目标，但不再被解释为平台能力全集 |
| 当前实现基线与核心功能测试用例 | `keep` | 继续提供当前事实和验收锚点 |
| 本索引、五份阶段主方案及一份 P0 配套方案 | `keep` + 修订 | 提供本轮可执行节奏、长期 seam、阶段硬门和退出条件 |
| 旧分阶段执行计划 | `no-op` | 已归档且仍有历史审计价值，不恢复、不删除 |
| README 活跃索引 | 更新 | 保证所有新增文档可发现 |

阶段实现完成后，只更新真实完成部分：当前实现变化进入基线文档，AGV 状态按证据升级；未实现的
后续阶段继续保持目标方案或评审稿状态。

P0-MCP 完成并形成可复现回执后，其实施稿退出“下一阶段”主阅读顺序，转为历史验收材料；归档或
保留原路径必须届时按引用矩阵决定，当前不提前移动。

## 12. 临时决策与退出台账

| 当前阶段性决定 | 为什么现在采用 | 长期 seam | 触发升级或退出 |
| --- | --- | --- | --- |
| 安全 Workspace 当前 29 个静态 leaf | 提供可复现基线 | per-Agent exact-commit lane，不依赖固定数量 | 用例增删或第二 Agent 接入时只更新该 Agent suite，不改平台契约 |
| P1 通过 `AgentTestRun` 执行 sample | 复用成熟 pytest runner 和证据采集 | `EvaluationExecution/Assessment/ComparisonGroup` 作为独立聚合，只引用 sample run | 第二执行协议或非 pytest sample 出现时新增 adapter，不改领域对象 |
| P1 建立 Agent 详情测评入口 | 单 Agent 发布评测已是正式产品任务 | 测试资产只深链 sample/run；组件可由测评中心复用 | 第二 benchmark/protocol、跨 Agent campaign、持续隐藏集运营或专家队列任一出现时启用测评中心 |
| P2A 每部署只启用 Claude | 先证明调用方边界与行为等价 | BusinessAgentVersion backend-owned RuntimeBinding 和内部中立事实 | 第二 Runtime 真实需求与协议 spike 通过 |
| P2B 只有 shadow outcome | 缺可信身份、职责分离和线上安全证据 | 评估、人工决定、激活记录分离，activation 绑定精确 build/scope | control plane、canary、观察窗和回滚阈值全部获批 |
| 单组织单部署 | 当前不建设组织和成员产品 | principal/resource scope 贯穿治理资源 | 出现跨组织托管或共享控制面真实需求时另立多租户 ADR |

## 13. 平台指标树

每个实施阶段必须登记 `baseline / target / guardrail / measurement window / owner / evidence source`。
平台层至少覆盖：

- 反馈到已验证改进的转化率和周期，同类问题复发率、候选退化率与发布回滚率；
- 未授权、跨 scope、隐藏集泄漏和敏感信息泄漏次数；
- run 成功率、证据完整率、延迟 SLO、队列积压、恢复时间和存储增长；
- 活跃被治理 Agent、完成重复闭环的 Agent，以及跨 Agent 复用后的独立评测通过情况；
- 每次已验证改进的模型/计算成本、存储成本和领域专家工时；
- 离线评测变化与线上业务结果的相关性、漂移和基准失效情况。

## 14. 评审清单

评审本索引时应一次确认：

- 是否接受“P0 → P1/P2A 受限并行 → P2B → P3 平台基础/独立扩展”的依赖关系；
- 是否接受 P0 必须同时关闭 14 个 Workspace 失败和独立 P0-MCP GAP，但 Workspace 测试不进入
  根静态 collection、`29` 不作为稳定平台契约；
- 是否接受固定原始 `openapi-mcp-server`/`mock_service` commit 只作为隔离合成夹具，且
  P0-MCP 不验证认证、生产安全或业务 Agent 能力；
- 是否接受 P1 的 8 案例、无工具、确定性评分和独立安全否决范围，以及 Workspace 可见回归包与
  evaluator-owned 发布基准分权；
- 是否接受 P1 只称协议化回归/发布准入，能力提升必须另有 paired、重复执行、隐藏样本和线上结果；
- 是否接受失败测评按 finding/case 关系通过幂等业务动作进入改进事项，而不是自动推进后续阶段；
- 是否接受 P2A 暂不迁移公开会话字段、不接第二生产 Runtime，但必须用第二类真实协议 spike
  验证中立边界；
- 是否接受 P2B 只评估精确 capability build 并保持 shadow，不提供自动启用；
- 是否接受 P3 是扩展组合准入框架，平台横切基础不被安全完整 MVP 永久阻断；
- 是否接受单组织先行、三类一级资产、Agent 详情/测评中心/测试资产的产品入口分工；
- 是否接受本轮不 bump `VERSION`、不建 tag、不变更运行卷。

上述裁决发生实质变化时，应先修订本索引和对应阶段方案，再进入实现。
