# Claude 原生业务Agent人类确认机制整改实现方案

> 文档状态：审批用工程整改方案。
> 适用范围：所有注册业务Agent（含main-agent）的 Playground / Agent Runtime 交互运行。
> 不适用范围：治理智能体 `governor`、归因/优化/执行/测试数据集治理/回归影响分析/发布治理等后台治理 job。
> 原生依据：Claude Code / Claude Agent SDK 的权限、hook、permission mode、`can_use_tool` / `canUseTool` 和 `AskUserQuestion` 是一等机制；AgentGov 只做 Web UI、等待桥接、审计投影和业务边界收敛。

## 1. 结论

当前项目已有业务流程确认、版本变更集审批、对话级确认和 `.claude/settings.json` / hook 权限控制，但还没有实现 Claude Agent SDK 原生的人类确认闭环。

本次整改目标不是自创审批系统，而是把 Claude Code CLI 中已经存在的交互确认能力，用 Claude Agent SDK 原生 `can_use_tool` 回调接到 AgentGov Web Playground：

```text
Claude 原生权限评估
  -> can_use_tool / AskUserQuestion
  -> AgentGov Web 等待桥接
  -> 用户允许 / 拒绝 / 修改后允许 / 回答澄清问题
  -> PermissionResultAllow 或 PermissionResultDeny
  -> Claude Agent SDK 继续原生执行
```

整改后的核心边界：

- 覆盖对象：所有注册业务Agent（含main-agent）。
- 排除对象：治理智能体 `governor` 和所有后台治理 job。
- 执行语义：以 Claude Code / Claude Agent SDK 原生返回值为准。
- AgentGov 数据表：只记录 Web 等待、审计、超时和 UI 状态，不定义新的 Claude 执行状态机。

## 2. 治理对象预检

| 维度 | 结论 |
| --- | --- |
| 被治理对象 | 所有注册业务Agent（含main-agent）在 Playground 运行时发起的工具使用请求和澄清问题。 |
| 治理执行者 | Claude 原生权限系统 + AgentGov Web 人类确认桥接 + 后端审计记录。 |
| 资产类型 | 执行资产（workspace 权限配置、hook、MCP 工具）、审计资产（用户输入请求记录）、数据资产（run/session/tool request 关联）。 |
| 生命周期 | Claude 原生只有允许/拒绝执行语义；AgentGov 仅记录 `waiting/resolved/cancelled` 这类 UI/审计状态。 |
| 反馈归属 | 每条确认请求必须归属到 `agent_id`、`run_id`、`session_id`、`sdk_session_id` 和可选 `tool_use_id`。 |
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
| `can_use_tool` / `canUseTool` | 工具未被自动批准时触发，执行暂停，callback 返回允许或拒绝。 | 作为唯一运行时人类确认入口。 |
| 返回类型 | Python 返回 `PermissionResultAllow(updated_input=...)` 或 `PermissionResultDeny(message=...)`。 | 不自创 `approved_modified` 等执行状态；修改后允许就是 `allow + updated_input`。 |
| `updated_permissions` | 用户选择“以后不再询问”时，可回传 `context.suggestions` 中的 `PermissionUpdate`。 | 首版不做跨 session 永久记忆；后续如做，必须复用 `updated_permissions`。 |
| `AskUserQuestion` | 作为工具触发 `canUseTool`，input 含 `questions` 数组，返回 `answers` 或 `response`。 | 前端单独渲染澄清问题卡片，但后端仍返回 `PermissionResultAllow(updated_input=...)`。 |
| Python streaming 要求 | Python `can_use_tool` 需要 streaming mode，并配置返回 `{"continue_": True}` 的 `PreToolUse` hook 保持 stream 打开。 | 仅业务 Agent stream 入口注入 SDK 必需的 keepalive hook；不把它当权限 hook。 |
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

## 5. 目标架构

