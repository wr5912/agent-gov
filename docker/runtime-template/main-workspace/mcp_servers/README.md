# MCP Servers

本目录提供四个示例 MCP 服务：

- `soc_data_mcp`：查询告警、资产、进程、网络连接和统计数据。
- `security_kb_mcp`：查询内部 SOP、检测规则说明和知识库。
- `response_orchestrator_mcp`：生成处置计划和 dry-run；默认禁止真实执行。
- `report_template_mcp`：读取模板并生成报告文件。

安装依赖：

```bash
cd /main-workspace
python -m pip install -r mcp_servers/requirements.txt
```

本地调试：

```bash
python mcp_servers/soc_data_mcp/server.py
```

Claude Code 中使用 `/mcp` 检查连接状态。
