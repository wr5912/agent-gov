---
name: playbook-dry-run
description: 剧本只读预演。对待执行剧本做风险检查和影响预估，不下发任何真实动作。
allowed-tools:
  - Read
  - Grep
  - mcp__sec-ops__*
context: fork
---

## 安全约束

只读预演，严禁调用 `sec-ops` 的写工具（`mcp__sec-ops__soc_api__execute` / `manual` / `create*` / `update*` / `delete*` 等），不产生任何真实处置。

## 步骤

1. 读取待执行剧本（复用剧本或临时剧本）。
2. 用 `sec-ops` 的查询/校验工具（`mcp__sec-ops__soc_api__*` 查询/validate 类）核对每步原子动作的存在性、参数合法性、风险等级。
3. 评估：影响范围、可能的连带影响、回滚可行性、需人工确认的步骤。

## 输出

- 预演结论：可执行 / 需调整 / 需人工复核
- 逐步风险标注与整体影响范围
- 阻断性问题清单（动作不存在、参数非法、缺回滚等）
