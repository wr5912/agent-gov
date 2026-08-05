# AgentGov 下一阶段 P2B Governor 受控学习基础实施方案

> 文档状态：评审稿。
>
> 工程阶段说明：P2B 只建立 Governor 的 shadow 学习证据和独立评估基础，不表示 Governor 可以
> 自主学习、自主改写或自动启用能力。
>
> 证据上游与退出前置：[P1 网络安全测评纵向闭环实施方案](./AgentGov下一阶段P1网络安全测评纵向闭环实施方案.md)。
> 账本、候选和隔离评估可提前开发，但 P2B 退出前必须取得 P1 真实闭环证据；Runtime
> 接入与退出另以 [P2A Runtime 边界提取与 Claude Adapter 实施方案](./AgentGov下一阶段P2ARuntime边界提取与ClaudeAdapter实施方案.md)
> 的 gateway 等价验收为前置。
>
> 需求依据：[Governor 自研究与受控自学习能力需求](../Governor自研究与受控自学习能力需求.md)。

## 1. 目标与退出结果

把当前事项级 Governor 的产物转化为可审计、不可变、绑定能力版本的学习证据，并针对一个人工
选定的归因方法缺口，从方法语义候选构建精确、不可变的 Governor 能力 build，
再完成隔离评估：

```text
四阶段真实结果
  -> 不可变 GovernorRunEvidence
  -> 人工修订 diff 与后续结果
  -> 一个 ATTRIBUTION 能力缺口
  -> GovernorMethodCandidate
  -> immutable GovernorCapabilityVersion build
  -> backend-owned ApplicabilityScope proposal
  -> dev pack 迭代
  -> 不可读 holdout 上的 blind current vs candidate
  -> immutable EvaluationOutcome
  -> 未来独立 HumanReviewDecision
  -> 未来单独 ActivationRecord
```

P2B 退出时，线上 Governor 仍使用当前静态 config/profile；当前运行态尚无
scope-aware active binding。P2B 只建立不接入线上 resolver 的 shadow
`BaselineBindingProjection`。即使候选 build 评估通过，也不产生 `ActivationRecord`、
不变更线上配置、不自动切换。

## 2. 实际问题与边界

### 2.1 事实依据

- 当前 Governor 已有集中 job spec、typed formatter、独立 Workspace、Trace 和受控写入；
- Attribution 与 OptimizationPlan 采用当前值 upsert，人工修订会覆盖原值，不能作为学习账本；
- 历史 `agent_jobs` 已明确只读，不能恢复写方法承担新运行队列；
- 当前静态 Governor version/config hash 不能表达方法修订、候选、评估和回退；
- 现有 Asset Registry 缺少适用条件、反证、评估、版本和失效语义；
- 当前共享 API key 与请求体 `operator` 不能证明有权启用 Governor 能力；
- Governor 可读取 Workspace 中的敏感配置，直接增加自由网络搜索会产生外泄和提示注入风险。

### 2.2 本阶段做

- 记录不可变 Governor run、原始产物、人工修订 diff 和后续结果；
- 建立 immutable capability build 和 shadow `BaselineBindingProjection`，将当前静态
  config/profile 投影为评估基线，但不接入线上 resolver、不切换线上版本；
- 只为 `ATTRIBUTION` 生成一个 typed 方法候选；
- 将候选确定性物化为精确 GovernorCapabilityVersion build，在 dev pack 验证后，
  再在候选不可读的 holdout pack 上盲化比较 current 与 candidate；
- 分别记录 `EvaluationOutcome`；`HumanReviewDecision` 和 `ActivationRecord` 是后续
  独立对象，不伪装成 candidate/evaluation status；
- 提供只读证据查询，供工程评审和测试使用。

### 2.3 本阶段不做

- 不开放 WebFetch、主动搜索或外部研究；
- 不自动跨 Agent 召回和全局应用；
- 不允许 Governor 修改自身 Workspace、prompt、skill、当前静态 config/profile 或未来
  scope-aware active binding；
- 不新增四阶段用户阶段，不把 Governor 注册为业务 Agent；
- 不恢复旧全局 eval case/run API；
- 不提供激活、回退或请求体自报 operator 的公开动作，不产生
  `ActivationRecord`。

主要替代方案“先加历史案例检索”不采用，因为它只能提高召回，不能证明方法有效、适用或可安全
启用。

## 3. 治理对象矩阵

