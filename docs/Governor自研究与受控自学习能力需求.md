# Governor 自研究与受控自学习能力需求

> 文档状态：产品能力目标方案。
>
> 权威边界：本文定义 Governor 自研究与受控自学习的产品目标、治理边界、需求和验收口径，
> 不表示当前 OpenAPI、数据库、Runtime 或前端已经具备对应能力，也不预设具体 API、存储、
> Job、UI 或技术架构。
>
> 关联权威：[项目目标愿景使命](./项目目标愿景使命.md)、
> [AgentGov 术语与版本边界](./AgentGov术语与版本边界.md)、
> [反馈闭环当前实现基线](./反馈闭环当前实现基线.md)。

## 1. 治理对象预检

### 1.1 治理对象矩阵

| 维度 | 结论 |
| --- | --- |
| 被治理对象 | 一阶对象仍是所有注册业务 Agent；新增的二阶治理对象是 Governor 的归因、优化、执行规划和回归保障方法及其能力版本。 |
| 治理执行者 | Governor 自主发现问题、开展研究和提出方法候选；平台负责确定性证据投影与门禁；独立评估和授权人员负责正式启用。 |
| 资产类型 | 一级资产只有数据/证据、方法论和执行三类；version、provenance、audit、scope 与 lifecycle 是贯穿三类资产的治理维度，不再并列为第四、第五类资产。 |
| 生命周期 | 研究问题、证据与假设、方法候选、评估、启用、持续验证、降级或淘汰构成完整生命周期；这些名称表达需求语义，不预设持久化状态枚举。 |
| 反馈归属 | 原始证据始终归属到业务 Agent、业务 Agent 版本、场景和改进事项；Governor 能力效果还必须归属到对应 Governor 能力版本。 |
| 当前实现边界 | Governor 是近似无状态的事项级治理执行者，按当前事项证据生成归因、优化、执行和回归候选；不存在持续研究、跨任务记忆、自身评估和受控启用闭环。 |
| 目标能力边界 | 形成“内部证据为主、联网研究为辅、研究自主、启用受控、全局可发现、仅在批准 `ApplicabilityScope` 生效、离线仍可运行”的 Governor 能力演进机制；全局可发现不等于全局默认生效。 |

Governor 因自身能力进入二阶治理而成为“被治理的能力载体”，不因此成为业务 Agent。它仍不进入
业务 Agent 注册表、Playground 业务选择器或业务 Agent 发布链，也不对外暴露为可编排对象。

### 1.2 闭环链路

```text
业务 Agent 运行
-> 反馈、归因、优化、执行、测试、发布或回滚
-> 识别 Governor 能力缺口
-> 内部证据研究 + 可选外部研究
-> 形成带来源、适用条件、反证和不确定性的方法候选
-> 后端物化 immutable GovernorCapabilityVersion build
-> 后端投影独立 ApplicabilityScope proposal
-> dev pack 迭代
-> 候选不可读 holdout 上的 blind current/candidate 独立评估
-> immutable EvaluationOutcome
-> 授权人员作出独立 HumanReviewDecision
-> 平台以 ActivationRecord 变更 scope-aware active binding
-> Governor 精确能力 build 在批准 scope 内生效
-> 后续反馈分析优化闭环验证真实净效果
-> 持续观察、保留、回退、降级或淘汰
-> 更新资产 Registry 和审计记录
```

### 1.3 风险自检

- 不把当前 Governor 的静态、只读执行方式误当长期产品边界。
- 不把运行数据、Trace 或一条 AssetRecord 直接等同于已经验证的方法论资产。
- 不混淆业务 Agent 与 Governor：业务 Agent 是长期一阶治理对象，Governor 是内部治理执行者和
  二阶能力治理对象。
- 不丢失反馈路由：跨 Agent 共享知识时仍保留来源 Agent、版本、场景、反例和适用条件。
- 不允许 Governor 同时担任方法候选的提出者和唯一裁决者。
- 不用 Governor 可读的 dev case 或候选自报分数替代评估方所有的 holdout 和盲化裁决。
- 不把方法文字本身当作已评估能力；被评估、启用和回退的必须是同一精确 immutable build。
- 不把“自研究”“自学习”等能力词直接变成四阶段改进治理的新按钮、用户阶段或平行工作台。

