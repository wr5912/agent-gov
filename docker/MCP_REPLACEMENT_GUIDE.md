# AgentScope Runtime MCP 配置指南

AgentGov 不在控制面解释或执行 MCP。每个受管 Agent 的 MCP 声明属于版本化 Harness，保存在：

```text
business-agents/<agent_id>/workspace/mcp/<server-name>.json
```

AgentScope Runtime 创建版本固定的 Workspace 时读取这些文件，解析显式环境变量引用，并通过 AgentScope 公共 `MCPClient` 接入。API 容器不接收 MCP 凭据。

内置 sec-ops 声明对应本机免鉴权 MCP，因此没有 `Authorization` 或 token 引用；不发送无效占位
header。它仍要求全部批准工具、资源和模板真实存在，不因免鉴权而放宽能力清单。
下面是**需要 Bearer 认证的 MCP** 配置示例；只有显式声明的凭据才是该 Harness 的绑定前提。

## HTTP MCP 示例

```json
{
  "schema_version": 1,
  "name": "sec-ops",
  "credential_refs": [
    {"env": "SEC_OPS_MCP_URL", "path": "mcp_config.url"},
    {"env": "SEC_OPS_MCP_TOKEN", "path": "mcp_config.headers.Authorization"}
  ],
  "mcp_config": {
    "type": "http_mcp",
    "url": "${SEC_OPS_MCP_URL}",
    "headers": {"Authorization": "Bearer ${SEC_OPS_MCP_TOKEN}"},
    "timeout": 30.0
  },
  "enable_tools": ["soc_api__list_alerts_api_v1_alerts_get"],
  "enable_resources": ["openapi://soc_api/resp/action-defs"],
  "enable_resource_templates": ["openapi://soc_api/resp/playbooks/{playbook_id}"]
}
```

`enable_tools` 是 AgentScope 原生 tool 的精确名称清单。AgentScope 2.0.8 不直接暴露
MCP resources；Runtime 因此仅针对上述两个 resource allowlist 注册三个只读公共工具：

- `mcp__<server>__resources_list`
- `mcp__<server>__resource_templates_list`
- `mcp__<server>__resource_read(uri)`

它们使用官方 `mcp` SDK，返回前会同时核对 Harness allowlist 与服务端实时公布的
resource/template；未知项、复杂 URI template、二进制或超限正文一律拒绝。

## 强制约束

- `mcp_config.type` 只能使用 `http_mcp` 的 streamable HTTP `/mcp` transport；本次单 Runtime 原子切换不接受 `stdio_mcp`、SSE transport 或 redirect。
- `enable_tools`、`enable_resources`、`enable_resource_templates` 必须全部显式存在且名称唯一；tool 名不得使用通配符。服务端新增能力不会自动进入已有 Harness。
- endpoint 必须是按 server 名派生的单个 `${<SERVER>_MCP_URL}`，其他引用也只能使用同一 `<SERVER>_MCP_*` 命名空间；例如 `sec-ops` 只能引用 `SEC_OPS_MCP_*`。每个自定义 Header 必须包含环境变量引用，它们都要在 `credential_refs` 中以准确 JSON 路径声明。literal endpoint、literal Header、跨 server/跨用途凭据、多报、漏报和路径不一致都会拒绝加载。
- Token、Authorization Header、私有 endpoint 等真实值只能放在私有 `docker/.env`，不得写入 Harness、镜像或 AgentGov API 环境；解析后的 link-local、保留地址和非 loopback 私网 IP literal 也会被拒绝。
- `Host`、`Connection`、`Content-Length`、`Transfer-Encoding`、`Proxy-Authorization`、`Forwarded`、`X-Forwarded-*` 等 routing/hop-by-hop Header 禁止配置；Header 名大小写重复以及解析后包含控制字符或非可见 ASCII 的 URL/Header 也会拒绝加载。
- AgentScope 2.0.8 的公共 `MCPClient` 没有按 client 关闭环境代理的配置，因此独立 Runtime 容器禁止 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 及其小写形式，Compose 也不向 Runtime 转发宿主代理。不得通过覆盖 AgentScope 私有方法规避；需要代理时应先升级到具备公共隔离配置的上游版本并重新验收。
- endpoint 路径固定为 streamable HTTP `/mcp`。容器中的明文 HTTP MCP 只允许使用 `host.docker.internal`；`localhost`、`127.0.0.1`、`::1` 仅供 local-debug，远程 MCP 必须使用 HTTPS。
- Runtime 在 Session 绑定时核对所有 `enable_tools` 均真实存在；resource facade 注册前核对所有批准的 URI/template 均由服务端公布，缺少任一项即 fail closed。
- MCP 服务必须实际校验配置的 Authorization；仅发送一个未被服务端验证的 Header 不构成认证。
- Harness 是不可变版本资产。修改 MCP 声明后必须形成新的 Agent Git 候选、完成人工审批并创建新 Session；`force` 不能绕过 MCP/manifest/subagent 审批，已有 Session 不热改配置。

## 验证

先执行离线准入，再刷新唯一 Runtime：

```bash
make runtime-bootstrap-scan
make cutover-check
make build
make up
make smoke
```

检查失败时使用 `make compose-diagnose`。诊断信息只能确认配置/连接结果，不应打印实际凭据。