| 维度 | 结论 |
| --- | --- |
| 一阶治理对象 | 业务 Agent、版本、场景、改进事项和四阶段产物 |
| 二阶治理对象 | Governor 的归因方法、能力 build、适用 scope 和治理效果 |
| 候选提出者 | Governor research job，只能提出业务语义 |
| 事实与门禁所有者 | 后端固定证据、build、scope、评估输入、blind mapping、结果和状态 |
| 独立评估者 | 与候选生成上下文隔离、不知 A/B 身份的 evaluator profile；确定性门优先 |
| 未来人工裁决者 | 拥有可验 principal/role 的授权人员；决定不写入评估结果 |
| 本阶段启用者 | 无；只产出 passed/failed/inconclusive shadow `EvaluationOutcome` |
| 数据/证据资产 | 原始产物、人工 diff、Trace、评估/发布/回退结果等证据 |
| 方法论资产 | 归因方法、适用条件、反证和评估规程 |
| 执行资产 | 精确 capability build、research/evaluator prompt、Signature、OutputModel、dev/holdout pack |
| 横切治理维度 | version、provenance、audit、scope 和 lifecycle 贯穿三类一级资产，不再并列成第四/第五类资产 |

### 3.1 AGV 基线与本阶段贡献

P2B 只建立窄 scope 的 shadow 学习和评估基础，不以设计或局部回执预授权
[AgentGov 核心功能测试用例](../AgentGov核心功能测试用例.md)状态升级：

| AGV | 当前基线 | P2B 贡献 | 阶段后状态裁决 |
| --- | --- | --- | --- |
| AGV-006 三类治理资产联合追溯 | `gap` | GovernorRunEvidence、方法候选和执行 build 形成三类资产的 shadow 引用 | 保持 `gap`；未完成一次业务改进事项对三类资产的端到端联合追溯 |
| AGV-009 失败转化为组织级知识 | `gap` | 失败证据可产生 MethodCandidate、immutable build 和 shadow outcome | 保持 `gap`；未经人工决定、激活和后续同类问题捕获证据，不得宣称已转化为组织级知识 |
| AGV-010 跨 Agent 共享已验证经验 | `gap` | `ApplicabilityScope` 和独立 outcome 为未来跨 Agent 验证提供边界 | 保持 `gap`；P2B 首切片只有精确目标 Agent 窄 scope，无第二 Agent change set、评估和发布/回退证据 |
| AGV-012 方法论资产复用 | `current` | 将方法 revision 确定性物化到 capability build，增加适用、反证和评估关系 | 保持 `current`；P2B 不删除已有集中 methodology profile/typed contract 证据 |
| AGV-019 治理 Agent 只输出建议和治理产物 | `current` | Governor 只提出方法语义，build/scope/outcome 均由后端投影 | 保持 `current`；P2B 无 activation/rollback mutation、无 `ActivationRecord`、不改线上静态 config |
| AGV-045 能力包与跨 Agent 方法沉淀 | `gap` | Governor method/capability build 只是治理 Agent 自身方法演进的局部基础 | 保持 `gap`；它不是业务 Agent 能力包，无两个业务 Agent 的独立应用 provenance、评测、发布和回退 |

## 4. 新子域与单一契约

新增独立 `governor_learning` 子域，不继续扩大 `improvement_governor_service.py`、
`improvement_execution_service.py` 或中心路由。

### 4.1 `GovernorRunEvidence`

append-only 记录每次 Governor 业务产物：

- backend-owned：evidence ID、job type、Agent/improvement、capability version、method revision、
  输入证据引用、trace、时间和后续结果引用；
- boundary-owned：`raw_agent_text`、具体 `formatter_output` 的边界序列化、最终 projected payload；
- 人工修订：原产物 digest、修订后 digest、字段级 diff、确认结果；
- 业务结果：执行是否形成有效 change set、测试是否通过、是否发布、是否回退或复发。

现有 Attribution/OptimizationPlan 等表继续承担“当前业务投影”；每次生成和人工修改先追加证据，
再更新当前投影。两步必须处于一个可恢复的一致性边界，不能只覆盖当前值。

### 4.2 `ApplicabilityScope` 与 shadow baseline projection

`ApplicabilityScope` 是 backend-owned typed 值，至少表达：

- `capability_key`：如 `ATTRIBUTION`；
- `business_domain` 和可选精确 `agent_id`；
- 场景/问题类型、风险等级与必要的 Runtime 约束；
- 适用条件与明确不适用条件的结构化引用。

