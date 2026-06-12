# 多业务 Agent 治理基座设计

本文档为 AgentGov 从「单一 main agent + 5 个固定治理 Agent」演进到「多业务 Agent 治理」给出最小可落地基座设计，作为 AGV-004、AGV-024、AGV-028 等 `gap` 与 Phase 2 `future` 用例的共同前置。它是架构方案，不是完成承诺；落地按目标达成分阶段执行计划的迭代闭环逐步推进。

## 治理对象预检

| 维度 | 结论 |
| --- | --- |
| 被治理对象 | 业务 Agent（当前仅 main-agent，目标是可注册多个），不含治理 Agent |
| 治理执行者 | 后端注册表 + 确定性校验；治理 Agent 仍只产出建议经投影 |
| 资产类型 | 新增执行资产（Agent 定义/身份）与数据资产（Agent 注册记录、归属关系） |
| 生命周期 | 业务 Agent 引入 draft/active/evaluating/deprecated/archived（AGV-020/021） |
| 反馈归属 | feedback、job、change set、release、eval 增加 `agent_id` 归属（AGV-024） |
| 当前实现边界 | `agent_profiles.py` 硬编码 6 角色；change set/release 无 `agent_id`，隐含 main-agent；无创建入口、无注册表 |
| 目标能力边界 | 可注册/配置业务 Agent，闭环对象从单 main-agent 扩展到任意已注册业务 Agent |

闭环链路（基座要打通的是「对象」与「归属」两环，其余沿用现有闭环）：

```text
注册业务 Agent(对象) -> 运行 -> 反馈(归属 agent_id) -> 归因 -> 优化 -> 评估 -> 版本(按 agent_id) -> Registry
```

风险自检：main-agent 是首个被注册的业务 Agent 样板，不是长期边界；业务 Agent（被治理）与治理 Agent（执行者，AGV-005 已显式 `category`）不混淆；基座先打通身份与归属，不一次性实现全部多 Agent 能力。

## 当前实现边界（代码事实）

- `app/runtime/agent_profiles.py`：`AgentRole` 是 6 元 `Literal`，`build_profiles()` 硬编码返回 6 个 profile；无动态创建。AGV-005 已加 `category`（business/governance）显式区分。
- 无创建/配置入口：`app/routers/`、`app/services/` 无 create-agent route/service/store。
- 版本治理：`AgentChangeSetModel`、`AgentReleaseModel`（`app/runtime/runtime_db.py`）无 `agent_id`，`agent_governance.py` 多处硬指 main agent workspace。
- 配置项：system prompt 为预设 `claude_code`；模型/max_turns 为 profile 常量或 main-agent 请求级覆盖；skills/tools/MCP 为 workspace 级文件。

## 最小基座增量（向后兼容）

目标是引入「业务 Agent 身份」这一最小抽象，把 main-agent 表达为「首个已注册业务 Agent」，使后续按 `agent_id` 扩展成为加法而非重写。

1. **Agent 身份与注册表（解锁 AGV-004 身份部分、AGV-022 雏形）**
   - 新增业务 Agent 注册记录：`agent_id`、`name`、`category`（复用 AGV-005）、`status`（生命周期）、`workspace_dir`、`created_by`、`created_at`。
   - 单一真相来源：注册表为业务 Agent 的权威集合；main-agent 作为内置首条记录种子化，保证现有流程零行为变更。
   - 暂不实现任意 system prompt/模型按实例配置（留作 AGV-004 后续增量），先保证「稳定身份 + 归属对象」。

2. **`agent_id` 归属贯通（解锁 AGV-024、AGV-028 完整性）**
   - feedback、agent job、change set、release、eval run 增加可空 `agent_id`，默认回填 main-agent，保证历史数据与现有单 Agent 流程不破坏。
   - 反馈路由：每条反馈可归属到 `agent_id` + version + run/session/task + 场景；缺失时回退 main-agent 并记录，不串扰其他 Agent。

