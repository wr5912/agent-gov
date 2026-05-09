---
name: ocsf-stix-analysis
description: 分析 OCSF process_activity/network_activity 到 STIX 2.1 observed-data/SCO 图的映射，用于威胁建模和图谱构建。
allowed-tools:
  - Read
  - Grep
  - Glob
  - mcp__security-kb__.*
---

## 映射原则

- OCSF 只作为输入事件标准，不长期保存原始 OCSF。
- 每个事件映射为一个 STIX `observed-data` 加一组 SCO 对象。
- 重点表达：什么时候、什么终端、什么进程、产生了什么行为。
- 映射失败只记录错误摘要进入死信队列，不保存完整原始事件。

## 输出

- OCSF 字段路径
- STIX 对象和字段
- 图谱节点/边建议
- 映射缺口
- 死信队列错误结构建议
