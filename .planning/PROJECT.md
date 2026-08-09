# AgentGov

## What This Is

AgentGov 是面向通用智能体应用的优化闭环治理平台，以 Agent Runtime、Feedback Loop 和 Version Governance 为核心，把业务 Agent 的真实运行、反馈、归因、优化、测评、发布与回滚沉淀为可追溯的治理证据。平台面向业务 Agent 开发者、评测方和治理操作人员；外部业务系统继续拥有最终用户界面、业务权限与生产审批。

## Core Value

让每个业务 Agent 的能力变化都能从真实运行与失败证据出发，经独立测评和安全门验证后形成可发布、可回滚、可追溯的精确版本。

## Current Milestone: v3.1 业务智能体测评与平台治理演进

**Goal:** 从 P0 安全准入基线出发，交付首条独立发布测评闭环、Runtime 内部边界和 Governor shadow 学习证据，并建立 P3 扩展组合准入与最终真实验收门。

**Target features:**

- 安全 Workspace 在精确 commit 上零未分类失败，并由真正隔离的 per-Agent lane 生成可审计回执。
- P0-MCP 以固定上游、精确两工具、真实 Claude Runtime 和强制清理形成窄 capability 回执。
- 独立 `EvaluationBenchmark` / `EvaluationProtocolRevision` 驱动 8-case 静态发布测评、finding 到四阶段改进、配对比较与精确发布门。
- `RuntimeGateway`、Claude 委托 adapter 与真实第二协议 spike 收口调用边界，不改变现有公开会话契约。
- Governor 形成不可变 evidence、精确 capability build 和盲化 holdout shadow outcome，但不自动启用。
- P3 以 EvalOps、资产关系、principal/scope、数据治理、集成可靠性、SLO/成本和 `OnlineOutcome` 为组合准入维度，未满足门的扩展不伪装成已实施。

## Requirements

### Validated

- ✓ Git-backed change set、candidate worktree、精确 Diff、release/restore 与可恢复回滚主链路已经替代旧 tar snapshot 主流程；旧 `/api/agent-versions/main/*` 已退出活跃契约 — 前序里程碑 Phases 0-5。
- ✓ OpenAPI、前端生成类型和治理工作台已迁移到 Git-backed 版本治理主链；历史版本引用可作只读解释 — 前序里程碑 Phase 5。
- ✓ Docker 宿主机持久化根已统一为 `${HOME}/volume-agent-gov`，旧 `docker/volume/` 只作迁移来源或显式兼容边界 — 前序里程碑 Phase 0-5。
- ✓ Claude SDK/Agent 仍是会话、消息、工具、HITL、subagent 与 Trace 的运行事实源；后端承担 API、证据投影和治理编排 — 当前运行基线。
- ✓ 四阶段改进治理保持“反馈整理 → 归因分析 → 优化执行 → 测试发布”，状态推进只作为真实业务动作副作用 — 当前产品契约。
- ✓ Workspace Git、精确 commit、suite digest 和发布门已有基础骨架 — 当前实现；隔离、安全与独立发布测评仍属于 v3.1 active scope。
- ✓ `security-operations-expert` 初始化源的完整 Workspace suite 在当前精确候选上 `58 passed`；危险/畸形 hook 输入结构化 fail-closed、审计路径受批准 data 根约束，且 bootstrap 扫描通过 — Phase 6。

### Active

- [ ] 把 exact-commit runner 从 API 容器 root、继承 env、可写 `/data` 的现状迁入最小权限隔离 lane。
- [ ] 完成 P0-MCP 精确 capability tuple 的直接协议、真实 AgentGov live 与 cleanup 回执。
- [ ] 冻结一次 run 不重开、同 session 新 run 恢复、原生事实/canonical 投影/边界展示三层语义，并关闭 P0 双门。
- [ ] 建立 evaluator-owned 发布基准、typed 测评领域/API、确定性评分、安全否决、配对比较和 finding 关系。
- [ ] 在“业务 Agent 详情 → 测评”完成发布测评、四阶段改进、候选复测与 Release 的真实纵向闭环。
- [ ] 提取 Runtime core、registry/gateway、Claude 委托 adapter，并用真实 Codex `thread/turn` 协议证伪 Claude-shaped 假设。
- [ ] 建立 Governor append-only evidence、immutable capability build、窄 `ApplicabilityScope` 和盲化 shadow evaluation。
- [ ] 落实 P3 平台基础准入维度与独立扩展启动门，接入 `OnlineOutcome`、观察窗和回滚证据。
- [ ] 通过当前工作树真实容器、浏览器、live Agent、历史数据、安全对抗、离线与 AGV 审计完成 v3.1 收口。

