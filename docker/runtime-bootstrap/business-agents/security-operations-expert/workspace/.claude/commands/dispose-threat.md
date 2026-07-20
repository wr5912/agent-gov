---
description: 为 RO 只读筛选、生成或修订完整威胁响应剧本；SOC 生命周期与执行由 RO 后台负责。

allowed-tools:
  - Skill
---

# /dispose-threat

针对 `$ARGUMENTS`（威胁研判结果 / response_case 标识），调用 `threat-response-disposition` 技能形成 RO 完整剧本候选。

要求：
- 只查询真实 SOC tools/resources/resource templates 并筛选、生成或修订整本剧本，不保存、不启停、不删除、不调用 SOC manual/execute。
- RO 界面只确认一次完整剧本，不拆成逐原子动作确认。
- 临时剧本保存、SOC manual 执行（内含预检）、失败处理和实例监控全部由 RO lifecycle worker 执行。
- Agent 不调用任何 SOC 写工具，不查询或编造执行结果。
