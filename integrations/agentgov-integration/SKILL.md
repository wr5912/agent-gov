---
name: agentgov-integration
description: Use when an upper-layer system integrates AgentGov through HTTP APIs for managed Agent runs, SDK-native SSE, transitional Responses projection, Conversations, Web HITL, feedback improvement, testing, and releases. Encodes contract discovery, auth, ownership, terminal handling, and deprecated-surface boundaries.
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
   `x-agentgov-sse-events` 生成解析分支，不能维护本文副本。

## 选择正确入口

- 新 managed turn 或协议 adapter：`POST /api/agent-runtime/sdk-events`。请求中的非空
  `message`、`agent_id` 都是必填字段；该流是第一方 Playground 和未来外置 OpenAI adapter
  的事实输入。
- 会话创建、列表、读取、删除和历史：`/v1/conversations*`。URL 使用
  `conversation_id`，读取历史不传 `agent_id`。
- 现有 OpenAI 风格调用方可过渡使用 `/v1/responses`，但必须检查
  `x-agentgov-contract-status` 与 `x-agentgov-known-deviations`。它不是完整 OpenAI
  Responses drop-in replacement。
- byte-exact Runtime 诊断只用 `/api/debug/agent-runtime/raw-events`；它不是业务 SSE。
- `/api/chat`、`/api/chat/stream`、`/v1/chat/completions`、`/api/sessions*` 均是
  deprecated 兼容面。不要为新系统建立依赖；Sessions 计划在下一次确认的破坏性版本删除。

## 业务 Agent 与受管流

1. `GET /api/agent-registry` 选择 Agent。新 Agent 通过
   `POST /api/agent-registry/{agent_id}/workspace/import` 导入单顶层 `workspace/` 包；平台
   没有通用模板创建 API。
2. 建立 SDK-native SSE，机械保留 `claude.sdk.*`，并处理 OpenAPI 声明的
   `agentgov.session`、confirmation、result、error、done 等控制事件。
3. `agentgov.result` 只是 SDK ResultMessage 到达；只有 `agentgov.done` 表示 managed turn
   已持久化收口。HTTP `200` 后若 EOF 前没有 `agentgov.done`，按运行失败处理。
4. 当前 session admission 可能晚于 SSE headers。不要因 HTTP `200` 自动重试或记成功；
   先按 `run_id`/conversation 事实判断是否已创建运行，避免重复副作用。

## Web HITL

1. 收到 `agentgov.confirmation.requested` 后保持原 SSE，按 `request_id` 渲染卡片，把
   `decision_token` 只放内存。
2. 提交
   `POST /v1/agentgov/confirmation-requests/{request_id}/decision`。Bearer API key 与一次性
   `decision_token` 都必需；body 只使用 OpenAPI 声明字段。
3. 需要刷新等待态时，只调用 `GET /api/claude-user-input-requests`；仅精确
   `run_id + status=waiting` 查询可能返回 token，宽泛列表不返回。
4. 收到 `agentgov.confirmation.resolved` 更新原卡片。断流或 token 丢失时标为中断，不伪造
   决策、不探测历史 HITL aliases。

## 反馈、测试与发布

- 以 typed `source_refs` 创建 `/api/feedback-cases`，再通过 `/api/improvements` 的四阶段
  operation 生成/确认归因、优化、执行和回归测试设计；不要提交 backend-owned run/change
  set 绑定。
- 测试内容只来自业务 Agent 精确 Git commit 的 `workspace/tests/`；运行使用
  `/api/agent-test-runs` 或 change-set test-run operation。
- 发布/回滚使用 `/api/agent-change-sets/*` 与 `/api/agent-releases/*`，并遵守 OpenAPI
  暴露的状态冲突和测试门禁。

## 不变量与验收

- 会话/消息事实来自 Agent/SDK transcript；集成方不另建并行消息副本。
- 工具、MCP、skills、subagents、hooks 与权限由业务 Agent Workspace 配置，不通过请求参数接管。
- 对 `4xx/5xx` 和无 terminal EOF fail closed；不要把 `404` 当空列表或把截断流当成功。
- 最小验收覆盖：鉴权失败、必填/空白字段、SDK-native 正常终态、首事件前失败、HITL
  requested→decision→resolved→done、conversation 刷新回放、活动会话删除 `409`、所有
  deprecated 路径未被新客户端调用。
