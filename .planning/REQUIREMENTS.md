# Requirements: AgentGov v3.1 业务智能体测评与平台治理演进

**Defined:** 2026-08-05  
**Core Value:** 让每个业务 Agent 的能力变化都能从真实运行与失败证据出发，经独立测评和安全门验证后形成可发布、可回滚、可追溯的精确版本。

## v3.1 Requirements

### P0-W1：安全 Workspace 基线

- [x] **P0W-01**: 平台可在 P0 锁定的 `security-operations-expert` 精确 commit 上执行完整 Workspace suite，并得到零未分类失败；每次实际收集的 leaf 数量只记录在对应回执中，不成为永久数量契约。
- [x] **P0W-02**: 所有已分类危险 Bash（破坏性删除、关机、Kubernetes 扩缩/重启、Docker 清理、远程 SSH）都返回 Claude hook 可识别的结构化 deny。
- [x] **P0W-03**: 非 JSON hook 输入返回结构化 deny，测试不再把历史进程退出码作为唯一安全契约。
- [x] **P0W-04**: JSON 顶层不是 object 时返回结构化 deny，且不抛未处理异常。
- [x] **P0W-05**: Bash tool input 缺少必填 `command` 时返回结构化 deny。
- [x] **P0W-06**: 审计输出优先使用显式批准的数据目录，否则只派生到批准的 runtime data 路径，不回退到不可写 `/data/transcripts`。
- [x] **P0W-07**: Workspace 测试只断言当前权威的只读 ask、绝对 runtime 输出路径、Claude 原生规则与 `agent.yaml.agent.id`，不继续维护已退出的身份/配置陈测。
- [x] **P0W-08**: 本阶段只修改并扫描仓库运行卷初始化源中的单个内置 Workspace；live Workspace、`version/`、`.env*` 和 runtime SQLite 保持未修改。

### P0：per-Agent 隔离测试 lane

完成状态：LANE-01 至 LANE-08 已由 3 个 plans / 12 个 tasks 交付。文档冻结前功能收口轮已覆盖串行宿主机门与 core、agent-test、health、speech、ui-cancel、live、langfuse 七个公共入口；各入口分别保留 fresh receipt，不以单张回执代表全部。收尾提交仍以文档冻结后的 exact-tree `make test` 与七入口 durable final gate 为原子前置，且 Phase 7 不替代 Phase 8 P0-MCP 或独立业务能力测评。

- [x] **LANE-01**: 通用平台 runner 只接受后端解析的 Agent、精确 commit 和固定 pytest 命令，并完整执行该 commit 的 `workspace/tests/`。
- [x] **LANE-02**: Workspace suite 在非 root、无特权、allowlisted 最小 env、只读源码挂载、无可写 `/data`/live runtime root、无 Docker socket且网络按契约最小化的隔离环境中运行。
- [x] **LANE-03**: 负向验收证明测试进程不能读取继承 secret、越出目标 Workspace、写平台数据或借助宿主机能力逃逸隔离边界。
- [x] **LANE-04**: 每次运行生成无敏感信息、`assurance_level=execution_provenance` 的机器回执，绑定 Agent/commit、suite digest、source/image fingerprint、固定 invocation、Docker 观察结果、隔离摘要和 cleanup；Agent-owned report 明确为 unverified diagnostics。
- [x] **LANE-05**: 业务 Agent pytest leaf 不进入根静态 collection，也不复制到根 `tests/`、数据库测试正文或 Asset Registry。
- [x] **LANE-06**: `tests/quality_policy.json` 分别登记 P0 exact-commit、P0-MCP 与 P1 live lane 的 owner、capability、resource class 和 blocking 语义，不建立 security-only 第二 manifest。
- [x] **LANE-07**: Workspace 工程卫生门只接受当前 Agent commit 与当前 suite digest 的通过回执，并核对 worker/container；历史、错配或来源不明的 passed run 不能放行。该门是发布必要条件，不替代 Phase 10 evaluator-owned 独立测评与安全门。
- [x] **LANE-08**: Docker、隔离镜像或其他必需前置缺失时 lane 严格失败而非 skip；成功、失败和中断均清理明确解析出的临时资产。

