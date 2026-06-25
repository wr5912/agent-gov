# vLLM 模型网关与 Sidecar 整改方案

> 状态：工程整改方案  
> 日期：2026-06-24  
> 适用范围：AgentGov Runtime / Agent job / Claude Code 后端模型网关 / DSPy formatter

## 1. 结论

AgentGov 应把本地或内网 vLLM 视为可观测、可诊断的模型后端，而不是直接假设它天然兼容 Claude Code。后续代码整改采用以下主策略：

1. **vLLM 版本事实只来自运行中服务的 `GET /version` 响应**，禁止新增或使用 `MODEL_PROVIDER_VLLM_VERSION` 这类手动版本配置。
2. **用户只配置一个真实模型服务 URL：`MODEL_PROVIDER_API_URL`**；它指向本地或内网 vLLM base URL，不用 URL 字符串推断 provider，也不暴露第二个模型服务 URL。
3. **`MODEL_PROVIDER_BACKEND=vllm` 且运行中 vLLM 版本低于 `0.23.0` 时，所有 AgentGov 模型请求默认通过 LiteLLM sidecar**；`0.23.0` 是 AgentGov 的路由策略阈值，不是 vLLM 官方最小支持版本声明。
4. **`ANTHROPIC_BASE_URL` 是 Runtime 注入给 Claude Code 的派生环境变量**，不是用户主配置；是否注入 sidecar 地址由 provider route 决定。
5. **`/version` 探测失败时**，系统打印脱敏且限频的 warning，记录结构化诊断，并默认走 LiteLLM sidecar。
6. **版本只决定路由安全边界，能力探测决定是否允许进入 Agent job / Claude Code 执行路径**；模型不具备 chat、streaming 或 tool calling 能力时，不伪造成功结果，必须给出可定位的结构化错误。
7. **不把旧 direct-vLLM 路由当成长期兼容入口**；若重构收益更高，应统一到 probe + LiteLLM sidecar + capability gate，而不是在 Runtime 调用路径里散落兼容分支。

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

### 2.2 当前仓库事实

- `AppSettings` 当前已有 `MODEL_PROVIDER_API_KEY` / `MODEL_PROVIDER_API_URL`，没有 `MODEL_PROVIDER_VLLM_VERSION`，也没有单独的模型服务 URL。
- `ClaudeRuntime` 与 `AgentJobRunner` 会把 `settings.provider_api_url` 注入为 Claude Code 子进程的 `ANTHROPIC_BASE_URL`。
- `DSPyOutputFormatter` 当前复用 `settings.provider_api_url`，并通过 URL 是否包含 `anthropic` 推断 provider；这在 sidecar 场景下容易出现半配置风险，后续必须改成显式 route/provider 决策。
- README 已声明：离线部署不等于无模型，Agent job、Claude Code 调用和 DSPy 输出规范化应指向本地或内网模型网关；formatter 不可用时 job 失败并写入 `error_json`，不能生成 offline/raw 伪成功结果。

## 3. 运行时对象矩阵

| 维度 | 结论 |
| --- | --- |
| 运行时对象 | AgentGov Runtime 的模型接入链路，包括 Claude Code 子进程、后台 Agent job、DSPy formatter、LiteLLM sidecar 与本地 vLLM |
| 控制点 | 后端配置选择、provider probe、LiteLLM sidecar、能力门、结构化错误、warning 日志和测试硬门组合 |
| 运行时产物 | sidecar 配置、probe result、warning、`error_json`、health 摘要和验收用例 |
| 生命周期 | `unprobed` -> `version_detected` / `version_probe_failed` -> `route_selected` -> `capability_passed` / `capability_failed` -> `active` / `blocked` |
| 故障归属 | 模型接入失败必须归属到具体 Agent profile、job/run/session、provider endpoint、route 和 probe item，不得只落后端日志 |
| 当前实现边界 | 已能向 Claude Code 注入 `ANTHROPIC_BASE_URL`；尚无 vLLM 版本探测、sidecar 路由、能力门和统一错误契约 |
| 目标能力边界 | 低版本或不可判定版本 vLLM 可通过 LiteLLM sidecar 正常接入；能力不足时给出明确失败原因，不进入 Agent job / Claude Code 执行路径 |

运行时调用链路：

```text
模型后端 -> 版本探测 -> 路由决策 -> 能力探测 -> LiteLLM 转发 -> Agent job / Claude Code 调用 -> 用户可见成功结果或结构化错误
```

