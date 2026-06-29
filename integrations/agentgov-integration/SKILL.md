---
name: agentgov-integration
description: Use when integrating AgentGov (the agent runtime governance backend) from an upper-layer business system through its HTTP API, including agent runs, SSE chat streaming, Web HITL confirmation cards, session replay, feedback loops, evaluation/regression, assets, and version releases. Encodes integration journeys, auth, ownership boundaries, and anti-patterns without binding to Claude-only clients.
---

# 集成 AgentGov 运行治理底座

AgentGov 是 agent 运行治理底座，被上层业务系统通过 HTTP API 集成。本 skill 给出最短集成路径与硬边界。契约真相源是 AgentGov 容器的 OpenAPI（`/openapi.json`、`/docs`）；具体字段、状态码和 schema 以 OpenAPI 为准，不要照搬本文自造类型。

## 接入前提

- Base URL 由 AgentGov 部署方提供。外部 / 同主机常见 `http://<host>:48080`（宿主暴露端口 `HOST_PORT`；当前实现分支使用 `4XXXX` 端口段），同 Docker 网络内服务用 `http://claude-agent-api:8080`。
- 所有 `/api/*`、`/v1/*` 带 `Authorization: Bearer <API_KEY>`；缺失或错误 token 返回 `401`。
- 先 `GET /health` 探活，再拉取 `/openapi.json` 作为对接基线。
- 原生 `/api/chat` 与 `/api/chat/stream` 都要求显式有效 `agent_id`（如 `main-agent` 或已注册业务 Agent）。

## 集成旅程

1. 选 / 建业务 Agent：`GET|POST /api/agent-registry`；`main-agent` 是预制业务 Agent，但 chat 仍要显式传 `agent_id`。
2. 跑对话：`POST /api/chat` 返回完整结果；`POST /api/chat/stream` 返回 SSE；`POST /v1/chat/completions` 走 OpenAI 兼容出口 Agent。
3. 回放历史：`GET /api/sessions`、`GET /api/sessions/{session_id}/messages`；只传 `session_id`。
4. 提交反馈：`POST /api/feedback-cases`，可挂 `run_id`/`session_id`；归因、优化、回归由 AgentGov 内部治理链路处理。
5. 评估 / 回归：使用 `/api/eval-cases`、`/api/eval-runs`、`/api/regression-assets/...`。
6. 版本发布 / 回滚：使用 `/api/agent-change-sets/...`、`/api/agent-releases/...`。
7. 资产复用：使用 `/api/assets`、`/api/assets/{asset_id}/inherit`。

## SSE + Web HITL 确认卡

需要人工确认卡时使用 `/api/chat/stream`，并确认 AgentGov 侧 `ENABLE_CLAUDE_WEB_HITL=true`。非流式 `/api/chat` 不承载 Web HITL。

实现流程：

1. 建立 `/api/chat/stream` SSE 连接，持续读取到 `done`。
2. 正常处理 `session`、`message`、`result`、`done`；忽略但保留连接活性的 `heartbeat`。
3. 遇到 `claude_user_input_required`，按 `request_id` 渲染内联确认卡，并保留其中的 `decision_token` 在内存中。
4. 用户决策后，调用 `POST /api/claude-user-input-requests/{request_id}/decision`。
5. 遇到 `claude_user_input_resolved`，把同一张卡片更新为已处理。

确认类型：

- `tool_permission`：动作是 `allow_once`、`allow_for_run`、`deny`。`allow_for_run` 只在同一 `business_agent_id + run_id` 内跳过后续工具确认，不跨 run、不写入永久权限。
- `ask_user_question`：动作是 `answer_question`，可提交结构化选项，也可提交自然语言 `response`。

HITL 硬边界：

- 不要关闭 SSE 连接再等用户决策；Claude SDK 正在原 stream 里等待确认。
- `decision_token` 是一次性敏感 token，只放内存；不要写入 localStorage、日志、埋点或服务端会话副本。
- 页面刷新或 token 丢失后，不要伪造决策；提示用户重新运行当前任务。
- 用户断开 stream 时，把等待卡片标为中断或失效。

## 硬边界

- 产品对话 id 用 `session_id`，不要用响应里的 `sdk_session_id`。
- 读会话历史只传 `session_id`，不传 `agent_id`；归属由 AgentGov 按会话事实解析。
- 高风险动作的人审批在上层业务系统完成；AgentGov 只记录 operator/reason/审计事件。
- 客户端类型由 OpenAPI 生成，不要自造 schema；也不要在上层系统并行存储会话/消息副本。
- 工具、MCP、skills、subagents 由 AgentGov 侧 Claude Code 官方配置管理，不通过 chat 入参接管。

## 验证

- 用 `/health` + 最小 `POST /api/chat` 打通鉴权与运行。
- 用 `/api/chat/stream` 验证 SSE 解析、`heartbeat` 容忍、`done` 收尾。
- 用一次会触发工具确认的 stream 验证 `claude_user_input_required -> decision POST -> claude_user_input_resolved -> done`。
- 用返回的 `session_id` 调 `/api/sessions/{id}/messages` 验证回放。
- 对 `4xx/5xx` 做稳健处理，不要把 `404`/`500` 当空数据吞掉。
