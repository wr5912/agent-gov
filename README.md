# 智能体治理平台 AgentGov

一个 **Docker 化的智能体治理平台 AgentGov** 项目（Agent Runtime · Feedback Loop · Version Governance）。

AgentGov 不提供通用协作看板，也不替代 Multica、Jira、GitHub Issues 等协作平台。外部协作平台可以在长期生态集成阶段把任务分配给 AgentGov 管理下的受治理业务 Agent；当前重点是打磨 AgentGov 自身的 Runtime、反馈闭环、归因优化、评估回归、版本治理和多业务 Agent 治理能力。

目标：

- 不重写 Claude Agent loop。
- 通过 Docker 容器封装 Claude Agent SDK / Claude Code Runtime。
- 通过 Runtime Profile 隔离业务 Agent 与单一治理智能体（governor）：业务 Agent（含预制 `main-agent`）住 `data/business-agents/<agent_id>/workspace`、各自独立并列的 `claude-root`；唯一特殊的是治理智能体 governor（`/governor-workspace` 与 `claude-roots/governor`），承担归因、优化方案、执行和回归用例生成。
- 容器对外提供 HTTP API，供 Web UI、业务系统、Agent 平台控制面调用。上层业务系统集成 AgentGov 底座的权威参考见 [docs/AgentGov集成指南.md](docs/AgentGov集成指南.md)（契约真相源是容器 OpenAPI `/openapi.json`、`/docs`）。

## 目录结构

README 只描述稳定模块边界，不维护逐文件清单，避免实现持续迭代时文档频繁漂移。精确文件列表以 `git ls-files` 和当前工作区为准。

```text
.
├── app/                         # FastAPI API 和 Runtime 控制面
│   ├── runtime/                 # Claude SDK 适配、profile、版本、schema 和运行时支撑
│   │   ├── stores/              # 反馈闭环 SQLite store facade 与领域 mixin
│   │   ├── response_schemas/    # HTTP response schema
│   │   ├── records/             # 内部 Pydantic record
│   │   ├── integrations/        # Langfuse、外部治理等适配器
│   │   ├── prompts/             # 反馈闭环 Agent prompt 构造
│   │   └── normalizers/         # LLM 输出归一化
│   └── services/                # 跨 store/runtime/profile 的应用服务编排
├── frontend/                    # React/Vite UI：Playground、反馈工作台、评估和版本视图
├── docker/                      # Dockerfile、Compose、entrypoint、运行卷模板、vendored A2UI SDK
│   ├── runtime-volume-seeds/        # 运行卷初始种子（Compose 只读挂载，镜像内保留兜底）
│   │   ├── governor-workspace/  # 治理智能体 governor 种子（顶层，按 job_type 执行治理任务）
│   │   ├── data/business-agents/main-agent/workspace/  # 预制业务 Agent main 的配置种子
│   │   └── templates/business-agent/  # 新业务 Agent 创建模板 catalog（general…）
│   └── vendor/                  # vendored Google A2UI Python SDK
├── docs/                        # 架构、治理和示例配置文档
├── tests/                       # 后端测试
├── scripts/                     # 维护脚本
├── Makefile
├── pyproject.toml
└── requirements.txt
```

运行态目录不在仓库内：容器部署默认根为宿主机 `${HOME}/volume-agent-gov`，本机调试默认 `/tmp/local-debug-volume-agent-gov`，由 `make runtime-bootstrap` / `make local-debug-bootstrap` 或容器 entrypoint 从 `docker/runtime-volume-seeds/` 渲染补齐。Compose 默认把当前 checkout 的 `docker/runtime-volume-seeds/` 只读挂到 API/worker 容器内，Docker 镜像内仍保留一份 seed 作为不带宿主机模板时的兜底。其下含 `governor-workspace/`、`claude-roots/governor`、`data/`（`business-agents/<id>/{workspace,claude-root,version}`——含预制 main-agent；`runtime.sqlite3`、证据/任务临时文件、候选 worktree、发布归档）和可选 `langfuse/`，均默认不提交。`docker/volume/` 不是当前布局，仅在显式沿用旧目录时作为迁移来源或兼容路径。

## 快速启动

```bash
make setup
```

编辑 `docker/.env`：

```bash
MODEL_PROVIDER_BACKEND=anthropic_compatible
MODEL_PROVIDER_API_KEY=<your-model-provider-api-key>
# vLLM 场景改为 MODEL_PROVIDER_BACKEND=vllm，并填写不带 /v1 的 base URL。
# MODEL_PROVIDER_API_URL=http://vllm:8000
API_KEY=<your-runtime-api-key>
HOST_PORT=58080
API_PORT=8080
AGENT_MODEL=claude-sonnet-4-5
```

`docker/.env.example` 已包含端口、模型提供商、Claude Agent SDK 运行参数、路径、权限、skills、MCP、hooks、session 等配置项的注释。默认端口遵循项目规则 `50000 + 容器端口`，例如 API 端口映射为 `58080:8080`。

离线部署表示不依赖公网远程服务，不表示无模型运行；反馈闭环、执行优化和 DSPy 输出规范化应指向本地或内网模型网关。模型接入通过 `MODEL_PROVIDER_BACKEND` 显式选择 adapter，不通过 URL 字符串推断 provider；`MODEL_PROVIDER_API_URL` 是唯一真实模型服务 URL。vLLM 场景中 `ANTHROPIC_BASE_URL` 由 Runtime 派生为内部 LiteLLM sidecar 地址，不要求用户维护第二个 upstream URL。Agent 若没有输出精确匹配 `schema_version` 的完整 JSON，Runtime 会交给 DSPy formatter 规范化，formatter 不可用时 job 会失败并写入 `error_json`，不会生成 offline/raw 占位结果。

