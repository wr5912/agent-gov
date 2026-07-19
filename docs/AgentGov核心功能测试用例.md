# AgentGov 核心功能测试用例

本文档由 [项目目标愿景使命](./项目目标愿景使命.md) 派生，用于指导 AgentGov 的研发、验收和回归验证持续向目标愿景使命逼近。

它不是接口实现说明，也不是某个版本的完成承诺。它是一组目标牵引型核心功能测试用例：当前已经具备的能力应持续回归，尚未完全具备的能力应作为后续开发的验收锚点。

> 文档层级：长期核心验收权威。
> 术语口径：用例目标按长期产品和四阶段改进治理术语表达；自动验收中的 API、pytest nodeid、字段名和实现标识符保持当前代码原名。术语映射见 [AgentGov术语与版本边界](./AgentGov术语与版本边界.md)。
> 归档边界：本文是目标愿景使命的验收清单，不因当前实现文档或四阶段改进治理方案存在而归档。
> 四阶段改进治理覆盖规则：涉及改进治理工作台 UI、阶段、决策卡、处理记录或效果图验收时，以 [AgentGov 四阶段改进治理工作台 UI 整改方案](./AgentGov_四阶段改进治理工作台UI整改方案.md) 为准；旧“反馈工作台 / 回归资产 / 版本管理”入口只作为当前实现或历史验收线索。

## 使用方式

- 每个新增功能、重构或治理改动，都应至少挂接到本文档中的一个核心用例。
- 每个改进事项、版本发布或重大方案评审，都应检查是否增强了至少一个目标用例，且没有破坏已有 `current` 用例；涉及当前迁移前优化批次时，按 [AgentGov术语与版本边界](./AgentGov术语与版本边界.md) 映射到改进事项闭环。
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
| 治理成熟度路径 | 内置业务 Agent 闭环、多业务 Agent、场景包、跨 Agent 方法论沉淀 | AGV-043, AGV-044, AGV-045 |
| 典型落地场景 | 安全运营只是典型场景之一，平台不绑定单一行业 | AGV-046 |
| 产品边界 | AgentGov 负责治理能力，外部系统负责业务界面、权限、生产系统和高风险动作责任；当前不建设通用协作模型 | AGV-047, AGV-048, AGV-049 |
| OpenAI 兼容主路径 | Responses-first 接口可承载 Playground 主运行、会话恢复、外部 API 集成，并保留原生 Chat 兼容面 | AGV-050 |

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

状态：`gap`

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

当前缺口：旧 batch 闭环测试已随旧链路删除；当前只有 ImprovementItem 各阶段的分段测试，尚无从 Runtime 事实到反馈、归因、执行、回归和 release 的单条端到端证据。

### AGV-003 当前前端边界不越界为最终业务门户

状态：`current`

目标来源：系统定位、产品边界。

前置条件：前端可访问或存在前端文档。

测试步骤：

1. 打开前端或阅读 README 的前端说明。
2. 检查前端是否定位为开发调试与治理观察界面。
3. 检查是否没有把外部业务门户、权限审批、生产系统操作职责放入 AgentGov 前端。

成功标准：

- 前端支持 Playground、改进治理工作台（当前实现可表现为迁移前反馈工作台）、评估和版本治理视图等治理观察能力。
- 文档明确外部业务系统承载最终用户业务界面和生产流程。
- 文档明确 AgentGov 当前不提供通用协作看板；Multica 当前只服务本仓库持续 CI。
- 高风险业务动作不由 AgentGov 前端绕过外部系统审批。

证据要求：README、前端页面或用户流程截图。

自动验收：`tests/test_agv_acceptance.py::test_agv_003_048_frontend_is_debug_observation_boundary`。

### AGV-004 Agent 创建与配置能力

状态：`current`

目标来源：使命、核心目标、多 Agent 治理对象。

前置条件：平台允许定义或导入业务 Agent。

测试步骤：

1. 导出一个可运行业务 Agent 的 Workspace 包，修改名称、用途、适用场景和责任边界。
2. 以新 `agent_id` 导入该包，配置 system prompt、skills、tools、MCP、profile 和运行约束。
3. 查询该 Agent 的定义、配置和独立 Git 版本源。

成功标准：

- 新 Agent 具有稳定身份和配置。
- Agent 定义能作为后续运行、反馈、评估和版本治理的归属对象。
- 仓库运行卷初始化源不泄露 API key、凭据型 MCP header 或本机私有路径。
- live workspace 与 per-Agent Git 可原样保存业务运行所需私有配置，平台导入、导出和摘要不得
  在日志或回执中回显正文。

证据要求：Agent 定义记录、配置摘要、live Workspace 原样往返证据和运行卷初始化源准入扫描结果。

自动验收：`tests/test_agent_workspace_packages.py::test_builtin_workspace_package_can_create_a_new_agent_without_identity_rewrite`（Workspace 包导入创建稳定注册身份）、`tests/test_agent_registry_store.py::test_direct_create_and_template_catalog_endpoints_are_removed`（旧直接创建和模板目录不可用）、`tests/test_agent_workspace_packages.py::test_workspace_export_import_round_trip_preserves_binary_endpoint_and_env`（live Workspace 私有配置与二进制原样往返）、`tests/test_runtime_bootstrap_tools.py::test_runtime_bootstrap_safety_scan_is_read_only` 与 `test_runtime_bootstrap_safety_sanitizes_embedded_secret`（仓库初始化源扫描只读且秘密可被显式清理）。配置面采用 Claude Code 原生文件，运行业务 Agent 时以该 Workspace 为 cwd；平台不另建通用模板或 per-Agent 模型凭据来源。