3. **按 Agent 的版本治理（解锁 AGV-016 多 Agent、AGV-036）**
   - change set/release 以 `agent_id` 维度组织；main-agent 现有版本链作为该 agent_id 的链，其余业务 Agent 各自独立链。

4. **创建入口（AGV-004 实现部分，需产品化翻译门）**
   - 新增「注册业务 Agent」动作前，先做用户旅程：谁创建、最短路径、必须决策点；优先复用已有确认/编辑/版本动作表达，不直接堆按钮。
   - 配置不泄露 API key、MCP header、本机私有路径（AGV-004 成功标准③，沿用 runtime-env-governance 边界）。

## 分步落地（映射 AGV 用例）

| 步骤 | 内容 | 推进用例 | 风险/门槛 |
| --- | --- | --- | --- |
| B1 | 业务 Agent 注册表 + main-agent 种子（只读身份） | AGV-004(身份)、AGV-022(雏形) | schema 新增，需迁移与回填测试 |
| B2 | `agent_id` 贯通 feedback/job/version（默认 main） | AGV-024、AGV-028 | 跨 store 字段加法 + 回填，幂等与回滚测试 |
| B3 | 按 agent_id 的版本链 | AGV-016(多)、AGV-036 | change set/release 维度扩展 |
| B4 | 创建/配置入口 + 用户旅程 | AGV-004(完整) | 产品化翻译门，需用户可见入口评审 |
| B5 | 生命周期状态机 | AGV-020、AGV-021 | 集中状态机 + 非法转移测试 |

每步独立成迭代：先 preflight、再最小实现、过 `make test` 与治理硬门、绑定/升级 AGV 状态、回写迭代日志。B1–B3 为向后兼容加法，风险可控；B4 涉及用户可见入口，落地前需按产品化翻译门确认。

> 进度（2026-06-12）：B1（注册表）、B2（feedback/run agent_id 贯通）、B4（创建/配置/运行入口 + 删除影响面）、B5（生命周期状态机，AGV-020）已落地；场景包/去重（AGV-026/023）作为独立子系统已落地。B3 keystone 推进中：B3.1（change set/release agent_id 维度）、B3.2（per-agent 版本 store 工厂）、B3.3（按 agent_id 选 store 落候选/发布/回滚，与 main 物理隔离）、B3.4-eval（eval run agent_id 归属，四维一致）已落地并过 `make test`，AGV-021 已关闭；剩余 B3.4-exec（task agent_id 传播 + 执行流水线 per-agent store 选择，耦合一次性落地，蓝图见下）→ B3.5 关闭 AGV-010/017/027/031/044。

## B3 详细架构（per-agent 版本治理，最终 keystone）

只读架构核查结论：`GitAgentVersionStore` 当前是 **main-agent-rooted 单实例**（仓库根在 main workspace），`agent_governance` 流程隐式用它、未按 agent_id 参数化；执行优化（execution-optimizer）改的是 main workspace。因此业务 Agent 的反馈虽已 agent-scoped（误路由防护 + agent_id 归属），其优化产出的 change set/release 仍落在 main agent。B3 即把版本治理改为 **多租户 per-agent**。

核心架构决策：

- **多租户版本 store 工厂**：把单实例 `GitAgentVersionStore` 改为按 agent_id 取/建实例——main 沿用现有仓库；业务 Agent 在其 workspace 下（`data_dir/business-agents/{agent_id}/version/{repo,worktrees,releases}`）各自 git 版本链。main 行为不变（隔离降风险）。
- **change set/release 加 agent_id 维度**：additive 可空列（迁移，默认 `main-agent`）+ 索引，按 agent_id 选对应版本 store 与查询过滤。
- **优化流水线 agent 参数化**：业务 Agent 的反馈批次（已 agent-scoped）→ 归因/方案/执行作用于该 Agent 的 workspace + 版本 store，产出其 change set/release。
- **审计/版本边界**：每个 Agent 的 change set/release/version 链独立，rollback 不物理删历史。

增量切片（每片过 `make test` + 治理硬门；中间态对业务 Agent 为空版本链=queryable-but-empty，B3.3 起非空）：

