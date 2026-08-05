# AgentGov 术语与版本边界

> 文档定位：AgentGov 文档体系的术语和版本边界权威来源。
> 适用范围：产品目标、四阶段改进治理方案、当前实现基线、评审报告、测试用例和后续 docs 重构。
> 核心规则：长期产品、四阶段改进治理方案和当前实现使用统一领域术语；历史名称只在迁移、归档和负向测试中保留，并必须声明版本边界。

## 1. 为什么需要本文件

当前 `docs/` 同时包含三类材料：

1. 长期产品权威，例如 `项目目标愿景使命.md` 和 `AgentGov核心功能测试用例.md`。
2. 当前实现基线，例如反馈闭环、Workspace pytest、版本治理和业务 Agent 包方案。
3. 四阶段改进治理方案，例如 `AgentGov_四阶段改进治理工作台UI整改方案.md`；旧 ASCII 草图和对应设计一致性报告已进入归档，只作为历史设计证据。

这些材料产生于不同阶段，存在 `main agent`、`反馈信息`、`feedback signal`、`优化批次`、`proposal`、`ImprovementItem` 等多套名称。直接机械改名会掩盖当前实现事实，不改又会让读者误以为旧词仍是未来产品术语。

因此，本文件定义一条边界：

- **目标愿景和四阶段改进治理方案**：以业务 Agent、改进事项、反馈、系统理解、归因结果、优化方案、回归测试设计、Workspace 测试文件、平台测试运行、发布、资产 Registry 等领域术语为准。
- **当前实现基线**：使用当前代码、API、数据库和 UI 的真实名称；已删除名称只能出现在迁移说明或历史解释中。
- **历史评审报告**：保留当时证据和措辞，不回写成新的产品术语；如被引用，应通过本文解释映射关系。

补充权威规则：`docs/AgentGov_四阶段改进治理工作台UI整改方案.md` 与四张效果图是改进治理工作台 UI、流程、入口、决策卡和处理记录的最高依据。其他文档中任何七段链路、旧反馈工作台菜单、旧发布入口或旧测试验收描述与其冲突时，均按四阶段方案解释或废除。

## 2. 文档层级

| 层级 | 代表文档 | 术语要求 | 归档判断 |
| --- | --- | --- | --- |
| 长期产品权威 | `项目目标愿景使命.md`、`AgentGov核心功能测试用例.md` | 使用长期产品术语；引用实现字段时保留原名 | 不归档，除非有新的长期权威完全替代 |
| 当前实现基线 | `反馈闭环当前实现基线.md`、Workspace pytest、版本治理和 Workspace 包方案 | 使用当前真实实现名称，明确与四阶段方案的关系 | 仍解释运行态时保留；被新基线完全替代后归档 |
| 四阶段改进治理方案 | `AgentGov_四阶段改进治理工作台UI整改方案.md` | 使用四阶段改进治理统一术语；改进治理工作台以四阶段方案为最高依据 | 属于方案权威，不能替代当前实现基线 |
| 历史评审与复盘 | `docs/design_review_report/`、`docs/code_review_reports/`、`docs/codex_setting_review_reports/` | 保留历史证据原文；新增结论可引用本文 | 默认保留原路径，除非引用链已迁移 |

## 3. 改进治理工作台四阶段权威链路

改进治理工作台只允许以下四个用户可见阶段：

| 用户阶段 | 用户心智 | 废除的旧主路径 |
| --- | --- | --- |
| 反馈整理 | 整理反馈，确认问题对象是否成立 | 把 `feedback_intake`、`triage` 或“反馈信息”拆成独立用户阶段 |
| 归因分析 | 确认根因是否可信，是否进入方案生成 | 把归因 job、profile 或治理 Agent 职责名暴露成用户主路径 |
| 优化执行 | 确认方案是否可执行，并允许系统实施 | 把“优化批次”“proposal”“execution task”作为一级用户对象 |
| 测试发布 | 确认回归测试设计、生成 Workspace 测试文件、查看平台测试运行和发布条件 | 把“回归资产”“版本管理”独立菜单作为改进治理主路径 |

