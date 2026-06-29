# Claude 原生业务Agent人类确认机制整改实现方案

> 文档状态：审批用工程整改方案，不代表当前代码已经全部实现。
> 适用范围：所有注册业务Agent（含 `main-agent`）的 Playground / Agent Runtime 交互运行。
> 不适用范围：治理智能体 `governor`、归因/优化/执行/测试数据集治理/回归影响分析/发布治理等后台治理 job。
> 原生依据：Claude Code / Claude Agent SDK 的权限、permission mode、`can_use_tool` / `canUseTool` 和 `AskUserQuestion` 是一等机制；AgentGov 只做 Web UI、等待桥接、审计投影和业务边界收敛。

## 1. 结论

当前项目已有业务流程确认、版本变更集审批、对话级确认和 `.claude/settings.json` / hook 权限控制，但还没有实现 Claude Agent SDK 原生的人类确认闭环。

本次整改目标不是自创审批系统，而是把 Claude Code CLI 中已经存在的交互确认能力，用 Claude Agent SDK 原生 `can_use_tool` 回调接到 AgentGov Web Playground：

```text
Claude 原生权限评估
  -> can_use_tool / AskUserQuestion
  -> AgentGov Web 等待桥接
  -> 用户允许一次 / 拒绝 / 回答澄清问题
  -> PermissionResultAllow 或 PermissionResultDeny
  -> Claude Agent SDK 继续原生执行
```

整改后的核心边界：

- 覆盖对象：所有注册业务Agent（含 `main-agent`）。
- 排除对象：治理智能体 `governor` 和所有后台治理 job。
- 执行语义：以 Claude Code / Claude Agent SDK 原生返回值为准。
- AgentGov 数据表：只记录 Web 等待、审计、超时和 UI 状态，不定义新的 Claude 执行状态机。
- 首版不支持修改工具参数：工具权限确认只允许 `allow_once` 或 `deny`。
- `AskUserQuestion` 允许选择 Claude 给出的选项，也允许输入“其他”自然语言回答；该回答等效于 Claude Code 的 `type something`，不是修改工具参数。

## 2. 治理对象预检

| 维度 | 结论 |
| --- | --- |
| 被治理对象 | 所有注册业务Agent（含 `main-agent`）在 Playground 运行时发起的工具使用请求和澄清问题。 |
| 治理执行者 | Claude 原生权限系统 + AgentGov Web 人类确认桥接 + 后端审计记录。 |
| 资产类型 | 执行资产（workspace 权限配置、hook、MCP 工具）、审计资产（用户输入请求记录）、数据资产（run/session/tool request 关联）。 |
| 生命周期 | Claude 原生只有允许/拒绝执行语义；AgentGov 仅记录 `waiting/resolved/cancelled/expired` 这类 UI/审计状态。 |
| 反馈归属 | 每条确认请求必须归属到 `agent_id`、`run_id`、`api_session_id`、可空 `sdk_session_id` 和可选 `tool_use_id`。 |
| 当前实现边界 | `ClaudeRuntime._build_options()` 未传 `can_use_tool`；前端 SSE 未处理用户输入请求；seed workspace 仍含对话级确认和部分 MCP mutation 放行策略。 |
| 目标能力边界 | 在线 Playground 支持业务 Agent 工具审批和 `AskUserQuestion` 澄清；不做长等待审批中心，不改变后台治理 job。 |

闭环链路：

```text
所有注册业务Agent（含main-agent）
  -> Playground 运行
  -> Claude 原生权限评估
  -> can_use_tool / AskUserQuestion
  -> Web 人类输入
  -> 原生 PermissionResult
  -> tool result / assistant response
  -> run/session/tool audit
```

风险自检：

- 不把 `main-agent` 写成特殊对象；它只是注册业务Agent之一。
- 不把治理 Agent 的发布审批、改进阶段确认、回归门禁确认混成 Claude tool approval。
- 不把 AgentGov 的审计状态误写成 Claude SDK 生命周期。
- 不通过 `ChatRequest.allowed_tools` / `disallowed_tools` / `permission_mode` 重建一套权限入口；继续以 `.claude/settings.json` 和 SDK options 为准。

