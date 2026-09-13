---
name: "runtime-env-governance"
description: "治理 agent-gov 的 AgentScope Runtime/env、本机 PyCharm 调试、Docker/Compose 部署、Langfuse/OTel、volume 路径及 provider/MCP 凭据边界。用户提到 RUNTIME_VOLUME_MODE、RUNTIME_CONTAINER、docker/.env、docker/.env.local-debug、覆盖、PyCharm 调试、Langfuse、本机/容器模式或 MODEL_PROVIDER_API_KEY 时使用。"
---

# Runtime / Env 治理

本技能用于防止 runtime/env 改动再次出现“配置写了但不生效”“本机和容器数据混用”“把 env 文件叫覆盖”“provider/MCP 密钥越过 Runtime 边界”“把 AgentScope Runtime 失败误报成 API 自身故障”等问题。

## 必做矩阵

执行前先写 Consumer x Mode x Boundary 矩阵：

| Consumer | Mode | Env source | Runtime root | Secret boundary | Verification |
| --- | --- | --- | --- | --- | --- |
| AgentGov API container | container | 默认 `docker/.env`，自动化可由 `COMPOSE_ENV_FILE` 选择一份完整 env；Compose 注入 `RUNTIME_CONTAINER=1` | `${HOME}/volume-agent-gov/data`、Governor workspace 和候选 Harness 只读挂载 | `API_KEY`、Runtime 共享密钥和可选 Langfuse 查询凭据；禁止 provider/MCP 密钥 | `AppSettings`、sanitized Compose config、API health |
| AgentScope Runtime | container | 与 API 选择同一份完整 Compose env，但只注入 Runtime 所需键 | `${HOME}/volume-agent-gov/agentscope-runtime/{data,workspaces,candidates}` | `MODEL_PROVIDER_API_KEY`、MCP 凭据和 Runtime 共享密钥只在此进程 | Runtime `/health`、会话/消息持久化、OTel smoke |
| Host AgentGov API / PyCharm | local-debug | 非容器进程选择 `docker/.env.local-debug` | `/tmp/local-debug-volume-agent-gov` by default | API key、Runtime 共享密钥和可选 Langfuse 查询凭据；示例不含 provider/MCP 密钥 | settings、bootstrap、startup log |
| Host AgentScope Runtime | local-debug | 独立进程使用未提交的私有 shell/env | `/tmp/local-debug-volume-agent-gov/agentscope-runtime` | provider/MCP 密钥仅进入该进程 | Runtime settings、health 和集成测试 |
| Vite frontend dev | frontend-local | `frontend/.env.local` | none | `VITE_*` 会进入浏览器包，只能放本地调试所需值，不能放 provider/MCP/Langfuse secret | `pnpm` unit/build 或浏览器 smoke |
| Langfuse self-hosted | container profile + host browser | Compose service URL in containers，`localhost:50402` on host/browser | `${HOME}/volume-agent-gov/langfuse` | OTel 仅写语义元数据、长度和 SHA-256；查询/摄取凭据保持私有 | `make langfuse-smoke`、trace 完整性测试 |
| Governance model request | AgentGov API 调度，AgentScope Runtime 执行 | API 只发送签名上下文和稳定引用 | 各自事实库 | provider/MCP 密钥不返回 API；API 只保存 run/session/reply/trace 引用 | main-flow、负向凭据边界和 trace gate |
| Real container acceptance | core / langfuse / browser | `COMPOSE_ENV_FILE` 选择一份完整 env | runner 创建临时根，禁止复用真实宿主卷 | runner 不输出 env 值、prompt、tool 参数或凭据 | 公共 Make 入口 build、`--force-recreate`、freshness 后验收 |

## 术语规则

