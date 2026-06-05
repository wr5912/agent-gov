# .codex 分层说明

本目录保存团队通用模板。复制到其他项目时，通用层只承载团队统一工作方式、环境硬约束和可迁移治理规则；项目专属路径、脚本、CI、base-ref 策略和产品不变量必须放在项目覆盖层。

## 可作为团队模板复用

- `config.toml`：安全、通用的 Codex 项目配置模板。
- `rules/project.rules`：规则入口和执行顺序。
- `rules/architecture.rules`：通用架构卫生阈值。
- `rules/verify.rules`：通用验证要求。
- `skills/project-skill/SKILL.md`：团队开发通用技能。
- `agents/worker.toml`：通用项目 worker 画像。

## 通用入口配置

- `hooks.json`：本仓库已按 `AGENTS.override.md` 接入 Stop 治理硬门；复制为通用模板前应清空项目命令，各项目在自己的覆盖层重新声明并配置本地命令。

## 通用质量优先策略

通用层已包含“替换旧设计模式”：当用户要求重构、去除旧设计、优化架构、提高代码质量，或 Analyze 阶段发现旧 facade、兼容 shim、历史路径、重复实现、schema 双轨、状态分散、过期 API、不可达分支等旧债信号时，默认优先级为代码质量 > 新设计/框架/架构 > 旧模式兼容或保留。

涉及 Agent、DSPy、LLM 输出格式化或提示词输出要求时，通用层要求先区分 backend-owned、agent-owned 和 boundary-owned 字段；后端已知字段不得让 LLM 复述后作为权威输出。

项目覆盖层可在 `AGENTS.override.md` 中继续收紧兼容边界、迁移策略和治理硬门。

`.codex` 不存放项目债务账本。治理通过 git base 对比执行：既有超限未增长输出 `BASELINE`，新增超限或旧债增长输出 `FAIL`。

## 复用要求

- 新项目应在自己的 `AGENTS.override.md` 中声明治理命令、CI 的 base-ref 策略和产品不变量。
- 新项目必须保留团队统一环境硬约束：`.venv`、`uv`、`pnpm`、Python 3.10+、Node.js 20+、国内下载源和 Docker 目录规范。
- 旧债清单如需记录，应放在普通 docs 或 issue，不得放入 `.codex` 作为机器豁免。
