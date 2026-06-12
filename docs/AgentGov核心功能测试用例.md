# AgentGov 核心功能测试用例

本文档由 [项目目标愿景使命](./项目目标愿景使命.md) 派生，用于指导 AgentGov 的研发、验收和回归验证持续向目标愿景使命逼近。

它不是接口实现说明，也不是某个版本的完成承诺。它是一组目标牵引型核心功能测试用例：当前已经具备的能力应持续回归，尚未完全具备的能力应作为后续开发的验收锚点。

## 使用方式

- 每个新增功能、重构或治理改动，都应至少挂接到本文档中的一个核心用例。
- 每个优化批次、版本发布或重大方案评审，都应检查是否增强了至少一个目标用例，且没有破坏已有 `current` 用例。
- 本文档中的 `gap` 和 `future` 用例不要求当前版本全部通过，但它们定义了后续研发要逼近的方向。
- 当功能从 `gap` 或 `future` 进入可验证状态时，应把状态更新为 `current`，并补充可运行测试、API 验证、UI 验证或人工验收路径。

## 状态标记

| 状态 | 含义 | 研发用途 |
| --- | --- | --- |
| `current` | 当前系统应具备，适合作为回归验收项 | 发布前应验证，失败需修复或说明例外 |
| `gap` | 目标明确但当前能力不足或不完整 | 作为近期迭代、方案设计和验收标准 |
| `future` | 属于长期愿景或成熟度演进 | 作为路线图牵引，不阻断当前版本 |

## 覆盖矩阵

| 目标愿景使命章节 | 核心要求 | 覆盖用例 |
| --- | --- | --- |
| 系统定位 | AgentGov 是通用智能体治理平台，不是单点助手或单行业系统 | AGV-001, AGV-002, AGV-003 |
| 使命 | 让智能体运行、反馈、经验、方法和版本演进沉淀为优化闭环治理体系 | AGV-004, AGV-005, AGV-006, AGV-007 |
| 愿景 | 成为智能体应用的优化闭环治理基础设施 | AGV-008, AGV-009, AGV-010 |
| 三层资产模型 | 沉淀数据资产、方法论资产和执行资产 | AGV-011, AGV-012, AGV-013 |
| 三大核心能力 | Runtime、Feedback Loop、Version Governance 构成治理链路 | AGV-014, AGV-015, AGV-016 |
| 多 Agent 治理对象 | 区分业务 Agent 和治理 Agent，支持多业务 Agent 治理 | AGV-017, AGV-018, AGV-019 |
| Agent 生命周期 | 支持 draft、active、evaluating、deprecated、archived 等治理状态 | AGV-020, AGV-021 |
| Agent 资产 Registry | 建立 Agent、prompt、skill、SOP、eval、release 等资产关联 | AGV-022, AGV-023 |
| 反馈路由与归属 | 每条反馈可归属到 Agent、version、run、session、task 和场景 | AGV-024, AGV-025 |
| 能力域与场景包 | 按能力域组织可迁移、可复制、可审计的能力包 | AGV-026, AGV-027 |
| 反馈到资产闭环 | 从运行、反馈、归因、优化、评估到版本和 Registry 闭环 | AGV-028, AGV-029, AGV-030 |
| 核心目标 | 创建 Agent、运行可复盘、反馈归因、优化资产化、评估门禁、版本固化、API 集成 | AGV-031 至 AGV-040 |
| 治理边界与审批 | 高风险变更需人工或外部系统确认，不绕过责任边界 | AGV-041, AGV-042 |
| 治理成熟度路径 | main agent 样板、多业务 Agent、场景包、跨 Agent 方法论沉淀 | AGV-043, AGV-044, AGV-045 |
| 典型落地场景 | 安全运营只是典型场景之一，平台不绑定单一行业 | AGV-046 |
| 产品边界 | AgentGov 负责治理能力，外部系统负责业务界面、权限、生产系统、高风险动作责任和通用协作流转 | AGV-047, AGV-048, AGV-049 |

## 核心功能测试套件

### AGV-001 通用治理平台定位不被单行业绑定

状态：`current`

目标来源：系统定位、典型落地场景。

前置条件：存在 AgentGov 产品介绍、README 或目标愿景使命文档。

测试步骤：

1. 阅读项目首屏文档、产品定位文档和前端标题。
2. 检查是否以“智能体治理平台 AgentGov”为主口径。
3. 检查安全运营是否只作为典型场景出现。

成功标准：

- 文档主定位不再是“网络安全运营智能体底座”。
- 安全运营、客服、研发助手、知识管理、企业流程自动化等可作为场景出现，但不定义平台唯一边界。
- “开发平台”如果出现，只能作为子能力或工程形态，不应替代“治理平台”主定位。

证据要求：文档截图、文本检索结果或 PR diff。

自动验收：`tests/test_agv_acceptance.py::test_agv_001_governance_platform_positioning`。

### AGV-002 Agent Runtime、Feedback Loop、Version Governance 形成治理链路

状态：`current`

目标来源：三大核心能力。

前置条件：系统存在 Runtime API、反馈闭环和版本治理相关能力说明。

测试步骤：

1. 检查文档是否把 Runtime、Feedback Loop、Version Governance 解释为治理链路。
2. 发起一次 Agent 运行或检查已有 run 记录。
3. 从该运行关联到反馈、归因、优化、评估或版本治理对象。

成功标准：

- Runtime 被定义为事实产生层。
- Feedback Loop 被定义为经验转化层。
- Version Governance 被定义为治理固化层。
- 三者不是孤立功能清单，而能形成从事实到改进再到版本固化的链路。

证据要求：run、feedback、eval 或 release 的关联记录。

自动验收：`tests/test_feedback_batch_closed_loop.py::test_fob_da60_optimization_closed_loop_runs_regression_after_promotion`。

### AGV-003 当前前端边界不越界为最终业务门户

状态：`current`

目标来源：系统定位、产品边界。

前置条件：前端可访问或存在前端文档。

测试步骤：

1. 打开前端或阅读 README 的前端说明。
2. 检查前端是否定位为开发调试与治理观察界面。
3. 检查是否没有把外部业务门户、权限审批、生产系统操作职责放入 AgentGov 前端。

成功标准：

- 前端支持 Playground、反馈工作台、评估和版本视图等治理观察能力。
- 文档明确外部业务系统承载最终用户业务界面和生产流程。
- 文档明确 AgentGov 不提供通用协作看板，也不替代 Multica、Jira、GitHub Issues 等协作平台。
- 高风险业务动作不由 AgentGov 前端绕过外部系统审批。

证据要求：README、前端页面或用户流程截图。

自动验收：`tests/test_agv_acceptance.py::test_agv_003_048_frontend_is_debug_observation_boundary`。

### AGV-004 Agent 创建与配置能力