Docker 构建阶段已在 Dockerfile 中固定使用国内镜像源：Debian apt 使用阿里源，uv/pip 使用阿里 PyPI 源，Node 包源使用 npmmirror；这些源不再通过 `docker/.env` 覆盖，避免不同机器构建时漂移。Compose 运行环境也会固定同名 pip/uv/pnpm 变量，避免已有本地 `docker/.env` 旧变量影响容器内后续安装命令。基础镜像固定使用 `python:3.11-slim` 和 `node:22-alpine`；基础镜像拉取没有统一、稳定的公共国内 registry 可直接写死，建议通过 Docker daemon registry mirror 或团队内网基础镜像仓库处理，如需切换应直接修改 Dockerfile 的 `FROM` 行。

镜像构建阶段会安装 `a2ui-adk` 相关 Python 依赖，并从 `docker/vendor/A2UI/agent_sdks/python` 安装已 vendor 的 Google A2UI v0.9 Python SDK。PyPI 依赖在 build 阶段完成下载，容器运行时不会再为 `a2ui-adk` 访问互联网。

`LITELLM_LOCAL_MODEL_COST_MAP=True` 会强制 LiteLLM 使用包内置模型价格表，避免启动或 import 时访问 GitHub 获取远程 cost map。Compose 会启动独立 `agent-gov-litellm-sidecar` 服务；当 `MODEL_PROVIDER_BACKEND=vllm` 时，Runtime 会先通过 `{MODEL_PROVIDER_API_URL}/version` 探测运行中 vLLM 版本，低版本、未知版本或探测失败默认走 sidecar；仅当显式 `MODEL_PROVIDER_VLLM_ALLOW_DIRECT=true` 且探测版本 >= `MODEL_PROVIDER_VLLM_SIDECAR_THRESHOLD` 时才直连 vLLM 原生 Anthropic 端点（仍受能力门验收，不兼容即 fail-closed）。sidecar 或 `/v1/models` 不可达时阻断 Agent job，返回稳定模型接入错误。

为减少 bind mount 权限问题，Compose 中的 API 容器默认以 root 运行，启动时会对 `${HOME}/volume-agent-gov/data/`、main 与 governor workspace 和 `${HOME}/volume-agent-gov/claude-roots/*` 对应的容器挂载目录执行 `chmod -R a+rwX`，方便直接写入。生产环境如果需要收紧权限，可以再切换到非 root 用户并配套处理宿主机目录 owner/ACL。

启动：

```bash
make build
make up
make logs
```

`make up` 会同时启动 API、前端相关服务和 `claude-agent-worker`。改进事项闭环中需要模型执行的归因、方案、执行计划和回归用例生成由 governor 生成，结果写入 `/api/improvements/{improvement_id}/...` 对应内容子资源，并保留 Langfuse Trace 引用供前端查看。

只新增或修改 `docker/runtime-volume-seeds/` 下的预制业务 Agent 配置时，在已使用当前 Compose 配置 recreate 过容器后，不需要重建镜像；重启 API/worker 会让 entrypoint 按宿主机只读挂载的 seed 重新 bootstrap。修改 Dockerfile、Python 代码、bootstrap 渲染逻辑或依赖时仍需 `make build` 并 recreate。

治理类 Agent job、改进事项生成动作和前端治理请求默认使用 `GOVERNANCE_AGENT_TIMEOUT_SECONDS=300`。`DSPY_OUTPUT_FORMATTER_TIMEOUT_SECONDS` 只是高级覆盖项，未配置时跟随治理超时；业务 Playground 的流式 idle timeout、模型探测超时和 Docker healthcheck 不共用该值。Web HITL 人工确认等待使用独立的 `HITL_TIMEOUT_SECONDS=300`，只影响 `/api/chat/stream` 的人工确认请求。Web HITL 的 SDK callback 等待态保存在 API 进程内存中，开启 `ENABLE_CLAUDE_WEB_HITL=true` 时必须保持单 API 进程；`WEB_CONCURRENCY`、`API_WORKERS` 或 `UVICORN_WORKERS` 大于 1 会在启动时失败。

健康检查：

```bash
make smoke
```

部署到 Docker Compose 主机：

```bash
scripts/deploy_agent_gov_to_host
# 或指定目标主机，默认用户 root、默认目录 ~/work/agent-gov
scripts/deploy_agent_gov_to_host 172.16.112.232
# 本机部署不走 SSH，默认目录按当前用户解析
scripts/deploy_agent_gov_to_host localhost
```

该脚本会先校验本地 git `origin` 远端项目名必须是 `agent-gov`，然后把
`origin/master` 的跟踪代码同步到目标目录。远端目标使用 `root@<host>:~/work/agent-gov`，
`localhost`、`127.0.0.1` 和 `::1` 目标直接在本机执行，不经过 SSH。
远端同步代码时会强制归属为 `root:root`，避免保留本地开发用户的数值 UID/GID。
在本机构建 `VERSION` 对应的 `agent-gov-api`、`agent-gov-ui` 和
`agent-gov-litellm-sidecar` 镜像，并把项目镜像包和 Langfuse 依赖镜像包写入目标
`images/` 目录。目标私有 `docker/.env` 会被保留；如果目标尚未
创建该文件，脚本才会从 `docker/.env.example` 初始化。启动时使用目标 `docker/.env`
和目标 `${HOME}/volume-agent-gov` 运行态目录，先 `down --remove-orphans` 删除 Compose
管理的容器，再按目标 `docker/.env` 的 `CONTAINER_NAME_PREFIX` 清理残留容器，最后用
`--force-recreate --no-build --pull never` 启动包含 Langfuse profile 的全量服务。当前
Compose 默认容器名前缀是 `agent-gov`，它只是默认值；脚本以目标 `docker/.env` 为准。

只重启已有部署，不同步代码、不构建镜像：

