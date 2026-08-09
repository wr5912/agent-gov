# Roadmap: AgentGov

## Overview

v3.1“业务智能体测评与平台治理演进”从前序 Phase 5 之后继续编号，以 P0 双门建立可信准入基线，再交付 P1 独立发布测评纵向闭环、P2A Runtime 内部边界和 P2B Governor shadow 学习证据；P3 只按真实证据启用平台基础与独立扩展，最后用当前工作树真实容器、浏览器、live Agent、历史数据、离线与 AGV 证据完成审计。路线图不把 P0-MCP 当作安全领域成绩，不把 P1 首切片称为完整 MVP，也不把 P2B shadow outcome 当作线上激活。

**Granularity calibration:** Fine。仓库当前没有 `.planning/config.json`，因此本轮不写入或推断持久化配置；10 个阶段来自方案中的自然可验证边界与用户指定依赖，而非为满足数量而拆分。

## Milestones

- ✅ **v3.0.3 Agent 版本治理与 Diff 对比重构** — Phases 0-5；摘要见 `.planning/MILESTONES.md`。
- 🚧 **v3.1 业务智能体测评与平台治理演进** — Phases 6-15；in progress（Phase 6 complete）。

## v3.1 业务智能体测评与平台治理演进（In Progress）

**Milestone Goal:** 以独立、隔离、可回滚的真实证据串联业务 Agent 测试、发布测评、Runtime 边界、Governor shadow 学习与平台扩展准入。

## Phases

**Phase numbering:** 前序 Phase 0-5 已完成并移入里程碑摘要；本里程碑从 6 连续编号，不重置。

- [x] **Phase 6: P0-W1 安全 Workspace 基线修复** - 让精确 commit 的完整安全 Workspace suite 在当前权威配置下零未分类失败。 (completed 2026-08-09)
- [ ] **Phase 7: per-Agent 最小权限隔离测试 lane** - 让平台在无 root、无继承 secret、无可写 live data 的环境中执行 exact-commit suite 并形成发布门回执。
- [ ] **Phase 8: P0-MCP 精确两工具回执** - 以固定上游、过滤 OpenAPI、真实 Claude Runtime 和强制清理证明窄 capability tuple。
- [ ] **Phase 9: P0 状态语义与双门准入收口** - 冻结 run/session/facts 语义并以 Workspace 与 MCP 两个独立门关闭 P0。
- [ ] **Phase 10: P1 独立测评领域与 API** - 建立 evaluator-owned 协议、typed execution/assessment/comparison、确定性安全门和 finding 关系。
- [ ] **Phase 11: P1 单 Agent 测评 UI 与发布闭环** - 用户可从业务 Agent 详情完成发布测评、纳入四阶段改进、候选复测与精确 Release。
- [ ] **Phase 12: P2A Runtime 边界与 Claude Adapter** - 所有生产 Claude 调用经 backend binding、gateway 和委托 adapter，且由真实第二协议证伪边界。
- [ ] **Phase 13: P2B Governor Shadow 学习证据** - 形成不可变 evidence、exact capability build、窄 scope 与盲化 outcome，保持线上静态行为不变。
- [ ] **Phase 14: P3 平台基础与扩展组合准入** - 以 EvalOps、资产、scope、数据、集成、SLO、OnlineOutcome 与回滚门独立批准扩展线。
- [ ] **Phase 15: 真实 E2E 与完成审计** - 在同一完成候选上完成容器、浏览器、live Agent、历史数据、安全、离线、指标和 AGV 审计。

## Dependency Graph

```text
Phase 6 -> Phase 7 -> Phase 8 -> Phase 9
                                      |-> Phase 10 -> Phase 11 -|
                                      |-> Phase 12 ------------|-> Phase 13 -> Phase 14 -> Phase 15
```

Phase 10-11（P1）与 Phase 12（P2A）在 Phase 9 退出后具备并行前置条件；默认仍按编号交接，若使用独立 workstream 并行，Phase 13 退出前必须汇合 P1 真实闭环与 P2A gateway 等价证据。

## Phase Details

### Phase 6: P0-W1 安全 Workspace 基线修复

**Goal**: 安全业务 Agent 的完整 Workspace 自测在当前权威身份、权限和路径契约下可信全绿，为后续隔离 lane 锁定精确 commit。
**Depends on**: Phase 5（前序里程碑已完成）
**Requirements**: P0W-01, P0W-02, P0W-03, P0W-04, P0W-05, P0W-06, P0W-07, P0W-08
**Success Criteria** (what must be TRUE):
  1. 平台维护者可在锁定 commit 上运行完整 Workspace suite，看到当前快照全部通过且没有删除安全断言或固定平台 leaf 数量。
  2. 危险 Bash、非 JSON、错误顶层类型和缺少 command 的输入全部结构化 deny，任何输入都不产生未处理异常或静默放行。
  3. 审计输出只落到批准的 runtime data 路径，原生配置/身份测试与当前 Workspace 权威一致。
  4. `runtime-bootstrap-scan` 证明初始化源安全，live Workspace、版本目录、私有 env 和 runtime DB 没有被修改。
