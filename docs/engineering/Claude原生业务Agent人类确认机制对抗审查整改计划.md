# Claude 原生业务Agent人类确认机制对抗审查整改计划

> 文档状态：历史整改计划；2026-07-13 起由当前运行时契约取代。本文保留决策背景，文中的 `bypassPermissions`、仅开关开启才挂 callback、Bash 全量直放和普通请求禁止 `updated_input` 的绝对表述不再代表现状。
> 生成日期：2026-06-29。
> 关联主方案：`docs/engineering/Claude原生业务Agent人类确认机制整改实现方案.md`。

当前契约：非流式权限询问全部 fail-closed；流式始终挂显式 callback，HITL 关闭时拒绝 ask；通用 Bash 进入 `ask` 且 run 授权按低风险类别隔离。`updated_input` 仍禁止普通 HITL 使用，但 RO 认证的 `approved_execution` 可用它精确批准 `soc_api__create` / `soc_api__manual` 的单次输入，且有一次性 claim 防回放。

## 1. 整改结论

本计划用于约束主方案落地时的实现口径，防止把 Claude Code / Claude Agent SDK 原生 HITL 能力改成 AgentGov 自建审批系统。

最终边界如下：

- `/api/chat` 非流式入口固定为无需询问的 direct mode：服务端传 `permission_mode="bypassPermissions"`，不挂 `can_use_tool`，不创建 Web 用户输入请求。
- `/api/chat/stream` 是唯一 Web HITL 入口：只有 `ENABLE_CLAUDE_WEB_HITL=true` 时才挂 `can_use_tool`，并通过 SSE 暴露等待事件。
- 工具权限确认首版不允许修改工具参数：用户只能允许一次或拒绝；后端和前端都不得提供 `allow_modified`。
- `AskUserQuestion` 不是工具参数修改：用户可以选择 Claude 给出的选项，也可以输入“其他”自然语言回答；该自由文本等效于 Claude Code 的 `type something`。
- stream 下的 `AskUserQuestion` 不应 fail-fast；非流式入口不承载在线问答，不能偷偷自动选择推荐项代替用户。
- `permission_prompt_tool_name` 与 `can_use_tool` 互斥；Web HITL 路径不得同时传两者。
- 服务重启后无法恢复原 SDK callback；孤儿等待请求必须取消并要求用户重新运行。

## 2. Findings 与详细修改建议

