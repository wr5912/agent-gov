---
description: 审查原始安全数据到 OCSF、OCSF 到 STIX 的标准化链路，并输出修正建议和回归用例。
allowed-tools:
  - Skill
  - Read
  - Grep
  - Glob
  - mcp__sec-ops-data__*
---

针对 `$ARGUMENTS`（样例文件、告警/事件 ID、OCSF 输出、STIX 输出或用户反馈），调用 `security-data-standardization-review` 技能执行完整标准化审查。

要求：
- 先确认输入范围和字段路径。
- 分别审查原始数据到 OCSF、OCSF 到 STIX。
- 输出缺陷归因、修正建议和最小回归用例。
- 不直接修改生产规则、图谱或外部系统。
