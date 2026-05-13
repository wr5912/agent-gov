---
name: incident-response
description: 生成事件响应处置计划、dry-run、回滚方案和验证步骤。该 skill 有潜在副作用，只允许用户显式调用。
disable-model-invocation: true
allowed-tools:
  - Read
  - mcp__sec-ops-data__*
context: fork
agent: incident-responder
---

## 安全约束

默认只生成计划和 dry-run，不执行生产动作。

## 必须输出

- 目标对象
- 证据依据
- 处置动作
- 影响范围
- 回滚方案
- 验证方法
- 审批提示
