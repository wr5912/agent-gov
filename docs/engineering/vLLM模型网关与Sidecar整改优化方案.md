# vLLM 模型网关与 Sidecar 整改方案

> 状态：已落地的工程基线与后续验收方案
> 日期：2026-07-14
> 适用范围：AgentGov Runtime / Claude Code 后端模型网关 / DSPy formatter

## 1. 结论

AgentGov 把本地或内网 vLLM 视为可观测、可诊断的模型后端，而不是直接假设它天然兼容 Claude Code。当前实现采用以下主策略：

1. **vLLM 版本事实只来自运行中服务的 `GET /version` 响应**，禁止新增或使用 `MODEL_PROVIDER_VLLM_VERSION` 这类手动版本配置。
2. **用户只配置一个真实模型服务 URL：`MODEL_PROVIDER_API_URL`**；它指向本地或内网 vLLM base URL，不用 URL 字符串推断 provider，也不暴露第二个模型服务 URL。
3. **`MODEL_PROVIDER_BACKEND=vllm` 默认通过 LiteLLM sidecar**；仅当显式 `MODEL_PROVIDER_VLLM_ALLOW_DIRECT=true` 且探测到的运行版本 `>= MODEL_PROVIDER_VLLM_SIDECAR_THRESHOLD`（默认 `0.23.0`）时，才把 Claude Code 直连 vLLM 原生 Anthropic 端点。direct 仍须通过能力门的真实 Claude Code 兼容探测（含 system-in-messages 判别），不通过即 fail-closed 报 `VLLM_DIRECT_CLAUDE_CODE_COMPAT_FAILED`，不静默回落。版本低于阈值、版本不可解析、404 等非传输类探测失败或开关关闭走 sidecar；超时、连接失败、408、429 或 5xx 会在模型请求前 fail-fast，避免对同一不可达服务再做一轮完整能力探测。`0.23.0` 是 AgentGov 路由策略阈值，不是 vLLM 官方最小支持版本声明。
4. **`ANTHROPIC_BASE_URL` 是 Runtime 注入给 Claude Code 的派生环境变量**，不是用户主配置；是否注入 sidecar 地址由 provider route 决定。
5. **`/version` 探测失败时**，系统打印脱敏且限频的 warning，后台 readiness 缓存记录结构化诊断；控制面 liveness 不依赖该探测。
6. **版本只决定路由安全边界，能力探测决定是否允许进入治理模型请求 / Claude Code 执行路径**；模型不具备 chat、streaming 或 tool calling 能力时，不伪造成功结果，必须给出可定位的结构化错误。
7. **不把旧 direct-vLLM 路由当成长期兼容入口**；若重构收益更高，应统一到 probe + LiteLLM sidecar + capability gate，而不是在 Runtime 调用路径里散落兼容分支。
8. **真实 Claude Code 请求抓包与回放是架构前置硬门**；不能因为第一个 vLLM schema 错误指向 `system` 位置，就推断只需修一个字段。
9. **模型能力预检必须前置并独立于协议管道验收**；Qwen 等目标模型能否完成类 Claude Code 大 system、多工具循环和 schema-exact JSON 输出，不能等到端到端 live 验收才混合暴露。

## 2. 一手依据与当前事实

### 2.1 外部权威依据

- vLLM 官方 Claude Code 集成文档说明：Claude Code 可通过 `ANTHROPIC_BASE_URL` 指向 vLLM，前提是 vLLM 提供 Anthropic Messages API，且模型具备强 tool calling 能力。  
  参考：<https://docs.vllm.ai/en/latest/serving/integrations/claude_code/>
- vLLM 官方在线服务文档说明：vLLM 提供 OpenAI-compatible server，可作为 OpenAI API 兼容后端。  
  参考：<https://docs.vllm.ai/en/stable/serving/online_serving/>
- vLLM 源码历史中，`v0.5.4`、`v0.8.3`、`v0.14.0` 的 OpenAI API server 均存在 `GET /version`，返回形态为 `{"version": "<vllm-version>"}`；但生产环境反代、定制镜像或新版本路由可能屏蔽该接口，因此必须把 `/version` 不可达作为显式诊断分支处理。  
  参考：<https://raw.githubusercontent.com/vllm-project/vllm/v0.14.0/vllm/entrypoints/openai/api_server.py>
