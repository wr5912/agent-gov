# 智能体治理平台 AgentGov

AgentGov 是面向多业务智能体的治理平台。当前代码采用 AgentScope Runtime 单一执行链路：
AgentGov 负责身份、版本、策略、反馈与发布治理，AgentScope
负责会话、智能体执行、工具调用和原生事件流。

## 架构边界

默认 Compose 只启动三个核心服务：

- `agent-gov-api`：FastAPI 控制面和唯一公开后端入口。
- `agentscope-runtime`：内部 AgentScope 执行面，仅在 Compose 网络暴露 `8090`。
- `agent-gov-ui`：React/Vite 前端。

可选 `docker/docker-compose.langfuse.yml` 的 `langfuse` profile 提供 Langfuse 及其存储服务；
公共 Make 入口按需加载该文件，核心服务不要求填写未启用的 Langfuse 存储凭据。浏览器和外部调用方只访问
AgentGov API，不直接访问 AgentScope Runtime 管理面。模型与 MCP 凭据只注入
`agentscope-runtime`；AgentGov API 仅把可选 Langfuse 项目凭据用于查询，不执行写入。
这对凭据同时供 Runtime 摄取使用，凭据本身没有降权成只读 key。

前端是运行调试与治理观察界面，不提供 Terminal，不接管 AgentScope Runtime
进程或生产处置；所有运行交互均通过 AgentGov API 完成。AgentGov 负责治理，
外部业务系统继续负责业务界面、用户权限、生产系统和高风险动作。
本期不建设产品内的通用协作模型，也不接入外部研发协作平台；待核心治理能力稳定后再基于
真实需求选型。

当前 Compose 是单租户 operator 部署：一个受信任操作者使用一套 API 凭据，平台不提供
跨用户的数据隔离或横向授权。API、UI 和 Langfuse 的宿主端口默认只绑定 `127.0.0.1`；远程
部署脚本以及 `make up`、`make all-up`、`make ui-up`、`make langfuse-up` 遇到非 loopback 绑定都会 fail closed，只有分别显式设置
`API_ALLOW_PUBLIC_BIND=1`、`FRONTEND_ALLOW_PUBLIC_BIND=1` 或
`LANGFUSE_ALLOW_PUBLIC_BIND=1` 才继续。该开关只是风险确认，不会把共享 token 变成多用户
认证；需要远程接入时仍须在前方配置 TLS、身份认证和访问控制。启动服务应使用上述受支持的 Make 入口，不把裸 `docker compose up` 作为绕过治理门的部署接口。

```text
Browser / Client
       |
       v
agent-gov-ui ----> agent-gov-api ----> agentscope-runtime ----> Model / MCP
                          |
                          +----> SQLite / Agent Harness Git
                          |
                          +----> Langfuse query API

agentscope-runtime ----OTLP----> Langfuse ingest
```

## 目录结构

```text
.
├── agentscope_runtime/          # AgentScope Runtime 服务和策略中间件
├── app/
│   ├── runtime_gateway/         # AgentGov 到 AgentScope 的窄适配层
│   ├── routers/                 # 治理、反馈、资产和配置 API
│   └── services/                # 治理应用服务
├── docker/
│   └── runtime-bootstrap/       # 初始 AgentScope Harness
├── frontend/                    # React / TypeScript / Vite UI
├── scripts/                     # 启动、验收、迁移和质量门禁
├── tests/
└── Makefile
```

前端由仓库内的 React/TypeScript 代码直接构建；A2UI 已退出镜像与运行依赖，仓库不再携带
`docker/vendor/A2UI` 或专用安装清单。

运行态默认位于宿主机 `${HOME}/volume-agent-gov`：

- `data/runtime.sqlite3` 保存 AgentGov 治理数据和 `run_id` 映射。
- `data/business-agents/<agent_id>/workspace` 是受版本治理的业务 Agent Harness。
- `agentscope-runtime/data/agentscope.db` 保存 AgentScope 会话和消息事实。
- `agentscope-runtime/workspaces/` 是 Runtime 工作目录。
- `agentscope-runtime/candidates/` 是候选 Harness 的只读输入边界。
- `langfuse/` 保存可选的 Langfuse 数据。

## 快速启动

要求 Docker Compose、Python 3.11、`uv` 和 pnpm。

```bash
make setup
```

编辑 `docker/.env`，至少替换以下私有值：