```bash
scripts/restart_agent_gov_on_host
# 或指定目标主机，默认用户 root、默认目录 ~/work/agent-gov
scripts/restart_agent_gov_on_host 172.16.112.232
# 本机重启不走 SSH，默认目录按当前用户解析
scripts/restart_agent_gov_on_host localhost
```

重启脚本同样会先校验本地 git `origin` 远端项目名必须是 `agent-gov`。目标目录必须已有
`docker/.env` 和已加载镜像；脚本会 `down --remove-orphans`，按 `CONTAINER_NAME_PREFIX`
清理残留容器，再用 `--force-recreate --no-build --pull never` 重新创建包含 Langfuse
profile 的全量服务，然后检查 API、UI 和 Langfuse 是否可访问。

## 前端 UI

`frontend/` 是一个 React/Vite 前端，用于对接本项目已有的 AgentGov API。它包含 Playground 聊天、会话管理、subagents/skills 发现、Claude 配置映射摘要、反馈信号、改进事项、证据包、归因分析、优化方案、执行记录、回归评估和 Agent 版本管理。前端默认使用 Claude 暖色系界面。

先启动后端：

```bash
make up
```

启动前端开发服务：

```bash
cd frontend
pnpm install
pnpm dev
```

打开：

```text
http://localhost:5173
```

Runtime API 设置默认读取前端环境变量：`VITE_RUNTIME_API_BASE` 默认是 `http://localhost:58080`，`VITE_RUNTIME_API_KEY` 可填后端 `docker/.env` 中的 `API_KEY`。如果浏览器通过远程地址访问前端（例如 `http://172.16.138.228:55173`），前端会把默认 loopback API 地址自动迁移为同一浏览器主机的 `:58080`，避免远程浏览器把 `localhost` 解析成自己的机器。开发模式下，Vite 会把 `/api`、`/health`、`/v1` 代理到 `VITE_DEV_PROXY_TARGET`，默认也是 `http://localhost:58080`。

前端构建检查：

```bash
pnpm build
pnpm preview
```

Docker Compose 前端服务使用 `docker/.env` 注入配置：

```bash
make ui-build
make ui-up
make ui-smoke
```

反馈优化工作台浏览器回归使用 Playwright，默认读取 `docker/.env` 中的 `API_KEY` 并按 Compose 端口访问 `http://localhost:55173` 和 `http://localhost:58080`。该检查会创建一条测试反馈信号和改进事项，并把截图写入根目录 `artifacts/`：

```bash
make ui-feedback-smoke
```

默认访问地址：

```text
http://localhost:55173
```

相关配置项为 `FRONTEND_HOST_PORT`、`FRONTEND_RUNTIME_API_BASE`、`FRONTEND_RUNTIME_API_KEY`、`FRONTEND_LANGFUSE_URL`。`FRONTEND_RUNTIME_API_KEY` 留空时，Compose 会复用 `API_KEY`。

这个 UI 不接管 Claude Code CLI 进程，不编辑宿主机敏感文件，不提供 Terminal。聊天、反馈闭环、评估和版本管理都通过后端 Runtime API 完成。

每条 Claude Agent 回复的“回复细节”会保留完整 SDK/流式事件，并汇总本次请求的 Skill / Tool 使用情况。详情窗口支持关键字查找事件内容，底层 JSON 会完整展开显示。

## 反馈优化闭环

Runtime 的反馈优化闭环以多 Agent 架构为准。每次 `/api/chat` 或 `/api/chat/stream` 都会生成 `run_id`，并在 SQLite 中写入本次回答的轻量运行记录。Playground 回复上的反馈入口只采集 feedback signal；用户在“改进事项”中把反馈归并为事项后，按反馈整理、归因分析、优化方案/执行、测试发布四个工作面板推进。治理 Agent 生成的归因、优化方案、执行记录和回归评估都写入事项级内容子资源，并保存 `generation_trace_id` / `generation_trace_url`。

完整 API 以运行时 OpenAPI 为准：本地运行后访问 `http://localhost:58080/openapi.json`，或使用 `scripts/export_openapi.py` 导出临时 OpenAPI JSON。下面仅保留按职责分组的高层索引，避免 README 随接口细节频繁漂移：

前端 OpenAPI 类型由运行时 schema 临时导出后生成，命令为：

```bash
pnpm --dir frontend generate:api-types
```