- LiteLLM 提供 Anthropic `/v1/messages` 与 OpenAI-compatible endpoint 能力，作为 AgentGov sidecar 的唯一实现依赖；但不能绕过 AgentGov 自身的探测、脱敏日志和错误契约。
  参考：<https://docs.litellm.ai/docs/anthropic_unified/>、<https://docs.litellm.ai/docs/providers/openai_compatible>

这些外部依据只能证明能力方向，不能证明“任意 vLLM 版本 + 任意 Claude Code CLI 版本 + 任意模型”都可直接生产使用。因此本方案不把文档页面存在本身当成兼容性证明，最终准入只看运行中服务探测和模型服务连通性与能力验收。

真实失败点也不能由单次 400 响应反推。vLLM 往往在遇到第一个 schema 错误时就拒绝请求，因此 `messages[1].role=system` 这类报错只能说明第一个不兼容点，不代表后续字段、content block、tool schema、streaming event 或错误信封都兼容。

### 2.2 当前仓库事实

- `AppSettings` 已提供 `MODEL_PROVIDER_API_KEY` / `MODEL_PROVIDER_API_URL`，没有 `MODEL_PROVIDER_VLLM_VERSION` 或第二个 upstream URL。
- `ModelProviderRouter` 集中完成版本探测、路由选择、能力门、Claude Code env 和 formatter 配置；`DSPyOutputFormatter` 不再通过 URL 字符串猜测 provider。
- Compose 已包含 LiteLLM sidecar；API 启动后在后台执行 single-flight provider readiness 探测，具体模型请求复用同一路由并在传输故障时 fail-fast。
- 独立 Agent job worker 已退役。改进事项治理模型动作在 API 进程内执行，升级前 `agent_jobs` 只保留只读历史。
- `/health/live` 不访问 provider，`/health/ready` 只读 readiness 缓存，`/health` 返回控制面状态及 provider 摘要；前端分别展示 Runtime online 和 Provider degraded。

## 3. 运行时对象矩阵

| 维度 | 结论 |
| --- | --- |
| 运行时对象 | AgentGov Runtime 的模型接入链路，包括 Claude Code 子进程、治理模型请求、DSPy formatter、LiteLLM sidecar 与本地 vLLM |
| 控制点 | 后端配置选择、provider probe、LiteLLM sidecar、能力门、结构化错误、warning 日志和测试硬门组合 |
| 运行时产物 | sidecar 配置、probe result、warning、结构化 API error、health 摘要和验收用例 |
| readiness 生命周期 | `not_checked` -> `checking` -> `ready` / `degraded`；路由内部另保留 version/capability probe 结果 |
| 故障归属 | 模型接入失败必须归属到具体 Agent profile、run/session、provider endpoint、route 和 probe item，不得只落后端日志 |
| 当前实现边界 | vLLM 版本探测、sidecar/direct 路由、能力门、统一错误契约、缓存 readiness 与 liveness 隔离均已实现；真实目标模型的生产准入仍由私有 live 验收决定 |
| 目标能力边界 | 低版本或不可判定版本 vLLM 可通过 LiteLLM sidecar 接入；能力不足时给出明确失败原因，不进入治理模型请求 / Claude Code 执行路径 |
| 前置证据边界 | 先抓包和回放真实 Claude Code 请求，再判断是 LiteLLM 可直接承载、需要改走 OpenAI-compatible 专项路径，还是当前模型不可用 |

运行时调用链路：

```text
模型后端 -> 后台版本探测 -> 路由决策 -> 请求前能力探测 -> LiteLLM 转发 -> Claude Code 调用 -> 用户可见成功结果或结构化错误
```

“模型服务连通性与能力验收”只指对真实运行链路的验证：能否连接模型服务、读到版本、列出模型、完成 chat、完成 tool calling、完成 streaming，并把失败原因投影到 `/health/ready`、`/health`、API response 和 UI。它不是 AgentGov 改进工作台流程。

## 4. Runtime / Env 边界矩阵