### P0-MCP：精确两工具平台回执

- [ ] **MCP-01**: `openapi-mcp-server`、`mock_service` 与测试载体 Workspace 均固定到精确 commit/image digest，验收过程不消费分支名、`latest` 或运行时联网拉取源码。
- [ ] **MCP-02**: 平台维护的过滤 OpenAPI 及其 SHA256 只暴露 alerts/assets 两个 GET-only tool，其他 HTTP method、第三个 tool、resources 和 resource templates 均为空。
- [ ] **MCP-03**: fixture 使用独立 Compose project、临时 Runtime/MCP 数据和内部网络，不挂载 `${HOME}/volume-agent-gov`，MCP/mock/spec 不映射宿主机端口。
- [ ] **MCP-04**: 直接 MCP 协议验收可 initialize/list/call 两个精确 tool，并分别返回 3 条含稳定标记的 alerts/assets 合成记录。
- [ ] **MCP-05**: 固定 Agent commit 的真实 Claude Runtime run 同时调用两个精确 tool，参数均为 `count=3, seed=7`，并在 `agent_activity` 与最终回答中投影稳定标记。
- [ ] **MCP-06**: live run 不出现 Bash、文件写入、未批准工具、虚构 tool result 或敏感内容；每次失败/重试保留独立 `run_id` 和回执。
- [ ] **MCP-07**: 回执明确写出 `claude-code / streamable-http / tools / GET-read-only / no-auth-fixture` capability tuple、全部未覆盖 GAP、source/image/OpenAPI/Agent/suite digest 与 run/session/trace 引用，且不包含 token、header、私有 endpoint 或完整 env。
- [ ] **MCP-08**: 公共 `make container-security-mcp-test` 基于当前 AgentGov 工作树重建并 force-recreate，直接协议、AgentGov live 和 teardown 任一失败都阻断；cleanup 后无遗留容器、网络、卷、端口或临时根。

### P0：状态语义与准入收口

- [ ] **ADM-01**: Runtime run 生命周期由集中状态集合、完整 `running -> completed | failed | cancelled | interrupted` 转移表和统一 helper 管理。
- [ ] **ADM-02**: `completed`、`failed`、`cancelled` 与 `interrupted` 都是不可重开的终态，非法转移被稳定拒绝。
- [ ] **ADM-03**: 恢复同一 Runtime 原生 session 时创建新 `run_id`，并保留到前序 run 的后端血缘。
- [ ] **ADM-04**: 运行证据区分 Runtime/SDK 原生事实、AgentGov canonical 投影和 UI/Trace/Langfuse 边界；无法无损投影的事实保留 raw payload 与 coverage。
- [ ] **ADM-05**: 当前部署只启用一个后端解析的 Runtime binding，客户端不能通过 request/query/header/Vite env 覆盖；长期 `BusinessAgentVersion -> RuntimeBinding` seam 保留。
- [ ] **ADM-06**: P0 只有 exact-commit Workspace lane 与 P0-MCP 两个独立阻断门都通过且声明范围零未分类失败时才能退出，任何一门都不能豁免另一门。
- [ ] **ADM-07**: P1、P2A、P2B 分别绑定明确 AGV 锚点、质量 owner 和验收入口；阶段计划本身不预授权升级任何 AGV 状态。
- [ ] **ADM-08**: P0 退出证据包含目标测试、quality policy、`runtime-bootstrap-scan`、`codex-guard`、typecheck、main-flow、串行全量测试与真实容器回执；local-debug、私有 env 改写或 live volume 修改不能替代。

### P1：测评领域、独立协议与 API

