# 智能体治理平台 AgentGov

一个 **Docker 化的智能体治理平台 AgentGov** 项目（Agent Runtime · Feedback Loop · Version Governance）。

当前阶段聚焦把 AgentGov 平台本身做好，强化智能体开发、运行、反馈优化、评估回归、版本治理和多业务 Agent 治理闭环。本期不建设产品内的通用协作模型，也不接入外部研发协作平台；待核心能力稳定且出现真实需求后，再重新做协作平台选型。

目标：

- 不重写 Claude Agent loop。
- 通过 Docker 容器封装 Claude Agent SDK / Claude Code Runtime。
- 通过 Runtime Profile 隔离所有注册业务 Agent（含 `main-agent`）与单一治理智能体（governor）：业务 Agent 住 `data/business-agents/<agent_id>/workspace`，并使用各自独立的 `claude-root`；治理智能体 governor 使用 `/governor-workspace` 与 `claude-roots/governor`，承担归因、优化方案、执行和回归用例生成。
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
├── docker/                      # Dockerfile、Compose、运行卷初始化源、vendored A2UI SDK
│   ├── runtime-bootstrap/       # 运行卷初始化源（Compose 只读挂载，镜像内保留兜底）
│   │   ├── governor-workspace/  # 治理智能体 governor 的初始 Workspace
│   │   └── business-agents/security-operations-expert/workspace/
│   │                            # 唯一内置、默认且受保护的业务 Agent Workspace
│   └── vendor/                  # vendored Google A2UI Python SDK
├── docs/                        # 架构、治理和示例配置文档
├── tests/                       # 后端测试
├── scripts/                     # 维护脚本
├── Makefile
├── pyproject.toml
└── requirements.txt
```

运行态目录不在仓库内：容器部署默认根为宿主机 `${HOME}/volume-agent-gov`，本机调试默认 `/tmp/local-debug-volume-agent-gov`。API 启动协调器或 `make runtime-bootstrap` / `make local-debug-bootstrap` 从 `docker/runtime-bootstrap/` 初始化缺失的 governor Workspace，以及缺失的内置 `security-operations-expert` 整体 Workspace；已存在目录不会被逐文件回灌。普通业务 Agent 只通过业务 Agent Workspace 包导入创建。运行根下包含 `governor-workspace/`、`claude-roots/governor`、`data/business-agents/<id>/{workspace,claude-root,version}`、`runtime.sqlite3`、候选 worktree、发布归档和可选 `langfuse/`，均默认不提交。`docker/volume/` 仅是旧布局迁移来源，不是当前运行路径。

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

离线部署表示不依赖公网远程服务，不表示无模型运行；反馈闭环、执行优化和 DSPy 输出规范化应指向本地或内网模型网关。模型接入通过 `MODEL_PROVIDER_BACKEND` 显式选择 adapter，不通过 URL 字符串推断 provider；`MODEL_PROVIDER_API_URL` 是唯一真实模型服务 URL。vLLM 场景中 `ANTHROPIC_BASE_URL` 由 Runtime 派生为内部 LiteLLM sidecar 地址，不要求用户维护第二个 upstream URL。治理 Agent 原始文本统一经 DSPy Signature 与 Pydantic OutputModel 规范化和校验。改进事项生成由 API 进程直接调用 governor/formatter；失败时按各业务动作契约返回结构化错误或写入 `generated_by=heuristic` 的确定性回退，调用方应结合 `generated_by` 与 `generation_trace_id` 判断来源。历史 `agent_jobs` 只保留只读查询，不再有可领取队列或独立 worker。

Docker 构建阶段已在 Dockerfile 中固定使用国内镜像源：Debian apt 使用阿里源，uv/pip 使用阿里 PyPI 源，Node 包源使用 npmmirror；这些源不再通过 `docker/.env` 覆盖，避免不同机器构建时漂移。Compose 运行环境也会固定同名 pip/uv/pnpm 变量，避免已有本地 `docker/.env` 旧变量影响容器内后续安装命令。基础镜像固定使用 `python:3.11-slim` 和 `node:22-alpine`；基础镜像拉取没有统一、稳定的公共国内 registry 可直接写死，建议通过 Docker daemon registry mirror 或团队内网基础镜像仓库处理，如需切换应直接修改 Dockerfile 的 `FROM` 行。

镜像构建阶段会安装 `a2ui-adk` 相关 Python 依赖，并从 `docker/vendor/A2UI/agent_sdks/python` 安装已 vendor 的 Google A2UI v0.9 Python SDK。PyPI 依赖在 build 阶段完成下载，容器运行时不会再为 `a2ui-adk` 访问互联网。

`LITELLM_LOCAL_MODEL_COST_MAP=True` 会强制 LiteLLM 使用包内置模型价格表，避免启动或 import 时访问 GitHub 获取远程 cost map。Compose 会启动独立 `agent-gov-litellm-sidecar` 服务；当 `MODEL_PROVIDER_BACKEND=vllm` 时，Runtime 会在后台通过 `{MODEL_PROVIDER_API_URL}/version` 探测运行中 vLLM 版本，低版本、未知版本或非传输类探测失败默认走 sidecar；仅当显式 `MODEL_PROVIDER_VLLM_ALLOW_DIRECT=true` 且探测版本 >= `MODEL_PROVIDER_VLLM_SIDECAR_THRESHOLD` 时才直连 vLLM 原生 Anthropic 端点（仍受能力门验收，不兼容即 fail-closed）。`MODEL_PROVIDER_API_URL` 必须是不带末尾 `/v1` 的 vLLM 服务基地址，错误配置会在网络探测前以 `VLLM_BASE_URL_INVALID` 拒绝。API 启动后在后台预热完整 provider 能力门，首个模型请求与预热共享 single-flight 和成功缓存；外部 vLLM 超时或不可达不会拖死 API/UI 健康检查，但会阻断具体模型请求，并返回包含 `error_code`、`probe`、`reason`、脱敏 `endpoint`、`retryable` 和 `action` 的稳定错误。

`MODEL_PROVIDER_API_KEY` 只用于 sidecar 访问上游模型服务；LiteLLM 的 `LITELLM_MASTER_KEY` 则是 Proxy Admin / Virtual Key 管理面的根凭据，二者不是同一个信任边界，也不得复用。AgentGov 的 sidecar 不发布宿主机端口，只在 Compose 内网承担推理协议转换，并关闭 Admin UI、Swagger 与 Redoc，因此用户无需配置 `LITELLM_MASTER_KEY`。若未来把它改造成可被外部客户端访问的共享模型网关，必须另行设计独立的代理管理员密钥、应用 Virtual Key、网络暴露和轮换方案，不能沿用当前内部 sidecar 边界。参考 LiteLLM 官方的 [Virtual Keys](https://docs.litellm.ai/docs/proxy/virtual_keys)、[Admin UI](https://docs.litellm.ai/docs/proxy/ui) 与 [Production Best Practices](https://docs.litellm.ai/docs/proxy/prod)。

为减少 bind mount 权限问题，Compose 中的 API 容器默认以 root 运行，启动时会对 `${HOME}/volume-agent-gov/data/`、governor Workspace 和 `${HOME}/volume-agent-gov/claude-roots/*` 对应的容器挂载目录执行 `chmod -R a+rwX`，方便直接写入。生产环境如果需要收紧权限，可以再切换到非 root 用户并配套处理宿主机目录 owner/ACL。

启动：

```bash
make build
make up
make logs
```

`make up` 会启动 API、前端和配置所需的 LiteLLM sidecar，不再启动独立 Agent job worker。改进事项闭环中需要模型执行的归因、方案、执行计划和回归用例生成由 API 进程调用 governor，结果写入 `/api/improvements/{improvement_id}/...` 对应内容子资源，并保留 Langfuse Trace 引用供前端查看。

只修改 `docker/runtime-bootstrap/` 下的初始化内容时，在已使用当前 Compose 配置 recreate 过容器后不需要重建镜像；执行 `make up` 即可让 API 启动协调器处理整体缺失的 Workspace、Git 版本源初始化和运行 receipt。初始化源不会覆盖已存在的运行态 Workspace；升级现有 Agent 必须走 Workspace 包导入或直接治理其 live Workspace。修改 Dockerfile、Python 代码、初始化逻辑或依赖时仍需 `make build` 并 recreate。

治理类 Agent job、改进事项生成动作和前端治理请求默认使用 `GOVERNANCE_AGENT_TIMEOUT_SECONDS=300`。`DSPY_OUTPUT_FORMATTER_TIMEOUT_SECONDS` 只是高级覆盖项，未配置时跟随治理超时；业务 Playground 的流式 idle timeout、模型探测超时和 Docker healthcheck 不共用该值。Web HITL 人工确认等待使用独立的 `HITL_TIMEOUT_SECONDS=300`，只影响流式业务 Agent 运行。Web HITL 的 SDK callback 等待态保存在 API 进程内存中，开启 `ENABLE_CLAUDE_WEB_HITL=true` 时必须保持单 API 进程；`WEB_CONCURRENCY`、`API_WORKERS` 或 `UVICORN_WORKERS` 大于 1 会在启动时失败。非流式运行始终对权限询问 fail-closed，不再使用 `bypassPermissions`。

`security-operations-expert` 是当前唯一内置、默认且受保护的业务 Agent，这三个属性由平台分别派生。具体 Agent 的角色、工具、权限和业务流程只由其 Workspace 定义，不构成平台默认行为。Workspace 可以按字节导出，但任何新建或覆盖导入都要求包根目录 `agent.yaml.agent.id` 有效，并与 URL 中的目标 `agent_id` 逐字一致；平台不会代为改写身份。初始化源不回灌现有实例，现有实例升级走“导出候选 → 人工把候选包 ID 设置为测试 ID → 新 ID 导入测试 → 导出验证包 → 人工把包 ID 设置回目标 ID → 携带目标预期当前提交版本覆盖 → 回归/发布”流程。

健康检查：

```bash
make smoke
```

`GET /health/live` 只表示 API 进程存活，不访问 Git、CLI、数据库或外部模型服务，也是 Compose 对 API 的健康检查；`GET /health/ready` 只读取后台 provider 探测缓存，模型不可用时返回 `503` 和明确诊断；`GET /health` 返回控制面状态及同一份 provider 摘要。`make up` 只要求控制面 liveness，模型 provider 降级不会再被 Compose 折叠成 `dependency claude-agent-api failed to start`。需要诊断时运行 `make compose-diagnose`，输出会明确区分“API liveness 失败”和“API 已存活但外部模型 provider 探测失败”，并把 Compose dependency 文案标记为次级症状，而不是镜像启动失败；需要把 provider readiness 作为验收门时运行 `make smoke`。

外部 vLLM 探测超时的确定性容器回归使用真实 Compose API/UI/LiteLLM 容器和多视口 Playwright：

```bash
make container-health-e2e
```

本地默认使用 `docker/.env`。隔离验收可从 `docker/.env.example` 生成单一临时文件，并用 `COMPOSE_ENV_FILE=/path/to/env make container-health-e2e` 显式选择；该文件不是 layered override。健康 E2E 始终使用独立 Compose project、动态端口和临时 runtime 根，不写 `${HOME}/volume-agent-gov`。

部署到 Docker Compose 主机：

```bash
scripts/deploy_agent_gov_to_host
# 或使用位置参数指定主机；默认用户 root、默认目录 ~/work/agent-gov
scripts/deploy_agent_gov_to_host 172.16.112.232
```

该脚本获取并归档最新 `origin/master`，在本机构建 `VERSION` 对应的
`agent-gov-api`、`agent-gov-ui` 和 `agent-gov-litellm-sidecar` 镜像，然后把跟踪代码、
项目镜像包和 Langfuse 依赖镜像包同步到目标机。目标机私有 `docker/.env` 会被保留；
尚未创建时才从 `docker/.env.example` 初始化。部署使用目标机
`${HOME}/volume-agent-gov`，停止并删除已有 AgentGov 容器，加载归档镜像后启动包含
Langfuse profile 的全量 Compose 服务，并验证 API、UI 和 Langfuse 地址。

部署前由操作者在本地执行 `make test` 和需要的真实容器验收。脚本只接受零个或一个主机
位置参数；如需调整用户、远端目录或本地构建 env，分别使用 `DEPLOY_USER`、`REMOTE_DIR`
和 `LOCAL_COMPOSE_ENV_FILE`。

## 前端 UI

`frontend/` 是一个 React/Vite 前端，用于对接本项目已有的 AgentGov API。它包含 Playground 聊天、会话管理、subagents/skills 发现、Claude 配置映射摘要、反馈信号、改进事项、证据包、归因分析、优化方案、执行记录、回归测试设计、平台测试运行和 Agent 版本管理。前端默认使用 Claude 暖色系界面。

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

反馈优化工作台的确定性设计基准使用 mock 后端；真实功能与效果验收必须显式指向已经启动的容器 UI/API，不会回退到 mock。真实检查会创建测试改进事项、执行四阶段业务动作、确认回归测试设计、在同一待发布变更中新增 Workspace pytest 文件、等待当前待发布提交的平台测试运行终态，并验证未知提交的失败详情与精确提交普通发布；三种视口和失败详情截图写入 `/tmp`：

```bash
make ui-design-parity

RUNTIME_UI_BASE=http://localhost:55173 \
RUNTIME_API_BASE=http://localhost:58080 \
RUNTIME_API_KEY='<optional-api-key>' \
make ui-feedback-smoke
```

浏览器/API 单动作默认最多等待 5 分钟；待发布版本的完整 Workspace pytest 套件单独等待
15 分钟，因为持续沉淀的回归测试文件会使套件运行时间随版本增长。较大的业务 Agent 测试
套件可通过 `REAL_TEST_RUN_TIMEOUT_MS` 调整终态等待时间，不应通过放宽
`REAL_ACTION_TIMEOUT_MS` 掩盖单个治理动作超时。

默认访问地址：

```text
http://localhost:55173
```

相关配置项为 `FRONTEND_HOST_PORT`、`FRONTEND_RUNTIME_API_BASE`、`FRONTEND_RUNTIME_API_KEY`、`FRONTEND_LANGFUSE_URL`。`FRONTEND_RUNTIME_API_KEY` 留空时，Compose 会复用 `API_KEY`。

这个 UI 不接管 Claude Code CLI 进程，不编辑宿主机敏感文件，不提供 Terminal。聊天、反馈闭环、平台测试和版本管理都通过后端 Runtime API 完成。

每条 Claude Agent 回复的“回复细节”会保留 canonical SDK 事件，并汇总本次请求的 Skill / Tool 使用情况。流式请求默认通过 `INCLUDE_PARTIAL_MESSAGES=true` 接收 SDK `StreamEvent` 文本增量；这些增量只用于 SSE 传输，不写入会话消息、SQLite run 或 Langfuse SDK message 事实。最终 `AssistantMessage` 是权威快照：前后缀一致时只补发未到达的后缀，不一致时以 `STREAM_TEXT_DIVERGED` fail-closed，避免重复字或伪成功。前端按 animation frame/最长 32ms 合并增量并在 `response.completed` 用 canonical 文本校准 DOM。详情窗口支持关键字查找事件内容，底层 JSON 会完整展开显示。Claude Code 若在本轮末尾生成 Prompt Suggestion，Playground 会在输入框上方显示“下一步建议”；点击只把文本填入输入框，不会自动发送。

## 反馈优化闭环

Runtime 的反馈优化闭环以多 Agent 架构为准。每次 `/api/chat` 或 `/api/chat/stream` 都会生成 `run_id`，并在 SQLite 中写入本次回答的轻量运行记录。Playground 回复上的反馈入口只采集 feedback signal；用户在“改进事项”中把反馈归并为事项后，按反馈整理、归因分析、优化执行、测试发布四个工作面板推进。治理 Agent 生成的归因、优化方案、执行记录和回归测试设计都写入事项级内容子资源，并保存 `generation_trace_id` / `generation_trace_url`。

完整 API 以运行时 OpenAPI 为准：本地运行后访问 `http://localhost:58080/openapi.json`，或使用 `scripts/export_openapi.py` 导出临时 OpenAPI JSON。下面仅保留按职责分组的高层索引，避免 README 随接口细节频繁漂移：

前端 OpenAPI 类型由运行时 schema 临时导出后生成，命令为：

```bash
pnpm --dir frontend generate:api-types
```

- 业务 Agent 与 Workspace：`GET /api/agent-registry`、`POST /api/agent-registry/{agent_id}/workspace/export`、`POST /api/agent-registry/{agent_id}/workspace/import`、`POST /api/agent-registry/{agent_id}/workspace/restore`。Workspace 包导入是创建普通业务 Agent 的唯一入口；新 ID 必须提供 `name`，已有 ID 覆盖必须携带预期当前提交版本。两种导入都要求包根目录 `agent.yaml.agent.id` 有效，并与 URL 中的目标 ID 完全一致；缺失、无效或来源 ID 不一致会在任何 Workspace、注册表、Git 或会话变更前被明确拒绝。live Workspace 包可包含真实 endpoint 和私有运行配置，导出包应按敏感运行资产保管；导入、恢复和导出快照都绑定 per-Agent Git commit，并在下一 turn 生效。live Workspace 纳入仓库内置初始化源前必须在仓库外形成候选，并通过 `make runtime-bootstrap-scan` 准入检查。
- 反馈采集与处置单：`GET /api/agent-runs`、`POST/GET /api/feedback-signals`、`GET /api/feedback-signals/{signal_id}`、`POST/GET /api/soc-events`、`GET /api/soc-events/{event_id}`、`GET /api/pending-correlations`、`POST /api/pending-correlations/{pending_id}/resolve`、`POST/GET /api/feedback-cases`、`GET /api/feedback-cases/{feedback_case_id}`。`AgentRunResponse` 会返回 `langfuse_trace_id` / `langfuse_trace_url`，用于运行证据面板定位具体 Langfuse Trace。
- Agent job 历史：`GET /api/agent-jobs`、`GET /api/agent-jobs/{job_id}` 仅查询升级前保留的历史记录；没有创建、领取或重试队列入口。
- 证据包与分析任务：`POST /api/feedback-cases/{feedback_case_id}/evidence-packages`、`GET /api/evidence-packages/{evidence_package_id}`、`GET /api/evidence-packages/{evidence_package_id}/files/{file_name}`、`POST /api/feedback-cases/{feedback_case_id}/attribution-jobs`、`POST /api/feedback-cases/{feedback_case_id}/attribution-jobs/regenerate`。
- 改进事项四阶段内容：`POST/GET /api/improvements`、事项详情/lifecycle/archive，以及 `normalized-feedback` 的读取和确认；关键生成动作是 `POST /api/improvements/{improvement_id}/attribution/generate`、`POST /api/improvements/{improvement_id}/optimization-plan/generate`、`POST /api/improvements/{improvement_id}/execution/apply`、`POST /api/improvements/{improvement_id}/regression-test-design/generate`，具体运行证据通过 `GET /api/langfuse/traces/{trace_id}` 查询。内容生成成功后由后端推进对应阶段；`/lifecycle` 只允许返回较早阶段返工。执行记录只能由隔离 worktree 的 `execution/apply` 业务动作生成。生成回归测试只形成完整 pytest 代码候选；确认待发布变更后，后端只新增 `tests/test_feedback_*.py`，并把配置与测试收口为相对修复前版本的同一待发布 commit；平台测试必须再由独立显式动作创建。
- Workspace 测试、资产投影与平台运行：`GET /api/agent-test-assets`、`GET /api/agent-registry/{agent_id}/test-suite`、`/test-suite/file`、手工运行 `POST /api/agent-test-runs`、待发布变更运行 `POST /api/agent-change-sets/{change_set_id}/test-runs`、分页历史 `GET /api/agent-test-runs/history`、单次详情/取消，以及每 Agent 的 `GET/PUT .../test-schedule` 和 `GET .../test-schedule/events`；`agentgov_testkit` 使用 `/api/agent-test-sessions` 和 `/api/agent-test-sessions/{test_session_id}/messages`。测试内容只来自精确 Git 提交中的 `workspace/tests/`；定时触发只固定触发时当前有效 commit，不绑定或推进待发布变更。平台固定执行 `python -m pytest -q -p agentgov_testkit.pytest_plugin tests`，不会只选择本次新增或修改的用例。客户端不能提交命令、状态、业务归属或报告。
- 版本治理：`GET /api/agent-repository` 及 current/snapshot/discard-changes，`POST/GET /api/agent-change-sets` 及 events/diff/file-diff，发布使用 `POST /api/agent-change-sets/{change_set_id}/publish`，`GET /api/agent-releases` 及 restore/rollback。发布要求同一业务 Agent、当前待发布 `commit_sha` 上存在完整 `workspace/tests/` 的 `passed` 平台运行；旧提交通过不能放行新提交，已有失败和新增失败都必须修复。反馈闭环待发布版本不能强制绕过测试条件；未关联反馈、由版本治理 API 手工创建的待发布版本仍可通过受保护 API 强制发布，但必须提供非空原因并持久化原阻塞项和警告。provenance 不完整始终不可绕过。

3.0.0 不保留旧评测链兼容层。SQLite migration 0040 归档并删除更早的全局用例/场景包链；migration 0048 归档后删除数据库测试集、旧评估运行及待发布变更上的历史评测字段，并建立平台测试运行表；migration 0049 收口四阶段测试产物命名；migration 0051 原样归档旧的自然语言测试设计并替换为 pytest 代码候选契约；migration 0052 增加每 Agent 测试定时策略、调度事件与运行触发来源字段。旧名称只允许出现在迁移、归档和旧入口不存在的负向测试中。

运行态数据默认保存在 Docker 数据卷 `/data` 下，对应宿主机 `${HOME}/volume-agent-gov/data/`：

- `/data/runtime.sqlite3` 是反馈信号、SOC 事件、处置单、证据包、改进事项、四阶段内容子资源、平台测试运行、待发布变更、release 和 API session 的权威存储；升级前遗留的 `agent_jobs` 行作为只读历史保留。
- 归因、方案、执行和回归测试设计的治理 Agent 结果都以 SQLite 为权威存储；可执行测试正文只保存在业务 Agent Workspace Git，不在 SQLite 双写。
- `/data/business-agents/<agent_id>/workspace` 是业务智能体 Git 版本源；待发布 worktree 默认在同级 `version/worktrees/`，发布归档默认在 `version/releases/`。所有注册业务 Agent（含 `main-agent`）使用相同布局。
- `/data/feedback-signals/`、`/data/soc-events/`、`/data/feedback-cases/` 等旧目录仅为兼容路径，不再是权威存储。

当前实现基线见 [反馈闭环当前实现基线.md](docs/反馈闭环当前实现基线.md)。旧版 `FEEDBACK_OPTIMIZATION_LOOP_MVP.md` 已废弃，旧接口语义不再作为实现依据。

## Langfuse 监控

本项目优先通过 Claude Code 内置 OpenTelemetry 导出能力接入 Langfuse。开启后，API 运行时会把 `docker/.env` 中的 Langfuse 配置转换为 Claude Code 子进程可识别的 `CLAUDE_CODE_*` 和 `OTEL_*` 环境变量。

本系统的 Langfuse 只按本地 Docker profile 部署。容器内 API 通过
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

Runtime 会把 Langfuse trace 的 Session 设为 Playground/API 层 `session_id`，并在 metadata 中同时保留 `api_session_id`、`run_id`、`provider_gate_ms`、`sdk_init_ms`、`first_text_delta_ms`、`complete_ms` 和运行上下文；Claude SDK 返回的 `sdk_session_id` 仍保存到 SQLite，用于后续会话 resume。总耗时当前只作观测：当业务 Agent 的 OpenAPI→MCP 工具集合仍很大时，不用总耗时阈值掩盖上游工具收缩问题。

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

前端 topbar 的 Langfuse 按钮在地址为本机/缺省（`http://localhost:53000`）时，会按当前浏览器访问的 host 自动派生跳转地址（端口沿用配置值），因此远端用户点击会跳到 `http://<当前访问host>:53000` 而非 `localhost`，无需为每个部署硬编码 IP。若把 `FRONTEND_LANGFUSE_URL` 显式设为非本机的可达地址，则按该配置跳转。运行证据和改进事项 Trace 按钮会保留具体 `/project/.../traces/{trace_id}` 路径，只把容器内 `LANGFUSE_BASE_URL` 的 origin 替换为浏览器可达的前端 Langfuse 地址。

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

发布流程：① 改 `VERSION` → ② `make sync-version` → ③ 使用该版本镜像完成 `make test` 与真实容器验收 → ④ commit 并推送目标分支 → ⑤ `make tag`。`make tag` 只创建并推送 `v<VERSION>` 这一枚 annotated tag；禁止使用 `git push --tags` 批量推送本地 tag。

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
    "agent_id": "your-business-agent"
  }'
```

选择其他已注册业务 Agent：

```bash
curl -X POST "$API_BASE/api/chat" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d '{
    "agent_id": "soc-analyst",
    "message": "分析这个告警：rundll32.exe 加载 WININET.dll，父进程为 EdgeUpdate，命令行为 DispatchAPICall 1"
  }'
```

流式接口：

```bash
curl -N -X POST "$API_BASE/api/chat/stream" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d '{"message":"你好，先介绍你的能力", "agent_id":"your-business-agent"}'
```

流式业务对话会尽力生成下一轮建议（每轮**至多 N 条**候选，默认 3，由 `BACKEND_PROMPT_SUGGESTION_COUNT` 配置；模型给不满就少给，不凑数）。`AppSettings` 默认关闭后端派生这一受控特例，官方 `docker/.env.example` 与 `docker/.env.local-debug.example` 通过 `ENABLE_BACKEND_PROMPT_SUGGESTION=true` 显式开启；关闭时回退 Claude Code 原生 `--prompt-suggestions`，但该路径可能受上游 feature gate 或 cache 状态抑制。启动日志的 `prompt_suggestion_source` 会显示当前使用 `backend` 还是 `claude_native`。原生 `/api/chat/stream` 以 `event: prompt_suggestion` 输出 `{suggestion, suggestions, run_id, session_id}`；canonical `/v1/responses` 仅在 control 模式以 `event: agentgov.prompt_suggestion` 输出统一信封，`payload` 为 `{suggestion, suggestions, session_id}`，strict 模式不输出 AgentGov 扩展事件。`suggestions` 是完整候选列表；`suggestion` 恒等于 `suggestions[0]`，为向后兼容保留，只读它的客户端无需改动。整批候选在**一帧**内下发。建议生成失败或模型明确返回空时不影响正式回答，也不进入消息历史、SQLite run、response retrieve 或 SDK transcript；失败会记录不含异常正文的结构化 warning。

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

注意：这是兼容接入用的轻量 shim，不是完整 OpenAI API 实现。新集成使用 `/v1/responses` 的 `agentgov.agent_id` 显式选择业务 Agent；原生 `/api/chat*` 同样要求 `agent_id`。`agent`、`skills`、`skills_mode`、`allowed_tools`、`disallowed_tools` 和 `permission_mode` 已从 Chat 请求契约删除，传入会返回 `422`。工具权限、MCP、skills、subagents 和 hooks 以业务 Agent workspace 的 Claude Code 项目配置为准。

## 管理 API

```bash
curl -H "Authorization: Bearer $API_KEY" "$API_BASE/api/agents"
curl -H "Authorization: Bearer $API_KEY" "$API_BASE/api/skills"
curl -H "Authorization: Bearer $API_KEY" "$API_BASE/api/config"
curl -H "Authorization: Bearer $API_KEY" "$API_BASE/api/sessions"
```

## 配置挂载说明

`docker/docker-compose.yml` 会为容器内 API 注入 `RUNTIME_CONTAINER=1`，Runtime 自动读取 `docker/.env`，并从 `HOST_RUNTIME_VOLUME_ROOT` 派生运行态目录。`docker/.env.example` 默认值为 `${HOME}/volume-agent-gov`；本机 PyCharm/uvicorn 调试在宿主机进程中自动读取 `docker/.env.local-debug`，默认使用 `/tmp/local-debug-volume-agent-gov`，避免调试数据与容器部署数据混用：

```yaml
volumes:
  - ./runtime-bootstrap:/app/docker/runtime-bootstrap:ro
  - ${HOST_RUNTIME_VOLUME_ROOT}/governor-workspace:/governor-workspace
  - ${HOST_RUNTIME_VOLUME_ROOT}/data:/data
  - ${HOST_RUNTIME_VOLUME_ROOT}/claude-roots/governor:/claude-roots/governor
```

运行卷初始化源保存在 `docker/runtime-bootstrap/`。API 是共享运行卷的启动协调者；没有独立的 `agent-gov-runtime-init` 容器。初始化、缺失 Workspace 复制和 receipt 更新必须持有独占租约。启动完成后，`${DATA_DIR}/.agent-gov/runtime-coordination/receipt.json` 绑定代码、初始化源、env 摘要、运行模式和卷身份。

已有业务 Agent Workspace 是其 Git 版本真相源。初始化逻辑只在内置业务 Agent 的整个 Workspace 缺失时原样复制，不会逐文件回灌，也不会因 receipt 或运行模式变化自动改写 settings、MCP 或 hook。Compose 中 `RUNTIME_BOOTSTRAP_HOST_DIR` 默认是 `./runtime-bootstrap`（相对 `docker/docker-compose.yml`），以只读 bind mount 覆盖镜像内 `/app/docker/runtime-bootstrap`；部署目录缺少该初始化源时 Compose 会失败。真实运行态文件默认不提交到本仓库。

常用模板命令：

```bash
make runtime-bootstrap
make runtime-validate
make local-debug-env
make local-debug-bootstrap
make local-debug-validate
make runtime-bootstrap-scan
make runtime-clean
make local-debug-clean
make runtime-bootstrap-clean
make clean-runtime-artifacts
```

`runtime-bootstrap` / `local-debug-bootstrap` 通过同一协调器准备并验证运行卷；`runtime-validate` / `local-debug-validate` 只读校验 receipt、目录边界和必要配置。运行态配置回滚依赖每个业务 Agent 自己的 Git version/release，不依赖散落备份文件。

live Workspace 通过产品 Workspace 导出 API 按精确 Git commit 原样导出；AgentGov 不提供从
整个 runtime volume 生成或恢复仓库初始化源的并行命令。需要把某个 live Workspace 纳入内置
初始化源时，先在仓库外保留逐字节候选，再人工选择要提交的 `workspace/` 内容并运行
`make runtime-bootstrap-scan`。真实密钥、MCP 私有 header、数据库凭据和本机私有路径会阻断
提交；非秘密 endpoint、内网地址和较宽权限只提示复核，不会被静默改写。`runtime-bootstrap-clean`
与 `clean-runtime-artifacts` 仅用于清理由旧工具留下的 staging、备份和 `.bak-*` 产物。

## subagent 文件格式

示例：`workspace/.claude/agents/task-researcher.md`

```markdown
---
name: task-researcher
description: 收集任务相关事实并输出引用明确的分析结果。
tools:
  - Read
  - Grep
  - Glob
model: inherit
---

你负责读取当前 Workspace 中的资料，区分事实与推断，并返回可核查的分析结果。
```

项目依赖 Claude Code 原生发现机制加载 `.claude/agents/*.md`。后端不会通过 SDK Options 显式注入 agents。

## skill 文件格式

示例：`workspace/.claude/skills/evidence-analysis/SKILL.md`

```markdown
---
name: evidence-analysis
description: 基于 Workspace 资料完成证据优先的分析。
allowed-tools:
  - Read
  - Grep
  - Glob
context: fork
agent: task-researcher
---

## 输入

用户提供任务目标、相关资料标识和期望输出。

## 步骤

1. 读取与任务相关的 Workspace 资料。
2. 区分已确认事实、推断和缺失信息。
3. 输出结论、引用依据和待补信息。
```

## 权限与安全

`SKILL.md` 的 `allowed-tools` 字段兼容 Claude Code CLI。Agent、skill、MCP、权限、hooks 和 sandbox 均由 Claude Code project discovery 加载。Runtime 的 `ClaudeAgentOptions` 只选择 `setting_sources=["project"]`，不注入 agents、工具 allow/deny、`permission_mode` 或 hooks；`can_use_tool` 仅作为项目权限规则判定为 `ask` 后的人机交互桥，不参与 allow/deny 判定。

每个业务 Agent 的权限真相位于其运行态 `workspace/.claude/settings.json`。以下仅展示通用结构，不是平台为所有 Agent 注入的默认权限：

```json
{
  "permissions": {
    "allow": ["Read(./**)", "Glob", "Grep", "Skill"],
    "ask": ["Bash(*)", "Edit(./**)", "Write(./**)"],
    "deny": [
      "Read(./.env)",
      "Read(./.env.*)",
      "Read(./secrets/**)",
      "Read(../claude-root/**)"
    ]
  }
}
```

这意味着：

- 示例允许读取和搜索当前 Workspace；实际 Agent 可以在自身配置中增加经过审查的工具和输出路径。
- 如果 Workspace 把 Bash 放入 `ask`，`allow_for_run` 只绑定 `business_agent_id + run_id + low-risk category`，高风险或未分类请求不能整轮放行。
- 写/处置类 MCP 工具如放入 `ask`，才会在 `/v1/responses` control mode（`stream=true`）+ `ENABLE_CLAUDE_WEB_HITL=true` 下走 Web 确认卡片。
- 非流式入口和关闭 HITL 的流式入口都对实际权限询问显式拒绝；任何未预先允许且未进入 `ask` 的工具也不会被后端放行。

高危 Bash 阻断由 Workspace `.claude/settings.json` 中的原生 `PreToolUse` command hook 与 fail-closed sandbox 执行，hook 脚本随 Workspace 配置一同治理；后端不保留并行策略实现。具体输出目录由各 Agent Workspace 声明。

容器启动不迁移或修复已有 live Workspace；已有 Agent 需要调整时，应直接修改其 Workspace 并通过 per-Agent Git/版本治理留痕。`requires_web_hitl` 仅是从 Workspace `permissions.ask` 派生的只读观测值，权限真相仍只在 project settings。

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
- 业务 Agent owner（首次 turn 原子认领后不可变）
- 创建/更新时间
- turns
- title 和 metadata
- 当前活动 `run_id` 及其过期时间租约

`${HOME}/volume-agent-gov/data/sessions/` 是历史兼容路径，不再是权威存储。下一次请求传入同一个 `session_id` 时，运行时会尝试使用 SDK `resume` 继续 Claude Code 会话。一个 session 同时只允许一个活动 turn；活动租约期间 `/api/sessions/{session_id}` 与 `/v1/conversations/{conversation_id}` 的删除请求返回 `409`，租约过期后可重新认领。Playground 会同步禁用活动会话的删除按钮。

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

Docker Compose 部署默认读取 `docker/.env`；自动化环境可以通过 `COMPOSE_ENV_FILE` 选择另一份完整 env 文件，但同一次运行只读取这一份，不做分层覆盖。Compose 会为 API 注入内部标记 `RUNTIME_CONTAINER=1`，Runtime 因此自动选择容器部署配置，默认宿主机运行态根目录是 `${HOME}/volume-agent-gov`。

本机 host/PyCharm 调试无需额外设置 `RUNTIME_VOLUME_MODE`。宿主机 Python 进程会自动读取 `docker/.env.local-debug`；该文件不会被 Docker Compose 加载，默认把全部 workspace、data 和 claude-root 指向 `/tmp/local-debug-volume-agent-gov`：

```bash
make local-debug-env
make local-debug-bootstrap
```

`docker/.env.local-debug` 不是极简覆盖文件，它应与 `docker/.env` 保持 Runtime/API 应用配置同构；主要差异只应是路径、端口和宿主机访问地址。模型提供商、治理任务、DSPy、Claude SDK、Runtime Langfuse tracing 等配置都应在两个文件中有同名 key。Compose、前端容器端口、Langfuse Postgres/ClickHouse/Redis/MinIO 镜像和初始化账号等部署编排项只放在 `docker/.env`。

功能测试和验收测试不使用 `docker/.env.local-debug`，除非测试目标明确是本机调试 env 选择本身。`make test` 是离线功能硬门；需要真实模型和真实运行态的 live 验收必须先部署 Docker Compose 容器环境，并通过 `make container-live-test` 在容器内使用 `docker/.env` 和容器挂载路径执行。

需要调整本机调试路径时编辑 `docker/.env.local-debug`；需要调整容器部署路径时编辑 `docker/.env` 或部署系统注入的 `HOST_RUNTIME_VOLUME_ROOT`。需要显式沿用旧目录时，可以在对应模式中把 `HOST_RUNTIME_VOLUME_ROOT` 设置为 `<repo root>/docker/volume`。

本机 API 中的治理模型任务不复用交互式 Claude `/login` 状态。运行归因、优化方案、执行或回归测试设计生成前，必须在私有 `docker/.env.local-debug` 配置模型后端：Anthropic-compatible 路径需要 `MODEL_PROVIDER_API_KEY`，本地/内网 vLLM 路径需要 `MODEL_PROVIDER_BACKEND=vllm` 和不带 `/v1` 的 `MODEL_PROVIDER_API_URL`。缺少模型凭据或模型 URL 时，请求会在启动 Claude Code 前失败；缺少 Anthropic-compatible key 时错误码为 `AGENT_AUTH_REQUIRED`，并提示当前 profile 和 env 文件。

API 使用 `LOG_LEVEL` 控制应用日志级别：容器部署的 `docker/.env` 默认 `LOG_LEVEL=info`，本机调试的 `docker/.env.local-debug` 默认 `LOG_LEVEL=debug`。API 启动日志会打印 `log_level`、`runtime_volume_mode`、`settings_env_file`、`model_provider_backend`、`model_provider_vllm_sidecar_threshold`、`model_provider_vllm_allow_direct`、`provider_api_key_configured`、`provider_api_url_configured`、`governance_agent_timeout_seconds`、`dspy_output_formatter_timeout_seconds`、`claude_web_hitl_enabled`、`hitl_timeout_seconds`、`workspace_dir`、`data_dir`、`claude_root` 和 `langfuse_base_url`。如果 PyCharm 调试时看到 `runtime_volume_mode=container`，说明进程被误标记为容器或环境变量被外部覆盖。

本机 PyCharm 调试如果需要访问本机 Docker 暴露的 HTTP MCP 服务，在 `docker/.env.local-debug` 中设置 `MCP_SERVER_URL=http://localhost:58001/mcp`，然后执行 `make local-debug-bootstrap`；协调器只准备缺失 workspace 并写入 receipt，不改写已有配置。workspace `.mcp.json` 中的 `${MCP_SERVER_URL}` 由 Claude Code 子进程使用当前所选完整 runtime env 原生解析。main profile 和 feedback profiles 都只使用各自 workspace 的官方 `.mcp.json`。宿主机存在代理变量时，同时在 `CLAUDE_ENV_JSON` 中设置 `NO_PROXY` 和 `no_proxy`，避免本机地址请求被代理转发。

如果刚从 Docker API 切换到 PyCharm 本机调试，先修复后端共享 volume 的宿主机权限。该脚本只处理 API 使用的 workspace、data 和 `claude-roots/*`，不处理 Langfuse 数据卷：

```bash
scripts/fix_host_backend_volume_permissions.sh
```

PyCharm 后端调试建议使用 Python run configuration：

```text
Module name: app.runtime.service_launcher
Parameters: api
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