| Consumer | Mode | Env source | Runtime root | Secret boundary | Verification |
| --- | --- | --- | --- | --- | --- |
| API container | container | `docker/.env` + Compose `RUNTIME_CONTAINER=1` | `${HOME}/volume-agent-gov` | `MODEL_PROVIDER_API_KEY` 只在私有 env；不写入仓库 | `/health/live`、`/health/ready`、startup log、provider probe |
| Host Python / PyCharm | local-debug | `docker/.env.local-debug` 被非容器进程选择 | `/tmp/local-debug-volume-agent-gov` | 不复用 Claude `/login`；真实 key 不入库 | settings 测试、local-debug probe |
| Claude Code / governance request | container 或 local-debug | `MODEL_PROVIDER_API_URL` 指向真实模型服务；`ANTHROPIC_BASE_URL` 由 Runtime 派生 | profile workspace / data_dir | warning/API/health 诊断脱敏；自托管 Langfuse 开发 trace 保留完整 prompt/tool I/O | provider precheck、main-flow test |
| LiteLLM sidecar | container | 使用同一个 `MODEL_PROVIDER_API_URL` 访问 vLLM 模型服务 | 无持久业务数据 | 模型服务 key/header 私有；日志脱敏 | sidecar `/health`、version/capability probe |
| vLLM 模型服务 | local 或内网 | 由 `MODEL_PROVIDER_API_URL` 指定 | vLLM 自身运行目录 | 只作为模型后端，不写 AgentGov docs 中的真实地址 | `/version`、`/v1/models`、chat/tool probe |
| DSPy formatter | API 进程 | 统一走 provider route 决策 | 当前 profile data_dir | 不把 raw prompt 或 raw output 写 warning | formatter 单测、结构化 API error |

## 5. 目标架构

```text
User private env
        |
        | MODEL_PROVIDER_API_URL = local / intranet vLLM base URL
        v
AgentGov provider route + probe gate
        |
        | version < threshold 或 version unknown
        v
LiteLLM sidecar
        |
        | OpenAI-compatible /v1/chat/completions
        v
Local / intranet vLLM
```

### 5.1 配置原则

- `MODEL_PROVIDER_API_URL`：唯一公开模型服务 URL，指向真实 vLLM base URL，例如 `http://vllm:8000`，不带 `/v1`。
- `MODEL_PROVIDER_BACKEND`：显式 provider adapter，取值为 `vllm`、`ollama`、`openai_compatible` 或 `anthropic_compatible`；它只决定探测和转换策略，不表示运行事实版本。
- `ANTHROPIC_BASE_URL`：Runtime 注入给 Claude Code 子进程的派生环境变量。默认（sidecar 路由）注入内部 LiteLLM sidecar 地址；`anthropic_compatible` 与 vLLM direct（opt-in）场景注入 `MODEL_PROVIDER_API_URL`。
- LiteLLM sidecar 后端地址不暴露第二个用户配置项，直接复用 `MODEL_PROVIDER_API_URL` 访问真实模型服务。
- `MODEL_PROVIDER_VLLM_SIDECAR_THRESHOLD`：direct 路由的版本准入阈值，默认 `0.23.0`；与 `MODEL_PROVIDER_VLLM_ALLOW_DIRECT` 一起决定是否允许 direct。它不是事实版本，只是策略参数。
- `MODEL_PROVIDER_VLLM_ALLOW_DIRECT`：direct 路由显式开关，默认 `false`（恒走 sidecar）。仅当 `true` 且探测版本 `>= threshold` 时才把 Claude Code 直连 vLLM 原生 `/v1/messages`，且仍须过能力门验收（不通过 fail-closed）。
- `MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS`：探测/能力门 HTTP 超时秒数，默认 `30`（真实大模型首 token 常 >3s，过小会被能力门误判超时永久阻断）。
- `MODEL_PROVIDER_WARNING_TTL_SECONDS`：同一 endpoint + 同一失败原因的 warning 限频窗口，默认 `300`。
- 禁止配置 `MODEL_PROVIDER_VLLM_VERSION`；任何文档、env 示例、代码和测试都不得把它作为兜底。
- 禁止把 `MODEL_PROVIDER_UPSTREAM_URL` 作为用户配置项；如果实现内部需要模型服务后端地址，必须由 `MODEL_PROVIDER_API_URL` 派生。
- 只有 `MODEL_PROVIDER_BACKEND=vllm` 时才执行 vLLM `/version` 探测和阈值路由；其他 backend 走各自 adapter，不用 URL 字符串猜测服务类型。

用户侧最小配置示例：

```text
MODEL_PROVIDER_BACKEND=vllm
MODEL_PROVIDER_API_URL=http://vllm:8000
MODEL_PROVIDER_API_KEY=<private-key-or-empty-if-backend-allows>
```