- [ ] **EVAL-01**: 平台评测方可登记稳定 `EvaluationBenchmark`，并从仓库外 evaluator-owned 只读 Git 的精确 commit 解析不可变 `EvaluationProtocolRevision` 与 content/corpus/scorer/safety digest；数据库和 Registry 不复制正文。
- [ ] **EVAL-02**: 首个发布协议固定 8 类合成/脱敏 holdout case 与 `allowed_tools=[]`，覆盖合法映射、类型错误、缺证、歧义、虚构压力、prompt injection、敏感值和高风险动作。
- [ ] **EVAL-03**: 业务 Agent Workspace、文件工具、Subagent、Trace 和 UI 都不能枚举或读取其他 holdout case、Ground Truth、scorer、阈值或排序；执行器只向当次 sample 提供必要输入。
- [ ] **EVAL-04**: typed `EvaluationExecution` 绑定一个精确 BusinessAgentVersion、一个 protocol revision、一个 purpose、一个环境指纹和 1..N 个不可变 sample refs；purpose 至少区分 workspace regression、release baseline 与 release candidate。
- [ ] **EVAL-05**: 完成 execution 产生唯一不可变 `Assessment`；Scorecard/Violation/SafetyGate 与 `EvaluationComparisonGroup`、`EvaluationReviewDecision`、`ReleaseGateDecision` 保持独立对象和审计关系。
- [ ] **EVAL-06**: 确定性 Scorecard 按 40/30/20/10 四维、总分 80、critical 断言和独立安全门判定；同一已捕获输出重复评分 3 次得到字节级一致规范结果。
- [ ] **EVAL-07**: baseline 与 candidate 各执行 3 次独立 Agent sample，使用不同 `run_id`，并分别记录分项均值、最小值、方差、失败率和任一 safety veto。
- [ ] **EVAL-08**: 无工具协议中的工具调用、虚构事实/引用/审批、敏感标记复述、注入改写规则、高风险动作越权和隐藏失败中的任一项都独立否决整次 assessment，高综合分不能抵消。
- [ ] **EVAL-09**: baseline/candidate 只有 benchmark/protocol、corpus/scorer/safety digest、Runtime/模型/tool policy/环境、资源预算和采样规程完全兼容时才可比较；不兼容时标记 `incomparable` 并在新条件下成对重跑。
- [ ] **EVAL-10**: fresh DB、历史 DB、重复 migration 与回滚均能持久化最小 typed execution/sample/assessment/comparison/finding 元数据和引用，不复制 benchmark/Workspace 正文、SDK 事实或 `AgentTestRun.report_json`。
- [ ] **EVAL-11**: `EvaluationExecutionDetailResponse` 以 execution 为查询主键聚合 protocol、sample refs、assessment、comparison、findings 与 release gate；`AgentTestRunResponse` 只增加轻量可选 sample ref，历史普通报告保持 `null`。
- [ ] **EVAL-12**: OpenAPI 与前端生成类型从同一 Pydantic 契约派生；Agent/formatter 伪造 ID、commit、score、approval、status、scope 或 provenance 时后端忽略或拒绝。
- [ ] **EVAL-13**: 已完成且同 Agent/scope 的 `EvaluationFinding` 可幂等创建新 ImprovementItem 或关联已有事项，finding 与事项为可双向追溯多对多关系，跨 Agent/非法状态被拒绝，重复/并发/部分失败在同一事务收敛。
- [ ] **EVAL-14**: 发布门只接受当前 Workspace suite digest、当前批准 benchmark/protocol、同协议 comparison、candidate assessment 和无 safety veto 的组合；旧 run、不可比结果、任一 critical 退化或历史 passed 不能放行。

### P1：单 Agent 测评 UI 与发布闭环

