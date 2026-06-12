# AgentGov 目标达成分阶段执行计划

本文档是以核心功能测试用例为驱动、分阶段逼近项目目标愿景使命的执行路线。

它派生自 [项目目标愿景使命](../项目目标愿景使命.md) 与 [AgentGov核心功能测试用例](../AgentGov核心功能测试用例.md)。三者分工：愿景定义方向，测试用例定义验收锚点，本计划定义推进节奏。本文档不重复用例内容与 `开发推进规则`（进入条件、状态升级、发布前检查以用例文档为准），只定义阶段划分、单次迭代闭环和跟踪方式。

## 治理对象预检

按 `agentgov-governance-preflight`，把「研发推进过程」本身当作一个被治理的反馈闭环（自举），先建模再执行：

| 维度 | 结论 |
| --- | --- |
| 被治理对象 | AgentGov 自身向愿景演进的能力集合，以 AGV 用例为切片 |
| 治理执行者 | 研发动作 + 治理硬门（`make test`、`check_codex_governance --mode fail`）+ AGV 用例人工/自动验收 |
| 资产类型 | 数据资产：迭代日志、run/feedback/eval 证据；方法论资产：本计划与推进规则；执行资产：测试、smoke 脚本、状态升级后的 README/OpenAPI |
| 生命周期 | 每个 AGV 用例状态 `future` → `gap` → `current`，对应被治理能力的成熟度 |
| 反馈归属 | 每次迭代结果归属到具体 AGV 编号、提交和版本 tag |
| 当前实现边界 | 26 `current` 已应具备并需回归；10 `gap`、13 `future` 尚未完整 |
| 目标能力边界 | 全部 49 个用例达到 `current` 且互不退化，即愿景在可验收意义上达成 |

闭环链路（与产品自身闭环同构）：

```text
选用例 -> 执行(测试/smoke/人工) -> 记录反馈(通过/退化/缺口证据)
-> 归因 -> 最小优化(补齐一个 gap 或加固一个 current)
-> 评估(make test + AGV 验收 + 治理硬门) -> 版本(commit/tag + 状态升级)
-> 回写用例状态与迭代日志
```

风险自检：不把 `future` 误当已实现；不为追状态升级造脆测/冗测；不在调试前端塞生产控制台；补 `gap` 前先做用户旅程，不直接堆按钮/job/schema；不把 Multica 等外部协作平台对接提前成前三个产品大版本的近期目标。

## 现状基线

来自用例文档状态分布：

| 状态 | 数量 | 含义 | 在本计划中的角色 |
| --- | --- | --- | --- |
| `current` | 26 | 当前应具备 | 回归锚点，任何阶段不得退化 |
| `gap` | 10 | 目标明确、能力不足 | 第一阶段主战场 |
| `future` | 13 | 长期愿景/成熟度 | 第二至五阶段路线 |

> 基线随迭代更新：初始 22/14/12；阶段 1 已将 AGV-005（业务/治理边界）、AGV-041（高风险审批门）、AGV-037 与 AGV-047（外部系统/职责边界，由审批门+无业务所有权端点+审计记录背书）补到 `current`；合入 AGV-049（外部协作平台集成，future）后总数 49（26/10/13）。AGV-004 创建/身份/profile/配置容器就绪，运行执行待 live 环境。

`gap`/`future` 用例按主题聚类：Agent 创建与边界、三层资产模型完整性、反馈路由与归属、闭环可恢复、Registry、生命周期、场景包、跨 Agent 方法论、审批与责任边界、外部协作生态集成。

## 单次迭代闭环（最小推进单元）

每次迭代只推进一个可验收增量，对齐用例文档「每个改动挂接至少一个 AGV 用例」：

1. 选 1 个目标用例（优先当前阶段的 `gap`，或一个退化的 `current`）。
2. 按用例 `测试步骤` 执行：自动测试 / API smoke / UI smoke / 人工验收，任选适配项。
3. 记录反馈到迭代日志：通过、退化或缺口，附证据（测试 nodeid、API 响应、截图或 PR diff）。
4. 归因：缺口属于实现缺失、契约不一致、前端未暴露还是文档滞后。
5. 最小优化：补齐该 `gap` 或加固该 `current`，遵守产品化翻译门与代码质量优先。
6. 评估：`make test` 全绿（含治理硬门与覆盖率门）、目标用例达到成功标准、`current` 不退化。
7. 版本与升级：提交（提交说明带 AGV 编号），按用例文档「状态升级规则」更新用例状态并同步 README/OpenAPI/前端/覆盖清单；必要时打 tag。
8. 回写迭代日志，决定下一个用例。