Runtime 内部派生：

```text
vllm_version_probe_url = {MODEL_PROVIDER_API_URL}/version
vllm_models_probe_url = {MODEL_PROVIDER_API_URL}/v1/models
litellm_sidecar_backend_base_url = {MODEL_PROVIDER_API_URL}
claude_child_ANTHROPIC_BASE_URL = http://agent-gov-litellm-sidecar:4000   # 默认 sidecar 路由
# vLLM direct 路由（ALLOW_DIRECT=true 且 version>=threshold）：claude_child_ANTHROPIC_BASE_URL = {MODEL_PROVIDER_API_URL}
```

`claude_child_ANTHROPIC_BASE_URL` 是对子进程注入的内部派生值，不能反向要求用户在 `.env` 中维护第二个 URL。

### 5.2 前置抓包与回放硬门

任何 sidecar 或 direct-vLLM 实现前，必须先完成真实请求证据采集和回放：

1. 抓取 Claude Code 实际发出的 Anthropic Messages 请求，至少覆盖 system blocks、messages、content blocks、tool schema、tool_use、tool_result、streaming 和错误响应。
2. 抓包只能落私有临时目录，进入文档、日志或测试 fixture 前必须脱敏；不得保留 prompt 原文、API key、Authorization header、tool args 原文或业务数据。
3. 回放同一请求到目标 vLLM / LiteLLM 链路，记录所有不兼容点；不能只根据第一个 400 错误制定转换规则。
4. 抓包和回放结果决定后续路径：LiteLLM 可直接承载、需要改走 OpenAI-compatible 专项路径，或目标模型/服务暂不准入。

`system` 相关转换必须保语义：如果 LiteLLM 或后续 adapter 对 system blocks 做上提、合并、拆分或重排，必须用抓包证据说明原始顺序和作用域，并用行为对比验证，而不是只验证请求不再 400。

### 5.3 版本路由规则

| 条件 | 路由 | 结果 |
| --- | --- | --- |
| `/version` 成功，版本 `< threshold`（任意 `ALLOW_DIRECT`） | LiteLLM sidecar | 所有 AgentGov 模型请求通过 LiteLLM sidecar |
| `/version` 成功，版本 `>= threshold`，`MODEL_PROVIDER_VLLM_ALLOW_DIRECT=false`（默认） | LiteLLM sidecar | 默认仍走 sidecar，不切 direct |
| `/version` 成功，版本 `>= threshold`，`MODEL_PROVIDER_VLLM_ALLOW_DIRECT=true` | direct（Claude Code 直连 vLLM 原生 `/v1/messages`） | 须过能力门真实 Claude Code 兼容探测（system-in-messages + tool_use + streaming）；不通过 fail-closed 报 `VLLM_DIRECT_CLAUDE_CODE_COMPAT_FAILED`，不静默回落 |
| `/version` 超时、连接失败、408、429 或 5xx | 模型请求前 fail-fast | readiness 降级并返回 `VLLM_VERSION_PROBE_FAILED`；控制面仍 live |
| `/version` 返回 404、非 JSON、缺少 version 或版本不可解析 | LiteLLM sidecar | 打印 warning，记录 `VLLM_VERSION_PROBE_FAILED` |
| `MODEL_PROVIDER_BACKEND` 不是 `vllm` | 不执行 vLLM 版本阈值路由 | 交给对应 adapter 的探测和错误契约 |

### 5.4 能力门规则

能力探测在路由选定后执行。未通过能力门时，不允许启动治理模型请求或 Claude Code 执行路径。

| 条件 | 结果 | 错误码 |
| --- | --- | --- |
| `/v1/models` 不可用 | 请求失败 | 返回 `VLLM_MODELS_PROBE_FAILED` |
| chat completion 不可用 | 请求失败 | 返回 `VLLM_CHAT_PROBE_FAILED` |
| tool calling 不可用或不稳定 | 阻止治理模型请求 / Claude Code 执行路径 | 返回 `VLLM_TOOL_CALLING_UNSUPPORTED` |
| LiteLLM sidecar 自身不可用 | 请求失败 | 返回 `MODEL_PROVIDER_SIDECAR_UNAVAILABLE` |
| LiteLLM 对 Claude Code tool/streaming 验收失败 | 请求失败 | 返回 `LITELLM_CLAUDE_CODE_COMPAT_FAILED` |
| vLLM direct（opt-in）原生 `/v1/messages` 不兼容 Claude Code（system-in-messages / tool_use / streaming） | 请求失败（fail-closed，不回落 sidecar） | 返回 `VLLM_DIRECT_CLAUDE_CODE_COMPAT_FAILED` |
| 目标模型无法完成类 Claude Code 工具循环 | 阻止治理模型请求 / Claude Code 执行路径 | 返回 `MODEL_AGENT_LOOP_CAPABILITY_FAILED` |
| 目标模型无法输出 schema-exact JSON | 阻止 governor / formatter 依赖路径 | 返回 `MODEL_SCHEMA_EXACT_OUTPUT_FAILED` |

