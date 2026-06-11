# .claude 分层说明

本目录保存团队通用 Claude Code 项目配置。复制到其他项目时，通用层只承载团队统一工作方式、环境硬约束和可迁移治理规则；项目专属路径、脚本、CI、base-ref 策略、端口、运行态数据路径和产品不变量放在目标项目自己的 `CLAUDE.project.md`。

## 默认模板内容

- `settings.json`: 可提交的 Claude Code 项目级设置，默认只放安全共享限制。
- `rules/project.md`: 通用工作流和执行顺序。
- `rules/architecture.md`: 通用架构卫生阈值。
- `rules/verify.md`: 通用验证要求。
- `skills/project-skill/SKILL.md`: 团队开发通用技能。
- `agents/project-worker.md`: 可选项目 worker 子代理。

## 复用要求

- 新项目应重新填写自己的 `CLAUDE.project.md`。
- 默认模板不得硬编码项目私有脚本名、CI workflow、端口、volume 路径、API key 名称或产品不变量。
- Hook 默认不配置；只有目标项目已经提供稳定、无交互、可重复运行的治理命令时，才在目标项目 `.claude/settings.json` 中启用。
- 个人偏好、本机路径、私有 MCP server 和真实凭据不得进入本目录。