状态：`current`

目标来源：使命、核心目标、多 Agent 治理对象。

前置条件：平台允许定义或导入业务 Agent。

测试步骤：

1. 创建一个新的业务 Agent，填写名称、用途、适用场景和责任边界。
2. 配置 system prompt、skills、tools、MCP、profile、模型参数和运行约束。
3. 保存后查询该 Agent 的定义和配置。

成功标准：

- 新 Agent 具有稳定身份和配置。
- Agent 定义能作为后续运行、反馈、评估和版本治理的归属对象。
- 配置不泄露 API key、MCP header 或本机私有路径。

证据要求：Agent 定义记录、配置摘要和安全脱敏检查结果。

自动验收：`tests/test_agent_registry_store.py::test_create_business_agent_endpoint_registers_and_lists`（POST 创建得稳定身份并进注册表归属对象集合）、`tests/test_agent_registry_store.py::test_business_agent_workspace_scaffolds_safe_config_container`（创建即得完整可编辑配置面 CLAUDE.md/system prompt + .claude/settings.json/skills·tools + .mcp.json/MCP，且不泄露 API key/MCP header/本机私有路径）、`tests/test_agent_registry_store.py::test_initialize_business_agent_workspace_is_idempotent_and_preserves_edits`（配置幂等保留用户编辑）。边界说明：配置面采用与 main agent 一致的 SDK 原生文件（运行业务 Agent 时 cwd=workspace 真实加载）；模型参数用平台默认模型（离线不变量提供本地化模型），暂不做 per-agent 模型凭据配置以免引入凭据泄露面。

### AGV-005 业务 Agent 与治理 Agent 边界清晰

状态：`current`

目标来源：多 Agent 治理对象。

前置条件：系统存在业务 Agent 与 attribution、proposal、execution、eval-case、regression-impact 等治理 Agent。

测试步骤：

1. 创建或选择一个业务 Agent。
2. 针对该业务 Agent 发起反馈优化流程。
3. 检查归因分析、方案生成、执行优化、用例治理、回归影响分析是否由治理 Agent 执行。

成功标准：

- 业务 Agent 是被治理对象。
- 治理 Agent 是闭环流程的执行者。
- 受治理业务 Agent 可以作为外部协作成员参与任务流转，治理 Agent 默认不作为协作成员暴露。
- 治理 Agent 的输出不直接变成生产事实，必须经过后端校验、评估和版本治理。

证据要求：agent job 的 profile、输入输出和最终投影记录。

自动验收：`tests/test_agent_profiles_category.py`（业务/治理身份与权限边界）；治理 Agent 输出经后端投影校验由 `tests/test_agent_job_store.py::test_agent_job_worker_logs_claim_and_runtime_failure` 覆盖。

### AGV-006 治理闭环产物覆盖数据、方法论和执行资产

状态：`current`

目标来源：使命、三层资产模型。

前置条件：存在一次完整或模拟的反馈优化流程。

测试步骤：

1. 从一次反馈中生成归因结果。
2. 生成优化方案、SOP 或执行计划。
3. 生成或关联 prompt、skill、playbook、eval case 或 governance rule。
4. 检查三类资产是否被记录并可追溯。

成功标准：

- 数据资产记录发生了什么。
- 方法论资产记录如何分析、如何优化、如何评估。
- 执行资产记录可以被 Agent 或系统复用的改进单元。

证据要求：反馈 case、归因输出、优化方案、执行资产记录。

自动验收：`tests/test_feedback_batch_closed_loop.py::test_fob_da60_optimization_closed_loop_runs_regression_after_promotion`（一次闭环同时产出并可追溯三类资产：数据资产=feedback/eval/run 记录，方法论资产=归因/优化/评估方法契约被应用，执行资产=eval case/change set）、`tests/test_methodology_assets.py`（方法论资产层为命名+版本化+结构化契约）。三层映射：数据资产由 SQLite 记录承载、方法论资产由 `AGENT_JOB_SPECS` 治理方法的结构化 Pydantic 契约+受版本治理的治理 Agent profile 承载、执行资产由 eval case/change set/workspace 配置容器承载。

### AGV-007 外部业务系统可通过 API 接入治理能力

状态：`current`

目标来源：使命、核心目标、产品边界。

前置条件：API 服务运行，配置有效 API key。

测试步骤：

1. 使用 API 发起 Agent 运行或查询运行记录。
2. 通过 API 提交反馈信号或查询反馈 case。
3. 查询 agent job、eval run 或 version governance 相关资源。

成功标准：

- 外部系统不需要使用 AgentGov 前端即可接入运行、反馈、评估或版本治理能力。
- API 响应包含可继续流转的 ID、状态和错误信息。
- 未授权请求返回 401 或等价鉴权错误。

证据要求：curl 记录、API 响应或 OpenAPI 路径检查。

自动验收（部分）：`tests/test_api_error_handlers.py::test_api_key_authentication_returns_structured_401`。返回体可续流转的完整性部分需运行态验收。

### AGV-008 真实业务运行轨迹可持续沉淀

状态：`current`

目标来源：愿景、数据资产。

前置条件：已发起至少一次 Agent 运行。

测试步骤：

1. 发起 Agent 运行。
2. 查询 run、session、trace、tool 调用、agent_activity 或等价运行详情。
3. 验证运行信息可用于后续反馈、归因和评估。

成功标准：

- 每次运行至少包含 run ID、session ID、输入、输出、状态和错误信息。
- 工具调用、skill 使用或 trace 信息可被审计。
- 运行记录可作为反馈闭环的事实基础。

证据要求：运行记录 API 响应、trace 链接或 SQLite 投影。

自动验收：`tests/test_feedback_store_cases_and_jobs.py::test_case_evidence_and_job_outputs`。

### AGV-009 失败案例可转化为组织级治理知识

状态：`current`

目标来源：愿景、方法论资产。

前置条件：存在失败反馈或回归失败案例。

测试步骤：

1. 针对失败案例收集证据。
2. 完成归因分析并输出问题类型、证据链、影响范围和改进方向。
3. 将改进方向沉淀为 SOP、prompt、skill、eval case 或治理规则。

成功标准：

- 失败不是只保留为错误日志。
- 失败能沉淀为可复用的治理知识。
- 后续同类问题可以被评估用例或治理规则捕获。

证据要求：失败 case、归因输出、沉淀资产和后续回归结果。

自动验收：`tests/test_feedback_batch_closed_loop.py::test_fob_da60_candidate_eval_cases_require_promotion_before_regression`（失败反馈沉淀为 eval case/回归资产，捕获同类问题）。

### AGV-010 跨 Agent 共享已验证的治理经验

状态：`future`

目标来源：愿景、治理成熟度路径。

前置条件：平台存在多个业务 Agent 和已验证方法论资产。

