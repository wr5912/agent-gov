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

- `hooks.json`：通用模板默认不绑定任何项目脚本；各项目如需 Stop hook，应在自己的覆盖层声明并配置本地命令。

`.codex` 不存放项目债务账本。治理通过 git base 对比执行：既有超限未增长输出 `BASELINE`，新增超限或旧债增长输出 `FAIL`。

## 复用要求

- 新项目应在自己的 `AGENTS.override.md` 中声明治理命令、CI 的 base-ref 策略和产品不变量。
- 新项目必须保留团队统一环境硬约束：`.venv`、`uv`、`pnpm`、Python 3.10+、Node.js 20+、国内下载源和 Docker 目录规范。
- 旧债清单如需记录，应放在普通 docs 或 issue，不得放入 `.codex` 作为机器豁免。