- 反馈采集与处置单：`GET /api/agent-runs`、`POST/GET /api/feedback-signals`、`GET /api/feedback-signals/{signal_id}`、`POST/GET /api/soc-events`、`GET /api/soc-events/{event_id}`、`GET /api/pending-correlations`、`POST /api/pending-correlations/{pending_id}/resolve`、`POST/GET /api/feedback-cases`、`GET /api/feedback-cases/{feedback_case_id}`。
- Agent job 队列：`GET /api/agent-jobs`、`GET /api/agent-jobs/{job_id}`。
- 证据包与分析任务：`POST /api/feedback-cases/{feedback_case_id}/evidence-packages`、`GET /api/evidence-packages/{evidence_package_id}`、`GET /api/evidence-packages/{evidence_package_id}/files/{file_name}`、`POST /api/feedback-cases/{feedback_case_id}/attribution-jobs`、`POST /api/feedback-cases/{feedback_case_id}/attribution-jobs/regenerate`。
- 改进事项四阶段内容：`POST/GET /api/improvements`、`GET /api/improvements/{improvement_id}`、`POST /api/improvements/{improvement_id}/lifecycle`、`POST /api/improvements/{improvement_id}/archive`、`GET/PUT /api/improvements/{improvement_id}/normalized-feedback`、`POST /api/improvements/{improvement_id}/normalized-feedback/confirm`、`GET/PUT /api/improvements/{improvement_id}/attribution`、`POST /api/improvements/{improvement_id}/attribution/generate`、`POST /api/improvements/{improvement_id}/attribution/confirm`、`GET/PUT /api/improvements/{improvement_id}/optimization-plan`、`POST /api/improvements/{improvement_id}/optimization-plan/generate`、`POST /api/improvements/{improvement_id}/optimization-plan/confirm`、`GET/PUT /api/improvements/{improvement_id}/execution`、`POST /api/improvements/{improvement_id}/execution/apply`、`POST /api/improvements/{improvement_id}/execution/confirm`、`GET /api/improvements/{improvement_id}/regression-assessment`、`POST /api/improvements/{improvement_id}/regression-assessment/generate`、`POST /api/improvements/{improvement_id}/regression-assessment/confirm`、`GET /api/langfuse/traces/{trace_id}`。
- 评估、回归资产和版本治理：`POST /api/eval-datasets/feedback/sync`、`GET /api/eval-cases`、`PATCH /api/eval-cases/{eval_case_id}`、`POST/GET /api/eval-runs`、`GET /api/eval-runs/{eval_run_id}`、`POST/GET /api/eval-runs/{eval_run_id}/impact-analysis`、`GET /api/regression-assets`、`GET/PATCH /api/regression-assets/{eval_case_id}`、`POST /api/regression-assets/{eval_case_id}/promote`、`POST /api/regression-assets/{eval_case_id}/archive`、`POST /api/regression-assets/{eval_case_id}/mark-flaky`、`POST /api/regression-assets/{eval_case_id}/unmark-flaky`、`POST /api/regression-assets/{eval_case_id}/supersede`、`GET /api/regression-assets/{eval_case_id}/revisions`、`GET /api/regression-assets/{eval_case_id}/governance-events`、`GET /api/agent-repository`、`POST /api/agent-repository/discard-changes`、`POST /api/agent-repository/snapshot`、`GET /api/agent-repository/current`、`POST/GET /api/agent-change-sets`、`GET /api/agent-change-sets/{change_set_id}`、`GET /api/agent-change-sets/{change_set_id}/events`、`GET /api/agent-change-sets/{change_set_id}/diff`、`GET /api/agent-change-sets/{change_set_id}/file-diff`、`POST /api/agent-change-sets/{change_set_id}/publish`、`GET /api/agent-releases`、`GET /api/agent-releases/{release_id}`、`POST /api/agent-releases/{release_id}/restore`。

运行态数据默认保存在 Docker 数据卷 `/data` 下，对应宿主机 `${HOME}/volume-agent-gov/data/`：

- `/data/runtime.sqlite3` 是反馈信号、SOC 事件、处置单、证据包 manifest 和文件内容、`agent_jobs`、改进事项、四阶段内容子资源、评估用例、评估运行和 API session 的权威存储。
- 归因、方案、执行和回归用例生成的治理 Agent 结果都以 SQLite 为权威存储；后端从 SQLite、证据包和 Langfuse trace 构造 prompt context，不再要求内部 Agent 读取 job 输入目录。
- `/data/business-agents/<agent_id>/workspace` 是业务智能体 Git 版本源；候选 worktree 默认在同级 `version/worktrees/`，发布归档默认在 `version/releases/`。预制 `main-agent` 对应 `/data/business-agents/main-agent/version/{worktrees,releases}`。
- `/data/feedback-signals/`、`/data/soc-events/`、`/data/feedback-cases/` 等旧目录仅为兼容路径，不再是权威存储。

当前实现基线见 [反馈闭环当前实现基线.md](docs/反馈闭环当前实现基线.md)。旧版 `FEEDBACK_OPTIMIZATION_LOOP_MVP.md` 已废弃，旧接口语义不再作为实现依据。

## Langfuse 监控

本项目优先通过 Claude Code 内置 OpenTelemetry 导出能力接入 Langfuse。开启后，API 运行时会把 `docker/.env` 中的 Langfuse 配置转换为 Claude Code 子进程可识别的 `CLAUDE_CODE_*` 和 `OTEL_*` 环境变量。

本系统的 Langfuse 只按本地 Docker profile 部署。容器内 API/worker 通过
Compose 服务名访问 Langfuse Web，宿主机和浏览器通过映射端口访问。
`docker/.env.example` 已内置默认值；启用 telemetry 时只需要补齐本地
Langfuse 初始化出的项目 key：

```bash
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-local-dev
LANGFUSE_SECRET_KEY=sk-lf-local-dev
LANGFUSE_BASE_URL=http://langfuse-web:3000
LANGFUSE_NEXTAUTH_URL=http://localhost:53000
FRONTEND_LANGFUSE_URL=http://localhost:53000
LANGFUSE_OTEL_SIGNALS=traces,metrics
```

### Runtime enrich 与 input/output

Claude Code 原生 OTEL 会导出 `claude_code.interaction`、`claude_code.llm_request`、`claude_code.tool` 等 span，但它们只携带链路结构、`tool_name`、token 和 `user_prompt` 等属性，**标准 observation 的 `input` / `output` 为空**——因为 Claude Code 把工具入参/结果、raw API body 走 OTLP `logs` 信号导出，而当前 Langfuse（v3）没有 `/v1/logs` 接收端（返回 404），这些内容不会进入 Langfuse。所以 `claude_code.*` span 的 `input` / `output` 列为空属预期，不是缺陷。

为让请求/响应、逐工具入参/结果和逐轮报文在 Langfuse 里真正可读，Runtime 在启用 Langfuse 且配置了 public/secret key 时，从 **claude-agent-sdk 原生 message 流**投影出自建 observation（不解析 CLI transcript、不重写 agent loop）：

