---
name: report-generation
description: 基于模板生成安全运营日报、周报、事件报告、复盘报告或管理层汇报。
allowed-tools:
  - Read
  - mcp__sec-ops-data__*
context: fork
agent: report-writer
---

## 步骤

1. 判断报告类型。
2. 获取模板。
3. 查询必要统计数据。
4. 生成报告草稿。
5. 标注未获取数据和需要人工确认项。

报告输出应适合直接复制到工单、邮件或知识库。
