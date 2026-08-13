# AgentGov 下一阶段 P0 准入收口实施方案

> 文档状态：实施契约；Phase 6/7 已收口 Workspace 与 per-Agent lane，P0-MCP 仍待实施。
>
> 工程阶段说明：P0 是下一轮研发准入阶段，不是四阶段改进治理工作台的用户阶段。
>
> 上游入口：[AgentGov 下一阶段实施方案索引](../AgentGov下一阶段实施方案索引.md)。
>
> 交付边界：当前已发布版本仍为 `3.0.3`；Phase 7 是已验证但未发布的 v3.1 工作树能力，且不证明
> P0-MCP 或 evaluator-owned 发布测评已经完成。

## 1. 目标与退出结果

P0 原始问题是“平台主门已绿，但旗舰安全 Agent 自测仍红且缺少独立阻断回执”的准入矛盾。
Phase 6/7 已收口 Workspace 自测、per-Agent exact-commit lane 与 Runtime 生命周期基础；P0-MCP
仍是独立阻断项，不能由前述通过证据替代。

退出时必须同时得到：

- `security-operations-expert` 在 P0 锁定的精确 Workspace commit 上完整 suite 零未分类失败；
  实际 leaf 数量只进入阶段回执，不是平台永久的用例数量契约；
- 业务 Agent Workspace 测试仍由对应 Agent 拥有，按 per-Agent exact-commit lane
  独立执行；`tests/quality_policy.json` 只登记平台 runner/隔离/回执的通用契约和
  阶段 lane，不将该 Workspace 的 pytest leaf 并入根静态 collection；
- 模拟 MCP 平台验收在独立容器 lane 中通过并生成无敏感信息的准入回执；
- P1 真实 Agent 行为测评拥有单独的容器验收 lane，不与 P0 静态测试或 P0-MCP 混淆；
- Runtime run/session/status 和事实投影边界形成已批准裁决；
- P1、P2A、P2B 分别绑定 AGV 用例、质量 owner 和验收入口。

## 2. 实际问题与边界

### 2.1 事实依据

- 当前仓库治理硬门、类型检查和主流程测试通过；
- 2026-08-05 初始审计时，内置安全 Agent Workspace 首轮完整独立测试为 15 通过、14 失败；
  该历史缺口已由 Phase 6 的 exact-commit Workspace lane 收口；
- 根质量策略 collection 只覆盖根 `tests` 与 Governor Workspace 测试；这是测试内容
  所有权边界，不是应通过收集业务 Agent Workspace leaf 关闭的缺口；
- 初始真实缺口是没有把“指定 Agent + 精确 commit + 完整 Workspace suite + 隔离执行
  + 回执”作为可重复的 P0 阻断 lane 验收；Phase 6/7 已建立该入口；
- 历史 14 个失败由 6 类高风险执行意图未 deny、3 个畸形输入契约、1 个审计路径错误和 4 个陈旧配置
  断言组成；其分类保留为阶段审计证据，不再表示当前待关闭问题；
- 当前没有“真实 Runtime 加载模拟 MCP → Agent 调用 → AgentGov 投影原生 tool facts”的
  阶段回执；该 GAP 与上述 14 个静态失败相互独立；
- 原多 Runtime 草案曾允许历史 run 恢复为 `running`；本轮目标方案已同步为一次 run
  终态不可重开、恢复会话必须创建新 run，P0 在下文冻结同一语义。

2026-08-13 最终文档冻结候选的 collect-only 基数为：质量策略 manifest `2225` 个 pytest leaf，
`main-full` 选择 `2220` 个 leaf，host no-Docker acceptance `402` 个 leaf，main-flow backend
`694` 个 leaf（`640` 个精确 selector）。七个公共入口的历史功能轮曾在同一候选 tree、同一份完整
Compose env 上全部成功，但该候选已被后续 terminal authority 修复超越，不能充当本文冻结后 exact
tree 的最终提交回执；本文冻结后仍须重跑串行全量和七个公共入口。P0-MCP 与 evaluator-owned 发布
测评保持未实现，根 `VERSION` 与已发布 tag 仍为 `3.0.3`。

### 2.2 本阶段做