- [ ] **EUI-01**: 用户可从“业务 Agent 详情 → 测评”查看 benchmark/protocol、Scorecard、baseline/candidate comparison、安全门和 finding 列表，而不是只有无上下文总分。
- [ ] **EUI-02**: 测评详情只通过 backend URL 深链到“资产复利 → 测试资产 → 运行详情”的具体 sample/pytest/stdout/stderr/invocation 与 Trace，测试资产页不拼装 assessment 或复制隐藏正文。
- [ ] **EUI-03**: 页面具有空态、成功态、评分失败、安全否决、`incomparable`、基础设施错误可重试和多事项关联状态；所有状态都不泄露 holdout/Ground Truth。
- [ ] **EUI-04**: “运行 Workspace 回归”和“发起平台发布测评”是两个真实业务动作，不互相隐式触发，也不通过 `/lifecycle` 或状态推进伪装完成。
- [ ] **EUI-05**: 用户选择 blocking findings 执行“纳入改进治理”后，新事项停在反馈整理，且不会自动归因、生成方案、执行优化或创建 change set。
- [ ] **EUI-06**: 一条真实失败证据可贯通 finding → ImprovementItem → Attribution → OptimizationPlan → AgentChangeSet/Diff → 新 candidate commit → Workspace 回归 → 同协议 assessment → Release。
- [ ] **EUI-07**: 公共真实容器入口用当前工作树重建并 force-recreate，由隔离 evaluator 对真实 baseline/candidate 各执行 8-case、3 samples；该入口不启动 P0-MCP fixture，全部 case 均无工具。
- [ ] **EUI-08**: OpenAPI 导出、前端生成类型、前端 build 和真实浏览器 smoke 对测评页面的空/成功/失败/否决/不可比/重试/deep-link 场景零漂移。
- [ ] **EUI-09**: P1 只保留 Release → `OnlineOutcome` 的关系 seam 与 metric definition/scope，不伪造线上结果；AGV-051 及其他 AGV 只按实际证据保持或复核状态。

### P2A：Runtime 边界与 Claude 委托 adapter

- [ ] **RT-01**: 独立 runtime core 提供 typed `RuntimeSessionRef`、`RuntimeRun`、`RuntimeEvent` 与 `RuntimeProvenance`，core 不出现 `sdk_session_id`、Claude options 或 Claude event type。
- [ ] **RT-02**: `RuntimeBindingResolver`、代码内 `RuntimeRegistry` 与 `RuntimeGateway` 只按 backend-owned BusinessAgentVersion binding 为一次 run 解析一个 adapter bundle，并暴露小端口而非巨型 Runtime 接口。
- [ ] **RT-03**: 生产只注册 `claude-code`；未知 `AGENT_RUNTIME_KIND` 在启动前 fail-fast，请求/query/header/Vite env 均无 Runtime selector，启动诊断只显示 kind、版本和 capability 摘要。
- [ ] **RT-04**: 每项 Runtime capability 使用 `support_level + constraints + coverage + evidence_ref` 表达，不支持能力返回稳定 typed diagnosis，不能用布尔值、空事件或默认成功自证。
- [ ] **RT-05**: 固定版本的真实 Codex app server `thread/turn`、stream、cancel、approval 与 event spike 形成可重复脱敏回执，并将候选边界逐项分类为 reusable、Claude-shaped、candidate-specific 或 unsupported；fake/文档阅读不能替代。
- [ ] **RT-06**: `ClaudeCodeAdapterBundle` 委托现有 `ClaudeRuntime`、SessionStore、HITL、event 与 telemetry 实现，不重写 agent loop，不翻译 Claude 原生 Workspace 配置。
- [ ] **RT-07**: Responses、会话、HITL、raw events、业务聊天和 Governor job 的生产调用方经 gateway/小端口运行；直接构造 `ClaudeRuntime` 只存在于 Claude adapter、composition root 与测试。
- [ ] **RT-08**: 非流式、SSE、取消、同 session 新 run 恢复、HITL、subagent、raw event、Trace 与 Langfuse 的 golden suite 和当前工作树真实容器行为等价。
- [ ] **RT-09**: P2A 保持现有 DB columns、`sdk_session_id`、OpenAPI、SSE 与前端生成类型单一公开契约，不增加 alias、双写、并行 session/message 数据库或 Runtime 数据副本。
- [ ] **RT-10**: native/adapter payload 伪造 Agent ID、run ID、status、principal 或 provenance 时后端覆盖或拒绝；必需链路保持离线可用，且不新增顶层卷或真实凭据。

### P2B：Governor 受控学习基础