### AGV-005 业务 Agent 与治理 Agent 边界清晰

状态：`current`

目标来源：多 Agent 治理对象。

前置条件：系统存在业务 Agent 与单一治理执行者 `governor`，并能按 job type 承担归因分析、方案生成、执行优化、用例治理和回归影响分析职责。

测试步骤：

1. 创建或选择一个业务 Agent。
2. 针对该业务 Agent 发起反馈优化流程。
3. 检查归因分析、方案生成、执行优化、用例治理、回归影响分析是否由 `governor` 按 job type 执行，而不是暴露为多个当前治理 Agent。

成功标准：

- 业务 Agent 是被治理对象。
- 治理 Agent 是闭环流程的执行者。
- 业务 Agent 是被治理对象，治理 Agent 是平台内部的闭环执行者；当前不预设二者在后期协作系统中的成员模型。
- 治理 Agent 的输出不直接变成生产事实，必须经过后端校验、评估和版本治理。

证据要求：governor profile、输入输出、Trace 和最终投影记录。

自动验收：`tests/test_agent_profiles_category.py`（业务/治理身份与权限边界）；治理 Agent 输出的字段所有权与后端投影由 `tests/test_improvement_governor_service.py::test_hostile_formatter_output_does_not_crash_or_pollute` 覆盖。

### AGV-006 治理闭环产物覆盖数据、方法论和执行资产

状态：`gap`

目标来源：使命、三层资产模型。

前置条件：存在一次完整或模拟的反馈优化流程。

测试步骤：

1. 从一次反馈中生成归因结果。
2. 生成优化方案、SOP 或执行计划。
3. 生成或关联 prompt、skill、playbook、Workspace pytest 测试文件或 governance rule。
4. 检查数据、方法论和执行资产是否被记录并可追溯。

成功标准：

- 数据资产记录发生了什么。
- 方法论资产记录如何分析、如何优化、如何验证。
- 执行资产记录可以被 Agent 或系统复用的改进单元。

证据要求：反馈 case、归因输出、优化方案、Workspace 测试提交和执行资产记录。

当前实现已经把回归测试设计确定性物化为同一业务 Agent Workspace 的不可变 pytest 文件，并将待发布提交与平台测试运行关联；方法论资产也有结构化契约测试。仍缺一次改进事项同时产出并联合追溯三类资产的端到端证据。
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

状态：`gap`

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

自动验收（部分）：`tests/test_conversations_api.py::test_items_project_transcript_via_owning_agent`
与 `tests/test_session_history.py::test_read_session_history_projects_via_committed_sdk_store`
证明 Playground 历史由 owning Agent 的 SDK transcript 投影，SQLite `agent_runs` 不再作为消息恢复源。
仍缺一次真实运行将 input/output/status/error、tool/skill 活动、Trace 与后续反馈、
归因和评估联合反查的运行态证据。

### AGV-009 失败案例可转化为组织级治理知识

状态：`gap`

目标来源：愿景、方法论资产。

前置条件：存在失败反馈或平台测试失败案例。

测试步骤：

1. 针对失败案例收集证据。
2. 完成归因分析并输出问题类型、证据链、影响范围和改进方向。
3. 将改进方向沉淀为 SOP、prompt、skill、Workspace pytest 用例或治理规则。

成功标准：

- 失败不是只保留为错误日志。
- 失败能沉淀为可复用的治理知识。
- 后续同类问题可以被 pytest 用例或治理规则捕获。

证据要求：失败 case、归因输出、沉淀资产、测试文件和后续平台运行结果。

当前实现能把已确认的 `RegressionTestDesign` 新增为 `tests/test_feedback_*.py`，提交更新后的待发布版本并自动排队运行；已有文件不会被覆盖、删除或弱化。仍缺把失败经验自动提升为 SOP、prompt、skill 或跨事项复用治理规则的联合证据。
### AGV-010 跨 Agent 共享已验证的治理经验

状态：`gap`

目标来源：愿景、治理成熟度路径。

前置条件：平台存在多个业务 Agent 和已验证方法论资产。

测试步骤：

1. 选择一个已经验证有效的 SOP、prompt 片段、skill 或测试设计。
2. 经目标 Agent 开发者审查后，把它实现到另一个业务 Agent 的 Workspace。
3. 对目标 Agent 的待发布提交运行独立 pytest。

成功标准：

- 治理经验可以跨 Agent 复用。
- 复用过程记录来源、适用范围、风险和验证结果。
- 不同 Agent 共享方法论时仍保留各自 Workspace、提交、测试和审计边界。

证据要求：资产复用记录、目标 Agent change set 和平台测试结果。

当前业务 Agent 的 Workspace Git、测试文件、平台运行和版本链已按 Agent 隔离，但尚无跨 Agent 方法论包的复制、适用范围、风险和独立验证报告契约。旧场景包实现已归档，不能作为该能力的验收证据。
### AGV-011 数据资产完整记录事实基础