**Plans**: 1/1 plans complete

### Phase 7: per-Agent 最小权限隔离测试 lane

**Goal**: 平台可在不信任业务 Agent 测试代码的前提下，隔离执行任意 Agent 精确 commit 的完整 suite，并让回执成为可靠发布条件。
**Depends on**: Phase 6
**Requirements**: LANE-01, LANE-02, LANE-03, LANE-04, LANE-05, LANE-06, LANE-07, LANE-08
**Success Criteria** (what must be TRUE):
  1. 平台维护者只选择 Agent 与精确 commit，即可运行固定 pytest 命令并获得完整 suite 结果，无需把该 Agent 的 leaf 复制进平台根 collection。
  2. 回执证明执行进程非 root、无继承 secret、无可写 live `/data`/runtime root、无宿主机控制能力，并且 hostile 测试无法越出 Workspace。
  3. 每次运行都有可复现的 commit/suite/image/invocation/isolation/cleanup 摘要，敏感值不进入回执。
  4. 发布门拒绝历史或 digest 错配的通过记录；缺失 Docker/镜像/隔离前置时严格失败且不遗留临时资产。
**Plans**: 0/3 plans complete

### Phase 8: P0-MCP 精确两工具回执

**Goal**: 平台维护者可重复证明 AgentGov 在精确 `claude-code / streamable-http / tools / GET-read-only / no-auth-fixture` 组合下完成真实 MCP 工具闭环。
**Depends on**: Phase 7
**Requirements**: MCP-01, MCP-02, MCP-03, MCP-04, MCP-05, MCP-06, MCP-07, MCP-08
**Success Criteria** (what must be TRUE):
  1. 直接协议 `tools/list` 只返回 alerts/assets 两个 GET tool，两个调用返回固定 seed 的稳定合成标记，resources/templates 与其他工具为空。
  2. 固定 Agent commit 的真实 Claude run 调用两个精确工具和参数，`agent_activity`、tool result 与最终回答可相互核对且无 Bash、写入或虚构结果。
  3. 机器回执绑定所有 source/image/OpenAPI/Agent/suite/run/session/trace 证据，明确未覆盖 GAP 和 no-auth 边界，不含 token/header/env。
  4. 公共容器入口从当前工作树重建；成功、失败或中断都清理独立 Compose project、网络、卷和临时根，任一 cleanup 失败都使回执失败。
**Plans**: TBD

### Phase 9: P0 状态语义与双门准入收口

**Goal**: 后续评测、Runtime 与 Governor 工作共享唯一 run/session/facts 语义，并只能在 P0 两个独立阻断门真实通过后开始。
**Depends on**: Phase 8
**Requirements**: ADM-01, ADM-02, ADM-03, ADM-04, ADM-05, ADM-06, ADM-07, ADM-08
**Success Criteria** (what must be TRUE):
  1. 维护者可验证所有 run 终态不可重开；恢复同一原生 session 会产生带前序血缘的新 run。
  2. 原生事实、canonical 投影与边界展示可区分，无法无损映射的字段仍能通过 raw payload 和 coverage 复盘。
  3. 客户端无法选择或覆盖 Runtime，当前部署解析唯一 backend binding，同时保留长期 BusinessAgentVersion binding seam。
  4. P0 退出报告分别列出 Workspace 与 P0-MCP 门、质量 owner、AGV 锚点和全量验证；任一门失败、local-debug 替代或真实卷/env 改写都会阻断。
**Plans**: TBD

### Phase 10: P1 独立测评领域与 API

**Goal**: 平台可对精确业务 Agent 版本执行独立、隐藏、可重复的发布测评，并生成不可伪造的 typed 结论、比较、finding 和发布门事实。
**Depends on**: Phase 9
**Requirements**: EVAL-01, EVAL-02, EVAL-03, EVAL-04, EVAL-05, EVAL-06, EVAL-07, EVAL-08, EVAL-09, EVAL-10, EVAL-11, EVAL-12, EVAL-13, EVAL-14
**Success Criteria** (what must be TRUE):
  1. 评测方可用仓库外精确 Git revision 冻结 benchmark/protocol 与 8-case holdout，候选 Agent、Workspace、Trace 和 API 都不能读取隐藏正文或改写规则。
  2. baseline/candidate 各 3 次独立执行形成 typed execution/assessment；同输出三次评分一致，安全否决独立于综合分。
  3. 只有协议、Runtime、模型、工具、环境和采样指纹兼容时产生比较；不兼容结果明确为 `incomparable` 并要求成对重跑。
  4. execution 聚合 API、sample ref、OpenAPI 与生成类型来自单一 typed 契约，历史普通报告仍可读取且不被伪造为评测结果。
  5. 用户选择的合法 findings 可幂等、事务性地创建或关联 ImprovementItem；发布门拒绝旧 suite、旧协议、不可比、critical 退化或 safety veto。