## 分阶段路线

阶段对齐愿景「治理成熟度路径」（AGV-043 至 AGV-045），并把外部协作平台深度对接作为长期生态集成阶段（AGV-049）。阶段内可按用例并行，阶段间以「上一阶段目标用例全部达 `current`」为推进前提。

### 阶段 0：固本（current 回归基线）

- 目标：把 22 个 `current` 用例各建立一条可重复验收路径（自动测试、smoke 或人工三类至少其一），形成不退化基线。
- 覆盖用例：AGV-001、AGV-002、AGV-003、AGV-007、AGV-008、AGV-011、AGV-014、AGV-015、AGV-016、AGV-018、AGV-019、AGV-030、AGV-033、AGV-035、AGV-036、AGV-038、AGV-039、AGV-040、AGV-042、AGV-043、AGV-046、AGV-048。
- 退出标准：每个 `current` 用例有登记的验收方式，并纳入回归；后续阶段每次 `make test` 即守住该基线。
- 进度：22/22 个 `current` 用例已登记自动验收（焦点 acceptance 测试或绑定既有回归），见用例文档各 `自动验收` 行与迭代日志；其中 5 个为 `partial`，运行态/人工子标准已标注。
- 验证：自动测试绑定 `tests/coverage_policy.json` 主流程项；非自动项在迭代日志登记人工/smoke 路径。

### 阶段 1：单 Agent 闭环补齐（gap → current，对应 stage 1 main agent 样板）

把剩余 10 个 `gap` 全部推进到 `current`，做实 main agent 样板闭环。

| 子主题 | 覆盖用例 | 退出标准 |
| --- | --- | --- |
| Agent 创建与边界 | AGV-004、AGV-005 | 能创建/配置 Agent；业务 Agent 与治理 Agent 边界在 API、数据和文档一致 |
| 三层资产模型完整 | AGV-006、AGV-012、AGV-013、AGV-032、AGV-034 | 数据/方法论/执行资产各有可调用产物；运行记录区分事实/推断/建议；优化沉淀为可执行资产 |
| 反馈路由与闭环 | AGV-009、AGV-024、AGV-028、AGV-029 | 反馈可归属到 Agent/version/run/场景；失败案例转化为治理知识；闭环完整且失败可恢复 |
| 审批与责任边界 | AGV-037、AGV-041、AGV-047 | 高风险动作需审批；外部系统责任边界清晰且 AgentGov 不越界 |

退出标准：阶段 1 目标用例状态升级为 `current`，且阶段 0 基线无退化。

### 阶段 2：多业务 Agent 扩展（future，对应 stage 2，AGV-044）

- 覆盖用例：AGV-017（多 Agent 接入统一闭环）、AGV-020 与 AGV-021（生命周期状态围绕版本治理）、AGV-025（路由隔离不串扰）、AGV-031（创建/配置/运行/治理统一入口）、AGV-044（阶段验收）。
- 退出标准：至少两个业务 Agent 各自跑通独立的运行、反馈、归因、优化、评估、版本闭环；反馈不跨 Agent 污染。

### 阶段 3：Registry 与场景包（future，对应 stage 3，AGV-045 前半）

- 覆盖用例：AGV-022 与 AGV-023（资产 Registry 记录关系并防散落重复）、AGV-026 与 AGV-027（能力域/场景包组织并跨 Agent 复用）。
- 退出标准：资产之间建立可查询关联（某反馈影响哪个 Agent、触发哪个优化、改了哪些 prompt/skill、进了哪个版本）；场景包可迁移复制。

### 阶段 4：跨 Agent 方法论沉淀（future，对应 stage 4，AGV-045 后半）

- 覆盖用例：AGV-010（跨 Agent 共享已验证经验）、AGV-044 与 AGV-045 收尾。
- 退出标准：不同业务 Agent 可复用其他 Agent 已验证的优化经验与方法论资产，形成组织级治理知识库。

### 阶段 5：外部协作生态集成（future，对应 AGV-049）

- 覆盖用例：AGV-049（外部协作平台对接晚于核心治理稳定）。
- 启动条件：AgentGov 至少完成三个产品大版本，且受治理 Runtime、反馈闭环、归因优化、评估回归、版本治理、多业务 Agent 治理和资产 Registry 已稳定运行。
- 退出标准：Multica、Jira、GitHub Issues 等外部协作平台可以把任务分配给受治理业务 Agent，并接收 AgentGov 返回的运行状态、治理结果和版本化交付物；AgentGov 不复制通用协作看板、issue 生命周期、assignee/squad 管理或外部平台的协作状态流转。

## 分支与跟踪

