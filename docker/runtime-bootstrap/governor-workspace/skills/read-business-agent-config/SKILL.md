---
name: read-business-agent-config
description: 归因/优化时通过受控工具读取目标业务 Agent 的非敏感 Harness（AGENT.md/agent.yaml/mcp/*.json/skills/subagents），核对当前配置真相。
---

# 按需读取业务 Agent 配置

当本次 job 需要判断「问题是否出在目标业务 Agent 的配置上」或「优化应改哪个配置资产」时，先调用 `HarnessList()` 获取本次 run 绑定的文件清单，再用 `HarnessRead(path)` 读取相关 Harness 文件，而不是仅凭 job input 的摘要推断。目标 Agent 由 Runtime 的可信 metadata 绑定，不由模型选择。

## 路径约定

业务 Agent 的 `agent_id` 由本次 job 的输入上下文给出。其 workspace 固定在：

```
/business-agents/<agent_id>/workspace/
├── AGENT.md                      # 系统 prompt / 角色定义
├── agent.yaml                    # 权限策略、runtime_middlewares、permission_mode
├── mcp/*.json                      # MCP server 清单与连接方式
├── skills/<name>/SKILL.md  # 各 skill 的定义与正文
├── subagents/<name>/{agent.yaml,AGENT.md}        # 子 Agent 定义
└── tests/                         # 受治理回归资产（仅在任务需要时读取）
```

先调用 `HarnessList()` 摸清实际资产清单，再把返回的完整逻辑路径交给 `HarnessRead(path)`；不要猜测路径，也不要用 `Read`/`Glob`/`Grep` 访问 `/business-agents`。

## 使用原则

- **按需、最小**：只读与本次结论直接相关的配置，不必全量读整个 workspace。
- **对齐结论**：归因指向某配置资产（prompt/skill/mcp_config/settings）前，先读该资产确认，再下结论；优化的 `changes[].target` 必须指向真实存在的配置文件。
- **只读不写**：本 Agent 无写权限；需要改配置只产出 operations，由后端受治理 apply 落盘。
- **密钥**：禁止读取 `.env`、credential、token、secret 或任何 Runtime/AgentGov 数据库；凭据状态只能使用后端已脱敏的证据摘要。
