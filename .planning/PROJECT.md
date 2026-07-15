# agent-gov GSD 项目

## 项目意图

本项目是 AgentGov 的反馈优化闭环运行时。当前长程重构重点是把主 Agent 配置版本治理从 tar/manifest 快照模式升级为 Git-backed change set、候选回归、发布归档和可恢复回滚链路。

## 已完成长程里程碑

### Agent 版本治理与 Diff 对比重构

目标：

- 主 Agent 配置变更先进入 candidate worktree，不直接写 `/main-workspace`。
- 候选 Diff 支持审批、回归门禁、发布和回滚。
- 旧 `/api/agent-versions/main/*` 和旧 tar snapshot 主流程在消费者迁移完成后删除。
- 历史 `agent-version-*` 引用通过 legacy projection 可解释展示，不再尝试 diff/rollback。
- Docker host runtime root 默认迁到 `${HOME}/volume-agent-gov`，旧 `docker/volume` 只作为迁移来源或显式兼容路径。

## 必读方法论

长程阶段执行前必须读取：

- `.planning/METHODOLOGY.md`
- `docs/engineering/长程重构质量闭环.md`
- `docs/engineering/GSD长程重构阶段清单.md`
- `docs/archive/design/Agent版本治理与Diff对比重构方案.md`
- `docs/archive/design/Agent版本治理与Diff对比重构方案评审报告.md`
- `docs/design_review_report/Agent版本治理与Diff对比重构方案评审报告v2.md`

## 项目验证门槛

非琐碎代码、配置、测试和治理文档变更必须运行：

```bash
.venv/bin/python scripts/check_codex_governance.py --mode fail
```

长程阶段默认验证矩阵：

- `make test`
- `.venv/bin/python scripts/export_openapi.py`
- `pnpm --dir frontend generate:api-types`
- `pnpm --dir frontend build`
- `pnpm --dir frontend verify:feedback-browser`
- 使用迁移后的历史数据验证列表、详情和旧版本投影

不适用或无法运行时，阶段总结必须写明原因、残余风险和恢复路径。