- `runtime.main_agent` / `runtime.governor.{job_type}`：API 层 / 治理 job 根 span，记录请求输入、最终回答、SDK 消息、usage、cost、stop_reason 和 errors，并写入 trace-level `input` / `output`。
- `runtime.main_agent.claude_sdk_query`：业务聊天的 Claude SDK 调用 generation，记录实际 prompt/model、输出、token usage、成本和错误状态。
- `sdk.tool.{tool_name}`：**每个工具一条 span**，`input` = `ToolUseBlock.input`（工具入参）、`output` = `ToolResultBlock.content`（工具结果），工具报错映射为 ERROR level。
- `sdk.llm.{turn}`：**每个 LLM 轮次一条 generation**，`input` = 该轮增量 messages（报文）、`output` = 该轮 assistant 内容、`model` + 逐轮 token usage。

Runtime 会把 Langfuse trace 的 Session 设为 Playground/API 层 `session_id`，并在 metadata 中同时保留 `api_session_id`、`run_id` 和运行上下文；Claude SDK 返回的 `sdk_session_id` 仍保存到 SQLite，用于后续会话 resume。

本项目把 Langfuse 定位为本地调测工具。`LANGFUSE_ENABLED=true` 时，Runtime 默认向 Claude Code 子进程开启 `OTEL_LOG_USER_PROMPTS`、`OTEL_LOG_TOOL_DETAILS`、`OTEL_LOG_TOOL_CONTENT` 和 `OTEL_LOG_RAW_API_BODIES`（`user_prompt` 经 traces 落为 span 属性）；逐工具入参/结果/报文以上述 `sdk.*` 观测在同一条 trace 中查看。`LANGFUSE_OTEL_SIGNALS` 默认 `traces,metrics`（不含被 Langfuse 丢弃的 `logs`）。Runtime 输出中的 `agent_activity` 字段会额外汇总 requested skills、实际 Skill 调用、tool calls 和 tool results。

### 本地 Langfuse Docker profile

Langfuse 自托管 profile 默认不随 API 启动。它按 Langfuse 官方 v3 低规模 Docker Compose 形态运行：`langfuse-web`、`langfuse-worker`、Postgres、ClickHouse、Redis、MinIO。持久化数据统一写入 `${HOME}/volume-agent-gov/langfuse/`。ClickHouse 默认固定为 `24.3`，满足 Langfuse v3 的最低版本要求。

启动本地 Langfuse：

```bash
make langfuse-up
make langfuse-smoke
```

默认访问地址：

- Langfuse UI: `http://localhost:53000`
- MinIO API: `http://localhost:59000`
- MinIO Console: `http://localhost:59001`

这些端口均可在 `docker/.env` 中调整：`LANGFUSE_HOST_PORT`、`LANGFUSE_MINIO_HOST_PORT`、`LANGFUSE_MINIO_CONSOLE_HOST_PORT`。默认端口遵循项目规则 `50000 + 容器端口`。

#### 远端访问

Langfuse Web 与 MinIO 端口默认绑定 `0.0.0.0`，远端用户可通过 `http://<宿主机地址>:53000` 访问 Langfuse 界面。仅限本机访问时在 `docker/.env` 设 `LANGFUSE_BIND_IP=127.0.0.1`。

前端 topbar 的 Langfuse 按钮在地址为本机/缺省（`http://localhost:53000`）时，会按当前浏览器访问的 host 自动派生跳转地址（端口沿用配置值），因此远端用户点击会跳到 `http://<当前访问host>:53000` 而非 `localhost`，无需为每个部署硬编码 IP。若把 `FRONTEND_LANGFUSE_URL` 显式设为非本机的可达地址，则按该配置跳转。

要让 Langfuse 自身的登录与媒体上传在远端完全可用，需在私有 `docker/.env` 把以下地址指向宿主机外部地址：`LANGFUSE_NEXTAUTH_URL`、`LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT`、`LANGFUSE_S3_BATCH_EXPORT_EXTERNAL_ENDPOINT`。暴露到网络时务必同时替换 `LANGFUSE_SALT`、`LANGFUSE_NEXTAUTH_SECRET`、`LANGFUSE_ENCRYPTION_KEY`、数据库/Redis/MinIO 密码和初始化账号密码。

让 API 容器把 Claude Code telemetry 写入本地 Langfuse profile：

```bash
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-local-dev
LANGFUSE_SECRET_KEY=sk-lf-local-dev
LANGFUSE_BASE_URL=http://langfuse-web:3000
```

然后重启 API 并发起一次请求：

```bash
make up
make smoke
make chat
```

`docker/.env.example` 中提供了 headless initialization 默认值，会在首次启动 Langfuse 时创建本地开发用组织、项目、API key 和管理员账号。生产环境必须替换 `LANGFUSE_NEXTAUTH_SECRET`、`LANGFUSE_SALT`、`LANGFUSE_ENCRYPTION_KEY`、数据库密码、Redis 密码、MinIO 密码和初始化用户密码。

停止 Langfuse profile：

```bash
make langfuse-stop
```

启用 Langfuse 后，Runtime 默认把以下 Claude Code 内容采集开关传给子进程：

```bash
OTEL_LOG_USER_PROMPTS=1
OTEL_LOG_TOOL_DETAILS=1
OTEL_LOG_TOOL_CONTENT=1
OTEL_LOG_RAW_API_BODIES=1
```

当前前端调试界面、Playground 证据面板和自托管 Langfuse 只面向开发调试人员，不作为生产安全边界；这些面默认保留完整 prompt、tool input/output、job input/output、raw text 和 trace I/O，便于定位 Claude Code / Agent SDK 运行问题。真实密钥、MCP header、数据库凭据、本机私有路径和运行态数据仍不得写入仓库、提交说明或公开文档。

容器启动后可通过 `/health` 查看状态字段：`langfuse_enabled`、`langfuse_public_key_configured`、`langfuse_secret_key_configured`、`langfuse_otel_signals`。不要把真实 Langfuse key 写入 `docker/.env.example` 或提交到仓库。

