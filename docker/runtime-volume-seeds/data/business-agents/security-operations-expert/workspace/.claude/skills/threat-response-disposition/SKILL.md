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
2. **查 SOC 能力**：用 `sec-ops` 的 SOC 查询/推荐工具（`mcp__sec-ops__soc_api__*` 中的剧本推荐/列表/查询类，按工具说明识别）查候选剧本与可用原子动作（剧本步骤的积木）。
3. **生成方案**：委派 `response-playbook-planning` 产出 `disposition-plan/v1`。
4. **解析剧本**：委派 `response-playbook-builder` 复用已发布剧本或生成 `temporary-playbook/v1`。
5. **落地校验**：用 `sec-ops` 的剧本/动作校验工具（`mcp__sec-ops__soc_api__*` 中的 validate 类）逐条核对剧本步骤引用的原子动作真实存在、参数合法；不过则标 `needs_human_review`。
6. **预演**：调用 `playbook-dry-run` 做只读预演与风险检查。
7. **执行确认**：按 approval_policy 计算确认等级，发起人工确认（确认先于执行）。

### 阶段二：执行反馈
8. **执行**：审批通过后，把整本剧本（复用剧本标识或临时剧本步骤）经 `sec-ops` 的剧本执行工具（`mcp__sec-ops__soc_api__execute` / `mcp__sec-ops__soc_api__manual`，属 ask 型需人审）提交给 SOC 执行，记录返回的 execution_id。不在 agent 侧拆步下发。
9. **接收结果**：用 `sec-ops` 的执行状态/结果查询工具（`mcp__sec-ops__soc_api__*` 中的 running/结果查询类）按 execution_id 取执行状态、逐步结果与结果证据，区分“执行完成”与“效果达成”。
10. **效果评估**：按成功标准判定效果；未达成则给二次响应建议。
11. **剧本入库候选**：临时剧本经确认后用 `sec-ops` 的剧本入库工具（`mcp__sec-ops__soc_api__create` 类，属 ask 型需人审）入库。
12. **过程记录与摘要**：写过程记录、外部调用、学习样本；委派 `response-playbook-summarizer` 产出 `analyst-summary/v1`。

## 输出

- 处置方案、剧本、执行结果、效果反馈、过程记录、分析师摘要。
- trace_id / actor / approval_ref / execution_id 等确定性审计标识由后端/边界层与 MCP 结果提供并在投影时贯穿附加，Agent 不编造这些字段。