本文不改变“反馈整理、归因分析、优化执行、测试发布”四阶段用户主链路。Governor 的自研究和
受控自学习属于平台内部元治理闭环，不构成第五个用户阶段。

## 2. 背景与实际问题

AgentGov 已能让 Governor 围绕单个改进事项生成归因、优化方案、受控执行操作和回归测试代码候选，
并通过业务 Agent Workspace、精确 Git 提交、平台测试运行、发布与回滚形成可审计的业务 Agent
改进链路。

当前 Governor 的能力仍主要来自固定的 Workspace 指令、Skill、按 Job 类型选择的 prompt 和结构化
输出契约。每次治理任务聚焦当前事项和当前证据，不会系统性吸收历史事项中的人工修订、执行失败、
测试结果、发布结果、回滚原因和问题复发情况，也不会把跨 Agent 反复出现的有效方法沉淀为可验证、
可版本化、可淘汰的 Governor 治理能力。

这导致以下产品问题：

- 同类归因和优化问题可能在不同事项中被重复分析，成功经验不能稳定复用。
- 用户对 Governor 产物的实质性修订没有完整转化为后续治理能力。
- 测试通过、发布成功、问题复发和回滚等下游结果没有成为 Governor 方法有效性的反馈。
- 通用资产可以被记录或复制，但“已记录”不能证明“适用于当前场景”或“已经产生正向效果”。
- Governor 能发现当前事项中的配置问题，但不能主动识别自身长期存在的分析盲区和方法缺口。

## 3. 产品目标与成功原则

Governor 能够持续从全平台业务 Agent 的运行、反馈、人工修订、测试、发布、回滚和问题复发结果中
识别自身治理能力缺口，自主开展内部证据研究，并在联网且提供搜索能力时辅以可追溯的外部研究。
研究结果形成带来源、适用条件、反证、不确定性和验证依据的方法候选。候选必须先由平台物化为精确、
不可变的 Governor 能力 build，经候选不可读的 holdout 盲化评估产生独立结果，再由
授权人员作出与评估分离的人工决定。只有平台写入 ActivationRecord 并变更精确
ApplicabilityScope 的 active binding 后，该 build 才能在当前业务 Agent 和场景选择性使用。

本能力的首要成功标准是提升“反馈分析优化闭环”的真实净效果，而不是增加研究次数、知识条目数量
或网络搜索量。净效果包括：

- 归因更准确、证据更充分、责任边界更可信。
- 优化方案更可执行，受控执行中的拒绝、返工和无效变更更少。
- 回归测试更能覆盖真实失败模式，待发布版本通过完整测试并成功发布的比例提高。
- 同类问题复发、发布后退化和回滚减少。
- 全过程不牺牲离线可用、安全、隐私、业务 Agent 隔离和人工责任边界。

## 4. 术语定义

