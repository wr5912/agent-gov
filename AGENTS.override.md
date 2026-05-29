# claude-agent-runtime 项目覆盖说明

本文件只放本仓库专属约束。团队通用行为仍以 `AGENTS.md` 和 `.codex/rules/` 为准；复制通用模板到其他项目时，不应把本文件中的路径、命令和 CI 当作默认配置。

## 必读项目文档

- `docs/AGENT_GOVERNANCE_REFLECTION_AND_PLAN_R2.md`：解释本仓库治理硬门的来源、失效点和落地目标。

## 本仓库治理硬门

本仓库的非琐碎代码、配置、测试和治理文档变更，必须运行：

```bash
.venv/bin/python scripts/check_codex_governance.py --mode fail
```

- `--mode warn` 只允许在 Analyze 阶段观察问题，不允许作为 Verify 通过标准。
- `make test` 已依赖 `codex-guard`，会先运行上述 fail 模式治理检查。
- `.codex/hooks.json` 的 `Stop` hook 会运行同一条治理硬门。
- `.github/workflows/governance.yml` 是本仓库 CI 入口，会在 PR 和 `main` push 上运行治理硬门与测试。

## 本仓库差异治理

治理脚本通过 git base 对比约束新增债务和旧债增长：

- 本地 hook 默认对比 `HEAD`。
- PR CI 对比目标分支。
- `main` push CI 对比 `HEAD^`。
- 治理输出 `BASELINE` 表示既有超限未增长，不阻断；`FAIL` 表示新增超限或旧债增长。
- 旧债清单如需人工跟踪，只能放在普通 docs 或 issue 中，不得作为治理放行豁免。

## 本仓库产品与环境不变量

- 离线模式是产品不变量；必需工作流不得依赖远程服务。
- Docker 持久化路径统一在 `docker/volume/` 下。
- 环境变量放在 `docker/.env`；应用配置放在 `config/*.yaml` 或 `config/*.json`。
- 不得将 API key、MCP header、数据库凭据或本机私有路径写入仓库。

## 本仓库专属边界

- 不要把 `claude-agent-runtime` 的脚本名、CI workflow 或 hook 命令硬编码回 `AGENTS.md`、通用 `.codex/rules/` 或通用 skill 文本。
- 如果其他项目复用本仓库的团队模板，必须在自己的 `AGENTS.override.md` 中重新声明治理命令、base-ref 策略和产品不变量。