状态：`gap`

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

当前缺口：运行配置诊断和非法文件路径已有局部 evidence package 测试；仍缺正常文件读取、trace 引用、完整性、幂等和归因 job 输出的一体化证据。

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

自动验收：`tests/test_methodology_assets.py::test_methodology_registry_is_single_source_named_and_structured`（每个治理方法有命名 profile + 独立结构化 Pydantic 契约，单一来源复用、非散落 NL）、`tests/test_methodology_assets.py::test_methodology_assets_are_carried_by_version_governed_governance_agents`（方法论由受版本治理的治理 Agent 承载，提供版本/修订记录）。

### AGV-013 执行资产可被 Agent 或系统调用

状态：`gap`

目标来源：三层资产模型。

前置条件：存在 prompt、skill、profile、playbook、Workspace pytest 测试或 workflow。

测试步骤：

1. 选择一个执行资产。
2. 让业务 Agent 或治理 Agent 在受控流程中使用它。
3. 检查调用过程、输出结果和版本关联。

成功标准：

- 执行资产不是只存在于文档中。
- 调用过程可追踪、可评估、可回滚。
- 修改执行资产会进入版本治理或审计记录。

证据要求：调用记录、资产版本和评估结果。

自动验收（部分）：`tests/test_agent_governance_publish.py::test_restore_release_switches_current_workspace_without_mutating_release_history` 证明 release 恢复不改历史。仍缺当前 execution apply API 对真实候选 worktree、文件 diff、version/change set 持久化和源 workspace 不变的端到端验收。

### AGV-014 Runtime 运行可复盘

状态：`gap`

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

自动验收（部分）：`tests/test_conversations_api.py::test_items_project_transcript_via_owning_agent`、
`tests/test_session_history.py::test_endpoint_projects_history` 和
`tests/test_session_history.py::test_normalize_message_hostile_inputs_do_not_crash_or_pollute_role`
证明前端会话历史的 API 真相来自 SDK transcript，并覆盖 owning Agent 和 hostile 消息投影。
仍缺真实运行下 UI 回复细节对 input/output、tool/skill、错误与 Trace 的联合展示证据。

### AGV-015 Feedback Loop 形成标准化归因链路

状态：`gap`

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

当前缺口：归因 governor 的字段所有权和失败回退已有分段测试，但 evidence package 完整性、attribution job 完成输出与“证据不足转人工复核”尚无一条当前契约下的联合验收。

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

自动验收：`tests/test_agent_governance_publish.py::test_candidate_committed_change_set_can_publish_directly`、`tests/test_agent_git_store.py::test_git_store_file_diff_returns_unified_diff`。

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

### AGV-018 业务 Agent 不按历史 ID 特殊化

状态：`current`

目标来源：多 Agent 治理对象、成熟度路径。

前置条件：系统存在多个注册业务 Agent，且定义了内置、默认、受保护属性。

测试步骤：

1. 检查文档、UI 和 API 是否把 `main-agent` 描述为默认、内置、受保护或模板。
2. 检查 `security-operations-expert` 的内置、默认、受保护属性是否分别表达。
3. 验证所有注册业务 Agent 使用同一运行、反馈和版本治理路径。

成功标准：

- `main-agent` 只是普通历史示例，不享有隐式兜底或保护。
- 内置、默认、受保护属性不由来源字段互相推断。
- 文档不把任一业务 Agent ID 写成治理模型本身。

证据要求：文档检索结果。

自动验收：`tests/test_agv_acceptance.py::test_agv_018_business_agents_do_not_special_case_historical_main_id`。

### AGV-019 治理 Agent 只输出建议和治理产物

状态：`current`

目标来源：多 Agent 治理对象、治理边界。

前置条件：治理 Agent profile 可运行。

测试步骤：

1. 触发归因、方案、执行优化、用例治理或回归影响分析业务动作。
2. 检查治理 Agent 是否只读取允许输入。
3. 检查输出是否由后端校验和投影，不直接修改生产事实。

成功标准：

- 治理 Agent 不直接修改主 Agent 配置或生产系统。
- 输出通过 formatter、Pydantic 校验、store 投影和 API 展示。
- 失败会返回结构化错误或形成明确回退来源，并投影到用户可见状态。

证据要求：业务动作输入、validated output、结构化错误或投影记录。

自动验收（部分）：`tests/test_improvement_governor_service.py::test_hostile_formatter_output_does_not_crash_or_pollute`、`tests/test_improvement_governor_service.py::test_attribution_rejects_governor_workspace_as_business_agent_evidence`。权限边界部分由运行时测试与人工验收覆盖。

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

自动验收（按三条成功标准）：①明确合法转移——`agent_lifecycle` 状态机（draft→active→evaluating→deprecated→archived，archived 终态）+ API `POST /api/agent-registry/{id}/lifecycle`；②非法转移被拒并返回可理解错误——`tests/test_agent_registry_store.py::test_business_agent_lifecycle_transitions_and_archived_excluded_from_run`（archived→active 被 StateTransitionError 拒绝，409 带转移说明）；③archived 仍可审计但不参与新运行——同测试断言 archived Agent 仍在注册表可查、但 `/api/chat?agent_id=` 拒绝运行（`AGENT_RUNNABLE_LIFECYCLE_STATES`）。状态列由迁移 0009 持久化；`main-agent` 与其他普通业务 Agent 使用同一状态机。

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