## 6. 探测与错误契约

### 6.1 版本探测

版本探测只允许访问运行中的 vLLM：

```text
GET {MODEL_PROVIDER_API_URL}/version
```

成功响应示例：

```json
{"version": "0.14.0"}
```

失败分支：

- 连接失败：`connection_error`
- 超时：`timeout`
- HTTP 非 2xx：`http_status`
- 非 JSON：`invalid_json`
- 缺少 `version`：`missing_version`
- 版本不可解析：`invalid_version`

版本探测失败不允许读取手动版本配置。系统必须：

1. 记录结构化 probe result。
2. 打印脱敏且限频的 warning。
3. 对非传输类失败保守路由到 LiteLLM sidecar；对超时、连接失败、408、429 或 5xx 在具体模型请求前 fail-fast。
4. 把最终错误投影到 API response、readiness 缓存和用户可见状态，同时保持 `/health/live` 不受影响。

### 6.2 Warning 日志

`/version` 探测失败时必须打印 warning，但必须限频和脱敏。

日志字段：

```text
level=warning
event=VLLM_VERSION_PROBE_FAILED
provider_endpoint=http://vllm:8000
reason=timeout|http_404|invalid_json|missing_version|invalid_version
status_code=...
duration_ms=...
action=fallback_to_litellm_sidecar
route_threshold=0.23.0
```

日志规则：

- 不打印 API key、Authorization header、完整 query、prompt、messages、tool args、模型输入输出。
- `provider_endpoint` 只允许脱敏后的 scheme + host + port；必须去掉 path、query、userinfo 和敏感 header。
- 同一 endpoint + 同一 reason 在 `MODEL_PROVIDER_WARNING_TTL_SECONDS` 内只打印一次。
- 失败原因变化、endpoint 变化或恢复后再次失败时重新打印。
- 版本成功时的路由决策（direct/sidecar、是否满足阈值、`allow_direct`）使用 `info` 事件 `VLLM_VERSION_ROUTE`（按 endpoint+route TTL 限频），不使用 `warning`。

### 6.3 结构化错误

统一错误字段：

```json
{
  "code": "VLLM_TOOL_CALLING_UNSUPPORTED",
  "message": "vLLM model service is reachable but tool calling probe failed.",
  "route": "litellm_sidecar",
  "probe": "tool_calling",
  "endpoint": "http://vllm:8000",
  "status_code": 200,
  "duration_ms": 248,
  "retryable": false,
  "action": "check model tool parser or choose a tool-calling capable model"
}
```

错误投影要求：

- API 请求：返回稳定错误码和脱敏详情。
- UI：在 Runtime online 与 Provider degraded 之间明确区分，并展示 error code、probe、reason 和建议动作。
- `/health/ready` 与 `/health`：暴露最近一次 provider probe 摘要，不暴露 secret 和 prompt；`/health/live` 不执行探测。
- warning/API/health 诊断：只记录脱敏后的 route、probe、code、duration，不写 raw prompt 或 tool 参数；自托管 Langfuse 开发 trace 遵循项目调试面原则，保留完整 prompt、tool input/output、治理输入输出、raw text 和 trace I/O。
- 流式错误：如果上游在 SSE 中途返回错误，必须转换为 Anthropic-compatible `event: error`，并投影稳定错误码；不能只处理 HTTP 非 2xx。

## 7. LiteLLM Sidecar 整改原则

### 7.1 实现边界

sidecar 固定借助 LiteLLM 实现，作为协议边界，不是应用决策层。它只负责：