- 已对安全 Workspace 测试完成 KEEP/REFACTOR/GAP 分类与修复；
- 已保持安全 Workspace 测试的 per-Agent exact-commit 所有权，并为平台执行器、隔离和回执
  建立可重复阻断 lane；
- 下一项使用固定版本的模拟 HTTP 服务与原始 OpenAPI MCP 转换器，建立隔离、只读、可复现的
  P0-MCP 平台验收；
- 已固定 Runtime 生命周期、部署选择和事实分层裁决；
- 已建立后续阶段的测试、AGV 与环境验收映射。

### 2.3 本阶段不做

- 不新增安全领域案例、评分器、Scorecard 或 UI；
- 不把模拟 MCP 当作生产 MCP、安全认证、真实研判或业务 Agent 开发成果；
- 不迁移 `sdk_session_id`，不接第二 Runtime；
- 不新增 Governor 学习表、候选或启用动作；
- 不修改业务 Agent 运行卷，不回灌已有 live Workspace；
- 不通过删除安全断言、扩大权限或跳过测试获得绿灯。

主要替代方案“允许 P1 边实现边修基线”不采用，因为它会使新增领域行为与历史漂移混在同一
红绿循环中，无法判断测评失败来自 Agent、测试协议还是基础配置。

## 3. 治理对象与责任

| 对象 | 权威来源 | P0 动作 |
| --- | --- | --- |
| 内置安全 Agent 身份与能力 | `agent.yaml`、`CLAUDE.md`、Workspace 原生配置 | 只按当前权威内容裁决陈测 |
| 权限与安全行为 | `.claude/settings.json`、rules、hooks | 高风险执行意图与畸形输入继续 fail-closed |
| Workspace 测试 | `tests/test_*.py` | 修复为行为契约，不断言已经退出的内部形态 |
| 测试组合 | Workspace Git + `tests/quality_policy.json` | Workspace 保有 leaf 所有权；策略只登记平台通用契约与阶段 lane |
| P0-MCP 测试夹具 | 固定上游 commit、过滤 OpenAPI 和容器回执 | 只验 AgentGov MCP 工具闭环，不承载领域能力 |
| Runtime 生命周期 | 集中状态机与已批准阶段裁决 | 一次 run 不重开，session 可由新 run 恢复 |
| 环境选择 | settings、Compose 与 Runtime/env 治理 | 只选择一份完整 env，不改变现有模式 |

## 4. 工作包

### 4.1 P0-W1：安全 Workspace 测试同步

基准运行的 14 个失败按如下证据逐项处理：

| 失败组 | 数量 | 当前证据 | 处置 | 成功标准 |
| --- | ---: | --- | --- | --- |
| 高风险执行意图未 deny | 6 | 破坏性文件系统操作、主机或服务中断、编排工作负载归零、编排工作负载重启、容器环境全局清理、外部远程访问 | `KEEP` / 修复实现 | 六类风险均返回 Claude hook 可识别的结构化 deny；文档和测试说明不保存可执行样本 |
| 非 JSON 输入 | 1 | hook 已结构化 deny，但测试只接受历史 exit code `2` | `REFACTOR` 测试 | 断言当前结构化 decision，不把进程码作为唯一安全契约 |
| JSON 顶层数组 | 1 | `[]` 触发 `AttributeError` | `KEEP` / 修复实现 | 任意错误顶层类型都结构化 deny，不抛未处理异常 |
| Bash 缺少 command | 1 | 当前 exit `0` 且无输出 | `KEEP` / 修复实现 | 必填字段缺失时结构化 deny |
| 审计 fallback | 1 | 回退到 `/data/transcripts` 后发生权限失败 | `REFACTOR` 实现 | 显式 data dir 优先，否则只派生到批准的 runtime data |
| 原生配置与身份陈测 | 4 | 仍断言旧 ask 权限、旧相对输出路径、旧 CLAUDE 文案和 manifest identity 缺失 | `REFACTOR` 测试 | 改为断言当前只读 ask、绝对 runtime 输出路径、原生规则和 `agent.yaml.agent.id` 权威 |

六类高风险执行意图和两个真实畸形输入缺陷不能通过删除断言、扩大 allowlist 或允许异常退出收口。
非 JSON 的历史 exit-code 断言及四个配置/身份断言也不能继续作为当前契约。所有 hook 用例统一
验证 Claude 原生结构化 decision 语义，退出码只作为进程诊断证据。

