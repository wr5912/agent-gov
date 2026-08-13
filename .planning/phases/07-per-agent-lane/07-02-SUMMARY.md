---
phase: 07-per-agent-lane
plan: 02
subsystem: isolated-agent-test-worker
tags: [docker-sandbox, worker, inspect-first, git-authority, recovery]

requires:
  - phase: 07-per-agent-lane
    plan: 01
    provides: exact-commit materializer、durable run 与 typed receipt
provides:
  - worker-only Docker authority 与 named-volume source authority
  - inspect-first 最小权限 pytest sandbox
  - 全终态 cleanup 与 execution provenance receipt
  - hardened Git authority 与只读优先 activation operator recovery
affects: [07-03, 08-p0-mcp, workspace-activation]

key-files:
  created:
    - app/agent_testing/docker_engine.py
    - app/agent_testing/container_executor.py
    - app/agent_testing/worker.py
    - docker/agent-test-sandbox.Dockerfile
    - app/runtime/runtime_db_migrations_0057.py
    - app/runtime/runtime_db_migrations_0058.py
    - app/runtime/recovery_read_only_git.py
    - app/runtime/business_agent_lifecycle.py
    - app/runtime/agent_repository_guard.py
    - app/runtime/workspace_activation_recovery_authority.py
    - app/services/agent_workspace_activation_recovery_wiring.py
  modified:
    - docker/docker-compose.yml
    - app/runtime/agent_git_environment.py
    - app/services/agent_workspace_activation_recovery.py
    - Makefile

key-decisions:
  - "只有无 env_file/无网络的 worker 持有 Docker socket；API 与 sandbox 都不持有宿主机控制权。"
  - "sandbox start 前必须以 inspect 逐字段复核 image、argv、env、user、network、mount、limits 和 security options。"
  - "核心与 operator 共用固定 git-dir/work-tree 和 hardened Git authority；0058 只重装存量卷 authority triggers，不建第二状态源。"

patterns-established:
  - "Inspect before start: 实际 Docker 观察值而非计划参数决定是否放行。"
  - "Writer fence: 普通 repository/config/bootstrap writer 在 stable lock 内统一校验实例、删除与 activation fence；仅 activation/recovery 专用 authority 可跨越自身 activation fence。"

requirements-completed: [LANE-02, LANE-03, LANE-04, LANE-08]
completed: 2026-08-10
---

# Phase 7 Plan 02：worker/sandbox 与 Git recovery authority 总结

**不可信 Workspace pytest 已进入 worker-only source authority 与 inspect-first 最小权限 sandbox，activation recovery 也收口为只读优先、精确 apply/resume 的本机 operator 边界。**

## 执行数据

- **完成：** 2026-08-10
- **任务：** 4/4
- **需求：** LANE-02、LANE-03、LANE-04、LANE-08
- **提交：** 本阶段统一提交待关闭时生成，不填写未生成哈希。

## 交付结果

- API 无 Docker socket；worker 不加载 env file、不接入业务网络，只持有必要的 Docker 与专用 named-volume authority。
- sandbox 使用 immutable image ID、非 root、只读 root/source、network none、cap drop ALL、no-new-privileges、最小 env 和限额 tmpfs。
- worker 对 success、pytest failure、timeout、cancel、restart 以及 materialize/inspect/ingest/cleanup error 统一收敛终态；cleanup failure 不可被隐藏。
- Git 命令固定绝对 executable、git-dir 和 work-tree，禁止继承 GIT_*、repo-local 外部驱动、alternates/commondir/grafts/partial clone 与 replace/lazy-fetch 改写 authority。
- raw commit object 提供 tree/parents；operation refs 必须是 direct refs，HEAD 只能 detached 或指向 refs/heads/*，清理不得改变 HEAD topology。
- 0057 recovery attempt 保持 append-only 且终态证据严格；0058 幂等重装 0055/0057 triggers，保证已应用旧 migration 的卷获得新约束。

## 对抗性复审修正

- 关闭前独立复审发现 recovery_required 期间普通 Git writer 仍可写入；实现改为在 stable per-Agent
  lock 内统一校验 exact instance、deletion fence 与 activation fence，覆盖 repository、config 和 bootstrap
  writer。只有 activation/recovery 专用 authority 可跨越自身 activation fence，普通 writer 必须等 operation
  精确终态并释放围栏后再重试。
- 实现者在本计划交付时对 activation 四组组合回归取得 111 passed。
- 根任务当时对 writer fence 4 个精确 nodeid 独立复跑：4 passed。

## 验证摘要

- Git/activation 组合回归：275 passed；这是 Plan 02 历史复审快照，不代替最终同候选回执。
- Ruff check/format、Pyright（0 error）与 make codex-guard 均通过。
- 完整容器与阶段验收见 07-VERIFICATION.md。

## 边界核对

- recovery 只提供 list/inspect/apply/resume，不提供 force complete/reject、clear fence、任意 SHA/DB 路径或 HTTP/UI mutation。
- 本计划不声明 P0-MCP 或独立业务测评已完成。

---
*Phase: 07-per-agent-lane / Plan: 02*
*Completed: 2026-08-10*