## 3. Claude 原生机制核查

官方文档给出的关键机制如下：

| 原生机制 | 官方语义 | AgentGov 整改口径 |
| --- | --- | --- |
| `can_use_tool` / `canUseTool` | 工具未被自动批准时触发，执行暂停，callback 返回允许或拒绝。 | 作为唯一运行时工具权限确认入口。 |
| 返回类型 | Python 返回 `PermissionResultAllow(...)` 或 `PermissionResultDeny(message=...)`。 | 工具权限确认首版只返回允许一次或拒绝；不自创执行状态。 |
| `updated_input` | 可用于 `AskUserQuestion` 回答，也可用于修改工具输入。 | 首版仅用于 `AskUserQuestion` 的回答载体，不用于修改真实工具参数。 |
| `updated_permissions` | 用户选择“以后不再询问”时，可回传 `context.suggestions` 中的 `PermissionUpdate`。 | 首版不做跨 session 永久记忆；后续如做，必须复用 `updated_permissions`。 |
| `AskUserQuestion` | 作为工具触发 `canUseTool`，input 含 `questions` 数组，返回 `answers` 或 `response`。 | 前端单独渲染澄清问题卡片，允许 option 选择和“其他”自由文本。 |
| 权限评估顺序 | Hooks -> deny rules -> ask rules -> permission mode -> allow rules -> `canUseTool`。 | 硬拒绝放在 deny rules / PreToolUse；人类确认只处理原生流程落到 `can_use_tool` 的请求。 |
| `dontAsk` | 不提示，未预批准工具直接拒绝，`canUseTool` 不会被调用。 | Playground 业务交互默认不能使用 `dontAsk`。 |
| `defer` | 属于 `PreToolUse` hook 的 `permissionDecision: "defer"`，用于结束 query 后恢复。 | 首版不做长等待；不得把 `defer` 伪装成 `can_use_tool` 返回值。 |

官方参考：

