# 业务 Agent 创建模板 catalog

每个子目录是一个**可选模板**（`template_id` = 子目录名），创建业务 Agent 时
`POST /api/agent-registry` 可传 `template_id` 选择；缺省用 `general`。

## 工作机制

创建业务 Agent 时，所选模板目录被拷贝进该 Agent 的运行卷 workspace
（`${HOST_RUNTIME_VOLUME_ROOT}/data/business-agents/<id>/workspace/`），并只替换通用模板中
明确声明的身份 token：

- `{{AGENT_ID}}` → 该 Agent 的 `agent_id`
- `{{AGENT_NAME}}` → 该 Agent 的显示名

声明式业务 Agent seed 不走这套身份替换；同 ID 或跨 ID 实例化都按字节原样复制。已有 live
workspace 不因模板或 seed 更新而被回灌。

## 边界（与 runtime-template-safety 同口径）

这里的“模板”特指 repo-tracked generic template，不是已导入的 live workspace。generic
template **不得**含真实 api_key、MCP header、数据库凭据或本机私有路径；其起始权限采用保守
基线（只读自身 workspace，Bash 与写入动作按 project settings 请求确认，拒读
`.env`/`secrets`）。新增模板按 `general` 的结构组织：`CLAUDE.md`、
`.claude/settings.json`、`.mcp.json`，按需加 `.claude/{skills,agents,rules}` 等。

声明式 repo seed/builtin 位于 `data/business-agents/<seed_id>/workspace/`：它在实例化时原样
复制，并保留自己的权限配置，但进入 Git 前仍必须满足仓库安全边界。明确秘密和本机私有路径
会阻断提交检查；非秘密真实 endpoint、内网地址和较宽权限只提示提交者复核，不会被静默改写。
live workspace 原样导入不套用本节基线；把 live workspace 归档为 repo builtin 时，先在仓库
外保留逐字节候选，再由提交者处理扫描结果。
