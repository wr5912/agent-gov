---
name: read-business-agent-config
description: 归因/优化时按需读取目标业务 Agent 的 workspace 原始配置（CLAUDE.md/settings/.mcp.json/skills/.env），核对当前配置真相，避免脱离实际臆断。
allowed-tools:
  - Read
  - Glob
  - Grep
---

# 按需读取业务 Agent 配置

当本次 job 需要判断「问题是否出在目标业务 Agent 的配置上」或「优化应改哪个配置资产」时，用 Read/Glob/Grep 直接读该业务 Agent 的 workspace 原始配置，而不是仅凭 job input 的摘要推断。

## 路径约定

业务 Agent 的 `agent_id` 由本次 job 的输入上下文给出。其 workspace 固定在：

```
/data/business-agents/<agent_id>/workspace/
├── CLAUDE.md                      # 系统 prompt / 角色定义
├── .claude/settings.json          # 权限（allow/ask/deny）、hooks、defaultMode
├── .mcp.json                      # MCP server 清单与连接方式
├── .claude/skills/<name>/SKILL.md  # 各 skill 的定义与正文
├── .claude/agents/<name>.md        # 子 Agent 定义
└── .env                           # 运行环境变量（可读；含密钥时按证据引用，勿在结论里逐字回填）
```

先用 `Glob` 列 `.claude/skills/*/SKILL.md`、`.claude/agents/*.md` 摸清资产清单，再对与本次归因/优化直接相关的文件用 `Read` 读正文。

## 使用原则

- **按需、最小**：只读与本次结论直接相关的配置，不必全量读整个 workspace。
- **对齐结论**：归因指向某配置资产（prompt/skill/mcp_config/settings）前，先读该资产确认，再下结论；优化的 `changes[].target` 必须指向真实存在的配置文件。
- **只读不写**：本 Agent 无写权限；需要改配置只产出 operations，由后端受治理 apply 落盘。
- **密钥**：`.env` 可读用于判断（如占位符未解析、凭据缺失），但结论/建议里引用「存在/缺失/未解析」即可，不必逐字复制密钥值。
