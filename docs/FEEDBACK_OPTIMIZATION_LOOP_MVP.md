# 告警研判反馈归因闭环 MVP 计划

## Summary

- 第一版聚焦“告警研判”场景，目标是把用户/分析师反馈转成可审计的归因结果和待审优化建议。
- 反馈来源拆成两条线：聊天面板反馈与外部 SOC 工作流事件；用 `run_id + session_id + alert_id/case_id` 串联 trace、回答、工具调用、SOC UI 操作和反馈。
- Claude Agent Runtime 只作为 UI 聊天面板后端，不承接整个网络安全运营系统后端职责；SOC 系统通过事件 API 把关键操作反馈推送进闭环。
- 不做自动自进化，不直接改 `CLAUDE.md`、skills、agents、MCP 或权限配置；只生成 proposal，等待人工 review。

## Key Changes

- Runtime 每次 `/api/chat` / `/api/chat/stream` 生成并返回 `run_id`，并写入 Langfuse observation metadata/output，便于用本地反馈和 Langfuse trace 互查。
- 新增本地反馈存储，使用数据卷 JSONL：
  - `/data/feedback/runs.jsonl`
  - `/data/feedback/events.jsonl`
  - `/data/feedback/feedback.jsonl`
  - `/data/feedback/attributions.jsonl`
  - `/data/feedback/pending_correlations.jsonl`
  - `/data/optimization-proposals/proposals.jsonl`
- 新增反馈 API：
  - `POST /api/feedback`：提交反馈，立即返回归因结果和 proposal。
  - `POST /api/feedback/events`：接收 SOC UI / 告警平台推送的领域事件。
  - `GET /api/feedback`：按 `run_id/session_id/alert_id/case_id` 查询反馈。
  - `GET /api/optimization-proposals`：查看待审优化建议。
- 前端在 assistant 回复上增加“反馈/归因”入口，支持：
  - 采纳 / 部分采纳 / 不采纳
  - 最终结论、最终风险等级
  - 证据不足、工具误报、工具参数错误、数据不全、风险等级不合理、处置建议不可执行等标签
  - 可选择本次回复中实际调用过的 MCP tool 作为归因对象。
- 新增隐式反馈捕捉，但只作为弱信号：
  - 聊天追问/纠错：重新分析、不是/不对、误报、补充证据、给时间线、查进程链。
  - 分析师操作：修改 verdict/severity/recommendation、补充 IOC/资产/证据、拒绝或采纳建议。
  - 工具信号：工具失败、权限拒绝、查询无结果、查询结果过多、人工后续用不同条件查询成功。
  - 所有隐式反馈写入 `auto_captured=true`、`confidence=low|medium|high`、`requires_review=true`。
- 外部 SOC 系统只推送有反馈意义的领域事件，不采集所有点击：
  - `case.verdict_changed`
  - `case.severity_changed`
  - `recommendation.accepted`
  - `recommendation.rejected`
  - `recommendation.modified`
  - `evidence.added`
  - `tool.manual_query_after_agent`
  - 完整工单生命周期事件和 SOAR 执行动作进入 Phase 2，不纳入 MVP。
- 归因先用确定性规则：
  - `tool_false_positive` / `tool_data_incomplete` -> `tool_quality_gap`
  - `wrong_tool` / `tool_param_error` -> `tool_usage_gap`
  - `evidence_insufficient` -> `evidence_gap`
  - verdict/severity 被分析师修改 -> `verdict_calibration_gap`
  - permission denied -> `permission_gap`
  - runtime errors -> `runtime_bug`
- 外部事件关联策略按优先级执行：
  - `run_id` 精确关联。
  - `session_id + alert_id/case_id` 关联。
  - `alert_id/case_id + 时间窗口` 关联。
  - IOC / asset / hostname 等实体相似关联。
  - 无法关联则进入待人工确认队列，不生成自动 proposal。
- JSONL 写入约束：
  - 所有反馈文件 append-only 写入。
  - 写入通过单 writer 或文件锁保证并发安全。
  - `event_id` 用于外部事件幂等去重，重复事件返回已存在结果。
  - 原始 SOC 事件只保留归因所需的最小字段，不保存密钥、凭据、MCP header 或大段原始日志。
- Proposal 只生成待审建议，目标对象限定为：
  - `.claude/skills/alert-triage/SKILL.md`
  - `.claude/skills/threat-hunting/SKILL.md`
  - `.claude/output-styles/security-analysis.md`
  - `/data/optimization-proposals/tool-registry/<tool>.yaml.proposal`
  - `/data/optimization-proposals/evals/alert-triage/*.json`

## Label Mapping

| UI 标签 | API label | 归因类型 |
| --- | --- | --- |
| 证据不足 | `evidence_insufficient` | `evidence_gap` |
| 工具误报 | `tool_false_positive` | `tool_quality_gap` |
| 工具数据不全 | `tool_data_incomplete` | `tool_quality_gap` |
| 工具参数错误 | `tool_param_error` | `tool_usage_gap` |
| 调用了错误工具 | `wrong_tool` | `tool_usage_gap` |
| 风险等级不合理 | `severity_mismatch` | `verdict_calibration_gap` |
| 结论不准确 | `verdict_mismatch` | `verdict_calibration_gap` |
| 处置建议不可执行 | `recommendation_not_actionable` | `recommendation_gap` |
| 权限拒绝 | `permission_denied` | `permission_gap` |
| Runtime 错误 | `runtime_error` | `runtime_bug` |