| 术语 | 定义 |
| --- | --- |
| 自研究 | Governor 主动提出与自身治理能力相关的研究问题，收集内部证据，并在条件允许时查阅外部资料，形成可证伪的结论和方法候选。 |
| 受控自学习 | Governor 根据研究和后续效果提出自身能力变更，但变更必须经过独立评估、人工启用、版本记录和持续验证；不包含无监督直接自改。 |
| 学习证据 | 能证明 Governor 某种分析或优化方法有效、无效或仅在特定条件下有效的事实，包括成功案例、失败案例、人工修订、测试、发布、回滚和复发结果。 |
| 方法候选 | 尚未正式生效的归因方法、优化 SOP、prompt、skill、playbook、评估规程或其他治理执行资产。 |
| GovernorCapabilityVersion build | 由平台将 allowlist 准入的 Workspace 行为配置、rules/skills、job spec、prompt builder、typed output/projection 和方法 revision 可执行内容物化得到的精确不可变执行版本；方法文字不等于 build。`ApplicabilityScope`、active/shadow binding、运行时 Runtime binding、model/provider 与 dev/holdout pack 属于横切关系/provenance，不进入 build digest。 |
| `ApplicabilityScope` | 方法/build 可安全适用的 capability key、业务域、Agent、场景/问题类型、风险等级、Runtime 约束及不适用条件的 typed 边界。 |
| dev pack | 位于 evaluator-owned 版本化存储、通过明确只读授权供候选研究、构建和快速回归使用的可见评估资产，不用于独立发布裁决，不进入 build。 |
| holdout pack | 位于 evaluator-owned 版本化存储、候选生成上下文与 candidate build 不可读的隔离评估资产，只向受控 evaluator 暴露必要输入，不进入 build。 |
| `EvaluationOutcome` | 绑定 current/candidate build、scope、holdout/protocol/scorer/evaluator 版本、blind mapping 和运行 provenance 的不可变评估事实。 |
| `HumanReviewDecision` | 授权人员基于评估事实对指定 scope 作出的 `approved_for_canary | rejected | deferred` 决定；不是评估字段。 |
| `ActivationRecord` | 平台实际变更或回退 scope-aware active binding 时创建的记录，包含 from/to build、scope、binding revision、principal、观察和回退门。 |
| 全局知识池 | 所有业务 Agent 的已验证治理经验均可被 Governor 发现并提议进入目标 scope 验证的逻辑范围；全局可发现不等于可直接复用或全局无条件生效。 |
| 适用上下文 | `ApplicabilityScope` 的业务语义解释；不能仅以一段自由文本作为线上解析依据。 |
| 外部研究 | 在联网环境且提供搜索或检索工具时，对标准、官方文档、研究论文和可信实践资料开展的可追溯研究。 |
| 独立评估 | 不由提出方法候选的同一 Governor 单独裁决；候选不可读 holdout，evaluator 不知 blind A/B 身份，确定性门先于模型评判。 |

“学习”默认指方法论资产和执行资产的受控演进，不指底层模型参数训练或在线权重更新。

## 5. 范围与非目标

### 5.1 本期需求范围

- 从全平台闭环结果中发现 Governor 的重复失败、证据盲区、方法冲突和能力缺口。
- 以内生运行与治理证据为默认研究基础，覆盖成功、失败和反例。
- 在联网且提供搜索能力时开展外部研究，并保留可核验来源。
- 形成带适用条件和验证语义的方法候选。
- 由平台将方法候选物化为精确 immutable GovernorCapabilityVersion build，
  并将适用语义投影为与 build 独立的 `ApplicabilityScope` proposal；不直接评估一段方法文字。
- 让已验证经验进入全局知识池供发现，但只能通过 scope-aware active binding
  在已批准 Agent 和场景中生效。
- 对 current/candidate build 进行 dev/holdout 隔离和 blind A/B 独立评估，分别记录
  `EvaluationOutcome`、`HumanReviewDecision` 和 `ActivationRecord`。
- 把候选 build 启用后的真实业务结果继续反馈到 Governor 能力治理闭环，越过安全或
  退化门时按 ActivationRecord 中的回退目标和触发器收敛。
- 让 Governor 使用学习成果时能够解释其来源、适用性和不确定性。

### 5.2 明确非目标

- 不训练、微调或在线更新底层模型权重。
- 不允许 Governor 直接修改自身 Workspace、绕过后端门禁或自行正式启用能力。
- 不把公网、远程搜索服务或外部 SaaS 变成必需工作流依赖。
- 不把 Governor 注册成业务 Agent，也不向外部集成方暴露 Governor 编排入口。
- 不新增四阶段改进治理的用户阶段，不重构当前工作台信息架构。
- 不在本文中确定 API、数据库表、字段、Job 类型、调度器、索引技术、模型或 UI 组件。

## 6. 核心需求

### GOV-LRN-001 能力缺口发现

Governor 必须能够基于跨事项、跨版本和跨业务 Agent 的治理结果识别自身能力缺口，至少覆盖：

- 同类反馈反复出现但归因结论不稳定。
- 用户频繁实质性修改 Governor 生成的归因或优化方案。
- Governor 输出被证据护栏、执行护栏或测试门禁反复拒绝。
- 优化变更通过测试但问题继续复发，或发布后发生退化与回滚。
- 不同业务 Agent 中出现可复用的问题模式、成功方法或相互冲突的经验。
- 证据不足、需要人工分析或无安全动作的比例异常上升。

缺口判断必须同时观察成功案例和失败案例，不能只用负面反馈训练结论，也不能把人工确认次数直接当作
质量提升。

