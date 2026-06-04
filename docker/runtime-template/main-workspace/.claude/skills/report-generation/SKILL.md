---
name: report-generation
description: 基于模板生成安全运营日报、周报、事件报告、复盘报告或管理层汇报。
allowed-tools:
  - Read
  - Write
  - mcp__sec-ops-data__*
context: fork
agent: report-writer
---

## 步骤

1. 判断报告类型。
2. 获取模板。
3. 查询必要统计数据。
4. 生成报告草稿，并在聊天中直接返回完整内容。
5. 日报写入 `/data/outputs/reports/daily-secops-report-YYYY-MM-DD.md`。
6. 标注未获取数据和需要人工确认项。

如果 `mcp__sec-ops-data__*` 工具不可用，不要反复读取本地配置、样例或历史文件兜底；直接按模板输出“未获取”的数据缺口和需要人工确认项。
