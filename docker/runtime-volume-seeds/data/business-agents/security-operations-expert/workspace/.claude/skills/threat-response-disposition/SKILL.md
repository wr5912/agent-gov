---
name: threat-response-disposition
# AGENTGOV:THREAT-SKILL:START
description: 威胁响应处置的两阶段编排：由 RO 可信结构化 phase 或用户显式请求触发；proposal 只产出零副作用整本剧本提案，approved_execution 仅提交已批准剧本并在取得 instanceId 后停止。
disable-model-invocation: false
# AGENTGOV:THREAT-SKILL:END
allowed-tools:
  - Read
  - Grep
  - Glob
  - Skill
  - Task
  - mcp__sec-ops__*
---

<!-- AGENTGOV:THREAT-SKILL-BODY:START -->
## 安全约束

- phase 只能由 RO 的结构化上下文提供；缺失或未知时按 `proposal`，不得从自然语言自行提升为 `approved_execution`。
- 用户确认对象始终是完整剧本，不拆成单个原子动作确认或执行。
- `phase=proposal` 零副作用：禁止 `create*`、`manual`、`execute`、`update*`、`delete*`、`upload*`、`cancel*` 和 `rollback`。
- `phase=approved_execution` 只能使用 RO 提供的不可变批准快照；任何步骤、参数、目标或 source 漂移都必须停止。
- `mcp__sec-ops__soc_api__execute` 在本流程中始终禁止。
- `create` / `manual` 的 AgentGov ask 是 RO 内部授权握手，不是第二次用户确认；RO 对每次请求分别校验批准快照并内部 `allow_once`，禁止 run 级放行；不得调用 `AskUserQuestion`。
- 一切 SOC 查询与执行只经 `sec-ops` MCP 完成，严禁用 Bash、文件系统或网络命令模拟或替代。

## 闭环步骤

### phase=proposal

1. 归一化威胁研判结果和 response_case。
2. 可先调用 `mcp__sec-ops__soc_api__recommend`；结果为空时读取 `openapi://soc_api/resp/playbooks` 及服务端公布的剧本详情 resource template，查询全部已有剧本及详情。
3. 读取 `openapi://soc_api/resp/action-defs`、`openapi://soc_api/resp/plugins` 及服务端公布的对应 resource template，核对真实原子动作、输入/输出 Schema、风险、可回滚性和目标类型；不得因工具列表中没有 `soc_api__list/get` 就判断 SOC 目录不可达。
4. 委派 `response-playbook-planning` 形成目标、成功标准、风险和影响范围。
5. 委派 `response-playbook-builder` 选择已有剧本，或在内存中构建完整临时剧本；临时剧本此时不得保存。
6. 做只读结构、动作、参数、影响范围和回滚检查；失败则输出 `needs_human_review`。
7. 输出完整结构化整本剧本提案后立即停止，等待 RO 人工确认。

### phase=approved_execution

1. 严格核对 source、批准快照、剧本内容和执行上下文；不得自行修改已批准内容。
2. `source=temporary`：使用 RO 批准快照中预分配的 `playbook_id` 调用 `mcp__sec-ops__soc_api__create`；要求回执返回同一非空 `playbookId` 后再调用 `mcp__sec-ops__soc_api__manual`。
3. `source=published_reuse`：直接使用已批准 `playbookId` 调用 `mcp__sec-ops__soc_api__manual`，不得 create/update。
4. 只有 `manual` 返回非空 `instanceId` 才报告提交成功；否则输出 `needs_human_review`。
5. 获得 `instanceId` 后立即停止，不查询执行结果、不判效、不再次入库、不生成闭环摘要、不关闭处置单。

## 输出

- `phase=proposal`：按 RO JSON 契约输出 `resolution`；`published_reuse` 使用 `selected_playbook_id`，`temporary` 输出完整临时剧本、全部步骤与参数、动作输入/输出 Schema 摘要、风险、影响范围、回滚和验证方法。不得在临时剧本保存前编造 `playbookId`。
- `phase=approved_execution`：只输出 playbookId、instanceId、提交状态和后端提供的确定性审计标识。
- Agent 不编造 trace_id、approval_ref、playbookId、instanceId 或执行结果。
<!-- AGENTGOV:THREAT-SKILL-BODY:END -->
