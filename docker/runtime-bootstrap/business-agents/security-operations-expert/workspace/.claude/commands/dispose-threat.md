---
description: 为响应编排平台只读筛选、草拟或修订完整防御响应方案。

allowed-tools:
  - Skill
---

# /dispose-threat

针对 `$ARGUMENTS` 调用 `threat-response-disposition` 技能形成完整响应方案候选。

要求：
- 只使用用户材料与平台传入的脱敏 typed context，筛选、草拟或修订完整候选方案。
- 候选只描述防御目标、业务步骤、风险、回退考虑和验证标准，不包含可直接运行的操作内容。
- Agent 不产生 SOC 副作用，不查询或编造执行结果；所有系统变更由平台负责。