- [ ] **GOV-01**: 每次 Governor 业务产物在更新当前 Attribution/OptimizationPlan 投影前，原子追加 `GovernorRunEvidence`，保留 raw agent text、具体 formatter output、后端 projection、人工修订 diff 与后续测试/发布/回退结果引用。
- [ ] **GOV-02**: baseline 与 candidate 都物化为精确、不可变 `GovernorCapabilityVersion` build；manifest/digest 只包含 allowlisted 可执行 Workspace/job/prompt/skill/rule/typed contract/method 内容，并排除 scope、binding、Runtime、model/provider 与 dev/holdout pack。
- [ ] **GOV-03**: 首切片只生成 typed `ATTRIBUTION` `GovernorMethodCandidate`，其 `draft -> built -> evaluating -> evaluated` 技术生命周期由集中转移表/helper 管理，评估通过或 active 不是 candidate status。
- [ ] **GOV-04**: `ApplicabilityScope` 由后端投影为 attribution + security-operations + 精确目标 Agent + 批准风险等级的窄 scope；同一 build 扩大 scope 必须产生独立 outcome，不能修改 selector 继承结果。
- [ ] **GOV-05**: `BaselineBindingProjection` 只把当前静态 config/profile 投影为 shadow 评估基线，线上 Governor resolver 不读取它，也不存在权威 scope-aware binding。
- [ ] **GOV-06**: evaluator-owned dev pack 与 holdout pack 权限隔离、分别版本化并位于 capability build 外；research/candidate/Workspace 不能读取 holdout 正文、期望语义、scorer 或 pack 路径。
- [ ] **GOV-07**: 隔离 evaluator 在相同协议/环境下执行 exact current/candidate build，后端持有 blind A/B mapping，evaluator 看不到 build 身份，确定性门优先于模型辅助判断。
- [ ] **GOV-08**: 每次评估写入不可变 `EvaluationOutcome=passed | failed | inconclusive`，绑定 exact build/scope/pack/scorer/evaluator/Runtime provenance；重评新建 outcome，不包含人工决定或激活字段。
- [ ] **GOV-09**: research/evaluator 失败只记录 shadow failure，不阻断当前四阶段归因和 fallback；运行不调用 WebFetch、搜索、远程论文服务或外部记忆。
- [ ] **GOV-10**: P2B 只公开 baseline projection、method candidate 与 run evidence 的只读 API；仓库中不存在 activate/rollback mutation、`ActivationRecord`、权威 binding 或新增 Governor 用户页面。
- [ ] **GOV-11**: 每份业务产物和 outcome 可反查 exact capability build、method revision、ApplicabilityScope、trace 与 Runtime provenance；hostile Governor 输出中的 ID/status/version/gate/principal 被忽略。
- [ ] **GOV-12**: 每个 passed outcome 同时携带精确 scope、baseline/rollback build、观察窗、最小样本、指标、安全否决与回退阈值的线上观察契约，但不会执行线上切换。
- [ ] **GOV-13**: retention 可按分类删除或不可逆脱敏 raw payload/退役 pack，同时保留 digest、scope、outcome、原因/时间和 tombstone；活跃评估/评审/观察引用阻止删除。
- [ ] **GOV-14**: P2B 退出同时需要至少一条 P1 真实闭环证据、P2A gateway 等价和当前工作树真实容器中的 Governor 主链 + shadow evidence；两项上游未满足时只能保留开发结果，不能宣称阶段完成。

### P3：平台基础与扩展组合准入

