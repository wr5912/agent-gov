# .claude 分层说明

本目录保存团队通用 Claude Code 项目配置。复制到其他项目时，通用层只承载团队统一工作方式、环境硬约束和可迁移治理规则；项目专属路径、脚本、CI、base-ref 策略、端口、运行态数据路径和产品不变量放在目标项目自己的 `CLAUDE.project.md`。

## 默认模板内容

- `settings.json`: 可提交的 Claude Code 项目级设置，默认只放安全共享限制。
- `rules/project.md`: 通用工作流和执行顺序。
- `rules/architecture.md`: 通用架构卫生阈值。
- `rules/verify.md`: 通用验证要求。
- `skills/project-skill/SKILL.md`: 团队开发通用技能。
- `agents/project-worker.md`: 可选项目 worker 子代理。

## 本仓库专项技能镜像

以下技能是本仓库项目专项能力的 Claude 侧镜像，修改时需与 `.codex/skills/` 中的同名技能同步：

- `skills/agentgov-governance-preflight/SKILL.md`: AgentGov 产品定位、目标愿景使命、反馈闭环治理、多业务 Agent 和 prompt/skill/SOP/eval 资产沉淀的治理对象预检。
- `skills/runtime-env-governance/SKILL.md`: 治理本仓库 runtime/env、本机调试、容器部署、Langfuse 和后台 Agent job 模型凭据边界。
- `skills/docs-governance/SKILL.md`: 治理本仓库 `docs/` 文档新增、迁移、归档、入口索引和 Codex/Claude skill 镜像同步；只处理文档容器治理，产品内容建模仍走 `agentgov-governance-preflight`。

## 复用要求

- 新项目应重新填写自己的 `CLAUDE.project.md`。
- 默认模板不得硬编码项目私有脚本名、CI workflow、端口、volume 路径、API key 名称或产品不变量。
- Hook 默认不配置；只有目标项目已经提供稳定、无交互、可重复运行的治理命令时，才在目标项目 `.claude/settings.json` 中启用。
- 个人偏好、本机路径、私有 MCP server 和真实凭据不得进入本目录。