P2B 首切片将候选的适用语义投影为
`ATTRIBUTION + security-operations + 精确目标 Agent + 已批准风险等级`
的窄 scope proposal，不生成平台全局通配候选。跨 Agent 或更宽 scope 必须使用同一
build 另行评估，产生新 `EvaluationOutcome`，不能只修改 selector。

P2B 的 shadow `BaselineBindingProjection` 只记录“当前静态 config/profile 对应哪个
baseline build、用于哪个评估 scope”，不是线上权威 binding，不参与 Governor
job 解析。P3 才可在可验人工决定、canary 和 CAS 契约完整后，迁移为 backend-owned
`capability_key + scope_digest -> active capability_version_id` authoritative binding。届时冲突
必须 fail closed，未命中时只能回退到明确批准的 baseline，不自动选择“最相似”候选。

### 4.3 `GovernorCapabilityVersion`

`GovernorCapabilityVersion` 不是方法文字的版本号，而是可独立构建、执行和回退的
immutable build。首个 baseline 和每个 candidate build 都由后端对以下受控内容构建
manifest 并计算 digest：

- Governor Workspace 中经 allowlist 准入的行为配置、rules、skills 和原生包 digest；
- 集中 job registry、prompt builder 与精确 job spec；
- Signature、具体 FormatterOutputModel、ProjectedOutputModel 和确定性 projection；
- 方法 revision manifest 及其实际被 prompt/skill/job spec 消费的可执行内容。

build digest 的 allowlist 明确排除 `ApplicabilityScope`、active/shadow binding、运行时 Runtime
binding、model/provider、`dev_pack` 和 `holdout_pack`。这些属于评估或激活的横切关系/
provenance，不改变 build 身份。同一 build 可在多个 scope 上分别评估，每个 scope
必须产生独立 outcome、人工决定和激活记录。

评估执行的必须是 current build 与 candidate build，不允许在同一基线 build 上临时
注入一段方法正文后宣称候选能力已被评估。模型/provider 的精确运行值作为每次
run provenance 记录，不写入方法正文；若评估条件发生变化，结果不得直接比较。

### 4.4 `GovernorMethodCandidate`

首切片限定 `job_type=ATTRIBUTION`。Governor 只输出：

- 能力缺口解释；
- 方法正文和步骤；
- 适用条件；
- 反例和 counterevidence；
- 预期改善与风险；
- 建议的验证语义。

Candidate ID、来源 evidence、scope、status、build/digest、时间和 principal 均由后端生成或
覆盖。评估结果、人工决定和激活记录不是 candidate 字段。

候选生命周期集中定义：

```text
draft -> built -> evaluating -> evaluated
```

该状态只表达候选与 build 的技术生命周期，不表达通过、人工批准或 active。
所有非法转移必须被统一 helper 拒绝。

### 4.5 评估、人工决定与激活记录分离

`EvaluationOutcome` 是不可修改的评估事实，每次评估绑定：

- current/candidate capability build digest 和完全相同的 `ApplicabilityScope`；
- dev/holdout pack digest，evaluation protocol/scorer/evaluator profile 及版本；
- backend-owned blind A/B mapping，evaluator 不可见；
- 两个 build 的分项结果、安全门、退化判断和 `passed | failed | inconclusive`；
- trace、Runtime/model/provider、采样参数、重复次数和 backend provenance。

重新评估创建新 outcome，不覆盖旧结果。`HumanReviewDecision` 是独立的
`approved_for_canary | rejected | deferred` 决定，必须关联可验 principal/role、
理由、查看的 outcome 和 scope；没有可验身份时不能生成该记录。

`ActivationRecord` 只在授权控制面实际变更 scope-aware binding 或回退时创建，记录精确
from/to build、scope、binding revision/CAS、principal、线上观察与回退门。P2B 只固定
该语义边界，不建表、不提供写路由、不产生记录。

## 5. 隔离评估与独立性

### 5.1 评估资产

- `dev_pack` 和 `holdout_pack` 是两个权限隔离、分别版本化的 evaluator-owned
  Governor 执行资产，
  不是同一目录的两个标签；
- `dev_pack` 位于平台评估方受控存储，通过明确只读授权向 research job 开放，
  可在候选研究与 build 调试中使用，存放脱敏案例、公开期望语义和快速门；
- `holdout_pack` 属于评估方，位于平台受控的离线评估资产存储，不放入 Governor
  Workspace、research job 可读路径或 candidate build；候选侧只能看到 pack ID/digest
  和非泄题的 protocol 摘要；