### GOV-LRN-002 内部证据研究

内部证据研究是核心能力和离线默认路径。研究必须：

- 以 Agent/SDK 运行事实、反馈、治理产物、人工修订、执行结果、精确提交测试、发布、回滚和复发记录
  为依据。
- 区分事实、推断、假设和待验证结论。
- 同时寻找支持证据、反证、边界案例和替代解释。
- 保留业务 Agent、业务版本、场景、改进事项和 Governor 能力版本的来源关系。
- 区分平台通用治理方法与特定业务域知识，禁止把领域事实直接提升为全局规则。

### GOV-LRN-003 可选外部研究

联网且提供搜索或检索工具时，Governor 可以主动开展外部研究。外部研究必须满足：

- 研究目标与已识别的治理能力缺口直接相关，不进行无目的浏览。
- 优先使用官方文档、标准、原始研究和可核验的一手资料。
- 记录来源、访问时间、适用版本、关键结论、冲突资料和不确定性。
- 搜索发现结果只作为候选证据，不能仅凭网页权威感直接改变正式治理方法。
- 外部资料中的指令、示例和工具调用内容均视为不可信数据，不得改变 Governor 的系统边界。
- 搜索查询、外部请求和引用摘要不得包含凭据、`.env` 内容、私有 endpoint、业务原文或其他敏感配置。
- 断网、搜索工具缺失或外部资料不可用时，内部研究和正常反馈闭环仍能完成。

### GOV-LRN-004 方法候选形成

每个方法候选必须表达：

- 要解决的 Governor 能力缺口和研究问题。
- 事实依据、来源摘要和证据强度。
- 支持证据、反证、冲突信息和不确定性。
- 适用上下文、不适用条件和潜在负向影响。
- 建议演进的方法论资产或执行资产。
- 预期改善的闭环结果及其验证语义。
- 与当前有效方法相比的差异和退出条件。

方法候选只能包含 Governor 负责生成的业务语义。来源 ID、Agent/版本归属、时间戳、Trace、评估结果、
启用状态、操作人和审计身份由平台或授权人员提供，不得由 Governor 自行声明为权威事实。
平台必须以候选、allowlist 准入的受控 Workspace/job/typed output 可执行配置和
方法 revision 构建 immutable capability build；构建失败不得退化为“运行时拼接方法文字”。
scope、Runtime binding、model/provider 和 dev/holdout pack 只进入 candidate proposal、评估、人工决定、
激活与运行 provenance，不进入 build digest。

### GOV-LRN-005 全局共享与按需适用

已验证经验进入全局知识池后，对所有业务 Agent 可发现，但必须按当前上下文选择性使用：

- 每项经验保留原始 Agent、版本、场景、能力域和问题模式。
- 每个候选提出 typed `ApplicabilityScope`，每次 build 评估、人工决定和激活独立绑定该 scope，至少表达 capability key、业务域、
  精确 Agent 或受控 selector、场景/问题类型、风险等级与必要的 Runtime 约束。
- Governor 使用经验前核对当前场景是否满足适用条件，并主动检查反例。
- 来自单个业务 Agent 的成功经验不能自动成为所有 Agent 的默认行为。
- 跨 Agent 复用必须说明迁移依据，并在目标上下文中重新验证。
- 全局知识池只提供发现；线上只能通过 backend-owned
  `capability_key + scope_digest -> active build` 绑定解析。冲突或无批准匹配时 fail closed，
  不自动选择“最相似”方法。
- 不适用、失效或产生负迁移的经验必须能够降级或淘汰，不能因已进入全局池而永久生效。

### GOV-LRN-006 独立评估

方法候选正式启用前必须先物化为精确 immutable build，再经过独立评估：

- current build 与 candidate build 必须使用同一 `ApplicabilityScope`、协议、Runtime/模型条件、
  采样规则和 holdout pack；条件不同时不得直接比较。
- dev/holdout pack 均位于 evaluator-owned 版本化存储并排除在 build 外；dev pack 可通过
  明确只读授权供研究与 build 迭代，holdout pack 不得进入 Governor
  Workspace、候选生成上下文、candidate build 或其可读工具路径。
- 后端将两组脱敏输出盲化为 A/B，evaluator 不可见 build 身份和 blind mapping；
  确定性安全门先执行，模型评判不能推翻安全失败。
