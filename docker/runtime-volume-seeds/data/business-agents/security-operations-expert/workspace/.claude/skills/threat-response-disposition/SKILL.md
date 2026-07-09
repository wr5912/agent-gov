---
name: threat-response-disposition
description: 威胁响应处置闭环编排。串起处置方案、剧本、预演、审批、执行、效果评估和摘要。该 skill 有真实处置副作用，只允许用户显式调用。后续如有其他 response_type 的处置 skill，按 <type>-response-disposition 命名。
disable-model-invocation: true
allowed-tools:
  - Read
  - Grep
  - Glob
  - Skill
  - Task
  - mcp__sec-ops__*
context: fork
---

## 安全约束

- 高危处置动作必须满足四要素：证据、审批、先 dry-run、回滚方案。
- **一切 SOC 查询与执行只经 `sec-ops` MCP 工具完成（工具名以 `mcp__sec-ops__soc_api__` 开头）；严禁用 Bash / 文件系统 / 网络命令去调用、模拟或替代 SOC 动作。** 先看你可用的 `mcp__sec-ops__*` 工具列表，按工具说明选对应工具，不直连任何外部系统。
- **agent 不拆剧本、不逐个下发原子动作**：把（复用的已发布剧本或临时生成的）整本剧本交给 SOC，由 SOC 作为执行引擎运行；agent 只负责规划、选剧本/生成剧本、提交与取结果。
- 任一环节证据不足或校验不过，停在确认态，输出 `needs_human_review`，不继续推进。

## 闭环步骤

### 阶段一：响应决策
1. **接入归一化**：读取威胁研判结果，归一化处置上下文（资产、账号、实体、证据、置信度、trace 标识）。
2. **查 SOC 能力（用明确工具，别只靠 recommend）**：
   - 查**全部可复用剧本**用 `mcp__sec-ops__soc_api__list`（GET /resp/playbooks，返回全部剧本，含临时/草稿）；查某剧本详情用 `mcp__sec-ops__soc_api__get`。
   - 查**可用原子动作目录**用 `mcp__sec-ops__soc_api__list_2`（GET /resp/action-defs）；动作元数据用 `mcp__sec-ops__soc_api__list_3`（/resp/plugins）。
   - `mcp__sec-ops__soc_api__recommend` 只返回“已发布且匹配事件类型”的剧本，**常为空**；**为空不代表 SOC 不可用，必须回落到 `soc_api__list` 取全量剧本**，不要据 recommend 空判定“SOC 目录不可达/502”。
3. **生成方案**：委派 `response-playbook-planning` 产出 `disposition-plan/v1`。
4. **解析剧本**：委派 `response-playbook-builder` 复用 `soc_api__list` 里的现有剧本，或据 `soc_api__list_2` 的真实动作目录生成 `temporary-playbook/v1`。
5. **落地校验**：用 `soc_api__list_2` / `soc_api__get_1`（按 actionKey）逐条核对剧本步骤引用的原子动作真实存在、参数合法；不过则标 `needs_human_review`。
6. **预演**：调用 `playbook-dry-run` 做只读预演与风险检查。
7. **执行确认**：按 approval_policy 计算确认等级并写入方案/摘要；不得调用 `AskUserQuestion` 追加确认，运行时 Web HITL 只允许在 `soc_api__execute` 上出现。

### 阶段二：执行反馈
8. **执行**：把整本剧本经 `mcp__sec-ops__soc_api__manual`（POST /resp/instances/manual，手动触发整本剧本执行，需带 alert/事件上下文与 playbookId）提交给 SOC 执行，记录返回的 instanceId/execution_id；单动作独立执行才用 `mcp__sec-ops__soc_api__execute`（POST `/resp/actions/execute`）。只有 `soc_api__execute` 属 ask 型需 Web HITL，人审卡只允许出现在这里；其他 `sec-ops` 工具直接放行，不在 agent 侧拆步下发。
9. **接收结果**：用 `mcp__sec-ops__soc_api__get_3`（/resp/instances/{id}）、`soc_api__nodes`（实例节点任务）、`soc_api__list_5`（执行台账）按 execution_id/instanceId 取执行状态、逐步结果与结果证据，区分“执行完成”与“效果达成”。
10. **效果评估**：按成功标准判定效果；未达成则给二次响应建议。
11. **剧本入库候选**：临时剧本按业务规则确认后用 `sec-ops` 的剧本入库工具（`mcp__sec-ops__soc_api__create` 类）入库；该类工具不触发 Web HITL。
12. **过程记录与摘要**：写过程记录、外部调用、学习样本；委派 `response-playbook-summarizer` 产出 `analyst-summary/v1`。

## 输出

- 处置方案、剧本、执行结果、效果反馈、过程记录、分析师摘要。
- trace_id / actor / approval_ref / execution_id 等确定性审计标识由后端/边界层与 MCP 结果提供并在投影时贯穿附加，Agent 不编造这些字段。
