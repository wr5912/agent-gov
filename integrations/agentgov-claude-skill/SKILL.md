---
name: agentgov-claude-skill
description: Use when integrating AgentGov (the agent runtime governance backend) from an upper-layer business system — calling its HTTP API to run agents, replay session history, submit feedback, run evaluation/regression, or trigger version releases. Encodes the integration journeys, auth, and ownership boundaries.
---

# 集成 AgentGov 运行治理底座

AgentGov 是 agent 运行治理底座，被上层业务系统通过 HTTP API 集成。本 skill 给出最短集成路径与硬边界。**契约真相源是 AgentGov 容器的 OpenAPI（`/openapi.json`、`/docs`）——具体字段、状态码、schema 以它为准，不要照搬本文。**

## 接入前提

- Base URL：由 AgentGov 部署方提供。外部 / 同主机默认 `http://<host>:58080`（宿主暴露端口 `HOST_PORT`，约定 50000 + `API_PORT`，默认 58080）；同 Docker 网络内的服务用 `http://claude-agent-api:8080`。`8080` 是容器内端口、不是外部 Base URL，生产可能在反向代理 / TLS 之后。
- 认证：所有 `/api/*`、`/v1/*` 带 `Authorization: Bearer <API_KEY>`。
- 错误语义：`401` 未鉴权 / `404` 不存在 / `409` 状态冲突 / `400|422` 入参非法 / `500` 服务端或数据完整性异常。
- 先 `GET /health` 探活，并拉一次 `/openapi.json` 作为对接基线。

## 集成旅程（按需取用）

1. 选 / 建业务 Agent：`GET|POST /api/agent-registry`；只跑 main 时 `POST /api/chat` 省略 `agent_id`。
2. 跑对话：`POST /api/chat` 或 `POST /api/chat/stream`（SSE）——两个原生入口 **`agent_id` 必填有效**（main-agent 或已注册业务 Agent，缺失 422 / 未知 404）；或 `POST /v1/chat/completions`（OpenAI 兼容，无 agent_id，跑**运营者配置的出口 Agent**，经 `/api/settings/openai-compat-agent` 配置、未配置默认 main）。续聊用同一 `session_id`。
3. 回放历史：`GET /api/sessions`、`GET /api/sessions/{session_id}/messages`（`?limit=&offset=`）。
4. 提交反馈：`POST /api/feedback-cases`（可挂 `run_id`/`session_id`），在确认门上决策；批次走 `/api/feedback-optimization-batches/...`。
5. 评估 / 回归：`/api/eval-cases`、`/api/eval-runs`、`/api/regression-assets/...`。
6. 版本发布 / 回滚：`/api/agent-change-sets/...`、`/api/agent-releases/...`。
7. 资产复用：`/api/assets`、`/api/assets/{asset_id}/inherit`。

## 硬边界（必须遵守）

- 产品对话 id 用 `session_id`，**不要**用响应里的 `sdk_session_id`（内部 resume id）。
- 读会话历史**只传 `session_id`，不传 `agent_id`**：归属是 backend-owned，由底座解析。
- 高风险动作的人审批在**你（上层系统）**这边做；AgentGov 只记录 operator/reason/审计事件，不承载该审批。
- 客户端类型由 OpenAPI 生成，**不要**自造 schema；也**不要**在你侧并行存储会话/消息副本（单一真相源是 AgentGov 的 session transcript，按 `session_id` 取）。
- 工具 / MCP / skills / subagents 由 AgentGov 侧的 Claude Code 官方配置管理，不通过 chat 入参接管。
- `main-agent` 是第一阶段样板，不是长期边界；长期治理对象是业务 Agent。

## 验证

- 用 `/health` + 一次最小 `POST /api/chat` 打通鉴权与运行；用返回的 `session_id` 调 `/api/sessions/{id}/messages` 验证回放。
- 对 `4xx/5xx` 做稳健处理；不要把 `404`/`500` 当空数据吞掉。
