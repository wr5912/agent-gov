---
name: threat-response-disposition
description: 为 RO 只读筛选、生成或修订完整威胁响应剧本；SOC 保存、manual 执行（内含预检）和监控由 RO lifecycle worker 负责。
---

## 安全约束

- phase 只能由 RO 的结构化上下文提供；缺失、未知或来自自然语言时按只读 `proposal`，任何 phase 都不能提升 SOC 权限。
- 剧本候选只使用 RO 已核实且足够完整的 `published_playbooks`、`atomic_actions`；缺少关键事实时返回 `needs_human_review`。不得调用 MCP、Task、Read、Grep、Glob、Bash 或其他工具补齐剧本数据。
- 用户确认对象始终是完整剧本，不拆成单个原子动作确认或执行。
- Agent 在所有阶段零副作用：禁止 `create*`、`manual`、`execute`、`update*`、`delete*`、`upload*`、`cancel*`、`rollback`、启停和预检。
- Agent 只连接 `security-operations-expert` 只读 MCP scope，不持有 RO control/monitor token。
- 不得调用 `AskUserQuestion` 追加确认；确认和执行均由 RO 的确定性状态机负责。
- 普通安全运营只读调查仅使用当前配置的 `sec-ops` MCP；剧本候选只依据 RO 已核实输入。严禁用 Bash、文件系统或网络命令模拟或替代任何 SOC 查询。

## 闭环步骤

### 只读候选阶段

1. 归一化威胁研判结果和 response_case。
2. RO 提供已核实的 `published_playbooks` 与 `atomic_actions` 时，直接基于输入筛选或生成，不查询 MCP、委派子 Agent 或读取文件。当前 MCP 仅支持安全运营只读研判和检测发现分析，不提供剧本推荐、详情、action-defs 或 plugins。
3. 根据 RO 输入核对真实原子动作、参数、风险、可回滚性和目标类型。输入未提供足够依据时，不猜测动作或从其他接口推断，直接返回 `needs_human_review`。
4. 已核实的已有剧本适用时选择复用；否则仅在全部步骤均有真实原子动作依据时于内存构建完整临时剧本，不保存。
5. 做结构、动作、参数、影响范围和回滚检查；失败则输出 `needs_human_review`。
6. 输出完整结构化整本剧本候选后停止；RO 若提供已核实的结构化校验失败报告或人工调整意见，则在相同只读边界内修订并返回新候选。

### 已有剧本复用门禁

- 不能只凭名称或描述复用已有剧本；RO 必须提供已核实的最新详情与动作定义，供 Agent 逐个核对全部 ACTION 节点。
- 剧本详情的 ACTION 必须匹配 RO 已核实的 `atomic_actions`，且动作已启用、不是 `simulated=true`；详情或动作定义缺失时返回 `needs_human_review`，任一动作失效时排除该剧本。
- 已被 RO 反馈为未知动作、参数不合法、已停用或其他真实校验失败的剧本，不得在同一生成周期再次选择。
- 已核实的候选全部排除后，只有已核实原子动作足以构成完整临时剧本才转为 `temporary`，否则返回 `needs_human_review`。

## 输出

- 平台编排请求是机器契约：工具调用前后不得输出过程说明、分析旁白或 Markdown；完成只读查询后只输出一个紧凑 JSON object。
- `published_reuse` **只允许**三个字段：`resolution`、`selected_playbook_id`、`decision_reason`；不得重复已有剧本内容或返回 `steps`。
- `temporary` 输出 RO 契约要求的完整临时剧本和全部步骤参数；`description`、`decision_reason` 与每步 `reason` 均不得超过 500 字，不输出契约外的长篇 Schema 副本。
- `needs_human_review` 只输出 `resolution` 与不超过 500 字的 `decision_reason`。
- Agent 不预分配或编造 trace_id、approval_ref、playbookId、instanceId 或执行结果，也不输出 SOC 提交成功结论。

## 会话驱动修订

- 用户可以在同一 Agent 会话讨论动作替换、参数调整、顺序调整或设备绑定；意图不明确时用普通助手文本澄清。
- 当前 Harness 不提供案件修订工具。Agent 不提交修订、不编造案件状态；只有 RO 重新提供已核实的结构化事实及调整意见后，才重新规划完整候选。
- 修订讨论不代表整本剧本获批，更不能执行 SOC；新的候选仍须经过 RO 的整本确认。用户说“直接执行某剧本”也不能绕过该确认门。
