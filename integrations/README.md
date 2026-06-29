# AgentGov 集成分发物（integrations/）

本目录存放**发给上层业务系统**的集成辅助物，与 AgentGov 仓库自身的开发配置（`.claude/`、`.codex/`）相互独立——这里的内容面向**集成方**，不在本仓开发时触发。

## agentgov-integration/

面向用 Claude Code / Codex 或其他开发 Agent 开发的上层业务系统：把 `agentgov-integration/SKILL.md` 复制到你们项目的 `.claude/skills/agentgov-integration/SKILL.md`（或 Codex 对应 `.codex/skills/...`），集成方的开发 Agent 即可掌握集成 AgentGov 的旅程、Web HITL 确认卡机制与硬边界。

## 单一真相源

- 该 skill 派生自 [docs/AgentGov集成指南.md](../docs/AgentGov集成指南.md)（人读权威集成参考）。
- 集成契约真相源是 AgentGov 容器的 OpenAPI（`/openapi.json`、`/docs`）。
- 修改集成口径时：先改集成指南，再同步本 skill，最后以 OpenAPI 为字段/契约准绳。