1. **B3.1**（已落地）change set/release 加可空 `agent_id`（迁移默认 main）+ 按 agent 查询/过滤。
2. **B3.2**（已落地）per-agent 版本 store 工厂 `_store_for(agent_id)` + 业务 Agent 版本 store 懒初始化（根 `data_dir/business-agents/{agent_id}/version`），main-agent 复用主 store 行为不变；agent_id 路径段做安全字符校验防穿越。
3. **B3.3**（已落地）`create_change_set(agent_id=...)` 按目标 Agent 选版本 store 落 worktree/候选/发布/回滚/恢复，产出该 Agent 独立 change set/release（闭环非空，与 main 物理隔离）。
   - **B3.4-eval（已落地）** eval run 加 `agent_id`（迁移 0012），归属随其 change set 继承、无锚点回退 main、按 Agent 过滤；至此 run/feedback/eval/version 四维 agent_id 一致。
4. **B3.4-exec（执行就绪蓝图，待 deliberate 一次性落地）** 把 main-centric 优化执行流水线参数化为 per-agent。两部分耦合（task agent_id 是执行重构可测的前提，单做 task agent_id 会让业务执行落到 main store 报错=latent break），必须同片落地：
   - **归属传播链**：`signal.agent_id`（已 B2）→ 按 `feedback_case_id` 查信号派生 → `OptimizationTask.agent_id`（新增列，迁移 0013，在 `create_task`/`create_task_from_optimization_plan` 落、`list_tasks` 加过滤、投影）→ `create_change_set(optimization_task_id)` 已从 task 派生 agent_id（B3.3 现成）→ `change_set.agent_id` → eval 继承（已落地）。
   - **执行流水线 store 选择**：`ExecutionApplicationService` 由 task/change_set 的 agent_id 经 `agent_governance._store_for(agent_id)` 取 per-agent store，替换以下对 `self.agent_version_store` 的直用——单任务路径 `_prepare_candidate_change_set`（repository_status/current_version_id/version_summary）、`_commit_candidate_and_record_application`（commit_worktree/version_summary/diff_versions）；批次路径 `_batch_execution_baseline`（repository_status/current_version_id）、`_prepare_batch_candidate_change_set`（version_summary）、`_commit_batch_candidate_and_record_applications`（commit_worktree/version_summary/diff_versions）。main-agent → `_store_for` 返回主 store，主路径字节级不变（live 批次闭环测试不动）。
   - **安全建序**：task agent_id + 执行 store 选择同片改 → 加业务 Agent 执行端到端测试（业务反馈→task 带 agent_id→候选落业务 store→与 main 隔离）→ `make test` 全绿才推；中途不推半成品（避免 critical 执行路径残缺态）。
   - 版本图/release archive 列表按 Agent 维度（list_releases 已支持 agent_id 过滤，版本图按需补 agent_id 入参）。
5. **B3.5** 据此关闭 AGV-017/027/031/010/044（AGV-021 已由 B3.2/3.3 关闭；各自成功标准 + per-agent 版本/审计边界）。

风险与门槛：critical 数据版本子系统——以 additive 迁移 + per-agent store 隔离（不改 main 路径）+ 每片 make test + 版本历史不物理删除控制；B3.3 是把 main-centric 执行流水线参数化的核心难点，需深读 `agent_git_store`/`agent_governance`/execution 路径后再动。建议作为带 preflight 的 deliberate effort 一次性推进 B3.1→B3.4，避免中间 inert 态长期滞留。

## 不在本设计范围

- 任意业务 Agent 的 system prompt/模型/skill 全自定义运行时（AGV-004 高级配置，后续增量）。
- 场景包与跨 Agent 方法论沉淀（AGV-026/027/010，Phase 3/4）。
- 自动应用高风险变更（AGV-041 审批，独立设计）。

## 验收

- 本设计：进入 `docs/README.md`，通过文档治理硬门，可从用例文档与执行计划追溯。
- 落地验收：B1–B5 各步对应 AGV 用例从 `gap`/`future` 升级为 `current`，且既有 `current` 不退化。