“模型服务连通性与能力验收”只指对真实运行链路的验证：能否连接模型服务、读到版本、列出模型、完成 chat、完成 tool calling、完成 streaming，并把失败原因投影到 `/health`、API response 和 job `error_json`。它不是 AgentGov 改进工作台流程。

## 4. Runtime / Env 边界矩阵

| Consumer | Mode | Env source | Runtime root | Secret boundary | Verification |
| --- | --- | --- | --- | --- | --- |
| API / worker container | container | `docker/.env` + Compose `RUNTIME_CONTAINER=1` | `${HOME}/volume-agent-gov` | `MODEL_PROVIDER_API_KEY` 只在私有 env；不写入仓库 | `/health`、startup log、provider probe |
| Host Python / PyCharm | local-debug | `docker/.env.local-debug` 被非容器进程选择 | `/tmp/local-debug-volume-agent-gov` | 不复用 Claude `/login`；真实 key 不入库 | settings 测试、local-debug probe |
| Claude Code / Agent job | container 或 local-debug | `MODEL_PROVIDER_API_URL` 指向真实模型服务；`ANTHROPIC_BASE_URL` 由 Runtime 派生 | profile workspace / data_dir | 只注入脱敏日志；不打印 prompt/tool args | Agent job precheck、main-flow test |
| LiteLLM sidecar | container | 使用同一个 `MODEL_PROVIDER_API_URL` 访问 vLLM 模型服务 | 无持久业务数据 | 模型服务 key/header 私有；日志脱敏 | sidecar `/health`、version/capability probe |
| vLLM 模型服务 | local 或内网 | 由 `MODEL_PROVIDER_API_URL` 指定 | vLLM 自身运行目录 | 只作为模型后端，不写 AgentGov docs 中的真实地址 | `/version`、`/v1/models`、chat/tool probe |
| DSPy formatter | API / worker 进程 | 统一走 provider route 决策 | 当前 job data_dir | 不把 raw prompt 或 raw output 写 warning | formatter 单测、job error_json |

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
- `ANTHROPIC_BASE_URL`：Runtime 注入给 Claude Code 子进程的派生环境变量。低版本 vLLM 场景下注入内部 LiteLLM sidecar 地址；`anthropic_compatible` 场景可直接注入 `MODEL_PROVIDER_API_URL`。
- LiteLLM sidecar 后端地址不暴露第二个用户配置项，直接复用 `MODEL_PROVIDER_API_URL` 访问真实模型服务。
- `MODEL_PROVIDER_VLLM_SIDECAR_THRESHOLD`：路由阈值，默认 `0.23.0`；它不是事实版本，只是策略参数。
- `MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS`：探测超时，默认 `3`。
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
claude_child_ANTHROPIC_BASE_URL = http://agent-gov-litellm-sidecar:4000
```

`claude_child_ANTHROPIC_BASE_URL` 是对子进程注入的内部派生值，不能反向要求用户在 `.env` 中维护第二个 URL。

### 5.2 版本路由规则

| 条件 | 路由 | 结果 |
| --- | --- | --- |
| `/version` 成功，版本 `< threshold` | LiteLLM sidecar | 所有 AgentGov 模型请求通过 LiteLLM sidecar |
| `/version` 成功，版本 `>= threshold` | 默认仍可走 LiteLLM sidecar；direct 只允许作为显式设计例外 | direct 必须先通过能力探测和 Claude Code 真实请求验收 |
| `/version` 超时、404、非 JSON、缺少 version、版本不可解析 | LiteLLM sidecar | 打印 warning，记录 `VLLM_VERSION_PROBE_FAILED` |
| `MODEL_PROVIDER_BACKEND` 不是 `vllm` | 不执行 vLLM 版本阈值路由 | 交给对应 adapter 的探测和错误契约 |

### 5.3 能力门规则

能力探测在路由选定后执行。未通过能力门时，不允许启动 Agent job 或 Claude Code 执行路径。

| 条件 | 结果 | 错误码 |
| --- | --- | --- |
| `/v1/models` 不可用 | 请求失败 | 返回 `VLLM_MODELS_PROBE_FAILED` |
| chat completion 不可用 | 请求失败 | 返回 `VLLM_CHAT_PROBE_FAILED` |
| tool calling 不可用或不稳定 | 阻止 Agent job / Claude Code 执行路径 | 返回 `VLLM_TOOL_CALLING_UNSUPPORTED` |
| LiteLLM sidecar 自身不可用 | 请求失败 | 返回 `MODEL_PROVIDER_SIDECAR_UNAVAILABLE` |
| LiteLLM 对 Claude Code tool/streaming 验收失败 | 请求失败 | 返回 `LITELLM_CLAUDE_CODE_COMPAT_FAILED` |

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
3. 默认路由到 LiteLLM sidecar。
4. 若 LiteLLM sidecar 或能力探测仍失败，则把最终错误投影到 API response、job `error_json` 和用户可见状态。

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
- 版本成功但低于阈值属于预期路由决策，使用 `info`，不使用 `warning`。

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
- Agent job：写入 `error_json`，并在用户可见 tab/API 状态中展示失败原因。
- `/health`：暴露最近一次 provider probe 摘要，不暴露 secret 和 prompt。
- Langfuse / trace：只记录脱敏后的 route、probe、code、duration，不写 raw prompt 或 tool 参数。

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
- 是否能提供足够的脱敏日志、健康检查和失败诊断。

若 LiteLLM 未通过 Claude Code tool/streaming 验收，不实现 AgentGov 自研协议转换兜底；请求必须失败，并返回 `LITELLM_CLAUDE_CODE_COMPAT_FAILED`。AgentGov 的职责是把失败原因、probe item、endpoint、route、状态码和脱敏响应摘要投影到 API response、job `error_json`、`/health` 摘要和用户可见状态。

## 8. Formatter 接入整改

DSPy formatter 不能继续依赖 URL 字符串包含 `anthropic` 来推断 provider。后续整改必须：

1. 把 formatter 的模型 provider 和 API route 显式化，避免 `openai/<model>` 指向 Anthropic sidecar 或 `anthropic/<model>` 指向 OpenAI `/v1` 的半配置。
2. 低版本本地 vLLM 场景下，formatter 默认也通过同一 LiteLLM sidecar 路由，保证“所有 AgentGov 模型请求通过 sidecar”的一致性。
3. 如果后续确需让 formatter 直连 OpenAI-compatible vLLM，必须单独形成设计例外：说明为什么不是 Claude Code 请求、如何探测能力、如何避免与主 provider route 冲突。
4. formatter 不可用时，job 必须失败并写入 `error_json`，不得生成 raw/offline 伪成功结果。

## 9. 分阶段整改计划

| 阶段 | 目标 | 主要变更 | 验收 |
| --- | --- | --- | --- |
| P0 文档与配置边界 | 固化禁止手配版本、LiteLLM sidecar 默认策略和 warning 规则 | 更新 docs、env 示例和配置说明；禁止 `MODEL_PROVIDER_VLLM_VERSION` | 文档检查通过，env 示例无手配版本 |
| P1 Provider probe | 新增 vLLM `/version`、`/v1/models`、chat/tool capability probe | probe service、脱敏 warning、TTL 限频、结构化 probe result、`MODEL_PROVIDER_BACKEND` adapter 选择 | 单测覆盖版本成功、失败、不可解析、低版本 |
| P2 LiteLLM sidecar | 接入 LiteLLM 作为唯一协议转换 sidecar | LiteLLM service、healthcheck、由 `MODEL_PROVIDER_API_URL` 派生模型服务后端地址、错误映射 | fake vLLM 集成测试通过；Claude Code tool/streaming 不通过时报 `LITELLM_CLAUDE_CODE_COMPAT_FAILED` |
| P3 Runtime 路由 | Runtime/Agent job/Claude Code 统一使用 provider route | Runtime 内部把 sidecar 地址派生为 Claude Code 子进程 `ANTHROPIC_BASE_URL`；低版本和未知版本强制 sidecar | main-flow test 通过，job error_json 可见 |
| P4 Formatter 收敛 | 移除 URL 字符串 provider 推断 | formatter route 显式化，失败投影一致 | formatter 单测和批次 job 测试通过 |
| P5 模型服务连通性与能力验收用例 | 建立真实本地 vLLM gated live 验收 | 版本探测、chat、tool calling、LiteLLM sidecar streaming 验收用例 | 无凭据 skip；配置后 live pass 或给明确错误 |
| P6 收尾同步 | README、docs、skill 和发布说明同步 | README 模型接入运行说明、docs 索引、文档/Codex 硬门 | `check_docs_governance` 与 `check_codex_governance` 通过 |

## 10. 测试与验收场景

### 10.1 单元测试

- `/version` 返回 `0.13.0`：路由为 sidecar，日志为 info。
- `/version` 返回 `0.23.0`：允许进入 capability probe。
- `/version` 超时、404、非 JSON、缺少 `version`、版本不可解析：生成 `VLLM_VERSION_PROBE_FAILED`，打印 warning，默认 LiteLLM sidecar。
- 同一 endpoint + reason 在 TTL 内重复失败：只打印一次 warning。
- 配置中出现 `MODEL_PROVIDER_VLLM_VERSION`：设置层、env 示例或文档契约测试失败。
- 文档、env 示例或 settings 中出现公开 `MODEL_PROVIDER_UPSTREAM_URL`：文档契约测试失败。
- `MODEL_PROVIDER_BACKEND=vllm` 时，版本探测 URL 只能由 `MODEL_PROVIDER_API_URL` 派生。

### 10.2 集成测试

- fake vLLM 支持 `/version` 和 `/v1/models`，但 chat 不可用：返回 `VLLM_CHAT_PROBE_FAILED`。
- fake vLLM 支持 chat，但 tool calling 返回普通文本：返回 `VLLM_TOOL_CALLING_UNSUPPORTED`。
- LiteLLM sidecar 由 `MODEL_PROVIDER_API_URL` 派生的模型服务后端不可达：返回 `MODEL_PROVIDER_SIDECAR_UNAVAILABLE`。
- LiteLLM 无法兼容 Claude Code tool_use/tool_result 或 streaming event：返回 `LITELLM_CLAUDE_CODE_COMPAT_FAILED`，不启用自研转换兜底。
- Claude Code / Agent job 构造 options 时，`ANTHROPIC_BASE_URL` 指向 Runtime 派生的内部 sidecar 地址，而不是用户配置的第二 URL。
- formatter 失败时，job 写入稳定 `error_json`，用户可见状态不显示成功。

### 10.3 模型服务连通性与能力验收

模型服务连通性与能力验收只在私有 env 配置齐全时运行，缺少模型凭据或 vLLM 地址时 skip，不破坏离线 `make test` 产品不变量。该验收必须使用 Docker Compose 中的真实 API / worker / LiteLLM sidecar / vLLM 网络链路，不用 local-debug 结果替代容器结果。

必测项：

- `GET /version` 成功或失败诊断明确。
- `/v1/models` 可达。
- 最小 chat completion 可达。
- tool calling probe 可判定。
- LiteLLM sidecar `/health` 返回 backend route、version probe、capability probe 摘要。
- Agent job 从请求到失败或成功的用户可见状态一致。

## 11. 安全与日志约束

- 真实 API key、MCP header、数据库凭据、本机私有路径、prompt、messages、tool args 和模型 raw output 不得进入仓库、warning 或普通应用日志。
- warning 日志只允许脱敏 endpoint、错误类型、状态码、耗时、动作和阈值。
- `docker/.env`、`docker/.env.local-debug` 和 runtime volume 不得进入 staged diff。
- LiteLLM sidecar 调试抓包只能落临时目录，并必须脱敏后才能进入文档或评审材料。

## 12. 决策记录

| 决策 | 结论 |
| --- | --- |
| 是否允许 `MODEL_PROVIDER_VLLM_VERSION` | 不允许；版本事实只来自运行中的 `/version` |
| 是否允许公开 `MODEL_PROVIDER_UPSTREAM_URL` | 不允许；用户只维护 `MODEL_PROVIDER_API_URL`，LiteLLM sidecar 模型服务后端地址由它派生 |
| 是否通过 URL 字符串判断 provider | 不允许；使用 `MODEL_PROVIDER_BACKEND` 选择 adapter |
| `0.23.0` 是否是官方兼容起点 | 不是；它只是 AgentGov 可配置路由阈值，真实准入以探测和验收为准 |
| `/version` 不可用怎么办 | 打 warning、记录结构化诊断、默认 LiteLLM sidecar；LiteLLM sidecar 或能力探测失败时请求失败 |
| 低版本 vLLM 是否直连 Claude Code | 不直连；低于 `MODEL_PROVIDER_VLLM_SIDECAR_THRESHOLD` 全部走 LiteLLM sidecar |
| 阈值是否可配 | 可配；默认 `0.23.0`，仅表示策略阈值，不表示事实版本 |
| 能力不足是否可降级成 raw 成功 | 不允许；必须失败并给出稳定错误码 |
| sidecar 是否完全借助 LiteLLM | 是；不保留自研 thin normalizer 兜底 |
| LiteLLM 的 Claude Code tool/streaming 验收不通过怎么办 | 请求失败，返回 `LITELLM_CLAUDE_CODE_COMPAT_FAILED` 并投影到 `error_json` 和用户可见状态 |

## 13. 评审整改：方案稳健性补强（severe / medium）

> 来源：对本方案的一次独立再评审。只纳入与当前「LiteLLM sidecar + 能力门」架构相关的 severe / medium 项；与已废弃自研 thin normalizer 绑定的项不适用，见 §13.5。

### 13.1【severe】先抓包定性真实 Claude Code 请求，再固化「LiteLLM only、无自研兜底」

§7.2 与 §12 已决定 sidecar 完全借助 LiteLLM 且不保留自研兜底。该决策必须由对**真实 Claude Code 请求体的抓包与回放**前置验证，不能以「LiteLLM 必然兼容」为隐含假设：

- P0 / P1 增加硬前置：临时把 Runtime 注入的 `ANTHROPIC_BASE_URL` 指向受控抓包端点，打一条真实 `/api/chat`（含工具 / skills），dump CLI 实际外发的 Anthropic 请求体——重点记录 system 块位置、是否出现 system-in-messages、`tool_use` / `tool_result`、streaming 形态、`anthropic-version` / `anthropic-beta` 头；脱敏后只落临时目录（§11）。
- 用该真实请求体回放 LiteLLM sidecar，逐项核对 §10.2 的兼容项；只有回放通过，才把「LiteLLM only、无自研兜底」固化为决策。回放不通过则按 §5.3 / §10.2 返回 `LITELLM_CLAUDE_CODE_COMPAT_FAILED`，并据抓包事实评估补 LiteLLM 配置或调整协议层。
- 理由：对 vLLM 的探测只验证了**孤立标准请求形态**；上游 schema 校验在第一个错误即返回，真实 CC 请求可能串联多处不兼容，必须以抓到的真实请求为唯一依据，而非逐项假设「只差一处」。

### 13.2【severe】「协议连通」与「模型够用」拆成两道独立门，并前置最便宜的模型能力探测

- 验收口径区分两类门：**S-协议**（连通 / chat / streaming / tool 协议可用）与 **S-模型**（模型在完整 agent 循环与 governor schema-exact 输出上的质量）。§10.3 的 live 验收必须能区分失败属于协议未通过还是模型质量不达标，probe / error 字段需带该判定维度，避免把两类失败混成一个红灯。
- 在 P2 建 sidecar 之前先做**最便宜的模型能力探测**：用一条「类 Claude Code（claude_code 量级 system + 一组工具定义）」请求直打 vLLM OpenAI 端点，粗判模型的 `tool_use` 是否成形、是否乱循环。模型天花板先于工程投入暴露，避免在工具调用不达标的模型上投入完整 sidecar 与路由工程。这是 §5.3 能力门的「更早、更省」前移，不替代运行期能力门。

### 13.3【medium】流式错误事件也要归一化与验收

§10.2 已覆盖 streaming 成功事件。补充：Anthropic 流式以 SSE `event: error` 事件传递中途错误，而非 HTTP 状态码。LiteLLM sidecar 必须把上游中途错误规范成 Claude Code 可解析的 Anthropic `event: error`；验收项新增「流中途错误事件可被 CLI 正确识别并触发其重试 / 终止逻辑」，否则流式路径会静默挂起或误判。

### 13.4【medium】system / 消息映射的语义保真

LiteLLM 把 Claude Code 的 system（顶层块，及可能出现的 system-in-messages）映射到 vLLM OpenAI `messages` 时，可能因位置 / 合并而发生语义位移且**不报错**。补充验收：对同一请求，核对经 sidecar 转换前后模型实际看到的 system 语义是否一致（行为对比，而非仅「请求通过」），并在 §10.2 增加对应用例。

### 13.5 不适用说明

- **「自建代理依赖需显式固定（如 httpx）」不适用**：本方案已决定不保留自研 thin normalizer（§7.2 / §12），sidecar 唯一实现依赖为 LiteLLM，不引入自建反代的传递依赖固定问题。