- [ ] **PLAT-01**: 每条扩展线在启动前都有可审计准入记录，明确上游证据、真实用户任务、被治理对象、AGV、owner、适用平台维度、公开契约/数据迁移清单、身份、幂等、停止条件、回滚点和真实验收；缺项时不得启动。
- [ ] **PLAT-02**: EvalOps 保持“业务 Agent 详情 → 测评”单 Agent 主入口、“测试资产”sample/run drill-down 与按触发条件启用的独立测评中心分工；第二 benchmark/protocol、跨 Agent campaign、持续隐藏集或专家队列出现时才能启用测评中心。
- [ ] **PLAT-03**: Asset Registry 只关联 Agent/version、protocol、run、feedback、method、change set、release 与 OnlineOutcome 的稳定引用、digest、scope 和 provenance，不复制 Workspace、评测包、Runtime 或外部业务正文。
- [ ] **PLAT-04**: 能力/场景包应用只生成目标 Agent 的待审查 `AgentChangeSet`；来源 Agent 的通过结果不被继承，每个目标 Agent 在自己的版本、Runtime binding 和 scope 下独立测评。
- [ ] **PLAT-05**: AgentGov 治理动作使用服务端认证映射的不可伪造 principal 和 ResourceScope，并能分离 protocol 编辑、候选生成、独立评测、人工复核与启用职责；请求体伪造 operator/role/scope 被拒绝。
- [ ] **PLAT-06**: run/trace/feedback/evaluation/holdout/review/release 具有来源、授权、分类、保留、导出、删除、legal hold、备份恢复和部分失败审计；隐藏正文物理隔离，删除后保留必要 digest/tombstone。
- [ ] **PLAT-07**: 通用 integration contract 覆盖签名、稳定外部 ID、scope、幂等、重试、重复/乱序、超时、断网、撤销与对账；只读 observer 不能写配置、批准工具或控制外部进程，也不建立第二套 run/session/trace 真相源。
- [ ] **PLAT-08**: 每条已启动扩展登记可用性/延迟/队列/吞吐/证据完整性与模型/工具/存储/专家成本的 baseline、target、guardrail、window、owner、evidence source；超限时限流、排队、停止或显式降级，不静默丢证据或绕过安全门。
- [ ] **PLAT-09**: Release 后的 `OnlineOutcome` 只引用外部业务事实并绑定 Agent/version、metric definition、observation window、scope 与 provenance；它不改写离线 Assessment，达到安全/退化阈值时可触发可审计回滚。
- [ ] **PLAT-10**: 网络安全完整 MVP 只有在 50+ 版本化高质量案例、至少两名独立专家、可见/隐藏集分离、动态工具隔离/审批、完整 UI 状态和线上指标/回退阈值齐备时才可退出；P0-MCP 回执不能替代其中任何领域证据。
- [ ] **PLAT-11**: Runtime 公共主键迁移只有 P2A gateway/真实第二协议/历史数据/breaking change 证据获批后才可原子切到 opaque `platform_session_id`，不保留长期 alias/双写；第二生产 Runtime 还必须有真实需求、固定 spike 和完整 adapter suite。
- [ ] **PLAT-12**: Governor canary 只有服务端 principal、职责分离、exact passed outcome/build/scope、有效 `HumanReviewDecision=approved_for_canary`、观察窗与回退门齐备时才可用 CAS/idempotency 切换；P2B passed outcome 不能单独激活。
- [ ] **PLAT-13**: 外部 CLI observer 只有通用 API/webhook 确实不足时才启动；Multica 只有至少两个独立真实场景仍无法由通用能力满足时才重新评审，未配置任何外部协作产品时核心离线闭环保持完整。
- [ ] **PLAT-14**: AGV-051 至 AGV-055 只按各自真实 UI/身份/数据/集成/观察窗与成本证据复核，P3 框架或单条扩展通过不会连带升级其他 `gap/future`。

### v3.1：真实 E2E 与完成审计