- 按调用方协议接收请求；Claude Code 使用 Anthropic Messages，非 Claude 调用不得假装成 Claude 请求。
- 规范化 system、messages、content blocks、tool_use、tool_result。
- 转换为 vLLM OpenAI-compatible chat/tool 请求。
- 把 vLLM 响应转换回 Claude Code 期望的 Anthropic-compatible 响应。
- 输出健康状态、版本探测状态和能力探测状态。

LiteLLM sidecar 不负责：

- 决定 AgentGov 应用状态。
- 保存业务数据。
- 隐式修复模型输出。
- 在 tool calling 不合格时伪造成合格。

### 7.2 LiteLLM 验收策略

整改实施时不再保留自研 thin normalizer 兜底。LiteLLM 是唯一 sidecar 实现依赖，但必须通过 AgentGov 自身验收：

- 是否支持 Claude Code 实际发送的 Anthropic Messages 请求。
- 是否能稳定转译 OpenAI-compatible vLLM tool calls。
- 是否能处理 system blocks、tool_use、tool_result、streaming 和错误响应。
- 是否能把非 Anthropic 形态的流式错误转换为 Anthropic-compatible SSE `event: error`。
- 是否能提供足够的脱敏日志、健康检查和失败诊断。

若 LiteLLM 未通过 Claude Code tool/streaming 验收，不实现 AgentGov 自研协议转换兜底；请求必须失败，并返回 `LITELLM_CLAUDE_CODE_COMPAT_FAILED`。AgentGov 的职责是把失败原因、probe item、endpoint、route、状态码和脱敏响应摘要投影到 API response、`/health/ready`、`/health` 摘要和用户可见状态。

LiteLLM 通过协议验收不等于模型可用于 AgentGov。目标模型仍必须单独通过模型能力预检；失败时返回模型能力错误码，而不是归咎于 sidecar。

## 8. Formatter 接入整改

DSPy formatter 已通过集中 provider route 选择 provider：

1. 把 formatter 的模型 provider 和 API route 显式化，避免 `openai/<model>` 指向 Anthropic sidecar 或 `anthropic/<model>` 指向 OpenAI `/v1` 的半配置。
2. 低版本本地 vLLM 场景下，formatter 默认也通过同一 LiteLLM sidecar 路由，保证“所有 AgentGov 模型请求通过 sidecar”的一致性。
3. 如果后续确需让 formatter 直连 OpenAI-compatible vLLM，必须单独形成设计例外：说明为什么不是 Claude Code 请求、如何探测能力、如何避免与主 provider route 冲突。
4. formatter 不可用时，业务动作必须按契约返回结构化错误或标明 deterministic fallback 来源，不得生成 raw/offline 伪成功结果。

## 9. 分阶段整改状态

| 阶段 | 目标 | 当前状态 | 验收 |
| --- | --- | --- | --- |
| P0 文档与配置边界 | 禁止手配版本、保持单 URL 和阈值可配 | 已完成 | 文档检查和 env policy |
| P1 真实请求抓包与回放 | 用真实 Claude Code 请求决定接入路径 | 已固化为 direct/sidecar 兼容 probe；真实模型升级仍需重跑 | system-in-messages、tool_use 和 streaming probe |
| P2 Provider probe | vLLM version/models/chat/tool/streaming 探测 | 已完成 | provider router 单测 |
| P3 LiteLLM sidecar | 唯一协议转换 sidecar | 已完成 | sidecar health 与兼容错误契约 |
| P4 模型能力预检 | 区分协议故障与模型能力不足 | 已完成基础能力门 | tool loop 与 schema-exact JSON probe |
| P5 Runtime 路由与 Formatter 收敛 | Claude Code/formatter 统一 provider route | 已完成 | main-flow 和 formatter 测试 |
| P6 模型服务连通性与能力验收 | 容器健康降级回归和私有真实模型准入 | 健康降级 E2E 已完成；真实目标模型按环境 gated | `make container-health-e2e`、`make container-live-test` |
| P7 收尾同步 | README、docs、skill 和发布说明同步 | 本轮收口 | docs/Codex 治理硬门 |

## 10. 测试与验收场景

### 10.1 单元测试