- 这里不是 layered override。除非代码真实叠加读取多个 env 文件，否则不要写“覆盖文件”“私有覆盖”“覆盖配置”。
- 用“选择 env 文件”“本机调试 env 文件”“容器部署 env 文件”“私有 env 文件”描述当前实现。
- `COMPOSE_ENV_FILE` 只选择一份完整 Compose env，不能把它实现或描述成与 `docker/.env` 叠加。
- `RUNTIME_VOLUME_MODE` 不应出现在官方 env 示例中；模式选择由运行环境和 `RUNTIME_CONTAINER` 决定。

## 设计规则

- 本项目容器宿主机映射统一使用 `50400–50499`（含边界），隔离验收也在此范围选择空闲端口；内部端口和外部依赖端口不随之改动。默认分配以 README 与 Compose 为准。
- `.env.local-debug` 的内容不能承担“选择 local-debug”的职责；它只在已被选择后提供配置值。
- AgentGov API 不得读取、转发或记录 provider/MCP 密钥；所有聊天、治理和测评模型调用统一通过 AgentScope Runtime 公共 API。
- AgentScope Runtime 没有 `MODEL_PROVIDER_API_KEY` 或必需 MCP 凭据时应在启动/预检阶段失败，并由 API 投影稳定、已脱敏的错误码。
- local-debug API 示例故意不含 provider/MCP 密钥；本机 AgentScope Runtime 必须独立启动并从未提交的私有环境读取这些值。Compose、前端容器端口、Langfuse infra 和初始化账号只属于 container env。
- 用户要求“启动、重启、重建、部署、生效最新代码”时，默认在原 Docker Compose 容器服务中生效；除非用户明确要求本机调试，不另起临时 Vite 或旁路 API 服务。
- 真实容器验收前必须走公共 Make 入口：在隔离项目和临时卷中以当前工作树重建镜像、`--force-recreate`、加载所选 env/config 并校验本轮标记；结束后只清理该隔离项目。正式部署使用 `make build` 与 `make up` / `make all-up COMPOSE_UP_FLAGS=--force-recreate`，不能把隔离验收当作正式服务已更新。直接调用私有 target、`:impl`、live pytest 或真实容器 Node 脚本属于旁路。
- API 使用 `LOG_LEVEL` 控制应用日志级别；container 默认 `info`，local-debug 默认 `debug`。
- AgentGov API liveness 只访问 `/health/live`；Runtime readiness 通过 AgentScope Runtime `/health` 和 API `/health/ready` 返回结构化诊断，不以端口监听替代可用性。
- 真实 API key、MCP header、数据库凭据、本机私有路径和运行态 SQLite 不得提交。
- Playground 可显示执行所需业务内容，但 AgentGov 持久层和 Langfuse OTel 默认不得保存原始 prompt、输出、tool/MCP 参数或 secret；仅保留受控语义字段、精确 UTF-8 长度与 SHA-256，必要调试证据必须另行显式授权。
- 数据库和 workspace 路径必须随 mode 分离：container 默认 `${HOME}/volume-agent-gov`，local-debug 默认 `/tmp/local-debug-volume-agent-gov`。

## 验证清单

- `tests/test_settings.py` 覆盖 env 文件选择、`runtime_volume_mode`、`LOG_LEVEL`、路径派生和启动日志字段。
- `tests/test_repository_env_policy.py` 覆盖 root `.env` 禁止、官方 env 示例不含 `RUNTIME_VOLUME_MODE`、local-debug 不含 provider/MCP 密钥、Compose 仅向 Runtime 注入这些密钥。
- `tests/test_documentation_contracts.py` 覆盖 README 术语、PyCharm 环境变量留空、`AGENT_AUTH_REQUIRED` 和启动日志字段说明。
- 影响治理模型主流程时运行 `make main-flow-test`；提交、发版或用户要求完整验证时运行 `make test`。
- 提交前确认 `docker/.env`、`docker/.env.local-debug`、`frontend/.env.local`、runtime volume、SQLite、logs、dist 和 cache 都未进入 staged diff。

## 测试模式选择矩阵