- 分支：从 `master` 创建专用分支 `dev/agv-closed-loop`，承载基于本计划的迭代。每个迭代一个小提交或小 PR，提交说明带对应 AGV 编号；阶段收尾合回 `master` 并打 tag。
- 迭代日志：在本文档末尾「迭代日志」表持续追加，每行记录日期、AGV 编号、动作、结果、状态变更和证据链接。日志是数据资产，不删历史行。
- 进入/退出/发布前检查：统一引用用例文档「开发推进规则」，本计划不复制。

## 与现有治理门的衔接

每次迭代必须通过仓库既有硬门，不为推进愿景而绕过：

- `make test`：含 `codex-guard`（`check_codex_governance --mode fail`）、全量 pytest、覆盖率门。
- `check_orphan_tests.py`：删除旧能力时同步删测，无孤儿引用。
- `check_docs_governance.py`：新增文档进 `docs/README.md`、四对 skill 镜像一致、无未完成标记。
- 测试增删改按 `test-sync-governance`；runtime/env 改动按 `runtime-env-governance`；产品方案按 `agentgov-governance-preflight`。
- 状态升级触及主流程时同步 `tests/coverage_policy.json` 的 nodeid 绑定。

## 验收标准

- 计划自身：进入 `docs/README.md` 工程治理入口，通过文档治理硬门，可从愿景与用例文档追溯。
- 阶段验收：阶段覆盖的 AGV 用例从 `gap`/`future` 升级为 `current`，且全部既有 `current` 不退化。
- 总体达成：49 个用例全部 `current` 且互不退化，即愿景在可验收意义上落地。

## 迭代日志