`/health` 还会返回 `runtime_dependency_versions`，用于确认当前运行时实际解析到的 `claude-agent-sdk`、bundled Claude Code CLI、`langfuse` 和 OpenTelemetry 版本。`make langfuse-smoke` 会检查本地 Langfuse health、Runtime 版本、Redis/Bull ingestion 队列和最近一条 `runtime.*` trace 的基本结构；没有配置 Langfuse API key 或尚未产生 trace 时，会跳过对应深度检查。

## 版本与发布

版本唯一真相源是仓库根 `VERSION` 文件（semver，如 `2.7.15`）。其余全部派生、严格对齐，不允许第二个独立版本数字：

- 后端 `app/version.py` 读取 `VERSION` → OpenAPI `info.version` 与 `/health` runtime_version。
- 前端 `frontend/package.json` 的 `version` 同步自 `VERSION`（`make sync-version`）。
- docker 镜像 tag 由 `make build` / `make up` 从 `VERSION` 注入 `${APP_VERSION}` 派生，不在 compose 硬编码。
- git release tag 为 `v` + `VERSION`。

发布流程：① 改 `VERSION` → ② `make sync-version` → ③ `make test`（含版本一致性硬门）→ ④ commit → ⑤ `git tag v$(cat VERSION) && git push --tags`。

`scripts/check_version_consistency.py`（已并入 `make test` 的 `codex-guard`）断言上述各处与 `VERSION` 一致，并在 HEAD 带 `v*` tag 时要求其等于 `v`+`VERSION`，从根上堵住"打 tag 不 bump 版本号"的漂移。

## API 文档

容器启动后，FastAPI 自动提供详细 OpenAPI 文档：

- Swagger UI: `http://localhost:58080/docs`
- ReDoc: `http://localhost:58080/redoc`
- OpenAPI JSON: `http://localhost:58080/openapi.json`

如果你在 `docker/.env` 中修改了 `HOST_PORT`，把上面的 `58080` 替换成对应端口。`/health` 响应也会返回这些文档 URL。
当 `API_KEY` 非空时，Swagger UI 里先点击 `Authorize`，输入 `docker/.env` 中的 `API_KEY`；curl 请求则添加 `Authorization: Bearer $API_KEY`。

## 聊天 API

```bash
export API_BASE=http://localhost:58080
export API_KEY="$(awk -F= '$1 == "API_KEY" {sub(/^[^=]*=/, ""); print; exit}' docker/.env)"

curl -X POST "$API_BASE/api/chat" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d '{
    "message": "请说明当前 workspace 中有哪些 subagents 和 skills",
    "skills_mode": "all"
  }'
```

指定 subagent 和 skill：

```bash
curl -X POST "$API_BASE/api/chat" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d '{
    "agent": "soc-analyst",
    "skills": ["alert-triage"],
    "message": "分析这个告警：rundll32.exe 加载 WININET.dll，父进程为 EdgeUpdate，命令行为 DispatchAPICall 1"
  }'
```

流式接口：

```bash
curl -N -X POST "$API_BASE/api/chat/stream" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d '{"message":"你好，先介绍你的能力", "skills_mode":"all"}'
```

## OpenAI Compatible 接口

项目额外提供了一个最小的非流式 OpenAI Compatible shim：

```bash
curl -X POST "$API_BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d '{
    "model": "claude-sonnet-4-5",
    "messages": [
      {"role": "user", "content": "请介绍当前 Agent Runtime 的能力"}
    ]
  }'
```

注意：这是兼容接入用的轻量 shim，不是完整 OpenAI API 实现；真正的 Agent 偏好参数，如 `agent`、`skills`，建议使用 `/api/chat`。工具权限、MCP 和 hooks 以 Claude Code 官方配置文件为准。

## 管理 API

```bash
curl -H "Authorization: Bearer $API_KEY" "$API_BASE/api/agents"
curl -H "Authorization: Bearer $API_KEY" "$API_BASE/api/skills"
curl -H "Authorization: Bearer $API_KEY" "$API_BASE/api/config"
curl -H "Authorization: Bearer $API_KEY" "$API_BASE/api/sessions"
```

## 配置挂载说明

`docker/docker-compose.yml` 会为容器内 API/worker 注入 `RUNTIME_CONTAINER=1`，Runtime 自动读取 `docker/.env`，并从 `HOST_RUNTIME_VOLUME_ROOT` 派生运行态目录。`docker/.env.example` 默认值为 `${HOME}/volume-agent-gov`；本机 PyCharm/uvicorn 调试在宿主机进程中自动读取 `docker/.env.local-debug`，默认使用 `/tmp/local-debug-volume-agent-gov`，避免调试数据与容器部署数据混用：

```yaml
volumes:
  - ./runtime-volume-seeds:/app/docker/runtime-volume-seeds:ro
  - ${HOST_RUNTIME_VOLUME_ROOT}/governor-workspace:/governor-workspace
  - ${HOST_RUNTIME_VOLUME_ROOT}/data:/data
  - ${HOST_RUNTIME_VOLUME_ROOT}/claude-roots/governor:/claude-roots/governor
```

可复用模板保存在 `docker/runtime-volume-seeds/`，实际运行目录由 `make runtime-bootstrap`、`make local-debug-bootstrap` 或容器 entrypoint 从模板补齐。Compose 中 `RUNTIME_VOLUME_SEEDS_HOST_DIR` 默认是 `./runtime-volume-seeds`（相对 `docker/docker-compose.yml`），以只读 bind mount 覆盖镜像内 `/app/docker/runtime-volume-seeds`；部署目录不包含该模板时 Compose 会失败，而不是创建空目录并误覆盖镜像兜底。真实运行态文件默认不提交到 git；其中 `runtime.sqlite3`、临时 job、Agent 版本仓库、候选 worktree、发布归档和 Langfuse 运行数据都属于本地运行态。

常用模板命令：

