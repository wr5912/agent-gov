---
description: 生成事件处置计划和 dry-run，不执行真实动作。
allowed-tools:
  - mcp__soc-data__.*
  - mcp__security-kb__.*
  - mcp__response-orchestrator__create_response_plan
  - mcp__response-orchestrator__dry_run_action
---

为 `$ARGUMENTS` 生成处置计划，必须包含影响范围、风险、回滚和验证方法。