| 编号 | 严重级别 | Finding | 修改建议 | 验收锚点 |
| --- | --- | --- | --- | --- |
| F1 | Critical | `/api/chat` 与 `/api/chat/stream` 权限语义混在一起，容易让非流式调用卡在人类确认上，或让 stream 绕过确认。 | 在 `ClaudeRuntime` 中按执行入口显式构造 options：`run()` 固定 `bypassPermissions`；`stream()` 在开关开启时使用 `default + can_use_tool`。忽略请求体里的旧 `permission_mode`、`allowed_tools`、`disallowed_tools` 执行配置字段。 | 单测断言 `/api/chat` options 不含 `can_use_tool` 和 `permission_prompt_tool_name`；stream 开关 true/false 各有断言。 |
| F2 | Critical | `permission_prompt_tool_name` 与 `can_use_tool` 同时配置会被 SDK 拒绝，导致 Web HITL 一启动就失败。 | `_build_options()` 必须知道 execution mode；Web HITL 和 `/api/chat` bypass 路径都不传 `permission_prompt_tool_name`。启动日志或 health 中展示 HITL 开关和 prompt tool 处理结果。 | 构造误配环境时，HITL options 仍不含 prompt tool；测试覆盖该互斥保护。 |
| F3 | Critical | `can_use_tool` 在 SDK callback 内等待用户决策，若直接在 async generator 里运行 SDK，会造成 SSE 无法发出等待事件。 | `ClaudeRuntime.stream()` 改成后台 SDK task + `event_queue` 并发模型；SDK 普通事件、等待事件、错误、done 都写入队列；generator drain 队列并定时 heartbeat。 | 真 SDK 或 fake SDK 测试能先收到 `claude_user_input_required`，再提交决策并继续收到后续输出。 |
| F4 | Critical | 等待请求若只存在内存，服务重启后用户还能在 UI 上点击批准已经不存在的 callback。 | 新增 `claude_user_input_requests` 表记录审计投影；应用启动时把 `waiting` 请求改为 `cancelled + service_restarted`；决策 API 对非 waiting 请求返回 `409`。 | SQLite 迁移测试覆盖旧库升级；启动恢复测试覆盖 orphan waiting 被取消。 |
| F5 | Important | 工具参数修改能力会扩大权限面，用户确认可能变成用户重写命令。 | 删除或拒绝 `allow_modified`；决策 API schema 只接受 `allow_once`、`deny`、`answer_question`；工具 permission 卡片只读展示完整输入。 | API 对 `allow_modified` 或自带 `updated_input` 的工具授权请求返回 `422`；前端无可编辑工具参数入口。 |
| F6 | Important | `AskUserQuestion` 容易被误判为工具参数修改，或被非流式入口错误 fail-fast。 | 对 `tool_name == "AskUserQuestion"` 单独建 `ask_user_question` request type；UI 渲染 Claude 给出的问题和选项，并支持“其他”自由文本。后端把用户回答映射为 SDK `updated_input` 的 `answers` 或 `response`，不写回工具 input。 | 单选、多选、自由文本、“其他”回答都有测试；确认自由文本等效 Claude Code `type something`。 |
| F7 | Important | “ask 时自动采用推荐项”会隐藏用户意图，尤其在 destructive 或业务判断问题中风险高。 | stream UI 可以把 Claude 标注的默认/推荐选项预选或高亮，但提交动作仍由用户触发。非流式入口不能自动提交推荐项；如 SDK 实际要求交互，应返回结构化诊断或普通澄清输出，并提示调用方改用 stream。 | 测试断言非流式不创建用户输入请求；stream 中推荐项需要用户提交后才进入 SDK。 |
| F8 | Important | Workspace settings / hooks 中的宽泛 allow、旧“对话级确认”提示词会吞掉 Claude 原生 ask 路径。 | 收敛业务 Agent seed workspace：移除旧对话级确认口径，保留硬拒绝类 hook，把 mutation 工具交给 Claude 原生 ask。同步渲染到 `${HOME}/volume-agent-gov/data/business-agents/*`，避免旧运行卷继续失效。 | 重启真实容器后，业务 Agent workspace 中能看到新 seed；高风险工具在 stream 中触发 Web HITL，而不是静默执行。 |
| F9 | Important | 前端 SSE 只处理普通消息，无法承接等待、决策和中断状态。 | `frontend/src/api/runtime.ts` 增加 `claude_user_input_required` / `claude_user_input_resolved` 事件类型；ChatPanel 渲染阻塞卡片，提交 `POST /api/claude-user-input-requests/{id}/decision`。 | Playwright 在真实容器中验证等待卡片、允许一次、拒绝、AskUserQuestion 自由文本路径。 |
| F10 | Important | 当前前端 / Langfuse / 本地运行态 DB 会保留完整工具参数，若误当生产面使用会扩大暴露面。 | 明确这些面只服务开发调试；生产化另起 redaction policy。decision token 仍只存 hash，真实凭据不得进入仓库、提交说明、公开文档或最终回复。 | 单测断言 API/UI 返回 `input` 完整参数；仓库 env 示例和配置治理检查仍禁止真实凭据。 |
| F11 | Important | OpenAPI / 前端类型 / coverage policy 不同步会让主流程硬门形同虚设。 | 新增决策 API 后同步 schema、前端类型、`tests/coverage_policy.json`；主流程测试纳入业务 Agent Playground HITL。 | `make main-flow-test` 和 coverage policy 均包含 HITL 相关 nodeid 或 UI verification script。 |
| F12 | Important | Sidecar `/v1/responses` 若无调用方，会保留一条误导性兼容入口；若仍被外部依赖，直接删除会破坏集成。 | 先用 `rg` 和真实容器日志确认调用图。若只剩 dead code，删除路由、测试和文档，并保留迁移说明；若外部兼容仍需要，标注 deprecated、补 smoke 和移除条件。 | 删除或保留都有明确测试：无调用方时路由不存在；保留时有契约测试和文档说明。 |

## 3. 执行顺序

### 阶段 0：分支与环境隔离