## Public Interfaces

- `ChatResponse` / stream `result.data` 新增：
  - `run_id: string`
- 每次 chat 完成后写入 `/data/feedback/runs.jsonl`：
  - `run_id`, `session_id`, `alert_id?`, `case_id?`
  - `agent_activity`
  - `answer_summary`
  - `created_at`, `completed_at`
- `FeedbackCreateRequest` 最小字段：
  - `run_id`, `session_id`, `alert_id?`, `case_id?`
  - `feedback_source`: `explicit | analyst_action | case_outcome | tool_quality`
  - `analyst_action?`: `accepted | partially_accepted | rejected | modified_conclusion | requested_more_evidence`
  - `final_verdict?`, `final_severity?`
  - `labels: string[]`
  - `affected_tools?: string[]`
  - `auto_captured?: boolean`
  - `confidence?: low | medium | high`
  - `requires_review?: boolean`
  - `comment?: string`
- `POST /api/feedback` 生成 proposal 的前置条件：
  - 必须能定位到 `run_id` 对应的 run。
  - 告警研判 MVP 必须至少包含 `alert_id` 或 `case_id` 之一。
  - 如果缺少 `alert_id/case_id`，只持久化反馈和归因，不生成 proposal。
- `FeedbackEventIngestRequest` 最小字段：
  - `event_id`, `source_system`, `event_type`, `timestamp`
  - `run_id?`, `session_id?`, `alert_id?`, `case_id?`
  - `actor_id?`
  - `before?: object`
  - `after?: object`
  - `entities?: { asset_ids?: string[], iocs?: string[], hostnames?: string[] }`
  - `auto_captured?: boolean`
  - `confidence?: low | medium | high`
  - `requires_review?: boolean`
  - `comment?: string`
  - `metadata?: object`
- `FeedbackResponse` 返回：
  - `feedback`
  - `attribution`
  - `proposal?: object`
- `FeedbackEventIngestResponse` 返回：
  - `event`
  - `correlation_status`: `matched | pending_correlation | duplicate | stored_only`
  - `matched_run_id?: string`
  - `attribution?: object`
  - `proposal?: object`
- Langfuse 不作为唯一存储；只通过 `run_id/session_id/alert_id/case_id` 做关联。

## Test Plan

- 后端单测：
  - chat 返回 `run_id`，Langfuse metadata/output 包含 `run_id`。
  - chat 完成后写入 `/data/feedback/runs.jsonl`，包含 `agent_activity` 和 `answer_summary`。
  - `POST /api/feedback` 能写入 JSONL 并返回归因和 proposal。
  - 缺少 `alert_id/case_id` 的反馈只保存，不生成 proposal。
  - `POST /api/feedback/events` 能写入外部 SOC 事件，并按 `run_id/session_id/alert_id/case_id` 关联已有 run。
  - 无 `run_id` 的 SOC 事件能按 `case_id/alert_id` 关联；无法关联时标记为 `pending_correlation`。
  - 重复 `event_id` 不重复写入事件，也不重复生成 proposal。
  - 工具误报反馈归因到 `tool_quality_gap`，不归因到普通 Agent 错误。
  - 隐式反馈写入 `auto_captured=true` 和 `requires_review=true`，不会直接生成已确认标签。
  - verdict/severity 修改归因到 `verdict_calibration_gap`。
  - 查询接口按 `run_id/session_id/alert_id/case_id` 过滤正确。
- 前端验证：
  - 反馈表单能读取当前回复的 `run_id/session_id/agent_activity.tool_calls`。
  - 提交成功后展示归因结果和 proposal 摘要。
  - 聊天追问/纠错类隐式反馈能在本地事件流中可见，但 UI 标注为“待确认”。
  - `npm run build` 通过。
- 集成场景：
  - 用 `sec-ops-data` 生成一次告警研判。
  - 提交“工具误报 + 证据不足”反馈。
  - 模拟 SOC UI 推送 `case.verdict_changed` 和 `recommendation.rejected` 事件。
  - 本地 JSONL、API 查询、UI 展示、Langfuse `run_id` 关联均可验证。

## Assumptions

- MVP 只处理告警研判，不覆盖完整工单/SOAR 生命周期。
- 分析师反馈先视为“待审标签”，不直接作为真实训练数据或自动策略更新。
- 外部 SOC 操作反馈通过事件 API 汇入；Runtime 不接管 SOC 主系统业务流程。
- 隐式反馈只作为弱信号，必须经过人工确认或规则复核后才能进入优化 proposal。
- 不引入数据库和迁移；继续使用 Docker 数据卷。
- 不做自动修改 workspace；所有优化只进入 proposal。