- 两类 pack 都只保存脱敏后的内部案例和期望语义，不复制业务 Agent Workspace 测试正文；
- 两类 pack 都位于 capability build 外，build manifest allowlist 必须排除 pack 目录、
  ID 和 digest，避免评估资产改变可执行 build 身份；
- pack digest、scorer/protocol revision 和访问边界进入每次 evaluation；
- P2B 只覆盖一个归因能力缺口，案例必须同时包含成功、证据不足、跨 Agent 不适用、
  hostile backend-owned 字段和保守回退。

### 5.2 隔离 evaluator

- 在集中 `AgentJobType/spec` 注册独立 evaluation job；
- evaluation runner 用相同协议和运行条件执行 current/candidate build，不允许候选修改
  scorer、门禁、期望语义或 holdout 内容；
- 后端随机将两组脱敏输出投影为 A/B；evaluator profile 只读 holdout evaluation
  envelope、A/B 输出和评分规程，不可见 build 身份和 blind mapping；
- 不读取候选研究过程、研究 prompt 或候选自评结论；
- 不能写 candidate、build、status、binding、blind mapping 或业务 Agent Workspace；
- formatter 返回具体 Pydantic OutputModel，不返回 `BaseModel` 或裸 dict；
- 确定性门先执行，模型辅助判断不能推翻安全失败。

### 5.3 Shadow 评估门

候选 build 只有同时满足以下条件，`EvaluationOutcome` 才可记录为 `passed`：

- 所有安全、越权、恶意输入和离线场景通过；
- 至少一个目标归因指标相对 current 明确改善；
- 其他主要指标无实质退化；
- 改善能由候选方法解释，不来自放宽门禁或评估泄漏；
- 证据、current/candidate build、scope、pack、scorer、evaluator 和 Runtime provenance 完整。

证据不足时必须为 `inconclusive`，不得按默认及格处理。P2B 没有任何从
`passed` 到 `HumanReviewDecision` 或线上 active binding 的路径。

### 5.4 线上观察与回退契约

P2B 不激活候选，但每个 passed outcome 必须同时产出一份供后续人工评审的线上
观察契约：

- 精确 scope、当前 baseline build 和可回退 build；
- 启用前基线窗口、上线观察窗口与最小样本条件；
- 目标指标、安全否决、最大可接受退化、人工修订、拒绝、执行、测试、发布、复发和回退观测；
- 立即回退触发：安全/越权失败、scope 错用、关键指标越阈、证据或 Runtime coverage 不足；
- 回退后保留 ActivationRecord、观察证据和原 build，不用新成功结果覆盖失败。

后续 P3 启动 canary 时，必须引用针对 exact outcome/build/scope 且仍在有效期内的
`approved_for_canary` HumanReviewDecision，将该契约固化到 `ActivationRecord`，
并以 scope-aware binding CAS 实现原子 canary/回退；P2B 只验证其完整性，
不执行任何线上变更。

## 6. 数据流与字段所有权

```text
Governor 业务产物：
  raw_agent_text
    -> AttributionFormatterOutput
    -> backend projected output
    -> boundary JSON
    -> immutable GovernorRunEvidence

Governor 候选能力：
  candidate_raw_agent_text
    -> CandidateFormatterOutput
    -> backend projected GovernorMethodCandidate
    -> immutable GovernorCapabilityVersion build
    -> dev validation
    -> blind holdout evaluation
    -> immutable EvaluationOutcome
```

| 所有者 | 字段 |
| --- | --- |
| Backend-owned | evidence/candidate/build/outcome ID、Agent/improvement、job type、scope/binding、技术状态、pack/digest、blind mapping、trace、gate、时间、principal |
| Governor-owned | 缺口分析、方法正文、适用条件、反证、风险、验证语义 |
| Human-owned | 对业务产物的实际修订内容；未来 `HumanReviewDecision` 的决定与理由 |
| Boundary-owned | SQLite JSON、HTTP response、文件包、日志、Langfuse metadata |

Prompt/Signature 不要求 Governor 输出 backend-owned 字段。Hostile formatter 输出中的伪造 ID、
status、version、gate 或 principal 必须被忽略。

## 7. 当前业务链路兼容