测试步骤：

1. 选择一个已经验证有效的 SOP、prompt 模板、skill 或评估用例。
2. 将其应用到另一个能力域相近的业务 Agent。
3. 运行评估，验证迁移后效果。

成功标准：

- 治理经验可以跨 Agent 复用。
- 复用过程记录来源、适用范围、风险和验证结果。
- 不同 Agent 共享方法论资产时仍保留各自版本和审计边界。

证据要求：资产复用记录、目标 Agent change set、评估结果。

### AGV-011 数据资产完整记录事实基础

状态：`current`

目标来源：三层资产模型。

前置条件：存在运行、反馈、评估或版本治理数据。

测试步骤：

1. 查询运行记录、反馈信号、证据包、eval run 和 release 记录。
2. 检查每类记录是否包含 ID、时间、状态、关联对象和错误信息。
3. 检查数据是否可被后续环节引用。

成功标准：

- run、trace、feedback、evidence、eval result、release event 不以孤立日志存在。
- 关键记录之间有可追溯关联。
- 错误信息不会只留在后端日志。

证据要求：API 响应、数据库投影或 UI 详情页。

自动验收：`tests/test_feedback_store_cases_and_jobs.py::test_case_evidence_and_job_outputs`。

### AGV-012 方法论资产可被治理流程复用

状态：`current`

目标来源：三层资产模型。

前置条件：存在归因方法、优化 SOP、评估规程或发布准入规则。

测试步骤：

1. 选择一个反馈问题。
2. 应用既有归因方法或优化 SOP。
3. 检查产出的归因、优化和验证结论是否引用该方法论资产。

成功标准：

- 方法论不是散落在自然语言中的一次性建议。
- 方法论资产有名称、适用范围、版本或修订记录。
- 同类问题可以复用同一方法论。

证据要求：方法论资产记录、引用关系和复用结果。

自动验收：`tests/test_methodology_assets.py::test_methodology_registry_is_single_source_named_and_structured`（每个治理方法有命名 profile + 独立结构化 Pydantic 契约，单一来源复用、非散落 NL）、`tests/test_methodology_assets.py::test_methodology_assets_are_carried_by_version_governed_governance_agents`（方法论由受版本治理的治理 Agent 承载，提供版本/修订记录）。复用证据：`AGENT_JOB_SPECS` 单一来源被每个反馈 case 的同类方法应用，非每次重新发明（见闭环回归 `test_fob_da60_optimization_closed_loop_runs_regression_after_promotion`）。

### AGV-013 执行资产可被 Agent 或系统调用

状态：`current`

目标来源：三层资产模型。

前置条件：存在 prompt、skill、profile、playbook、eval case 或 workflow。

测试步骤：

1. 选择一个执行资产。
2. 让业务 Agent 或治理 Agent 在受控流程中使用它。
3. 检查调用过程、输出结果和版本关联。

成功标准：

- 执行资产不是只存在于文档中。
- 调用过程可追踪、可评估、可回滚。
- 修改执行资产会进入版本治理或审计记录。

证据要求：调用记录、资产版本和评估结果。

自动验收：`tests/test_feedback_batch_closed_loop.py::test_fob_da60_optimization_closed_loop_runs_regression_after_promotion`（eval case 执行资产经晋级后被 regression-run 在受控流程调用，impact-analysis 评估其结果）、`tests/test_api_execution_optimizer.py::test_apply_execution_job_endpoint_writes_file_and_creates_versions`（修改执行资产/workspace 配置创建 Agent 版本，进版本治理）、`tests/test_agent_governance_publish.py::test_restore_release_switches_current_workspace_without_mutating_release_history`（执行资产版本可回滚且不污染历史）。

### AGV-014 Runtime 运行可复盘

状态：`current`

目标来源：Agent Runtime、核心目标。

前置条件：API 服务运行。

测试步骤：

1. 发起一次聊天或任务执行。
2. 查看 run 详情、回复细节、SDK 事件、tool 调用和错误详情。
3. 确认这些信息能支撑用户或开发者复盘。

成功标准：

- 运行记录能解释输入、输出、工具、错误和状态。
- 调试前端能展示关键运行细节。
- API 能返回足够信息用于外部系统记录。

证据要求：UI 详情截图或 API JSON。

自动验收：`tests/test_feedback_store_cases_and_jobs.py::test_case_evidence_and_job_outputs`。

### AGV-015 Feedback Loop 形成标准化归因链路

状态：`current`

目标来源：Feedback Loop、核心目标。

前置条件：存在 feedback signal 和 evidence package。

测试步骤：

1. 创建或选择一条反馈信号。
2. 关联证据包并发起归因 job。
3. 查询 job 输出和反馈 case 状态。

成功标准：

- 反馈不是只记录好评或差评。
- 归因输出包含问题类型、证据引用、置信度、责任边界和下一步。
- 证据不足时输出需要人工复核，而不是编造结论。

证据要求：feedback case、evidence package、attribution job 输出。

自动验收（部分）：`tests/test_feedback_store_cases_and_jobs.py::test_case_evidence_and_job_outputs`。证据不足需人工复核的提示部分需运行态/人工验收。

### AGV-016 Version Governance 固化能力演进

状态：`current`

目标来源：Version Governance、核心目标。

前置条件：存在可发布或可回滚的 Agent 行为包变更。

测试步骤：

1. 创建或查询 Agent change set。
2. 查看 diff、事件、回归运行和发布状态。
3. 发布或查询 release，验证 restore / rollback 边界。

成功标准：

- 候选变更可 diff。
- 发布前能关联回归结果或门禁状态。
- 发布、恢复、回滚都有审计事件和版本记录。

证据要求：change set、release、rollback API 响应。

自动验收：`tests/test_agent_governance_publish.py::test_candidate_committed_change_set_can_publish_directly`、`tests/test_agent_version_store.py::test_agent_version_file_diff_returns_unified_diff`。

### AGV-017 多业务 Agent 接入统一治理闭环

状态：`current`

目标来源：多 Agent 治理对象、核心目标。

前置条件：平台支持两个或以上业务 Agent。

测试步骤：

1. 创建两个业务 Agent，分别配置不同场景和工具边界。
2. 对每个 Agent 发起运行和反馈。
3. 对每个 Agent 生成独立归因、优化、评估和版本记录。

成功标准：

- 每个业务 Agent 的反馈和版本治理互不混淆。
- 治理 Agent 可以服务不同业务 Agent。
- 查询时能按 Agent 维度过滤 run、feedback、eval、release。

证据要求：两个 Agent 的独立闭环记录。