- `/version` 返回低于阈值版本（任意 `allow_direct`）：路由为 sidecar。
- `/version` 返回 `>= threshold` 且 `MODEL_PROVIDER_VLLM_ALLOW_DIRECT=true`：路由为 direct（`claude_base_url=MODEL_PROVIDER_API_URL`、formatter `openai/` 前缀走 vLLM `/v1`）；边界 `version==threshold` 也判 direct。
- `/version` 返回 `>= threshold` 但 `allow_direct` 关闭（默认）：仍走 sidecar。
- direct 路由能力门对原生 `/v1/messages` 跑 Claude Code 兼容探测（含 system-in-messages + tool_use + streaming）；返回 400 或无 `tool_use` 即 fail-closed 报 `VLLM_DIRECT_CLAUDE_CODE_COMPAT_FAILED`（retryable=false）。
- 版本不可解析：路由为 sidecar，`version_probe.reason=invalid_version`。
- `/version` 超时或连接失败：生成 `VLLM_VERSION_PROBE_FAILED`，readiness 降级且模型请求在完整能力探测前 fail-fast；404、非 JSON、缺少 `version` 则保守选择 LiteLLM sidecar。
- 同一 endpoint + reason 在 TTL 内重复失败：只打印一次 warning。
- 配置中出现 `MODEL_PROVIDER_VLLM_VERSION`：设置层、env 示例或文档契约测试失败。
- 文档、env 示例或 settings 中出现公开 `MODEL_PROVIDER_UPSTREAM_URL`：文档契约测试失败。
- `MODEL_PROVIDER_BACKEND=vllm` 时，版本探测 URL 只能由 `MODEL_PROVIDER_API_URL` 派生。

### 10.2 集成测试

- fake vLLM 支持 `/version` 和 `/v1/models`，但 chat 不可用：返回 `VLLM_CHAT_PROBE_FAILED`。
- fake vLLM 支持 chat，但 tool calling 返回普通文本：返回 `VLLM_TOOL_CALLING_UNSUPPORTED`。
- LiteLLM sidecar 由 `MODEL_PROVIDER_API_URL` 派生的模型服务后端不可达：返回 `MODEL_PROVIDER_SIDECAR_UNAVAILABLE`。
- LiteLLM 无法兼容 Claude Code tool_use/tool_result 或 streaming event：返回 `LITELLM_CLAUDE_CODE_COMPAT_FAILED`，不启用自研转换兜底。
- fake vLLM 在 streaming 中途返回非 Anthropic 错误：LiteLLM sidecar 必须输出 Anthropic-compatible SSE `event: error`，否则返回 `LITELLM_CLAUDE_CODE_COMPAT_FAILED`。
- Claude Code 构造 options 时，`ANTHROPIC_BASE_URL` 指向 Runtime 派生的内部 sidecar 地址，而不是用户配置的第二 URL。
- formatter 失败时，业务动作返回稳定结构化错误或明确标记 deterministic fallback，用户可见状态不显示伪成功。

### 10.3 专项预检分层

专项预检必须把协议管道、Claude Code 请求兼容和模型能力质量拆开：

| 层级 | 目标 | 失败码 |
| --- | --- | --- |
| S1 协议管道 | vLLM 可连接、可列模型、可 chat、可 tool calling、可 streaming | `VLLM_MODELS_PROBE_FAILED` / `VLLM_CHAT_PROBE_FAILED` / `VLLM_TOOL_CALLING_UNSUPPORTED` |
| S2 Claude Code 兼容 | 真实 Claude Code 请求抓包回放、tool_use/tool_result 循环、SSE `event: error`、错误信封兼容 | `LITELLM_CLAUDE_CODE_COMPAT_FAILED` |
| C 模型能力 | 类 Claude Code 大 system、多工具选择、两轮以内工具循环、schema-exact JSON、乱循环检测 | `MODEL_AGENT_LOOP_CAPABILITY_FAILED` / `MODEL_SCHEMA_EXACT_OUTPUT_FAILED` / `MODEL_CONTEXT_LOAD_FAILED` |

C 类预检要在目标模型准入前运行。最小用例应直接用一条类 Claude Code 请求打目标模型：包含 claude_code 量级 system、一组工具、一个必须调用工具才能回答的问题，以及一个要求输出 governor schema-exact JSON 的任务。若 C 类预检失败，不能把失败归类为协议问题，也不能继续宣称该模型可用于治理模型请求。

### 10.4 模型服务连通性与能力验收

真实目标模型的连通性与能力验收只在私有 env 配置齐全时运行，缺少模型凭据或 vLLM 地址时 skip，不破坏离线 `make test` 产品不变量。该验收必须使用 Docker Compose 中的真实 API / LiteLLM sidecar / vLLM 网络链路，不用 local-debug 结果替代容器结果。