- 四阶段工作台仍只有反馈整理、归因分析、优化执行、测试发布；
- 当前 Governor job 成功/失败语义、fallback 和用户重试入口不变；
- 研究或评估失败只记录 shadow failure，不能阻断当前归因；
- 当前 Attribution/OptimizationPlan API 继续读取当前投影，不直接读取学习账本；
- 历史 `agent_jobs` 保持只读；新账本不恢复 create/claim/finish 写方法；
- Asset Registry 本阶段只可引用 evidence/candidate/build/outcome 的 ID、digest、scope 和只读摘要，
  不复制方法正文、holdout 内容或 active binding，不建立第二套版本真相源；
- P2B 不修改业务 Agent 测试、发布和回滚规则。

## 8. 只读接口

P2B 可以新增只读 API，不能新增 activation mutation：

- `GET /api/governor/baseline-binding-projections`：当前静态 config/profile 到 baseline build
  与评估 scope 的 shadow 投影摘要；该接口不得被线上 Governor resolver 调用；
- `GET /api/governor/method-candidates`：候选列表与 shadow 状态；
- `GET /api/governor/method-candidates/{candidate_id}`：证据、方法、scope、build 和
  `EvaluationOutcome` 详情；不暴露 blind mapping 或 holdout 内容；
- `GET /api/governor/run-evidence/{evidence_id}`：不可变运行证据。

这些接口使用当前 API 认证，只服务开发观测与评审；没有可信 principal/role 前，不提供
create/activate/rollback 路由。OpenAPI 和前端生成类型若公开这些只读接口，必须从 Pydantic 契约派生。
P2B 不要求新增用户可见页面。

## 9. 架构与持久化

- 新表只表达 Governor learning domain，不恢复旧 `agent_jobs`、`TestDataset` 或 `EvalRun`；
- row record、运行时投影和 API response 分开建模；
- evidence、capability build 和 `EvaluationOutcome` 的身份/digest/关系是 append-only，
  candidate 技术状态只能通过集中转移 helper 修改；
- 当前投影与 evidence 写入必须幂等；部分失败回滚，不在 DB 事务中调用模型或外部服务；
- 先运行 Governor 得到 typed result，再在短事务内写 evidence 和当前投影；
- `BaselineBindingProjection` 在 P2B 只用于 shadow 评估；线上 resolver 继续读取当前静态
  config/profile，不新增权威 scope-aware binding 表或解析路径。P3 才设计从 shadow projection
  迁移到 compare-and-swap binding，并分别写入 `HumanReviewDecision` 与 `ActivationRecord`；
- migration 必须覆盖 fresh DB、历史 DB、重复执行和回滚。

### 9.1 保留、脱敏与删除边界

“不可变”是指身份、digest、provenance、scope、关系和决策事实不可被后续成功覆盖，
不表示敏感 raw payload 必须无期保留。

- `raw_agent_text`、formatter/projected payload、人工 diff、Trace 和 pack case 按当前部署的
  数据分类、访问 scope 与 retention policy 保留；未配置允许类别时不额外保存原文；
- 候选/评估前排除凭据、`.env`、私有 endpoint 和未授权跨 Agent 原文；
- 过期或授权删除可移除/不可逆脱敏 raw payload 与退役 pack 正文，但保留 backend-owned
  digest、分类、scope、outcome、删除原因/时间和 tombstone 审计关系；
- 仍在活跃评估、人工评审或线上观察窗口中被引用的 pack 不得删除；
- P2B 不提供公开 delete API；保留策略执行必须是 backend-owned maintenance action，
  不接受 Governor 输出或请求体自报 principal。

## 10. 测试同步矩阵

| 行为变化 | 旧测试处置 | 新增测试 | 深度 |
| --- | --- | --- | --- |
| 业务产物追加证据 | Attribution/plan 测试 `REFACTOR` | 原始值、人工 diff、后续结果均保留 | store/service |
| immutable capability build | 当前 profile/version 测试 `REFACTOR` | baseline/candidate build digest、精确执行内容、每次 run 绑定 | contract |
| shadow baseline projection | 无 | 精确投影当前静态 config/profile、同 build 多 scope 独立 outcome、线上 resolver 不读取 projection | contract/security |
| candidate lifecycle | 无 | draft/built/evaluating/evaluated 合法与非法转移 | state machine |
| dev/holdout 隔离 | 无 | candidate/research 不可读 holdout、pack digest 固定、在用 pack 不可删除 | security/integration |
| 独立 evaluator | 无 | blind A/B、身份泄漏阻断、current vs candidate、safety fail | integration |
| 记录分离 | 无 | outcome 不写人工决定；P2B 无 ActivationRecord 和 binding mutation | contract/negative |
| shadow failure | Governor fallback 测试 `KEEP` | 研究失败不阻断当前归因 | main flow |
| hostile output | 现有污染测试 `KEEP` | 伪造 version/gate/status/principal | security |
| append-only 与并发 | 无 | 重复 evidence、并发评估、事务回滚 | concurrency |
| retention/delete | 无 | 原文过期脱敏、digest/tombstone 保留、活跃引用阻止删除 | data governance |
| 只读 API | 无 | 未知 ID、分页/详情、无 mutation route | API/OpenAPI |

