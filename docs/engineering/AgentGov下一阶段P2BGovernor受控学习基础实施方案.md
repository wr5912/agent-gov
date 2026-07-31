# AgentGov 下一阶段 P2B Governor 受控学习基础实施方案

> 文档状态：评审稿。
>
> 工程阶段说明：P2B 只建立 Governor 的 shadow 学习证据和独立评估基础，不表示 Governor 可以
> 自主学习、自主改写或自动启用能力。
>
> 前置方案：[P1 网络安全测评纵向闭环实施方案](./AgentGov下一阶段P1网络安全测评纵向闭环实施方案.md)。
>
> 需求依据：[Governor 自研究与受控自学习能力需求](../Governor自研究与受控自学习能力需求.md)。

## 1. 目标与退出结果

把当前事项级 Governor 的产物转化为可审计、不可变、绑定能力版本的学习证据，并针对一个人工
选定的归因方法缺口形成候选和隔离评估：

```text
四阶段真实结果
  -> 不可变 GovernorRunEvidence
  -> 人工修订 diff 与后续结果
  -> 一个 ATTRIBUTION 能力缺口
  -> GovernorMethodCandidate
  -> 冻结评估包上的 current vs candidate
  -> eligible / rejected
```

P2B 退出时，线上 Governor 仍使用原 active capability；即使候选评估通过，也不能自动切换。

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
- 建立 capability version 和 active pointer，但不切换线上版本；
- 只为 `ATTRIBUTION` 生成一个 typed 方法候选；
- 在冻结离线评估包上隔离比较当前与候选；
- 提供只读证据查询，供工程评审和测试使用。

### 2.3 本阶段不做

- 不开放 WebFetch、主动搜索或外部研究；
- 不自动跨 Agent 召回和全局应用；
- 不允许 Governor 修改自身 Workspace、prompt、skill 或 active pointer；
- 不新增四阶段用户阶段，不把 Governor 注册为业务 Agent；
- 不恢复旧全局 eval case/run API；
- 不提供激活、回退或请求体自报 operator 的公开动作。

主要替代方案“先加历史案例检索”不采用，因为它只能提高召回，不能证明方法有效、适用或可安全
启用。

## 3. 治理对象矩阵

| 维度 | 结论 |
| --- | --- |
| 一阶治理对象 | 业务 Agent、版本、场景、改进事项和四阶段产物 |
| 二阶治理对象 | Governor 的归因方法、能力版本和治理效果 |
| 候选提出者 | Governor research job，只能提出业务语义 |
| 事实与门禁所有者 | 后端固定证据、版本、评估输入、结果和状态 |
| 独立裁决者 | 与候选生成上下文隔离的 evaluator profile |
| 本阶段启用者 | 无；只产出 eligible/rejected shadow 结论 |
| 数据资产 | 原始产物、人工 diff、Trace、测试/发布/回退结果 |
| 方法论资产 | 归因方法、适用条件、反证和评估规程 |
| 执行资产 | research/evaluator prompt、Signature、OutputModel、冻结评估包 |
| 版本资产 | capability version、method revision、candidate digest |

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

### 4.2 `GovernorCapabilityVersion`

首个 baseline version 由后端对以下受控内容计算 digest：

- Governor Workspace 行为配置；
- 集中 job registry 与 prompt builder；
- Signature、FormatterOutputModel、ProjectedOutputModel；
- 方法 revision manifest。

运行时模型/provider 作为每次 run provenance 记录，不写入方法正文。系统保持一个 backend-owned
active pointer；P2B 只读取，不提供切换动作。

### 4.3 `GovernorMethodCandidate`

首切片限定 `job_type=ATTRIBUTION`。Governor 只输出：

- 能力缺口解释；
- 方法正文和步骤；
- 适用条件；
- 反例和 counterevidence；
- 预期改善与风险；
- 建议的验证语义。

Candidate ID、来源 evidence、status、版本、评估结果、时间和 operator 均由后端生成或覆盖。

候选生命周期集中定义：

```text
draft -> evaluating -> eligible | rejected
```

`eligible` 只表示满足 P2B shadow 门，不表示 active。所有非法转移必须被统一 helper 拒绝。

### 4.4 `GovernorCandidateEvaluation`

