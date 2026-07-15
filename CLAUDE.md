# Claude Code 团队项目说明

本文件是团队级 Claude Code 通用说明，可在多个项目中复用。项目专属路径、脚本、CI、端口、密钥边界和产品不变量放在 `.claude/rules/agentgov-project.md`，不要硬编码进通用规则。

## 读取顺序

Claude Code 会加载本文件，并通过 `@` 导入以下团队规则：

@.claude/rules/project.md
@.claude/rules/architecture.md
@.claude/rules/verify.md
@.claude/rules/agentgov-project.md

## 协作语言

- 所有对话、文档、代码注释和提交说明默认使用中文。
- 代码标识符、命令、日志关键字、错误信息和第三方 API 名称保留原文。

## 工作方式

- 非琐碎变更遵循 Analyze -> Plan -> Execute -> Verify。
- 写文件前先说明计划、涉及文件、验证方式和成功标准。
- 优先用仓库事实和官方文档定性，不基于猜测修改配置或代码。
- 普通小改保持精准；重构、去旧设计或质量治理任务优先收口旧入口、重复实现和漂移文档。
- 如需求不清、存在安全风险、可能破坏公开契约或影响持久化数据，先澄清再执行。

## Claude Code 配置边界

- 静态、长期有效的项目事实写在 `CLAUDE.md` 或 `.claude/rules/`。
- 多步骤、按需触发的流程写成 `.claude/skills/<skill-name>/SKILL.md`。
- 大量搜索、独立审查或需要隔离上下文的任务使用 `.claude/agents/` 中的子代理。
- 共享 MCP 只通过项目根目录 `.mcp.json` 提交；个人 MCP、凭据和本机路径留在用户级配置。
- Hook 默认关闭；只有目标项目存在真实可运行的治理命令时才启用。

## 安全边界

- 不读取、输出或提交 API key、OAuth token、MCP header、数据库凭据和私有证书。
- 不把个人模型、账号、密钥、本机绝对路径、运行态数据库或日志提交到团队仓库。
- 默认 `.claude/settings.json` 只放可共享的项目限制；个人偏好放在用户级或本地配置。

## Git 提交

提交标题使用 `<type>(<scope>): <描述>`，`scope` 可省略。小改动可以只有标题；非琐碎变更必须在标题后空一行补正文，说明背景、主要改动、兼容影响和验证结果。

非琐碎提交正文建议使用：

```text
Background:
- 为什么需要这个变更。

Changes:
- 主要改了什么。

Compatibility:
- API、schema、迁移、配置、部署或用户行为影响；无影响写“无”。

Verification:
- 实际运行过的测试、构建、治理检查或人工验收。
```

提交说明不得包含密钥、私有路径、用户隐私、完整 prompt 对话或流水账。

## 外部实践来源

本说明吸收并改写了 `forrestchang/andrej-karpathy-skills` 中适合团队开发的行为规则，保留其“四项准则”的工程思想，并按 Claude Code 官方配置面重新组织。

来源: https://github.com/forrestchang/andrej-karpathy-skills
许可: MIT