- 待发布版本、已发布版本和回滚版本可区分。
- 历史版本可追溯，不被物理删除破坏历史解释。
- rollback 不删除历史 release。

证据要求：version graph、release archive、rollback event。

自动验收：`tests/test_agent_governance_publish.py::test_business_agent_version_lifecycle_preserves_history_through_rollback`（业务 Agent 经候选→发布 v1/v2→restore→rollback：候选/已发布/回滚版本状态可区分，restore 切换当前版本不改写 release 历史，rollback 仅标记 `rolled_back` 而不物理删除 release，两条 release 在 Agent 维度仍可追溯）。per-agent 版本链由 `AgentGovernanceService._store_for(agent_id)` 提供物理隔离（业务 Agent 版本 store 根 `data_dir/business-agents/{agent_id}/version`），与 main agent 版本链互不混淆。

### AGV-022 Agent 资产 Registry 记录资产关系

状态：`current`

目标来源：Agent 资产 Registry。

前置条件：存在 Agent Workspace、prompt、skill、pytest 文件、change set 和 release。

测试步骤：

1. 查询某个 Agent 的资产 Registry。
2. 查看该 Agent 关联的配置、方法论、测试运行、change set 和 release。
3. 从某次 feedback 追溯到改进事项和最终版本。

成功标准：

- Registry 不是简单清单，而能表达资产关系。
- 能回答“某次反馈影响了哪个 Agent、改了哪些资产、进入哪个版本”。
- Registry 记录可被 API 或 UI 查询，测试正文仍以 Workspace Git 为准。

证据要求：Registry 查询结果、Workspace suite 摘要和平台测试运行。

自动验收：`tests/test_agent_registry_store.py::test_feedback_asset_provenance_traces_agent_and_relationship` 证明反馈可追溯到所属 Agent、改进事项及 change set；`tests/test_agent_testing.py::test_suite_inspection_treats_workspace_tests_as_versioned_source_of_truth` 证明测试内容从指定提交派生，不写入第二套 Registry body。
### AGV-023 Registry 防止资产散落和重复沉淀

状态：`gap`

目标来源：Agent 资产 Registry、能力域与场景组织。

前置条件：存在多个相似 prompt、skill、pytest 用例或 SOP。

测试步骤：

1. 导入或创建多个相似治理资产。
2. 运行去重、合并或关联检查。
3. 查看 Registry 中的主资产、替代关系和引用方。

成功标准：

- 重复资产有检测和治理建议。
- 合并或废弃资产可审计。
- 引用关系不会因资产合并而丢失。

证据要求：资产治理事件和引用关系。

当前 Registry 已把通用方法论/执行/审计资产与 Workspace pytest 真相分开，避免复制测试正文，但尚未提供跨资产重复检测、合并/废弃治理事件和引用重定向契约，因此本项保持 `gap`。
### AGV-024 反馈可归属到 Agent、version、run 和场景

状态：`gap`

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

自动验收（部分）：`tests/test_agent_governance_publish.py::test_governance_serves_multiple_business_agents_with_isolated_closed_loops` 证明已匹配 run 的多 Agent 隔离。仍缺无 source locator 拒绝、无匹配 run 的显式人工兜底，以及 signal/case 投影归属一致性证据。

### AGV-025 反馈路由错误不会污染其他 Agent

状态：`gap`

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

自动验收（部分）：
`tests/test_improvement_content.py::test_attach_feedback_case_rejects_cross_business_agent_without_side_effects`
证明跨 Agent 挂接被拒绝且无副作用；
`tests/test_improvement_content.py::test_feedback_case_assignment_is_unique_and_reassign_moves_authoritative_ref`
证明唯一 assignment 关系和重新归属会同步权威引用。仍缺管理员修正原始 feedback signal/case
Agent 归属时的专用 API、权限和完整审计证据，因此保留 `gap`。

### AGV-026 能力域或场景包可组织治理资产

状态：`gap`

目标来源：能力域与场景包。

前置条件：平台支持能力域或场景包。

测试步骤：

1. 创建一个场景包，例如“告警研判”或“客服投诉处理”。
2. 关联 Agent 定义、prompt、skill、SOP、Workspace pytest 测试和发布准入规则。
3. 查询场景包详情。

成功标准：

- 场景包表达业务目标、适用范围和风险等级。
- 场景包中的资产可迁移、可复制、可审计。
- Agent 可按场景包装配能力。

证据要求：场景包定义和资产关联。

当前缺口：旧场景包表、store 和公开 API 已迁移归档；Workspace 包可以复用完整 Agent 配置，
但尚无一等的能力域/场景资产聚合契约，不能以导出包替代本项验收。

### AGV-027 场景包支持跨 Agent 复用

状态：`gap`

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

当前证据：多业务 Agent 的版本库、Workspace pytest 与 `AgentTestRun` 已按 Agent 隔离；当前没有跨 Agent
场景资产复用记录，且 Agent active 门禁不能由已删除旧场景包链证明，因此本项保持 `gap`。

### AGV-028 反馈到资产闭环完整

状态：`gap`

