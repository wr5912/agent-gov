---
name: policy-change
description: 生成安全策略配置变更方案，例如防火墙、EDR、WAF、IAM、邮件网关、检测规则。只允许用户显式调用。
disable-model-invocation: true
allowed-tools:
  - Read
  - mcp__security-kb__.*
  - mcp__response-orchestrator__create_response_plan
  - mcp__response-orchestrator__dry_run_action
context: fork
agent: policy-operator
---

## 策略变更要求

输出变更方案时必须包含：

1. 变更目标。
2. 原策略或当前状态。
3. 新策略内容。
4. 命中范围估算。
5. 误伤风险。
6. 回滚方案。
7. 验证查询。
8. 是否需要审批。