- 当前整改分支使用私有 env 选择隔离端口，避免干扰 master 的默认 5XXXX 真实服务；本机私有端口不写入仓库。
- 私有 `docker/.env` 选择独立运行卷，例如 `/home/luopeng/volume-agent-gov-web-confirmation-smoke`。
- `ENABLE_CLAUDE_WEB_HITL` 默认保持 `false`；真实 HITL 验收时在私有 env 中开启。

### 阶段 1：后端入口语义收口

- 为 `ClaudeRuntime._build_options()` 增加 execution mode。
- `/api/chat` 固定 `permission_mode="bypassPermissions"`。
- `/api/chat/stream` 只在 HITL 开关开启时传 `can_use_tool`。
- Web HITL options 不传 `permission_prompt_tool_name`。
- health / runtime metadata / trace 记录 `claude_web_hitl_enabled` 与实际 permission mode。

### 阶段 2：等待请求与审计投影

- 新增 `claude_user_input_requests` 模型、store、service、router 和 SQLite 迁移。
- request id 与 decision token 分离；DB 只保存 token hash。
- 超时返回 `PermissionResultDeny`，记录为 `resolved + timeout_deny`。
- 客户端断开记录为 `cancelled + client_cancelled`。
- 服务重启把 orphan waiting 改为 `cancelled + service_restarted`。

### 阶段 3：SDK 并发桥接

- `stream()` 启动 SDK 后台 task。
- `can_use_tool` callback 创建等待请求并把 SSE 事件放入队列。
- 决策 API resolve future 后，callback 返回 `PermissionResultAllow` 或 `PermissionResultDeny`。
- `AskUserQuestion` 的用户回答只通过 `updated_input` 作为回答载体返回 Claude。

### 阶段 4：前端交互

- Runtime SSE 类型增加用户输入事件。
- ChatPanel 增加阻塞式确认卡片。
- 工具权限卡片只读展示完整输入，按钮只有“允许一次”和“拒绝”。
- `AskUserQuestion` 卡片展示选项、推荐/默认提示和“其他”自由文本输入。
- 已取消或已过期请求只显示历史状态，不允许继续提交。

### 阶段 5：Workspace seed 与运行卷修复

- 更新业务 Agent seed 中的 `CLAUDE.md`、settings 和 hook，移除旧对话级确认提示。
- 运行 workspace reconcile，覆盖既有 `${HOME}/volume-agent-gov/data/business-agents/*` 中的业务 Agent 配置。
- 保持 `governor` 和后台治理 job 不接入 Web HITL。

### 阶段 6：Sidecar 路由治理

- 审计 `/v1/responses` 的调用方、文档引用、测试和真实容器访问日志。
- 无调用方时删除；有兼容依赖时保留为 deprecated 并写明移除条件。
- 同步更新集成指南、smoke 或契约测试。

## 4. 测试与真实验收矩阵

| 验收面 | 必跑项 | 通过标准 |
| --- | --- | --- |
| 后端单测 | settings、runtime options、user input service、SQLite migration、router decision API | `/api/chat` bypass 与 stream HITL 隔离；orphan waiting 取消；无 `allow_modified`。 |
| 前端单测 | SSE parser、ChatPanel 确认卡片、AskUserQuestion 卡片 | 工具参数不可编辑；选项和“其他”都能提交。 |
| 主流程硬门 | `make main-flow-test` | coverage policy 已绑定 HITL 主流程。 |
| 治理硬门 | `git diff --check`、docs governance、codex governance、`make test` | 无新增治理债务。 |
| 真实容器 E2E | 私有 env 端口族启动 API/UI，选择业务 Agent，在 Playground 触发工具 ask 和 AskUserQuestion | UI 出现等待卡片；允许一次后工具继续；拒绝后 Claude 收到 deny；自由文本回答传回 Claude。 |
| 运行卷验证 | 检查 `${HOME}/volume-agent-gov-web-confirmation-smoke/data/business-agents` | 新 seed 已渲染，`response-disposal` 等预制业务 Agent 可在 topbar agent select 中选择并生效。 |

## 5. 不做项

- 不为首版实现长等待审批中心。
- 不跨服务重启恢复原 SDK callback。
- 不把后台治理 job 接入 Web HITL。
- 不新增 AgentGov 自建工具权限状态机。
- 不自动代表用户选择 `AskUserQuestion` 推荐项。
- 不允许用户在工具权限确认时修改 Bash、Write、Edit 或 MCP tool 参数。