目标来源：反馈到资产的闭环链路。

前置条件：存在可执行反馈优化流程。

测试步骤：

1. 发起业务 Agent 运行并提交反馈。
2. 将反馈归属到业务 Agent、运行版本和场景。
3. 生成证据、归因、优化方案和完整 pytest 代码候选，核对测试意图、断言依据与目标路径。
4. 应用改动并确认待发布变更，在同一 change set 中把配置与 Workspace pytest 收口为相对修复前版本的同一个待发布 commit；确认动作不得自动运行测试。
5. 显式运行当前待发布 commit 的完整 `tests/`，通过后发布；失败则返工或放弃待发布变更。

成功标准：

- 链路中每一步都有持久化记录或可审计事件。
- 任一步失败会投影为明确状态和错误详情。
- 成功后能从 release 反查反馈、归因、优化、测试文件和平台运行证据。

证据要求：端到端链路记录。

当前专项测试已分别覆盖生成、执行、测试物化、精确提交门禁和发布片段，真实容器验收也覆盖四阶段主路径；仍需持续保留单条 release 反查全部证据与 Trace 的联合断言。
### AGV-029 闭环失败可恢复

状态：`gap`

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

自动验收（部分）：`tests/test_responses_stream.py::test_stream_finalization_exhaustion_interrupts_and_allows_immediate_retry`、
`tests/test_runtime_db_0040.py::test_0040_archive_and_destructive_changes_roll_back_together`、
`tests/test_agent_governance_publish.py::test_publish_db_finalize_failure_rolls_back_metadata_and_retry_reconciles` 和
`tests/test_agent_maintenance_recovery.py::test_restore_reconciles_crash_after_git_before_operation_persistence`、
`tests/test_agent_maintenance_recovery.py::test_reconciler_completes_expired_restore_after_git_without_repeating_reset`、
`tests/test_agent_maintenance_recovery.py::test_worktree_cleanup_reconciles_crash_after_idempotent_git_delete`
已覆盖 Runtime finalize CAS 耗尽后的立即恢复、旧链归档与 DDL 原子回滚、publish/rollback/restore durable operation 对账，
以及终态 worktree cleanup 的启动恢复和用户重试入口。残余缺口是完整 Improvement 闭环在任一阶段失败后的
单条跨层恢复验收仍未统一覆盖。

### AGV-030 闭环结果能防止历史问题复发

状态：`gap`

目标来源：反馈到资产闭环、发布条件。

前置条件：历史反馈已经形成回归测试设计，且存在包含修复的未发布 change set。

测试步骤：

1. 从历史反馈生成并确认 `RegressionTestDesign`。
2. 在同一 change set 中新增 `tests/test_feedback_*.py` 并提交更新后的待发布版本。
3. 对该精确提交执行平台 pytest。
4. 验证失败或旧提交上的通过记录不能放行当前待发布提交。

成功标准：

- 高价值历史反馈能转化为随业务 Agent Git 版本化的 pytest 文件。
- 平台运行保留 nodeid、结果、stdout、stderr 和错误详情。
- 历史高危问题复发会阻断普通发布；例外只能通过有原因、有警告、有审计的强制发布。

证据要求：`RegressionTestDesign`、Workspace 测试提交、`AgentTestRun` 和发布审计。

自动验收（部分）：`tests/test_improvement_execution_service.py::test_generated_feedback_tests_are_flat_immutable_and_idempotent` 和 `test_materialized_feedback_test_rebinds_same_unpublished_change_set` 证明反馈测试文件扁平、不可覆盖且推进同一未发布 change set；`tests/test_agent_governance_publish.py::test_publish_requires_passed_platform_test_for_exact_candidate_commit` 证明旧提交或非通过运行不能满足普通发布条件；`test_force_publish_requires_reason_and_persists_warning_audit` 证明强制发布必须记录原因和警告。仍缺 flaky、测试过时和能力退化的自动分类解释。
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

自动验收：`tests/test_agent_registry_store.py::test_delete_business_agent_reports_impact_and_protects_builtin_agent`（Workspace 包导入创建治理对象后删除，`agent_id` 在 run/feedback/test/version 一致；删除前给出 `runs/feedback_signals/improvements/test_runs/change_sets/releases` 影响面；受保护的 `security-operations-expert` 不可删，未知 ID 返回 404）。配套：包导入创建 `test_builtin_workspace_package_can_create_a_new_agent_without_identity_rewrite`（AGV-004）、运行 `test_chat_routes_to_registered_business_agent`、生命周期归档 `test_business_agent_lifecycle_transitions_and_archived_excluded_from_run`。`agent_id` 全链路一致由 B2+B3.1~B3.4 归属贯通背书。

### AGV-032 运行记录支持事实、推断和建议分离

状态：`gap`

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

自动验收（部分）：`tests/test_feedback_output_normalizers.py::test_attribution_formatter_output_drops_backend_owned_fields` 证明后端字段所有权边界。仍缺 `reasoning_error` 与数据、工具、执行资产问题的独立分类回归；字段形状本身不能替代该业务语义验收。

### AGV-033 反馈进入问题分类和证据链

状态：`gap`

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

自动验收（部分）：`tests/test_runtime_db.py::test_feedback_store_soc_event_ingest_is_idempotent_under_concurrency` 证明并发摄取幂等。仍缺 matched/duplicate/pending 三分类响应、证据链与人工复核提示的联合验收。