每次评估绑定：

- candidate digest；
- current capability version；
- frozen pack digest；
- evaluator profile/version；
- current 与 candidate 的分项结果；
- safety gate、退化判断和最终 `eligible/rejected`；
- trace 和 backend provenance。

评估记录不可修改；重新评估创建新记录，不覆盖旧结果。

## 5. 冻结评估与独立性

### 5.1 评估资产

- 评估包作为 Governor 执行资产存放在 Governor Workspace 的受版本控制目录；
- 只保存脱敏后的内部案例和期望语义，不复制业务 Agent Workspace 的测试正文；
- 内容 digest 进入每次 evaluation；
- P2B 只覆盖一个归因能力缺口，案例必须同时包含成功、证据不足、跨 Agent 不适用、
  hostile backend-owned 字段和保守回退。

### 5.2 隔离 evaluator

- 在集中 `AgentJobType/spec` 注册独立 evaluation job；
- evaluator profile 只读冻结包、current 输出和 candidate 输出；
- 不读取候选研究过程、研究 prompt 或候选自评结论；
- 不能写 candidate、status、active pointer 或业务 Agent Workspace；
- formatter 返回具体 Pydantic OutputModel，不返回 `BaseModel` 或裸 dict；
- 确定性门先执行，模型辅助判断不能推翻安全失败。

### 5.3 Shadow 启用门

候选只有同时满足以下条件才标记 `eligible`：

- 所有安全、越权、恶意输入和离线场景通过；
- 至少一个目标归因指标相对 current 明确改善；
- 其他主要指标无实质退化；
- 改善能由候选方法解释，不来自放宽门禁或评估泄漏；
- 证据、candidate、pack 和 evaluator 版本完整。

P2B 没有任何从 `eligible` 到线上 active 的路径。

## 6. 数据流与字段所有权

```text
raw_agent_text
  -> AttributionFormatterOutput / CandidateFormatterOutput
  -> backend projected output
  -> boundary JSON
  -> immutable GovernorRunEvidence
```

| 所有者 | 字段 |
| --- | --- |
| Backend-owned | evidence/candidate/evaluation/version ID、Agent/improvement、job type、status、active pointer、pack/digest、trace、gate、时间、principal |
| Governor-owned | 缺口分析、方法正文、适用条件、反证、风险、验证语义 |
| Human-owned | 对业务产物的实际修订内容和候选评审意见 |
| Boundary-owned | SQLite JSON、HTTP response、文件包、日志、Langfuse metadata |

Prompt/Signature 不要求 Governor 输出 backend-owned 字段。Hostile formatter 输出中的伪造 ID、
status、version、gate 或 principal 必须被忽略。

## 7. 当前业务链路兼容

- 四阶段工作台仍只有反馈整理、归因分析、优化执行、测试发布；
- 当前 Governor job 成功/失败语义、fallback 和用户重试入口不变；
- 研究或评估失败只记录 shadow failure，不能阻断当前归因；
- 当前 Attribution/OptimizationPlan API 继续读取当前投影，不直接读取学习账本；
- 历史 `agent_jobs` 保持只读；新账本不恢复 create/claim/finish 写方法；
- Asset Registry 本阶段只可关联 evidence/candidate 的只读摘要，不直接复制方法正文为全局 active 资产；
- P2B 不修改业务 Agent 测试、发布和回滚规则。

## 8. 只读接口

P2B 可以新增只读 API，不能新增 activation mutation：

- `GET /api/governor/capability-version`：当前 active baseline 摘要；
- `GET /api/governor/method-candidates`：候选列表与 shadow 状态；
- `GET /api/governor/method-candidates/{candidate_id}`：证据、方法、评估和版本详情；
- `GET /api/governor/run-evidence/{evidence_id}`：不可变运行证据。

这些接口使用当前 API 认证，只服务开发观测与评审；没有可信 principal/role 前，不提供
create/activate/rollback 路由。OpenAPI 和前端生成类型若公开这些只读接口，必须从 Pydantic 契约派生。
P2B 不要求新增用户可见页面。

## 9. 架构与持久化

