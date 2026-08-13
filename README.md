# 智能体治理平台 AgentGov

AgentGov 把业务 Agent 的真实运行、反馈、归因、优化、测试、发布与回滚连接成一条可追溯的治理闭环。
它面向业务 Agent 开发者、评测方和治理操作人员；外部业务系统继续拥有最终用户界面、业务权限和生产审批。

> 当前发布版本：`3.0.3`。当前 v3.1 工作树的 Phase 7 已完成同候选验收，但尚未随当前发布版本发布。
> 实时进度以 [Project State](./.planning/STATE.md) 为准。

[五分钟启动](#五分钟启动) · [能力状态](#能力状态) · [核心工作流](#核心工作流) ·
[集成指南](./docs/AgentGov集成指南.md) · [完整文档](./docs/README.md)

## 为什么 AgentGov

业务 Agent 的一次修改，不应只留下新的 prompt 或一条“测试通过”记录。AgentGov 关注的是完整证据链：

```text
Business Agent Workspace
  -> SDK-native run / session / trace
  -> feedback / ImprovementItem
  -> attribution / optimization
  -> Git-backed change set
  -> regression test / evaluation
  -> release / rollback
```

这条链路解决三个核心问题：

- **变了什么**：变更与精确 Agent、Workspace Git commit、Diff 和版本关联。
- **为什么要变**：反馈、失败证据、归因结论和优化决策可以回溯。
- **凭什么发布**：测试、测评、安全门和人工决策各自留证，不用单一分数替代业务正确性。

产品长期目标、治理对象和成功度量见
[项目目标、愿景与使命](./docs/项目目标愿景使命.md)。

## 能力状态

“已验证”不自动等于“已发布”。本表同时标明能力成熟度与它所在的交付边界。

| 状态 | 能力 | 边界 |
| --- | --- | --- |
| 已验证 | SDK-native Agent Runtime、会话投影、运行取消，以及 Claude 原生工具、HITL、subagent 和 Trace 事实链 | Agent/SDK 是运行事实源；后端只做 API、typed 投影和治理编排 |
| 已验证 | 四阶段改进治理：反馈整理 → 归因分析 → 优化执行 → 测试发布 | 状态推进是业务动作的副作用，不是独立按钮 |
| 已验证 | Git-backed change set、精确 Diff、审批、发布、恢复与回滚 | 旧 tar snapshot 与旧 `/api/agent-versions/main/*` 已退出主流程 |
| 已验证 | 业务 Agent Workspace 的导入、导出、精确版本与历史树恢复 | Workspace 和 per-Agent Git 是敏感运行资产 |
| 已验证 | 内置 `security-operations-expert` 的 Workspace 安全基线 | 它是可替换示例，不定义平台的行业边界 |
| 已验证（v3.1 工作树，未发布） | per-Agent exact-commit Workspace pytest 隔离执行 lane | Phase 7 已在同一候选上通过串行 `make test` 与全部适用公共容器门；该能力尚未随 `3.0.3` 发布，也不代表 P0-MCP 或独立发布测评完成 |
| 规划中 | P0-MCP 精确 capability 回执、独立 evaluator-owned 测评、第二 Runtime 边界验证、Governor shadow 学习 | 尚不能作为当前 API、UI 或发布能力宣传 |

完整当前事实见 [反馈闭环当前实现基线](./docs/反馈闭环当前实现基线.md)；阶段计划与缺口见
[下一阶段实施方案索引](./docs/AgentGov下一阶段实施方案索引.md)。

## 五分钟启动

### 前置条件

- Docker Engine 与 Docker Compose plugin
- GNU Make
- `uv` 和 Python 3.11（用于本地工具、测试和文档治理）

### 1. 初始化

```bash
make setup
```

该命令首次创建 `docker/.env` 和 `.venv`。然后编辑 `docker/.env`，至少：

- 把示例 `API_KEY=change-me` 改成非默认值；
- 配置 `MODEL_PROVIDER_BACKEND` 和 `MODEL_PROVIDER_API_URL`；
- Anthropic-compatible 路径还必须配置 `MODEL_PROVIDER_API_KEY`；
- 本地或内网 vLLM 可按上游要求留空 key，URL 使用不带 `/v1` 的 base URL。

真实密钥、私有 endpoint 和运行态数据不得提交到仓库。

### 2. 构建并启动核心服务

```bash
make build
make up
```

`make up` 启动 API、前端、内部 LiteLLM sidecar 和专用测试 worker。Langfuse 是可选观测面，
不属于最小启动路径。

### 3. 验证

```bash
make smoke
```

| 入口 | 默认地址 | 含义 |
| --- | --- | --- |
| 前端 | <http://localhost:55173> | Playground 与治理调试界面 |
| API | <http://localhost:58080> | AgentGov 后端 |
| Swagger UI | <http://localhost:58080/docs> | 自托管 API 文档 |
| ReDoc | <http://localhost:58080/redoc> | 自托管 API 参考 |
| OpenAPI | <http://localhost:58080/openapi.json> | 字段与路由契约真相源 |
| Liveness | <http://localhost:58080/health/live> | 仅表示 API 进程存活 |
| Readiness | <http://localhost:58080/health/ready> | 模型 provider 不可用时返回 `503` |

停止核心服务：

```bash
make down
```

完整的 env 选择、Langfuse、健康诊断、持久化、可信主机部署和本机调试流程见
[部署与运行手册](./docs/engineering/部署与运行手册.md)。

## 第一次调用

推荐集成方使用 SDK-native SSE 入口；它不会先投影成旧 Chat 或 Responses 形状：

```bash
curl -N http://localhost:58080/api/agent-runtime/sdk-events \
  -H 'Authorization: Bearer <your-api-key>' \
  -H 'Content-Type: application/json' \
  -d '{
    "agent_id": "security-operations-expert",
    "message": "请说明你能提供哪些帮助"
  }'
```

随后可从 `/v1/conversations` 读取会话投影。完整认证、SSE 事件、HITL、会话恢复、Trace 和错误语义
见 [AgentGov 集成指南](./docs/AgentGov集成指南.md)。

`/v1/responses` 仍是活跃的过渡投影，但不是完整 OpenAI Responses 实现，也不是运行事实源。
`/api/chat*`、`/api/sessions*` 和 `/v1/chat/completions` 是 deprecated 兼容面，不应用于新集成。

## 核心工作流

### 1. 接入业务 Agent

普通业务 Agent 通过 Workspace 包接入。Workspace 中的 `CLAUDE.md`、`.claude/`、`.mcp.json`、
skills、hooks、subagents 和测试资产随同一 Git 版本治理；平台不把这些内容复制成第二套权威。

### 2. 运行并收集证据

Playground 和上层系统通过后端 Runtime API 完成会话。SDK/Agent 原生 session、message、tool、
HITL 与 subagent 事实保持权威，AgentGov 关联 run、feedback 和 Trace。

### 3. 四阶段改进

改进治理工作台只保留四个用户阶段：

1. 反馈整理；
2. 归因分析；
3. 优化执行；
4. 测试发布。

查看 Trace、Diff、测试详情等是辅助入口，不是额外流程阶段。UI 信息归属见
[四阶段改进治理工作台方案](./docs/AgentGov_四阶段改进治理工作台UI整改方案.md)。

### 4. 测试、发布与回滚

业务 Agent 自有 pytest 跟随 Workspace Git；平台记录精确 commit 上的执行证据。独立正式测评使用
evaluator-owned 协议和资产，不能由 Workspace 自测冒充。通过发布门后，change set 才进入 release；
历史发布可以恢复或回滚。

测试正文、执行通道和证据可信度见
[测试资产组合治理](./docs/engineering/测试资产组合治理.md) 与
[Workspace pytest 实现契约](./docs/engineering/业务AgentWorkspace原生pytest测试资产实现方案.md)。

## 架构边界

```text
Browser / External system
          |
          v
    FastAPI control plane  ----> SQLite governance records
          |
          +----> Claude Agent SDK / Claude Code agent
          |             |
          |             +----> per-Agent Workspace + native sessions
          |
          +----> Git-backed change sets / releases
          |
          +----> isolated test worker -> sandbox
          |
          +----> optional Langfuse
```

- **Agent/SDK 持有行为事实**：会话、消息、工具调用、HITL、subagent 和原生 Trace 不由后端重建平行副本。
- **后端保持薄层**：负责认证、公开 API、确定性编排、证据投影、状态机和审计。
- **Workspace 持有 Agent 资产**：prompt、skill、hook、MCP 配置和自有测试跟随 per-Agent Git。
- **外部系统持有业务责任**：用户、角色、租户、生产权限和高风险业务审批不归 AgentGov。
- **运行数据在仓库外**：Compose 默认使用 `${HOME}/volume-agent-gov`，SQLite 位于其
  `data/runtime.sqlite3`；不要提交运行卷。

更精确的 Workspace 生命周期、并发和恢复边界见
[Workspace 包工程契约](./docs/业务AgentWorkspace包导入与热加载产品工程方案.md)。

## 前端边界

当前前端是开发、调试和治理观察界面：

- 不接管 Claude Code CLI 进程；
- 不提供 Terminal；
- 所有 Agent 交互通过后端 Runtime API 完成；
- 不直接操作客户生产系统，不替代上层业务 UI、权限和人工责任。

## 文档导航

| 你要完成的任务 | 首选文档 |
| --- | --- |
| 理解产品目标与非目标 | [项目目标、愿景与使命](./docs/项目目标愿景使命.md) |
| 判断当前已经实现什么 | [反馈闭环当前实现基线](./docs/反馈闭环当前实现基线.md) |
| 启动、部署、诊断或本机调试 | [部署与运行手册](./docs/engineering/部署与运行手册.md) |
| 集成 Runtime、会话、HITL 或 Trace | [AgentGov 集成指南](./docs/AgentGov集成指南.md) |
| 导入、导出或恢复业务 Agent Workspace | [Workspace 包工程契约](./docs/业务AgentWorkspace包导入与热加载产品工程方案.md) |
| 设计测试与正式测评 | [测试资产组合治理](./docs/engineering/测试资产组合治理.md) |
| 查看当前里程碑与未完成门 | [Project State](./.planning/STATE.md) |
| 浏览全部活跃与归档文档 | [文档索引](./docs/README.md) |

文档中的状态词有严格含义：长期目标不代表当前实现，代码存在不代表真实容器验收通过，历史评审也不覆盖
OpenAPI、当前代码或最新验收回执。

## 安全与适用边界

- AgentGov 只用于用户拥有或明确授权环境中的防御性监测、研判、加固和响应。
- API key、模型 key、MCP header、数据库凭据、真实 endpoint、私有路径和运行日志不得进入仓库或公开材料。
- API 与普通业务 Agent 不挂载 Docker socket。专用测试 worker 是当前唯一受控例外；它只负责创建隔离
  sandbox，sandbox 本身无网络、非特权且不持有 socket。
- `docker/.env` 服务 Compose；`docker/.env.local-debug` 服务宿主机 Python/PyCharm；
  `frontend/.env.local` 只服务 Vite。三者不是 layered override。
- 本期不建设产品内的通用协作模型，也不接入外部研发协作平台；当前重点是把智能体开发与反馈优化闭环做强。
- UI、API 和 Langfuse 默认端口及示例凭据只适合开发环境。生产暴露前必须收紧绑定地址、凭据、TLS、
  网络边界、备份和恢复策略。

## 项目治理

- 版本唯一真相源是根 `VERSION`；不要从镜像 tag、前端常量或文档手工推导。
- 修改代码后先运行目标测试；涉及主流程运行 `make main-flow-test`，完整验证运行 `make test`。
- 真实 Compose 验收必须使用公开 Make 入口，例如 `make container-core-smoke`；
  Workspace pytest lane 使用 `make container-workspace-pytest-test`。这些入口会基于当前工作树重建和
  recreate 所需服务，本机测试不能替代容器回执。
- 仓库当前未提供 `LICENSE`、`CONTRIBUTING.md` 或 `SECURITY.md`；在维护者明确开源许可、
  贡献流程和私密漏洞报告渠道前，不应自行推定对应政策。

文档或治理配置变更还应运行：

```bash
make codex-guard
```
