---
name: agentgov-integration
description: 上层系统通过 HTTP API 集成 AgentGov 的 AgentScope 会话、原生 SSE、受管 run、HITL、反馈改进、测评和发布时使用；约束契约发现、鉴权、事实所有权、终态与废弃入口。
---

# 集成 AgentGov 运行治理底座

AgentGov 被上层业务系统通过 HTTP API 集成。本 skill 只给选择路径与硬边界；容器
`/openapi.json` 是 request/response、required、状态码、deprecated 和 SSE 事件的唯一 wire
真相源。先生成客户端类型，不在集成方手写平行 DTO 或事件枚举。

## 先做契约发现

1. 从部署方取得 Base URL；先检查 `GET /health`，再保存同一实例的 `/openapi.json` 和
   `info.version`。
2. 部署启用 API key 时，所有 `/api/*`、`/v1/*` 请求都发送
   `Authorization: Bearer <API_KEY>`；不要把 key 写入源码、日志或前端持久存储。
3. 读取 operation 的 `description`、responses、request schema 与扩展。SSE 必须从
   `x-agentgov-sse-contract` 读取传输约束；事件集合由当前 AgentScope 版本定义，客户端必须
   透传或容忍未知事件，不能维护本文副本或封闭枚举。

## 选择正确入口

- 创建 AgentScope 会话：`POST /api/runtime/sessions/`，body 提交 `agent_id`；客户端应发送
  稳定 `Idempotency-Key`，不因超时盲目创建第二个会话。
- 会话列表、状态、canonical Message 和删除：`/api/runtime/sessions*`。除创建外均按
  OpenAPI 要求同时提交 `agent_id`，防止跨 Agent 读取。
- 原生事件流：`GET /api/runtime/sessions/{session_id}/stream`。AgentGov 按原始字节转发
  AgentScope AgentEvent SSE；集成方必须容忍未知事件，不维护 Claude 或 AgentGov 私有事件枚举。
- 触发一次受管执行或提交同一 run 的 HITL 结果：`POST /api/runtime/chat/`。
- 查询运行、Trace 和精确取消：`GET /api/agent-runs/{run_id}`、
  `GET /api/agent-runs/{run_id}/trace`、`POST /api/agent-runs/{run_id}/cancel`。
- Claude SDK、`/api/agent-runtime/sdk-events`、`/v1/responses`、`/v1/conversations*`、
  `/api/chat*`、`/v1/chat/completions` 和 `/api/sessions*` 已删除，不是兼容面。

## 业务 Agent 与受管流

1. `GET /api/agent-registry` 选择 Agent。新 Agent 通过
   `POST /api/agent-registry/{agent_id}/workspace/import` 导入单顶层 `workspace/` 包；平台
   没有绕过 Git 治理的在线 Harness 写入入口。
2. 先连接该 Session 的原生 SSE，再调用 `/api/runtime/chat/`。成功响应必须同时包含
   `X-AgentGov-Run-Id`、`X-AgentGov-Session-Id`，且响应 body 的 `session_id` 与请求一致。
3. SSE 的 `REPLY_END` 表示 AgentScope reply 结束，不等于 AgentGov run 已完成。以
   `GET /api/agent-runs/{run_id}` 的受管终态为准；只有 canonical Message 持久化回执到达后
   run 才能从 `finalizing` 进入终态。
4. 同一 Session 同时只允许一个 active run。遇到 `409` 不重试并行 chat；取消必须使用
   精确 `run_id`，确认该 run 进入终态并释放 Session fence 后才能续发。

## Web HITL

1. 收到 AgentScope `REQUIRE_USER_CONFIRM` 后，按 `reply_id + tool_call.id` 渲染卡片；不要
   把工具建议直接当作平台授权规则。
2. 仍向 `/api/runtime/chat/` 提交原生 `USER_CONFIRM_RESULT`，携带同一 `session_id`、
   `reply_id` 和 `confirm_results`。它恢复原 run，不创建新 run。
3. 浏览器不得提交 `rules`。一次授权使用 `confirmation_scope=once`；用户明确选择
   “本次运行允许”时使用 `confirmation_scope=run`，由 AgentGov 从待处理 tool call 的
   AgentScope `suggested_rules` 生成仅当前 `run_id` 有效的 allow rule。
4. 拒绝、取消、重启恢复或终态都必须清理 run-scoped rule；不能把它写回已发布 Harness，
   也不能跨 run 复用。

## 反馈、测试与发布

- 以 typed `source_refs` 创建 `/api/feedback-cases`，再通过 `/api/improvements` 的四阶段
  operation 生成/确认归因、优化、执行和回归测试设计；不要提交 backend-owned run/change
  set 绑定。
- 测试内容只来自业务 Agent 精确 Git commit 的 `workspace/tests/`；运行使用
  `/api/agent-test-runs` 或 change-set test-run operation。
- 发布/回滚使用 `/api/agent-change-sets/*` 与 `/api/agent-releases/*`，并遵守 OpenAPI
  暴露的状态冲突和测试门禁。

## 不变量与验收

- 会话/消息事实来自 AgentScope Session/Message；集成方和 AgentGov 不另建并行正文副本。
- AgentScope `session_id`、AgentGov `run_id`、AgentScope `reply_id` 和 OTel `trace_id` 是不同
  生命周期的关联键，值不要求相同，也不得互相代替。
- 工具、MCP、skills、subagents、Workspace 与权限由已发布 AgentScope Harness 控制，不通过
  chat 请求接管；Harness 变更必须走候选、测试、人工确认与发布。
- 对 `4xx/5xx` 和无 terminal EOF fail closed；不要把 `404` 当空列表或把截断流当成功。
- Trace 只有观测到已结束的 `agentgov.run` root span 才算 `complete`；任意 trace JSON 存在
  不能作为反馈自动进化证据。
- 最小验收覆盖：鉴权失败、必填/空白字段、原生 SSE 正常与未知事件、首事件前失败、
  HITL requested→once/run decision→同 run 终态、刷新后 canonical Message 回放、旧 run 取消
  不影响新 run、活动会话删除 `409`、跨 Agent 越权和所有已删除入口均失败。
