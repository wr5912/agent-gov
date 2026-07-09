# 业务 Agent 创建模板 catalog

每个子目录是一个**可选模板**（`template_id` = 子目录名），创建业务 Agent 时
`POST /api/agent-registry` 可传 `template_id` 选择；缺省用 `general`。

## 工作机制

创建业务 Agent 时，所选模板目录被拷贝进该 Agent 的运行卷 workspace
（`${HOST_RUNTIME_VOLUME_ROOT}/data/business-agents/<id>/`），并渲染占位符：

- `{{AGENT_ID}}` → 该 Agent 的 `agent_id`
- `{{AGENT_NAME}}` → 该 Agent 的显示名

拷贝是幂等的：workspace 已存在的文件不覆盖（保留用户编辑）。

## 边界（与 runtime-template-safety 同口径）

模板里**不得**含真实 api_key / MCP header / 数据库凭据 / 本机私有路径；起始权限保守
（只读自身 workspace，Bash 由 sandbox/hook/deny 兜底直接执行，写入 workspace 需确认，拒读 `.env`/`secrets`）。新增模板按 `general`
的结构组织：`CLAUDE.md`、`.claude/settings.json`、`.mcp.json`，按需加 `.claude/{skills,agents,rules}` 等。