自动验收：`tests/test_agent_governance_publish.py::test_governance_serves_multiple_business_agents_with_isolated_closed_loops`（两个业务 Agent 各建 run→反馈→优化批次→评估→change set→release 独立闭环：①各维度按 Agent 过滤只见自身记录、版本链落各自独立 store 物理隔离=互不混淆；②单一 `AgentGovernanceService` 为两个 Agent 各自管理版本 store=治理 Agent 服务不同业务 Agent；③run/feedback/eval/change set/release 均可按 Agent 维度过滤）。归属沿 run.agent_id→signal→case→batch→task→change set→eval 全链路传播（B2 + B3.1~B3.4），优化执行流水线已 per-agent 参数化（B3.4-exec）。

### AGV-018 main agent 作为样板而非长期边界

状态：`current`

目标来源：多 Agent 治理对象、成熟度路径。

前置条件：当前系统围绕 main agent 有闭环能力。

测试步骤：

1. 检查文档和 UI 是否把 main agent 描述为默认治理对象或样板。
2. 检查是否存在把 main agent 写成平台唯一治理对象的表述。
3. 检查是否保留向多业务 Agent 扩展的目标。

成功标准：

- main agent 是第一阶段样板。
- 文档不把 `/main-workspace` 作为长期唯一产品边界。
- 目标测试用例持续保留多业务 Agent 扩展要求。

证据要求：文档检索结果。

自动验收：`tests/test_agv_acceptance.py::test_agv_018_main_agent_is_sample_not_long_term_boundary`。

### AGV-019 治理 Agent 只输出建议和治理产物

状态：`current`

目标来源：多 Agent 治理对象、治理边界。

前置条件：治理 Agent profile 可运行。

测试步骤：

1. 触发归因、方案、执行优化、用例治理或回归影响分析 job。
2. 检查治理 Agent 是否只读取允许输入。
3. 检查输出是否由后端校验和投影，不直接修改生产事实。

成功标准：

- 治理 Agent 不直接修改主 Agent 配置或生产系统。
- 输出通过 formatter、Pydantic 校验、store 投影和 API 展示。
- 失败会写入 error_json 并投影到用户可见状态。

证据要求：job 输入、validated output、error_json 或投影记录。

自动验收（部分）：`tests/test_agent_job_store.py::test_agent_job_worker_logs_claim_and_runtime_failure`。权限边界部分由运行时测试与人工验收覆盖。

### AGV-020 业务 Agent 生命周期状态可治理

状态：`current`

目标来源：Agent 生命周期。

前置条件：平台支持业务 Agent 生命周期状态。

测试步骤：

1. 创建 draft Agent。
2. 激活为 active。
3. 提交候选变更进入 evaluating。
4. 将旧版本 deprecated 或 archived。

成功标准：

- 生命周期状态有明确合法转移。
- 非法转移会被拒绝并返回可理解错误。
- archived Agent 仍可审计，但不参与新运行选择。

证据要求：状态转移记录和非法转移测试。

自动验收（按三条成功标准）：①明确合法转移——`agent_lifecycle` 状态机（draft→active→evaluating→deprecated→archived，archived 终态）+ API `POST /api/agent-registry/{id}/lifecycle`；②非法转移被拒并返回可理解错误——`tests/test_agent_registry_store.py::test_business_agent_lifecycle_transitions_and_archived_excluded_from_run`（archived→active 被 StateTransitionError 拒绝，409 带转移说明）；③archived 仍可审计但不参与新运行——同测试断言 archived Agent 仍在注册表可查、但 `/api/chat?agent_id=` 拒绝运行（`AGENT_RUNNABLE_LIFECYCLE_STATES`）。状态列由迁移 0009 持久化；main-agent 样板生命周期固定。

### AGV-021 Agent 生命周期围绕版本治理运转

状态：`current`

目标来源：Agent 生命周期、Version Governance。

前置条件：业务 Agent 支持多版本。

测试步骤：

1. 对 active Agent 生成候选变更。
2. 执行评估和回归。
3. 发布新版本并归档旧版本。
4. 触发 rollback 或 restore。

成功标准：

- 候选版本、已发布版本和回滚版本可区分。
- 历史版本可追溯，不被物理删除破坏历史解释。
- rollback 不删除历史 release。

证据要求：version graph、release archive、rollback event。

自动验收：`tests/test_agent_governance_publish.py::test_business_agent_version_lifecycle_preserves_history_through_rollback`（业务 Agent 经候选→发布 v1/v2→restore→rollback：候选/已发布/回滚版本状态可区分，restore 切换当前版本不改写 release 历史，rollback 仅标记 `rolled_back` 而不物理删除 release，两条 release 在 Agent 维度仍可追溯）。per-agent 版本链由 `AgentGovernanceService._store_for(agent_id)` 提供物理隔离（业务 Agent 版本 store 根 `data_dir/business-agents/{agent_id}/version`），与 main agent 版本链互不混淆。

### AGV-022 Agent 资产 Registry 记录资产关系

状态：`current`

目标来源：Agent 资产 Registry。

前置条件：存在 Agent 定义、prompt、skill、eval case、change set 和 release。

测试步骤：

1. 查询某个 Agent 的资产 Registry。
2. 查看该 Agent 关联的 prompt、skill、SOP、eval case、release。
3. 从某次 feedback 追溯到优化任务和最终版本。

成功标准：

- Registry 不是简单清单，而能表达资产关系。
- 能回答“某次反馈影响了哪个 Agent、改了哪些资产、进入哪个版本”。
- Registry 记录可被 API 或 UI 查询。

证据要求：Registry 查询结果或资产关系图。

自动验收：`tests/test_agent_registry_store.py::test_feedback_asset_provenance_traces_agent_and_relationship`（从反馈追溯归属 Agent 与资产关系结构）。API `GET /api/asset-registry/feedback/{feedback_case_id}` 返回资产关系链而非简单清单：`agent_ids`（反馈影响了哪个 Agent）、`optimization_tasks[].target_paths`（改了哪些资产）、`applied_agent_version_id`（进入哪个版本）、`eval_case_ids`/`latest_change_set_id`（关联评估与变更集）。关系由既有 provenance link（feedback_case→signal.agent_id、`list_tasks(feedback_case_id)`→optimization_task）聚合，免迁移。

### AGV-023 Registry 防止资产散落和重复沉淀

状态：`current`

目标来源：Agent 资产 Registry、能力域与场景包。

前置条件：存在多个相似 prompt、skill、eval case 或 SOP。

测试步骤：

1. 导入或创建多个相似治理资产。
2. 运行去重、合并或关联检查。
3. 查看 Registry 中的主资产、替代关系和引用方。

成功标准：

- 重复资产有检测和治理建议。
- 合并或废弃资产可审计。
- 引用关系不会因资产合并而丢失。

证据要求：资产治理事件和引用关系。

