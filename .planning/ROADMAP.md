# GSD 路线图

## 当前里程碑：Agent 版本治理与 Diff 对比重构

### 阶段 0：迁移契约与 GSD 对齐

状态：planned

目标：完成评审意见整改，把 GV-1 到 GV-19 转成可执行契约、阶段计划和 GSD artifacts。

交付：

- `docs/Agent版本治理与Diff对比重构方案.md` 评审采纳版。
- `.planning/phases/agent-version-governance-diff-refactor/CONTEXT.md`
- `.planning/phases/agent-version-governance-diff-refactor/PLAN.md`
- 项目级 Docker 持久化默认路径说明更新。

验收：

- 两份评审报告中的 GV-1 到 GV-19 都在方案或 phase plan 中有明确处理。
- 后续代码阶段不需要再决定 provider、legacy projection、candidate profile、publish 状态机、Gitea degraded、旧 API 删除顺序或 Docker 根目录策略。
- 治理硬门 fail 模式通过。

### 阶段 1：Git 服务、Provider 与 Legacy Bootstrap

状态：planned

目标：引入离线 Gitea、Git CLI wrapper、`AgentVersionProvider` 和 legacy projection，为 Git-backed 主流程打基础，但不开放发布。

验收：

- `main-agent.git` 可访问，`/main-workspace/.git` 指向内部 Git 服务。
- 历史 `agent-version-*` 可展示 deprecated 投影，不触发 500。
- Git 服务不可用时写接口 degraded/503，只读接口可用。

### 阶段 2：Change Set 与候选执行

状态：planned

目标：把执行应用迁到 candidate worktree，建立 change set 状态机和候选 diff。

验收：

- 候选执行不修改 `/main-workspace`。
- 重复 create、路径逃逸、baseline 冲突和并发 publish 可预测失败。
- `execution-optimizer` 候选阶段读取 candidate worktree。

### 阶段 3：审批与候选回归

状态：planned

目标：审批 candidate diff，并让回归真实运行在 candidate profile。

验收：

- 候选回归读取 candidate `.mcp.json` 与 `.claude/settings.json`。
- EvalRun 记录 candidate commit sha。
- 回归失败或 provider 失败阻断 publish。

### 阶段 4：发布、归档、回滚和 Reconciliation

状态：planned

目标：实现 Git main/tag push、release archive、rollback release 和启动 reconciliation。

验收：

- publish 任一步失败后重启给出确定状态。
- rollback 生成新 release，不删除历史 release。
- archive hash 可校验。

### 阶段 5：前端治理工作台与旧契约删除

状态：planned

目标：上线三栏治理工作台，迁移前端/OpenAPI/测试消费者，原子删除旧 snapshot API。

验收：

- 浏览器中可完成候选 diff 审查、审批、回归、发布和回滚。
- `rg "/api/agent-versions/main" app frontend tests docs` 仅命中废弃说明或迁移测试。
- OpenAPI、生成类型、前端 build 和 browser smoke 通过。