- Claude Code Docs: [Handle approvals and user input](https://code.claude.com/docs/en/agent-sdk/user-input)
- Claude Code Docs: [Configure permissions](https://code.claude.com/docs/en/agent-sdk/permissions)
- Claude Code Docs: [Intercept and control agent behavior with hooks](https://code.claude.com/docs/en/agent-sdk/hooks)

本地当前依赖核查：

- `.venv` 中 `claude-agent-sdk` 版本为 `0.2.95`。
- `ClaudeAgentOptions` 支持 `can_use_tool`、`hooks`、`permission_mode`、`allowed_tools`、`disallowed_tools`、`setting_sources`、`skills` 等参数。
- 本地 Python `PermissionResultAllow` 字段为 `updated_input`、`updated_permissions`；`PermissionResultDeny` 字段为 `message`、`interrupt`。

## 4. 当前项目差距

### 4.1 后端差距

- `app/runtime/claude_runtime.py` 的 `ClaudeRuntime._build_options()` 未传 `can_use_tool`。
- `app/runtime/agent_job_runner.py` 构造治理 job options，也未传 `can_use_tool`；这本身是正确边界，后续必须保持不接入。
- `app/runtime/policy.py` 有 `guard_tool_use()`，但未被业务运行入口挂载，只是单元级策略函数。
- `ChatRequest.allowed_tools`、`disallowed_tools`、`permission_mode` 已标注为 SDK 执行废弃字段；不能重新把它们做成用户确认配置面。
- 当前没有用户输入请求表、等待器、提交决策 API、超时取消逻辑和 run/session/tool 关联审计。

### 4.2 前端差距

- `frontend/src/api/runtime.ts` SSE 只处理 `session/message/result/error/done`。
- ChatPanel 没有 `claude_user_input_required` 类型事件处理。
- 当前“确认归因 / 确认方案 / 确认执行 / 强制发布确认”属于 AgentGov 业务治理流程，不是 Claude SDK tool approval。
- Trace 抽屉可以展示 tool/hook 历史，但没有阻塞等待和决策提交能力。

### 4.3 workspace 权限差距

- 业务 Agent seed workspace 的 `CLAUDE.md` 仍包含“对话级确认后执行”的提示词策略。
- `pre_tool_guard.py` 注释和测试仍基于旧假设：非交互后端无法呈现 `ask`。
- 部分 MCP mutation 通过 settings / hook 被放行，可能导致需要人类确认的动作不会进入 `can_use_tool`。
- 真实旧运行卷不会因 seed 更新自动修复，必须覆盖 `${HOME}/volume-agent-gov/data/business-agents/*` 中已存在的业务 Agent。

## 5. 目标架构

```text
Playground
  -> /api/chat/stream
  -> resolve_business_profile(agent_id)
  -> ClaudeRuntime.stream(profile.category == business)
  -> event_queue + sdk_query_task + pending_decisions
  -> ClaudeAgentOptions(can_use_tool=...)
  -> Claude 原生权限评估
  -> can_use_tool(tool_name, input_data, context)
  -> ClaudeUserInputService.create_waiting_request(...)
  -> SSE: claude_user_input_required
  -> UI 决策 / 回答
  -> POST /api/claude-user-input-requests/{id}/decision
  -> PermissionResultAllow / PermissionResultDeny
  -> Claude Agent SDK 继续执行
```

明确排除链路：

```text
governor / AgentJobRunner.run_profile_json
  -> ClaudeAgentOptions(...)
  -> 不挂 can_use_tool
  -> 继续走确定性权限、formatter、状态机和发布门禁
```

### 5.1 流式并发模型

当前 `ClaudeRuntime.stream()` 是一个 async generator，直接消费 SDK `query()`。如果 `can_use_tool` 在 SDK 内部等待用户决策，而 generator 同时负责向客户端 yield SSE，就会死锁：SDK 等决策，SSE 又等 SDK 产出下一条消息。

目标实现必须改成并发模型：

- `/api/chat/stream` 启动时创建 `run_id`、`event_queue` 和 `pending_decisions`。
- SDK `query()` 在后台 task 中运行。
- StreamingResponse 的 generator 只负责持续 drain `event_queue`，并定时发送 heartbeat。
- SDK 产生的普通 message/result/error/done 事件放入 `event_queue`。
- `can_use_tool` 回调创建 pending request 后，把 `claude_user_input_required` 放入 `event_queue`，再等待对应 decision future。
- 决策 API resolve future 后，`can_use_tool` 返回 Claude SDK 原生 PermissionResult。
- 客户端断开时取消 SDK task，并把所有 pending request 标记为 `cancelled` 或 `client_disconnected`，future 统一 resolve 为 deny。

### 5.2 `/api/chat` 非流式模式

`/api/chat` 是 non-interactive safe mode，不承载在线人工确认。

规则：

- 已由 workspace 明确允许的安全能力可以继续。
- 工具权限落到 `ask` 时，不自动采用推荐项，不自动允许，返回 `NEEDS_PERMISSION_CONFIRMATION`。
- `AskUserQuestion` 属于信息澄清，不等同于工具授权；默认返回 `NEEDS_USER_ANSWER`。
- 如后续启用显式策略 `auto_use_recommended_answer`，仅当 `AskUserQuestion` 有唯一推荐答案、该答案无需自由文本、且不会直接授权高风险工具时，才可自动采用推荐答案继续。
- 后续 Claude 因推荐答案触发工具 permission ask 时，仍然不能自动允许。
- 禁止把 `ask` 自动升级成 `allow`，禁止默认使用 `bypassPermissions`。

## 6. 后端实现方案

### 6.1 新增服务

新增 `ClaudeUserInputService`，职责：

- 创建等待请求。
- 将 `claude_user_input_required` 放入当前 stream 的 `event_queue`。
- 等待用户决策或回答。
- 把用户输入转换为 Claude SDK 原生结果。
- 记录审计。
- 处理超时、客户端断开和重复提交。

建议文件：

- `app/runtime/claude_user_input_service.py`
- `app/runtime/records/claude_user_input_records.py`
- `app/runtime/stores/claude_user_input_store.py`
- `app/routers/claude_user_input.py`

### 6.2 数据模型

新增表建议名：`claude_user_input_requests`。

该表不是 Claude SDK 生命周期表，只是 AgentGov 审计与 UI 等待投影。

| 字段 | 说明 |
| --- | --- |
| `request_id` | AgentGov 用户输入请求 id。 |
| `agent_id` | 所属注册业务Agent。 |
| `run_id` | 本次运行 id。 |
| `api_session_id` | AgentGov 产品会话 id。 |
| `sdk_session_id` | Claude SDK session id，可为空；创建等待请求后拿到 SDK session 时回填。 |
| `tool_use_id` | SDK context 中的 tool use id，可为空。 |
| `request_type` | `tool_permission` 或 `ask_user_question`。 |
| `tool_name` | `Bash`、`Write`、`Edit`、MCP tool、`AskUserQuestion` 等。 |
| `redacted_input_json` | 脱敏后的 tool input / question input 快照。 |
| `context_json` | `ToolPermissionContext` 的可序列化字段，如 suggestions、display_name、description、decision_reason。 |
| `risk_json` | 后端风险分类展示信息，不参与 SDK 权限决策。 |
| `status` | `waiting`、`resolved`、`cancelled`、`expired`。 |
| `decision` | `allow_once`、`deny`、`answer_question`、`timeout_deny`、`client_cancelled`。 |
| `decision_payload_json` | `AskUserQuestion` 答案或拒绝原因；不保存修改后的工具参数。 |
| `decided_by` | 服务端从认证上下文推导的操作者；不信任 request body。 |
| `created_at` / `expires_at` / `resolved_at` | 审计时间。 |

敏感数据策略：

- DB 只持久化 redacted snapshot，不持久化 raw tool input。
- raw tool input 只允许保存在内存 pending request 中，用于当前 SDK callback 等待期间。
- 对 Bash 命令、文件内容、MCP header、token-like 字段、长文本统一脱敏和长度截断。
- `sdk_session_id` 不是创建 pending request 的硬前提；主关联使用 `api_session_id + run_id + agent_id`。

### 6.3 SDK callback

伪代码：

```python
async def can_use_tool(tool_name: str, input_data: dict, context: ToolPermissionContext):
    request = await user_input_service.create_request(
        agent_id=profile.name,
        run_id=context_run_id,
        api_session_id=session.session_id,
        sdk_session_id=session.sdk_session_id_or_none,
        tool_name=tool_name,
        input_data=input_data,
        context=context,
    )

    decision = await user_input_service.wait_for_decision(request.request_id)

    if decision.action == "allow_once":
        return PermissionResultAllow()

    if decision.action == "answer_question":
        return PermissionResultAllow(updated_input=decision.ask_user_question_input)

    return PermissionResultDeny(message=decision.message)
```

实现约束：

- v1 永远不对 `Bash`、`Write`、`Edit`、MCP 等真实工具返回 `updated_input`。
- v1 不开放 `updated_permissions`，不提供“总是允许”。
- `AskUserQuestion` 的 `updated_input` 仅作为回答载体，必须包含 SDK 要求的 `questions + answers` 或 `response`。
- 所有输入输出进入 DB 前做 JSON 序列化校验和敏感字段脱敏。
- 超时对 SDK 的表现是 `PermissionResultDeny(message=...)`；表中记录 `decision=timeout_deny`。

### 6.4 Transport heartbeat

等待人工确认时不应依赖 SDK hook 保持连接，而应由 SSE transport 层发送 heartbeat。

实现约束：

- Stream generator 定时发送 `event: heartbeat` 或等价 envelope。
- heartbeat 必须刷新前端 idle timeout。
- v1 默认不通过 `ClaudeAgentOptions.hooks` 注入 keepalive hook，避免覆盖或干扰 workspace 原生 `PreToolUse` hooks。
- 如果后续确实需要 SDK hook 事件增强 UI，再单独验证 SDK hooks 与 `.claude/settings.json` hooks 的合并语义。

### 6.5 权限配置

业务 Agent workspace 权限原则：

- `deny`：密钥、Claude root、破坏性系统路径、明确不可执行命令。
- `allow`：只读工具、明确安全的 MCP 查询、受控输出目录写入。
- `ask`：`Bash(*)`、`Edit(./**)`、`Write(./**)`、MCP mutation / disposal 工具。
- `permission_mode`：Playground 业务交互默认 `default`；规划类业务 Agent 可用 `plan`；不得用 `dontAsk` 承载需要人类确认的交互流。

需要调整：

- 移除 seed workspace 中 MCP mutation 的宽泛 allow。
- `pre_tool_guard.py` 不再把 MCP mutation 伪 allow；只做硬拒绝或补充上下文。
- 保留治理 Agent 的确定性权限边界，不让其进入 Web 等待。
- 对真实运行卷增加幂等 workspace reconcile，覆盖 `${HOME}/volume-agent-gov/data/business-agents/*`。

### 6.6 运行卷 workspace reconcile

文档和实现不能只更新 `docker/runtime-volume-seeds`。已有真实业务 Agent workspace 必须被幂等修复。

reconcile 规则：

- 扫描所有 business agent workspace。
- 识别受管 `.claude/settings.json` 和 `hooks/pre_tool_guard.py`。
- 移除宽泛 MCP mutation allow 规则。
- 移除“预确认后伪 allow”的 hook 逻辑。
- 保留 hard deny：secret、危险 bash、越界路径等。
- 对用户自定义内容先备份，再只修改受管片段。
- seed 新建业务 Agent 和旧 volume 升级业务 Agent 行为一致。
- `main-agent`、`response-disposal` 和未来新增业务 Agent 走同一 reconcile 规则，不做 `main-agent` 特判。

### 6.7 API 契约

新增 API：

```text
GET /api/claude-user-input-requests?session_id=&run_id=&status=
POST /api/claude-user-input-requests/{request_id}/decision
```

`POST decision` 请求：

```json
{
  "action": "allow_once | deny | answer_question",
  "answers": {},
  "response": "AskUserQuestion 的可选自由文本回答",
  "message": "拒绝原因或替代建议",
  "decision_token": "一次性决策令牌"
}
```

响应：

```json
{
  "request_id": "cur-...",
  "status": "resolved",
  "decision": "allow_once",
  "resolved_at": "..."
}
```

授权与审计约束：

- 从 request body 删除 `operator`，不信任客户端自报操作者。
- 后端从认证上下文推导 `decided_by`；如果当前只有 API key 级认证，则记录为 `api_key_client` 或服务端可识别的 key id。
- SSE 下发 pending request 时附带一次性 `decision_token`。
- 决策 API 必须校验 `request_id + run_id/session_id + agent_id + decision_token`。
- 每个 pending request 只能决策一次；重复提交返回当前最终状态，不重复 resolve SDK future。

错误语义：

- `404`：请求不存在。
- `409`：请求已处理、已取消、已过期或不属于当前等待 future。
- `422`：`answers` 不符合 `AskUserQuestion` 问题格式，或 action 与 request type 不匹配。

### 6.8 SSE 契约

新增事件：

```text
event: claude_user_input_required
data: {
  "request_id": "...",
  "decision_token": "...",
  "run_id": "...",
  "session_id": "...",
  "agent_id": "...",
  "request_type": "tool_permission",
  "tool_name": "Bash",
  "redacted_input": {...},
  "context": {...},
  "risk": {...},
  "expires_at": "..."
}
```

```text
event: claude_user_input_resolved
data: {
  "request_id": "...",
  "status": "resolved",
  "decision": "allow_once"
}
```

保留现有 `session/message/result/error/done` 事件。

如果等待期间没有 assistant token 输出，后端必须发送 heartbeat，避免前端 180 秒 idle timeout 误杀有效审批等待。

## 7. 前端实现方案

### 7.1 ChatPanel 交互

在当前 assistant message 下渲染用户输入卡片。

工具审批卡片：

- 工具名。
- 风险等级和原因。
- 参数摘要。
- 完整 JSON 展开，但仅展示脱敏快照。
- 操作：
  - 允许一次。
  - 拒绝并填写原因。

澄清问题卡片：

- 识别 `tool_name == "AskUserQuestion"` 或 `request_type == "ask_user_question"`。
- 渲染 `questions[]`。
- 每个问题展示 `header`、`question`、`options[]`、`multiSelect`。
- 支持 option 选择。
- 支持“其他”自由文本输入；语义等效于 Claude Code 的 `type something`。
- 提交后生成 SDK 要求的 `questions + answers` 或 `response`。

### 7.2 不做的 UI

首版不做：

- 修改工具参数。
- “永久允许类似命令”。
- 离线审批中心。
- 跨 session 规则记忆。
- 自定义任意多步骤表单。

后续如做“永久允许”，只能基于 SDK `updated_permissions` 和 `context.suggestions`，不得绕过 Claude 原生权限规则。

### 7.3 与现有确认 UI 的边界

保持现有业务治理确认不变：

- 改进阶段确认：反馈整理、归因分析、优化执行、测试发布。
- 版本变更集审批：approve/reject/publish。
- 发布门禁和强制发布确认。

新增 Claude 用户输入卡片只出现在 Playground 对话流，不进入改进治理工作台阶段确认卡。

### 7.4 Runtime 设置清理

- 隐藏或删除 Playground 当前 per-request `allowed_tools`、`disallowed_tools`、`permission_mode` 控件。
- 如需保留展示，只能做只读说明：权限来自业务 Agent workspace。
- 前端所有等待、成功、拒绝、过期、断线状态都必须有可见反馈，不只写入 stream log。

## 8. 测试同步矩阵

| 行为变更 | 旧测试 | 处置 | 新增测试 | 深度要求 |
| --- | --- | --- | --- | --- |
| 业务 Agent stream 接入 `can_use_tool` | `tests/test_claude_runtime.py` | 改 | 模拟 SDK tool request，断言创建 user input request 并返回 PermissionResult。 | 正常、拒绝、超时、取消。 |
| stream 改为 SDK task + event queue | 弱覆盖 | 加 | SDK callback 等待期间 SSE 仍能发出 `claude_user_input_required` 和 heartbeat。 | 防死锁。 |
| 非流式 `/api/chat` 不承载在线权限确认 | 无或弱覆盖 | 加 | 工具权限 ask 返回 `NEEDS_PERMISSION_CONFIRMATION`。 | 不挂起、不自动允许。 |
| 非流式 `/api/chat` 的 `AskUserQuestion` | 无 | 加 | 默认返回 `NEEDS_USER_ANSWER`；显式策略下唯一推荐答案可继续。 | 不等同工具授权。 |
| governor 不接入人类确认 | 现有 Agent job 测试 | 加断言 | `AgentJobRunner` 不创建 user input request。 | 防止后台 job 卡死。 |
| `AskUserQuestion` | 无 | 加 | input questions -> UI answers / response -> `updated_input`。 | 单选、多选、自由文本、格式错误。 |
| 不支持修改工具参数 | 无 | 加 | decision API 拒绝 `updated_input` / `allow_modified`。 | Bash/Write/MCP 都不能改参执行。 |
| MCP mutation 从伪 allow 改 ask | `tests/test_pre_tool_guard.py` | 重写 | MCP mutation 不再 hook allow；硬拒绝命令仍 deny。 | 保留安全负向测试。 |
| 真实运行卷 reconcile | 无或弱覆盖 | 加 | 旧 main-agent / response-disposal volume 升级后移除宽泛 allow 和伪 allow hook。 | 幂等、保护用户片段。 |
| 前端 SSE 新事件 | 前端 stream 测试 | 改/加 | `claude_user_input_required` 渲染卡片，decision API 调用正确。 | 成功、失败、重复提交。 |
| OpenAPI 新接口 | `tests/test_openapi_export.py` | 改 | 新 path schema 出现，旧无关 approve/reject 路径不误增。 | 防漂移。 |

需要同步 `tests/coverage_policy.json`，把业务 Agent Playground 人类确认列入主流程覆盖。

## 9. 验收标准

### 9.1 后端验收

- 所有注册业务Agent（含 `main-agent`）经 `/api/chat/stream` 运行时，未自动批准的工具请求能触发 `can_use_tool`。
- `PermissionResultAllow()` 和 `PermissionResultDeny(message=...)` 行为符合 SDK 原生语义。
- `AskUserQuestion` 能从前端收集 option 或自由文本答案，并返回给 Claude。
- `/api/chat` 非流式遇到工具权限 ask 时快速返回 `NEEDS_PERMISSION_CONFIRMATION`，不挂起、不自动允许。
- `/api/chat` 非流式遇到澄清问题时默认返回 `NEEDS_USER_ANSWER`；只有显式策略允许时才可采用唯一推荐答案。
- governor 和后台治理 job 不会创建用户输入请求。
- 客户端断开、超时、重复提交都有确定性处理和审计记录。
- deny rules / PreToolUse 硬拒绝优先于人工确认。

### 9.2 前端验收

- Playground 中审批卡片跟随当前会话显示。
- 用户可以允许一次或拒绝工具请求。
- 用户不能在工具审批卡片里修改工具参数。
- 用户可以回答 Claude 的澄清问题，包括选择 option 和输入“其他”自由文本。
- 审批等待期间不会被 stream idle timeout 中断。
- 审批卡片不与改进治理工作台的阶段确认混淆。

### 9.3 文档与配置验收

- README / 集成指南说明业务 Agent 支持 Web HITL，治理 Agent 不走该机制。
- workspace `CLAUDE.md` 不再把“对话级确认”当唯一执行授权机制。
- `pre_tool_guard.py` 注释删除“SDK 无法呈现 ask”的旧假设。
- OpenAPI 与前端生成类型同步。
- seed 和 `${HOME}/volume-agent-gov` 旧运行卷都完成验收；不得用 fresh seed 结果声明旧运行卷通过。

## 10. 分阶段实施

### 阶段 1：真实 SDK 探针与后端骨架

- 在真实容器内用当前 `claude-agent-sdk` 跑最小交互探针。
- 验证 `can_use_tool` 会被触发。
- 验证 callback 等待期间 SSE heartbeat 不断。
- 验证 `PermissionResultAllow()` 后 SDK 继续执行。
- 验证 `PermissionResultDeny(...)` 后 SDK 收到拒绝原因。
- 新增 `ClaudeUserInputService`、store、record 和 API。
- 在业务 Agent stream options 接入 `can_use_tool`。
- 加后端单测覆盖 allow、deny、timeout、cancel。
- 明确排除 `AgentJobRunner` 和 governor。

退出标准：

- 真实 SDK 探针证明 callback + SSE 并发模型成立。
- 后端 monkeypatch SDK 测试可完整跑通 `can_use_tool` 等待与决策。
- 治理 job 测试证明不会进入等待。

### 阶段 2：前端 Playground 卡片

- 扩展 SSE parser 和 stream handler。
- ChatPanel 渲染 tool approval card。
- 实现 decision API 调用。
- 补前端测试和 build。

退出标准：

- 模拟 SSE 可看到卡片并提交决策。
- `pnpm --dir frontend build` 通过。

### 阶段 3：AskUserQuestion

- 后端识别 `tool_name == "AskUserQuestion"` 或 SDK 等价 request type。
- 前端渲染问题卡，支持单选、多选、自由文本。
- 返回官方格式 `questions + answers` 或 `response`。
- 非流式默认返回 `NEEDS_USER_ANSWER`；显式策略下可采用唯一推荐答案。

退出标准：

- `AskUserQuestion` 单选、多选、自由文本路径均通过测试。
- “其他”自由文本路径与 Claude Code `type something` 语义一致。

### 阶段 4：workspace 权限收敛

- 调整业务 Agent seed settings。
- 重写 `pre_tool_guard.py` 旧假设。
- 增加运行卷 policy reconcile，保护已有业务 Agent 活配置，不覆盖无关内容。

退出标准：

- MCP mutation 不再被伪 allow。
- 硬拒绝仍优先。
- 所有注册业务Agent（含 `main-agent`）的权限配置走同一抽象。
- 旧 volume 和 fresh seed 行为一致。

### 阶段 5：真实容器验收

- 重建服务并在真实容器中验证业务 Agent Playground。
- 使用一个安全 mock tool 或受控 Bash 命令触发审批。
- 验证允许一次、拒绝、澄清问题、超时取消。

退出标准：

- `make main-flow-test` 通过。
- `.venv/bin/python scripts/check_codex_governance.py --mode fail` 通过。
- 完整发布前执行 `make test`。

## 11. 风险与处理

| 风险 | 影响 | 处理 |
| --- | --- | --- |
| stream generator 与 SDK callback 互相等待 | 核心链路死锁 | 使用 SDK 后台 task + event queue + decision future。 |
| SDK hooks options 可能覆盖 project settings hooks | 业务 workspace 旧 hook 失效 | v1 heartbeat 放在 SSE transport 层，不注入 SDK keepalive hook；后续单独验证合并语义。 |
| `allow` 规则过宽导致不触发 `can_use_tool` | 高风险动作绕过 Web 确认 | 收敛 business settings，把 mutation 放入 ask；deny 保留硬拒绝。 |
| `dontAsk` 模式误用于 Playground | 不触发人类确认 | Playground 业务交互禁用 `dontAsk`，配置检测报警。 |
| 审批等待导致 SSE idle timeout | 用户还没决策流已断 | 等待期间发送 transport heartbeat。 |
| 后台治理 job 被错误接入等待 | 归因/优化/回归卡死 | 以 `profile.category == "business"` 且入口为 stream 为硬条件。 |
| 客户端伪造操作者或决策他人请求 | 审计失真或越权执行 | 不信任 body operator；使用 decision token 和 session/run/agent 绑定。 |
| raw tool input 入库 | 泄漏密钥、命令或文件内容 | DB 只存 redacted snapshot，raw input 只存在内存 pending request。 |
| 用户修改工具参数绕过策略 | Bash/Write/MCP 参数变形执行 | v1 不支持 `allow_modified`，工具参数只读展示。 |
| UI 术语混淆 | 用户分不清工具审批和治理阶段确认 | 卡片命名为“Claude 请求使用工具”或“Claude 需要补充信息”，不使用“发布审批/改进确认”。 |

## 12. 审批决策点

本方案提交审批时，需要确认以下决策：

1. 是否同意以 Claude Code / Claude Agent SDK 原生机制为最高优先级，不自建独立审批执行语义。
2. 是否同意首版只做在线 Playground 人类确认，不做长等待审批中心。
3. 是否同意首版不提供“永久允许类似命令”，避免写入本地权限规则造成治理漂移。
4. 是否同意首版不支持修改工具参数；工具确认只允许允许一次或拒绝。
5. 是否同意 `AskUserQuestion` 的“其他”自由文本等效于 Claude Code 的 `type something`，作为回答返回 Claude。
6. 是否同意所有注册业务Agent（含 `main-agent`）统一接入，不为 `main-agent` 设计特殊分支。
7. 是否同意治理智能体 `governor` 和后台治理 job 不接入 Web 人类确认。

## 13. 后续增强

首版完成并稳定后，再评估：

- 基于 Claude hook `permissionDecision: "defer"` 的长等待审批和 session resume。
- 基于 `updated_permissions` 的 session-scoped 或 localSettings-scoped 记忆授权。
- 非流式 `AskUserQuestion` 的 `auto_use_recommended_answer` 策略和适用边界。
- 接入企业审批系统或外部 ticket workflow。
- 更细的风险分类器和策略建议，但仍不得覆盖 Claude 原生 deny / ask / allow 语义。
