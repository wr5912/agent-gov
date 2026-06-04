# 如何替换为你本地开发的 MCP 服务并使其生效

本文说明如何把 `volume/main-workspace/mcp_servers/` 中的示例 MCP 服务替换为你本地开发的 MCP 服务，并让 Claude Code 智能体使用它们。

## 1. 先判断你的 MCP 服务类型

常见有两种：

1. **stdio MCP 服务**：Claude Code 通过启动本地命令连接，例如 `python server.py`、`node dist/server.js`、`uv run server`。
2. **HTTP/SSE MCP 服务**：服务已经作为 Web 服务启动，Claude Code 通过 URL 连接。

建议：
- 本地开发、随容器一起发布：优先 stdio。
- 企业统一服务、需要鉴权和横向扩展：优先 HTTP。

## 2. 替换 stdio MCP 服务

### 2.1 放置你的服务

方式 A：直接放到 volume 中：

```text
volume/main-workspace/mcp_servers/my_soc_mcp/
├── server.py
├── requirements.txt
└── README.md
```

方式 B：挂载外部路径到容器：

```bash
-v /your/local/mcp/my_soc_mcp:/main-workspace/mcp_servers/my_soc_mcp
```

### 2.2 修改 `.mcp.json`

打开：

```text
volume/main-workspace/.mcp.json
```

将原服务替换为你的服务，例如：

```json
{
  "mcpServers": {
    "soc-data": {
      "type": "stdio",
      "command": "${PYTHON_BIN:-python}",
      "args": ["${CLAUDE_WORKSPACE:-/main-workspace}/mcp_servers/my_soc_mcp/server.py"],
      "env": {
        "SOC_API_URL": "${SOC_API_URL}",
        "SOC_API_TOKEN": "${SOC_API_TOKEN}",
        "SOC_TENANT": "${SOC_TENANT:-default}"
      }
    }
  }
}
```

注意：
- `soc-data` 是 Claude Code 看到的 MCP server 名称。改名后，skills、subagents、settings 中的 `mcp__soc-data__...` 权限规则也要同步改。
- `${VAR:-default}` 表示环境变量存在则使用变量值，不存在则使用默认值。
- 不要把真实 token 写死到 `.mcp.json`。

### 2.3 安装依赖

如果你的 MCP 服务使用 Python：

```bash
cd /main-workspace
python -m pip install -r mcp_servers/my_soc_mcp/requirements.txt
```

如果使用 Node.js：

```bash
cd /main-workspace/mcp_servers/my_soc_mcp
npm install
npm run build
```

并把 `.mcp.json` 改成：

```json
{
  "mcpServers": {
    "soc-data": {
      "type": "stdio",
      "command": "node",
      "args": ["${CLAUDE_WORKSPACE:-/main-workspace}/mcp_servers/my_soc_mcp/dist/server.js"],
      "env": {
        "SOC_API_URL": "${SOC_API_URL}",
        "SOC_API_TOKEN": "${SOC_API_TOKEN}"
      }
    }
  }
}
```

## 3. 替换为 HTTP MCP 服务

如果你的 MCP 已经作为 HTTP 服务启动，例如：

```text
${MCP_SERVER_URL}
```

则 `.mcp.json` 可以配置为：

```json
{
  "mcpServers": {
    "soc-data": {
      "type": "http",
      "url": "${SOC_MCP_URL}",
      "headers": {
        "Authorization": "Bearer ${SOC_MCP_TOKEN}"
      }
    }
  }
}
```

注意：
- Claude Agent SDK 当前使用 `type: "http"` / `type: "stdio"` 标识 MCP server 类型；如果其他工具示例使用 `transport: "http"`，在本 runtime 中应改为 `type: "http"`。
- 如果 `${SOC_MCP_TOKEN}` 没有默认值且环境变量未设置，Claude Code 会解析失败。
- 开发环境可先去掉 `headers`，生产环境必须接入鉴权。
- 如果 Claude Code 运行在 Docker 容器内，容器自身地址不是宿主机地址。访问宿主机或企业 MCP 服务时，使用部署注入的 `${MCP_SERVER_URL}`，不要把本机 IP 或端口写入模板。

