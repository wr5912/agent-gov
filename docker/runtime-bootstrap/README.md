# 运行卷初始化源

本目录只用于准备空运行卷，不是业务 Agent 模板 catalog，也不是运行态 Workspace 的副本。

- `governor-workspace/`：治理 Agent 的仓库配置，初始化时覆盖同名受管文件。
- `business-agents/security-operations-expert/workspace/`：唯一内置业务 Agent Workspace；仅在
  对应运行态 Workspace 整体不存在时复制，已有内容绝不回灌。

普通业务 Agent 只通过 Workspace 包导入创建，并存放在
`${HOST_RUNTIME_VOLUME_ROOT}/data/business-agents/<agent_id>/workspace`。不得把 live Workspace
中的密钥、私有 header、数据库凭据或本机私有路径直接提交到本目录。

提交前运行：

```bash
make runtime-bootstrap-scan
```
