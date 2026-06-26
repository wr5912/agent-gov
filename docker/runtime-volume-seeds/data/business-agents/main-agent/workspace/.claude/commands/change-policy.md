---
description: 生成安全策略变更方案，不直接执行生产变更。
disable-model-invocation: true
allowed-tools:
  - mcp__sec-ops-data__*
---

为 `$ARGUMENTS` 生成安全策略变更方案，必须先 dry-run，并要求用户确认后才可执行。