自动验收（按三条成功标准）：①重复资产检测+治理建议——`tests/test_scenario_pack_store.py::test_scenario_pack_dedup_detect_and_merge`（`GET /api/scenario-packs/duplicates` 按规范化名检测重复并建议主资产）；②合并/废弃可审计——`POST /api/scenario-packs/{primary}/merge` 把重复并入主资产、重复包标记 `merged_into`/`merged_at` 保留不物理删除（可查=可审计）；③引用不丢失——合并并入各包关联（agent_ids/eval_case_ids/asset_refs 去重并集），重复包经 `merged_into` 重定向到主资产。

### AGV-024 反馈可归属到 Agent、version、run 和场景

状态：`current`

目标来源：反馈路由与归属。

前置条件：存在至少一条反馈信号。

测试步骤：

1. 提交反馈时携带或解析 Agent、version、run、session、task、场景信息。
2. 查询反馈详情。
3. 验证归因和优化任务继承该归属关系。

成功标准：

- 反馈不会进入无归属全局问题池。
- 归因、优化、评估和版本治理都能基于同一归属链路。
- 无法归属的反馈会被标记为待人工处理或需要补充上下文。

证据要求：feedback signal、feedback case 和 optimization task 关联字段。

自动验收（按三条成功标准）：① 不进无归属全局池——`tests/test_feedback_store_sources.py::test_create_signal_records_business_agent_attribution`（信号恒有 agent_id 归属）；② 同一归属链路——`tests/test_feedback_store_sources.py::test_create_signal_attributes_to_run_business_agent`（反馈据匹配 run 的 agent_id 归属到产生它的业务 Agent，归因/优化经 case→signal 继承）；③ 无法归属→人工/补充上下文——`tests/test_feedback_store_sources.py::test_feedback_signal_requires_source_locator`（无锚点反馈被拒、需补充上下文）、`::test_implicit_signal_defaults_to_review`（隐式反馈标记 requires_review）。归属维度：Agent(`agent_id`)、version(`agent_version_id`)、run(`run_id`)、session(`session_id`) 均为一等字段；场景经 signal `metadata` 携带（成功标准不要求专用场景字段，场景包组织为 future AGV-026/027）。

### AGV-025 反馈路由错误不会污染其他 Agent

状态：`current`

目标来源：反馈路由与归属、多业务 Agent 治理。

前置条件：存在多个业务 Agent。

测试步骤：

1. 对 Agent A 提交反馈。
2. 尝试把该反馈错误关联到 Agent B 的优化批次。
3. 查看系统是否拒绝或要求人工确认。

成功标准：

- 跨 Agent 反馈误路由被阻止或显式审计。
- Agent B 的评估和版本不受 Agent A 反馈污染。
- 管理员可以修正反馈归属并保留审计记录。

证据要求：错误响应、审计事件和修正记录。

自动验收（按三条成功标准）：① 跨 Agent 误路由被阻止——`tests/test_feedback_store_sources.py::test_create_optimization_batch_rejects_cross_agent_misroute`（优化批次混入跨 Agent 反馈被显式拒绝）；② Agent 评估/版本不受他 Agent 反馈污染——同上误路由防护 + `::test_list_signals_filters_by_agent_dimension`（反馈按 Agent 维度隔离），使每个 Agent 的批次/评估只含自身反馈；③ 管理员可修正反馈归属并保留审计——`::test_reassign_signal_agent_corrects_attribution_with_audit`（reassign 改写 agent_id 并在 metadata.attribution_corrections 保留 from/to/operator/reason 审计）。API：POST `/api/feedback-signals/{signal_id}/reassign-agent`。

### AGV-026 能力域或场景包可组织治理资产

状态：`current`

目标来源：能力域与场景包。

前置条件：平台支持能力域或场景包。

测试步骤：

1. 创建一个场景包，例如“告警研判”或“客服投诉处理”。
2. 关联 Agent 定义、prompt、skill、SOP、eval case、发布准入规则。
3. 查询场景包详情。

成功标准：

- 场景包表达业务目标、适用范围和风险等级。
- 场景包中的资产可迁移、可复制、可审计。
- Agent 可按场景包装配能力。

证据要求：场景包定义和资产关联。

自动验收（按三条成功标准）：①表达业务目标/适用范围/风险等级——`tests/test_scenario_pack_store.py::test_create_and_query_scenario_pack`、`::test_scenario_pack_api_create_list_get`（ScenarioPack 实体 business_goal/scope/risk_level，迁移 0010 持久化）；②资产可迁移/复制/审计——`::test_scenario_pack_associate_and_copy`（copy 生成模板复制资产引用，关联记录可查=可审计）；③Agent 可按场景包装配能力——同测试 associate agent_ids 即 Agent 装配该包。API：`POST/GET /api/scenario-packs`、`POST /{id}/assets`（装配）、`POST /{id}/copy`（复制/迁移）。

### AGV-027 场景包支持跨 Agent 复用

状态：`future`

目标来源：能力域与场景包、治理成熟度路径。

前置条件：存在两个相似业务场景的 Agent。

测试步骤：

1. 将一个场景包应用到 Agent A。
2. 基于同一场景包创建或配置 Agent B。
3. 比较两个 Agent 的评估结果和差异配置。

成功标准：

- 场景包可复用但不强制完全相同。
- 每个 Agent 保留自己的版本和审计边界。
- 复用后仍需评估通过才能进入 active。

证据要求：场景包应用记录和两个 Agent 的评估结果。

### AGV-028 反馈到资产闭环完整

状态：`current`

目标来源：反馈到资产的闭环链路。

前置条件：存在可执行反馈优化流程。

测试步骤：

1. 发起业务 Agent 运行并提交反馈。
2. 反馈归属到 Agent / version / 场景。
3. 生成证据、归因、优化方案和 eval case。
4. 创建候选 change set 并运行回归。
5. 发布或回滚，并更新 Registry。

成功标准：

- 链路中每一步都有持久化记录或可审计事件。
- 任一步失败会投影为明确状态和错误详情。
- 成功后能从 release 反查反馈、归因、优化和评估证据。

证据要求：端到端链路记录。

自动验收（按三条成功标准）：① 每步持久化/可审计——`tests/test_feedback_batch_closed_loop.py::test_fob_da60_optimization_closed_loop_runs_regression_after_promotion`（反馈→eval case 生成→归因→优化方案→执行→change set→回归→发布每步有记录与状态）；② 任一步失败投影状态+错误——`tests/test_feedback_batch_closed_loop.py::test_fob_da60_failed_batch_regression_blocks_publish`（回归失败阻断发布）、AGV-029 的 error_json 回归；③ release 反查证据——同闭环测试断言发布的优化任务携带 `feedback_case_ids`+`eval_case_ids`，构成 release→change set→优化→反馈/评估的 provenance 反查链路。前置「业务 Agent 运行+反馈归属」由 AGV-004 运行时与 AGV-024 归属链路提供。