另保留两项不计入上述 14 个失败的分类：

- MCP 工具权限转交继续 `KEEP`，保持 Claude 原生权限流程，后端不按 Agent ID 接管；
- P1 真实 Agent 语义行为继续标记为 `GAP`，由 `agentgov_testkit` 补充，不伪装成 P0 静态测试。

修改对象只限仓库运行卷初始化源中的单个
`docker/runtime-bootstrap/business-agents/security-operations-expert/workspace/`。不编辑 `version/`、
运行态 SQLite、`.env*` 或任何 live Workspace。

### 4.2 P0-MCP：模拟 MCP 平台验收

按
[P0 模拟 MCP 平台验收实施方案](./AgentGov下一阶段P0模拟MCP平台验收实施方案.md)
建立单独的阻断工作包：

- 固定 `openapi-mcp-server` commit
  `fb3d79c05cfaf70067a170af42a04503c619b688`；
- 固定 `mock_service` commit `8ff758a9d4867325b83ef5dbff6025288aed62f7`；
- 平台维护的过滤 OpenAPI 只暴露 alerts/assets 两个 GET operation；
- 在隔离 Compose project、临时 Runtime 根和内部网络中分别完成 MCP 直接协议验收与
  AgentGov live 验收；
- 回执固定 Agent commit、suite digest、上游 commit、镜像 digest、OpenAPI SHA256、
  tool list、run/session 引用和 cleanup 结果；
- 原始上游缺少认证和生产 allowlist，因此只允许合成数据，不得声明认证或生产安全通过。

P0-MCP 不修改 `security-operations-expert` 的业务职责，不增加 P1 测评案例，也不创建
`ImprovementItem`、产品 API、UI 或持久化 schema。14 个 Workspace 历史静态失败已由
Phase 6/7 收口；其通过证据不能豁免仍待完成的 P0-MCP，二者继续保留独立门证。

### 4.3 P0-W2：per-Agent exact-commit 质量 lane

- 安全 Workspace 的 pytest leaf 继续留在该 Agent Git，不加入根 collection，也不复制为
  根 `tests/`、数据库测试集或通用 Registry 正文；
- 平台提供通用 per-Agent exact-commit 执行入口，输入只由后端解析的 Agent、
  commit 和固定 `/usr/local/bin/python -I -P -m pytest -q --import-mode=importlib -p agentgov_testkit.pytest_plugin tests`
  组成，完整执行该 commit 的 `workspace/tests/`；`-I -P` 防止业务源码抢占平台 pytest，
  `--import-mode=importlib` 避免 pytest 把测试目录前插到 `sys.path`；
- worker 先校验精确 commit 的完整 Git tree，再生成确定性的沙箱源码投影；完整 tree SHA 绑定原始
  commit，`source_digest` 只绑定实际进入沙箱的文件。`.env`、local settings、私钥/凭据存储、
  secret/credential 目录，以及含字面量凭据的 `.mcp.json` 或 `.claude/settings.json` 不进入投影；
  live Workspace 与 per-Agent Git 仍逐字节保留这些私有运行资产。该规则不宣称对其余 Agent-owned
  源码完成了通用 DLP；
- worker 把投影写入仅由自身挂载的 Docker-managed named volume `/agent-test-runs`，API 只挂载
  `/data`，不能修改该运行卷。sandbox 复用同一 volume identity，只把当前 run 的 workspace
  subpath 以 `/workspace:ro` 挂载；不向 Docker daemon 提交可被别名重解析的宿主路径。`/output` 与 `/tmp` 使用限额
  tmpfs，报告经平台插件的稳定单行 stdout envelope 发出，由 worker tail 读取、严格限长校验并从
  诊断输出剔除，不存在第二个可写 host bind；报告仍是 `agent_owned_unverified`；
- 公共 `make container-workspace-pytest-test` 基于当前工作树构建 API、worker 与 sandbox，使用独立
  临时 Runtime 根、Compose project/labels 和端口；入口先形成候选 Git tree 与
  materialized snapshot，再用同一候选构建全部镜像。`docker/runtime-bootstrap` 已构建进 API 镜像，
  Compose 不为 `/app/docker/runtime-bootstrap` 配置 host bind；完成后清理且不读写现有 live volume；父
  orchestrator 收到 SIGINT/SIGTERM 时先终止并 reap 当前子进程组，再继续清理 exact worker、
  sandbox、named volume、project/network 与临时根，分别以 130/143 非零退出；