`feedback_intake → triage → attribution → optimization → execution → regression → release` 只能作为旧实现、内部子状态或迁移来源解释。若后端仍保留这些状态，前端、API DTO、Playwright、文档验收和用户帮助文案必须投影为四阶段。

## 4. 四阶段改进治理统一领域术语

| 中文展示名 | 英文领域名 | 代码 / API 词根 | 统一 ID | 说明 |
| --- | --- | --- | --- | --- |
| 业务 Agent | `BusinessAgent` | `agent` | `agent_id` | 被治理对象；所有注册业务 Agent（含 `main-agent`）遵循同一运行与治理机制。 |
| 业务 Agent 版本 | `BusinessAgentVersion` | 当前由 Workspace commit、待发布变更和 release 表达 | `agent_id + commit_sha` | 测试、测评、发布和回滚的精确被治理对象；长期可绑定 Runtime，但客户端不能逐请求改写。 |
| 业务 Agent Workspace | `BusinessAgentWorkspace` | `workspace` | 由 `agent_id` 归属 | Runtime 原生项目目录；当前 `claude-code` 实现承载 `CLAUDE.md`、`.mcp.json`、`.claude/` 等配置，其他 Runtime 使用各自原生包，不做自动翻译。 |
| 运行态 Workspace | `LiveWorkspace` | `workspace_dir` | 由 `agent_id` 归属 | `${RUNTIME_ROOT}/data/business-agents/<agent_id>/workspace/` 中当前实际运行和版本化的 Runtime 原生 Workspace。 |
| 业务 Agent Workspace 包 | `BusinessAgentWorkspacePackage` | `workspace/import`、`workspace/export` | 目标 `agent_id` | 完整 `.tar.gz` 交换包；普通新 Agent 的唯一创建输入，也是跨环境迁移和覆盖载体。 |
| 内置业务 Agent | `BuiltinBusinessAgent` | `builtin` | `agent_id` | 随当前代码版本提供出生 Workspace 的业务 Agent；当前唯一值为 `security-operations-expert`。 |
| 默认业务 Agent | `DefaultBusinessAgent` | `default` | `agent_id` | 未显式指定兼容入口 Agent 时使用的产品默认；当前为 `security-operations-expert`，不等同于内置或受保护属性。 |
| 受保护业务 Agent | `ProtectedBusinessAgent` | `protected` | `agent_id` | 不允许在线删除的业务 Agent；当前为 `security-operations-expert`。 |
| 运行卷初始化源 | `RuntimeBootstrapSource` | `runtime-bootstrap` | 不适用 | 仓库 `docker/runtime-bootstrap/`；仅用于初始化 governor 和显式内置 Workspace，不是模板 catalog 或运行态副本。 |
| Subagent | `Subagent` | `subagent` | `subagent_id` | 业务 Agent 内部可使用的子能力，不等同于业务 Agent。 |
| 治理 Agent | `Governor` | `governor` | `governor_job_id` | AgentGov 内部治理执行者，服务归因、方案、回归等流水线。 |
| 改进事项 | `ImprovementItem` | `improvement` | `improvement_id` | 四阶段改进治理用户主流程的一等治理单元，承接反馈到发布的闭环。 |
| 系统理解 | `NormalizedFeedback` | `normalized_feedback` | `normalized_feedback_id` | 系统把自然语言反馈整理成可确认、可归属、可治理的结构化理解。 |
| 反馈 | `Feedback` | `feedback` | `feedback_id` | 用户、业务系统或评估流程提供的质量信号与事实描述。 |
| 归因结果 | `Attribution` | `attribution` | `attribution_id` | 对问题来源、证据、责任边界和建议方向的解释。 |
| 优化方案 | `OptimizationPlan` | `optimization_plan` | `optimization_plan_id` | 可执行或可审批的改进方案，不等同于旧 `proposal` 文案。 |
| 执行记录 | `ExecutionRecord` | `execution` | `execution_id` | 后端受控应用改动、待发布版本、结果和审计记录。 |
| 回归测试设计 | `RegressionTestDesign` | `regression_test_design` | `regression_test_design_id` | 治理 Agent 生成并由用户确认的测试语义候选，不是可执行测试资产。 |
| Workspace 测试文件 | `WorkspaceTestFile` | `tests/test_*.py` | Git 路径 + `commit_sha` | 业务 Agent 可执行测试资产的唯一真相源，随 Workspace Git 版本化。 |
| 平台测试运行 | `AgentTestRun` | `agent_test_run` | `test_run_id` | 平台使用固定 pytest 命令在精确业务 Agent 提交上产生的持久化执行证据。 |
| 评测基准 | `EvaluationBenchmark` | 目标逻辑契约 | `benchmark_id` | 稳定命名的能力测量与治理容器，定义目的、能力维度、owner 和修订链；不是一次运行、具体试卷或脱离协议的总分。 |
| 评测协议修订 | `EvaluationProtocolRevision` | P1 先以受控评测包引用表达 | `protocol_id + revision/digest`，并归属 `benchmark_id` | 评测基准下不可变的测量合同，冻结 case/corpus、Ground Truth、scorer、安全门、采样和环境约束；正式发布修订不得由同一候选 Agent 变更，隐藏内容不得进入候选 Workspace。 |
| 评测执行 | `EvaluationExecution` | P1 目标为最小协议中立聚合，样本运行由 `AgentTestRun` 适配 | `evaluation_execution_id` | 把一个精确 Agent 版本、协议修订、purpose、Runtime/模型/工具/环境与 1..N 个独立 sample run 绑定；不复制运行事实，也不是旧 `EvalRun` 的兼容恢复。 |
| 评测结论 | `Assessment` | P1 目标 typed immutable record | `assessment_id`，归属 `evaluation_execution_id` | 对一次完成执行的 Scorecard、Violation、安全门和稳定性结论；不包含 baseline/candidate 可比性、人工决定或发布动作，被测 Agent 无权声明通过。 |
| 版本比较组 | `EvaluationComparisonGroup` | P1 目标 typed immutable record | `comparison_group_id` | 在兼容协议和环境下关联修复前与待发布版本的 execution/assessment，单独表达可比性和差异。 |
| 评测人工决定 | `EvaluationReviewDecision` | 目标 control-plane record | `review_decision_id` | 授权人员针对精确 assessment/comparison 作出的批准、拒绝或补证决定；不得回写机器评测事实。 |
| 发布门裁决 | `ReleaseGateDecision` | 目标 backend-owned record | `release_gate_decision_id` | 后端组合当前 Workspace 回归、assessment、comparison、安全门和所需人工决定形成的准入裁决；不等于 `Release` 本身。 |
| 线上效果 | `OnlineOutcome` | 外部事实引用 + AgentGov 受控投影 | 来源 ID + observation window | 发布后的业务效果、复发、成本、漂移和回滚信号；不能由离线分数替代。 |
| 修复前版本 | `BaseCommit` | `base_commit_sha` | `commit_sha` | 当前改进开始前的 Git 提交；用于 UI 对比。 |
| 待发布变更 | `AgentChangeSet` | `change_set` | `change_set_id` | 围绕一个业务 Agent 隔离、审查并准备发布的一组改动。 |
| 待发布版本 | `PendingReleaseCommit` | `candidate_commit_sha` | `commit_sha` | 当前待发布变更准备发布的精确 Git 提交；代码字段保留 `candidate_commit_sha`。 |
| 发布 | `Release` | `release` | `release_id` | 已满足发布条件并固化到业务 Agent 版本链的结果。 |
| 资产 Registry | `AssetRegistry` | `asset` | `asset_id` | 数据/证据、方法论和执行三类资产的关联视图；版本、provenance、审计、scope 和生命周期作为横切维度，不复制原生正文。 |
| Runtime 绑定 | `RuntimeBinding` | 近期由部署配置和 Agent 原生包共同约束 | backend-owned binding ID/digest | 业务 Agent 版本可使用的 Runtime 选择；一次 run 只绑定一个 Runtime，客户端请求不得覆盖。 |
| 治理身份 | `GovernancePrincipal` | 目标 control-plane contract；当前 API auth 尚不足以证明可信身份 | `principal_id` | AgentGov 自身治理操作的身份，不等同于外部业务系统的组织成员或业务角色。 |
| 资源范围 | `ResourceScope` | 目标 typed contract | scope ID/tuple | 约束 Agent、Workspace、评测包、trace、release 和 Governor 能力的可见与可操作范围；单组织部署也必须保留。 |
| Governor 能力版本 | `GovernorCapabilityVersion` | 目标 learning domain | capability key + version/build digest | 可执行 prompt、skill、job spec、typed contract 和方法 revision 的不可变组合；评测、激活和回退必须指向同一 build。 |
| Trace 摘要 | `TraceSummary` | `trace_summary` | `trace_summary_id` | 面向用户和治理流程的运行证据摘要，不暴露完整底层日志为主体验。 |
| 上下文包 | `ContextPackage` | `context_package` | `context_package_id` | 用于 AI 协作、Playwright 复现、问题转交和完整 JSON 导出的上下文。 |