**Plans**: TBD

### Phase 11: P1 单 Agent 测评 UI 与发布闭环

**Goal**: 用户可在业务 Agent 上下文中看懂发布测评证据，将真实失败纳入四阶段改进，并只在完整回归与同协议测评通过后发布。
**Depends on**: Phase 10
**Requirements**: EUI-01, EUI-02, EUI-03, EUI-04, EUI-05, EUI-06, EUI-07, EUI-08, EUI-09
**Success Criteria** (what must be TRUE):
  1. 用户从“业务 Agent 详情 → 测评”看到协议、分项、版本比较、安全门和 finding，并可深链到 sample/Trace，而不是在测试资产页看到无上下文总分。
  2. 页面空态、成功、评分失败、安全否决、不可比、基础设施重试和多事项关联均可在真实浏览器验证，隐藏集正文始终不可见。
  3. Workspace 回归、平台发布测评和“纳入改进治理”分别执行真实业务动作；新事项从反馈整理开始，不自动推进四阶段。
  4. 一条真实失败可完整产生改进、candidate、Workspace passed、同协议 candidate assessment 与 Release；旧历史证据无法越过门禁。
  5. 当前工作树的真实容器 8-case baseline/candidate 与前端 build/browser smoke 通过，P0-MCP 不参与评分，未实现的 OnlineOutcome 只显示为关系 seam 而非虚假结果。
**Plans**: TBD
**UI hint**: yes

### Phase 12: P2A Runtime 边界与 Claude Adapter

**Goal**: 所有受管 Claude 运行在不改变现有公开契约的前提下经过 backend binding 与候选 Runtime 边界，并由第二类真实协议暴露和移除 Claude-shaped 假设。
**Depends on**: Phase 9（可与 Phases 10-11 独立推进）
**Requirements**: RT-01, RT-02, RT-03, RT-04, RT-05, RT-06, RT-07, RT-08, RT-09, RT-10
**Success Criteria** (what must be TRUE):
  1. 所有生产入口按 BusinessAgentVersion binding 经 registry/gateway 解析唯一 `claude-code` adapter，客户端任何输入都不能选择 Runtime，未知配置在启动前失败。
  2. core 的 session/run/event/provenance/capability 契约无 Claude 专属字段，不支持能力具有 constraints、coverage、evidence 和稳定诊断。
  3. 真实 Codex app server spike 对每个候选端口给出可复用、Claude-shaped、特有或不支持分类，Claude-shaped 假设不再留在 core。
  4. 非流式、SSE、取消/恢复、HITL、subagent、raw event、Trace、Langfuse 与 Governor job 在真实容器中保持 Claude 等价。
  5. DB、`sdk_session_id`、OpenAPI、SSE 和生成类型无 alias/双写/副本，生产调用方除 adapter/composition 外不再直接构造 `ClaudeRuntime`。
**Plans**: TBD

### Phase 13: P2B Governor Shadow 学习证据

**Goal**: 工程评审者可审查一个归因能力候选的完整来源、exact build、窄 scope 和盲化评估结果，同时线上 Governor 行为保持不变。
**Depends on**: Phases 11 and 12
**Requirements**: GOV-01, GOV-02, GOV-03, GOV-04, GOV-05, GOV-06, GOV-07, GOV-08, GOV-09, GOV-10, GOV-11, GOV-12, GOV-13, GOV-14
**Success Criteria** (what must be TRUE):
  1. 每次真实 Governor 产物都保留原始 typed evidence、人工修订 diff 与后续结果，并可反查 exact capability build/method/scope/Runtime provenance。
  2. 一个 `ATTRIBUTION` candidate 被确定性物化为 immutable build，在物理隔离 holdout 上得到可解释的 blind `passed | failed | inconclusive` outcome。
  3. research/candidate/evaluator 不能泄露 holdout、blind mapping 或 backend-owned 字段，hostile 输出无法改写 ID、status、gate 或 principal。
  4. research/evaluator 失败不影响当前四阶段业务输出；只读审计 API 可用，但没有 activation/rollback mutation、权威 binding、`ActivationRecord` 或新增用户流程。
  5. passed outcome 含观察窗和回退契约，retention/tombstone 可验证；真实容器证明 shadow 记录与当前静态 Governor 同时成立且未发生线上切换。