```dotenv
API_KEY=replace-with-api-key
AGENTGOV_RUNTIME_SHARED_SECRET=replace-with-at-least-32-random-characters
MODEL_PROVIDER_API_KEY=replace-with-private-provider-key
MODEL_PROVIDER_API_URL=https://api.deepseek.com
AGENTSCOPE_MODEL_NAME=deepseek-chat
```

以上使用 DeepSeek 的 OpenAI 兼容接口，`AGENTSCOPE_MODEL_TYPE` 与
`AGENTSCOPE_CREDENTIAL_TYPE` 均为 `openai_credential`。若选择 Anthropic 兼容接口，
将基础地址改为 `https://api.deepseek.com/anthropic`，并将这两个类型同步设为
`anthropic_credential`；仅修改地址不会切换客户端协议。基础地址不要填写完整的
`/chat/completions` 或 `/messages` 请求路径，参见
[DeepSeek 官方接入说明](https://api-docs.deepseek.com/)。

模型高级参数只使用一个宿主配置键：

```dotenv
AGENTSCOPE_MODEL_PARAMETERS_JSON={}
```

内置 sec-ops MCP 按本机免鉴权服务声明，不发送占位 Authorization。容器通过
`SEC_OPS_MCP_URL`（默认 `http://host.docker.internal:58001/mcp`）访问；服务端必须提供
Harness 声明的完整工具、资源和模板清单。需要认证的 MCP 应在发布的 Harness 中显式声明
`credential_refs`，并仅向 Runtime 配置相应私有值；缺失凭据会在绑定该 Harness 时拒绝，
不阻断其他 Agent 的服务启动。

启动三个核心服务：

```bash
make images-prepare  # 新 Docker host 首次联网准备 digest-pinned 依赖镜像
make build
make up
```

连同 Langfuse 启动：

```bash
sed -i 's/^LANGFUSE_ENABLED=false$/LANGFUSE_ENABLED=true/' docker/.env
make langfuse-env
make all-up
```

`make images-prepare` 只拉取 Compose 当前声明的第三方镜像，以及三个项目 Dockerfile
声明的构建基础镜像；所有引用都必须以 `@sha256` 固定。拉取后逐一复核本地 image ID
和 Docker daemon/host-filesystem 边界。它是新机器的显式
联网准备步骤；日常 `build/up/all-up` 始终使用 `--pull never`，缺镜像时 fail closed。
离线发布应使用 `scripts/deploy_agent_gov_to_host` 产生并校验 SHA-256 的项目镜像与
Langfuse 依赖镜像包；目标机在首次 `docker load` 前会校验两个归档，加载后仍按
Compose 精确 digest 和 image ID 放行。

`docker/.env.example` 默认关闭 Langfuse，避免核心三服务在可选观测栈未启动时误连内部
Langfuse，也避免非空模板 key 被当作可用凭据。已有私有 `docker/.env` 应保留既有身份和
存储加密参数；`make langfuse-env` 只补齐空值或 `replace-with-*`，不轮换已有值。

`make up` / `make all-up` 会先只读检查现有 Runtime 数据库。空库创建当前
`agentscope-runtime-v3`；精确匹配的 `agentscope-runtime-v1` / `agentscope-runtime-v2` 可在启动锁内迁移到 v3，
其余旧 Claude、未知或漂移 schema 均在初始化与重建前拒绝，不自动清空。服务启动后必须
通过 Runtime readiness，否则命令失败。

本项目容器映射到宿主机的端口统一使用 **50400–50499（含边界）**，并由启动前置检查强制范围与五个端口互不重复；默认分配如下：

| 服务 | 默认宿主机端口 | 容器内部端口 |
| --- | --- | --- |
| API / Swagger | `50400` | `8080` |
| UI | `50401` | `5173` |
| Langfuse（启用时） | `50402` | `3000` |
| MinIO API（启用 Langfuse 时） | `50403` | `9000` |
| MinIO 控制台（启用 Langfuse 时） | `50404` | `9001` |

访问 UI：`http://localhost:50401`，API：`http://localhost:50400`，
Swagger：`http://localhost:50400/docs`，Langfuse：`http://localhost:50402`。
自定义宿主机映射也应选择该区间内的空闲端口；容器内部端口保持不变，AgentScope Runtime 和
数据库不新增宿主机映射。隔离容器验收从同一区间选择互异空闲端口，空间不足时明确失败。

以上端口默认仅能从宿主机访问。不要为方便调试直接把 `*_BIND_IP` 改成 `0.0.0.0`；确需远程
访问时，应先部署带 TLS 和身份认证的反向代理，再为需要暴露的单个服务设置对应
`*_ALLOW_PUBLIC_BIND=1`。

常用运维命令：

```bash
make logs                 # API 与 AgentScope Runtime 日志
make ui-logs              # 前端日志
make compose-diagnose     # 服务、容器与公开健康契约诊断
make down                 # 停止核心服务
make langfuse-stop        # 停止 Langfuse profile
make runtime-recreate     # 已构建当前源码镜像后，只重建 Runtime；保留 API 候选测试 Session
make ui-recreate          # 已构建当前源码镜像后，只重建 UI；保留 API、Runtime 与候选测试 Session
```

候选 Harness 新增 subagent 模板后，先 `make build`，在候选测试 Session 保持活动时运行
`make COMPOSE_ENV_FILE=docker/.env runtime-recreate`，再重试真实候选对话；该命令不发布候选，
也不替代最终的整栈重建与 Playground 验收。

## AgentScope Runtime 公共契约

外部客户端通过 AgentGov API 使用 Runtime：

- `GET /api/runtime/agents/{agent_id}/current`：读取当前发布版本到 Runtime Agent 的精确绑定。
- `GET /api/runtime/agent-schema`：读取固定 AgentScope 版本的原生 Agent 创建表单 schema。
- `POST /api/runtime/sessions/`：创建绑定 Agent 版本的会话。
- `GET /api/runtime/sessions/?governance_agent_id=...`：按业务 Agent 聚合各发布版本的会话。
- `PATCH /api/runtime/sessions/{session_id}`：只允许重命名已绑定会话。
- `GET /api/runtime/sessions/{session_id}/messages`：读取 canonical messages。
- `GET /api/runtime/sessions/{session_id}/status`：读取会话状态。
- `GET /api/runtime/sessions/{session_id}/workspace/status`：读取不含路径的 Workspace 状态投影。
- `GET /api/runtime/sessions/{session_id}/workspace/mcp`：读取不含配置与凭据的 MCP 连接及工具投影。
- `GET /api/runtime/sessions/{session_id}/workspace/skills`：读取当前 Session 实际加载的 skill 投影。
- `GET /api/runtime/sessions/{session_id}/stream`：透传 AgentScope `AgentEvent` SSE。
- `POST /api/runtime/chat/`：触发一次受治理运行。
- `POST /api/runtime/sessions/{session_id}/interrupt`：中断会话中的运行。
- `DELETE /api/runtime/sessions/{session_id}`：删除无活动运行的会话。
- `GET /api/agent-runs/{run_id}`：读取 AgentGov 运行映射。
- `GET /api/agent-runs/by-client-operation`：在 POST 响应不确定时按会话和操作 ID 定位唯一 run。
- `GET /api/agent-runs/{run_id}/pending-actions`：恢复当前 run 的待处理 HITL/外部执行项。
- `GET /api/agent-runs/{run_id}/trace`：按运行解析 OTel trace。
- `POST /api/agent-runs/{run_id}/cancel`：取消精确运行。

除健康检查外，公共 API 使用 `Authorization: Bearer <API_KEY>`。内部
`/internal/*` 只用于 Runtime receipt 回调，并由独立共享密钥保护。

### 标识符关系

- `session_id`：AgentScope 会话标识，多轮共享。
- `run_id`：AgentGov 为一次触发生成的治理标识，用于反馈、取消、审计和发布证据。
- `reply_id`：AgentScope 在一次运行中产生的回复/确认标识，一个 run 可关联多个。
- `trace_id`：同一次运行的 32 位十六进制 OTel Trace ID。

这些标识用途不同，值不要求相同。`GET /api/agent-runs/{run_id}` 返回它们之间的
映射；`GET /api/agent-runs/{run_id}/trace` 再从 `trace_id` 查询 Langfuse 语义轨迹。

Session 创建、chat 和会话读写请求中的 `agent_id` 使用该会话绑定的 `runtime_agent_id`；
业务 Agent ID 用于查询发布绑定和跨版本会话列表。若 `/current` 未返回已激活绑定，必须回到
候选的测试、审批与发布命令重试，客户端不能另走 provision 旁路。调用步骤见
[集成指南](docs/AgentGov集成指南.md)。替换决策、原方案修订和完整验收门槛见
[Runtime 替换实施基线与验收](docs/engineering/AgentGov_AgentScope_Runtime替换实施基线与验收.md)。

## Agent Harness 与受控改进

活动 Harness 使用 AgentScope 原生资产：

- `agent.yaml`：Agent 身份、模型引用、权限模式和 Workspace 策略。
- `AGENT.md`：系统指令和行为约束。
- `skills/**/SKILL.md`：可复用技能。
- `subagents/<name>/agent.yaml`、`subagents/<name>/AGENT.md`：子智能体定义。
- `mcp/*.json`：MCP 配置，敏感值只通过 `credential_refs` 引用。
- `tests/`：与该 Agent 版本绑定的回归测试。

Bash 的允许规则仍受安全子集约束：`date` 仅查询当前时间；`jq` 仅接受 `--null-input` / `-n`
与 JSON 字面量或 `.`、受控输出格式选项，不接受文件、stdin、环境读取、模块或任意 jq 程序；
`mkdir` 仅接受目录路径和 `--parents` / `-p`，每个目录仍须满足可写路径策略；`pwd` 不接受参数。
未知参数和动态 shell 展开均拒绝。读取文件使用受管 `Read`，不通过 Bash 绕过禁读规则。

聊天运行不能直接修改自己的活动 Harness。改进必须经过“反馈/证据 → 候选工作区 →
策略校验 → 回归测试 → 人工确认 → 原子发布”，发布只影响新会话；已有会话继续绑定
原版本。旧 Workspace 的一次性离线转换使用：

```bash
.venv/bin/python scripts/convert_claude_harness.py --help
```

转换器只用于迁移输入，不在生产启动或运行链路中调用。MCP 格式和凭据边界见
[`docker/MCP_REPLACEMENT_GUIDE.md`](docker/MCP_REPLACEMENT_GUIDE.md)。

## 健康、验收与质量门禁

- `GET /health/live`：只证明 AgentGov API 进程存活。
- `GET /health/ready`：检查 AgentScope Runtime 是否可达；不可达时返回 `503`。
- `GET /health`：返回 API、Runtime、依赖版本和可观测配置摘要。

```bash
make runtime-validate        # bootstrap dry-run + AgentScope cutover 静态检查
make cutover-check           # 再校验 Compose 服务解析
make smoke                   # 基于当前工作树 rebuild/force-recreate 后检查 readiness
make container-core-smoke    # readiness、UI 与 OpenAPI 并行只读验收
make container-openapi-check
make ui-smoke
REQUIRE_LIVE_RUNTIME=1 \
REAL_ACCEPTANCE_AGENT_ID=security-operations-expert \
REAL_SCENARIO_FILE=/outside/reviewed-scenarios.json \
make container-live-test langfuse-smoke
REQUIRE_LIVE_RUNTIME=1 \
REAL_ACCEPTANCE_AGENT_ID=security-operations-expert \
REAL_SCENARIO_FILE=/outside/reviewed-scenarios.json \
make ui-playground-cancel-smoke ui-feedback-smoke
REQUIRE_LIVE_RUNTIME=1 \
BROWSER_TECHNICAL_SCENARIO_FILE=/outside/browser-technical-scenarios.json \
make ui-playground-technical-smoke
make test
make typecheck
```

所有 Compose 验收都由 `scripts/run_container_acceptance.py` 在锁内执行。runner 只从所选
`COMPOSE_ENV_FILE` 读取模型、MCP、Langfuse 凭据和非宿主配置；每轮生成只用于该隔离栈的
一次性 API 密钥并同步前端，不修改所选文件中的正式身份。另建临时 Runtime 根、唯一 Compose project/
容器前缀和随机回环端口，覆盖全部 `HOST_*_MOUNT` 与 Langfuse 数据挂载。随后使用当前工作树
构建并 `--force-recreate`，结束时无论成功失败都执行 `down --volumes`，成功停止隔离容器后才删除
临时目录。若临时卷由容器用户写入，runner 使用已构建的 API 镜像、单一临时挂载和无网络维护
容器回收目录权限；不会更改正式卷权限。清理失败会报错并保留临时目录，不宣告验收成功。因此公开
验收不会重建既有项目，也不会读写 `${HOME}/volume-agent-gov`。`make smoke` 和浏览器验收只访问
AgentGov 的公开端口，不暴露 Runtime 管理面。

OpenAPI 离线导出始终使用独立临时环境，不沿用容器或宿主机的运行卷。

普通启动和隔离验收都在 Runtime 启动前，使用 API 镜像初始化合法业务 Git 并物化
clean HEAD 的不可变 Harness 快照；Runtime 仍只读加载快照中的 subagent 模板。该步骤不提交
已有 Workspace 的未提交变更、不创建 Session 或调用模型。运行中新发布或候选 Harness 的
模板仍遵循显式重启提示：Runtime 用 `409 / RUNTIME_TEMPLATE_RESTART_REQUIRED` 表达尚未加载的
模板；控制面保留精确快照和重试定位信息。发布等待重启时也保留审批证据与发布意图，处理在途运行并
执行受控 Runtime 重启后重试同一发布命令。详见[发布激活契约](docs/业务AgentWorkspace包导入与热加载产品工程方案.md#5-测试审批和发布激活)；
首次启动通过不代表候选测试与发布后的重载已经验收。

Runtime 镜像在构建期封存沙箱 gateway 的依赖与工具；运行时只使用镜像内的离线 wheel，
每个 Workspace 仍有独立的可写环境，不共享 gateway venv。缺少离线资产时明确失败，
不在线下载或退回无沙箱执行。Workspace 初始化和 MCP 能力校验共用有界等待预算。
Runtime 容器使用 Docker init 回收沙箱与健康探针的孤儿进程；不关闭沙箱探针。
内部签名校验仅重放一次已校验请求体，随后保留原始连接断开通知，SSE 仍透传原生字节。
Playground 为建连保留 60 秒预算并显示连接中状态。Gateway 只立即发送一个无业务语义的
SSE readiness comment `:\n\n`，使 Firefox 不必与 AgentScope 约 30 秒的空闲心跳竞态；随后使用
`aiter_raw()` 原样透传上游字节。Vite 不再注入额外前导帧，两层都不伪造业务事件或改写事件正文。
Playground 在初始 chat 尚未提交时建连失败，应保留具体错误并允许重试；请求已提交但结果
不确定时仍须按 `session_id/client_operation_id` 核对精确 run。暂时查询 `404` 时只用完全相同的
input、上下文和操作 ID 幂等重试，不能生成新意图；只有 chat 明确返回不会启动执行的 4xx 且
精确 operation 不存在时，才回滚 optimistic turn。恢复失败的具体原因不得被通用“等待终态”提示覆盖。
精确 run 终态确认后，还需通过 Runtime 公共 Session 状态确认执行槽已释放，才恢复发送；
例如 Runtime 的会话标题生成可能晚于治理 run 终态，不能在这段收尾期间提前提交下一轮。

`container-live-test` 和 `ui-feedback-smoke` 使用带 Langfuse 的隔离项目，验证真实运行和反馈来源的
完整 Trace，而不是在关闭观测的 core profile 下要求 Trace complete。
Trace 对账读取 Langfuse OTel 返回的 `metadata.attributes`，仅对内容长度做严格非负整数解析；
冲突属性、缺失身份、错误父链或不完整指纹仍不能被判为 complete。
`container-live-test` 会调用真实 provider，所以默认拒绝执行；必须显式设置
`REQUIRE_LIVE_RUNTIME=1`，隔离栈的一次性 API 密钥与所选 env 的模型凭据都须通过非占位校验。
该入口不再内置“最小成功”数据，必须提供源码仓库外的人工复核场景文件和明确的已发布 Agent；
runner 会拒绝仓库内场景、重复输入或循环复制输入制造证据：

```bash
REQUIRE_LIVE_RUNTIME=1 \
REAL_ACCEPTANCE_AGENT_ID=security-operations-expert \
REAL_SCENARIO_FILE=/outside/reviewed-scenarios.json \
LIVE_ACCEPTANCE_RUNS=50 \
LIVE_ACCEPTANCE_CONCURRENCY=10 \
make container-live-test
```

完整候选门使用 `REQUIRE_LIVE_RUNTIME=1 REAL_ACCEPTANCE_AGENT_ID=security-operations-expert
REAL_SCENARIO_FILE=/outside/reviewed-scenarios.json make container-release-candidate`。该目标会先重建
隔离 Compose，再执行 50 个实质不同场景、并发 10、Chromium 与 Firefox 各 3 次真实浏览器
取消验证；四阶段反馈闭环只执行一次真实生成、测试与发布，随后由 Chromium 与 Firefox 各 3 次
读取同一组已持久化闭环证据，避免重复发布污染后续 baseline。它还会在任何
`container-live-acceptance` GAP 仍打开时失败；它的名称刻意不宣称已经覆盖重启演练和最终发布签字。
正式公共浏览器入口强制 `BROWSER=both`，单浏览器只能从内部实现入口进行非正式调试，不生成
候选发布通过证据。

验收对象是 AgentGov 平台，业务 Agent 只是端到端调测载体。业务依赖缺失不作为平台验收前置条件；
专属外部服务的可用性与专业业务效果另行验收。平台的 MCP 接入、权限、人工审批、恢复和反馈改进
能力仍须用明确的测试 Agent 与受控工具验证，不能随业务依赖一并跳过。

底层 runner 仍保留 `--technical-integration-seed` 供开发者诊断模型、会话、SSE、Trace 和反馈
来源关联；它通过公共 Workspace 导入/删除 API 使用无 MCP/subagents 的临时 Agent，但不属于
`container-live-test` 或正式验收入口，也不会在业务 Agent 失败时自动替代。技术种子通过不代表
HITL、MCP 或平台改进效果通过，不能据此关闭正式验收 GAP。
它只能通过独立的公共隔离入口执行，并要求显式真实模型授权和仓库外技术场景；场景顶层
`agent_id` 固定为 `runtime-technical-integration-package`：

```bash
REQUIRE_LIVE_RUNTIME=1 \
TECHNICAL_SCENARIO_FILE=/outside/technical-scenario.json \
make container-technical-live-smoke
```

Playground 取消、断线恢复和幂等重试可通过独立的非正式浏览器技术入口检查。该入口仍由公共
隔离 runner 重建当前工作树镜像，将仓库外场景冻结为私有快照，并使用真实 Chromium、真实 API
和真实 provider；场景 `agent_id` 固定为 `security-operations-expert`，但提示不得调用业务工具：

```bash
REQUIRE_LIVE_RUNTIME=1 \
BROWSER_TECHNICAL_SCENARIO_FILE=/outside/browser-technical-scenarios.json \
make ui-playground-technical-smoke
```

它只形成 AgentGov Playground 技术证据，不验证该业务 Agent 的 MCP 或专业输出效果，也不满足
双浏览器各三次、50 个不同输入、并发 10 或人工发布签字，不能替代
`ui-playground-cancel-smoke` / `container-release-candidate` 的正式门禁。

场景文件必须符合 [正式场景 schema](config/live_acceptance_scenario.schema.json)：顶层只含
`agent_id` 与 `scenarios`；每条场景至少提供 `scenario_id`、`purpose`、`input`、`source_ref`、
`reviewed_by`、带时区的 `reviewed_at`，改进场景还要提供反馈及允许变更路径。元数据只用于
可追溯和机器拒绝明显伪证据，不能代替人工复核本身。该入口覆盖 Session 创建、原生 SSE、
chat、canonical messages、run/reply/trace 关联和反馈提交。
完整 SSE `data` frame 只要非法就 fail-closed；`success`/`retry` 还必须形成 reply_id 一致的
`REPLY_START -> 非空 TEXT_BLOCK_DELTA -> REPLY_END` 有序链，且 SSE 终态 reply 集合与持久 run 精确一致。
本轮通过范围见 [Runtime 替换验收基线](docs/engineering/AgentGov_AgentScope_Runtime替换实施基线与验收.md#63-当前能力边界)。
Trace 按实际完成场景记账；单独的 `container-live-test` 不自动证明双浏览器各三次、HITL、重启恢复、候选发布闭环、
平台改进效果通过。完整验收未执行前不得宣称原子切换生产验收通过。

## Langfuse 与 OTel

AgentScope Runtime 通过标准 OTLP 导出 trace；AgentGov API 只负责按 `trace_id` 查询和
展示。两者共用 `LANGFUSE_ENABLED` 开关和同一对项目 key；Compose 将 key 以
`AGENTGOV_OTEL_PUBLIC_KEY` / `AGENTGOV_OTEL_SECRET_KEY` 注入 Runtime，仅用于构造摄取认证，
不是权限降级后的独立 key。Runtime 不接收 Langfuse 登录、盐、加密或存储密码，API 不接收模型或 MCP secret。

一 run 对应一 Trace，正文在 Runtime 导出前转换为精确 UTF-8 长度与 SHA-256，仅保留
受控语义属性。`trace_status=complete` 还要求轨迹与持久化回执、reply、team、tool 和确认
事实完整对账；业务 terminal 不等于观测完整。详见
[AgentScope 与 Langfuse 观测契约及验收](docs/engineering/AgentScope与Langfuse观测契约及验收.md)。

首次自托管时，从 `docker/.env.example` 复制一份私有 `docker/.env`，填写管理员邮箱，将
`LANGFUSE_ENABLED` 改为 `true`，再运行 `make langfuse-env` 生成缺失的 Langfuse 凭据；其他
API、Runtime、模型和 MCP 必填项仍需自行配置。
初始化只处理空值和 `replace-with-*` 模板值，不轮换已有值；检测到已有数据且缺凭据时拒绝生成。
文件修改前保存权限为 `0600` 的同目录 `.env.bak-*` 备份。然后运行：

```bash
make all-up
REQUIRE_LIVE_RUNTIME=1 \
REAL_ACCEPTANCE_AGENT_ID=security-operations-expert \
REAL_SCENARIO_FILE=/outside/reviewed-scenarios.json \
make langfuse-smoke
```

`langfuse-smoke` 触发真实 AgentScope run，并验证同一 trace 下的
`agentgov.run` 根 observation、`invoke_agent` 和 `chat` 子 observation，以及核心
run/session/reply/version 关联属性。它从外部复核文件选择真实 `success` 场景；公开 API 只返回
授权后的 trace 引用与完整性状态，smoke 在隔离进程内用私有查询凭据读取严格正向投影，不把
observation payload 暴露给浏览器。安全出口会将 AgentScope span 名归一化，不携带模型名、
Agent 名或内容。

本地 Langfuse web 与 worker 默认成对锁定为 `3.225.7` 的多架构 manifest digest，四项存储
依赖也锁定到本轮已验证的 manifest digest，避免不同部署时间解析到不同实现。若通过私有 env 覆盖镜像，必须同时覆盖 web/worker，
并在升级前完成数据冷备、迁移检查和真实摄取/查询复验；v3 到 v4 属于独立的大版本迁移，
不能只改镜像标签。

示例只保留 12 项 Langfuse 输入：统一开关、一对项目 key、管理员邮箱/密码、盐、加密 key、
登录 secret 和四项存储密码。其余参数使用默认值，需要自定义时才加入私有 env：

| 配置 | 默认或派生规则 |
| --- | --- |
| `LANGFUSE_BASE_URL` | 容器内 `http://langfuse-web:3000`；API 查询与 Runtime 摄取同源 |
| `LANGFUSE_HOST_PORT` / `LANGFUSE_BIND_IP` | `50402` / `127.0.0.1`；公开绑定还需 `LANGFUSE_ALLOW_PUBLIC_BIND=1` |
| `LANGFUSE_NEXTAUTH_URL` / `FRONTEND_LANGFUSE_URL` | 登录 URL 由端口派生；浏览器入口默认沿用登录 URL，反向代理或远程访问时可分别指定 |
| `LANGFUSE_INIT_ORG_ID` / `LANGFUSE_INIT_PROJECT_ID` | `agent-gov`；名称默认 `AgentGov`，前端 Trace 链接同步使用项目 ID |
| `LANGFUSE_INIT_USER_NAME` / `LANGFUSE_MINIO_ROOT_USER` | `admin` / `minio`；已有非默认身份必须保留 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS` | 从内部 Langfuse URL 和同一对项目 key 自动派生，含 v4 摄取标记；自定义 Collector 时可显式提供 |
| `OTEL_SERVICE_NAME` / `OTEL_RESOURCE_ATTRIBUTES` | `agent-gov-agentscope-runtime` / `deployment.environment.name=local` |

已有部署不要重新生成盐、加密 key 或数据库密码。可先用
`.venv/bin/python scripts/initialize_langfuse_env.py --env-file docker/.env --compact --dry-run`
检查，再去掉 `--dry-run` 精简等于默认或派生值的条目；自定义值和原有凭据按原文保留。
旧 `LANGFUSE_INIT_PROJECT_PUBLIC_KEY/SECRET_KEY` 不再是配置入口，项目初始化直接复用主 key；
若旧配置中的两对身份不一致，工具会拒绝精简，必须先确认正确项目身份。

接入已有外部 Langfuse 只需开关、地址和项目 key；使用 `make up` 启动核心服务，
无需本地 Langfuse 管理员、盐或存储密码，也不要运行 `make langfuse-env` / `make all-up`。
关闭观测使用 `LANGFUSE_ENABLED=false`，即使仍有 OTLP 配置也不会初始化 Runtime exporter。
宿主机 API 的 `docker/.env.local-debug` 仍只负责查询；独立 Runtime 要显式设置同一开关、
内部摄取配置或标准 OTLP endpoint/header。`/health` 只报告查询开关与凭据是否配置，
不再返回未被 API 消费的 `langfuse_otel_endpoint_configured` / `langfuse_otel_signals`。

## 反馈与版本治理

每次受管运行都有独立 `run_id`。反馈信号引用该标识，随后可进入改进事项的反馈整理、
归因、优化执行、回归测试和发布流程。主要入口包括：

- `/api/feedback-signals`、`/api/feedback-cases`
- `/api/improvements`
- `/api/improvements/{improvement_id}/attribution/generate`
- `/api/improvements/{improvement_id}/optimization-plan/generate`
- `/api/improvements/{improvement_id}/execution/apply`
- `/api/improvements/{improvement_id}/regression-test-design/generate`
- `/api/agent-change-sets/{change_set_id}/publish`
- `/api/agent-runs/{run_id}/trace`

接口字段、状态码和请求示例以运行中的 `/openapi.json` 为唯一真相源。

## 部署

```bash
scripts/deploy_agent_gov_to_host
# 或
DEPLOY_REF=<已提交且本机可解析的commit或tag> scripts/deploy_agent_gov_to_host 172.16.112.232
```

远程脚本默认部署 `origin/master`；它拒绝运行脚本与 `DEPLOY_REF` 中的脚本内容不一致，因而不会
用新脚本的镜像假设部署旧 Compose，也不支持直接搬运未提交工作树。当前工作树的本机重建应使用
`make build` 与 `make all-up COMPOSE_UP_FLAGS=--force-recreate`。
普通部署脚本先校验本机与远端 Docker 架构一致，再用临时只读检查器校验远端 Runtime DB epoch，通过后才允许 rsync 覆盖远端
源码；它在停服前还会校验必需配置和 Compose 契约，随后加载
`agent-gov-agentscope-runtime`、`agent-gov-api`、`agent-gov-ui` 三个镜像，启动可选
Langfuse profile，并通过 `/health/ready` 验收。私有 `docker/.env` 会被保留。空卷、精确
`agentscope-runtime-v3` 或可迁移的精确 v1/v2 才允许普通部署；迁移重建 Session intent 与 run 表，
使用请求指纹并迁入 chat operation 关联。v1 会从 `session_name` 计算指纹后删除正文副本，v2 使用未知历史指纹标记；
并且只在旧 `agent_release_operations` 表结构、索引和外键均
精确且表为空时删除该表。非空旧操作记录或结构漂移会拒绝迁移；发现旧 Claude/未知 schema 时会在停服前 fail closed，
绝不自动清空或静默丢弃。

同一启动锁内还执行一次 HITL 隐私迁移：先把历史 tool call payload 收敛为指纹，再启用
SQLite `secure_delete`、截断 WAL、`VACUUM` 并再次截断 WAL；只有逻辑迁移和物理净化都成功，
才在独立事务写入 `agentscope-hitl-fingerprint-v1` marker。任一步失败都不写 marker，后续启动可安全
重试；未知 marker 或不符合精确契约的数据仍 fail closed。

如果操作者明确选择放弃旧数据、从空卷重新部署，应先停用本项目服务，核对解析后的全部数据挂载、
目录与占用容器，再将已确认的旧项目卷移出活动路径，创建空的 Runtime root。私有 env 与外部
MCP/模型服务不随数据卷重置；新栈仍走 `make build` 和 `make all-up COMPOSE_UP_FLAGS=--force-recreate`。
这是重新初始化，不是旧数据迁移，也不能作为备份恢复演练通过的证据。新栈开始写入后不能将旧卷
回挂到新 binary；旧卷残留的处置须单独核定，不能执行跨项目清理。

旧 epoch 切换目前只开放只读检查：

```bash
make cutover-inspect
```

`maintenance-down`、`prepare`、`execute`、`finalize`、`recover-finalize` 和 `restore` 均已统一
fail closed，Make 不提供这些目标。原因是高权限冷备/旧栈演练、整根切换的 crash-resume、跨入口
互斥及 root filesystem custody 尚未形成可证明闭环；内部 helper 构造器同样拒绝执行。需要切换时
只能走经独立审批的人工维护方案，先用普通 `make down` 完整停服并保留原目录；不得递归清理旧卷，
也不能把普通重建结果声明为原子切换、冷备或恢复验收通过。