详细原子要求及唯一 Phase 映射见 `.planning/REQUIREMENTS.md`。

### Out of Scope

- 生产 MCP 认证、写操作、客户数据或真实高风险处置 — P0-MCP 只验证 `claude-code / streamable-http / tools / GET-read-only / no-auth-fixture`；真实需求必须进入 P3 独立扩展线。
- 把 P1 8-case 静态切片宣称为完整安全测评 MVP或业务 Agent 总能力分 — 完整 MVP 需要 50+ 版本化案例、至少两名独立专家、动态隔离场景与线上结果。
- 请求级 Runtime selector、`sdk_session_id` 双写迁移或第二生产 Runtime — P2A 只提取内部边界；公共迁移和第二 Runtime 需通过 P3 独立启动门。
- Governor 联网研究、自修改、自动启用、全局传播或无 scope 激活 — P2B 只产出 shadow 证据；激活必须另有可信 principal、人工决定、canary、CAS 与回滚。
- 多组织/多租户产品、外部业务权限和生产审批 — v3.1 采用单组织控制面，外部业务系统继续拥有业务责任。
- Multica 或其他协作平台强依赖 — 只有两个以上真实场景证明通用 API/webhook/只读 observer 不足时才重新评审。
- 因完成规划或阶段自动 bump `VERSION`、创建 tag 或发布 — 发布点仍由用户明确确认。

## Context

- 前序“Agent 版本治理与 Diff 对比重构”里程碑已完成，历史 Phase 0-5 摘要在 `.planning/MILESTONES.md`，原 `.planning/phases/agent-version-governance-diff-refactor/` 按原路径保留。
- 2026-08-05 P0 初始基线：`security-operations-expert` 完整 Workspace suite 为 15 pass / 14 fail；失败分为 6 个危险 Bash、3 个畸形输入、1 个审计 fallback 路径和 4 个原生配置/身份陈测。Phase 6 在 2026-08-09 以两轮修正收口为当前 `58 passed`，实际 leaf 数量只进入阶段回执。
- exact commit、suite digest 与 publish gate 骨架已经存在，但当前执行仍位于 API 容器 root、继承运行 env、可写 `/data`，不能作为安全隔离证据。
- P0-MCP 的 fixture、过滤 spec、runner 和 receipt 尚未实现。已锁定的 `openapi-mcp-server` 与 `mock_service` 服务可用，但共享实例暴露 10 tools、1 resource、1 template，不能替代精确两工具验收。
- P1 的 `EvaluationBenchmark`、`EvaluationProtocolRevision`、`EvaluationExecution`、`Assessment` 等领域/API/UI 基本未实现。
- `ClaudeRuntime`、流式 Runtime 和 Governor 中心服务接近 800 行阈值；v3.1 新职责必须进入独立子域，不能继续向中心文件堆叠分支。
- 下一阶段实施权威为 `docs/AgentGov下一阶段实施方案索引.md` 及其 P0、P0-MCP、P1、P2A、P2B、P3 工程方案；长期产品与术语仍以目标愿景和版本边界文档为准。

## Constraints

