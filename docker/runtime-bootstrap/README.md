# AgentScope Runtime 初始化源

本目录只用于准备空运行卷，不是业务 Agent 模板 catalog，也不是运行态 Workspace 的副本。

- `governor-workspace/`：治理 Agent 的 AgentScope Harness，初始化时覆盖同名受管文件。
- `business-agents/security-operations-expert/workspace/`：唯一内置业务 Agent Workspace；仅在
  对应运行态 Workspace 整体不存在时复制，已有内容绝不回灌。

每个 Workspace 只保留 `agent.yaml`、`AGENT.md`、`skills/`、`mcp/`、`subagents/` 和 `tests/`
等 AgentScope 原生资产。`conversion-report.json` 是离线迁移的覆盖率与摘要证据，不是生产启动步骤。

普通业务 Agent 只通过 Workspace 包导入创建，宿主目录 `${HOST_DATA_MOUNT}/business-agents`
以只读方式挂载为 `/business-agents`。候选 Workspace 使用独立的
`${HOST_RUNTIME_VOLUME_ROOT}/agentscope-runtime/candidates/<candidate-id>/workspace`，不得混入已发布业务目录。
运行数据和可写 Workspace 分别位于 `agentscope-runtime/data`、`agentscope-runtime/workspaces`。

不得把 live Workspace 中的密钥、私有 header、数据库凭据或本机私有路径直接提交到本目录；
生产启动只消费已提交资产，不执行离线迁移。

提交前运行：

```bash
make runtime-bootstrap-scan
.venv/bin/python scripts/check_agentscope_cutover.py --bootstrap-only
```