### AGV-029 闭环失败可恢复

状态：`current`

目标来源：反馈到资产闭环、治理边界。

前置条件：可模拟治理 Agent 失败、评估失败或发布失败。

测试步骤：

1. 在归因、方案、评估或发布任一环节注入失败。
2. 查询用户可见状态和后端错误详情。
3. 执行重试、人工复核、放弃或回滚。

成功标准：

- 失败不会只停留在日志。
- 用户或外部系统能看到下一步可执行动作。
- 重试或回滚不会生成重复不一致资产。

证据要求：error_json、状态机事件、补偿或回滚记录。

自动验收：`tests/test_agent_job_store.py::test_agent_job_worker_logs_claim_and_runtime_failure`（失败写 error_json，不止日志）、`tests/test_agent_governance_publish.py::test_batch_regression_failed_cases_block_publish`（回归失败投影为 blocked 状态+下一步阻断）、`tests/test_agent_governance_publish.py::test_restore_release_switches_current_workspace_without_mutating_release_history`（回滚不改历史、无重复不一致资产）。

### AGV-030 闭环结果能防止历史问题复发

状态：`current`

目标来源：反馈到资产闭环、评估门禁。

前置条件：存在由反馈生成或晋级的回归用例。

测试步骤：

1. 从历史反馈生成 eval case。
2. 将用例加入批次或长期回归资产。
3. 在后续优化或发布前运行回归。

成功标准：

- 高价值历史反馈能转化为回归资产。
- 回归失败能解释是能力退化、用例过时、flaky 还是历史问题复发。
- 历史高危问题复发能阻断发布或要求人工确认。

证据要求：eval case、eval run、regression impact analysis。

自动验收：`tests/test_feedback_batch_closed_loop.py::test_fob_da60_failed_batch_regression_blocks_publish`、`tests/test_feedback_batch_closed_loop.py::test_fob_da60_candidate_eval_cases_require_promotion_before_regression`。

### AGV-031 Agent 创建、配置、运行、治理统一入口

状态：`current`

目标来源：核心目标 1。

前置条件：平台提供 Agent 管理 API 或 UI。

测试步骤：

1. 创建业务 Agent。
2. 配置行为边界和工具。
3. 发起运行。
4. 针对运行提交反馈并进入治理闭环。

成功标准：

- 用户不需要在多个不相关入口之间手工拼接治理对象。
- Agent ID 在运行、反馈、评估、版本中保持一致。
- 删除或归档 Agent 前有影响面提示。

证据要求：Agent 管理 UI/API 记录。

自动验收：`tests/test_agent_registry_store.py::test_delete_business_agent_reports_impact_and_protects_main`（统一入口 `POST/DELETE /api/agent-registry` 创建治理对象并删除，agent_id 在 run/feedback/eval/version 一致，删除前给出跨维度影响面提示 `runs/feedback_signals/optimization_tasks/eval_runs/change_sets/releases`，main-agent 样板不可删 400、未知 404）。配套统一入口：创建配置面 `test_business_agent_workspace_scaffolds_safe_config_container`（AGV-004 配置容器）、运行 `test_chat_routes_to_registered_business_agent`（`/api/chat?agent_id=` 路由）、生命周期归档 `test_business_agent_lifecycle_transitions_and_archived_excluded_from_run`（AGV-020 archived 拒新运行）。agent_id 全链路一致由 B2+B3.1~B3.4 归属贯通背书，无需在不相关入口间手工拼接治理对象。

### AGV-032 运行记录支持事实、推断和建议分离

状态：`current`

目标来源：核心目标 2、方法论资产。

前置条件：业务 Agent 输出包含分析或建议。

测试步骤：

1. 运行一个包含事实、推断和建议的任务。
2. 查看输出结构或详情。
3. 检查反馈和归因是否能引用事实、推断和建议。

成功标准：

- 输出不把推断伪装成事实。
- 反馈可指向具体事实错误、推断问题或建议问题。
- 归因能区分数据缺口、推理问题、工具问题和执行资产问题。

证据要求：输出详情和归因结果。

自动验收：`tests/test_feedback_output_normalizers.py::test_attribution_distinguishes_reasoning_error_from_data_and_tool_problems`（归因把推理问题 `reasoning_error` 独立于数据缺口/工具/执行资产分类）、`tests/test_feedback_output_normalizers.py::test_attribution_formatter_output_drops_backend_owned_fields_on_reasoning_error`（字段所有权边界）。字段层面：归因输出 `evidence_refs`（事实）与 `rationale`（推断）分离、`recommended_next_step`（建议）独立，结构上不把推断伪装成事实；反馈侧 `SocEventType` 以 `evidence.*`/`case.verdict_changed`（事实/推断）与 `recommendation.*`（建议）定向具体问题。本轮新增 `reasoning_error` 类目补齐"数据/推理/工具/执行资产"四分，并经 live DeepSeek 实测：数据与工具齐全但推断出错的场景被准确归为 `reasoning_error`。

### AGV-033 反馈进入问题分类和证据链

状态：`current`

目标来源：核心目标 3。

前置条件：存在反馈 case 和 evidence package。

测试步骤：

1. 提交质量反馈。
2. 创建或关联证据包。
3. 运行归因分析。

成功标准：

- 反馈可被分类为内容质量、工具数据、runtime、配置、SOP、评估缺口等问题。
- 归因引用证据而非凭空判断。
- 证据不足时要求人工复核。

证据要求：attribution output 和 evidence references。

自动验收（部分）：`tests/test_feedback_store_cases_and_jobs.py::test_soc_event_idempotency_and_pending_correlation`。标准化分类与人工复核提示部分需运行态/人工验收。

### AGV-034 优化形成可执行资产而非一次性建议

状态：`current`

目标来源：核心目标 4。

前置条件：存在归因结果和优化方案。

测试步骤：

1. 从归因结果生成优化方案。
2. 检查方案是否明确产物类型。
3. 将方案转化为 prompt、skill、SOP、playbook、eval case、governance rule 或 change set。

成功标准：

- 优化方案不只是自然语言建议。
- 每个优化任务有目标对象、预期效果、验证方式和风险。
- 可执行资产进入版本治理或 Registry。

证据要求：optimization task、change set 或资产记录。

自动验收：`tests/test_api_execution_optimizer.py::test_apply_execution_job_endpoint_writes_file_and_creates_versions`（优化方案落为文件写操作并创建 Agent 版本，可执行资产进入版本治理，非一次性 NL 建议）、`tests/test_feedback_batch_closed_loop.py::test_fob_da60_optimization_closed_loop_runs_regression_after_promotion`（优化方案进闭环并触发回归）。优化方案结构化字段（`target_summary`/`target_path` 目标对象、`acceptance_criteria` 预期效果与验证方式）由 `FeedbackOptimizationPlanFormatterOutput` 承载；任务级风险以 `confidence` + `human_review_required` 表达，执行级风险以 `ExecutionPlanFormatterOutput.risk` 承载。

