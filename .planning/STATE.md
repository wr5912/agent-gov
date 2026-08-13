---
gsd_state_version: 1.0
milestone: v3.1
milestone_name: 业务智能体测评与平台治理演进
status: executing
stopped_at: Phase 7 complete, Phase 8 ready
last_updated: "2026-08-13"
last_activity: 2026-08-13 -- Phase 7 completed; Phase 8 P0-MCP ready/planned
progress:
  total_phases: 10
  completed_phases: 2
  total_plans: 4
  completed_plans: 4
  percent: 20
---

# Project State

## Project Reference

See: `.planning/PROJECT.md` (updated 2026-08-13)

**Core value:** 让每个业务 Agent 的能力变化都能从真实运行与失败证据出发，经独立测评和安全门验证后形成可发布、可回滚、可追溯的精确版本。  
**Current milestone:** v3.1 业务智能体测评与平台治理演进  
**Current focus:** Phase 8 — P0-MCP 精确两工具回执（ready）
**Version boundary:** 当前发布版 3.0.3；v3.1 正在 executing，Phase 7 收尾不 bump `VERSION` 或 tag。

## Current Position

Phase: 8 of 15 (P0-MCP 精确两工具回执)
Plan: 00 of TBD
Status: Ready — Phase 7 complete
Last activity: 2026-08-13 -- Phase 7 complete; Phase 8 handoff ready

Progress: [██░░░░░░░░] 20%

## Performance Metrics

**v3.1 velocity:**

- Phase 7 plans completed: 3/3
- Phase 7 tasks completed: 12/12
- Plan 07-03: complete
- Timed plans: 1
- Recorded execution time: 72h（Phase 6；Phase 7 未记录分计划耗时）

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
| --- | ---: | ---: | ---: |
| 6. P0-W1 安全 Workspace 基线修复 | 1 | 72h | 72h |
| 7. per-Agent 最小权限隔离测试 lane | 3 complete | 未记录 | 未计算 |

*Updated after each plan completion.*

## Accumulated Context

### Decisions

Full decisions are logged in `.planning/PROJECT.md`.

- [v3.1] Phase 编号从 6 延续到 15；前序 Phase 0-5 摘要在 `.planning/MILESTONES.md`，历史 phase 目录保持原位。
- [P0] Workspace exact-commit 与 P0-MCP 是两个独立阻断门；业务 Agent leaf 不进入根静态 collection。
- [P1] Workspace 可见回归与 evaluator-owned 正式基准分权；首切片固定 8-case、无工具、静态 L2。
- [P2A/P2B] Runtime 近期只启用 Claude；Governor 只产 shadow outcome，不自动激活。
- [P3] 扩展线独立启动/退出，P3 框架不自动升级 AGV 或版本号。
- [Phase 6]: 保留并收紧 CLAUDE_HOOK_AUDIT_LOG 活跃契约 — 只接受批准 DATA_DIR 下固定审计文件，关闭任意路径旁路。
- [Phase 6]: Hook 只 hard-deny，不返回 allow — 未命中输入继续由 Claude 原生权限、deny 与 HITL 所有权裁决。
- [Phase 7]: Workspace pytest 回执只声明 `execution_provenance`；Agent-owned report 不冒充独立测评或安全结论。
- [Phase 7]: API、worker 与 sandbox 分离 authority；只有 worker 持有 Docker socket 和专用 named volume，sandbox 在固定最小权限契约下执行。
- [Phase 7]: profile 只能映射固定 verifier；首次 bootstrap 清理 caller env 并固定绝对工具链，候选契约为 `agentgov.container-acceptance-candidate.v5`。
- [Phase 7]: receipt 只有 `reserved -> prepared -> single terminal`，绑定 lifecycle lock；stale recovery 与 signal commit point 不得制造第二终态。
- [Phase 7]: runtime-bootstrap 只来自候选镜像；Compose 不再接受宿主 bind 或 `RUNTIME_BOOTSTRAP_HOST_DIR`。
- [Phase 7]: 0059 为 deletion journal 提供 append-only/transition/terminal authority；Workspace fingerprint、Git metadata/temp、Settings context race、defensive boundary 与 TIA 均已有精确宿主契约。
- [Phase 7]: P0 exact-commit、P0-MCP 与 P1 live 保持独立 lane；Phase 7 收尾提交由文档冻结后的 exact-tree durable final gate 原子约束，关闭也不代表 P0-MCP 或独立发布测评完成。

### Pending Todos

- 为 Phase 8 建立固定上游、过滤 OpenAPI、精确两工具/no-resource fixture 与独立 Compose/runtime authority。
- 完成直接 MCP 协议调用、真实 AgentGov Claude Runtime 双工具调用、最终回答标记、typed receipt 与 cleanup 证据。
- 在 Phase 8 完成候选上运行专项、主流程、串行全量与公共 `container-security-mcp-test`，再进入 Phase 9 双门收口。

### Blockers/Concerns

- [Phase 8] P0-MCP fixture/spec/runner/receipt 未实现；共享 MCP 的 10 tools/1 resource/1 template 不满足精确两工具门，必须使用固定上游的隔离两工具候选。
- [Phase 10] P1 EvaluationBenchmark/ProtocolRevision/Execution/Assessment 等领域与 API 基本为零。

## Deferred Items

| Category | Item | Status | Deferred At |
| --- | --- | --- | --- |
| P3 conditional | 生产 MCP、完整安全 MVP、公共 session 主键迁移、第二生产 Runtime、Governor canary、observer/Multica | 仅在各自启动门满足后另立实施计划 | v3.1 planning |

## Session Continuity

Last session: 2026-08-13
Stopped at: Phase 7 complete; ready to plan and execute Phase 8 P0-MCP on a new branch after the Phase 7 commit
Resume file: None