```text
Playground
  -> /api/chat/stream
  -> resolve_business_profile(agent_id)
  -> ClaudeRuntime.stream(profile.category == business)
  -> ClaudeAgentOptions(can_use_tool=..., hooks=keepalive_hook)
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

## 6. 后端实现方案

### 6.1 新增服务

新增 `ClaudeUserInputService`，职责：

- 创建等待请求。
- 向当前 stream 推送 `claude_user_input_required`。
- 等待用户决策。
- 把用户决策转换为 Claude SDK 原生结果。
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
| `session_id` | 产品会话 id。 |
| `sdk_session_id` | Claude SDK session id，可为空。 |
| `tool_use_id` | SDK context 中的 tool use id，可为空。 |
| `tool_name` | `Bash`、`Write`、`Edit`、MCP tool、`AskUserQuestion` 等。 |
| `request_kind` | `tool_permission` 或 `ask_user_question`。 |
| `input_json` | 原始 tool input / question input。 |
| `context_json` | `ToolPermissionContext` 的可序列化字段，如 suggestions、display_name、description、decision_reason。 |
| `risk_json` | 后端风险分类展示信息，不参与 SDK 权限决策。 |
| `ui_status` | `waiting`、`resolved`、`cancelled`；仅 UI / 审计状态。 |
| `sdk_response_behavior` | `allow`、`deny` 或空；对应原生 PermissionResult。 |
| `decision_variant` | `as_requested`、`updated_input`、`updated_permissions`、`question_answers`、`deny_with_message`、`timeout_deny`、`client_cancelled`。 |
| `decision_payload_json` | `updated_input`、`answers`、`updated_permissions` 或拒绝原因。 |
| `decided_by` | UI 用户或系统超时。 |
| `created_at` / `expires_at` / `resolved_at` | 审计时间。 |

状态约束：

- `ui_status=waiting` 表示 AgentGov Web 仍在等待用户输入。
- `ui_status=resolved` 表示已向 SDK 返回 `PermissionResultAllow` 或 `PermissionResultDeny`。
- `ui_status=cancelled` 表示 stream 断开、进程取消或超时前被系统中止。
- 超时对 SDK 的表现是 `PermissionResultDeny(message=...)`；表中可记录 `decision_variant=timeout_deny`。

### 6.3 SDK callback

伪代码：

```python
async def can_use_tool(tool_name: str, input_data: dict, context: ToolPermissionContext):
    request = await user_input_service.create_request(
        agent_id=profile.name,
        run_id=context_run_id,
        session_id=session.session_id,
        sdk_session_id=session.sdk_session_id,
        tool_name=tool_name,
        input_data=input_data,
        context=context,
    )

    decision = await user_input_service.wait_for_decision(request.request_id)

    if decision.action == "allow":
        return PermissionResultAllow(updated_input=input_data)

    if decision.action == "allow_modified":
        return PermissionResultAllow(updated_input=decision.updated_input)

    if decision.action == "allow_with_updated_permissions":
        return PermissionResultAllow(
            updated_input=input_data,
            updated_permissions=decision.updated_permissions,
        )

    if decision.action == "answer_question":
        return PermissionResultAllow(updated_input=decision.updated_input)

    return PermissionResultDeny(message=decision.message)
```

实现约束：

- `allow_modified` 只能修改 `input_data`，不能改 `tool_name`。
- `updated_permissions` 只允许来自 `context.suggestions` 的子集；首版 UI 默认不开放“总是允许”。
- `AskUserQuestion` 的 `updated_input` 必须包含原始 `questions` 和 `answers` 或 `response`。
- 所有输入输出进入 DB 前做 JSON 序列化校验和敏感字段脱敏。

### 6.4 Python streaming keepalive hook

官方文档要求 Python `can_use_tool` 在 streaming mode 下配合 `PreToolUse` hook 保持 stream 打开。

实现方式：

```python
async def keepalive_pre_tool_hook(input_data, tool_use_id, context):
    return {"continue_": True}