当前网络安全运营模拟数据服务示例：

```json
{
  "mcpServers": {
    "sec-ops-data": {
      "type": "http",
      "url": "${MCP_SERVER_URL}"
    }
  }
}
```

## 4. 同步权限配置

MCP 服务替换后，需要检查：

```text
volume/main-workspace/.claude/settings.json
```

示例读权限：

```json
"allow": [
  "mcp__soc-data__*",
  "mcp__security-kb__*"
]
```

示例高风险动作确认：

```json
"ask": [
  "mcp__response-orchestrator__execute_*",
  "mcp__*__*write*",
  "mcp__*__*update*",
  "mcp__*__*delete*"
]
```

命名规则通常是：

```text
mcp__<server-name>__<tool-name>
```

例如 MCP server 名称为 `soc-data`，工具名为 `query_alerts`，则工具权限名通常是：

```text
mcp__soc-data__query_alerts
```

为了 MVP 阶段快速迭代，可以先允许：

```json
"mcp__sec-ops-data__*"
```

生产环境建议收敛到具体工具名。

## 5. 同步 Agent 指令和 Skills

如果你修改了 MCP server 名称、工具名称或工具语义，需要同步以下文件：

```text
CLAUDE.md
agent.yaml
.claude/agents/*.md
.claude/skills/*/SKILL.md
.claude/commands/*.md
```

重点检查：

- `allowed-tools` 中的 MCP 工具名。
- `CLAUDE.md` 中的工具优先级说明。
- `agent.yaml` 中的 `mcp.servers` 描述。
- 高风险动作是否仍走 `ask` 或审批流程。

## 6. 使配置生效

### 6.1 新会话最稳妥

修改 `.mcp.json` 后，最稳妥方式是重启 Claude Code：

```bash
cd /main-workspace
claude
```

进入后运行：

```text
/mcp
```

检查 server 是否连接成功。

### 6.2 项目级 MCP 首次使用需要批准

`.mcp.json` 是项目级共享配置。Claude Code 首次加载项目级 MCP server 时，通常会提示用户批准。批准后才会连接和使用。

如需重置批准状态：

```bash
claude mcp reset-project-choices
```

然后重新进入 Claude Code 并通过 `/mcp` 审核。

### 6.3 Skills 生效

修改 `.claude/skills/<skill-name>/SKILL.md` 后，当前会话通常会自动发现变更。若新增顶层 skills 目录或发现异常，重启 Claude Code。

### 6.4 Subagents / commands / output styles 生效

修改以下配置后，建议重启 Claude Code：

```text
.claude/agents/
.claude/commands/
.claude/output-styles/
.claude/settings.json
```

## 7. 验证 Checklist

进入 Claude Code 后执行：

```text
/mcp
/skills
/agents
/hooks
```

然后用以下提示词测试：

```text
查询最近24小时高危告警数量，并列出Top规则
```

```text
对 ALERT-2026-0001 做告警研判，输出证据链和处置建议
```

```text
为 ALERT-2026-0001 生成主机隔离处置计划，只做 dry-run，不执行
```

如果失败，按以下顺序排查：

1. `.mcp.json` JSON 是否合法。
2. `command` 和 `args` 路径在容器内是否存在。
3. MCP 服务依赖是否安装。
4. 环境变量是否设置。
5. Claude Code `/mcp` 中是否显示连接错误。
6. `.claude/settings.json` 权限是否允许该 MCP 工具。
7. hooks 是否误拦截高风险工具。

## 8. 生产接入建议

- MCP 服务侧必须做租户隔离、鉴权、审计和限流。
- 读工具和写工具分开命名，例如 `query_alerts` 与 `execute_block_ip`。
- 写工具默认只 dry-run；真实执行要求 `approval_id`、`approver`、`change_ticket`。
- 所有执行动作返回统一结构：`executed`、`action_id`、`targets`、`status`、`rollback_id`、`evidence`。
- 大查询必须分页和限制默认时间范围，避免一次性拉取大量敏感日志。
- MCP 返回数据应脱敏或最小化，避免把完整原始日志塞进模型上下文。