新增测试同步进入 `tests/quality_policy.json` 的 `improvement-governance` owner、
`feedback-improvement-loop` capability 和相应 main-full/main-flow lane。

## 11. Runtime、env 与安全边界

- P2B 的账本、候选、build 与隔离评估开发可与 P2A 并行；接入与阶段退出等待 P2A gateway 就绪；
- P2B evaluation runner 通过 P2A `ManagedExecutionDriver`，按后端解析的当前部署默认 Runtime
  分别执行 current/candidate build；Runtime binding 与运行 provenance 位于 build 身份之外，
  候选和请求不得选择或覆盖 Runtime；
- 不增加独立 Runtime selector、Vite env 或模型 key；
- 宿主机与容器继续选择各自完整 env；无私有 `MODEL_PROVIDER_API_KEY` 时在启动 Governor 前
  返回稳定错误，不回显值；
- Governor 研究证据在进入模型前排除 `.env`、凭据、私有 endpoint 和跨 Agent 非授权原文；
- 不调用 WebFetch、搜索、远程论文服务或外部记忆；
- 不增加新的顶层 Docker 卷或改变 `${HOME}/volume-agent-gov` 根布局；新表使用当前
  runtime DB，`dev_pack` 与 `holdout_pack` 都位于当前持久根下 evaluator-owned 的版本化
  受控存储。research job 仅只读挂载 dev，evaluation runner 按次只读挂载所需 pack；
  Governor Workspace 与 candidate build 均不包含任何 pack。

## 12. 验证与退出门

验证：

1. learning records、state machine、migration 和 append-only 目标 pytest；
2. prompt/Signature/OutputModel/formatter/projection typed contract；
3. hostile backend-owned 字段污染和跨 Agent evidence 越权；
4. exact current/candidate build 的 dev validation 与 blind holdout evaluation；
5. 研究/evaluator 失败时现有四阶段归因正常；
6. 只读 API/OpenAPI 与历史 DB 投影；
7. `make codex-guard`、`make typecheck`、`make main-flow-test`；
8. 阶段提交前串行 `make test`；
9. 公共真实容器入口验证当前 Governor 主链路与 shadow 记录同时成立。

退出硬门：

- 至少一个真实四阶段结果形成完整不可变 `GovernorRunEvidence`；
- 一个 `ATTRIBUTION` candidate 已物化为 exact immutable capability build，并在盲化 holdout
  上得到 `passed | failed | inconclusive` 可解释 `EvaluationOutcome`；
- 每次 Governor 产物可反查精确 capability build、method revision、ApplicabilityScope
  和 Runtime provenance；
- 人工修改前后内容均保留，不覆盖原始证据；
- 研究/evaluator 失败不影响当前业务输出；
- 仓库中不存在 activation/rollback mutation route、权威 scope-aware binding 或
  `ActivationRecord`；线上静态 config/profile 及其 resolver 均未改变；
- candidate/research 无法读取 holdout 正文、期望语义、scorer 秘密或 blind mapping；
- passed outcome 包含完整线上观察和回退契约，但没有执行线上变更；
- 无网络研究、无敏感值外泄、无跨 Agent 原文污染。

## 13. P3 启动条件

只有同时具备以下条件，才评审人工启用：

- P1 至少一条安全测评改进发布闭环；
- P2B shadow 候选和 current 对比证据；
- 独立 evaluator、dev/holdout 权限隔离和 blind A/B 评估稳定；
- 认证中间件能提供不可伪造的 principal 和 role；
- `EvaluationOutcome` / `HumanReviewDecision` / `ActivationRecord` 分离契约获批；
- shadow `BaselineBindingProjection` 到权威 scope-aware binding 的迁移方案，以及后者的
  原子 CAS、幂等、并发防重和回滚设计获批；
- 启用前基线、线上观察窗口、退化/安全触发和可回退 build 已固定；
- UI 明确位于 Governor 能力治理面，不进入四阶段工作台。