## 5. 迁移前历史名称映射

| 历史名称 | 当前含义 | 四阶段改进治理映射 | 使用规则 |
| --- | --- | --- | --- |
| `main agent` / `main-agent` | 历史首个业务 Agent 示例 | 普通注册业务 Agent | 不再是默认、内置、受保护、模板或隐式兜底；长期文档只在历史说明或“所有注册业务 Agent（含 main-agent）”中使用。 |
| `main_agent_version_id` / `has_main_agent_version` / `main_agent_claude_md` | 旧代码把任意被治理业务 Agent 称为 main Agent | `business_agent_version_id` / `has_business_agent_version` / `business_agent_claude_md` | 活跃 OpenAPI、证据包和治理输出只使用中性字段；旧证据包由一次性数据库迁移改写，不保留双字段兼容。 |
| `seed` / `seed catalog` / `general template` | 已删除的业务 Agent 出生与创建双轨 | 运行卷初始化源 + Workspace 包导入 | 活跃实现和文档不得恢复；历史归档可保留。 |
| `origin=seed/user` | 已删除的注册表来源投影 | `builtin`、`default`、`protected` 三个独立派生属性 | 不再持久化，不得用于删除、准入或 UI 标签。 |
| `feedback signal` / `反馈信号` | 当前反馈来源或待关联信号 | `Feedback` 的来源类型之一 | 当前实现文档可保留；四阶段改进治理用户文案改为“反馈”。 |
| `反馈信息` | 当前反馈工作台用户主对象 | `Feedback` | 当前实现文档可保留；四阶段改进治理主流程用“反馈”。 |
| `feedback case` | 当前后端单反馈处理容器 | 可关联到 `ImprovementItem` 的证据与归因上下文 | 不作为四阶段改进治理用户一级对象。 |
| `feedback_intake / triage / attribution / optimization / execution / regression / release` | 当前或历史更细阶段/状态 | 投影为四阶段：反馈整理 / 归因分析 / 优化执行 / 测试发布 | 不作为改进治理工作台顶部阶段条。 |
| `optimization batch` / `优化批次` | 当前多条反馈合并生成方案的容器 | 由 `ImprovementItem` 聚合和阶段推进承接 | 不得把 `Batch` 继续当作四阶段改进治理用户主对象。 |
| `proposal` / `optimization proposal` | 当前方案生成 job 的输出命名 | `OptimizationPlan` | 当前代码/API 名可保留；用户主流程改为“优化方案”。 |
| `RegressionAssessment` / `regression-assessment` | 已删除的四阶段测试候选名称 | `RegressionTestDesign` / `regression-test-design` | 只允许出现在历史迁移、归档材料和旧入口不存在的负向断言中。 |
| `TestDataset` / `test_dataset` / 测试数据集 | 已删除的数据库测试内容副本和生命周期 | Workspace `tests/test_*.py` | 不作为当前资产、API、状态机或 UI 对象；历史 migration 可保留原名。 |
| `EvalRun` / `eval_run` / 评估运行 | 已删除的数据库数据集评估链 | 当前执行证据使用 `AgentTestRun`；长期中立概念使用 `EvaluationExecution` | 不恢复旧表、旧 API、数据集正文副本或逐 case review 链；新逻辑对象只能按新的协议权威和升级条件建立。 |
| 基线版本 / 候选版本 / 基线与候选 | 单独出现时含义不清的用户展示词 | 修复前版本 / 待发布版本；评测技术上下文可使用 baseline/candidate role | 代码字段 `base_commit_sha`、`candidate_commit_sha` 保留；UI 必须同时显示实际版本与协议，不用角色名替代版本身份。 |
| `反馈信息 / 优化批次 / 回归资产 / 版本管理` | 当前旧反馈工作台四菜单 | 四阶段改进治理工作台中的来源、方案、测试资产和发布条件能力 | 不作为改进治理工作台主导航或验收结构。 |
| 发布顶级入口 / `ReleaseWorkbench` | 当前或历史独立发布入口 | 测试发布阶段的发布条件预览与发布准备能力 | 不作为改进治理工作台外的默认主动作。 |
| `SDK 事件` | Playground 调试视图中的底层事件 | Trace / Trace 摘要 / Developer Debug | 用户主流程不以 SDK 事件为核心操作。 |
| `Run Summary` | 运行摘要 | `TraceSummary` 或运行证据摘要 | 需按用途区分面向用户的摘要与底层调试信息。 |
| 反馈优化 workspace | 当前旧反馈闭环工作台 | 四阶段改进治理改进事项闭环的能力来源 | 功能等价迁移前不能直接下线；迁移后再退役旧入口。 |

