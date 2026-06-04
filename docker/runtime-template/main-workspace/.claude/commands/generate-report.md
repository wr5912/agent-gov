---
description: 基于模板生成安全运营报告。
allowed-tools:
  - Read
  - Write
  - mcp__sec-ops-data__*
---

根据 `$ARGUMENTS` 选择模板并生成报告。缺失数据必须标注“未获取”。

日报必须同时在聊天中返回完整正文，并写入 `/data/outputs/reports/daily-secops-report-YYYY-MM-DD.md`。如果 MCP 工具不可用，直接输出数据缺口，不要用 Bash、本地样例或历史文件替代实时数据。