- 同时评估归因质量、证据可信度、优化可执行性、回归保障质量和下游闭环结果。
- 同时覆盖正常、失败、证据不足、恶意输入、跨 Agent 误用和敏感信息场景。
- `EvaluationOutcome` 必须绑定两个 build digest、scope、pack/protocol/scorer/evaluator 版本、
  blind mapping 与运行 provenance，并以 `passed | failed | inconclusive` 表达事实结果。
- Governor 不能修改评估事实、blind mapping、门禁结果、scope 或审计身份，也不能单独
  宣布自身候选通过。
- 任何关键安全场景失败、主要闭环指标发生实质退化或无法解释收益来源时，候选不得启用。

### GOV-LRN-007 人工启用与责任边界

Governor 可以自主研究、归纳和提交方法候选，但人工决定与平台激活必须分离：

- `HumanReviewDecision` 必须关联不可伪造的 principal/role、理由、查看的
  `EvaluationOutcome`、精确 build 和 scope；不写入 outcome 或 candidate status。
- 授权人员能够查看方法差异、适用范围、评估结果、风险和回退条件。
- 平台只有在仍有效的 `approved_for_canary` HumanReviewDecision 存在、scope 完全相符、
  exact EvaluationOutcome/build 已被评估且 binding revision/CAS 通过时，才可变更
  active binding 并写入独立 `ActivationRecord`。
- Governor 不得通过生成内容伪造人工确认、平台评估或正式版本状态。
- 高风险方法即使评估通过，也不能绕过既有业务权限和外部系统责任边界。

### GOV-LRN-008 持续效果反馈

候选 build 启用后必须按 `ActivationRecord` 中的线上观察契约继续以真实闭环结果验证：

- 启用前固化 baseline build、基线窗口、目标指标、安全否决、最大可接受退化、
  线上观察窗口、最小样本条件和可回退 build。
- 能关联后续 Governor 产物与当时生效的精确 build、scope、ActivationRecord
  和所使用的方法资产。
- 能比较启用前后的人工修订、护栏拒绝、执行、测试、发布、复发和回滚结果。
- 能发现只提升表面指标、却降低实际问题解决率或安全性的指标投机。
- 安全/越权失败、scope 错用、关键指标越阈、证据或 Runtime coverage 不足时立即触发
  回退或 fail closed；持续退化、适用条件失效或外部知识过期时触发复核、降级或淘汰。
- 失败、观察和回退记录作为数据/证据资产，由 audit/version/provenance 维度连接，
  不能被后续成功结果覆盖。

### GOV-LRN-009 可解释性与可审计性

Governor 使用学习成果生成归因、优化或回归候选时，必须能够解释：

- 使用了哪类方法和为什么适用于当前上下文。
- 结论基于哪些内部证据或外部来源。
- 哪些事实属于当前事项，哪些属于历史经验或外部研究。
- 存在哪些反证、不确定性和需要人工判断的边界。
- 当前生效的精确 Governor capability build、ApplicabilityScope、ActivationRecord
  及对应审计关系。

对用户和审计面输出的解释不得泄露原始密钥、私有配置或其他业务 Agent 的敏感内容。

### GOV-LRN-010 核心闭环不受阻断

- 自研究与学习任务不可用时，现有事项级 Governor 能力仍按明确失败或保守回退语义运行。
- 外部研究不进入离线必需链路，也不成为普通业务 Agent 请求的前置条件。
- 研究规模、调用成本和执行时长不得无界增长，不能用长期研究阻塞正常反馈处理。
- 研究失败不得伪装成已学习、已验证或已启用。

## 7. 字段与事实所有权

本节只定义所有权，不预设具体 schema。

