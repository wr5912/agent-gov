# PLAN：Agent 版本治理与 Diff 对比重构

## 摘要

本计划把评审意见整改成后续可执行阶段。阶段 0 只做文档、契约和 GSD artifact；代码实现从阶段 1 开始。

## 边界表

| 边界 | 单一真相来源 | 删除/迁移/保留 | 验证 |
| --- | --- | --- | --- |
| Agent version truth | Git commit/tag via `AgentVersionProvider` | 旧 tar snapshot 主流程迁移后删除 | provider 单测、OpenAPI、真实历史数据 |
| Historical version refs | existing payload projections | 旧 `agent-version-*` 保留解释，不物理删除历史数据 | 旧 API 删除后任务/eval 字段仍可展示 |
| Execution target | `ExecutionTargetContext` | 主流程从 `/main-workspace` 迁到 candidate worktree | 候选执行不改主 workspace |
| Runtime profile | candidate worktree context | 回归和 optimizer 候选阶段读 candidate worktree | candidate commit/worktree 测试 |
| Change set lifecycle | 集中状态机 | 分散 status 字面量不新增 | 非法转移、并发测试 |
| Publish lifecycle | publish/rollback 状态机 | 线性不可恢复流程不采用 | publish/tag/archive/rollback 测试 |
| Public API | 新 agent governance API | 旧 `/api/agent-versions/main/*` 最后阶段原子删除 | OpenAPI 导出、`rg` 验收 |
| Frontend types/UI | OpenAPI generated types | 删除旧 snapshot helper 和手写漂移字段 | type generation、build、browser smoke |
| Docker paths | `HOST_RUNTIME_VOLUME_ROOT` | 默认迁到 `${HOME}/volume-agent-gov`，旧 `docker/volume` 为兼容路径 | Compose 配置测试、README 检查 |

## 阶段 0 任务

1. 将现已归档的 `docs/archive/design/Agent版本治理与Diff对比重构方案.md` 改为评审采纳方案。
2. 新增 `.planning/PROJECT.md` 和 `.planning/ROADMAP.md`。
3. 新增本阶段 `CONTEXT.md` 和 `PLAN.md`。
4. 更新项目专属说明，使 Docker 持久化默认路径变为 `${HOME}/volume-agent-gov`。
5. 运行 `git diff --check` 和 `.venv/bin/python scripts/check_codex_governance.py --mode fail`。

## 后续实现任务

### 阶段 1：Git Provider 与 Bootstrap

- 新增 local Git provider 和统一 Git 操作入口。
- 新增 `AgentVersionProvider` 和临时 facade。
- 新增仓库状态、dirty 检查和 degraded 响应。
- 新增 `HOST_RUNTIME_VOLUME_ROOT` Compose 单根派生。

### 阶段 2：Change Set 与候选执行

- 新增 `agent_change_sets` model、migration、状态机和约束。
- 新增 branch/worktree 创建能力。
- 新增 `ExecutionTargetContext` 和 candidate 写入目标。
- 将 execution optimizer 候选读取目标改为 worktree。

### 阶段 3：审批与候选回归

- 新增 approve/reject API 和 events。
- 新增 candidate worktree runtime context。
- 新增 candidate regression run path。
- 在 eval metadata 中记录 candidate worktree 和 candidate commit。

### 阶段 4：发布、归档和回滚

- 实现 publish 状态机。
- 创建 annotated tag。
- 创建 release archive。
- 新增 rollback release。

### 阶段 5：前端治理与旧 API 删除

- 构建三栏治理工作台。
- 复用 `DiffViewer`。
- 迁移 OpenAPI、generated types、frontend helpers 和 tests。
- 删除旧 agent version router 和 tar manifest 主流程。

## 必需测试与验证

- 治理：`.venv/bin/python scripts/check_codex_governance.py --mode fail`。
- 后端：`make test`。
- API 契约：`.venv/bin/python scripts/export_openapi.py`。
- 生成类型：`pnpm --dir frontend generate:api-types`。
- 前端：`pnpm --dir frontend build`。
- 浏览器：`pnpm --dir frontend verify:feedback-browser`。
- 真实数据：使用迁移后的历史数据打开 task、eval run、regression plan 和 release/change set 详情。

## 验收标准

- GV-1 到 GV-19 都有明确实现处理。
- 候选执行和回归不会修改或读取错误 workspace。
- 旧 HTTP snapshot API 删除后，既有任务/eval 字段仍可解释展示。
- publish 和 rollback 在任一部分失败后可恢复，或明确进入人工恢复。
- Git provider 中断时，写接口 degraded，不破坏只读状态查询。
- 旧 snapshot API 只在所有消费者迁移后删除。