**Plans**: TBD

### Phase 14: P3 平台基础与扩展组合准入

**Goal**: 平台只在身份、资产、评测、数据、运营与回滚证据满足时独立启动扩展，并能把发布后的外部结果纳入观察和回退，而不把框架评审伪装成实现完成。
**Depends on**: Phases 11, 12 and 13；每条扩展线另满足自身上游门
**Requirements**: PLAT-01, PLAT-02, PLAT-03, PLAT-04, PLAT-05, PLAT-06, PLAT-07, PLAT-08, PLAT-09, PLAT-10, PLAT-11, PLAT-12, PLAT-13, PLAT-14
**Success Criteria** (what must be TRUE):
  1. 任一安全、Runtime、Governor、能力包或集成扩展在缺少专属上游、用户任务、owner、scope、迁移、回滚与真实证据时保持未启动；一条线通过不代表其他线完成。
  2. 操作者可沿 Agent/version、benchmark/protocol、finding/improvement、change set/release 与 OnlineOutcome 查看稳定引用和 provenance，Registry 不复制原生正文。
  3. 能力包应用只形成目标 Agent 的待审查 change set，并要求目标 Agent 独立测评；服务端 principal/scope 与职责分离阻止伪造身份或跨 scope 操作。
  4. 数据保留/删除/legal hold、通用集成重放/对账、SLO/容量/成本超限和 hidden-set 隔离都有可审计成功及失败证据。
  5. Release 后的外部 OnlineOutcome 在声明窗口内可触发回滚；完整安全 MVP、公共 Runtime 迁移/第二 Runtime、Governor canary 和 observer/Multica 只有各自条件满足才可启用，AGV 只按实证复核。
**Plans**: TBD
**UI hint**: yes

### Phase 15: 真实 E2E 与完成审计

**Goal**: v3.1 的能力声明全部由同一完成候选上的 fresh 真实证据支持，历史数据、安全、离线、指标、回滚和 AGV 缺口均可复核。
**Depends on**: Phases 6-14
**Requirements**: AUDIT-01, AUDIT-02, AUDIT-03, AUDIT-04, AUDIT-05, AUDIT-06, AUDIT-07, AUDIT-08, AUDIT-09
**Success Criteria** (what must be TRUE):
  1. 审查者可从 fresh source/image/config/commit/digest 回执确认所有适用 lane、容器、浏览器和 live Agent 都来自当前完成候选，而非 local-debug、旧 Trace 或历史 passed run。
  2. 真实浏览器 E2E 可追踪发布测评失败到改进、candidate、复测、Release、OnlineOutcome/观察和必要回滚；条件未满足的扩展明确显示未启动。
  3. fresh 与真实历史 DB 的 migration、列表、详情、sample deep-link 和关键页面无 500/漂移，安全审计无 secret、holdout、跨 scope 或 live-volume 旁路且 teardown 完整。
  4. 核心闭环在离线/内网与本地化 LLM 条件下成立，同一候选的治理、类型、主流程、全量、OpenAPI、生成类型、前端、浏览器和公共容器门全部通过。
  5. 完成审计逐项记录 102/102 requirement、AGV 实际状态、baseline/target/guardrail/window/owner/evidence、SLO/成本与回滚点，并保留前序 Phase 0-5 历史原位。
**Plans**: TBD
**UI hint**: yes

## Progress

**Execution order:** 默认按 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → 14 → 15；Phase 12 在 Phase 9 后可用独立 workstream 与 P1 并行，但 Phase 13 前必须汇合。

| Phase | Milestone | Plans Complete | Status | Completed |
| --- | --- | --- | --- | --- |
| 6. P0-W1 安全 Workspace 基线修复 | v3.1 | 1/1 | Complete | 2026-08-09 |
| 7. per-Agent 最小权限隔离测试 lane | v3.1 | 0/3 | Planned | - |
| 8. P0-MCP 精确两工具回执 | v3.1 | 0/TBD | Not started | - |
| 9. P0 状态语义与双门准入收口 | v3.1 | 0/TBD | Not started | - |
| 10. P1 独立测评领域与 API | v3.1 | 0/TBD | Not started | - |
| 11. P1 单 Agent 测评 UI 与发布闭环 | v3.1 | 0/TBD | Not started | - |
| 12. P2A Runtime 边界与 Claude Adapter | v3.1 | 0/TBD | Not started | - |
| 13. P2B Governor Shadow 学习证据 | v3.1 | 0/TBD | Not started | - |
| 14. P3 平台基础与扩展组合准入 | v3.1 | 0/TBD | Not started | - |
| 15. 真实 E2E 与完成审计 | v3.1 | 0/TBD | Not started | - |