| 所有者 | 负责内容 | 不得负责 |
| --- | --- | --- |
| Governor | 研究问题、业务假设、证据摘要、方法候选、适用/不适用语义、反证、不确定性、风险说明和验证建议。 | ID、Agent/版本归属、typed scope、build/digest、时间戳、Trace 身份、评估结果、人工决定、binding 或审计身份。 |
| 平台后端 | 来源引用、Agent/版本/场景归属、typed `ApplicabilityScope`、immutable build、Runtime binding、确定性 provenance、dev/holdout 访问边界、blind mapping、评估输入/结果、binding CAS、时间戳、门禁和审计关系。 | 替代 Governor 生成开放性的业务归因、方法假设和优化语义。 |
| 独立 evaluator | blind A/B 分项判断、风险说明和不确定性；平台投影为 `EvaluationOutcome`。 | 查看 blind mapping、改写 build/scope/holdout/门禁、作出人工决定或变更 active binding。 |
| 授权人员 | `HumanReviewDecision` 的启用评审、拒绝或延后决定及理由；启用后的降级、回退和淘汰另作为受权控制动作，由平台写入 `ActivationRecord` 或退役记录。 | 伪造评估事实、来源证据、系统执行结果或绕过平台直接改 binding。 |
| 边界层 | 数据库、HTTP、文件、日志、Trace 和外部资料快照等序列化表示。 | 成为内部业务语义或生命周期事实的平行真相源。 |

任何 Governor 输出中出现的平台权威字段都必须被忽略或由平台权威值覆盖。

## 8. 资产分类与生命周期要求

### 8.1 资产分类

| 资产层 | 典型内容 | 要求 |
| --- | --- | --- |
| 数据/证据资产 | run、trace、feedback、人工修订、评估结果、决定、激活、测试、发布、回退、复发和研究来源。 | 保留来源、时间、Agent/build、scope、访问边界和保留/删除策略。 |
| 方法论资产 | 归因方法、优化 SOP、证据评判法、评估规程和发布判断原则。 | 表达适用条件、反证、验证结果和退出条件。 |
| 执行资产 | immutable capability build、prompt、skill、profile、playbook、dev/holdout pack、评估协议和自动化治理规则。 | 可版本化、可比较、可验证，不与方法文字或自然语言摘要形成双轨真相。 |

version、provenance、audit、scope 和 lifecycle 是横切治理维度：它们必须能还原每次
Governor 产物的精确 build/scope、评估、人工决定、激活/回退和责任关系，但不因此创建
“版本资产”或“审计资产”两类并行资产。不可变是指关系和决策事实不被覆盖，不要求敏感 raw
payload 无期保留；原文过期/授权删除后应保留 digest、tombstone、删除原因与审计关系。

### 8.2 生命周期语义

| 对象/阶段 | 进入条件 | 退出条件 |
| --- | --- | --- |
| MethodCandidate | 存在明确能力缺口、证据、适用/不适用语义和验证建议。 | 由平台物化为 immutable build，或因证据/构建失败而停止；候选不直接进入“已启用”。 |
| CapabilityBuild | build manifest、digest 与 allowlist 准入的可执行行为内容已冻结；scope、Runtime binding 和 pack 在 build 外。 | 对一个或多个 scope 分别进入 dev validation 和 blind holdout evaluation；build 本身始终不可修改。 |
| EvaluationOutcome | current/candidate build、scope、holdout/protocol/scorer/evaluator、blind mapping 和运行条件已固定。 | 产生 passed、failed 或 inconclusive 不可变事实；不直接改 binding。 |
| HumanReviewDecision | 授权人员查看完整 outcome、build、scope、风险和回退条件。 | 记录 `approved_for_canary`、`rejected` 或 `deferred`；不写入 outcome。 |
| ActivationRecord | 针对 exact outcome/build/scope 且仍有效的 `approved_for_canary` 决定存在，binding CAS 通过。 | 变更 scope-aware binding 并进入 canary 观察，或回退到记录中的 baseline build。 |
| 已退役 build | 已被替代、长期无效、scope 失效或风险不可接受。 | 不再进入正常 binding 解析；保留必要 digest、provenance 与审计关系。 |

这些阶段不要求新增同名用户按钮或 API；具体实现必须在设计阶段证明每个持久状态确有生命周期约束，
并使用集中状态机和完整转移表。

## 9. 成功指标与启用门槛

### 9.1 主要闭环指标

- 归因结果经人工确认前无需实质性修改的比例。
- 证据引用有效、责任边界准确且经后续事实验证的比例。
- 优化方案通过目标、路径和内容护栏并形成有效变更的比例。
- 回归测试能够复现原问题并阻断退化的比例。
- 待发布精确提交通过完整 Workspace 测试并成功发布的比例。
- 同类问题复发、发布后退化和回滚的变化。
- Governor 输出进入证据不足、人工复核或保守回退的原因分布。