## 6. 资产分类、评测关系与产品入口

### 6.1 三类一级资产

| 一级资产 | 典型内容 | 不属于该分类的并列项 |
| --- | --- | --- |
| 数据/证据资产 | run、trace、feedback、evaluation result、release/rollback event、online outcome | version 和 audit 是治理维度，不另复制正文 |
| 方法论资产 | 归因方法、优化 SOP、评测规程、发布策略、回滚策略 | 方法修订号属于 version 维度 |
| 执行资产 | Agent 行为包、prompt、skill、profile、playbook、Workspace 测试、独立评测包、治理规则 | 执行记录属于数据/证据资产 |

所有一级资产统一携带 version/revision、digest、provenance、owner、applicability scope、审计、访问
边界、保留/删除状态和生命周期。`AssetRegistry` 只投影稳定引用和关系，不成为 Workspace、评测包、
Runtime 原生事实或外部业务数据的第二真相源。

### 6.2 测试、基准与发布评测

- 业务 Agent Workspace 测试属于 Agent 自有的可见工程契约和已知问题回归集；
- `EvaluationBenchmark` 是稳定治理容器，其 `EvaluationProtocolRevision` 是独立版本化、不可变的
  能力测量合同，可包含可见开发集和候选不可读取的 holdout；
- 平台发布评测使用指定基准、精确 Agent 版本和受控环境形成 `Assessment` 与版本比较证据；
- 线上效果记录发布后的真实业务结果、漂移、复发、成本和回滚条件。