### AGV-034 优化形成可执行资产而非一次性建议

状态：`gap`

目标来源：核心目标 4。

前置条件：存在归因结果和优化方案。

测试步骤：

1. 从归因结果生成优化方案。
2. 检查方案是否明确产物类型。
3. 将方案转化为 prompt、skill、SOP、playbook、Workspace pytest、governance rule 或 change set。

成功标准：

- 优化方案不只是自然语言建议。
- 每个优化任务有目标对象、预期效果、验证方式和风险。
- 可执行资产进入版本治理或 Registry。

证据要求：optimization task、change set 或资产记录。

自动验收（部分）：`tests/test_improvement_execution_service.py::test_governor_success_applies_and_binds_version`、
`tests/test_improvement_execution_service.py::test_parallel_apply_creates_only_one_change_set` 和
`tests/test_improvement_execution_service.py::test_missing_link_is_reconciled_in_same_request_after_finalize`
证明服务层可生成候选并绑定版本，且并发申请和 finalize/link 分步失败可幂等对账。
仍缺 execution apply HTTP API 经真实 governor、Git worktree、file diff 和 change set 投影的联合端到端证据。

### AGV-035 平台测试成为发布前质量闸门

状态：`current`

目标来源：核心目标 5。

前置条件：存在待发布 change set，且该提交包含可运行的 `tests/test_*.py`。

测试步骤：

1. 对待发布提交创建 `AgentTestRun`。
2. 使用平台固定命令执行 pytest 并持久化结果。
3. 根据当前精确提交的结果决定发布、修复、重跑或强制发布。

成功标准：

- 没有当前待发布提交的 `passed` 记录时，普通发布被阻断。
- 运行 item、stdout、stderr 和结构化 error 能解释失败。
- 取消、中断、错误和旧提交通过均不能冒充当前提交通过。
- 强制发布必须有非空原因，并持久化原阻塞项和警告；provenance 不完整不可绕过。

证据要求：Workspace suite、`AgentTestRun`、发布阻塞原因和 release 审计。

自动验收：`tests/test_agent_testing.py::test_test_run_store_uses_independent_lifecycle_and_exact_commit_gate`、`test_test_run_cancel_and_restart_recovery_are_explicit`、`tests/test_agent_governance_publish.py::test_publish_requires_passed_platform_test_for_exact_candidate_commit` 和 `test_force_publish_requires_reason_and_persists_warning_audit`。
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

自动验收：`tests/test_agent_governance_publish.py::test_restore_release_switches_current_workspace_without_mutating_release_history`、`tests/test_agent_git_store.py::test_git_store_file_diff_returns_unified_diff`。

### AGV-037 外部业务系统责任边界清晰

状态：`current`

目标来源：核心目标 7、产品边界。

前置条件：存在外部业务系统集成场景。

测试步骤：

1. 外部业务系统通过 API 提交上下文、运行请求或反馈。
2. AgentGov 返回治理结果、建议或候选变更。
3. 高风险业务动作由外部系统确认。

成功标准：

- AgentGov 不接管外部系统的用户、角色、审批和生产动作责任。
- AgentGov 当前不额外建立 issue、任务、看板、协作成员和状态流转模型。
- 外部系统能追踪 AgentGov 的运行和建议。
- 高风险动作不会由 Agent 自动绕过审批执行。

证据要求：API 调用、external governance item 或审批记录。

自动验收：`tests/test_agv_acceptance.py::test_agv_037_047_governance_scope_not_business_ownership`；高风险不绕过审批由 AGV-041 背书。

### AGV-038 API 错误和 job 失败可见

状态：`current`

目标来源：Runtime、Feedback Loop、产品边界。

前置条件：可触发错误请求或失败的治理模型调用。

测试步骤：

1. 发起无效输入、无权限请求或模拟治理模型调用失败。
2. 查询 API 响应、provider readiness 和 UI 状态。
3. 检查错误是否可用于下一步处理。

成功标准：

- 错误包含 error code 或明确 detail。
- 模型调用失败包含 probe、reason、retryable 和 action 等可执行诊断。
- UI 不显示误导性空态。

证据要求：错误响应、readiness 摘要、UI 失败态。

自动验收：`tests/test_api_error_handlers.py::test_feedback_store_error_handler_returns_structured_error`、`tests/test_model_provider_router.py::test_vllm_transport_failure_stops_agent_request_with_precise_diagnostic`、`tests/test_health_endpoints.py::test_readiness_reports_sanitized_vllm_timeout_and_recovers`。

### AGV-039 当前调试前端可观察核心治理链路

状态：`current`

目标来源：系统定位、产品边界。

前置条件：前端和后端可运行。

测试步骤：

1. 打开前端。
2. 查看 Playground、四阶段改进治理工作台，以及必要的全局审计入口。
3. 执行或查看一条反馈优化流程。

成功标准：

- 前端能观察运行、反馈、评估和版本状态。
- 关键失败态有错误详情或可追踪 Trace。
- 前端不要求用户必须在 AgentGov 内完成所有业务操作。

证据要求：浏览器截图、console error 记录、API request 结果。

