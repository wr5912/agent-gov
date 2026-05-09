---
description: 生成安全策略变更方案，不直接执行生产变更。
disable-model-invocation: true
allowed-tools:
  - mcp__security-kb__.*
  - mcp__response-orchestrator__create_response_plan
  - mcp__response-orchestrator__dry_run_action
---

为 `$ARGUMENTS` 生成安全策略变更方案，必须先 dry-run，并要求用户确认后才可执行。