```bash
make runtime-bootstrap
make runtime-repair-managed-config
make local-debug-env
make local-debug-bootstrap
make local-debug-repair-managed-config
make runtime-volume-seeds-scan
make runtime-volume-seeds-export
make runtime-clean
make local-debug-clean
make runtime-volume-seeds-clean
make clean-runtime-artifacts
```

当前开发阶段默认保持目录清爽：`runtime-repair-managed-config`、`local-debug-repair-managed-config`、`runtime-volume-seeds-export` 和 `runtime-volume-seeds-restore` 成功后会自动清理 `.runtime-volume-seeds-backups`、`.runtime-volume-seeds-staging`、旧式 `*.bak-*` 和模板替换临时目录。运行态配置回滚依赖 Git、runtime-volume-seeds 重新渲染和 Agent version/release 机制，不依赖散落备份文件。

`runtime-volume-seeds-export` 会把 `${HOME}/volume-agent-gov` 中允许导出的配置复制到 staging，完成脱敏和校验后才替换模板，并在成功后清理 staging 和临时备份。API key、MCP header、数据库凭据、IP、PORT、URL、本机路径、`.mcp.local.json`、`.env`、SQLite、日志、transcripts、uploads、worktrees 和 release archives 都不会进入模板。

## subagent 文件格式

示例：`docker/runtime-volume-seeds/main-workspace/.claude/agents/soc-analyst.md`

```markdown
---
name: soc-analyst
description: 告警研判与安全事件初筛专家。用于分析告警、资产、进程、网络连接、规则命中和时间线。
tools:
  - Read
  - Grep
  - Glob
  - Bash
  - mcp__sec-ops-data__*
model: inherit
---

你是 SOC 告警研判专家。工作目标是快速判断告警是否值得升级，并给出证据链。
```

项目依赖 Claude Code 原生发现机制加载 `.claude/agents/*.md`。后端不会通过 SDK Options 显式注入 agents。

## skill 文件格式

示例：`docker/runtime-volume-seeds/main-workspace/.claude/skills/alert-triage/SKILL.md`

```markdown
---
name: alert-triage
description: 对告警进行分级研判、证据收集、时间线整理和升级建议。适用于用户提供告警 ID、规则名、主机、进程、IP 或时间范围时。
allowed-tools:
  - Read
  - Grep
  - Glob
  - mcp__sec-ops-data__*
context: fork
agent: soc-analyst
---

## 输入

用户可能提供：告警 ID、资产、账号、进程、IP、域名、规则名、时间范围。

## 步骤

1. 查询告警详情和关联实体。
2. 查询资产画像、账号画像、历史告警和近期行为。
3. 检索内部 SOP / 规则说明。
4. 输出：结论、证据、攻击阶段、影响范围、处置建议、待补证据。
```

## 权限与安全

`SKILL.md` 的 `allowed-tools` 字段兼容 Claude Code CLI。通过 Agent SDK 调用时，最终工具边界仍以 Claude Code 官方配置为准：`.claude/settings.json`、subagent/skill frontmatter 和 Claude Code 自身发现规则共同生效，后端不通过 Options 注入 allow/deny 列表、permission mode 或 hooks。

默认 `docker/runtime-volume-seeds/main-workspace/.claude/settings.json` 中设置：

```json
{
  "permissions": {
    "allow": ["Read(./**)", "Glob", "Grep", "Skill", "Write(/data/outputs/**)", "mcp__sec-ops-data__*"],
    "ask": ["Bash(*)", "Edit(./**)", "Write(./**)"],
    "deny": ["Read(./.env)", "Read(./.env.*)", "Read(/claude-roots/main/.claude.json)"]
  }
}
```

这意味着：

- 默认允许读文件、搜索文件、调用 `sec-ops-data` MCP，以及把报告类产物写入运行态输出目录。
- 默认不允许 Bash、WebFetch、WebSearch。
- 任何未预先允许的工具不会弹交互式确认，而是拒绝。

`app/runtime/policy.py` 还提供了 SDK 级 PreToolUse hook，用于阻断高危 Bash 命令，并把 main profile 的 `Write` 限制在 `/data/outputs`。日报类输出应写入 `/data/outputs/reports/daily-secops-report-YYYY-MM-DD.md`。

不要把宿主机敏感目录挂入容器，例如：

```yaml
# 不要这样做
- /:/host
- /var/run/docker.sock:/var/run/docker.sock
- ~/.ssh:/root/.ssh
```

## 会话机制

API 维护一个轻量 session store，权威数据保存在 SQLite：

```text
${HOME}/volume-agent-gov/data/runtime.sqlite3
```

它保存：

- API 层 `session_id`
- Claude SDK 返回的 `sdk_session_id`
- 创建/更新时间
- turns
- title 和 metadata

`${HOME}/volume-agent-gov/data/sessions/` 是历史兼容路径，不再是权威存储。下一次请求传入同一个 `session_id` 时，运行时会尝试使用 SDK `resume` 继续 Claude Code 会话。

## 生产化建议

这个项目当前面向开发、验证和内部集成环境，不是完整企业平台。生产化前建议补充：

1. 更严格的鉴权和租户隔离。
2. 每个租户独立 workspace/data volume。
3. 独立 MCP Gateway，不让 Agent 直连高危 MCP。
4. 线上 OpenTelemetry 后端的采样、告警和保留策略。
5. 高危工具 Human-in-the-loop 审批。
6. 上传文件病毒扫描和敏感信息检测。
7. 容器 seccomp/AppArmor/gVisor/Firecracker 隔离。
8. Agent package 签名与审核机制。

## 本地开发

首次开发先运行 `make setup` 创建 `.venv` 和默认 `docker/.env`。本项目 Makefile 中的 Python 脚本入口统一使用 `.venv/bin/python`，不要直接依赖宿主机 `python3`。

Docker Compose 部署只读取 `docker/.env`。Compose 会为 API/worker 注入内部标记 `RUNTIME_CONTAINER=1`，Runtime 因此自动选择容器部署配置，默认宿主机运行态根目录是 `${HOME}/volume-agent-gov`。