### 9.2 安全与治理指标

- 未经独立评估、`HumanReviewDecision` 和 `ActivationRecord` 的候选 build
  正式生效次数必须为零。
- 候选生成上下文/candidate build 读取 holdout 正文、期望语义、scorer 秘密或 blind mapping
  的次数必须为零。
- 实际 active build/scope 与被评估、人工决定或 ActivationRecord 记录不一致的次数必须为零。
- 密钥、私有 endpoint、跨 Agent 敏感内容进入外部查询或对外解释的次数必须为零。
- 不满足适用条件却被跨 Agent 使用的方法次数必须为零。
- 外部资料中的提示注入改变 Governor 权限、目标或事实所有权的次数必须为零。
- 断网情况下内部研究和现有反馈闭环必须保持可用。

### 9.3 最小启用门槛

一个方法候选已物化的精确 capability build 只有同时满足以下条件才能启用：

- 所有关键安全、越权、恶意输入和离线场景通过。
- current/candidate build 在同一 scope、holdout/protocol/scorer/evaluator 和运行条件上得到
  `passed` `EvaluationOutcome`，且至少一个目标闭环结果明确改善。
- 其他主要闭环指标没有发生实质退化。
- 改善能够由候选方法解释，而不是来自评估数据泄漏、模型偶然性或放宽门禁。
- 授权人员以可验 principal/role 写入针对 exact outcome/build/scope 的
  `approved_for_canary` `HumanReviewDecision`，确认风险、线上观察、最大退化和回退条件；
  canary 启动时该决定仍在有效期内。
- 平台以 binding CAS 写入 `ActivationRecord`；实际 from/to build、scope 和评估/决定完全一致，
  启用后立即进入记录中的观察窗口。

## 10. 验收场景

| 场景 | 验收结果 |
| --- | --- |
| 离线内部研究 | 无公网和搜索工具时，Governor 能从内部成功与失败证据形成方法候选；现有反馈闭环不被阻断。 |
| 联网外部研究 | 搜索只围绕明确能力缺口，结果保留来源、时间、版本、冲突和不确定性，且不能直接正式生效。 |
| 外部提示注入 | 网页要求泄露配置、改变权限或绕过评估时，Governor 将其视为不可信数据并拒绝执行。 |
| 敏感配置保护 | 研究证据包含 `.env`、凭据或私有 endpoint 时，外部查询和对外解释不包含原始敏感值。 |
| 跨 Agent 复用 | Agent A 的成功经验进入全局池后，对 Agent B 仅可发现；无独立评估和批准 Agent B `ApplicabilityScope` binding 时不生效。 |
| build 完整性 | 方法文字相同但 allowlist 内 prompt/skill/job spec/typed output 或 native package 可执行内容任一 digest 不同时，形成新 build，不复用旧评估结果。 |
| build 与横切关系 | 只变更 scope、Runtime/model 运行条件或 dev/holdout pack 时 build digest 不变，但必须建立新 EvaluationOutcome，不得复用不可比的结果。 |
| dev/holdout 隔离 | Governor 可用 dev pack 迭代，但候选生成上下文、candidate build 和其工具均无法读取 holdout 正文、期望语义或 scorer 秘密。 |
| blind A/B | evaluator 只看到脱敏 A/B 输出与评分规程，不知 build 身份；后端 blind mapping 不能被 evaluator/Governor 改写。 |
| 独立评估失败 | 候选由 Governor 自评通过，但 `EvaluationOutcome` 为 failed/inconclusive 或确定性安全门失败时，候选 build 不得启用。 |
| 评估/决定/激活分离 | 评估通过后仍需授权人员写入独立 HumanReviewDecision；平台只在 CAS 成功时写入 ActivationRecord，三者不共用一个 status。 |
| 人工修订回流 | 用户实质修改 Governor 产物后，原始产物、修订差异和最终结果均可用于后续能力缺口分析，不互相覆盖。 |
| 启用后退化 | 新 build 导致安全/scope 错用或测试、发布、复发、回退等指标越过 ActivationRecord 门限时，系统能定位 build/scope 并原子回退到记录的 baseline。 |
| 四阶段主链路 | 自研究与受控自学习不增加改进治理工作台的第五阶段，也不把内部研究对象暴露为用户一级对象。 |

