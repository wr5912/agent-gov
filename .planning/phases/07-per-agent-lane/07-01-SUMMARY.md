---
phase: 07-per-agent-lane
plan: 01
subsystem: agent-testing-authority
tags: [exact-commit, raw-git, typed-receipt, activation, deletion]

requires:
  - phase: 06-p0-w1-workspace
    provides: 可锁定的安全 Workspace suite 与权威配置基线
provides:
  - raw Git object exact-commit 物化与固定 pytest suite 边界
  - API validate-and-enqueue、durable run 与 typed execution provenance receipt
  - receipt-aware 发布工程卫生门
  - Workspace activation 与业务 Agent 删除的 durable journal/fence
affects: [07-02, 07-03, 08-p0-mcp, release-gate]

key-files:
  created:
    - app/agent_testing/execution_contracts.py
    - app/agent_testing/materializer.py
    - app/runtime/runtime_db_migrations_0053.py
    - app/runtime/runtime_db_migrations_0055.py
    - app/runtime/runtime_db_migrations_0056.py
  modified:
    - app/agent_testing/service.py
    - app/services/agent_workspace_activation.py
    - app/services/business_agent_deletion.py

key-decisions:
  - "API 只负责验证 Agent/commit 并创建 durable queued run，不在 API 进程执行 pytest。"
  - "Agent-owned report 只是 unverified diagnostics；发布门只信任后端绑定当前 commit/suite/source 的 typed receipt。"
  - "activation 和 deletion 先持久化 intent/journal 再变更 Git 或文件系统，未决操作持续 fence runtime。"

patterns-established:
  - "Exact source: 从 raw Git objects 物化精确 commit，不执行 checkout/filter/hook。"
  - "Receipt authority: report 与 backend-owned receipt 分开持久化和判权。"

requirements-completed: [LANE-01, LANE-04, LANE-07]
completed: 2026-08-10
---

# Phase 7 Plan 01：exact-commit 与 durable authority 收口总结

**平台测试入口已从 API 内本地 pytest 收口为 exact-commit validate-and-enqueue，并以 backend-owned typed receipt 作为发布必要的工程卫生证据。**

## 执行数据

- **完成：** 2026-08-10
- **任务：** 5/5
- **需求：** LANE-01、LANE-04、LANE-07
- **提交：** 本阶段统一提交待关闭时生成，不填写未生成哈希。

## 交付结果

- suite inspection 和 test run 都从 raw Git objects 物化精确 commit；dirty live Workspace、replace ref、hook 或 filter 不参与源码证据。
- symlink、gitlink、越界/非法路径、超限对象和 live fixture 稳定 fail-closed，不回退旧 runner。
- run 的 source observation、report 和 receipt 分权持久化；历史 null receipt 可读但不具备发布资格。
- 覆盖导入/restore 使用两阶段 activation intent，终态重新校验 audit、admission、活动工作、Git 字节与 durable refs。
- 业务 Agent 删除通过精确实例 token、幂等键、durable journal 和布局外稳定锁执行，已公开 ID 不再重用。

## 验证摘要

- 相关 Git/activation/deletion 行为在本计划交付时的阶段候选中完成 275 passed 组合回归。
- 同一阶段候选当时通过 make main-flow-test 的 609 passed 与 make test 的 main-full 1809 passed；
  这些数字保留为 Plan 01 历史快照，不代表最终候选。
- 最终同候选验收只以 07-VERIFICATION.md 为准。

## 边界核对

- 本计划不把 Workspace pytest 报告升格为独立业务测评或安全结论。
- worker、sandbox、真实容器回执和 operator recovery 由 07-02/07-03 继续收口。

---
*Phase: 07-per-agent-lane / Plan: 01*
*Completed: 2026-08-10*
