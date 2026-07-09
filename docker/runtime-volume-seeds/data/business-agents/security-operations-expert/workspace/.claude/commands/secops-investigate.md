---
description: 启动一次网络安全运营研判，输出事实、推断、风险、证据缺口和处置建议。
allowed-tools:
  - Skill
  - mcp__sec-ops__*
---

针对 `$ARGUMENTS`（告警、事件、资产、账号、日志摘要或调查目标），调用 `security-operations-analysis` 技能执行安全运营研判。

要求：
- 先区分事实、推断和建议动作。
- 证据不足时列缺口，不补造结论。
- 涉及真实封禁、隔离、禁用、剧本执行或生产策略变更时，只输出进入 `threat-response-disposition` 的 response_case 摘要。