```

接入约束：

- 该 hook 只为 SDK streaming 机制服务，不做权限决策。
- 不替代业务 workspace 中 `.claude/settings.json` 的 hook。
- 必须测试 SDK options 中注入 hook 后，workspace 原有 PreToolUse shell hook 仍然生效；若 SDK 行为是覆盖而非追加，必须改为把原 hook 迁移到 SDK hooks 或保留 project settings hook 的等价执行。

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

### 6.6 API 契约

新增 API：

```text
GET /api/claude-user-input-requests?session_id=&run_id=&status=
POST /api/claude-user-input-requests/{request_id}/decision
```

`POST decision` 请求：

```json
{
  "action": "allow | deny | allow_modified | answer_question",
  "updated_input": {},
  "answers": {},
  "response": "可选自由文本",
  "message": "拒绝原因或替代建议",
  "operator": "ui"
}
```

响应：

```json
{
  "request_id": "cur-...",
  "ui_status": "resolved",
  "sdk_response_behavior": "allow",
  "decision_variant": "updated_input",
  "resolved_at": "..."
}
```

错误语义：

- `404`：请求不存在。
- `409`：请求已处理、已取消、已过期或不属于当前等待 future。
- `422`：`updated_input` 不符合当前工具输入约束，或 `answers` 不符合 `AskUserQuestion` 问题格式。

### 6.7 SSE 契约

新增事件：

```text
event: claude_user_input_required
data: {
  "request_id": "...",
  "run_id": "...",
  "session_id": "...",
  "agent_id": "...",
  "tool_name": "Bash",
  "request_kind": "tool_permission",
  "input": {...},
  "context": {...},
  "risk": {...},
  "expires_at": "..."
}
```

```text
event: claude_user_input_resolved
data: {
  "request_id": "...",
  "ui_status": "resolved",
  "sdk_response_behavior": "allow",
  "decision_variant": "as_requested"
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
- 完整 JSON 展开。
- 操作：
  - 允许一次。
  - 拒绝并填写原因。
  - 修改后允许。

澄清问题卡片：

- 识别 `tool_name == "AskUserQuestion"`。
- 渲染 `questions[]`。
- 每个问题展示 `header`、`question`、`options[]`、`multiSelect`。
- 支持自由文本作为某问题答案。
- 提交后生成：

```json
{
  "questions": [...],
  "answers": {
    "问题原文": "选项 label 或自由文本"
  }
}
```

### 7.2 不做的 UI

首版不做：

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

## 8. 测试同步矩阵

| 行为变更 | 旧测试 | 处置 | 新增测试 | 深度要求 |
| --- | --- | --- | --- | --- |
| 业务 Agent stream 接入 `can_use_tool` | `tests/test_claude_runtime.py` | 改 | 模拟 SDK tool request，断言创建 user input request 并返回 PermissionResult。 | 正常、拒绝、超时、取消。 |
| 非流式 `/api/chat` 不承载在线确认 | 无或弱覆盖 | 加 | 需确认工具时返回 deny message。 | 明确提示使用 Playground stream。 |
| governor 不接入人类确认 | 现有 Agent job 测试 | 加断言 | `AgentJobRunner` 不创建 user input request。 | 防止后台 job 卡死。 |
| `AskUserQuestion` | 无 | 加 | input questions -> UI answers -> `updated_input`。 | 单选、多选、自由文本、格式错误。 |
| MCP mutation 从伪 allow 改 ask | `tests/test_pre_tool_guard.py` | 重写 | MCP mutation 不再 hook allow；硬拒绝命令仍 deny。 | 保留安全负向测试。 |
| 前端 SSE 新事件 | 前端 stream 测试 | 改/加 | `claude_user_input_required` 渲染卡片，decision API 调用正确。 | 成功、失败、重复提交。 |
| OpenAPI 新接口 | `tests/test_openapi_export.py` | 改 | 新 path schema 出现，旧无关 approve/reject 路径不误增。 | 防漂移。 |

需要同步 `tests/coverage_policy.json`，把业务 Agent Playground 人类确认列入主流程覆盖。

## 9. 验收标准

### 9.1 后端验收

- 所有注册业务Agent（含main-agent）经 `/api/chat/stream` 运行时，未自动批准的工具请求能触发 `can_use_tool`。
- `PermissionResultAllow(updated_input=...)` 和 `PermissionResultDeny(message=...)` 行为符合 SDK 原生语义。
- `AskUserQuestion` 能从前端收集答案并返回给 Claude。
- governor 和后台治理 job 不会创建用户输入请求。
- 客户端断开、超时、重复提交都有确定性处理和审计记录。
- deny rules / PreToolUse 硬拒绝优先于人工确认。

### 9.2 前端验收

- Playground 中审批卡片跟随当前会话显示。
- 用户可以允许、拒绝、修改后允许工具请求。
- 用户可以回答 Claude 的澄清问题。
- 审批等待期间不会被 stream idle timeout 中断。
- 审批卡片不与改进治理工作台的阶段确认混淆。

### 9.3 文档与配置验收

- README / 集成指南说明业务 Agent 支持 Web HITL，治理 Agent 不走该机制。
- workspace `CLAUDE.md` 不再把“对话级确认”当唯一执行授权机制。
- `pre_tool_guard.py` 注释删除“SDK 无法呈现 ask”的旧假设。
- OpenAPI 与前端生成类型同步。

## 10. 分阶段实施

### 阶段 1：原生契约与后端骨架

- 新增 `ClaudeUserInputService`、store、record 和 API。
- 在业务 Agent stream options 接入 `can_use_tool` 和 SDK keepalive hook。
- 加后端单测覆盖 allow、deny、updated_input、timeout、cancel。
- 明确排除 `AgentJobRunner` 和 governor。

退出标准：

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

- 后端识别 `tool_name == "AskUserQuestion"`。
- 前端渲染问题卡，支持单选、多选、自由文本。
- 返回官方格式 `questions + answers` 或 `response`。

退出标准：

- `AskUserQuestion` 单选、多选、自由文本路径均通过测试。

### 阶段 4：workspace 权限收敛

- 调整业务 Agent seed settings。
- 重写 `pre_tool_guard.py` 旧假设。
- 增加运行卷 policy reconcile，保护已有业务 Agent 活配置，不覆盖无关内容。

退出标准：

- MCP mutation 不再被伪 allow。
- 硬拒绝仍优先。
- 所有注册业务Agent（含main-agent）的权限配置走同一抽象。

### 阶段 5：真实容器验收

- 重建服务并在真实容器中验证业务 Agent Playground。
- 使用一个安全 mock tool 或受控 Bash 命令触发审批。
- 验证允许、拒绝、修改后允许、澄清问题、超时取消。

退出标准：

- `make main-flow-test` 通过。
- `.venv/bin/python scripts/check_codex_governance.py --mode fail` 通过。
- 完整发布前执行 `make test`。

## 11. 风险与处理

| 风险 | 影响 | 处理 |
| --- | --- | --- |
| SDK hooks options 可能覆盖 project settings hooks | 业务 workspace 旧 hook 失效 | 先写契约测试确认合并行为；如覆盖则显式合并/迁移。 |
| `allow` 规则过宽导致不触发 `can_use_tool` | 高风险动作绕过 Web 确认 | 收敛 business settings，把 mutation 放入 ask；deny 保留硬拒绝。 |
| `dontAsk` 模式误用于 Playground | 不触发人类确认 | Playground 业务交互禁用 `dontAsk`，配置检测报警。 |
| 审批等待导致 SSE idle timeout | 用户还没决策流已断 | 等待期间发送 heartbeat。 |
| 后台治理 job 被错误接入等待 | 归因/优化/回归卡死 | 以 `profile.category == "business"` 且入口为 stream 为硬条件。 |
| “永久允许”破坏权限治理 | 用户误扩大授权 | 首版不做；后续必须只使用 `context.suggestions` 的 `updated_permissions`。 |
| UI 术语混淆 | 用户分不清工具审批和治理阶段确认 | 卡片命名为“Claude 请求使用工具”或“Claude 需要补充信息”，不使用“发布审批/改进确认”。 |

## 12. 审批决策点

本方案提交审批时，需要确认以下决策：

1. 是否同意以 Claude Code / Claude Agent SDK 原生机制为最高优先级，不自建独立审批执行语义。
2. 是否同意首版只做在线 Playground 人类确认，不做长等待审批中心。
3. 是否同意首版不提供“永久允许类似命令”，避免写入本地权限规则造成治理漂移。
4. 是否同意所有注册业务Agent（含main-agent）统一接入，不为 `main-agent` 设计特殊分支。
5. 是否同意治理智能体 `governor` 和后台治理 job 不接入 Web 人类确认。

## 13. 后续增强

首版完成并稳定后，再评估：

- 基于 Claude hook `permissionDecision: "defer"` 的长等待审批和 session resume。
- 基于 `updated_permissions` 的 session-scoped 或 localSettings-scoped 记忆授权。
- 接入企业审批系统或外部 ticket workflow。
- 更细的风险分类器和策略建议，但仍不得覆盖 Claude 原生 deny / ask / allow 语义。