- 新表只表达 Governor learning domain，不恢复旧 `agent_jobs`、`TestDataset` 或 `EvalRun`；
- row record、运行时投影和 API response 分开建模；
- append-only evidence/evaluation 禁止 update/delete 主流程；
- 当前投影与 evidence 写入必须幂等；部分失败回滚，不在 DB 事务中调用模型或外部服务；
- 先运行 Governor 得到 typed result，再在短事务内写 evidence 和当前投影；
- active pointer 在 P2B 只读，P3 才设计 compare-and-swap 激活；
- migration 必须覆盖 fresh DB、历史 DB、重复执行和回滚。

## 10. 测试同步矩阵

| 行为变化 | 旧测试处置 | 新增测试 | 深度 |
| --- | --- | --- | --- |
| 业务产物追加证据 | Attribution/plan 测试 `REFACTOR` | 原始值、人工 diff、后续结果均保留 | store/service |
| capability version 绑定 | 当前 profile/version 测试 `KEEP` | baseline digest、每次 run 绑定 | contract |
| candidate lifecycle | 无 | 合法/非法转移、过期 pack | state machine |
| 独立 evaluator | 无 | context 隔离、current vs candidate、safety fail | integration |
| shadow failure | Governor fallback 测试 `KEEP` | 研究失败不阻断当前归因 | main flow |
| hostile output | 现有污染测试 `KEEP` | 伪造 version/gate/status/principal | security |
| append-only 与并发 | 无 | 重复 evidence、并发评估、事务回滚 | concurrency |
| 只读 API | 无 | 未知 ID、分页/详情、无 mutation route | API/OpenAPI |

新增测试同步进入 `tests/quality_policy.json` 的 `improvement-governance` owner、
`feedback-improvement-loop` capability 和相应 main-full/main-flow lane。

## 11. Runtime、env 与安全边界

- P2B 的账本、候选和冻结评估开发可与 P2A 并行；接入与阶段退出等待 P2A gateway 就绪；
- P2B 最终使用 P2A 当前部署选择的同一 `ManagedExecutionDriver`；
- 不增加独立 Runtime selector、Vite env 或模型 key；
- 宿主机与容器继续选择各自完整 env；无私有 `MODEL_PROVIDER_API_KEY` 时在启动 Governor 前
  返回稳定错误，不回显值；
- Governor 研究证据在进入模型前排除 `.env`、凭据、私有 endpoint 和跨 Agent 非授权原文；
- 不调用 WebFetch、搜索、远程论文服务或外部记忆；
- 不改变 `${HOME}/volume-agent-gov` 布局；新表使用当前 runtime DB，评估资产位于 Governor
  Workspace 受控目录。

## 12. 验证与退出门

验证：

1. learning records、state machine、migration 和 append-only 目标 pytest；
2. prompt/Signature/OutputModel/formatter/projection typed contract；
3. hostile backend-owned 字段污染和跨 Agent evidence 越权；
4. current vs candidate 冻结包评估；
5. 研究/evaluator 失败时现有四阶段归因正常；
6. 只读 API/OpenAPI 与历史 DB 投影；
7. `make codex-guard`、`make typecheck`、`make main-flow-test`；
8. 阶段提交前串行 `make test`；
9. 公共真实容器入口验证当前 Governor 主链路与 shadow 记录同时成立。

退出硬门：

- 至少一个真实四阶段结果形成完整不可变 `GovernorRunEvidence`；
- 一个 `ATTRIBUTION` candidate 在冻结包上得到 `eligible` 或 `rejected` 可解释结果；
- 每次 Governor 产物可反查精确 capability version 和 method revision；
- 人工修改前后内容均保留，不覆盖原始证据；
- 研究/evaluator 失败不影响当前业务输出；
- 仓库中不存在 activation/rollback mutation route，active pointer 未改变；
- 无网络研究、无敏感值外泄、无跨 Agent 原文污染。

## 13. P3 启动条件

只有同时具备以下条件，才评审人工启用：

- P1 至少一条安全测评改进发布闭环；
- P2B shadow 候选和 current 对比证据；
- 独立 evaluator 与冻结评估包稳定；
- 认证中间件能提供不可伪造的 principal 和 role；
- 原子激活、幂等、并发防重和回滚设计获批；
- UI 明确位于 Governor 能力治理面，不进入四阶段工作台。