自动验收（部分）：`tests/test_agv_acceptance.py::test_agv_003_048_frontend_is_debug_observation_boundary`。运行态状态观察与错误展示需前端/运行态验收。

### AGV-040 离线或内网部署不破坏必需闭环

状态：`current`

目标来源：产品不变量、使命。

前置条件：使用本地或内网模型网关配置。

测试步骤：

1. 启动 API、UI 和必要本地服务。
2. 发起运行、反馈、归因或评估动作。
3. 验证流程不依赖公网远程服务。

成功标准：

- 必需工作流可指向本地或内网模型网关。
- 缺少模型凭据时模型动作明确失败，而不是生成 raw/offline 伪结果。
- 示例配置不泄露真实凭据。

证据要求：env 配置摘要、业务动作结果、健康检查。

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

1. 分别查询配置摘要、live Workspace 原样导出包、仓库运行卷初始化源和公开文档。
2. 检查 live 包是否保持原字节且按敏感资产交付，仓库与公开输出是否出现真实密钥、凭据型
   header 或本机私有路径。
3. 对受保护 API 使用错误凭据请求。

成功标准：

- 真实密钥、凭据型 MCP header、数据库凭据和本机私有路径不会进入项目仓库、提交说明、
  公开文档或运行卷初始化源。
- live workspace 导出包是精确敏感资产，可包含这些运行值；它不能被误称为“安全脱敏模板”，
  也不能在日志、错误详情或公开摘要中回显。
- 初始化源中的普通 endpoint、内网地址与专用权限只提示复核，不被静默改写；真正秘密仍阻断 CI。
- 当前前端调试界面和自托管 Langfuse 属开发调试面，可展示完整 prompt、tool input/output、job input/output、raw text 和 trace I/O，不作为生产安全边界。
- 错误凭据被拒绝。

证据要求：Workspace 包 digest/commit、运行卷初始化源扫描结果、调试观测面边界说明和 401 响应。

自动验收：`tests/test_repository_env_policy.py::test_official_env_examples_do_not_ship_configured_model_provider_key`、`tests/test_agent_workspace_packages.py::test_workspace_export_import_round_trip_preserves_binary_endpoint_and_env`、`tests/test_runtime_bootstrap_tools.py::test_runtime_bootstrap_safety_scan_is_read_only`、`tests/test_runtime_bootstrap_tools.py::test_runtime_bootstrap_safety_sanitizes_embedded_secret`；前端/Langfuse 完整调试观测由运行时和 UI 验收覆盖。

### AGV-043 内置业务 Agent 端到端闭环

状态：`gap`

目标来源：治理成熟度路径。

前置条件：默认内置业务 Agent `security-operations-expert` 及反馈闭环能力可用。

测试步骤：

1. 对 `security-operations-expert` 发起运行。
2. 提交反馈并进入归因、优化、评估和版本治理。
3. 查询完整链路。

成功标准：

- 内置业务 Agent 的完整闭环可运行。
- 闭环中产生的资产可用于后续抽象。
- 文档不把具体业务 Agent ID 误写成长期治理边界。

证据要求：`security-operations-expert` 端到端闭环记录。

当前缺口：分段能力已有测试，但仍缺少从默认内置业务 Agent 运行、反馈、归因、执行、回归到发布且可反查 provenance 的单条验收。

### AGV-044 第二阶段多业务 Agent 扩展

状态：`current`

目标来源：治理成熟度路径。

前置条件：平台完成多业务 Agent 建模。

测试步骤：

1. 验证通用治理模型不依赖内置业务 Agent ID。
2. 通过 Workspace 包接入一个新的业务 Agent。
3. 重用运行、反馈、归因、优化、评估和版本治理能力。

成功标准：

- 多业务 Agent 不需要复制内置业务 Agent 的硬编码路径。
- 反馈、版本、评估都按 Agent 维度隔离。
- 新 Agent 接入有明确迁移和验收步骤。

证据要求：多 Agent API、配置和回归结果。

自动验收：`tests/test_agent_registry_store.py::test_workspace_imported_business_agents_share_governance_without_builtin_special_cases`（通过 Workspace 包接入新业务 Agent，复用 run/feedback/eval/version 能力且全部经 `agent_id` 归属；新 Agent 与内置 Agent 的版本 store 经同一 `_store_for` 工厂取得不同实例、物理隔离）。配套：多 Agent 独立闭环隔离 `test_governance_serves_multiple_business_agents_with_isolated_closed_loops`（AGV-017）、接入配置面 `test_builtin_workspace_package_can_create_a_new_agent_without_identity_rewrite`（AGV-004）、统一入口与影响面 `test_delete_business_agent_reports_impact_and_protects_builtin_agent`（AGV-031）。

### AGV-045 第三与第四阶段场景包和跨 Agent 方法论沉淀

状态：`gap`

目标来源：治理成熟度路径、能力域与场景包。

前置条件：已有多个业务 Agent 和可复用方法论资产。

测试步骤：

1. 建立一个跨 Agent 可复用的方法论资产。
2. 绑定到一个有适用范围和风险说明的能力包。
3. 在多个 Agent 中复用并评估。

成功标准：

- 方法论资产不是单 Agent 私有经验。
- 能力包能组织 prompt、skill、SOP、eval 和版本策略。
- 跨 Agent 复用后仍保留独立审计和评估结果。