### AGV-035 评估成为发布前质量闸门

状态：`current`

目标来源：核心目标 5。

前置条件：存在候选变更和 active eval case。

测试步骤：

1. 创建候选 change set。
2. 运行关联 eval case 或 regression plan。
3. 根据结果决定发布、修复、重跑或回滚。

成功标准：

- 未通过回归的高风险变更不能无提示发布。
- eval run item 能解释失败原因。
- gate override 必须有原因和审计记录。

证据要求：eval run、gate result、publish decision。

自动验收：`tests/test_agent_governance_publish.py::test_batch_regression_failed_cases_block_publish`、`tests/test_feedback_batch_closed_loop.py::test_fob_da60_failed_batch_regression_blocks_publish`。

### AGV-036 版本治理提供 diff、发布、恢复和回滚

状态：`current`

目标来源：核心目标 6。

前置条件：存在 Agent repository 和 change set。

测试步骤：

1. 查看 repository 状态。
2. 创建或查询 change set。
3. 查看 diff 和 file diff。
4. 发布 release 并测试 restore 或 rollback。

成功标准：

- diff 能解释候选行为包变化。
- release 有 tag、archive 或等价发布记录。
- restore/rollback 可审计，且不会删除历史 release。

证据要求：agent governance API 响应。

自动验收：`tests/test_agent_governance_publish.py::test_restore_release_switches_current_workspace_without_mutating_release_history`、`tests/test_agent_version_store.py::test_agent_version_file_diff_returns_unified_diff`。

### AGV-037 外部业务系统责任边界清晰

状态：`current`

目标来源：核心目标 7、产品边界。

前置条件：存在外部业务系统或模拟 webhook。

测试步骤：

1. 外部业务系统或协作平台通过 API 提交任务、上下文或反馈。
2. AgentGov 返回治理结果、建议或候选变更。
3. 高风险业务动作由外部系统确认，协作状态由外部协作平台流转。

成功标准：

- AgentGov 不接管外部系统的用户、角色、审批和生产动作责任。
- AgentGov 不接管外部协作平台的 issue、任务、看板、协作成员和状态流转。
- 外部系统能追踪 AgentGov 的运行和建议。
- 高风险动作不会由 Agent 自动绕过审批执行。

证据要求：API 调用、external governance item 或审批记录。

自动验收：`tests/test_agv_acceptance.py::test_agv_037_047_governance_scope_not_business_ownership`；高风险不绕过审批由 AGV-041 背书。

### AGV-038 API 错误和 job 失败可见

状态：`current`

目标来源：Runtime、Feedback Loop、产品边界。

前置条件：可触发错误请求或失败 job。

测试步骤：

1. 发起无效输入、无权限请求或模拟治理 job 失败。
2. 查询 API 响应、job 详情和 UI 状态。
3. 检查错误是否可用于下一步处理。

成功标准：

- 错误包含 error code 或明确 detail。
- job 失败写入 error_json。
- UI 不显示误导性空态。

证据要求：错误响应、job record、UI 失败态。

自动验收：`tests/test_api_error_handlers.py::test_feedback_store_error_handler_returns_structured_error`、`tests/test_agent_job_store.py::test_agent_job_worker_logs_claim_and_runtime_failure`。

### AGV-039 当前调试前端可观察核心治理链路

状态：`current`

目标来源：系统定位、产品边界。

前置条件：前端和后端可运行。

测试步骤：

1. 打开前端。
2. 查看 Playground、反馈工作台、回归资产、版本管理等入口。
3. 执行或查看一条反馈优化流程。

成功标准：

- 前端能观察运行、反馈、评估和版本状态。
- 关键失败态有错误详情或可追踪 job。
- 前端不要求用户必须在 AgentGov 内完成所有业务操作。

证据要求：浏览器截图、console error 记录、API request 结果。

自动验收（部分）：`tests/test_agv_acceptance.py::test_agv_003_048_frontend_is_debug_observation_boundary`。运行态状态观察与错误展示需前端/运行态验收。

### AGV-040 离线或内网部署不破坏必需闭环

状态：`current`

目标来源：产品不变量、使命。

前置条件：使用本地或内网模型网关配置。

测试步骤：

1. 启动 API、worker 和必要本地服务。
2. 发起运行、反馈、归因或评估 job。
3. 验证流程不依赖公网远程服务。

成功标准：

- 必需工作流可指向本地或内网模型网关。
- 缺少模型凭据时 job 明确失败，而不是生成 raw/offline 伪结果。
- 示例配置不泄露真实凭据。

证据要求：env 配置摘要、job 结果、健康检查。

自动验收（示例凭据不泄露）：`tests/test_repository_env_policy.py::test_official_env_examples_do_not_ship_configured_model_provider_key`。

### AGV-041 高风险动作需要审批

状态：`current`

目标来源：治理边界与审批。

前置条件：存在会影响 prompt、skill、tool、SOP 或生产策略的高风险候选变更。

测试步骤：

1. 生成高风险优化建议。
2. 尝试自动应用或发布。
3. 检查系统是否要求授权用户或外部业务系统确认。

成功标准：

- 高风险变更不会无审批发布。
- 审批记录包含操作人、原因、影响范围和回滚方案。
- 拒绝或放弃变更有审计事件。

证据要求：approval、reject、abandon 或 external confirmation 记录。

自动验收：`tests/test_agent_governance_publish.py::test_high_risk_change_set_requires_approval_before_publish`、`tests/test_agent_governance_publish.py::test_rejected_change_set_records_audit_event`。

### AGV-042 权限和敏感信息边界

状态：`current`

目标来源：治理边界、产品边界。

前置条件：存在 API key、MCP header、数据库凭据或敏感工具输入。

测试步骤：

1. 查询配置摘要、运行详情、trace、导出包或 UI 详情。
2. 检查是否出现真实密钥、header 或敏感路径。
3. 对受保护 API 使用错误凭据请求。

成功标准：

- 敏感信息不会出现在日志、trace、diagnostics、导出包或 UI 中。
- 错误凭据被拒绝。
- 脱敏字段仍能支撑诊断。

证据要求：脱敏检查结果和 401 响应。

自动验收（示例凭据不泄露）：`tests/test_repository_env_policy.py::test_official_env_examples_do_not_ship_configured_model_provider_key`；日志/trace 脱敏与错误凭据拒绝由运行时测试与人工验收覆盖。

### AGV-043 第一阶段 main agent 样板闭环

状态：`current`

目标来源：治理成熟度路径。

前置条件：main agent 及反馈闭环能力可用。

测试步骤：