| 改动类型 | 测试环境 | 推荐验证命令 | 不使用 |
| --- | --- | --- | --- |
| docs / skill / README 术语同步 | 宿主机仓库环境 | `git diff --check`、`scripts/check_docs_governance.py`、`scripts/check_codex_governance.py --mode fail`、相关 skill 单测 | 不默认跑 `make test`，不使用 `local-debug` |
| settings/env 选择代码 | 宿主机仓库环境 | `tests/test_settings.py`、`tests/test_repository_env_policy.py`、`tests/test_documentation_contracts.py` | 不用 `docker/.env.local-debug` 伪装容器 |
| Runtime 健康和凭据边界回归 | 宿主机 + Docker Compose | Runtime/settings/health 单测后运行 `make container-core-smoke` | 不用端口监听或 API 单测替代 Runtime health |
| live 模型或真实运行态验收 | 隔离 Docker Compose + Langfuse | 提供仓库外人工复核场景后运行 `REQUIRE_LIVE_RUNTIME=1 REAL_ACCEPTANCE_AGENT_ID=security-operations-expert REAL_SCENARIO_FILE=/outside/reviewed-scenarios.json make container-live-test`；该公共入口固定要求完整 Trace，可用严格整数环境变量 `LIVE_ACCEPTANCE_RUNS`／`LIVE_ACCEPTANCE_CONCURRENCY` 调整配额 | 不直接调用私有 target，不使用 `docker/.env.local-debug` |
| core 只读容器 smoke | 隔离 Docker Compose | `make container-core-smoke`；一次重建后并行 readiness、UI 首页和 OpenAPI 检查 | 不并发 build/up/down，不直接调用私有 target |
| 启动 / 重启 / 重建 / 部署生效 | 所选部署 env / 持久卷 | `make build` 后 `make up` / `make all-up COMPOSE_UP_FLAGS=--force-recreate`；旧库预检先于初始化，readiness 失败必须阻断 | 不用隔离验收或 local-debug 代替正式部署 |
| local-debug 专项能力 | 宿主机 Python / PyCharm | 明确命名的 local-debug 专项测试和 bootstrap/repair 命令 | 不把结果声明为容器验收 |
| Langfuse / OTel | 隔离 Docker Compose | 提供仓库外复核成功场景后运行 `REQUIRE_LIVE_RUNTIME=1 REAL_ACCEPTANCE_AGENT_ID=security-operations-expert REAL_SCENARIO_FILE=/outside/reviewed-scenarios.json make langfuse-smoke`，并核验 trace root、run/session/reply/trace 关联及无原文泄漏 | 不用任意 trace 存在替代 `agentgov.run` root 完整结束；不从公开 API 读取 observation payload |
| 发版或用户要求完整验证 | 发布前工作区 | `make test`，按变更追加 `make container-core-smoke`、绑定外部复核场景的 `make langfuse-smoke` 和 UI 公共 smoke | 不用单一 coverage 百分比替代主流程或 live 证据 |

## 安全并行边界

- 宿主机目标 pytest、配置审计和前端 build 可按任务并行，默认最多 3 路；最终 `main-full` 保持单进程串行。
- `make container-core-smoke` 只在同一轮刷新和 freshness 校验完成后并行三个只读 HTTP 检查。
- Compose build/recreate、真实浏览器、live provider、Langfuse 与隔离 health E2E 由统一锁串行；`container-core-smoke` 内部只读检查可由公共入口并行；TIA/xdist 未晋级前只作 shadow。

## 配置面选择

- 常驻入口只放在唯一根 `AGENTS.md`；展开说明放 `.codex/guidance/*.md`，命令执行策略才放 `.codex/rules/*.rules`。
- 详细矩阵和 checklist 保留在本技能，避免常驻上下文膨胀。
- 可机械检查的内容优先放入 pytest 或治理脚本；不要只写成人工自报规则。
