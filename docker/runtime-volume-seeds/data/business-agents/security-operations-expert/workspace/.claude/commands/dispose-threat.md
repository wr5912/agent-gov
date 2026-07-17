---
description: 调查威胁并准备响应处置；真实 create/manual 由 Claude 原生工具卡逐次确认，取得 instanceId 后停止。
allowed-tools:
  - Skill
  - mcp__sec-ops__*
---

针对 `$ARGUMENTS`（威胁研判结果或 response_case 标识）调用 `threat-response-disposition` 技能。

要求：

- 先查实证据与 SOC 能力，形成完整剧本并完成 dry-run。
- 临时剧本依次对 `create`、`manual` 弹出工具卡；已发布剧本只对 `manual` 弹出工具卡。
- 用户拒绝任一步即停止，不改写已展示的工具输入。
- `manual` 返回非空 `instanceId` 后只输出提交回执；不宣称执行完成或效果达成。