证据要求：能力包、方法论资产和跨 Agent 评估报告。

当前缺口：治理方法已有集中 typed formatter/prompt registry，多业务 Agent 也已有独立版本与评估边界；
仍缺跨 Agent 能力包及其复用 provenance、风险和逐 Agent 验证报告。旧场景包实现已迁移归档。

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

证据要求：文档和内置业务 Agent Workspace 说明。

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
- AgentGov 当前不复制通用协作看板、issue 生命周期和成员管理。
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
3. 检查前端是否明确作为开发调试观测面展示完整运行证据，而不是生产控制台。

成功标准：

- 前端不接管 Claude Code CLI 进程。
- 前端不编辑宿主机敏感文件。
- 聊天、反馈、评估和版本治理通过后端治理 API 完成。

证据要求：前端页面、代码检索或手工验证记录。

自动验收：`tests/test_agv_acceptance.py::test_agv_003_048_frontend_is_debug_observation_boundary`。

### AGV-049 多智能体协作平台选型晚于核心治理稳定

状态：`current`

目标来源：产品边界、治理成熟度路径。

前置条件：存在 AgentGov 产品路线说明和后期多智能体协作设想。

测试步骤：

1. 阅读目标愿景使命、README 和当前 CI 工程契约。
2. 检查当前 Multica 是否只用于 `agent-gov` 仓库持续 CI 通知。
3. 检查近期阶段是否聚焦 AgentGov 本身、智能体开发和反馈优化闭环。
4. 检查多智能体协作平台是否明确留到核心能力稳定且出现真实需求后重新选型，且没有提前锁定 Multica 或脑补协作模型。

成功标准：

- Multica 当前只用于 `agent-gov` 仓库持续 CI 的 AID 通知和研发协作会话。
- 当前产品不包含通用协作看板、issue/task 生命周期、成员、assignee 或 squad 模型。
- 当前重点明确是 AgentGov 平台、智能体开发和反馈优化闭环。
- 核心能力稳定且出现真实需求后才重新选择多智能体协作方案；Multica 只是候选，不是承诺，当前不定义固定版本门槛、任务分配、成员或状态模型。

证据要求：目标愿景使命、执行计划、README 或后续集成设计中的阶段说明。

自动验收：`tests/test_agv_acceptance.py::test_agv_049_collaboration_platform_selection_is_deferred_while_multica_ci_is_current`。该验收同时约束两条边界：当前 Multica 只服务仓库 CI；后期多智能体协作在真实需求出现后重新选型，不预先锁定 Multica。

### AGV-050 OpenAI Responses-first 接口替代原生 Chat 主路径

状态：`current`

目标来源：核心目标 7、Runtime、外部 API 集成、`docs/engineering/OpenAI兼容接口能否替代原生Chat端点评估.md`。

前置条件：API、UI 容器已用当前代码和 `docker/.env` 配置启动；存在至少一个 runnable 业务 Agent（默认 `security-operations-expert`）。

测试步骤：

1. 通过 Playground 发起一次真实业务 Agent 运行。
2. 确认前端主运行请求走 `POST /v1/responses` control 模式，而不是旧 `/api/chat/stream`。
3. 通过 `/v1/conversations` 读取会话列表，并通过 `/v1/conversations/{conversation_id}/items` 验证会话 items 契约可用。
4. 通过 `GET /v1/responses/{response_id}` 验证 `resp_<run_id>` 可从持久化 run 重建响应。
5. 发起对抗式与边界请求：strict 模式 `instructions`、control 缺 `agentgov.agent_id`、`agentgov` 未知字段、非法 `max_turns`、保留 metadata 注入、旧 `/api/chat`/`/api/chat/stream` 缺 `agent_id`。

成功标准：

- `/v1/responses` + `/v1/conversations` 承载 Playground 主运行与会话恢复主路径，前端不再把旧 `/api/chat/stream` 作为主运行入口。
- control 模式能把业务 Agent、conversation、run、response retrieve 串成同一条运行事实链；`response.output[]` 与 `agentgov.run_id/session_id/conversation_id` 可审计。
- hostile 输入被 4xx 拒绝，保留 metadata 不回显，旧原生 Chat 入口仍按兼容契约拒绝缺失 `agent_id`，不静默跑 main。
- `/v1/chat/completions` 保持兼容入口定位，不作为 HITL、会话治理或工具时间线主控制面。

证据要求：OpenAPI/pytest 契约、前端网络请求、真实容器 Playwright 截图、API 响应、容器健康状态。

自动验收：核心 API 契约已绑定到 `tests/quality_policy.json` 的 `openai_responses_first_surface` 主流程，覆盖 `tests/test_responses_api.py`、`tests/test_responses_stream.py`、`tests/test_responses_retrieve.py`、`tests/test_conversations_api.py`；旧 Chat 兼容由 `tests/test_chat_stream_agent_id.py` 和 `tests/test_openai_compat_agent_config.py` 回归。真实容器端到端验收使用 `pnpm --dir frontend run verify:openai-responses-container`：该脚本打开 Compose UI、真实调用 Compose API，验证 UI 请求 `/v1/responses`、会话走 `/v1/conversations`、retrieve 可用，并执行 hostile / boundary 请求。

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
