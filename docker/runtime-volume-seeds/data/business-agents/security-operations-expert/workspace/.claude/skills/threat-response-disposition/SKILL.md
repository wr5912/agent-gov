---
name: threat-response-disposition
description: 威胁响应处置编排；先完成只读调查、完整剧本与 dry-run，再通过 Claude 原生工具卡逐次确认 create/manual，并在取得 instanceId 后停止。
disable-model-invocation: false
allowed-tools:
  - Read
  - Grep
  - Glob
  - Skill
  - Task
  - mcp__sec-ops__*
---

## 安全约束

- 用户未明确要求真实处置时，只输出调查、方案与 dry-run，不调用写工具。
- 确认对象是工具卡中展示的完整输入；确认后不得修改参数，也不得拆成原子动作逐个执行。
- `create` 与 `manual` 是两次独立高风险请求，只接受当次 `allow_once` 或 `deny`；不得请求 run 级放行。
- `mcp__sec-ops__soc_api__execute` 及 update/delete/upload/cancel/rollback 始终禁止。
- 一切 SOC 查询与提交只经 `sec-ops` MCP，严禁用 Bash、文件系统或网络命令替代。
- 不调用 `AskUserQuestion` 追加并行确认；必要业务信息在发起写工具前通过正常回复向用户说明，信息不足则停止。

## 工作流

1. 归一化威胁研判结果、response_case、受影响实体和证据。
2. 可先调用 `mcp__sec-ops__soc_api__recommend`；为空时读取服务端剧本、action-defs、plugins resource/resource template。
3. 委派 `response-playbook-planning` 形成目标、成功标准、风险和影响范围。
4. 委派 `response-playbook-builder` 选择已发布剧本，或仅在内存中构建完整临时剧本。
5. 做只读结构、动作、参数、影响范围和回滚检查；失败则输出 `needs_human_review` 并停止。
6. `source=temporary`：调用 `mcp__sec-ops__soc_api__create`。只有用户允许且回执含非空 `playbookId`，才以该 ID 调用 `mcp__sec-ops__soc_api__manual`，等待第二次用户确认。
7. `source=published_reuse`：不得 create/update，直接以已核对的 `playbookId` 调用 `mcp__sec-ops__soc_api__manual`。
8. 任一确认被拒绝、工具失败或回执 ID 缺失时立即停止并如实报告。`manual` 返回非空 `instanceId` 后立即停止。

## 输出

- 写工具之前：展示证据、source、完整剧本或已发布剧本标识、风险、影响范围、dry-run 与回滚方案。
- 提交之后：只输出 `playbookId`、`instanceId` 和提交状态；不编造审计标识、执行结果或效果结论。
- 后续实例查询、效果评估和处置单关闭属于独立流程，不由本次提交结果代替。
