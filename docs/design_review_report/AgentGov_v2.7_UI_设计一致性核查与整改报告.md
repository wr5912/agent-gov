# AgentGov v2.7 方案与目标愿景使命一致性核查与整改报告

> 核查日期：2026-06-19
> 分支：`feat/agentgov-cross-gen-v2.7`
> 对照方案：`docs/AgentGov_ASCII_UI_草图方案_v2.7.md`
> 长期权威：`docs/项目目标愿景使命.md`
> 核查原则：以代码、测试、真实 Playwright 浏览器和可复现命令为准；移动端不作为本轮目标偏差和整改优先级。

## 1. 核查口径

本轮不把移动端响应式作为核心偏差。重点只看三个问题：

1. 实现是否偏离 AgentGov 的系统定位、目标、愿景和使命。
2. 实现是否偏离 v2.7 方案中的治理对象、W1/W2/W3 能力边界和闭环链路。
3. 测试是否真实证明上述目标，而不是只证明局部 UI 可点击。

治理链路按以下对象审计：

```text
业务 Agent -> 运行 -> 反馈 -> 归因 -> 优化 -> 执行 -> 回归 -> 发布 -> 资产 Registry
```

## 2. 整改后总体结论

桌面端 v2.7 主体验已经从“UI 外壳接近方案”推进到“关键治理证据链可验证”：

- W1：ImprovementItem 实体、7 段状态机、Agent scoping、治理工作台外壳继续成立。
- W2：ContextPackage、反馈归属、中文相似归并、后端归因/方案首切生成、发布页真实动作已加固。
- W3：Asset Registry 已展示 provenance，并进入 UI 主流程硬门。

仍不能宣称 v2.7 目标能力完全达成的边界：

- 后端归因/方案生成已从浏览器迁出，但仍是后端首切生成，不等价于 LLM/Governor 深度分析。
- ImprovementItem 与旧 feedback batch 闭环引擎的归因、执行、回归、版本能力尚未完全合并。
- 执行记录到真实 change set / release / agent version 的权威绑定仍需继续增强。

## 3. 目标愿景使命对照

| 长期要求 | 整改后事实 | 剩余偏差 |
| --- | --- | --- |
| AgentGov 是通用智能体治理平台，不是单点 Runtime 封装。 | 业务 Agent 顶栏选择、ImprovementItem scoping、版本治理、资产 Registry 均按 Agent 归属运转。 | 旧反馈优化 workspace 仍作为 Developer/诊断入口存在，新旧闭环能力还未完全收口。 |
| 把反馈、经验、方法和版本演进沉淀为可复用治理体系。 | ContextPackage 现在包含系统理解、归因、证据、来源反馈、Trace run/session、Agent version、links 和 assets；Asset Registry 显示 provenance。 | 执行记录与真实 change set / release / agent version 的闭环绑定仍需深化。 |
| Runtime 是事实层，Feedback Loop 是经验转化层，Version Governance 是治理固化层。 | ReleaseWorkbench 的“去运行回归”调用真实 change set regression API；“强制发布”有二次确认、后端 `force` 参数和 `force_published` 审计事件。 | 发布门禁仍主要按 change set 状态判断，尚未直接以 ImprovementItem 回归阶段作为权威来源。 |
| main agent 只是样板，长期治理对象是多业务 Agent。 | Feedback 记录新增 `agent_version_id`、`scenario`、`task_id`、`alert_id`、`case_id`；Drawer、来源反馈表、ContextPackage 都可见。 | 仍需把更多 eval/release 资产按 Agent version 做端到端追踪。 |
| 反馈到资产闭环：run -> feedback -> attribution -> optimization -> eval -> release -> Registry。 | v2.7 UI 硬门已覆盖 ContextPackage 证据链、发布动作和 Asset Registry provenance。 | eval/release 到 asset 的完整反查图谱仍是后续增强点。 |

## 4. 关键整改项

### P0 完成口径与硬门