- worker 在容器启动前及执行器完成 cleanup 后，从同一实际 volume 目录重算摘要；回执以
  `not_observed`、`pre_only`、`stable`、`changed` 区分真实观察。只有 queued、pre、post 三个
  `source_digest` 一致且 `source_observation=stable` 的通过回执具备发布资格；未执行、只完成前置
  观察或发现变化时不能伪填稳定证据；
- typed receipt 固定为 `assurance_level=execution_provenance`；
  `receipt.result.workspace_report_authority=agent_owned_unverified` 声明 pytest report/items/invocations
  经 strict schema 有界读取且不能注入 status、worker、
  score 或 approval。它只证明精确提交在上述固定隔离环境中按固定命令执行及 Docker 观察结果，
  不等于独立证明业务正确性、能力质量或真实 Agent 行为；
- `tests/quality_policy.json` 中登记的是平台通用契约测试与阶段 lane 元数据：
  - owner：平台 runner/隔离由 `agent-lifecycle` 负责，本次安全基线由
    `security-response` 接收；
  - capabilities：`agent-lifecycle`、`security-boundaries`；
  - lane：P0 阶段显式 exact-commit acceptance，不并入根 `main-full` collection；
  - resources：`hermetic`、`git`、`process`、`docker`、`serial`；
  - lifecycle：现有安全契约为 `KEEP`，陈测同步项为 `REFACTOR`；
- P1 的真实 Agent 语义用例属于独立、尚未实现的 `p1-live` lane，公共入口登记为
  `make container-agent-evaluation-test`，不默认进入离线 `main-full`；
- P0-MCP 进入独立 `container-security-mcp-test` lane，作为阶段验收但不进入离线
  `main-full`；
- `make main-flow-test` 只绑定会改变反馈闭环/API/UI 的场景，不用静态 hook 数量充当主流程证据。

### 4.4 P0-W3：Runtime 裁决冻结

实施前固定以下规则：

1. 当前 P0/P2A 实施切片中，一个部署同时只启用一个受管 Runtime，客户端请求不能覆盖；
   这是当前阶段限制，不是长期平台边界。内部身份和证据契约必须保留 backend-owned
   `BusinessAgentVersion -> RuntimeBinding` seam，以便未来同一部署治理不同 Runtime 的
   `BusinessAgentVersion`；
2. `RuntimeSessionRef` 表达可恢复的原生会话；`run_id` 表达一次不可变执行尝试。
3. `running -> completed | failed | cancelled | interrupted`；所有终态均不可重开。
4. 恢复同一 Runtime session 时创建新 `run_id`，并通过后端关联保留前序 run。
5. Runtime 层统一使用 `completed`；业务测试或发布领域现有 `passed/succeeded` 暂时保留在各自
   边界，不混入 Runtime 状态机。
6. 事实分为：
   - Runtime/SDK 原生事实；
   - AgentGov canonical 投影；
   - UI、Trace、Langfuse 边界展示。
7. 不能无损映射的原生事实保留原始 payload 和 coverage，不强行归一化。

本轮已将该裁决同步到多 Runtime 目标方案；后续若调整恢复语义，必须先通过同一 ADR 同步修改
状态机、目标方案、实现计划和非法转移测试，避免再次形成两个活跃真相源。

### 4.5 P0-W4：阶段验收绑定

| 后续阶段 | 主要 AGV 锚点 | 质量 owner | 主要验收 |
| --- | --- | --- | --- |
| P1 安全测评 | AGV-002、009、028、035、043、046、051 | `improvement-governance` + `security-response` | Workspace 回归、独立发布基准、配对测评、改进闭环、精确发布 |
| P2A Runtime | AGV-002、014、040、050 | `runtime-platform` + `integrations` | contract、SSE/resume/HITL、真实容器 |
| P2B Governor | AGV-006、009、010、012、019 | `improvement-governance` | 不可变证据、隔离评估、shadow 不生效 |

