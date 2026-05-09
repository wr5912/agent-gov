---
name: threat-hunting
description: 基于 IOC、TTP、行为模式或狩猎假设执行威胁狩猎，并输出查询条件、发现和后续检测建议。
allowed-tools:
  - Read
  - Grep
  - Glob
  - mcp__soc-data__.*
  - mcp__security-kb__.*
context: fork
agent: threat-hunter
---

## 步骤

1. 明确狩猎假设。
2. 拆解需要的数据源：进程、网络、身份、文件、DNS、代理、EDR。
3. 通过 MCP 查询证据。
4. 分析命中、噪声和覆盖缺口。
5. 生成可复用检测逻辑。

## 输出

- 狩猎假设
- 查询范围
- 命中结果
- 证据链
- 检测建议
- 数据覆盖缺口