- `docs/AgentGov_ASCII_UI_草图方案_v2.7.md` §17.6 已改为分层事实快照：W1 基本达成，W2/W3 区分“已加固首切”和“未完成深接入”。
- `scripts/verify_v27_ui_design_parity.mjs` real-container 模式会创建 `audit-v27-*` 数据并按 `improvement_id` 精确选择。
- `tests/coverage_policy.json` 已把 v2.7 主流程绑定到 `verify:design-parity`、`verify:asset-registry`、`verify:message-actions-browser`。

### P1 ContextPackage 证据链

- `frontend/src/contextPackage.ts` 已接入 NormalizedFeedback、Attribution、ImprovementFeedback、OptimizationPlan、ExecutionRecord、Asset 和 links。
- JSON 上下文不再输出 `attribution: null`、`evidence: []` 这种误导性空结构；缺数据时输出 `missing_reasons`。
- Playwright 设计硬门断言 JSON 中存在 `attribution_id`、`agent_version_id`、`optimization_plan_id`、`asset_id`。

### P2 治理后端生成首切

- `POST /api/improvements/{id}/attribution/generate`：后端生成初版归因并持久化。
- `POST /api/improvements/{id}/optimization-plan/generate`：后端生成初版优化方案并持久化。
- 前端 `ImprovementWorkbench` 已改为调用后端 generation endpoint，不再在浏览器里拼接归因和方案。

边界声明：这一步只是把职责从前端迁回治理后端，尚不等价于 LLM/Governor 深度归因。

### P3 多 Agent 归属与相似归并

- `improvement_feedbacks` 增加 `agent_version_id`、`scenario`、`task_id`、`alert_id`、`case_id`，并补 `0014_improvement_feedback_context` migration。
- Feedback Drawer 自动带入 Agent version、场景、run/session、alert/case。
- 相似归并算法加入中文 uni/bi/tri-gram 和短查询覆盖率，补无共享 feedback ref 的中文长文本测试。

### P4 发布门禁台

- ReleaseWorkbench 调用 `runAgentChangeSetRegression`，不再把“去运行回归”做成刷新。
- `AgentChangeSetPublishRequest` 增加 `force`；后端仅在 `force=True` 且状态允许时绕过失败回归门禁，并写入 `force_published`、`force_publication_blocker`、`force_publish_note`。
- 发布页“强制发布”需二次点击确认，并显示结果或错误。

### P5 Asset Registry provenance

- Asset Registry 每条资产显示归属 Agent、来源改进事项、继承来源。
- `scripts/verify_asset_registry.mjs` 已覆盖 `asset_create`、`asset_inherit`、`asset_provenance`。

## 5. 测试与验证

已执行并通过：

```bash
.venv/bin/python -m pytest -q tests/test_improvement_content.py tests/test_improvement_merge.py tests/test_agent_governance_publish.py tests/test_asset_registry.py tests/test_assets_api.py
# 28 passed

pnpm --dir frontend build

pnpm --dir frontend run verify:design-parity
# DESIGN_PARITY 18/19 passed (mock); baseline 18/18 held

pnpm --dir frontend run verify:asset-registry
# asset_create / asset_inherit / asset_provenance passed

pnpm --dir frontend run verify:message-actions-browser
# message actions passed

make main-flow-test
# pytest=23 ui=4 passed

make test
# 566 passed, 2 skipped; coverage policy OK: line=76.84% branch=59.20% main_flow_pytest=23 main_flow_ui=4

make container-live-test
# docker compose --env-file docker/.env ... tests/test_live_runtime_acceptance.py
# 2 passed
```

说明：`verify:design-parity` 中 `message-actions` 单项仍不是该脚本的 baseline 项，因为消息动作由独立脚本 `verify:message-actions-browser` 负责生成/注入 assistant message 后验证。

## 6. 剩余风险

1. Governor/LLM 深度归因尚未接入 ImprovementItem 主链路；当前是后端首切生成。
2. 旧 feedback batch 闭环能力尚未完全迁移到 ImprovementItem，短期仍需保留 Developer/诊断入口。
3. 执行记录与真实发布版本之间还需要更强的 change set / release 反查关系。
4. 移动端不在本轮目标偏差范围内，后续如要进入交付范围需另建验收标准。