P2B 可以为 AGV-045 提供方法候选、评估证据和 scope 基础，但 AGV-045 要求的“能力包跨
Agent 应用并逐 Agent 验证”不属于 P2B shadow 切片，不得因 P2B 退出而升级该 AGV 状态。

P1 只为 AGV-051 提供单 benchmark、单 Agent 详情入口和发布版本比较的局部证据；独立测评
中心、完整人工 review 和 `OnlineOutcome` 未交付，因此 P1 退出后 AGV-051 仍保持 `gap`。
AGV-002、009、028、043 是否升级必须按各自完整成功标准逐项验证，P0/P1 方案不预授权改变状态；
AGV-035 与 AGV-046 保持 `current`。

## 5. 架构与数据边界

- Markdown 不受代码行数阈值影响；P0 的代码修改不得向接近 800 行的 Runtime/Governor 文件添加新职责。
- 状态裁决必须落到集中状态集合、完整转移表和统一 helper，不能散落字符串判断。
- 质量策略是测试分类单一入口，不新增第二份 security-only manifest。
- Workspace 初始化源只服务空卷出生；已有 Agent Workspace 整体跳过，不逐文件回灌。
- P0-MCP 使用独立临时 Runtime 根、Compose project 和 fixture network，不挂载
  `${HOME}/volume-agent-gov`，完成后必须清除容器、网络、卷和临时目录。
- per-Agent exact-commit 真实验收同样隔离于产品运行卷，但使用独立的
  `make container-workspace-pytest-test` 入口和 agent-test Compose profile，不与 P0-MCP lane 混跑。
- exact-commit lane 已引入 SQLite migration `0053`，为历史 `agent_test_runs` 增加
  worker fence、source/tree、容器引用与 nullable typed receipt；旧行不回填、不获得发布资格。
  migration `0054` 为 Workspace 导入审计增加 suite status 与完整 diagnostics，使候选检查先于激活，
  并让 accepted audit 与注册表发布处于同一一致性边界；测试检查异常不得再产生“请求失败但导入已生效、
  且无审计”的假失败。migration `0055` 进一步持久化导入/恢复激活意图、原状态和候选身份；prepared
  后 graph identity 不可变，进程崩溃后由同一状态机在剔除继承 `GIT_*`、replace ref、hook、lazy fetch
  影响的 Git authority 下，按 canonical commit/tree/parent、精确 HEAD、index 语义与工作区指纹幂等完成或补偿，
  无法证明一致时保留 runtime fence。受管命令还必须显式固定 `--git-dir` / `--work-tree`，拒绝
  repository-local 外部命令驱动、`core.worktree`、object alternates、非规范 `commondir`、grafts 和
  partial clone/promisor；commit tree/parents 从 raw object header 解析，operation refs/HEAD 只接受
  direct-ref canonical topology。migration `0058` 幂等重装 0055/0057 authority triggers，使已应用过
  旧版迁移的持久卷也获得后续增强的状态转移、终态不可变/禁止删除与终态证据约束。
  手工 run 的公开请求收紧为必填完整 40 位 commit，OpenAPI、官方生成前端类型和发布工作台同步
  展示后端 receipt eligibility。产品持久卷根布局不变，但 worker 的一次性运行根必须是 API 不挂载的
  Docker-managed named volume，并以真实 volume identity 与 mountinfo 证明其和 data 根不重叠；sandbox
  只复用同一 volume 的当前 run workspace subpath，不把宿主路径作为 authority。这是平台级长期门证契约，
  不是仅供本次安全 Agent 基线验收的临时旁路。

## 6. 测试同步矩阵