- **事实权威**：SDK/Agent 原生会话、消息、工具和 subagent 事实不可由后端平行复制；后端只做 typed 投影、门禁和审计。
- **测试与测评分权**：Workspace pytest 属 Agent-owned 可见回归；正式基准/holdout/scorer 属 evaluator-owned 独立资产，数据库和 Registry 不复制正文。
- **字段所有权**：ID、commit、protocol、score、gate、principal、scope、status、时间和 provenance 均为 backend/evaluator-owned，Agent/Governor 的 hostile 输出不能覆盖。
- **安全**：候选不可读取或修改 holdout、Ground Truth、scorer 和阈值；危险输入、越权、跨 scope、秘密泄漏和部分失败必须 fail-closed。
- **离线**：必需闭环始终可在离线/内网和本地化 LLM 下运行；远程研究、外部协作或公网 fixture 不能成为退出条件。
- **Runtime/env**：容器选择完整 `docker/.env`，本机调试选择 `docker/.env.local-debug`，Vite 只用 `frontend/.env.local`；不使用 layered override 语义。
- **数据与卷**：日常容器根保持 `${HOME}/volume-agent-gov`；P0-MCP 使用独立 Compose project 与临时根，严禁挂载 live volume，成功、失败或中断都必须清理。
- **UI**：P1 主入口是“业务 Agent 详情 → 测评”；测试资产仅作 sample/run deep-link；四阶段工作台不增加第五阶段，Governor 元治理不混入其中。
- **架构**：typed record、持久化 record、运行投影、API response、OpenAPI 和前端生成类型保持边界；生命周期使用集中状态集合、完整转移表与统一 helper。
- **验证**：local-debug、mock Agent、旧 Trace、历史 passed run 或单次重复评分不能替代当前工作树真实容器/browser/live 证据。

## Key Decisions

| Decision | Rationale | Outcome |
| --- | --- | --- |
| v3.1 Phase 从 6 延续到 15 | 保留前序 Phase 0-5 历史与可追溯性 | ✓ Phase 6 已按连续编号完成，历史目录保持原位 |
| P0 Workspace 与 P0-MCP 是独立阻断门 | 静态 Agent 契约与 Runtime/MCP 平台回执互不替代 | — Pending |
| 业务 Agent 测试不进入根静态 collection | 测试正文归属 Workspace Git，平台只治理 runner、隔离、lane 和 receipt | ✓ Phase 6 的 58 leaf 仍只归属该 Workspace；Phase 7 实现通用 lane |
| 保留并收紧 `CLAUDE_HOOK_AUDIT_LOG` | Runtime 仍主动注入该变量，直接移除会破坏活跃契约；任意路径又不能成为旁路 | ✓ 仅接受批准 `DATA_DIR` 下固定审计文件，其他路径 fail-closed |
| Workspace hook 只返回 hard-deny | 未分类输入必须继续由 Claude 原生权限、deny 与 HITL 裁决，hook 不接管 allow 所有权 | ✓ Phase 6 对危险/畸形输入拒绝，安全 Bash、非 Bash 与 MCP 无显式 allow |
| P1 先做 8-case、无工具、静态 L2 | 先证明独立发布测评闭环，不把首切片冒充完整 MVP | — Pending |
| `AgentTestRun` 只作首个 sample adapter | 保持 `EvaluationExecution/Assessment/ComparisonGroup` 协议中立，不恢复旧 `TestDataset/EvalRun` | — Pending |
| P2A 生产仅注册 `claude-code` | 先以委托 adapter 保持等价，再由真实第二协议识别抽象偏差 | — Pending |
| P2B 只交付 shadow outcome | 当前缺可信激活身份、职责分离、canary 与线上安全证据 | — Pending |
| P3 按扩展线独立启动与退出 | 安全垂域、Runtime、Governor、能力包和集成不能互相代表完成 | — Pending |
| 测试通过、离线得分、发布成功、线上效果分别留证 | 避免用单一分数或发布动作替代真实能力与业务结果 | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition:**

1. Requirements invalidated? → Move to Out of Scope with reason.
2. Requirements validated? → Move to Validated with phase reference.
3. New requirements emerged? → Add to Active.
4. Decisions to log? → Add to Key Decisions.
5. “What This Is” still accurate? → Update if drifted.

**After each milestone:**

1. Review all sections against current code, public contracts and real runtime evidence.
2. Recheck the Core Value.
3. Audit Out of Scope and temporary decisions for exit conditions.
4. Update Context, metrics and verified AGV evidence.

---
*Last updated: 2026-08-09 after Phase 6*