- [ ] **AUDIT-01**: 最终验收从当前工作树重建/force-recreate，并为 P0 exact-commit、P0-MCP、P1 live、P2A Claude/spike、P2B shadow 与实际启动的 P3 线分别生成 fresh source/image/config/commit/digest 回执。
- [ ] **AUDIT-02**: 真实浏览器与 live Agent 可完成“发布测评失败 → finding → 四阶段改进 → candidate → Workspace/同协议复测 → Release → OnlineOutcome/观察 → 必要时回滚”的可追溯场景，未启动的条件扩展明确标记未启动而非伪造成功。
- [ ] **AUDIT-03**: fresh DB 与真实历史 runtime 数据都能完成 migration、列表、详情、sample deep-link、评测聚合和关键 UI，无 500、字段漂移或历史 payload 伪造。
- [ ] **AUDIT-04**: 对抗性完成审计证明无 secret/env/私有 endpoint 泄漏、无 holdout/Ground Truth 枚举、无跨 Agent/scope 污染、无可写 live volume 旁路且所有临时环境清理完成。
- [ ] **AUDIT-05**: 必需链路在离线/内网与本地化 LLM 条件下可运行；外部研究、远程 fixture、Multica 或第二 Runtime 缺失不会破坏核心闭环。
- [ ] **AUDIT-06**: `codex-guard`、typecheck、main-flow、串行 `make test`、OpenAPI 导出、前端类型生成/漂移检查、frontend build、真实浏览器 smoke 与所有适用公共容器入口在同一完成候选上通过。
- [ ] **AUDIT-07**: 完成审计逐项引用 AGV-002/006/009/010/012/014/019/028/035/040/043/045/046/050/051-055 的真实证据并保留未满足 gap，任何状态升级都有对应用户行为和当前运行回执。
- [ ] **AUDIT-08**: 完成回执为每个阶段结果登记 baseline、target、guardrail、measurement window、owner、evidence source、SLO/成本摘要与可执行回滚点。
- [ ] **AUDIT-09**: GSD 完成审计确认 102 条 v3.1 requirement 全部且仅映射一个 Phase，Phase 6-15 的计划/验证产物可追溯，前序 Phase 0-5 与 `.planning/phases/agent-version-governance-diff-refactor/` 历史保持原位。

## Out of Scope

| Feature | Reason |
| --- | --- |
| 生产 MCP、真实凭据/客户数据、写操作与高风险自动处置 | P0-MCP 仅为隔离合成 no-auth/read-only 平台 fixture；真实需求必须通过 P3 独立准入。 |
| 把 8-case P1 称为完整安全测评 MVP或 Agent 总能力分 | P1 只证明静态发布测评纵切片；完整 MVP 的案例、专家、动态环境和线上效果门不可降级。 |
| P2A 期间迁移公开 `sdk_session_id` 或接入第二生产 Runtime | 先证明内部边界和 Claude 等价；公开迁移与第二 Runtime 是 P3 条件扩展。 |
| P2B 自动启用、联网研究、自修改或全局传播 | 当前只允许 shadow build/outcome；可信 principal、人工决定、canary、CAS 与回滚未先验满足。 |
| 多租户组织/成员/业务权限产品 | v3.1 只治理 AgentGov 自身单组织 principal/resource scope，外部系统拥有业务权限。 |
| 新增四阶段第五阶段、空壳测评中心或 Governor 元治理入口 | 用户动作与信息架构按现有四阶段和触发式入口分层，不把内部概念实体化。 |
| 自动 bump `VERSION`、创建 tag 或推送发布 | 版本发布必须由用户明确确认，不能由阶段完成隐式触发。 |

## Traceability

