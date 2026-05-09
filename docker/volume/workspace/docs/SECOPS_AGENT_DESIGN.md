# AI 智能化网络安全运营 Agent 设计说明

## 1. 设计目标

该智能体不是单纯聊天助手，而是 SOC/SIEM/EDR/NDR/SOAR/知识库之上的操作层。核心价值：

- 把自然语言转成安全运营查询、研判、处置和报告流程。
- 把复杂工具封装为 MCP 工具，并通过 Claude Code 的 subagents/skills 编排。
- 用 settings、hooks、dry-run 和审批规则控制高风险动作。

## 2. 推荐分层

```text
用户/API
  ↓
Claude Code Agent Runtime
  ↓
CLAUDE.md 主指令 + settings 权限 + hooks 安全控制
  ↓
Skills / Commands / Subagents
  ↓
MCP 工具层
  ↓
SOC/SIEM/EDR/NDR/SOAR/知识库/报告模板
```

## 3. MVP 工具边界

MVP 阶段建议只做：

- 读：告警、资产、进程、网络、知识库、模板。
- 写：报告文件、处置计划文件。
- 不做：真实隔离、真实封禁、真实策略下发。

当读链路稳定后，再增加 dry-run 写工具，最后接入审批后的执行工具。

## 4. 风险控制

- 模型不是权限边界，MCP 服务必须自行做权限控制。
- hooks 是辅助控制，不应作为唯一防线。
- 所有生产动作要经过服务端审批校验，不只依赖模型“是否询问”。
- 原始安全数据应在数据平台侧查询和聚合，尽量不要长期进入 Agent 文件系统。