领域关系固定为：`EvaluationExecution` 聚合 1..N 个 sample run，完成后产生独立
`Assessment`；`EvaluationComparisonGroup` 关联 baseline/candidate 的 execution/assessment；
`EvaluationReviewDecision` 和 `ReleaseGateDecision` 在其后形成，不回写前述事实。

“业务 Agent 测评”是上述基准、执行、结论和效果的产品能力总称；“平台发布评测”只是其中
`release_baseline/release_candidate` purpose 的受控用法，不等于所有开发诊断或周期评测。

测试通过、离线得分、发布成功和线上能力提升是四种不同结论，文档、API 和 UI 不得互相替代。

### 6.3 UI 入口归属

- “业务 Agent 详情 → 测评”是单 Agent 测评主入口；
- “独立测评中心”管理跨 Agent/协议 campaign、隐藏集、批次执行和人工评审；
- “资产复利 → 测试资产”管理测试源码、suite 和执行证据；
- “资产复利 → 治理资产”展示方法论与执行资产及其审计关系，不把审计重新定义为一级资产；
- 改进治理工作台只承接失败证据进入 `ImprovementItem` 后的四阶段闭环，不承担测评运营或
  Governor 元治理。

“测试资产”和“治理资产”是按用户任务组织的产品视图，不是两套新的一级资产分类。前者关联
Workspace 测试执行资产与对应运行证据，后者组织可复用的方法论/执行资产并展示 provenance 和
审计关系；审计记录只可追溯，不随资产继承。

