# Project Milestones: AgentGov

## v3.0.3 Agent 版本治理与 Diff 对比重构（Shipped: 2026-07-28）

**Delivered:** Git-backed candidate/change set、精确 Diff、候选回归、release/restore 与回滚主链路取代旧 tar snapshot 主流程，并完成 OpenAPI、前端生成类型、治理工作台和 Docker 持久化根迁移。

**Phases completed:** 0-5。

**Key accomplishments:**

- 候选变更在独立 worktree 中执行与回归，不直接修改 live Workspace。
- change set、审批、候选回归、发布、归档和回滚具有精确 commit 与可恢复状态证据。
- 历史 `agent-version-*` 只作 legacy projection；旧 `/api/agent-versions/main/*` 和 tar snapshot 活跃主流程已删除。
- OpenAPI、前端生成类型和浏览器治理工作台已迁移到新契约。
- 宿主机持久化根统一为 `${HOME}/volume-agent-gov`，旧 `docker/volume/` 只作迁移来源或显式兼容边界。

**Completed phase summary preserved from the previous ROADMAP:**

| Phase | Name | Delivered outcome |
| ---: | --- | --- |
| 0 | 迁移契约与 GSD 对齐 | GV-1 至 GV-19、provider、legacy projection、候选 profile、发布状态机、旧 API 删除顺序和 Docker 根路径形成可执行边界。 |
| 1 | Git 服务、Provider 与 Legacy Bootstrap | Git-backed repository/provider 与历史只读投影可用，写路径在 Git 服务异常时 fail/degrade，不破坏只读查询。 |
| 2 | Change Set 与候选执行 | 候选执行不修改 live Workspace，路径逃逸、基线冲突、重复创建和并发发布具有确定性失败。 |
| 3 | 审批与候选回归 | 候选回归读取 candidate `.mcp.json` / `.claude/settings.json`，精确记录 candidate commit，失败阻断发布。 |
| 4 | 发布、归档、回滚与 Reconciliation | publish/tag/archive/restore 可追溯，失败后可恢复，回滚生成新 release 而不删除历史。 |
| 5 | 前端治理工作台与旧契约删除 | 浏览器可审查 Diff、审批、回归、发布和回滚；旧 snapshot API 从活跃代码、OpenAPI 和生成类型退出。 |

**Historical artifacts:**

- `.planning/phases/agent-version-governance-diff-refactor/CONTEXT.md`
- `.planning/phases/agent-version-governance-diff-refactor/PLAN.md`
- `docs/archive/design/Agent版本治理与Diff对比重构方案.md`

这些历史文件继续按原路径保留，不移动、不删除。旧 ROADMAP 没有独立 milestone version 字段；本条按 v3.1 的前序发布基线 `v3.0.3` 补录，不推断不存在的 plan/task/LOC 统计。

**What's next:** v3.1“业务智能体测评与平台治理演进”，从 Phase 6 延续执行 P0 准入、P1 发布测评、P2 Runtime/Governor 基础、P3 组合准入与真实完成审计。

---
