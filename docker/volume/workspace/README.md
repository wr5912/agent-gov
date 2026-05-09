# AI 智能化网络安全运营 Claude Code Volume

该目录是用于把 Claude Code 作为 Agent 后端的安全运营智能体配置骨架，面向以下场景：

- 数据查询统计
- 告警分析
- 威胁狩猎
- 处置响应
- 策略配置
- 知识检索
- 基于模板的报告生成
- OCSF/STIX 威胁建模辅助分析

## 目录结构

```text
volume/
├── workspace/                 # 项目级 Claude Code 工作区
│   ├── CLAUDE.md              # 主 Agent 指令
│   ├── agent.yaml             # 平台元配置
│   ├── .mcp.json              # 项目级 MCP 配置
│   ├── .claude/               # 项目级 settings/subagents/skills/commands/rules
│   ├── hooks/                 # Claude Code hooks
│   ├── mcp_servers/           # 示例 MCP server
│   ├── templates/             # 报告模板
│   └── docs/                  # 使用文档
├── claude-root/               # 用户级 Claude Code 配置和状态目录挂载点
└── data/                      # session/transcript/upload/output/memory 数据目录
```

## 快速使用

假设容器内挂载方式为：

```bash
-v ./volume/workspace:/workspace \
-v ./volume/claude-root:/root \
-v ./volume/data:/data
```

进入容器后：

```bash
cd /workspace
python -m pip install -r mcp_servers/requirements.txt
claude
```

在 Claude Code 中运行：

```text
/mcp
/skills
/agents
```

用于检查 MCP 服务、skills 和 subagents 是否被发现。

## 本地私有配置

```bash
cp CLAUDE.local.md.example CLAUDE.local.md
cp .claude/settings.local.json.example .claude/settings.local.json
```

然后按需设置环境变量：

```bash
export CLAUDE_WORKSPACE=/workspace
export SOC_API_URL=http://host.docker.internal:8080
export SOC_API_TOKEN=replace-me
export SECURITY_KB_API_URL=http://host.docker.internal:8090
export SECURITY_KB_API_TOKEN=replace-me
export RESPONSE_EXECUTION_ENABLED=false
```

不要把 `.env`、`CLAUDE.local.md`、`.claude/settings.local.json`、密钥或 Claude 全局状态提交到仓库。

## 内置 MCP 服务

`.mcp.json` 默认配置了四个 stdio MCP 服务：

- `soc-data`：安全运营数据查询。
- `security-kb`：知识库/SOP 查询。
- `response-orchestrator`：处置计划、dry-run、审批后执行扩展点。
- `report-template`：报告模板读取和报告文件生成。

生产落地时，应把这些示例服务替换为你本地开发或企业内部的 MCP 服务。详见：[MCP_REPLACEMENT_GUIDE.md](docs/MCP_REPLACEMENT_GUIDE.md)。

## 安全设计

- 默认防御场景。
- 生产处置默认禁止执行，仅允许计划和 dry-run。
- 高风险 MCP 写入/执行动作会触发确认。
- hooks 会记录工具调用审计摘要。
- settings 拒绝读取常见密钥和 Claude 全局状态。
