---
description: 对已授权安全运营材料开展只读研判，输出事实、推断、风险、证据缺口和防御建议。
allowed-tools:
  - Skill
---

针对 `$ARGUMENTS` 调用 `security-operations-analysis` 技能执行已授权环境中的只读安全运营研判。

要求：
- 先区分事实、推断和建议动作。
- 证据不足时列缺口，不补造结论。
- 需要系统变更时，只输出进入 `threat-response-disposition` 的响应方案摘要，不提供执行内容。