| 变更 | 旧测试处置 | 新增测试 | 深度 |
| --- | --- | --- | --- |
| 安全 hook 行为修复 | `KEEP` / `REFACTOR` | 畸形、越权和边界输入 | 正常 + fail-closed |
| 旧身份/settings 契约退出 | `REFACTOR` 或有证据的 `DELETE-CANDIDATE` | 当前 manifest/原生配置契约 | 不断言私有实现 |
| per-Agent exact-commit lane | Workspace leaf `KEEP` | 完整 tree、敏感投影、独立运行根、实际 bind 四态观察、隔离、回执和 policy 契约 | 根 collection 不得收集业务 Agent leaf；API 不得拥有 sandbox 源修改权 |
| 手工 run / 发布门证公开契约 | 旧的省略 commit、status-only UI 断言 `REFACTOR` | 完整 commit、nullable typed receipt、stable 发布门、历史不放行、retry blocker | OpenAPI + 生成类型 + UI build |
| Workspace 导入审计 | 警告列表旧投影 `DELETE` | ready/warning/invalid 完整 diagnostics、检查不可用隔离、激活与审计原子性、部分失败 | API + migration + UI 三态 |
| Workspace Git authority | 继承 Git config/env 的实现细节测试 `REFACTOR` | 外部驱动不执行、对象图不跨 Agent、raw commit/ref/HEAD topology、0058 旧卷刷新 | core/operator 双路径 + migration 幂等负向测试 |
| 模拟 MCP 平台验收 | 无 | 两工具协议、Agent activity、稳定数据、越界与清理 | 隔离真实容器 |
| Runtime 状态裁决 | 现有状态机测试 `KEEP` | `interrupted` 不可重开、新 run 恢复 | 非法转移负向测试 |
| 真实 Agent 语义 | 无 | P1 `GAP` | 不在 P0 伪造 |

## 7. 验证与退出门

按顺序执行：

1. 安全 Workspace 目标 pytest 全部通过；
2. `make runtime-bootstrap-scan`，确认初始化源无敏感文件、symlink 或越界内容；
3. `.venv/bin/python scripts/check_test_quality_policy.py --manifest-only`，确认根 collection 仍只收集平台与
   Governor 测试，且 per-Agent lane 的平台契约完整；
4. 公共 `make container-workspace-pytest-test`，确认当前工作树的 `docker/runtime-bootstrap` 由
   Dockerfile 构建进候选 API 镜像，Compose 配置与运行中容器均不存在
   `/app/docker/runtime-bootstrap` 宿主机 bind 或 `RUNTIME_BOOTSTRAP_HOST_DIR`；同时确认
   exact-commit、敏感文件不进入投影、同一 named volume 目录的 pre/post 观察为 `stable`、
   hostile isolation、Docker stdout report envelope 回收和零 container/temp 残留；
5. 公共 `make container-security-mcp-test`，确认 MCP 直接协议、AgentGov live 证据和 cleanup 回执；
6. 状态机与质量策略目标 pytest；
7. `make codex-guard`；
8. `make typecheck`；
9. `make main-flow-test`；
10. 阶段提交前串行 `make test`。

退出失败条件：

- 任一已分类高风险执行意图或畸形 hook 输入被放行；
- 安全 Workspace 测试被复制或收集进根 collection，或 exact-commit lane 不能生成完整
  回执；
- API 能修改 worker 的运行根、敏感运行配置进入 sandbox 投影，或未观察 post source 却生成
  `stable` 回执；
- Workspace 导入返回失败但注册表或 Git 已生效，或 accepted import 缺少完整审计；
- P0-MCP 只能靠 mock Agent 活动、浮动上游、宿主机端口、真实运行卷或跳过清理通过；
- 因存在临时 token 变量而宣称上游 MCP 认证已验证；
- 通过 local-debug 结果宣称容器通过；
- Runtime 生命周期仍存在两套合法转移表；
- 通过修改真实 env、运行卷或私有数据才能测试通过。

## 8. 回退与修订条件

- P0 只同步当前安全契约；若安全 Agent 的业务职责或权限模型改变，应先修订其 Workspace 权威文件，
  再重新分类测试。
- P0-MCP 只服务平台夹具；出现真实认证、写操作、生产数据或防御性状态回放需求时停止扩展该夹具，
  转入 P3 独立准入。
- 质量策略分类可随真实资源消耗调整，但不得移除 blocking lane 而不提供等价门。
- 历史 `29/29` 只用于识别 2026-08-05 初始评审范围；后续 Workspace 用例增删按新 commit
  的完整 suite 和 digest 验收，长期文档不维护固定数量断言。
- 若上游 Runtime SDK 明确提供可审计的同一 run 原地恢复语义，需另立 ADR 重新评审；在此之前保持
  “新 run、同 session”。
- 本阶段不 bump `VERSION`、不创建 tag；发布点由用户另行确认。
