# 阶段上下文：Agent 版本治理与 Diff 对比重构

## 目标

完成评审意见整改，并为后续代码阶段建立可执行、可验证的 GSD phase plan。

## 来源文档

- `docs/archive/design/Agent版本治理与Diff对比重构方案.md`
- `docs/archive/design/Agent版本治理与Diff对比重构方案评审报告.md`
- `docs/design_review_report/Agent版本治理与Diff对比重构方案评审报告v2.md`
- `.planning/METHODOLOGY.md`
- `docs/engineering/长程重构质量闭环.md`
- `docs/engineering/GSD长程重构阶段清单.md`

## 已核实问题

两份评审报告的 GV-1 到 GV-19 均属实。关键证据：

- 当前回归 runner 通过普通 chat path 运行，未绑定 candidate worktree。
- `safe_workspace_target()` 直接使用 `settings.main_workspace_dir`。
- `AgentVersionStore` 被 main、runtime、worker、router、execution service 和 `FeedbackStore` provider 多处耦合。
- 旧 `/api/agent-versions/main/*` 仍被后端、前端、OpenAPI 测试和生成类型依赖。
- 历史 DB 表仍保存旧 agent version id。
- Compose、`.env.example`、Makefile 和 README 仍围绕 `docker/volume`。

## 已锁定决策

- 本轮只覆盖 `main-agent`。
- 旧 tar snapshot 不导入 Git 历史。
- 旧版本 id 不再提供旧 HTTP API；既有任务/eval 字段保留解释，不在本轮物理删除历史目录。
- 旧 `/api/agent-versions/main/*` 最后阶段原子删除，不在基础设施阶段提前删除。
- 候选执行和候选回归必须运行在 candidate worktree。
- 默认 host runtime root 改为 `${HOME}/volume-agent-gov`。
- 本轮默认采用 local Git provider；Gitea 只作为后续可选外部服务展示/发现能力，产品 API 仍是审批和发布主入口。
- v1 不做多用户 RBAC，使用声明式 `operator`，但必须记录 request source、API key alias 或部署身份。

## 阶段 0 不做

- 不实现 Gitea 服务。
- 不修改后端业务代码。
- 不修改前端工作台代码。
- 不删除旧 API 或旧版本目录。
- 不运行浏览器 smoke，除非后续代码阶段涉及 UI。

## 成功标准

- 方案文档采纳 GV-1 到 GV-19。
- `.planning` 中存在项目、路线图和本 phase 的 context/plan。
- 项目不变量与 `${HOME}/volume-agent-gov` 默认路径一致。
- 治理硬门 fail 模式通过。