| 日期 | AGV | 动作 | 结果 | 状态变更 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 2026-06-11 | — | 建立本执行计划 | 计划成文并入索引 | — | `docs/engineering/AgentGov目标达成分阶段执行计划.md` |
| 2026-06-11 | AGV-001 | 阶段0固本：定位口径建自动回归 | 通过 | `current` 已自动化 | `tests/test_agv_acceptance.py::test_agv_001_governance_platform_positioning` |
| 2026-06-11 | AGV-003 | 阶段0固本：前端边界建自动回归 | 通过 | `current` 已自动化 | `tests/test_agv_acceptance.py::test_agv_003_048_frontend_is_debug_observation_boundary` |
| 2026-06-11 | AGV-046 | 阶段0固本：安全运营可替换建自动回归 | 通过 | `current` 已自动化 | `tests/test_agv_acceptance.py::test_agv_046_security_ops_is_replaceable_example_scenario` |
| 2026-06-11 | AGV-048 | 阶段0固本：前端非生产控制台建自动回归 | 通过 | `current` 已自动化 | `tests/test_agv_acceptance.py::test_agv_003_048_frontend_is_debug_observation_boundary` |
| 2026-06-11 | AGV-040 | 阶段0固本：示例凭据边界绑定既有回归（不新增冗测） | 通过 | `current` 部分自动化 | `tests/test_repository_env_policy.py::test_official_env_examples_do_not_ship_configured_model_provider_key` |
| 2026-06-11 | AGV-042 | 阶段0固本：敏感信息示例边界绑定既有回归（运行时脱敏/拒绝凭据由运行时测试覆盖） | 通过 | `current` 部分自动化 | `tests/test_repository_env_policy.py::test_official_env_examples_do_not_ship_configured_model_provider_key` |
| 2026-06-11 | AGV-002/007/008/011/014/015/016/019/030/033/035/036/038/039/043 | 阶段0固本批量：15 个 current 用例绑定既有回归（10 full、5 partial） | 通过（12 既有 nodeid 全绿） | current 已锚定 | 见用例文档各 `自动验收` 绑定 |
| 2026-06-11 | AGV-018 | 阶段0固本：main agent 样板不变量新增文档测试 | 通过 | `current` 已自动化 | `tests/test_agv_acceptance.py::test_agv_018_main_agent_is_sample_not_long_term_boundary` |
| 2026-06-11 | AGV-005 | 阶段1：把业务/治理 Agent 隐含区分固化为显式 category（单一真相来源）+ 权限边界断言 | 通过 | `gap` → `current` | `app/runtime/agent_profiles.py`、`tests/test_agent_profiles_category.py`（4 测试） |
| 2026-06-11 | AGV-004/024/028 | 阶段1：剩余 gap 多阻塞于多 Agent 基座，先做基座设计（preflight + 最小架构增量 B1-B5） | 设计成文待评审 | 无（设计，零产品代码） | `docs/多业务Agent治理基座设计.md` |
| 2026-06-11 | AGV-004/022 | 基座 B1：持久化业务 Agent 身份注册表（model + 迁移0007 + store + 幂等 sync），main-agent 种子 | 通过 | AGV-004 基座就绪（仍 gap，待创建入口 B4） | `app/runtime/agent_registry_db.py`、`app/runtime/stores/agent_registry_store.py`、`tests/test_agent_registry_store.py`（3 测试） |
| 2026-06-11 | AGV-004/022 | 基座 B1 接入：应用 lifespan 幂等 seed 业务 Agent 注册表，使其在运行态被真实消费（非仅测试） | 通过 | 基座转为运行态真实消费 | `app/main.py`、`tests/test_agent_registry_store.py::test_lifespan_seeds_business_agent_registry` |
| 2026-06-12 | AGV-024 | 基座 B2 slice1：feedback_signals 增后端派生 agent_id（默认活跃业务 Agent），数据层归属；契约安全不改公开 schema/前端 | 通过 | AGV-024 数据层归属就绪（仍 gap，待 version/场景 路由与公开暴露） | `app/runtime/runtime_db.py`、迁移0008、`app/runtime/stores/feedback_source_store.py`、`tests/test_feedback_store_sources.py::test_create_signal_records_business_agent_attribution` |
| 2026-06-12 | AGV-024 | B2 slice2：agent_id 暴露于 FeedbackSignalRecord/Response + 前端 api.ts 同步重生成，反馈归属可查 | 通过 | AGV-024 归属可查 | `app/runtime/records/source_records.py`、`app/runtime/schemas.py`、`frontend/src/types/api.ts` |
| 2026-06-12 | AGV-004/007 | B4 slice：GET /api/agent-registry 暴露注册业务 Agent，注册表获外部消费者；解决与 catalog /api/agents 路径冲突 | 通过 | AGV-004 定义可查（仍 gap，待创建写入入口）；AGV-007 外部接入推进 | `app/routers/agents.py`、`app/runtime/schemas.py::AgentSummaryResponse`、`app/main.py`、`tests/test_agent_registry_store.py::test_list_agents_endpoint_returns_registered_business_agents` |
| 2026-06-12 | AGV-004 | B4 create：POST /api/agent-registry 注册业务 Agent，创建即得稳定身份与归属对象；重复 409、空名 400、省略 id 自动生成；不泄露密钥 | 通过 | AGV-004 创建能力就绪（仍 gap，运行态需动态 profile） | `app/routers/agents.py`、`app/runtime/stores/agent_registry_store.py::create_business_agent`、`app/runtime/schemas.py::AgentCreateRequest`、`tests/test_agent_registry_store.py`（+2） |
| 2026-06-12 | AGV-004 | B4 运行态原语：build_business_agent_profile 为任意注册业务 Agent 动态构造 profile（role=business-agent，被治理对象权限边界）；新增 business-agent 角色 | 通过 | AGV-004 运行态原语就绪（待接入 chat 运行时） | `app/runtime/agent_profiles.py::build_business_agent_profile`、`tests/test_agent_profiles_category.py::test_build_business_agent_profile_is_governed_business_object` |
| 2026-06-12 | AGV-004 | B4 运行态：创建即幂等初始化业务 Agent workspace 与起始 CLAUDE.md（保留用户编辑），为运行提供配置容器 | 通过 | AGV-004 配置容器就绪（运行执行仍待 chat 运行时接入，需 live SDK） | `app/runtime/business_agent_workspace.py`、`app/routers/agents.py`、`tests/test_agent_registry_store.py`（+2） |
| 2026-06-12 | AGV-041 | 高风险审批门：request_change_set_approval 标记待审批（记操作人/原因/影响/回滚），pending_approval 不可直接发布，approve 后方可；reject/abandon 有审计事件 | 通过 | `gap` → `current` | `app/services/agent_governance.py`、`app/runtime/state_machines.py`、`tests/test_agent_governance_publish.py`（+2） |
| 2026-06-12 | AGV-037/047 | 外部系统/职责边界：路由面无用户/角色/租户/权限/生产所有权端点（不复制信息架构），治理面有审批门+审计事件+运行记录，产品边界文档明确职责 | 通过 | 双双 `gap` → `current` | `tests/test_agv_acceptance.py::test_agv_037_047_governance_scope_not_business_ownership` |
| 2026-06-12 | AGV-049 | 将 Multica 等外部协作平台对接定位为长期生态集成，排在三个产品大版本和核心治理能力稳定之后 | 文档边界成文 | 新增 `future` 用例 | `docs/项目目标愿景使命.md`、`docs/AgentGov核心功能测试用例.md`、`README.md` |