此外，`make container-health-e2e` 使用确定性慢 vLLM 服务、真实 Compose API/UI/LiteLLM 容器和 Playwright，强制验证 provider 探测超时时 API/UI 仍健康、错误诊断明确、日志不泄露 key。它不替代真实目标模型能力准入。

必测项：

- `GET /version` 成功或失败诊断明确。
- `/v1/models` 可达。
- 最小 chat completion 可达。
- tool calling probe 可判定。
- streaming success 与 SSE `event: error` 均可判定。
- LiteLLM sidecar `/health/readiness` 可达，Runtime readiness 返回 backend route、version probe、capability probe 摘要。
- 治理模型请求从发起到失败或成功的用户可见状态一致。

## 11. 安全与日志约束

- 真实 API key、MCP header、数据库凭据、本机私有路径、prompt、messages、tool args 和模型 raw output 不得进入仓库、warning 或普通应用日志。
- warning 日志只允许脱敏 endpoint、错误类型、状态码、耗时、动作和阈值。
- `docker/.env`、`docker/.env.local-debug` 和 runtime volume 不得进入 staged diff。
- LiteLLM sidecar 调试抓包只能落临时目录，并必须脱敏后才能进入文档或评审材料。
- 新增 LiteLLM 及关键传递依赖时必须显式 pin 或进入锁定文件；不能依赖容器里已有的 `httpx`、`starlette` 等传递依赖“碰巧可用”。
- 抓包 fixture 只能保存结构形态、字段名、脱敏长度和错误摘要；不得保存真实 system prompt、用户消息、工具参数或模型输出原文。

## 12. 决策记录

| 决策 | 结论 |
| --- | --- |
| 是否允许 `MODEL_PROVIDER_VLLM_VERSION` | 不允许；版本事实只来自运行中的 `/version` |
| 是否允许公开 `MODEL_PROVIDER_UPSTREAM_URL` | 不允许；用户只维护 `MODEL_PROVIDER_API_URL`，LiteLLM sidecar 模型服务后端地址由它派生 |
| 是否通过 URL 字符串判断 provider | 不允许；使用 `MODEL_PROVIDER_BACKEND` 选择 adapter |
| `0.23.0` 是否是官方兼容起点 | 不是；它只是 AgentGov 可配置路由阈值，真实准入以探测和验收为准 |
| `/version` 不可用怎么办 | 打 warning、记录结构化诊断、默认 LiteLLM sidecar；LiteLLM sidecar 或能力探测失败时请求失败 |
| 低版本 vLLM 是否直连 Claude Code | 不直连；版本低于 `MODEL_PROVIDER_VLLM_SIDECAR_THRESHOLD`、不可解析或探测失败一律走 LiteLLM sidecar |
| 高版本 vLLM 是否可直连 | 仅作为显式 opt-in：`MODEL_PROVIDER_VLLM_ALLOW_DIRECT=true` 且 `version>=threshold` 才 direct，且须过 direct 能力门（真实 Claude Code 兼容探测），不通过 fail-closed 报 `VLLM_DIRECT_CLAUDE_CODE_COMPAT_FAILED` |
| 阈值与开关是否参与路由 | 参与；`MODEL_PROVIDER_VLLM_SIDECAR_THRESHOLD`（默认 `0.23.0`，策略阈值非事实版本）与 `MODEL_PROVIDER_VLLM_ALLOW_DIRECT`（默认 `false`）共同决定 direct/sidecar |
| 能力不足是否可降级成 raw 成功 | 不允许；必须失败并给出稳定错误码 |
| sidecar 是否完全借助 LiteLLM | 是；不保留自研 thin normalizer 兜底 |
| LiteLLM 的 Claude Code tool/streaming 验收不通过怎么办 | 请求失败，返回 `LITELLM_CLAUDE_CODE_COMPAT_FAILED` 并投影到 `error_json` 和用户可见状态 |
| 是否可以只根据第一个 vLLM 400 错误制定转换规则 | 不可以；必须抓包并回放真实 Claude Code 请求，记录完整不兼容点 |
| 模型能力失败是否等同协议失败 | 不等同；C 类预检失败返回模型能力错误码，不归咎于 LiteLLM 或 vLLM 管道 |
