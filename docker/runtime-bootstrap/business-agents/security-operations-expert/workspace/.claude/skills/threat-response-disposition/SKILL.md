---
name: threat-response-disposition
description: 为 RO 只读筛选、生成或修订完整威胁响应剧本；SOC 保存、manual 执行（内含预检）和监控由 RO lifecycle worker 负责。

disable-model-invocation: false

allowed-tools:
  - Read
  - Grep
  - Glob
  - Skill
  - Task

---

## 安全约束

- phase 只能由 RO 的结构化上下文提供；缺失、未知或来自自然语言时按只读 `proposal`，任何 phase 都不能提升 SOC 权限。
- RO 已提供 `published_playbooks`、`atomic_actions` 等已查实事实并明确禁止工具时，直接使用输入事实，禁止调用 MCP、Task、Read、Grep、Glob、Bash 或其他工具。
- 用户确认对象始终是完整剧本，不拆成单个原子动作确认或执行。
- Agent 在所有阶段零副作用：禁止 `create*`、`manual`、`execute`、`update*`、`delete*`、`upload*`、`cancel*`、`rollback`、启停和预检。
- Agent 只连接 `security-operations-expert` 只读 MCP scope，不持有 RO control/monitor token。
- 不得调用 `AskUserQuestion` 追加确认；确认和执行均由 RO 的确定性状态机负责。
- 一切 SOC 查询只经 `sec-ops` MCP 完成，严禁用 Bash、文件系统或网络命令模拟或替代。

## 闭环步骤

### 只读候选阶段

1. 归一化威胁研判结果和 response_case。
2. 若 RO 输入已包含 `published_playbooks`、`atomic_actions` 等已查实事实并明确禁止工具，直接基于输入完成筛选或生成，不执行后续工具查询或子 Agent 委派。
3. 其他只读会话先调用 `mcp__sec-ops__soc_api__get_resp_playbooks_recommend`，再用剧本详情 resource template 逐个核对推荐候选。推荐候选均不合格时即视为没有对应的可复用剧本，直接依据真实 action-defs 生成临时剧本；不得为解析超大 resource 输出而改用 Bash、读取 Claude 内部 tool-results 文件或反复重试同一查询。仅当推荐接口明确不可用且剧本列表可在当前 MCP 返回中完整读取时，才读取 `openapi://soc_api/resp/playbooks` 兜底。
4. 读取 `openapi://soc_api/resp/action-defs`、`openapi://soc_api/resp/plugins` 及服务端公布的对应 resource template，核对真实原子动作、输入/输出 Schema、风险、可回滚性和目标类型；`soc_api__get_resp_playbooks` 等无参 GET 以 resource 暴露，`soc_api__get_resp_playbooks_by_playbook_id` 等带路径参数 GET 以 resource template 暴露，不得只检查 `tools/list` 就判断 SOC 目录不可达。
5. 委派 `response-playbook-planning` 形成目标、成功标准、风险和影响范围。
6. 委派 `response-playbook-builder` 选择已有剧本，或在内存中构建完整临时剧本；临时剧本此时不得保存。若当前模型不能稳定收敛子 Agent 输出，可由主 Agent 在相同只读边界内直接完成，但不得降低下列校验要求。
7. 做只读结构、动作、参数、影响范围和回滚检查；失败则输出 `needs_human_review`。
8. 输出完整结构化整本剧本候选后停止；RO 若提供结构化校验失败报告或人工调整意见，则在相同只读边界内修订并返回新候选。

### 已有剧本复用门禁

- 不能只凭推荐结果、名称或描述复用已有剧本；必须读取候选的最新详情，并逐个核对全部 ACTION 节点。
- 剧本详情的 ACTION `properties.plugin_id`（或 `actionSummary[].pluginId`）必须匹配当前 action-defs 的 `actionKey`，且动作已启用、不是 `simulated=true`；任一动作失效时立即排除该剧本，继续筛选其他候选。
- 已被 RO 反馈为未知动作、参数不合法、已停用或其他真实校验失败的剧本，不得在同一生成周期再次选择。
- 推荐候选全部排除后应立即转为临时剧本，不用 Bash、文件系统或 Claude 内部持久输出继续穷举目录。

## 输出

- 平台编排请求是机器契约：工具调用前后不得输出过程说明、分析旁白或 Markdown；完成只读查询后只输出一个紧凑 JSON object。
- `published_reuse` **只允许**三个字段：`resolution`、`selected_playbook_id`、`decision_reason`；不得重复已有剧本内容或返回 `steps`。
- `temporary` 输出 RO 契约要求的完整临时剧本和全部步骤参数；`description`、`decision_reason` 与每步 `reason` 均不得超过 500 字，不输出契约外的长篇 Schema 副本。
- `needs_human_review` 只输出 `resolution` 与不超过 500 字的 `decision_reason`。
- Agent 不预分配或编造 trace_id、approval_ref、playbookId、instanceId 或执行结果，也不输出 SOC 提交成功结论。

## 会话驱动修订

- 最新 SOC 契约已将预检合并到执行接口：RO 不单独调用预检，用户整本确认后由 RO 调用执行接口，并按“返回实例”或“内置预检拒绝且无实例”处理。本文更早出现的 `preflight` 仅代表禁止 Agent 直接调用该历史能力。
- 识别到 RO 绑定的 `response_case_id` 后，可调用 `mcp__sec-ops__response_orchestration_agent_tools__get_case_revision_context_api_v1_agent_tools_cases_case_id_context_get` 获取当前剧本、不可变基线、候选绑定及允许的修订操作；必须同时传当前 `message_provenance` 中的 `console_conversation_id` 和 `user_message_id`，该查询只读。
- 用户可以在同一 Agent 会话多轮讨论动作替换、参数调整、顺序调整或设备绑定。意图不明确时使用普通助手文本澄清，禁止用工具调用代替澄清，也不得把讨论中的建议提前提交。
- 仅当用户在当前绑定会话明确接受具体修订后，才可调用 `mcp__sec-ops__response_orchestration_agent_tools__request_case_revision_api_v1_agent_tools_cases_case_id_revisions_post`。提交内容必须逐项对应用户接受的调整，不得扩写为未确认的变化。
- `case_id`、`console_conversation_id`、`user_message_id`、`base_action_request_id`、`base_playbook_digest` 必须来自 RO/AI Console 提供的稳定结构化上下文，禁止从自然语言猜测或编造；Agent 不提交 actor、权限结论、凭据或令牌。
- 修订工具只创建新版本并触发原 Agent 会话重新规划，不代表整本剧本获批，更不能执行 SOC。新的完整剧本仍须经过 RO 唯一的整本确认；用户说“直接执行某剧本”也不能绕过该确认门。