| Requirement | Phase | Status |
| --- | ---: | --- |
| P0W-01 | Phase 6 | Complete |
| P0W-02 | Phase 6 | Complete |
| P0W-03 | Phase 6 | Complete |
| P0W-04 | Phase 6 | Complete |
| P0W-05 | Phase 6 | Complete |
| P0W-06 | Phase 6 | Complete |
| P0W-07 | Phase 6 | Complete |
| P0W-08 | Phase 6 | Complete |
| LANE-01 | Phase 7 | Complete |
| LANE-02 | Phase 7 | Complete |
| LANE-03 | Phase 7 | Complete |
| LANE-04 | Phase 7 | Complete |
| LANE-05 | Phase 7 | Complete |
| LANE-06 | Phase 7 | Complete |
| LANE-07 | Phase 7 | Complete |
| LANE-08 | Phase 7 | Complete |
| MCP-01 | Phase 8 | Pending |
| MCP-02 | Phase 8 | Pending |
| MCP-03 | Phase 8 | Pending |
| MCP-04 | Phase 8 | Pending |
| MCP-05 | Phase 8 | Pending |
| MCP-06 | Phase 8 | Pending |
| MCP-07 | Phase 8 | Pending |
| MCP-08 | Phase 8 | Pending |
| ADM-01 | Phase 9 | Pending |
| ADM-02 | Phase 9 | Pending |
| ADM-03 | Phase 9 | Pending |
| ADM-04 | Phase 9 | Pending |
| ADM-05 | Phase 9 | Pending |
| ADM-06 | Phase 9 | Pending |
| ADM-07 | Phase 9 | Pending |
| ADM-08 | Phase 9 | Pending |
| EVAL-01 | Phase 10 | Pending |
| EVAL-02 | Phase 10 | Pending |
| EVAL-03 | Phase 10 | Pending |
| EVAL-04 | Phase 10 | Pending |
| EVAL-05 | Phase 10 | Pending |
| EVAL-06 | Phase 10 | Pending |
| EVAL-07 | Phase 10 | Pending |
| EVAL-08 | Phase 10 | Pending |
| EVAL-09 | Phase 10 | Pending |
| EVAL-10 | Phase 10 | Pending |
| EVAL-11 | Phase 10 | Pending |
| EVAL-12 | Phase 10 | Pending |
| EVAL-13 | Phase 10 | Pending |
| EVAL-14 | Phase 10 | Pending |
| EUI-01 | Phase 11 | Pending |
| EUI-02 | Phase 11 | Pending |
| EUI-03 | Phase 11 | Pending |
| EUI-04 | Phase 11 | Pending |
| EUI-05 | Phase 11 | Pending |
| EUI-06 | Phase 11 | Pending |
| EUI-07 | Phase 11 | Pending |
| EUI-08 | Phase 11 | Pending |
| EUI-09 | Phase 11 | Pending |
| RT-01 | Phase 12 | Pending |
| RT-02 | Phase 12 | Pending |
| RT-03 | Phase 12 | Pending |
| RT-04 | Phase 12 | Pending |
| RT-05 | Phase 12 | Pending |
| RT-06 | Phase 12 | Pending |
| RT-07 | Phase 12 | Pending |
| RT-08 | Phase 12 | Pending |
| RT-09 | Phase 12 | Pending |
| RT-10 | Phase 12 | Pending |
| GOV-01 | Phase 13 | Pending |
| GOV-02 | Phase 13 | Pending |
| GOV-03 | Phase 13 | Pending |
| GOV-04 | Phase 13 | Pending |
| GOV-05 | Phase 13 | Pending |
| GOV-06 | Phase 13 | Pending |
| GOV-07 | Phase 13 | Pending |
| GOV-08 | Phase 13 | Pending |
| GOV-09 | Phase 13 | Pending |
| GOV-10 | Phase 13 | Pending |
| GOV-11 | Phase 13 | Pending |
| GOV-12 | Phase 13 | Pending |
| GOV-13 | Phase 13 | Pending |
| GOV-14 | Phase 13 | Pending |
| PLAT-01 | Phase 14 | Pending |
| PLAT-02 | Phase 14 | Pending |
| PLAT-03 | Phase 14 | Pending |
| PLAT-04 | Phase 14 | Pending |
| PLAT-05 | Phase 14 | Pending |
| PLAT-06 | Phase 14 | Pending |
| PLAT-07 | Phase 14 | Pending |
| PLAT-08 | Phase 14 | Pending |
| PLAT-09 | Phase 14 | Pending |
| PLAT-10 | Phase 14 | Pending |
| PLAT-11 | Phase 14 | Pending |
| PLAT-12 | Phase 14 | Pending |
| PLAT-13 | Phase 14 | Pending |
| PLAT-14 | Phase 14 | Pending |
| AUDIT-01 | Phase 15 | Pending |
| AUDIT-02 | Phase 15 | Pending |
| AUDIT-03 | Phase 15 | Pending |
| AUDIT-04 | Phase 15 | Pending |
| AUDIT-05 | Phase 15 | Pending |
| AUDIT-06 | Phase 15 | Pending |
| AUDIT-07 | Phase 15 | Pending |
| AUDIT-08 | Phase 15 | Pending |
| AUDIT-09 | Phase 15 | Pending |

**Coverage:**

- v3.1 requirements: 102 total
- Mapped to phases: 102
- Unmapped: 0 ✓
- Duplicate mappings: 0 ✓

---
*Requirements defined: 2026-08-05*  
*Last updated: 2026-08-13 after Phase 7 completion and Phase 8 handoff*
