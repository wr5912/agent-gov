---
description: 启动一次威胁响应处置闭环（响应决策 + 执行反馈），高危动作需审批、先 dry-run、带回滚。
allowed-tools:
  - Skill
  - mcp__sec-ops__*
---

针对 `$ARGUMENTS`（威胁研判结果 / response_case 标识），调用 `threat-response-disposition` 技能执行完整响应处置闭环。

要求：
- 任何执行前先查实 SOC 能力并预演。
- 高危动作满足证据、审批、dry-run、回滚四要素后才执行。
- 最终输出处置方案、执行结果、效果评估和分析师摘要。