本机 host/PyCharm 调试无需额外设置 `RUNTIME_VOLUME_MODE`。宿主机 Python 进程会自动读取 `docker/.env.local-debug`；该文件不会被 Docker Compose 加载，默认把全部 workspace、data 和 claude-root 指向 `/tmp/local-debug-volume-agent-gov`：

```bash
make local-debug-env
make local-debug-bootstrap
```

`docker/.env.local-debug` 不是极简覆盖文件，它应与 `docker/.env` 保持 Runtime/API/worker 应用配置同构；主要差异只应是路径、端口和宿主机访问地址。模型提供商、Agent job、DSPy、Claude SDK、Runtime Langfuse tracing 等配置都应在两个文件中有同名 key。Compose、前端容器端口、Langfuse Postgres/ClickHouse/Redis/MinIO 镜像和初始化账号等部署编排项只放在 `docker/.env`。

功能测试和验收测试不使用 `docker/.env.local-debug`，除非测试目标明确是本机调试 env 选择本身。`make test` 是离线功能硬门；需要真实模型和真实运行态的 live 验收必须先部署 Docker Compose 容器环境，并通过 `make container-live-test` 在容器内使用 `docker/.env` 和容器挂载路径执行。

需要调整本机调试路径时编辑 `docker/.env.local-debug`；需要调整容器部署路径时编辑 `docker/.env` 或部署系统注入的 `HOST_RUNTIME_VOLUME_ROOT`。需要显式沿用旧目录时，可以在对应模式中把 `HOST_RUNTIME_VOLUME_ROOT` 设置为 `<repo root>/docker/volume`。

本机后台 Agent job 不复用交互式 Claude `/login` 状态。运行“重新生成回归用例”等 worker 任务前，必须在私有 `docker/.env.local-debug` 配置模型后端：Anthropic-compatible 路径需要 `MODEL_PROVIDER_API_KEY`，本地/内网 vLLM 路径需要 `MODEL_PROVIDER_BACKEND=vllm` 和不带 `/v1` 的 `MODEL_PROVIDER_API_URL`。缺少模型凭据或模型 URL 时，job 会在启动 Claude Code 前失败；缺少 Anthropic-compatible key 时错误码为 `AGENT_AUTH_REQUIRED`，并提示当前 profile 和 env 文件。

API 和 worker 统一使用 `LOG_LEVEL` 控制应用日志级别：容器部署的 `docker/.env` 默认 `LOG_LEVEL=info`，本机调试的 `docker/.env.local-debug` 默认 `LOG_LEVEL=debug`。API 和 worker 启动日志会打印 `log_level`、`runtime_volume_mode`、`settings_env_file`、`model_provider_backend`、`model_provider_vllm_sidecar_threshold`、`model_provider_vllm_allow_direct`、`provider_api_key_configured`、`provider_api_url_configured`、`governance_agent_timeout_seconds`、`dspy_output_formatter_timeout_seconds`、`claude_web_hitl_enabled`、`hitl_timeout_seconds`、`workspace_dir`、`data_dir`、`claude_root` 和 `langfuse_base_url`。如果 PyCharm 调试时看到 `runtime_volume_mode=container`，说明进程被误标记为容器或环境变量被外部覆盖。

本机 PyCharm 调试如果需要访问本机 Docker 暴露的 HTTP MCP 服务，在 `docker/.env.local-debug` 中设置 `MCP_SERVER_URL=http://localhost:48001/mcp`，然后执行 `make local-debug-repair-managed-config` 修复 `${HOST_RUNTIME_VOLUME_ROOT}/main-workspace/.mcp.json`。main profile 和 feedback profiles 都只使用各自 workspace 的官方 `.mcp.json`。宿主机存在代理变量时，同时在 `CLAUDE_ENV_JSON` 中设置 `NO_PROXY` 和 `no_proxy`，避免本机地址请求被代理转发。

如果刚从 Docker API/worker 切换到 PyCharm 本机调试，先修复后端共享 volume 的宿主机权限。该脚本只处理 API/worker 共享的 workspace、data 和 `claude-roots/*`，不处理 Langfuse 数据卷：

```bash
scripts/fix_host_backend_volume_permissions.sh
```

PyCharm 后端调试建议使用 Python run configuration：

```text
Module name: uvicorn
Parameters: app.main:app --reload --reload-dir app --host 0.0.0.0 --port 8080
Working directory: <repo root>
Python interpreter: <repo root>/.venv/bin/python
Environment variables: 留空即可
```

异步反馈闭环、优化任务和评估生成依赖 `agent_jobs` worker。需要调试这些流程时，另建一个 PyCharm configuration：

```text
Module name: app.worker.agent_jobs
Parameters: 留空
Working directory: <repo root>
Python interpreter: <repo root>/.venv/bin/python
Environment variables: 留空即可
```

前端本机启动使用 Vite 自己的本地环境文件：

```bash
cd frontend
cp .env.example .env.local
```

把 `frontend/.env.local` 中的后端地址改成：

```env
VITE_RUNTIME_API_BASE=http://localhost:8080
VITE_DEV_PROXY_TARGET=http://localhost:8080
VITE_LANGFUSE_URL=http://localhost:53000
```

如果后端 `API_KEY` 非空，可在 `frontend/.env.local` 中手工加入 `VITE_RUNTIME_API_KEY=<your-runtime-api-key>`，或在 UI 设置弹窗中保存。

然后直接启动：

```bash
pnpm dev
```

前端会把浏览器 localStorage 中旧默认值 `http://localhost:58080` 自动迁移到当前默认 Runtime API 地址；远程访问前端时会使用当前浏览器主机名加 `:58080`。如果你手工配置过其他非 loopback 地址，仍可在前端设置弹窗中修改。