P1 即建立“业务 Agent 详情 → 测评”主入口，现有测试运行详情只作为 sample/run 证据深链。出现
第二个 benchmark/protocol、跨 Agent campaign、持续隐藏集运营或专家评审队列任一条件时，必须
启用独立测评中心；单个受控 holdout 本身不要求预建全局运营菜单。

## 7. 写作规则

1. 写长期产品、目标愿景、四阶段改进治理方案、用户主流程时，优先使用第 4 节统一术语。
2. 写当前实现事实、API、数据库、pytest、OpenAPI、文件路径、环境变量时，保留真实标识符，不做表面改名。
3. 引用旧方案时，应说明它属于当前实现基线、历史评审或迁移前设计，不把旧词提升为未来产品术语。
4. 讨论多 Agent 时，必须区分业务 Agent、Subagent 和治理 Agent；所有注册业务 Agent（含 `main-agent`）遵循同一机制。
5. 讨论资产沉淀时，一级分类只使用数据/证据、方法论和执行资产；版本、provenance、审计、scope 和生命周期按横切治理维度表达。
6. 四阶段改进治理 UI、API、DTO、事件、ContextPackage 和 Playwright 选择器应同名同义；新增旧名别名必须有明确迁移理由。
7. 后续代码整改计划默认以四阶段方案为准；重构收益更大时，不为旧设计增加兼容层，除非用户明确批准。
8. 讨论业务 Agent 创建、迁移和出生配置时，只使用“业务 Agent Workspace 包”“运行态 Workspace”“内置业务 Agent”和“运行卷初始化源”；“模板”不用于指代导出的 `security-operations-expert` Workspace 包。
9. 讨论评测时，必须明确被测 Agent 版本、评测协议 revision、评测方、Runtime/模型/工具/环境和结果用途；不把 Workspace pytest、能力基准、发布裁决或线上效果混成一个“测评得分”。
10. 讨论平台身份时，必须区分 AgentGov 治理操作的 `GovernancePrincipal/ResourceScope` 与外部业务系统的组织、业务权限和生产审批。

## 8. 归档规则

四阶段改进治理方案出现后，旧文档不自动归档。当前实现基线文档仍承担三类价值：

- 解释真实运行态和代码行为。
- 保留迁移前的设计取舍、评审证据和风险清单。
- 支撑当前测试、部署、治理硬门和回归分析。

只有同时满足以下条件，才应移动到 `docs/archive/`：

1. 新文档已经完全替代其权威内容。
2. README、docs、`.planning`、代码注释和测试引用已迁移。
3. 归档索引记录原路径、归档路径、替代文档和归档日期。
4. 移动不会影响当前开发、测试、部署或治理流程。

2026-06-23 已完成一次强收敛：旧 ASCII 草图、三篇反馈闭环重复长文、长期回归资产旧完整稿和对应历史评审已迁入 `docs/archive/`。2026-07-18 起，原活跃长期回归资产方案也由 Workspace pytest 工程契约完全替代，不再保留平行活跃入口。后续新增或移动文档仍必须满足上述四项条件，不能只因为“旧”而归档。