## 11. 可行性分析

### 11.1 现有可复用基础

- 当前反馈闭环已经产生反馈、归因、优化、执行、精确提交测试、发布和回滚证据。
- Governor 已使用独立 Workspace 和 Runtime Profile，并保持读取与生成、后端受控写入之间的边界。
- Governor 输出已有结构化校验、证据边界护栏和 Langfuse Trace 引用。
- 业务 Agent 的候选变更、完整 Workspace 测试、发布和回滚已经具备版本治理基础。
- 当前 Asset Registry 已有方法论、执行记录及审计用途元数据入口，但尚不能表达
  immutable capability build、ApplicabilityScope、评估/决定/激活关系，也不能由此宣称
  已具备安全跨 Agent 复用。

### 11.2 关键能力缺口

- 没有 MethodCandidate -> immutable CapabilityBuild -> EvaluationOutcome ->
  HumanReviewDecision -> ActivationRecord -> online observation/rollback 的完整生命周期。
- Governor 每次任务主要使用当前事项上下文，没有跨事项经验召回和长期研究记忆。
- 当前资产记录不足以表达来源可信度、适用条件、评估结果、版本、失效和淘汰。
- 没有 dev/holdout 权限隔离、blind A/B 和 scope-aware active binding。
- 人工修订、测试、发布、回滚和复发结果尚未形成不可变的 Governor 学习证据链。
- Governor 自身不具备与业务 Agent 等价的测试和发布链，当前静态版本标识不能证明能力演进。
- 现有 WebFetch 只用于核对明确 URL，不等同于主动搜索和外部研究来源治理。

### 11.3 结论

该需求与 AgentGov“把运行、反馈、归因、优化、评估和版本演进沉淀为可复用治理资产”的长期定位一致，
现有反馈闭环、受控执行、测试发布、Trace 和资产 Registry 提供了较强基础。

整体可行性为“有条件可行”。主要难点不是让 Governor 多读取一些资料，而是建立学习证据、
方法候选、immutable capability build、独立评估、人决定、平台激活、scope-aware binding
和效果回流组成的二阶治理闭环。该能力属于新的产品能力层，
不能作为单一 prompt、Skill 或无状态记忆增强处理。

## 12. 未采用的主要替代方案

| 替代方案 | 本期不采用原因 |
| --- | --- |
| 只增加历史案例检索 | 能提高召回，但不能判断经验是否有效、适用于何种场景，也不能治理 Governor 方法本身。 |
| 直接微调底层模型 | 训练成本、可解释性、回退和数据隔离复杂度高，且现有问题首先是证据、方法和评估闭环缺失。 |
| Governor 完全自主修改并启用自身能力 | 提出者与裁决者合一，容易产生自证正确、指标投机、安全绕过和不可追责变化。 |
| 所有经验全局统一生效 | 会把特定业务域经验错误推广到其他 Agent，产生负迁移和跨 Agent 污染。 |
| 把开放网络研究设为必需链路 | 违反离线产品不变量，并引入外部依赖、敏感信息、提示注入、时效和许可风险。 |

## 13. 兼容与修订条件

- 本文不改变当前 API、数据库、配置、Docker 卷、业务 Agent Workspace、四阶段 UI 或发布条件。
- 后续实现不得恢复已删除的全局数据库测试集或旧评估链；Governor 的独立评估必须与当前
  Workspace pytest 和精确提交证据边界协调，但不能复制业务 Agent 测试正文。
- 后续实现若修改公开 API、Runtime Profile、环境变量、持久化数据或用户可见流程，必须另行完成
  契约设计、迁移说明、OpenAPI/前端类型、文档和测试同步。
- 当能力完成实现并成为当前运行态时，应同步更新
  [反馈闭环当前实现基线](./反馈闭环当前实现基线.md)；本文继续承担需求依据，直到被新的权威需求完整替代。
- 若离线产品不变量、Governor 内部角色、跨 Agent 共享策略或人工启用原则发生变化，必须重新评审本文。
- 若该能力取消或被其他机制完整替代，应将本文移入归档并在归档索引记录替代关系。
