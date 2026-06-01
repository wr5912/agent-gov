# Claude Agent Runtime API

一个 **Docker 化 Claude Agent Runtime API** 项目。

目标：

- 不重写 Claude Agent loop。
- 通过 Docker 容器封装 Claude Agent SDK / Claude Code Runtime。
- 通过六套 Runtime Profile 隔离主智能体、归因分析智能体、优化方案生成智能体、执行优化智能体、用例治理智能体和回归影响分析智能体：`/main-workspace`、`/attribution-analyzer-workspace`、`/proposal-generator-workspace`、`/execution-optimizer-workspace`、`/eval-case-governor-workspace`、`/regression-impact-analyzer-workspace` 与独立 `claude-roots/*`。
- 容器对外提供 HTTP API，供 Web UI、业务系统、Agent 平台控制面调用。

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
├── docker/                      # Dockerfile、Compose、运行卷模板、vendored A2UI SDK
│   └── volume/
│       ├── main-workspace/      # 主智能体 workspace 和受管配置骨架
│       ├── attribution-analyzer-workspace/ # 归因分析智能体 profile
│       ├── proposal-generator-workspace/  # 优化方案生成智能体 profile
│       ├── execution-optimizer-workspace/ # 执行优化智能体 profile
│       ├── eval-case-governor-workspace/ # 用例治理智能体 profile
│       ├── regression-impact-analyzer-workspace/ # 回归影响分析智能体 profile
│       ├── claude-roots/        # Claude Code roots，运行态目录默认不提交
│       ├── data/                # runtime.sqlite3、证据/任务临时文件、版本包等运行态数据
│       └── langfuse/            # 可选本地 Langfuse profile 数据，默认不提交
├── docs/                        # 架构、治理和示例配置文档
├── tests/                       # 后端测试
├── scripts/                     # 维护脚本
├── Makefile
└── requirements.txt
```

## 快速启动

```bash
make setup
```

编辑 `docker/.env`：

```bash
MODEL_PROVIDER_API_KEY=sk-ant-xxxx
API_KEY=<your-runtime-api-key>
HOST_PORT=58080
API_PORT=8080
AGENT_MODEL=claude-sonnet-4-5
```

`docker/.env.example` 已包含端口、模型提供商、Claude Agent SDK 运行参数、路径、权限、skills、MCP、hooks、session 等配置项的注释。默认端口映射为 `58080:8080`，符合项目端口规则 `50000 + 容器端口`。

Docker 构建阶段已在 Dockerfile 中固定使用国内镜像源：Debian apt 使用阿里源，uv/pip 使用阿里 PyPI 源，npm 使用 npmmirror；这些源不再通过 `docker/.env` 覆盖，避免不同机器构建时漂移。Compose 运行环境也会固定同名 pip/uv/npm 变量，避免已有本地 `docker/.env` 旧变量影响容器内后续安装命令。基础镜像固定使用 `python:3.11-slim` 和 `node:22-alpine`；基础镜像拉取没有统一、稳定的公共国内 registry 可直接写死，建议通过 Docker daemon registry mirror 或团队内网基础镜像仓库处理，如需切换应直接修改 Dockerfile 的 `FROM` 行。

镜像构建阶段会安装 `a2ui-adk` 相关 Python 依赖，并从 `docker/vendor/A2UI/agent_sdks/python` 安装已 vendor 的 Google A2UI v0.9 Python SDK。PyPI 依赖在 build 阶段完成下载，容器运行时不会再为 `a2ui-adk` 访问互联网。

`LITELLM_LOCAL_MODEL_COST_MAP=True` 会强制 LiteLLM 使用包内置模型价格表，避免启动或 import 时访问 GitHub 获取远程 cost map。

为减少 bind mount 权限问题，Compose 中的 API 容器默认以 root 运行，启动时会对 `docker/volume/data/`、六个 workspace 和 `docker/volume/claude-roots/*` 对应的容器挂载目录执行 `chmod -R a+rwX`，方便直接写入。生产环境如果需要收紧权限，可以再切换到非 root 用户并配套处理宿主机目录 owner/ACL。

启动：

```bash
make build
make up
make logs
```

健康检查：

```bash
make smoke
```

## 前端 UI

`frontend/` 是一个 React/Vite 前端，用于对接本项目已有的 Claude Agent Runtime API。它包含 Playground 聊天、会话管理、subagents/skills 发现、Claude 配置映射摘要、反馈信号、反馈处置单、证据包、归因分析、优化方案、外部治理、优化任务、回归评估和 Agent 版本管理。前端默认使用 Claude 暖色系界面。

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

Runtime API 设置默认读取前端环境变量：`VITE_RUNTIME_API_BASE` 默认是 `http://localhost:58080`，`VITE_RUNTIME_API_KEY` 可填后端 `docker/.env` 中的 `API_KEY`。开发模式下，Vite 会把 `/api`、`/health`、`/v1` 代理到 `VITE_DEV_PROXY_TARGET`，默认也是 `http://localhost:58080`。

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

默认访问地址：

```text
http://localhost:55173
```

相关配置项为 `FRONTEND_HOST_PORT`、`FRONTEND_RUNTIME_API_BASE`、`FRONTEND_RUNTIME_API_KEY`、`FRONTEND_LANGFUSE_URL`。`FRONTEND_RUNTIME_API_KEY` 留空时，Compose 会复用 `API_KEY`。

这个 UI 不接管 Claude Code CLI 进程，不编辑宿主机敏感文件，不提供 Terminal。聊天、反馈闭环、评估和版本管理都通过后端 Runtime API 完成。

每条 Claude Agent 回复的“回复细节”会保留完整流式事件，并汇总本次请求的 Skill / Tool 使用情况。详情窗口支持关键字查找事件内容，底层 JSON 会完整展开显示。

## 反馈优化闭环

Runtime 的反馈优化闭环以多 Agent 架构为准。每次 `/api/chat` 或 `/api/chat/stream` 都会生成 `run_id`，并在 SQLite 中写入本次回答的轻量运行记录。Playground 回复上的反馈入口只采集 feedback signal；归因分析、批次优化方案和执行在 Feedback 工作台中按 `feedback case -> evidence package -> attribution-analyzer -> optimization batch -> proposal-generator -> execution-optimizer -> regression run` 链路处理。

完整 API 以运行时 OpenAPI 为准：本地运行后访问 `http://localhost:58080/openapi.json`，或使用 `scripts/export_openapi.py` 生成 [docs/开放接口规范.json](docs/开放接口规范.json)。下面仅保留按职责分组的高层索引，避免 README 随接口细节频繁漂移：

前端 OpenAPI 类型由 [docs/开放接口规范.json](docs/开放接口规范.json) 生成，命令为：

```bash
pnpm --dir frontend generate:api-types
```

- 反馈采集与处置单：`GET /api/agent-runs`、`POST/GET /api/feedback-signals`、`GET /api/feedback-signals/{signal_id}`、`POST/GET /api/soc-events`、`GET /api/soc-events/{event_id}`、`GET /api/pending-correlations`、`POST /api/pending-correlations/{pending_id}/resolve`、`POST/GET /api/feedback-cases`、`GET /api/feedback-cases/{feedback_case_id}`。
- 证据包与分析任务：`POST /api/feedback-cases/{feedback_case_id}/evidence-packages`、`GET /api/evidence-packages/{evidence_package_id}`、`GET /api/evidence-packages/{evidence_package_id}/files/{file_name}`、`POST /api/feedback-cases/{feedback_case_id}/attribution-jobs`、`POST /api/feedback-cases/{feedback_case_id}/proposal-jobs`、`POST /api/feedback-cases/{feedback_case_id}/proposal-jobs/regenerate`、`GET /api/feedback-analysis/jobs/{job_id}`、`GET /api/feedback-analysis/jobs/{job_id}/attribution`、`GET /api/feedback-analysis/jobs/{job_id}/proposal`、`POST /api/feedback-analysis/jobs/{job_id}/proposal/revalidate`。
- 批次优化、任务和外部治理：`POST/GET /api/feedback-optimization-batches`、`GET/POST /api/feedback-optimization-batches/{batch_id}/eval-cases`、`PATCH/DELETE /api/feedback-optimization-batches/{batch_id}/eval-cases/{eval_case_id}`、`POST /api/feedback-optimization-batches/{batch_id}/attribution-jobs`、`POST /api/feedback-optimization-batches/{batch_id}/optimization-plan`、`POST /api/feedback-optimization-batches/{batch_id}/optimization-plan/tasks/{plan_task_id}/execute`、`POST /api/feedback-optimization-batches/{batch_id}/regression-runs`、`GET /api/optimization-tasks`、`POST /api/optimization-tasks`、`GET /api/optimization-tasks/{task_id}`、`POST /api/optimization-tasks/{task_id}/mark-applied`、`POST/GET /api/optimization-tasks/{task_id}/execution-jobs`、`POST /api/optimization-tasks/{task_id}/execution-jobs/{execution_job_id}/apply`、`POST/GET /api/optimization-tasks/{task_id}/regression-runs`、`GET /api/external-governance-webhooks`、`GET /api/external-governance-items`、`POST /api/external-governance-items/{external_item_id}/notify`。
- 评估和版本：`POST /api/eval-datasets/feedback/sync`、`GET /api/eval-cases`、`PATCH /api/eval-cases/{eval_case_id}`、`POST/GET /api/eval-runs`、`GET /api/eval-runs/{eval_run_id}`、`GET /api/agent-versions/main/current`、`GET /api/agent-versions/main`、`POST /api/agent-versions/main/snapshots`、`POST /api/agent-versions/main/{version_id}/rollback`、`GET /api/agent-versions/main/diff`、`GET /api/agent-versions/main/file-diff`、`GET /api/agent-versions/main/{version_id}`。

运行态数据默认保存在 Docker 数据卷 `/data` 下，对应宿主机 `docker/volume/data/`：

- `/data/runtime.sqlite3` 是反馈信号、SOC 事件、处置单、证据包 manifest 和文件内容、分析任务、优化方案、优化任务、评估用例、评估运行和 API session 的权威存储。
- `/data/.runtime-tmp/jobs/` 是归因、建议和评估 Agent 的临时 job workspace。
- `/data/agent-versions/main/` 保存主智能体受管配置版本 manifest 和 bundle。
- `/data/external-governance-webhooks.yaml` 是外部治理 Webhook 配置文件；示例见 `docs/外部治理Webhook示例.yaml`。
- `/data/feedback-signals/`、`/data/soc-events/`、`/data/feedback-cases/` 等旧目录仅为兼容路径，不再是权威存储。

正式设计文档见 [反馈优化闭环多智能体架构.md](docs/反馈优化闭环多智能体架构.md)。旧版 `FEEDBACK_OPTIMIZATION_LOOP_MVP.md` 已废弃，旧接口语义不再作为实现依据。

## Langfuse 监控

本项目优先通过 Claude Code 内置 OpenTelemetry 导出能力接入 Langfuse。开启后，API 运行时会把 `docker/.env` 中的 Langfuse 配置转换为 Claude Code 子进程可识别的 `CLAUDE_CODE_*` 和 `OTEL_*` 环境变量。

接入 Langfuse Cloud 或既有自托管实例时，最小配置为：

```bash
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-xxxx
LANGFUSE_SECRET_KEY=sk-lf-xxxx
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_OTEL_SIGNALS=traces,metrics,logs
```

其他区域或自托管环境：

```bash
# US Cloud
LANGFUSE_BASE_URL=https://us.cloud.langfuse.com

# 自托管
LANGFUSE_BASE_URL=http://langfuse.example.com
# 或显式指定 OTLP endpoint
LANGFUSE_OTEL_ENDPOINT=http://langfuse.example.com/api/public/otel
```

### Runtime enrich 与 input/output

仅依赖 Claude Code OTEL 时，Langfuse 中可能可以看到 `claude_code.interaction`、`claude_code.llm_request`、tool span、metrics 和 logs，但标准 observation 的 `input` / `output` 不一定完整。原因是 Claude Code OTEL 主要导出 Claude Code 自身的链路结构和事件，trace 顶层 `input` / `output` 需要由 Runtime 在 API 调用结束后补齐。

为补齐 API 层可读的请求和响应，Runtime 会在启用 Langfuse 且配置了 public/secret key 时额外创建两类 observation：

- `runtime.chat`：API 层根 span，记录请求输入、最终回答、SDK 消息、usage、cost、stop_reason 和 errors，并写入 trace-level `input` / `output`。
- `runtime.claude_sdk_query`：Claude SDK 调用 generation，记录实际 prompt/model，并在调用结束后写入输出、token usage、成本和错误状态。

本项目把 Langfuse 定位为本地调测工具。`LANGFUSE_ENABLED=true` 时，Runtime 默认向 Claude Code 子进程开启 `OTEL_LOG_USER_PROMPTS`、`OTEL_LOG_TOOL_DETAILS`、`OTEL_LOG_TOOL_CONTENT` 和 `OTEL_LOG_RAW_API_BODIES`，并把 Runtime enrich 的请求/响应原样写入 Langfuse，便于在同一条 trace 中查看 prompt、工具参数、工具结果、raw API body 和最终输出。Runtime 输出中的 `agent_activity` 字段会额外汇总 requested skills、实际 Skill 调用、tool calls 和 tool results。

### 本地 Langfuse Docker profile

Langfuse 自托管 profile 默认不随 API 启动。它按 Langfuse 官方 v3 低规模 Docker Compose 形态运行：`langfuse-web`、`langfuse-worker`、Postgres、ClickHouse、Redis、MinIO。持久化数据统一写入 `docker/volume/langfuse/`。ClickHouse 默认固定为 `24.3`，满足 Langfuse v3 的最低版本要求。

启动本地 Langfuse：

```bash
make langfuse-up
make langfuse-smoke
```

默认访问地址：

- Langfuse UI: `http://localhost:53000`
- MinIO API: `http://localhost:59000`
- MinIO Console: `http://localhost:59001`

这些端口均可在 `docker/.env` 中调整：`LANGFUSE_HOST_PORT`、`LANGFUSE_MINIO_HOST_PORT`、`LANGFUSE_MINIO_CONSOLE_HOST_PORT`。因为容器内端口 `3000`、`9000`、`9001` 均小于 `10000`，默认宿主机端口遵循项目规则 `50000 + 容器端口`。

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

容器启动后可通过 `/health` 查看状态字段：`langfuse_enabled`、`langfuse_public_key_configured`、`langfuse_secret_key_configured`、`langfuse_otel_signals`。不要把真实 Langfuse key 写入 `docker/.env.example` 或提交到仓库。

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
    "message": "分析这个告警：rundll32.exe 加载 WININET.dll，父进程为 EdgeUpdate，命令行为 DispatchAPICall 1",
    "allowed_tools": ["Read", "Grep", "Glob"],
    "permission_mode": "dontAsk"
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

注意：这是兼容接入用的轻量 shim，不是完整 OpenAI API 实现；真正的 Agent 参数，如 `agent`、`skills`、`allowed_tools`，建议使用 `/api/chat`。

## 管理 API

```bash
curl -H "Authorization: Bearer $API_KEY" "$API_BASE/api/agents"
curl -H "Authorization: Bearer $API_KEY" "$API_BASE/api/skills"
curl -H "Authorization: Bearer $API_KEY" "$API_BASE/api/config"
curl -H "Authorization: Bearer $API_KEY" "$API_BASE/api/sessions"
```

## 配置挂载说明

`docker/docker-compose.yml` 默认挂载：

```yaml
volumes:
  - ./volume/main-workspace:/main-workspace
  - ./volume/attribution-analyzer-workspace:/attribution-analyzer-workspace
  - ./volume/proposal-generator-workspace:/proposal-generator-workspace
  - ./volume/execution-optimizer-workspace:/execution-optimizer-workspace
  - ./volume/eval-case-governor-workspace:/eval-case-governor-workspace
  - ./volume/regression-impact-analyzer-workspace:/regression-impact-analyzer-workspace
  - ./volume/data:/data
  - ./volume/claude-roots/main:/claude-roots/main
  - ./volume/claude-roots/attribution-analyzer:/claude-roots/attribution-analyzer
  - ./volume/claude-roots/proposal-generator:/claude-roots/proposal-generator
  - ./volume/claude-roots/execution-optimizer:/claude-roots/execution-optimizer
  - ./volume/claude-roots/eval-case-governor:/claude-roots/eval-case-governor
  - ./volume/claude-roots/regression-impact-analyzer:/claude-roots/regression-impact-analyzer
```

你可以只改宿主机目录，不需要改镜像：

- `docker/volume/main-workspace/CLAUDE.md`
- `docker/volume/main-workspace/CLAUDE.local.md.example`：本地私有指令模板，可复制为 `CLAUDE.local.md`
- `docker/volume/main-workspace/.claude/settings.json`
- `docker/volume/main-workspace/.claude/settings.local.json.example`：本地私有权限模板，可复制为 `.claude/settings.local.json`
- `docker/volume/main-workspace/.claude/agents/*.md`
- `docker/volume/main-workspace/.claude/skills/*/SKILL.md`
- `docker/volume/main-workspace/.claude/commands/*.md`
- `docker/volume/main-workspace/.claude/rules/*`
- `docker/volume/main-workspace/.claude/output-styles/*.md`
- `docker/volume/main-workspace/.mcp.json`
- `docker/volume/main-workspace/.worktreeinclude`
- `docker/volume/main-workspace/agent.yaml`
- `docker/volume/attribution-analyzer-workspace/CLAUDE.md`
- `docker/volume/attribution-analyzer-workspace/agent.yaml`
- `docker/volume/proposal-generator-workspace/CLAUDE.md`
- `docker/volume/proposal-generator-workspace/agent.yaml`
- `docker/volume/execution-optimizer-workspace/CLAUDE.md`
- `docker/volume/execution-optimizer-workspace/agent.yaml`
- `docker/volume/eval-case-governor-workspace/CLAUDE.md`
- `docker/volume/eval-case-governor-workspace/agent.yaml`
- `docker/volume/regression-impact-analyzer-workspace/CLAUDE.md`
- `docker/volume/regression-impact-analyzer-workspace/agent.yaml`

`docker/volume/data/` 中的运行态文件默认不提交到 git；其中 `runtime.sqlite3`、临时 job、Agent 版本包和 Langfuse 运行数据都属于本地运行态。

## subagent 文件格式

示例：`docker/volume/main-workspace/.claude/agents/soc-analyst.md`

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

默认情况下，项目依赖 Claude Code 原生发现机制加载 `.claude/agents/*.md`。如需 SDK-only 显式注入，可把 `docker/.env` 中的 `ENABLE_PROGRAMMATIC_AGENTS` 改为 `true`。

## skill 文件格式

示例：`docker/volume/main-workspace/.claude/skills/alert-triage/SKILL.md`

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

`SKILL.md` 的 `allowed-tools` 字段兼容 Claude Code CLI。通过 Agent SDK 调用时，最终工具边界以请求中的 `allowed_tools` 和 `docker/.env` 的 `DEFAULT_ALLOWED_TOOLS` 为准。

默认 `docker/.env.example` 中设置：

```bash
DEFAULT_ALLOWED_TOOLS=Read,Grep,Glob,Skill,mcp__sec-ops-data__*
DEFAULT_DISALLOWED_TOOLS=Bash,WebFetch,WebSearch
PERMISSION_MODE=dontAsk
ENABLE_POLICY_HOOKS=true
```

这意味着：

- 默认只允许读文件、搜索文件。
- 默认不允许 Bash、WebFetch、WebSearch。
- 任何未预先允许的工具不会弹交互式确认，而是拒绝。

`app/runtime/policy.py` 还提供了 SDK 级 PreToolUse hook，用于阻断高危 Bash 命令。

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
docker/volume/data/runtime.sqlite3
```

它保存：

- API 层 `session_id`
- Claude SDK 返回的 `sdk_session_id`
- 创建/更新时间
- turns
- title 和 metadata

`docker/volume/data/sessions/` 是历史兼容路径，不再是权威存储。下一次请求传入同一个 `session_id` 时，运行时会尝试使用 SDK `resume` 继续 Claude Code 会话。

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

```bash
make setup
source .venv/bin/activate
export WORKSPACE_DIR=$PWD/docker/volume/main-workspace
export MAIN_WORKSPACE_DIR=$PWD/docker/volume/main-workspace
export ATTRIBUTION_ANALYZER_WORKSPACE_DIR=$PWD/docker/volume/attribution-analyzer-workspace
export PROPOSAL_GENERATOR_WORKSPACE_DIR=$PWD/docker/volume/proposal-generator-workspace
export EXECUTION_OPTIMIZER_WORKSPACE_DIR=$PWD/docker/volume/execution-optimizer-workspace
export EVAL_CASE_GOVERNOR_WORKSPACE_DIR=$PWD/docker/volume/eval-case-governor-workspace
export REGRESSION_IMPACT_ANALYZER_WORKSPACE_DIR=$PWD/docker/volume/regression-impact-analyzer-workspace
export DATA_DIR=$PWD/docker/volume/data
export CLAUDE_ROOT=$PWD/docker/volume/claude-roots/main
export MAIN_CLAUDE_ROOT=$PWD/docker/volume/claude-roots/main
export ATTRIBUTION_ANALYZER_CLAUDE_ROOT=$PWD/docker/volume/claude-roots/attribution-analyzer
export PROPOSAL_GENERATOR_CLAUDE_ROOT=$PWD/docker/volume/claude-roots/proposal-generator
export EXECUTION_OPTIMIZER_CLAUDE_ROOT=$PWD/docker/volume/claude-roots/execution-optimizer
export EVAL_CASE_GOVERNOR_CLAUDE_ROOT=$PWD/docker/volume/claude-roots/eval-case-governor
export REGRESSION_IMPACT_ANALYZER_CLAUDE_ROOT=$PWD/docker/volume/claude-roots/regression-impact-analyzer
export CLAUDE_HOME=$CLAUDE_ROOT/.claude
.venv/bin/python -m uvicorn app.main:app --reload --host "${API_HOST:-127.0.0.1}" --port "${API_PORT:-8080}"
```
