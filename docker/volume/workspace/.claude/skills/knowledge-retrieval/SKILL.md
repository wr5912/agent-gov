---
name: knowledge-retrieval
description: 检索内部安全知识库、SOP、规则说明、处置手册和历史经验。
allowed-tools:
  - Read
  - Grep
  - Glob
  - mcp__security-kb__.*
context: fork
agent: knowledge-curator
---

## 检索原则

- 优先返回内部 SOP 和经过验证的知识。
- 输出知识来源、适用条件、限制和更新时间。
- 不确定时标注“待验证”。