1. 对 main agent 发起运行。
2. 提交反馈并进入归因、优化、评估和版本治理。
3. 查询完整链路。

成功标准：

- main agent 作为第一阶段样板闭环可运行。
- 样板闭环中产生的资产可用于后续抽象。
- 文档不把样板误写成长期边界。

证据要求：main agent 端到端闭环记录。

自动验收：`tests/test_feedback_batch_closed_loop.py::test_fob_da60_optimization_closed_loop_runs_regression_after_promotion`、`tests/test_feedback_batch_closed_loop.py::test_fob_da60_candidate_eval_cases_require_promotion_before_regression`。

### AGV-044 第二阶段多业务 Agent 扩展

状态：`future`

目标来源：治理成熟度路径。

前置条件：平台完成多业务 Agent 建模。

测试步骤：

1. 将 main agent 闭环范式抽象为通用治理模型。
2. 接入一个新的业务 Agent。
3. 重用运行、反馈、归因、优化、评估和版本治理能力。

成功标准：

- 多业务 Agent 不需要复制 main agent 的硬编码路径。
- 反馈、版本、评估都按 Agent 维度隔离。
- 新 Agent 接入有明确迁移和验收步骤。

证据要求：多 Agent API、配置和回归结果。

### AGV-045 第三与第四阶段场景包和跨 Agent 方法论沉淀

状态：`future`

目标来源：治理成熟度路径、能力域与场景包。

前置条件：已有多个业务 Agent 和场景包。

测试步骤：

1. 建立一个跨 Agent 可复用的方法论资产。
2. 绑定到一个场景包。
3. 在多个 Agent 中复用并评估。

成功标准：

- 方法论资产不是单 Agent 私有经验。
- 场景包能组织 prompt、skill、SOP、eval 和版本策略。
- 跨 Agent 复用后仍保留独立审计和评估结果。

证据要求：场景包、方法论资产和跨 Agent 评估报告。

### AGV-046 安全运营作为示例场景可被替换

状态：`current`

目标来源：典型落地场景。

前置条件：系统包含安全运营示例 Agent 或文档。

测试步骤：

1. 阅读安全运营相关 CLAUDE.md、README 或产品文档。
2. 检查是否表明安全运营是典型示例。
3. 验证目标文档同时保留其他场景。

成功标准：

- 安全运营场景可以继续作为示例业务 Agent。
- 平台核心概念不依赖 SOC、SIEM、SOAR 才成立。
- 替换为客服、研发助手或知识管理时，治理模型仍适用。

证据要求：文档和模板说明。

自动验收：`tests/test_agv_acceptance.py::test_agv_046_security_ops_is_replaceable_example_scenario`。

### AGV-047 AgentGov 职责边界不侵入外部业务系统

状态：`current`

目标来源：产品边界。

前置条件：存在外部系统集成场景。

测试步骤：

1. 外部系统提交业务对象和反馈。
2. AgentGov 返回治理结果。
3. 检查用户、角色、租户权限、审批和生产执行是否仍由外部系统负责。

成功标准：

- AgentGov 不复制外部业务系统的信息架构。
- AgentGov 不复制 Multica、Jira、GitHub Issues 等协作平台的看板、issue 生命周期和成员管理。
- 生产处置动作不由 AgentGov 自行承担最终责任。
- 外部系统可以审计 AgentGov 的建议和运行记录。

证据要求：集成流程图、API 记录或外部治理项。

自动验收：`tests/test_agv_acceptance.py::test_agv_037_047_governance_scope_not_business_ownership`；生产处置不由 AgentGov 承担、外部可审计运行与建议。

### AGV-048 开发调试前端不成为隐藏生产控制台

状态：`current`

目标来源：产品边界、治理边界。

前置条件：前端可访问。

测试步骤：

1. 检查前端是否提供 Terminal、敏感文件编辑或生产系统直接操作。
2. 检查高风险治理动作是否需要确认。
3. 检查前端输出是否脱敏。

成功标准：

- 前端不接管 Claude Code CLI 进程。
- 前端不编辑宿主机敏感文件。
- 聊天、反馈、评估和版本管理通过后端 Runtime API 完成。

证据要求：前端页面、代码检索或手工验证记录。

自动验收：`tests/test_agv_acceptance.py::test_agv_003_048_frontend_is_debug_observation_boundary`。

### AGV-049 外部协作平台对接晚于核心治理稳定

状态：`future`

目标来源：产品边界、治理成熟度路径。

前置条件：存在 AgentGov 产品路线说明和外部协作平台集成设想。

测试步骤：

1. 阅读目标愿景使命、执行计划和 README。
2. 检查 Multica、Jira、GitHub Issues 等外部协作平台是否只作为协作系统边界或长期生态集成方向出现。
3. 检查近期阶段是否仍聚焦 AgentGov 自身 Runtime、反馈闭环、归因优化、评估回归、版本治理、多业务 Agent 治理和资产 Registry。
4. 检查是否把深度外部协作平台对接排在至少三个产品大版本稳定之后。

成功标准：

- AgentGov 当前目标不包含通用协作看板、issue 同步、assignee 映射、squad 管理或 Multica adapter。
- 外部协作平台负责任务流转和状态协作，AgentGov 负责受治理业务 Agent 的运行、治理和版本化交付。
- 治理 Agent 不作为外部协作成员暴露。
- Multica 等外部协作平台深度对接被明确归入长期生态集成阶段，不阻断前三个产品大版本的核心治理能力打磨。

证据要求：目标愿景使命、执行计划、README 或后续集成设计中的阶段说明。

## 开发推进规则

### 新功能进入条件

任何新功能如果声称服务 AgentGov 目标愿景使命，必须在方案中声明：

- 对应的 AGV 用例编号。
- 影响的目标章节。
- 当前状态是增强 `current`、补齐 `gap`，还是推进 `future`。
- 验证方式：自动测试、API smoke、UI smoke、人工验收或文档审查。

### 状态升级规则

当某个 `gap` 或 `future` 用例进入可验收状态时，必须同步：

- 更新本文档状态。
- 补充可运行测试或人工验收动作。
- 补充 README、OpenAPI、前端说明或设计文档中的对应说明。
- 如影响主流程，绑定到主流程测试或覆盖清单。

### 发布前检查规则

发布前至少检查：

- 所有 `current` 用例没有因本次改动退化。
- 本次改动补齐了哪些 `gap` 用例。
- 是否新增了目标愿景使命中的概念但没有对应 AGV 用例。
- 是否把 `future` 能力误描述为当前已实现。

## 文档治理关联

本文档只定义 AgentGov 核心功能测试用例，不承载 `docs/` 目录的归档、迁移和权威来源治理规则。相关策略统一维护在 [文档治理与归档策略](./文档治理与归档策略.md)。
