# 产品级代码与文档评审报告（第二轮 · R2）

> 评审范围：`master` 分支工作树（提交 `4225d5f` / v0.2.6），覆盖 `app/`、`frontend/`、`scripts/`、`tests/`、`docs/` 与根目录，**按要求不含 `docker/`**
> 评审日期：2026-05-29
> 评审视角：面向对象、高内聚低耦合、可维护性、扩展性、灵活性、鲁棒性、代码-文档一致性
> 评审方法：12 维并行深读智能体 → 对每条 finding 派独立对抗式校验智能体核对源码 → 主线再做事实复核（共 102 个智能体）。结构化产出 89 条，**确认 87、剔除 2**
> 与第一轮关系：本报告取代 [`CODE_AND_DOCS_REVIEW.md`](./CODE_AND_DOCS_REVIEW.md)（R1，重构前基线）。配套治理反思见 [`AGENT_GOVERNANCE_REFLECTION_AND_PLAN_R2.md`](./AGENT_GOVERNANCE_REFLECTION_AND_PLAN_R2.md)
> 整改注记（2026-05-30）：本文主体保留 R2 原始审查证据与行号，用作整改来源清单；部分证据路径已在后续整改中变化。当前已完成的相关变更包括：`feedback_jobs.py` 改名为 `feedback_prompts.py`；Pydantic 记录模型 `external_governance_models.py` / `feedback_compensation_models.py` 改名为 `external_governance_records.py` / `feedback_compensation_records.py`；不可达的 `CasesWorkspace.tsx`、`EvalWorkspace.tsx`、`ProposalWorkspace.tsx`、`EvidenceRunsDetails.tsx`、`AttributionDetails.tsx` 已删除，仅保留可达的批次主流程与 `AttributionResult.tsx`；profile 级 MCP 过滤与路径 hook 已落地。阅读本文时应把原始证据视为“发现时状态”，以当前代码和治理检查结果为整改准绳。
> 治理收敛注记（2026-05-30）：R2 暴露出的机器可判定体积债已继续清理：`feedback_workbench` / `feedback_cases` / `feedback_batches` / `optimization` 路由工厂已拆成资源组注册函数；`ClaudeRuntime.run` / `ClaudeRuntime.stream` 已抽出请求上下文、查询状态与收尾记录逻辑；`AppSettings` 已移除 SQLite 迁移后不再使用的旧文件存储目录属性。后续又清理了 runtime 纯转发私有方法、统一了路由内部 `status` 参数命名、把外部治理通知改为先落库 `sending` 审计记录再发送、复用 `JOB_IN_PROGRESS_STATES` 消除在途 job 状态字面量重复、抽出反馈单 payload 构造器消除手工创建与来源创建的字段重复，将 `FeedbackJobOrchestrator` / `FeedbackEvalRunner` 迁入 `app/services/`，并将 `FeedbackStore` facade 与 `feedback_*_store.py` mixin 迁入 `app/runtime/stores/`，将 response schema、内部 record、Langfuse/外部治理适配器、prompt 构造和输出归一化分别迁入 `app/runtime/response_schemas/`、`app/runtime/records/`、`app/runtime/integrations/`、`app/runtime/prompts/`、`app/runtime/normalizers/`，使 `app/runtime` 顶层 Python 文件数从 53 收敛到 25；测试工具 `feedback_store_test_utils.__all__` 已改为显式导出清单，store 测试也已由通配导入改为显式导入；`.codex/hooks.json` 已接入 Stop 治理硬门，`.github/workflows/governance.yml` 已覆盖当前 `master` 分支。当前 `scripts/check_codex_governance.py --mode fail` 输出为 `OK: no Codex governance regressions found.`。

严重度：🟧 高（架构/鲁棒性风险，应尽快整改）· 🟨 中（可维护性/一致性债，排期整改）· 🟩 低（建议项）。本轮**无架构级严重(S)问题**——R1 的 4 个上帝模块已系统性拆分。

---

## 0. 重构成效：R1 体积债已系统性清偿

R1 评审 → 治理护栏硬化 → 拆分，这条提交链（`eae5716`→`850e33e`→`126a2e6`/`88fd96e`→`2210f95`）是**可观测的成功**。四个上帝模块全部回落到阈值附近：

| 模块 | R1 行数 | R2 行数 | 处置 |
|---|---|---|---|
| `app/runtime/feedback_store.py` | 5022 | **298** | 拆为 ~15 个 `feedback_*_store.py`，本体退化为合理的组合根（共享 Session + Mixin 装配） |
| `app/runtime/claude_runtime.py` | 1403 | **791** | Langfuse/Job/Eval/Activity/格式化均委派给独立协作者 |
| `frontend/.../ExternalFeedbackWorkspace.tsx` | 5124 | **405** | 拆为 `feedback-workspace/` 下多个子工作台 |
| `app/main.py`（曾 1659/76 路由） | 1659 | **133** | 拆为 15 个 `app/routers/*`，main 仅做装配 |

本轮已确认问题中**没有任何一条是"新增的 800+ 行手写文件"**——体积膨胀这条主线债被有效拦住。这印证了"可机器判定的行数阈值 + 拆分动作"确有因果效力（详见配套反思 R2 §2）。

---

## 1. 总览与评分

| 维度 | R1(重构前) | R2(现状) | 主要残留风险 |
|---|---|---|---|
| 模块拆分(SRP) | 🟥 不及格 | 🟩 良好 | 已完成 runtime 子包拆分；`useFeedbackWorkspaceActions` 已收敛到 319 行并只保留可达主流程。剩余关注点是后续增长时继续拆领域 hook |
| 内聚 | 🟧 偏低 | 🟩 较好 | prompt/normalizer/response schema/record 已拆出；`schemas.py` 与 `feedback_schemas.py` 作为核心契约文件保留，后续只在字段继续扩张时再拆 |
| 耦合 | 🟧 偏高 | 🟨 一般 | profile 常量、编排器模板、状态机和服务边界已收敛；Mixin 间横向调用仍是 store 聚合实现的主要长期耦合点 |
| 扩展性 | 🟧 偏低 | 🟨 一般 | 新增 Job 仍需复制整套 try/except(FO-2)；profile 声明的隔离策略字段无人消费(RC-1) |
| 鲁棒性 | 🟧 偏低 | 🟨 提升中 | 状态机 dead guard 已修复并补测试；执行应用、清理事务、外部通知、鉴权与并发去重已补关键守卫。剩余风险主要是更细粒度的跨进程锁与 schema 多轨长期治理 |
| 安全 | 🟨 一般 | 🟨 一般 | 鉴权失败错误体不含 `error_code`，游离于域错误信封(BA-3)；401 分支无测试(TS2-4) |
| 文档一致性 | 🟨 一般 | 🟨 一般 | `openapi.json`/README 与代码已同步；但治理与产品文档**自身**已 stale(CD-1~6) |
| 测试覆盖 | 🟨 一般 | 🟨 一般 | 回滚/补偿覆盖扎实；但状态机非法转移、Agent 超时、鉴权、并发去重未测(TS2-1~5) |

**确认问题分布**：🟧 高 5 条 · 🟨 中 40 条 · 🟩 低 42 条，合计 87。按维度：

| 维度 | 高 | 中 | 低 | 小计 |
|---|--:|--:|--:|--:|
| 后端运行时核心层 | 0 | 5 | 6 | 11 |
| 后端 API 层 | 0 | 2 | 5 | 7 |
| 反馈存储层 | 0 | 7 | 1 | 8 |
| 反馈编排与作业层 | 0 | 5 | 1 | 6 |
| Schema 与响应模型一致性 | 0 | 3 | 3 | 6 |
| 状态机与状态治理 | 2 | 2 | 3 | 7 |
| 外部治理与支撑模块 | 0 | 3 | 3 | 6 |
| 前端组件 | 1 | 3 | 4 | 8 |
| 前端 API 与类型 | 0 | 1 | 6 | 7 |
| 项目目录结构与模块边界 | 0 | 4 | 4 | 8 |
| 测试质量与覆盖 | 2 | 3 | 2 | 7 |
| 代码与文档一致性 | 0 | 2 | 4 | 6 |

---

## 2. 一句话结论

> **体积债已还清，R2 暴露出的最高风险已完成代码整改。** R1 的 4 个上帝模块被真实拆小，本轮 R2 又继续补齐了状态机转移表、case/eval_run/proposal 生命周期守卫、执行应用幂等与补偿留痕、外部通知审计、Agent profile 策略落地、OpenAPI 类型单源、不可达前端分支删除与 runtime 子包拆分。本文下方 finding 保留原始审查证据；阅读时应以顶部整改注记与第 16 节当前状态为准。

---

## 3. 后端运行时核心层

> 范围：`app/runtime/{claude_runtime,settings,runtime_db,session_store,agent_*,policy,config_mapping,errors,execution_targets}.py`

**维度小结**：拆分后边界总体清晰：claude_runtime 已把 Langfuse/Job/Eval/Activity/OutputFormatter 委派给独立协作者，runtime_db 的 SQLite WAL/busy_timeout/连接池/线程缓存配置专业，session_store 用上下文管理器保证事务边界，settings 用 alias+property 把环境变量与裸字面量解耦，errors 为 feedback 域提供了分层异常并接入统一 HTTP 处理器。但仍存在三类残留问题：(1) 运行时与 stores 之间通过 profile 名称字符串字面量耦合、profile 上声明的隔离策略字段是“死策略”给出虚假沙箱感；(2) 机械碎片化残留——claude_runtime 上一批纯转发/已死的委派方法、run 与 stream 大段样板重复、Langfuse 编排与适配器方法重复实现；(3) 异常分层不覆盖运行时态（维护中以裸 RuntimeError 泄漏为 500）、settings 目录派生逻辑在两处分歧。

#### [RC-1] 🟨 中 AgentRuntimeProfile 声明的隔离/沙箱策略字段几乎全部未被任何代码消费（虚假沙箱）

- **位置**：`app/runtime/agent_profiles.py:35-38, 60-67, 80-118`
- **原则**：鲁棒性/扩展性/一致性
- **证据**：dataclass 字段 readable_paths/writable_paths/denied_paths/allowed_mcp_servers/langfuse_observation_name 在每个 profile 都被精心赋值（如 main-agent: denied_paths=(attribution_analyzer_claude_root, ...), allowed_mcp_servers=("sec-ops-data","security-kb")）。但全仓 grep（除 agent_profiles.py 自身）对这些字段的引用为 0：`grep -rn readable_paths|writable_paths|denied_paths|allowed_mcp_servers|.langfuse_observation_name app/ tests/`（除定义处）无任何命中；仅 max_output_bytes / max_runtime_seconds 在 agent_job_runner.py:96-116 被使用。同时 _build_options/build_options 直接把整份 .mcp.json 透传给 SDK（claude_runtime.py:346 mcp_servers=str(profile.mcp_config_path)），并未按 allowed_mcp_servers 过滤。
- **修复**：二选一：要么删除这些未消费字段，避免给读者“子代理被路径/MCP 沙箱隔离”的错觉；要么在 build_options 中真正落地——按 allowed_mcp_servers 过滤 mcp_servers，并通过 PreToolUse hook/can_use_tool 校验 Read/Write/Edit 目标是否落在 readable_paths/writable_paths 且不在 denied_paths。当前状态属于安全策略层面的‘已声明未执行’。

#### [RC-3] 🟨 中 ClaudeRuntime 残留一批纯转发且已无人调用的‘死委派’方法（机械碎片化残留）

- **位置**：`app/runtime/claude_runtime.py:412-432, 494-500`
- **原则**：SRP/DRY/易维护
- **证据**：_direct_schema_candidate(412)、_format_agent_text(415)、_raw_agent_text_payload(431)、_evaluate_eval_case(494)、_eval_tool_names(499) 全部仅转发给 AgentJobRunner/FeedbackEvalRunner。grep 证实生产代码与 tests 均不再调用这些方法（`grep -rn _direct_schema_candidate|_raw_agent_text_payload|_format_agent_text|_eval_tool_names|_evaluate_eval_case app/ tests/` 仅命中其在 claude_runtime 的定义与真正 owner FeedbackEvalRunner 内部 self._ 调用）。逻辑已迁出，转发壳被遗留。
- **修复**：删除这些无调用方的转发方法。保留的薄委派（如 _build_job_options 有测试用、_selected_eval_cases 有测试用）可留；其余应清理，避免读者误以为 ClaudeRuntime 仍承担解析/打分职责。

#### [RC-4] 🟨 中 维护中状态以裸 RuntimeError 泄漏为 500，未纳入异常分层（错误分层不完整）

- **位置**：`app/runtime/claude_runtime.py:243-245, 508, 642`
- **原则**：一致性/鲁棒性
- **证据**：_raise_if_version_maintenance(243) `raise RuntimeError("Agent version maintenance is in progress; retry after restore completes.")`，在 run(508) 与 stream(642) 入口同步调用。但 error_handlers.py:9-18 只注册了 `@app.exception_handler(FeedbackStoreError)`，errors.py 的异常树根为 FeedbackStoreError(ValueError) 且全部面向 feedback 域，没有运行时/可重试类别。相对地，agent_version_store.py:203/503/505 对完整性问题用的是结构化 AgentVersionIntegrityError。
- **修复**：为‘维护中/服务暂不可用’定义可重试异常（如 status_code=503 的 RuntimeUnavailableError，挂入统一处理器），让客户端拿到结构化 error_code 与 Retry-After，而非裸 500。agent_job_runner.py:97 的‘输出超限’RuntimeError 同理可归类。

#### [RC-6] 🟨 中 run 与 stream 之间存在大段遥测/持久化样板重复（DRY）

- **位置**：`app/runtime/claude_runtime.py:505-637, 639-791`
- **原则**：DRY/易维护
- **证据**：run(505) 与 stream(639) 各自重复：初始化 run_id/created_at/telemetry_input/messages/answer_parts/usage/...（515-523 vs 649-657）、双层 _start_langfuse_observation span+generation（525-537 vs 659-682）、相同的 query 消费循环（540-556 vs 685-721）、相同的 _runtime_output_payload + _update_langfuse_observation + _set_langfuse_trace_io + _record_feedback_run + session.save 收尾（563-624 vs 736-789）。两份逻辑仅在‘是否 yield 事件’上不同。
- **修复**：抽出共享的‘会话准备/遥测开场/结果落库/遥测收尾’为私有 helper（如 _begin_run()/_finalize_run()），run 与 stream 仅保留各自的消费/产出差异，降低双份维护成本与漂移风险。

#### [RC-7] 🟨 中 异常抑制依赖错误信息文案前缀做跨模块字符串匹配（脆弱耦合）

- **位置**：`app/runtime/claude_runtime.py:114-118`
- **原则**：耦合/鲁棒性
- **证据**：_should_suppress_exception(114): `text = str(exc); return text.startswith("Claude Code returned an error result:")`。该前缀文案由另一个模块 agent_job_runner.py:168 生成：`return [f"Claude Code returned an error result: {subtype}"]`。run(558)/stream(723) 据此决定是否吞掉异常。文案一旦在 result_errors 处改写，抑制逻辑将静默失效（异常会被重复记录或反之）。
- **修复**：改用类型/标记判定而非文案匹配：例如让 SDK 错误结果走自定义异常类型或在 errors 列表里携带结构化 code，_should_suppress_exception 判断 code 而非 str(exc) 前缀。

#### [RC-10] 🟩 低 runtime_version="0.2.6" 等版本号在多处硬编码，易随发布漂移

- **位置**：`app/main.py:41, 62`
- **原则**：DRY/一致性
- **证据**：main.py:41 `runtime_version="0.2.6"`、:62 `version="0.2.6"`；feedback_store.py:62 默认参数 `runtime_version: str = "0.2.6"`；docker-compose.yml:56/105 镜像 tag 0.2.6。该值会落库进 FeedbackJobModel.runtime_version（runtime_db.py:180），用于复现性追踪，但分散硬编码使 main.py 与 feedback_store 默认值可独立漂移。
- **修复**：集中到单一来源（如从包元数据 importlib.metadata.version 或 settings 暴露 app_version），main.py 与 feedback_store 默认值统一引用，避免发布时漏改某处导致 trace 里记录错误版本。

#### [RC-11] 🟩 低 _build_options 内局部 import uuid 遮蔽模块级 import uuid

- **位置**：`app/runtime/claude_runtime.py:4, 376`
- **原则**：易维护/DRY
- **证据**：模块顶部 line 4 `import uuid`；_build_options 内 line 376 又 `import uuid` 后才 `uuid.UUID(session.session_id)`。局部 import 多余并遮蔽顶层导入，属拆分/搬迁残留。
- **修复**：删除 line 376 的局部 `import uuid`，直接使用模块级导入。

#### [RC-2] 🟩 低 profile 名称在 stores 层以裸字符串字面量耦合，绕过 agent_profiles 常量

- **位置**：`app/runtime/feedback_job_store.py:112, 171; 另见 feedback_execution_store.py:67,88; feedback_batch_plan_store.py:93`
- **原则**：耦合/DRY
- **证据**：feedback_job_store.py:112 `profile_name="attribution-analyzer"`、:171 `profile_name="proposal-generator"`；feedback_execution_store.py:67/88 `"profile_name": "execution-optimizer"` 与 `profile_name="execution-optimizer"`。该 profile_name 随后在 agent_job_runner.py:85 被用作字典键：`profile = self.profiles[profile_name]`，而 self.profiles 的键来自 agent_profiles.py 常量 ATTRIBUTION_ANALYZER_PROFILE 等。orchestrator（feedback_job_orchestrator.py:7-10,42,58）正确 import 了常量，但 stores 仍硬编码字面量。
- **修复**：stores 改为从 agent_profiles import ATTRIBUTION_ANALYZER_PROFILE/PROPOSAL_GENERATOR_PROFILE/EXECUTION_OPTIMIZER_PROFILE 并引用常量，消除‘改名一处即静默断链’的风险（self.profiles[profile_name] 取键会 KeyError）。

#### [RC-5] 🟩 低 settings 目录派生逻辑在 model_post_init 与 get_settings 两处重复且条件分歧

- **位置**：`app/runtime/settings.py:122-140, 305-333`
- **原则**：DRY/一致性
- **证据**：model_post_init(127) 仅当 `attribution_analyzer_workspace_dir == 默认值 且 main_workspace_dir != /main-workspace` 才派生；get_settings(309) 则用 `if "ATTRIBUTION_ANALYZER_WORKSPACE_DIR" not in os.environ:` 无条件覆盖。两套针对同四个 workspace/claude-root 目录的派生条件不同（默认值相等判定 vs 环境变量名是否存在），可能在‘自定义 main 且同时显式设置子目录别名’场景下相互覆盖产生不一致结果。
- **修复**：将子 profile 目录派生收敛到单一来源（推荐放进 model_post_init 或一个独立 _derive_profile_dirs() 并在两处共用），删除 get_settings 中的重复块；统一‘显式优先、否则按 main 派生’的判定。

#### [RC-8] 🟩 低 RuntimeLangfuseClient.start_observation 与 ClaudeRuntime._start_langfuse_observation 重复实现，适配器方法成死代码

- **位置**：`app/runtime/claude_runtime.py:265-273`
- **原则**：DRY/易维护
- **证据**：claude_runtime.py:265-273 _start_langfuse_observation 内联实现 get_client+try start_as_current_observation+except 返回 nullcontext(None)。runtime_langfuse.py:87-95 已存在等价的 start_observation。grep 显示 run/stream 只调用 self._start_langfuse_observation（claude_runtime.py:525/532/659/677），从不调用 self.langfuse.start_observation，故适配器里的 start_observation 是死方法。
- **修复**：让 _start_langfuse_observation 直接委派 self.langfuse.start_observation（与 update/set_trace_io/flush 的委派风格一致），或删掉适配器里的重复方法，二者只保留一处实现。

#### [RC-9] 🟩 低 领域异常基类继承自内建 ValueError，存在被宽泛 except ValueError 误吞的隐患

- **位置**：`app/runtime/errors.py:4-8`
- **原则**：鲁棒性/可维护
- **证据**：`class FeedbackStoreError(ValueError)`，其下 BusinessRuleViolation/ConflictError/NotFoundError 等全部是 ValueError 子类。仓内已有多处 `except ValueError`（claude_runtime.py:381 uuid 校验、config_mapping.py:18 relative_to、feedback_store.py:294 fromisoformat）。当前均为窄作用域故未触发，但任何未来在 try 块内调用会抛领域异常的代码都会被这些 except 静默吞掉并改变 HTTP 语义。
- **修复**：将 FeedbackStoreError 基类改为继承 Exception（而非 ValueError），保留 status_code/error_code 契约不变；既消除误吞风险，也避免领域异常与输入校验异常语义混淆。

---

## 4. 后端 API 层

> 范围：`app/main.py + app/routers/*`

**维度小结**：路由分组拆分总体是干净的高内聚边界：每个 router 用工厂函数显式注入依赖、统一 prefix/tags、统一通过 error_helpers + 单一 FeedbackStore 门面（13 个 mixin 组合）与统一 exception handler 收口域错误，避免了机械碎片化；但 feedback_batches.py 仍把"生成执行计划→应用→记录批次结果"的业务编排内联进路由层并重复两遍，optimization.py 的 mark-applied 把状态机前置校验与跨 store 的快照副作用放在路由里且非原子，是本维度最值得整改的可维护性/鲁棒性问题，另有若干一致性与死代码小问题。

#### [BA-1] 🟨 中 批次执行编排逻辑内联进路由层且重复两份（应下沉到 ExecutionApplicationService）

- **位置**：`app/routers/feedback_batches.py:124-154, 168-240`
- **原则**：SRP/DRY/耦合
- **证据**：approve 流程：execution_job = await runtime.run_execution_job(task["optimization_task_id"], force=True) ... if execution_job.get("status") == "ready": apply_result = execution_application.apply_ready_execution_job(...) ... batch = feedback_store.record_batch_execution_result(...)；execute 流程几乎逐行重复同一序列：execution_job = await runtime.run_execution_job(task["optimization_task_id"], force=req.force) ... if execution_job.get("status") == "ready": apply_result = execution_application.apply_ready_execution_job(...) ... batch = feedback_store.record_batch_plan_task_execution_result(...)
- **修复**：在 ExecutionApplicationService（或新增 BatchExecutionService）上提供一个 run_and_apply_execution_job(task_id, *, force, apply_note) -> {execution_job, apply_result} 方法，封装 run_execution_job→判断 ready→apply_ready_execution_job 的编排；两个 batch 路由各自只负责取 batch/plan_task、调用该方法、再调用对应的 record_batch_*_result，路由回到 thin controller。

#### [BA-2] 🟨 中 mark-applied 在路由层做状态机校验且快照副作用与标记非原子，存在并发下孤儿快照与规则分散

- **位置**：`app/routers/optimization.py:236-252`
- **原则**：鲁棒性/SRP/一致性
- **证据**：task = ensure_found(task, ...); if task.get("applied_agent_version_id"): return task; if task.get("status") not in {"pending_execution", "failed", "needs_human_review"}: raise_conflict(...); version = agent_version_store.create_snapshot(reason="proposal_applied", ...); updated = feedback_store.mark_task_applied(task_id, agent_version=version, note=req.note)。create_snapshot 会落盘 tar.gz bundle+manifest（agent_version_store.py:111 起），而 feedback_store.mark_task_applied(feedback_task_store.py:97-101) 内部仅再做 applied_agent_version_id 幂等判断、不校验状态前置条件。
- **修复**：将状态前置校验（哪些 status 可 mark-applied）下沉到 mark_task_applied，使其与 DB 事务一起做合法转移校验并幂等返回；快照创建应在 store 确认任务确实进入 applied 后或在同一编排服务内执行，避免读-判-快照-标记四步跨 store 非原子导致并发双请求各创建一个孤儿 bundle。最简做法：把这段编排移入 ExecutionApplicationService.mark_task_applied_manually(task_id, note) 并让 store 在事务内校验状态。

#### [BA-3] 🟩 低 鉴权失败错误体不含 error_code，与已统一的域错误信封不一致

- **位置**：`app/main.py:91-95`
- **原则**：一致性
- **证据**：def require_api_key(...): ... raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")。而 register_error_handlers 对 FeedbackStoreError 统一返回 {"detail": ..., "error_code": ...}（error_handlers.py:12-17），docs/CODE_AND_DOCS_REVIEW.md:379 明确记录了"统一 exception handler 返回 error_code 与 detail"的治理目标。
- **修复**：为 401 也走结构化信封：要么定义 AuthError 并由统一 handler 返回 {detail, error_code:"UNAUTHORIZED"}，要么注册 StarletteHTTPException/RequestValidationError handler 统一补 error_code 字段，使前端 readError() 对所有错误路径行为一致。

#### [BA-4] 🟩 低 plan_task 查找逻辑重复实现：已有 batch_plan_task 辅助函数却内联重写一遍

- **位置**：`app/routers/feedback_batches.py:36-41, 180-188`
- **原则**：DRY
- **证据**：已定义 def batch_plan_task(batch, plan_task_id)：for item in (plan or {}).get("tasks") or []: if isinstance(item, dict) and str(item.get("plan_task_id") or "") == plan_task_id: return item；但 execute_feedback_optimization_plan_task 内又用 next((item for item in (plan or {}).get("tasks") or [] if isinstance(item, dict) and str(item.get("plan_task_id") or "") == plan_task_id), None) 重新实现同一查找。
- **修复**：删除 180-188 的内联实现，直接 plan_task = batch_plan_task(batch, plan_task_id)；并将 189-190 的 if not plan_task: ensure_found(plan_task, ...) 改为 ensure_found(plan_task, "Optimization plan task not found") 以消除冗余守卫。

#### [BA-5] 🟩 低 raise_bad_request 为死代码

- **位置**：`app/routers/error_helpers.py:22-23`
- **原则**：DRY/可维护
- **证据**：def raise_bad_request(detail: str) -> None: raise BusinessRuleViolation(detail)；全仓 grep 仅此一处定义，无任何调用方（routers/services 全部使用 require_request 或 raise_conflict）。
- **修复**：删除 raise_bad_request，或若意图保留为公共抛错入口则在文档中标注并让 require_request 复用它；当前未用应删除以减少 API surface。

#### [BA-6] 🟩 低 status 查询参数内部命名不一致（status vs status_filter+alias）

- **位置**：`app/routers/optimization.py:53, 110, 140, 189`
- **原则**：一致性
- **证据**：optimization.py 与 feedback_cases.py 用 status: str | None = None；而 eval.py:42/82、feedback_workbench.py:130、feedback_batches.py:49 用 status_filter: str | None = Query(default=None, alias="status")。对外 query 名都是 status，但内部命名风格分裂。
- **修复**：统一为一种写法（建议统一用直白的 status: str | None = None，因为它们不与内置标识冲突），减少阅读时"为何此处要 alias"的认知负担；若坚持 alias 则全量统一。

#### [BA-7] 🟩 低 main.py 模块级 health() 与 core 路由 /health 重复，仅为单测保留

- **位置**：`app/main.py:132-134`
- **原则**：DRY/SRP
- **证据**：async def health() -> dict[str, Any]: return build_health_payload(settings=settings, app=app, agent_version_store=agent_version_store)；该函数未通过 include_router/add_api_route 注册为任何路由（真正 /health 在 routers/core.py:30-37），全仓唯一调用方是 tests/test_claude_runtime.py:359 的 main.health()。
- **修复**：删除 main.py 的模块级 health()，让测试改为调用 build_health_payload(...) 或直接对 /health 端点做请求测试，避免在入口模块留下与路由重复且仅服务于测试的影子实现。

---

## 5. 反馈存储层

> 范围：`app/runtime/feedback_*_store.py + agent_version_store.py`

**维度小结**：拆分总体是"有内聚边界"的：feedback_store.py 退化为合理的组合根（共享 Session/工具方法 + Mixin 装配），而非纯转发门面，各 *_store 按领域聚合（source/case/job/eval/execution/proposal/compensation/external_governance）职责较清晰，事务多用 *_row 内联函数在单个 Session.begin() 内组合实现跨表原子更新（如 complete_proposal_job、mark_execution_job_applied），这点做得不错。但 Mixin 模式带来强隐式耦合（各 Mixin 大量 self.调用其他 Mixin 的私有方法，无接口约束），状态机对 batch/task 完全失效，存在跨多事务的非原子清理、SOC 事件幂等的 TOCTOU 竞态、事务内做文件系统 rmtree、以及反馈用例构造与列表查询的重复样板/N+1 等可维护性与鲁棒性问题。

#### [FS-1] 🟨 中 batch/task 状态机的合法转移校验完全失效（声明了状态集合却无转移表）

- **位置**：`app/runtime/state_machines.py:75-120`
- **原则**：鲁棒性/状态机合法转移
- **证据**：_TRANSITIONS 只定义了 "job" 与 "execution_job" 两台机器；"batch"/"task" 只进 _KNOWN_STATES。validate_transition 中：`transitions = _TRANSITIONS.get(machine); if transitions is None: return`。而 feedback_batch_store.py:308 `validate_transition("batch", row.status, status)` 与 feedback_task_store.py:143 `validate_transition("task", row.status, status)` 都依赖它做合法性校验。
- **修复**：为 _TRANSITIONS 补全 "batch" 与 "task" 的合法转移图（例如 draft->attribution_running/needs_human_review、execution_planning->execution_ready->applied_pending_regression->regression_running->completed 等）。或者，如果当前确实只想校验状态枚举合法性，应在 validate_transition 与调用点注释明确写出'仅校验目标状态枚举，不校验转移合法性'，避免读者误以为 batch/task 受转移约束。当前实现让 _update_batch_row/_update_task_payload_row 可把批次从任意状态直接改到任意已知状态（如 completed->draft），与 job/execution_job 的严格约束自相矛盾。

#### [FS-2] 🟨 中 ingest_soc_event 的幂等是 check-then-insert（TOCTOU 竞态），且用 db.add 而非 merge，与文档幂等承诺冲突

- **位置**：`app/runtime/feedback_source_store.py:159-220`
- **原则**：鲁棒性/一致性（幂等）
- **证据**：先 `existing = self.find_event(req.event_id)`（160 行，独立读），未命中后在另一个事务块里 `db.add(SocEventModel(event_id=event["event_id"], ...))`（182 行）。同文件 create_signal 用的是 `db.merge(...)`（113 行）。docs/FEEDBACK_OPTIMIZATION_MULTI_AGENT_ARCHITECTURE.md:1301 'event_id 必须由 source_system 保证幂等'、1653 'POST /api/soc-events 对 event_id 幂等'。
- **修复**：将去重检查与插入放进同一事务，并改用 db.merge(SocEventModel(...)) 或对 SocEventModel.event_id 依赖主键约束 + 捕获 IntegrityError 回退到 duplicate 分支；同时把 PendingCorrelation 的创建一并纳入同一原子语义。否则并发重复上报会以未处理的 IntegrityError 形式 500，违背文档幂等承诺。

#### [FS-3] 🟨 中 discard_current_attribution / reset_batch_attribution 跨多个独立事务，无整体原子性与回滚

- **位置**：`app/runtime/feedback_job_store.py:341-360`
- **原则**：鲁棒性（事务边界/回滚）
- **证据**：discard_current_attribution 中：`if attribution_job_id: self._discard_job(attribution_job_id)`（348）与 `self._discard_proposal_job(proposal_job_id)`（350）各自 `with self.Session.begin()`（见 _discard_job 551-558、_discard_proposal_job 560-579），随后又单开 `with self.Session.begin() as db:` 更新 case 行（351-359）。reset_batch_attribution（feedback_batch_store.py:150-178）进一步对每个 feedback_case 循环调用本方法 + _discard_batch_draft_artifacts + _update_batch，全程跨数十个独立事务。
- **修复**：把'删除归因 job + 删除下游 proposal/外部项 + 复位 case 状态'重构为单个 *_row 组合，在一个 self.Session.begin() 内完成（仓库已有这种内联 *_row 模式，如 complete_proposal_job）。reset_batch_attribution 同理应在一个事务内完成批次内所有 case 与 batch 的复位，避免中途异常留下'归因已删、case 状态未复位'的半成品。

#### [FS-4] 🟨 中 事务块内执行文件系统删除（_cleanup_job_tmp/rmtree），提交前的不可回滚副作用

- **位置**：`app/runtime/feedback_store.py:223-239`
- **原则**：鲁棒性（事务边界）
- **证据**：_discard_batch_draft_artifacts：`with self.Session.begin() as db:` 内部 `for execution in execution_rows: db.delete(execution); self._cleanup_job_tmp(execution.execution_job_id)`（225-228）以及 `self._cleanup_job_tmp(execution_job_id)`（236）。_cleanup_job_tmp 即 `shutil.rmtree(self.tmp_jobs_dir / job_id, ...)`（feedback_job_store.py:548-549）。
- **修复**：将临时目录清理移到事务成功提交之后执行：先在事务内收集待清理的 job_id，commit 后再逐个 _cleanup_job_tmp。否则若事务后续 db.delete/commit 失败回滚，磁盘上的临时证据/输入文件已被删除，DB 与文件系统出现不一致。

#### [FS-5] 🟨 中 Mixin 之间通过 self.* 大量隐式横向调用，无接口契约，是强隐式耦合

- **位置**：`app/runtime/feedback_batch_store.py:43-95`
- **原则**：高内聚低耦合/扩展性
- **证据**：FeedbackBatchStoreMixin 直接调用属于其他 Mixin 的方法：`self._prepare_feedback_case_for_source`（source）、`self.find_eval_case`/`self._add_eval_case_row`（eval）、`self._case_model_from_dict`（case）、`self._upsert_feedback_source_annotation`（source）、`self.find_task`/`self.get_execution_job`/`self.get_eval_run`（task/execution/eval，_batch_to_dict 280-294）。全仓此类跨 Mixin self. 调用 60+ 处。这些依赖只有在 FeedbackStore 同时混入全部 Mixin 时才成立，单个 *_store 文件无法独立理解或单测。
- **修复**：承认这是'一个聚合 + 多个 partial 实现文件'而非真正独立模块；至少为跨 Mixin 依赖定义一个显式协议（typing.Protocol，声明 find_case/find_task/_update_batch_row 等被复用的方法签名），各 Mixin 用该 Protocol 做类型标注，使横向依赖在静态检查中可见、可约束；长期可考虑把 case/eval/task 等真正下沉为注入式协作对象而非平铺 Mixin。

#### [FS-6] 🟨 中 feedback_case 载荷构造在两处近乎逐字段重复（DRY）

- **位置**：`app/runtime/feedback_source_store.py:517-543`
- **原则**：易维护（DRY）
- **证据**：_new_feedback_case_for_source 构造的 dict（feedback_case_id/created_at/updated_at/status/title/priority/source_ids/signal_ids/event_ids/pending_correlation_ids/run_ids/session_ids/alert_ids/case_ids/evidence_package_ids/attribution_job_ids/proposal_job_ids）与 feedback_case_store.py:56-82 create_case 的字典逐字段几乎一致（同样 18 个键、同样的 _unique_strings(... run_id/matched_run_id/resolved_run_id ...) 聚合表达式）。
- **修复**：抽取一个共享构造器，如 _assemble_feedback_case(records, *, signals, events, pending, title, priority) 放在 case_store，由 create_case 与 _new_feedback_case_for_source 共用。否则新增字段（例如又一类 *_ids）必须同时改两处，极易漏改导致两条创建路径产出结构不一致。

#### [FS-7] 🟨 中 列表查询存在 N+1 / 嵌套会话：list_proposals 与 list_feedback_sources

- **位置**：`app/runtime/feedback_proposal_store.py:78-79,135-147`
- **原则**：鲁棒性/可维护（性能）
- **证据**：list_proposals 在 `with self.Session() as db:` 内遍历行，但每行调用 _proposal_to_dict，后者又新开 `with self.Session() as db:`（138 行）查 latest review——每个 proposal 一次独立会话。list_feedback_sources（feedback_source_store.py:309-344）对每个 signal/event/pending 走 _source_row，每行又 `find_eval_case`（新会话）+`get_job`（新会话）（feedback_source_store.py:625-627），并且 _cases_by_source_id/_find_case_for_source_id 各自 `list_cases(limit=1000)`（598-612）。
- **修复**：list_proposals 改为一次性批量查 ProposalReview（按 proposal_id 分组取最新）后在内存合并；list_feedback_sources 预取 eval_cases 与 attribution jobs 的映射表（类似已有的 _source_annotations_by_key/_cases_by_source_id），避免逐行开会话。当前规模 limit 默认 100~500，行数放大后查询数会成倍增长。

#### [FS-8] 🟩 低 _supersede_case_proposals 与 _discard_proposal_job 对外部治理项的处理语义不一致

- **位置**：`app/runtime/feedback_proposal_store.py:149-197`
- **原则**：一致性/可维护
- **证据**：_supersede_case_proposals 对 ExternalGovernanceItemModel 只把 pending_notification/notification_failed 标记为 superseded（178-196），保留行；而 feedback_job_store.py:560-579 _discard_proposal_job 对同类外部项是物理 `db.delete(item)` 连同其 ExternalNotificationModel 一起删除。两条路径都因 proposal 失效触发，外部治理项一边软标记保留、一边硬删除。
- **修复**：统一外部治理项在'方案被取代/丢弃'时的处置策略（建议统一为软标记 superseded 以保留审计轨迹，或统一硬删除）并抽成一个共享 helper，避免同一概念两种不可预期的副作用，给排查'外部任务为何消失/为何还在'埋坑。

---

## 6. 反馈编排与作业层

> 范围：`feedback_job_orchestrator/factory, agent_job_runner, feedback_eval_runner, app/services/execution_application.py`

**维度小结**：拆分总体是干净的：状态机已集中到 state_machines.py 并被各 store 一致调用，FeedbackJobFactory/AgentJobRunner/FeedbackJobOrchestrator 职责边界基本清晰，补偿记录用 Pydantic 模型做了不变量校验。但编排器四个 run_*_job 方法存在大段样板重复且通过下划线魔法键与 store 隐式耦合；执行应用的"写文件+落库"跨事务且无幂等/锁导致并发下可重复应用（文档 B-S4 已记录但未修；编排器与 store 之间还存在 fail_job/fail_execution_job 等签名不一致，是真正影响扩展性与鲁棒性的点。

#### [FO-1] 🟨 中 执行应用写文件与状态落库跨事务且无幂等键/锁，并发下可重复应用

- **位置**：`app/services/execution_application.py:45-112, 198-263`
- **原则**：鲁棒性（并发/事务边界、幂等）
- **证据**：apply_ready_execution_job 先 `if job.get("status") != "ready": raise ...`（60-61），再 create_snapshot(pre_execution)（70），再 self.apply_execution_operations(...) 写 workspace 文件（76 / 247-250 `dest.write_bytes(data)`），最后 self.feedback_store.mark_execution_job_applied(...)（92）在另一个事务里落库。状态检查与落库之间没有锁、也没有原子的 compare-and-set/幂等键；只有事后 _compensate_post_write_failure 在落库失败后回滚文件。
- **修复**：为 ready->applied 引入应用级幂等键或 SELECT...FOR UPDATE 等价的 compare-and-set（在落库事务内重新校验 status==ready 且 applied_agent_version_id 为空，否则中止），或对单个 task 加 asyncio.Lock/进程内互斥；把'写文件成功后必须落库'用 outbox/saga 表达，而非仅靠事后补偿。docs/CODE_AND_DOCS_REVIEW.md B-S4 已记录此问题但代码未修复。

#### [FO-2] 🟨 中 编排器四个 run_*_job 方法大段样板重复（DRY），新增 Job 类型需复制整套 try/except 流程

- **位置**：`app/runtime/feedback_job_orchestrator.py:41-190`
- **原则**：DRY/扩展性（开闭原则）
- **证据**：run_attribution_job / run_proposal_job / run_batch_optimization_plan / run_execution_job 四个方法结构几乎逐行相同：create_*_job -> `if job.get("_reused_existing") or job.get("status") != "queued": return ...` -> start_*_job -> `if not self.provider_configured(): complete_*(offline_*)` -> `try: raw = await self.run_profile_json(...); complete_*(raw) except asyncio.TimeoutError: fail_*(... AGENT_TIMEOUT) except Exception: fail_*(... AGENT_RUNTIME_ERROR)`。仅 profile_name / prompt / expected_schema_version / job_type / ID 键不同。
- **修复**：抽出一个泛化 _run_job(job, *, profile_name, build_prompt, schema_version, job_type, start_fn, complete_fn, fail_fn, offline_fn, result_fn) 模板方法，或为每种 job 定义一个声明式 JobSpec/JobExecutor 多态对象（与文档 B-S2 拆分计划一致），让新增 Agent/Job 只需注册一个 spec 而非复制 ~40 行。

#### [FO-3] 🟨 中 编排器与 store 通过下划线魔法键隐式耦合（_reused_existing/_no_actionable_attributions）

- **位置**：`app/runtime/feedback_job_orchestrator.py:50, 87, 130, 161`
- **原则**：高内聚低耦合（不通过字符串字面量/隐式约定跨模块耦合）
- **证据**：编排器用 `job.get("_reused_existing")`、`job.get("_no_actionable_attributions")` 判断流程分支；这些键由 store 用字面量塞入返回 dict：feedback_job_store.py:71/139 `return {**existing, "_reused_existing": True}`、feedback_batch_plan_store.py:50 `return {"_reused_existing": True, **batch}`、:63 `return {"_no_actionable_attributions": True, ...}`。跨模块约定全靠字符串字面量，无共享常量或类型。
- **修复**：用显式返回类型表达创建结果（如 dataclass JobCreateResult(job, reused: bool, no_actionable: bool)），或至少把这些 sentinel 键定义为单一模块的常量并由 store 与编排器共同引用；返回 payload 不应同时承载业务字段与控制流标志。

#### [FO-4] 🟨 中 job 与 execution_job 生命周期 API 签名/命名不一致，跨边界易错

- **位置**：`app/runtime/feedback_job_orchestrator.py:66-68, 187-189`
- **原则**：易维护（命名一致/异常分层一致）
- **证据**：归因/方案/批次走 `self.feedback_store.fail_job(job["job_id"], error_code="AGENT_TIMEOUT", message=...)`（关键字参数，定义见 feedback_job_store.py:295 `def fail_job(self, job_id, *, error_code, message)`），而执行走 `self.feedback_store.fail_execution_job(job["execution_job_id"], "AGENT_TIMEOUT", ...)`（位置参数，feedback_execution_store.py:149 `def fail_execution_job(self, execution_job_id, error_code, message)`）；同理 start_job/start_execution_job、complete_*_job 用 job_id 而 execution 用 execution_job_id。两套并行 API 命名与签名风格不统一。
- **修复**：统一两类 job 的生命周期接口签名（统一 keyword-only 的 error_code/message、统一 id 形参名或抽象为同一 JobLifecycle 协议），减少编排器在两套约定间手工切换的认知负担与笔误风险。

#### [FO-5] 🟨 中 补偿落库与 fail_execution_job 失败被静默吞掉，补偿记录可能丢失而无任何痕迹

- **位置**：`app/services/execution_application.py:138-148, 160-163`
- **原则**：鲁棒性（异常处理/回滚可观测性）
- **证据**：_compensate_post_write_failure 中 `try: self.feedback_store.record_execution_compensation(...) except Exception: pass`（138-148），随后 `try: self.feedback_store.fail_execution_job(...) except Exception: pass`（160-163）。补偿记录是人工恢复的唯一入口（restore_execution_compensation 依赖 find_execution_compensation），若其落库失败被 pass 吞掉，则 workspace 已改、状态未同步、且无补偿记录可供人工恢复，且无日志。
- **修复**：至少 logger.exception 记录这两处吞掉的异常并保留原始 error；考虑把补偿记录落库视为关键路径——失败时返回更高严重级别的 detail 或触发告警，避免静默丢失唯一的人工恢复线索。

#### [FO-7] 🟩 低 eval 用例无 required 检查时返回 passed 但 score=0.0，存在 passed/score 语义矛盾

- **位置**：`app/runtime/feedback_eval_runner.py:108-127`
- **原则**：鲁棒性（边界输入处理）
- **证据**：三个内置检查均依赖 checks_json 开关（requires_non_empty_answer/requires_no_runtime_errors 默认 True，requires_tool_use 默认缺省）。当用例显式把两个默认项关掉且未要求工具时 required_checks 为空，`score = passed_required / len(required_checks) if required_checks else 0.0` 取 0.0，而 `any(not item["passed"] for item in required_checks)` 对空列表为 False，故返回 ("passed", 0.0, ...)。一个 passed 的回归项得分为 0，下游聚合/展示会自相矛盾。
- **修复**：当 required_checks 为空时把 score 设为 1.0（无强制检查即视为满分通过），或显式返回 "skipped" 状态，避免 passed 与 0 分并存的歧义。

---

## 7. Schema 与响应模型一致性

> 范围：`feedback_schemas.py, schemas.py, *_response_schemas.py, *_models.py`

**维度小结**：schema 按域拆分形成了清晰的无环 DAG（error→schemas→{output,plan,agent_version}→analysis→workflow→optimization），薄模块（error_response_schemas、analysis）是为打破循环/承载 union 而提取的，属合理拆分而非机械碎片化；但同一实体普遍存在"LLM 校验 Output 模型 + 内部 Record 模型 + API Response 模型"多轨手写表示，字段集靠人工同步且已出现真实漂移（ExternalGovernanceItem 漏 4 字段、evidence_ref 三处 required 不一致），叠加 766 行的 feedback_schemas.py 把声明式 schema 和 ~500 行命令式归一化逻辑混在一起，是本维度最主要的可维护性/一致性债务。

#### [SC-1] 🟨 中 同一实体三轨手写表示（Output/Record/Response）字段靠人工同步，已发生真实漂移

- **位置**：`app/runtime/external_governance_models.py, app/runtime/feedback_workflow_response_schemas.py:external_governance_models.py:97-141; feedback_workflow_response_schemas.py:159-196`
- **原则**：DRY / 一致性
- **证据**：ExternalGovernanceItemRecord（内部真相）含 schema_version / superseded_at / superseded_reason / superseded_by_job_id：第102行 'schema_version: Literal["external-governance-item/v1"] = ...'、第139-141行 'superseded_at / superseded_reason / superseded_by_job_id'。而 API 层 ExternalGovernanceItemResponse(159-196) 缺少这 4 个字段（comm -23 比对：schema_version/superseded_at/superseded_by_job_id/superseded_reason 仅在 Record）。store 持久化的是 record.to_payload()（feedback_external_governance_store.py:223 row.payload_json = record.to_payload()），路由用 response_model=ExternalGovernanceItemResponse 暴露（optimization.py:134/152）。mark_superseded(external_governance_models.py:161-178) 会真实写入这些字段。
- **修复**：让 API Response 直接复用内部 Record（如 ExternalGovernanceItemResponse 继承或别名 ExternalGovernanceItemRecord，仅追加 latest_notification 的 Response 形态），或用单一 Base 模型 + 派生，消除手工同步；至少立即把 schema_version/superseded_* 4 个字段补进 Response，使 OpenAPI 与实际响应体一致。

#### [SC-2] 🟨 中 同一 evidence_ref 实体在三个模型中 required 不一致，导致 OpenAPI 自相矛盾

- **位置**：`app/runtime/feedback_schemas.py, app/runtime/feedback_output_response_schemas.py, app/runtime/feedback_plan_response_schemas.py:feedback_schemas.py:64-67; feedback_output_response_schemas.py:10-13; feedback_plan_response_schemas.py:10-13`
- **原则**：一致性 / DRY
- **证据**：EvidenceRef(64-67) 与 EvidenceRefResponse(10-13) 都是 'type: str / id: str / reason: str'（全必填），但 FeedbackOptimizationEvidenceRefResponse(10-13) 为 'type: Optional[str] = None / id: Optional[str] = None / reason: Optional[str] = None'（全可选）。三者序列化的是同一份由 _normalize_evidence_refs(feedback_schemas.py:662-673) 产出的、永远含非空 {type,id,reason} 的 dict，故差异纯属偶发漂移：OpenAPI 在不同端点把同一实体声明为时而必填时而可选。
- **修复**：抽出唯一的 EvidenceRefResponse 复用于 plan/output 两处，删除 FeedbackOptimizationEvidenceRefResponse；按归一化保证的事实将三字段统一定为 required。

#### [SC-3] 🟨 中 feedback_schemas.py 混合声明式 schema 与 ~500 行命令式 LLM 归一化逻辑，违反 SRP

- **位置**：`app/runtime/feedback_schemas.py:1-766（声明 64-261/685-714；归一化 264-682/716-766）`
- **原则**：SRP / 易维护
- **证据**：文件同时承载两类职责：12 个 Pydantic Output 模型（EvidenceRef/AttributionOutput/ProposalOutput/OptimizationPlanTaskOutput/FeedbackOptimizationPlanOutput/ExecutionPlanOutput 等），以及大量与 schema 无关的命令式清洗函数——normalize_attribution_output(272)、normalize_feedback_optimization_plan_output(413)、_normalize_plan_task_output_item(460)、_blocked_item_from_plan_task(521)、_normalize_actionability(579)、_normalize_problem_type(603)、_human_text(761) 等约 500 行。这些是对 LLM 原始输出的别名映射/兜底默认/结构修复，属 sanitization 层而非 schema。
- **修复**：将 normalize_*/_normalize_*/_human_text/_string_list 等迁到独立 feedback_output_normalizers.py，feedback_schemas.py 仅保留模型声明与对外的 validate_* 入口（validate_* 调用 normalizers）；既缩短文件，也让模型层重新成为纯声明式真相源。

#### [SC-4] 🟩 低 JobStatus Literal 为死代码，且与真正执行的 JOB_STATES 状态集发生漂移（缺/多 cancelled）

- **位置**：`app/runtime/feedback_schemas.py, app/runtime/state_machines.py:feedback_schemas.py:9-20; state_machines.py:15-25`
- **原则**：一致性 / 易维护
- **证据**：feedback_schemas.py:9-20 定义 JobStatus = Literal[...,"cancelled","needs_human_review"]，但全仓库无任何 import/使用（grep '\bJobStatus\b' 仅命中定义本身）。真正用于状态校验的是 state_machines.py:15-25 的 JOB_STATES 集合，其含 'evidence_packaging' 但不含 'cancelled'。两份 job 状态枚举各自维护、已不一致：JobStatus 多了 cancelled，缺了被实际允许的转移关系；JOB_STATES 才是 validate_transition(105) 的真相源。
- **修复**：删除未使用的 JobStatus（连同确认无引用的其它纯内部 Literal 一并审视），或将其改为 JOB_STATES 的单一真相源（如由集合派生 Literal），避免两套 job 状态定义继续漂移。

#### [SC-5] 🟩 低 schemas.py 中存在与 agent_version_response_schemas.py 重名的死类 AgentVersionRestoreResponse

- **位置**：`app/runtime/schemas.py, app/runtime/agent_version_response_schemas.py:schemas.py:605-609; agent_version_response_schemas.py:96-100`
- **原则**：DRY / 易维护
- **证据**：schemas.py:605 'class AgentVersionRestoreResponse(BaseModel)' 三字段均为弱类型 'dict[str, Any]'；agent_version_response_schemas.py:96 同名类用强类型 'restored_from_version: AgentVersionSummaryResponse' 等。路由实际只 import 后者（agent_versions.py:9-15 从 agent_version_response_schemas 导入，第60/63/65 使用），schemas.py 版本无任何引用，属遗留重复定义，易被误用且制造同名歧义。
- **修复**：删除 schemas.py:605-609 的 AgentVersionRestoreResponse（保留同处的 AgentVersionRestoreRequest）。

#### [SC-6] 🟩 低 Output 模型用严格 Literal，对应 Response 模型一律退化为 bare str，丢失类型契约且需双份维护

- **位置**：`app/runtime/feedback_schemas.py, app/runtime/feedback_output_response_schemas.py:feedback_schemas.py:75-88（含 problem_type:ProblemType 等）; feedback_output_response_schemas.py:21-34（problem_type:str 等）`
- **原则**：一致性 / 扩展性
- **证据**：AttributionOutput(75-88) 字段受 Literal 约束：problem_type: ProblemType、optimization_object_type: OptimizationObjectType、actionability: Actionability、recommended_next_step: Literal[...]。AttributionOutputResponse(21-34) 同名字段全部退化为 'problem_type: str / optimization_object_type: str / actionability: str / recommended_next_step: str'。两模型字段集逐字相同（diff 验证 IDENTICAL FIELD SETS），靠人工保持同步；Response 端枚举值不再出现在 OpenAPI，前端 api.ts 生成为宽 string。
- **修复**：将枚举 Literal（ProblemType/Actionability/OptimizationObjectType/Confidence）从 feedback_schemas.py 提到独立 enums 模块，Response 模型直接复用同一 Literal；新增/改名枚举值时只改一处并自动反映到 OpenAPI 与前端类型。

---

## 8. 状态机与状态治理

> 范围：`app/runtime/state_machines.py + 各 store status 写入点`

**维度小结**：状态机已抽出独立模块且 job/execution_job 两个机器通过 4 个 store 的统一 _update_*_row 收口写入（这是干净的高内聚收口，值得肯定）；但 batch/task 两个最复杂的机器只登记了状态集合却没有转移表，等于只做拼写校验而不拦截非法转移；同时 case/eval_run/proposal 三类有真实生命周期的实体完全没有纳入状态机，状态字面量仍在 feedback_schemas.py 等处重复定义且已与 state_machines.py 发生漂移，违反了本仓库自己在 AGENTS.md/治理文档里立下的 [F-S4]「含状态字段实体必须有集中状态机」红线。

#### [SM-1] 🟧 高 batch/task 状态机只登记状态集合、缺转移表，非法转移完全不被拦截

- **位置**：`app/runtime/state_machines.py:75-120`
- **原则**：鲁棒性 / 扩展性（状态机合法转移未强制）
- **证据**：_TRANSITIONS 只定义了 "job" 与 "execution_job" 两个键（75-95 行），没有 "batch"/"task"。而 _KNOWN_STATES 把 batch/task 都登记了（98-102 行）。validate_transition 中：`transitions = _TRANSITIONS.get(machine); if transitions is None: return`（115-117 行）。BATCH_STATES 有 22 个状态、TASK_STATES 有 11 个状态，却走 115-117 行直接 return。
- **修复**：为 "batch" 与 "task" 在 _TRANSITIONS 中补全转移表（例如 completed/regression_passed 为终态、approved 只能来自 pending_approval 等）。若短期无法穷举，应把 transitions is None 改为显式抛错或加 strict 标志，避免「登记了状态集却静默放行任意转移」这种比不接入更危险的伪治理——调用方（feedback_batch_store.py:308、feedback_task_store.py:143）会误以为已有完整守卫。
- **校验复核**：batch/task 在 _TRANSITIONS 中无转移表，validate_transition 对二者仅做"状态名是否在 BATCH_STATES(22)/TASK_STATES(11) 中登记"的存在性校验（109-110 行可拦截未知 target），但在 115-117 行因 _TRANSITIONS.get("batch"/"task") 为 None 直接 return，完全跳过转移合法性判定——任意"已登记状态间"的非法转移（如 batch completed->draft、rejected->approved，task completed->pending_execution）均被静默放行。需注意：并非"完全不被拦截"（未知状态名仍被拦），而是"已知状态之间的非法转移完全不被拦截"。对比 job/execution_job 已有完整转移表与强制校验，batch/task 构成不一致的伪守卫。

#### [SM-2] 🟧 高 FeedbackCase 有真实生命周期却无任何状态机，且经两条未校验路径直接写 status

- **位置**：`app/runtime/feedback_case_store.py:61, 193; app/runtime/feedback_job_store.py:356`
- **原则**：一致性 / 鲁棒性（违反本仓库 [F-S4]：含状态字段实体必须有集中状态机）
- **证据**：case 实际状态包括 pending_evidence(create_case:61)、pending_attribution(job_store:356)、attribution_queued(job_store:116)、pending_proposal(job_store:254/310)、proposal_queued(job_store:182)、pending_review/needs_human_review(job_store:223)。写入处 feedback_case_store.py:193 `row.status = status or row.status` 与 feedback_job_store.py:356 `row.status = "pending_attribution"` 均未调用 validate_transition。state_machines.py 的 _KNOWN_STATES 中也没有 "case" 键（grep "case" 无命中）。
- **修复**：在 state_machines.py 新增 CASE_STATES 与 case 转移表，并让 _append_case_update_row 与 discard_current_attribution(line 356) 统一调用 validate_transition("case", row.status, status)；消除 job_store:356 这条绕过 helper 的直写路径，使 case 状态也只有一个收口。
- **校验复核**：FeedbackCase 拥有真实的多态生命周期（pending_evidence/pending_attribution/attribution_queued/pending_proposal/proposal_queued/pending_review/needs_human_review），但在 state_machines.py 中没有对应的状态机（_KNOWN_STATES 无 \"case\" 键）。case 状态写入存在两类未校验路径：一是集中 helper feedback_case_store.py:193 `row.status = status or row.status`（被约9处调用，含 feedback_evidence_store.py:52），二是 feedback_job_store.py:356 `row.status = \"pending_attribution\"` 绕过 helper 的直写；二者均不调用 validate_transition。这与同仓库 job/execution_job/batch/task 四类实体一律经 validate_transition 收口形成不一致，违反本仓库 [F-S4]「含状态字段实体必须有集中状态机」。注：finding 表述「两条路径」基本准确（一处共享 helper + 一处直写绕过），但 helper 实际被多处调用，且证据漏列了 feedback_evidence_store.py:56 这一同样未校验的写入点。

#### [SM-4] 🟨 中 eval_run 与 proposal 有完成/失败/审批生命周期，但完全绕过状态机、无转移守卫

- **位置**：`app/runtime/feedback_eval_store.py:234, 252; app/runtime/feedback_proposal_store.py:117, 168`
- **原则**：鲁棒性（幂等/非法重入未防护，状态字段未集中治理）
- **证据**：eval_run：finish_eval_run 直写 `run.status = "completed"`(234)，fail_eval_run 直写 `run.status = "failed"`(252)，无 current 状态检查——已 completed 的 run 可被再次置为 failed。proposal：review_proposal 中 `row.status = next_status`(117) 无 current 校验，已 superseded/rejected 的 proposal 可被再次 approve；_supersede_case_proposals 直写 "superseded"(168)。这些机器名均未在 _KNOWN_STATES 出现。
- **修复**：将 eval_run（running→completed/failed 终态）与 proposal（pending_review→approved/rejected/needs_more_analysis/superseded）纳入 state_machines.py，在 finish/fail/review/supersede 写入前调用 validate_transition，至少拦截「终态再次变更」。

#### [SM-5] 🟨 中 文档声明的状态机与代码实现状态词汇零交集（实现与产品文档不一致）

- **位置**：`docs/FEEDBACK_OPTIMIZATION_PRODUCT_ADJUSTMENT_PLAN.md:524-560`
- **原则**：一致性（实现与 docs 矛盾）
- **证据**：文档 8.1 反馈信息状态列 new/annotated/eval_case_ready/attribution_ready/included_in_batch/validated/archived；8.2 批次状态列 eval_cases_generating/plan_generating/pending_plan_approval/plan_rejected/execution_applied 等。逐一 grep 代码：eval_case_ready/attribution_ready/included_in_batch/annotated/eval_cases_generating/plan_generating/pending_plan_approval/execution_applied 均 NOT_IN_CODE；实际 BATCH_STATES 用的是 attribution_running/optimization_plan_queued/pending_approval/approved 等。
- **修复**：以 state_machines.py 为权威，更新产品文档 8.1/8.2 的状态枚举与转移描述使之与 JOB_STATES/BATCH_STATES/TASK_STATES 对齐；或在文档标注「目标态 vs 现状」并给出映射，避免后续开发者按已失效的文档实现。

#### [SM-3] 🟩 低 JobStatus(Literal) 与 JOB_STATES 双源定义且已漂移（含未识别的 cancelled），属死代码

- **位置**：`app/runtime/feedback_schemas.py:9-20`
- **原则**：DRY / 一致性（状态集合非单源，schema 与状态机已分散并冲突）
- **证据**：feedback_schemas.py 定义 `JobStatus = Literal["created","evidence_packaging","queued","running","schema_validating","completed","failed","cancelled","timeout","needs_human_review"]`，比 state_machines.py 的 JOB_STATES 多出 "cancelled"；若某处用 JobStatus 校验通过 cancelled，再交给 validate_transition 会被判 "Unknown job status"。grep 显示 JobStatus 与 "cancelled" 在 app/ 内除本定义外零引用，是已漂移的孤儿副本。
- **修复**：删除 feedback_schemas.py 的 JobStatus（无引用），或将其改为由 state_machines.JOB_STATES 派生（如 `JobStatus = Literal[tuple(sorted(JOB_STATES))]` 思路或运行期校验），确保 schema 与状态机单源；若 cancelled 是规划状态则应先进 JOB_STATES 与转移表，否则移除以防再次漂移。

#### [SM-6] 🟩 低 「在途 job 状态集合」以内联字面量在多处重复，未复用 JOB_STATES

- **位置**：`app/runtime/feedback_job_store.py:529; app/runtime/feedback_batch_store.py:125`
- **原则**：DRY / 耦合（隐式状态约定跨模块复制）
- **证据**：feedback_job_store.py:529 `if job.get("status") not in {"created", "queued", "running", "schema_validating", "evidence_packaging"}:` 与 feedback_batch_store.py:125 `running = [job for job in jobs if job.get("status") in {"created", "queued", "running", "schema_validating", "evidence_packaging"}]` 重复同一组「在途」状态字面量，状态词汇散落在状态机模块之外。
- **修复**：在 state_machines.py 暴露语义子集常量（如 JOB_INFLIGHT_STATES = {created,queued,running,schema_validating,evidence_packaging}）并在两处引用，使状态分类也单源。

#### [SM-7] 🟩 低 状态机测试覆盖不足：无 batch/task 转移测试，也未断言非法 job 转移被拦截

- **位置**：`tests/test_state_machines.py:1-22`
- **原则**：鲁棒性 / 一致性（缺负向与边界测试，与 AGENTS.md「涉及状态字段至少补 1 个非法转移负向测试」要求不符）
- **证据**：全文件仅 3 个用例：execution_job 正向链路、execution_job completed→running 被拒、job 未知状态被拒。grep 全 tests/ 仅本文件引用 validate_transition/StateTransitionError；无 validate_transition("batch"/"task") 调用，也没有断言 job 内部非法转移（如 completed→running）或 batch/task 非法转移被拦的负向用例。
- **修复**：补充：(a) batch/task 一旦补齐转移表后的非法转移负向用例；(b) job completed→running、execution_job ready→queued 等终态/逆向被拒用例；(c) validate_transition 对 Unknown state machine 与 Unknown current 分支的覆盖，把当前未被任何测试触达的 107-114 行纳入。

---

## 9. 外部治理与支撑模块

> 范围：`external_governance*, output_formatter, message_utils, runtime_activity, runtime_langfuse`

**维度小结**：外部治理服务的对外门面（mixin）、Langfuse 适配器与 message_utils 拆分边界总体清晰，离线/禁用时 Langfuse 与 DSPy 格式化器均优雅降级（返回 None/nullcontext，build_env 仅在显式启用且缺密钥时 fail-fast），webhook 配置加载具备 YAML/类型/必填校验；但 ExternalGovernanceItemModel 的 row↔record 映射在拆分后被三个 store 模块各自重复实现（机械碎片化迹象），通知路径缺少对 superseded 终态的状态机保护、且 notify-then-persist 顺序在崩溃时会破坏“通知必落库”不变量，notification_payload 与 record 字段存在双处维护风险。

#### [GS-1] 🟨 中 ExternalGovernanceItemModel 的 row↔record 映射在三个模块中重复实现

- **位置**：`app/runtime/external_governance.py, app/runtime/feedback_proposal_store.py, app/runtime/feedback_external_governance_store.py:external_governance.py:239-266; feedback_proposal_store.py:190-217; feedback_external_governance_store.py:213-223`
- **原则**：DRY / 高内聚低耦合（SRP）
- **证据**：external_governance._item_record_from_row(239-254) 与 feedback_proposal_store._external_governance_record_from_row(199-217) 几乎逐字相同：均执行 `payload = dict(row.payload_json or {}); payload.update({"external_item_id": row.external_item_id, "created_at": row.created_at, ... "latest_notification_id": row.latest_notification_id}); return ExternalGovernanceItemRecord.model_validate(payload)`。同样地 _apply_item_record(256-266)、_apply_external_governance_record(213-223) 与 feedback_proposal_store.py:190-195 内联块都是相同的 `row.status=record.status; row.owner=record.owner; ... row.payload_json=record.to_payload()` 投影。
- **修复**：把 ExternalGovernanceItemModel 的 row→record 读取与 record→row 写回收敛到单一归属点（推荐放在 ExternalGovernanceService 或一个 ExternalGovernanceItemMapper），三处调用方统一复用 `service._item_record_from_row` 与 `service._apply_item_record`；feedback_proposal_store 与 feedback_external_governance_store 删除各自的副本，仅保留 mark_superseded/with_notification 等业务转移调用。这样新增列时只改一处。

#### [GS-2] 🟨 中 notify_item 缺少对 superseded 终态的状态机保护，会把已废弃项复活为 notified

- **位置**：`app/runtime/external_governance.py, app/runtime/external_governance_models.py, app/routers/optimization.py:external_governance.py:80-140; external_governance_models.py:143-159; optimization.py:155-160`
- **原则**：鲁棒性 / 状态机合法转移
- **证据**：notify_item 通过 find_item(external_item_id) 取项后直接发送并写回，无任何状态判断（80-140 行无 `status` 校验，grep 确认 external_governance.py 中 superseded 仅出现在 list_items 过滤处 66-69）。with_notification(models 143-159) 无条件设置 `"status": "notified" if notification.status == "sent" else "notification_failed"`，没有对 mark_superseded 产生的终态做前置校验。路由 notify_external_governance_item(optimization.py:155-160) 也未做任何 status 前置校验，直接 `result = feedback_store.notify_external_governance_item(...)`。因此对一个已 superseded 的项调用 /notify 会把它复活成 notified。
- **修复**：在 notify_item 开头校验 item["status"] != "superseded"（否则抛 BusinessRuleViolation），或在 with_notification 中拒绝从 superseded 转出；同时建议 find_item 提供 active-only 变体供通知路径使用，使 superseded 成为真正的终态。

#### [GS-4] 🟨 中 notification_payload 手工枚举约 30 个字段，与 ExternalGovernanceItemRecord 双处维护

- **位置**：`app/runtime/external_governance.py, app/runtime/external_governance_models.py:external_governance.py:162-201; external_governance_models.py:97-141`
- **原则**：DRY / 易维护
- **证据**：notification_payload(162-201) 逐字段从 item 字典手工搬运 title/description/objective/target_summary/owner/actionability/recommendation/recommended_actions/acceptance_criteria/expected_effect/validation/risk/analysis_summary/evidence_summary/evidence_refs/reason 以及 source/batch_id/optimization_plan_id/plan_task_id/target_type/target_path/task_context/feedback_case_ids/eval_case_ids/source_attribution_job_ids，而这些字段几乎全部已是 ExternalGovernanceItemRecord 的声明字段(models 110-135)。任何新增到 record 的业务字段若需进入通知，必须记得在此第二处补一遍，否则静默丢失。
- **修复**：用 record 字段集合驱动 payload：例如基于 ExternalGovernanceItemRecord.model_dump 取一组白名单/排除集（排除 latest_notification* 与 superseded* 等通知不需要的字段），再覆盖 schema_version 与 webhook_alias，避免逐字段手抄。

#### [GS-3] 🟩 低 通知先发送后落库，崩溃窗口会破坏“通知记录必落库”不变量且无幂等键

- **位置**：`app/runtime/external_governance.py, docs/FEEDBACK_OPTIMIZATION_MULTI_AGENT_ARCHITECTURE.md:external_governance.py:101-140; doc:1579`
- **原则**：鲁棒性（事务/幂等）/ 代码与文档一致
- **证据**：notify_item 先在 102 行执行 `response = (sender or self.send_webhook)(webhook, payload)` 真正发出 HTTP，再到 120 行 `with self.Session.begin() as db:` 才落库通知记录。若进程在成功 POST 与 DB 写入之间崩溃：外部系统已收到通知但无任何记录，且 item 仍处于 pending_notification，可被再次通知（payload 中无任何幂等/去重键）。架构文档明确要求“通知记录必须落库，包含目标 alias、HTTP 状态、响应摘要和失败原因”(doc 1579)。
- **修复**：改为“先以 sending 状态落库一条通知记录（含 notification_id 作幂等键）→ 再发送 → 用同一 notification_id 更新结果”，并在 payload 中带上 notification_id 供下游去重；或至少在发送前后各落一次库，保证任何崩溃点都不丢通知记录。

#### [GS-5] 🟩 低 同名 _string 在同一 runtime 包内语义分叉（强制 str 化 vs 仅透传 str）

- **位置**：`app/runtime/external_governance.py, app/runtime/feedback_store.py, app/runtime/feedback_job_factory.py:external_governance.py:278-282; feedback_job_factory.py:143-147; feedback_store.py:286-287`
- **原则**：一致性 / 易维护（命名一致）
- **证据**：external_governance._string(278-282) 与 feedback_job_factory._string(143-147) 均为 `text = str(value).strip(); return text or None`（对 int/bool 等也会强制转字符串）；而 FeedbackStore._string(286-287) 为 `return value if isinstance(value, str) and value else None`（非 str 一律返回 None）。feedback_external_governance_store 这个 mixin 内所有 self._string(...)（92-186 多处）解析的是 FeedbackStore 版本，与 external_governance 模块自带版本契约不同，同名但行为不一致，易在跨模块阅读/复用时踩坑。
- **修复**：统一命名与契约：把“仅透传 str”的语义命名为 _str_or_none，把“强制 str 化”的命名为 _coerce_str，并各自下沉到一个共享 util（如 message_utils 或新 text_utils），消除同名异义。

#### [GS-6] 🟩 低 _unique_strings 在 runtime_activity 与 feedback_store 中逐字重复

- **位置**：`app/runtime/runtime_activity.py, app/runtime/feedback_store.py:runtime_activity.py:208-217; feedback_store.py:257-265`
- **原则**：DRY
- **证据**：runtime_activity._unique_strings(208-217) 与 feedback_store._unique_strings(257-265) 实现完全相同：`result=[]; seen=set(); for value in values: if not isinstance(value,str) or not value or value in seen: continue; seen.add(value); result.append(value); return result`。两者位于无继承关系的不同模块，属于复制粘贴。
- **修复**：将 _unique_strings 提取为模块级共享工具函数（如放入一个 collections/text utils），两处统一导入复用，避免行为漂移。

---

## 10. 前端组件

> 范围：`frontend/src/components/** + feedback-workspace/**`

**维度小结**：拆分后大部分组件职责清晰、展示与状态/动作 hook 分离得当，common/selectors 抽象合理，request.ts 的超时+仅 GET 重试是干净边界；但拆分也留下两处明显的"机械碎片化"后遗症：CasesPanel/EvalPanel 整条分支已不可达（菜单被裁剪但渲染逻辑与配套 state/actions 仍保留），以及 useFeedbackWorkspaceActions 成为聚合 30+ 异构动作的上帝 hook；另有死代码、SSE 无超时、重复样板与工具函数多处重定义等中低问题。

#### [FC-2] 🟧 高 useFeedbackWorkspaceActions 是聚合 30+ 异构动作的上帝 hook（642 行、16 项入参）

- **位置**：`frontend/src/components/feedback-workspace/useFeedbackWorkspaceActions.ts:70-104; 599-641`
- **原则**：SRP/高内聚低耦合：本应是多个领域 hook 的职责被塞进一个 hook，导致入参面巨大、跨领域耦合、改一个领域必读全文件。这是拆分中典型的'换皮上帝模块'。
- **证据**：单个 hook 入参对象 16 项（clientConfig/onFeedbackChanged/onRefreshVersions/selectedSourceIds/setSelectedSourceIds/setSelectedCaseId/setSelectedBatchId/setActiveMenu/sourceRows/selectedCase/caseDetails/setCaseDetailView/setAttributionDetailTab/refreshWorkbench/tasksByProposalId/setToast），return 暴露 39 个成员（actionId, ...toggleSource, generateEvalCasesFromSelection, createBatchFromSelection, runCaseAction, reviewProposal, ... updateEvalCaseRecord）。函数横跨 signal/batch/proposal/task/execution/compensation/external/eval 多个领域，且各函数体大量重复 `setActionId(...) try{...await refreshWorkbench(); onFeedbackChanged?.()} catch{setToast(...)} finally{setActionId(null)}` 样板。
- **修复**：按领域拆为 useBatchActions/useProposalActions/useTaskExecutionActions/useEvalActions 等，各自持有自身依赖；抽出统一的 `runAction(actionId, fn, {successToast})` 包装器消除 try/catch/finally/refresh/onFeedbackChanged 样板，将 642 行降到每个 hook 100-150 行量级。
- **校验复核**：useFeedbackWorkspaceActions 是一个聚合 30 个动作函数的上帝 hook（642 行；入参对象 16 项；return 暴露 41 个成员——其中 28 个函数 + 13 个 state/draft/busy 值，finding 原文写 39 略有低估）。函数横跨 eval/batch/proposal/task/execution/compensation/external/attribution 等 7-8 个领域，且大量重复 setActionId/try{await refreshWorkbench();onFeedbackChanged?.()}/catch{setToast}/finally{setActionId(null)} 样板（约 20+ 处，22 个 catch、22 个 finally）。行号 70-104 与 599-641 均准确。

#### [FC-1] 🟨 中 CasesPanel/EvalPanel 整条渲染分支不可达（菜单裁剪后遗留死 UI 与配套 state/actions）

- **位置**：`frontend/src/components/feedback-workspace/useFeedbackWorkspaceState.ts:28-35; 配合 ExternalFeedbackWorkspace.tsx:211-308; useFeedbackWorkspaceActions.ts:293-301`
- **原则**：高内聚低耦合/扩展性：拆分后菜单被裁剪，但 CasesPanel/EvalPanel 及其在 state hook 中的 selectedCase* 系列 selector、actions 中的 runCaseAction/reviewProposal/createTask/syncEvalDataset/updateEvalCaseRecord 等仍全量保留并被传参，形成大片不可达却需维护的逻辑，新增菜单/路由时易误判其有效性。
- **证据**：useFeedbackWorkspaceState.ts: `export type MenuKey = "signals" | "batches" | "cases" | "evals" | "versions";` 但 `visibleMenuItems` 只含 signals/batches/versions。Grep 全仓 setActiveMenu 仅被调用为 "batches"（actions 中 3 处）与菜单按钮 `setActiveMenu(item.key)`（item 仅来自 visibleMenuItems）。因此 activeMenu 永远不会变成 "cases"/"evals"。而 ExternalFeedbackWorkspace.tsx 仍渲染 `{activeMenu === "cases" ? (<CasesPanel .../>) : null}`(211) 与 `{activeMenu === "evals" ? (<EvalPanel .../>) : null}`(298)。openTask 也写 `setSelectedCaseId(...); setCaseDetailView("tasks"); setActiveMenu("batches");`(294-297)——跳到 batches 而非 cases，case 详情 tasks 视图同样无法经 UI 到达。
- **修复**：明确取舍：若 cases/evals 仍是产品功能，则把它们加回 visibleMenuItems 并提供入口；若已废弃，则删除 ExternalFeedbackWorkspace 中 211-308 分支、MenuKey 中的 cases/evals、useFeedbackWorkspaceState 里仅服务于这两个面板的 selectedCaseProposals/Tasks/ExternalItems/EvalCases/loadCaseDetails 及 actions 中对应函数，使可达性与代码一致。

#### [FC-3] 🟨 中 ProposalsPanel/ProposalList 为未被引用的死代码，且与 ProposalDetailCard 重复实现审批卡片

- **位置**：`frontend/src/components/feedback-workspace/ProposalWorkspace.tsx:263-413`
- **原则**：DRY/易维护：拆分后遗留未删除的旧面板，约 150 行死代码，并与现役 ProposalDetailCard 重复维护同一套审批交互，修改审批逻辑需改两处易漏改。
- **证据**：Grep 全仓：`ProposalsPanel`/`ProposalList` 仅在本文件定义，ExternalFeedbackWorkspace 只 `import { ProposalDetails } from "./feedback-workspace/ProposalWorkspace"`。ProposalList(306-413) 里的 pending/approved 审批按钮块（363-384：批准/拒绝/补充分析、创建/查看优化任务）与上方 ProposalDetailCard(200-225) 几乎逐行重复。
- **修复**：删除 ProposalsPanel 与 ProposalList；若仍需列表视图，复用 ProposalDetailCard/ExternalGuidanceCard 组合，不要保留并行实现。

#### [FC-4] 🟨 中 streamChat 无超时/卡死保护，连接挂起将永久占用 streaming 状态

- **位置**：`frontend/src/api/runtime.ts:108-154`
- **原则**：鲁棒性：网络/服务端卡死场景缺少首字节或空闲超时与自动中断，状态机无法自愈。
- **证据**：streamChat 直接 `await fetch(makeUrl(config, "/api/chat/stream"), {... signal})` 并 `reader.read()` 循环，仅依赖外部传入的 signal；不像 requestJson(request.ts:39-40) 那样设置 `controller.abort("timeout")` 与 DEFAULT_REQUEST_TIMEOUT_MS。App.tsx 的 streaming 由 onDone/catch 复位，若服务端建立连接后长时间不发任何 SSE 事件且不关闭，read() 会一直挂起，UI 永远停在'运行中'，用户只能手动点停止。
- **修复**：为 streamChat 增加可配置的空闲超时（如基于 setTimeout 的看门狗，在每次 reader.read() 成功后重置；超时则 controller.abort 并向 onError 抛出'stream idle timeout'），与 requestJson 的超时策略保持一致。

#### [FC-5] 🟩 低 sourceKindText 映射与 Pill tone 三元表达式在两个文件重复

- **位置**：`frontend/src/components/feedback-workspace/BatchFeedbackDetails.tsx:6-10; 配合 SignalsWorkspace.tsx:5-9, 79, 122`
- **原则**：DRY/一致性：source kind 的文案与配色是跨组件共享的展示约定，分散维护会导致两处文案/配色漂移。
- **证据**：BatchFeedbackDetails.tsx:6-10 与 SignalsWorkspace.tsx:5-9 各自定义完全相同的 `const sourceKindText: Record<...> = { signal: "Feedback signal", soc_event: "SOC event", pending_correlation: "待关联" }`；两文件还反复内联 `row.kind === "pending_correlation" ? "orange" : row.kind === "soc_event" ? "green" : "blue"`（SignalsWorkspace 79、122；BatchFeedbackDetails 57、69）。
- **修复**：将 sourceKindText 与 sourceKindTone(row.kind) 上移至 selectors.ts（已集中其它 *Tone 函数），两个组件统一引用。

#### [FC-6] 🟩 低 submitProposalRegenerate 采用 fire-and-forget + 定时刷新，存在刷新竞态

- **位置**：`frontend/src/components/feedback-workspace/useFeedbackWorkspaceActions.ts:249-275`
- **原则**：鲁棒性/一致性：并发刷新缺乏顺序/取消保证，且与同模块其它动作的统一'await 后刷新'范式不一致。
- **证据**：`const regeneratePromise = regenerateProposalJob(...); window.setTimeout(() => { void refreshWorkbench(); }, 500); try { const job = await regeneratePromise; ... await refreshWorkbench(); }` —— 先排定一个 500ms 后的 refreshWorkbench，再 await 同一请求后又 refreshWorkbench。两次刷新无顺序保证，若 500ms 定时刷新晚于/早于请求完成刷新返回，可能用旧快照覆盖新状态；且此模式与其它 action（直接 await 后刷新）不一致。
- **修复**：去掉投机性的 setTimeout 刷新，统一为 await 请求后单次 refreshWorkbench；若确需即时反馈中间态，应由后端返回 pending job 后用乐观更新而非定时盲刷。

#### [FC-7] 🟩 低 Toast 无 key，连续相同文案不会重新动画也可能不清除

- **位置**：`frontend/src/components/ExternalFeedbackWorkspace.tsx:380`
- **原则**：鲁棒性：依赖一次性动画事件做状态清除，对重复内容/动画被打断的场景不可靠。
- **证据**：`{toast ? <div className="fw-toast" onAnimationEnd={() => setToast(null)}>{toast}</div> : null}` —— 仅靠 CSS 动画结束触发 setToast(null)，且元素无 key。若两次 setToast 传入相同字符串，React 复用同一 DOM 节点，动画已结束不会重放，onAnimationEnd 不再触发，toast 可能长期停留或被认为'未更新'。
- **修复**：为 toast 引入自增 id 作为 React key（每次 setToast 产生新 key 强制重挂载），或改用 useEffect + setTimeout 显式定时清除，避免依赖 onAnimationEnd。

#### [FC-8] 🟩 低 shortId/formatDate/isRecord 在多个模块重复定义且行为不一致

- **位置**：`frontend/src/components/AgentVersionsWorkspace.tsx:386-394; 对比 selectors.ts:572-583`
- **原则**：DRY/一致性：同名工具多份实现，shortId 行为分叉会让同一 ID 在版本面板与反馈面板显示不同截断形式。
- **证据**：AgentVersionsWorkspace.tsx 自带 `function shortId(value){ return value.length > 22 ? value.slice(0,22)+"..." : value }`(386-389) 与 `formatDate`(396-408)，而 selectors.ts:572 的 shortId 在 >16 时取 `slice(0,8)…slice(-6)`，两者截断规则不同。isRecord 也分别在 App.tsx:33、MessageBubble.tsx:589、selectors.ts:275、api/runtime.ts:213 各定义一份。
- **修复**：将 shortId/formatDate 统一到 selectors.ts（或新建 utils），AgentVersionsWorkspace 改为引用；isRecord 抽到共享 type-guard 模块，消除 4 处重复。

---

## 11. 前端 API 与类型

> 范围：`frontend/src/api/* + frontend/src/types/*`

**维度小结**：请求层（request.ts）抽象干净：统一封装超时/重试/错误解析，barrel（runtime.ts re-export feedback）让组件只依赖单一入口、未见字符串字面量跨模块耦合，整体优于历史的"上帝模块"。但类型层存在真实的 schema 单源漂移：ChatRequest/ChatResponse 在 types/runtime.ts 被手写重定义并与自动生成的 api.ts 同名 schema 漂移（其中 ChatResponse 为无人引用的死类型）；OptimizationProposalRecord 等大量 Record 类型重复 re-list 基类已有字段。此外流式路径（streamChat）的超时/错误处理与 requestJson 分裂，聚合接口 getFeedbackWorkbenchData 的容错（.catch）不一致，types/feedback.ts 把 UI 组件 Props 混入 API DTO 模块。

#### [FT-5] 🟨 中 getFeedbackWorkbenchData 的并行聚合容错不一致：部分接口失败会拖垮整个看板

- **位置**：`frontend/src/api/feedback.ts:564-578`
- **原则**：鲁棒性/一致性
- **证据**：Promise.all 中 getFeedbackSources/getOptimizationTasks/getExternalGovernanceItems/getExternalGovernanceWebhooks/getEvalCases/getEvalRuns/getFeedbackOptimizationBatches 带 .catch(()=>[]) 降级，但 getAgentRuns/getFeedbackSignals/getSocEvents/getPendingCorrelations/getFeedbackCases/getOptimizationProposals 无 .catch —— 这 6 个任一失败即 reject 整个 Promise.all，看板全空，容错策略前后矛盾且无依据。
- **修复**：统一策略：用 Promise.allSettled 或对全部 13 个调用一致地降级并收集 errors 返回给上层提示；显式区分‘必需数据失败=整体报错’与‘可选数据失败=降级’，而非任意分布 .catch。

#### [FT-1] 🟩 低 ChatResponse 手写类型与自动生成 schema 漂移且全仓库无人引用（死代码）

- **位置**：`frontend/src/types/runtime.ts:43-66`
- **原则**：DRY/一致性（前后端 schema 单源）
- **证据**：runtime.ts: export interface ChatResponse { run_id: string; ... agent_activity: AgentActivity; messages: Record<string, unknown>[]; ... errors: string[]; } —— 而生成的 api.ts:1609 ChatResponse 中 agent_activity?: {[key:string]:unknown}（可选+松散）、messages?/errors? 均为可选。grep 全仓库无任何 import/使用 ChatResponse。
- **修复**：删除手写 ChatResponse（确认无引用），若将来需要则改为 type ChatResponse = components["schemas"]["ChatResponse"]，把 agent_activity 的强类型通过 Omit+交叉的方式叠加，避免必选/可选语义与后端漂移。

#### [FT-2] 🟩 低 ChatRequest 在 runtime.ts 手写重定义，与生成 schema 漂移（可空性不一致）

- **位置**：`frontend/src/types/runtime.ts:26-41`
- **原则**：一致性/DRY（schema 单源）
- **证据**：runtime.ts: export interface ChatRequest { message: string; session_id?: string; ... skills_mode?: "all"|"default"|"none"; ... } —— 而 api.ts:1537 生成版 session_id?: string | null、alert_id?: string|null 等均为 ...|null。该手写类型被 api/runtime.ts 的 streamChat 实际使用，属于活跃漂移：后端字段语义改动不会反映到此处。
- **修复**：改为 type ChatRequest = components["schemas"]["ChatRequest"]（POST 请求体）派生，保持与 openapi 单源同步；streamChat 直接复用该派生类型。

#### [FT-3] 🟩 低 OptimizationProposalRecord 等 Record 类型重复列出基类已存在的字段

- **位置**：`frontend/src/types/feedback.ts:250-257`
- **原则**：DRY
- **证据**：export type OptimizationProposalRecord = OpenApiOptimizationProposalResponse & { status: ...; actionability?: string; target_type?: string; title?: string; recommendation?: string; ... } —— 但 api.ts:3167-3196 OptimizationProposalResponse 已含 actionability/target_type/title/recommendation（均 string|null）。这些字段在交叉类型里是冗余 re-list（且把 string|null 收窄成 string 仅为噪声），唯一有意义的增强只是 status 的字面量联合。
- **修复**：交叉类型中只保留真正需要收窄/新增的字段（如 status 字面量联合、latest_review），删除与基类完全重复的 actionability/target_type/title/recommendation，减少漂移面。

#### [FT-4] 🟩 低 流式请求 streamChat 与 requestJson 错误/超时处理分裂，未复用统一封装

- **位置**：`frontend/src/api/runtime.ts:108-128`
- **原则**：耦合/一致性/鲁棒性
- **证据**：streamChat 直接用裸 fetch(makeUrl(...), { ... signal })，仅依赖调用方 signal，无 DEFAULT_REQUEST_TIMEOUT_MS 超时、无 RETRYABLE_STATUS 重试、错误处理用 'Failed to start stream' 而非 readError 的分层逻辑（仅初始 !res.ok 时调用 readError）。而 request.ts:33-87 的 requestJson 提供了统一超时/重试/AbortController。
- **修复**：在 request.ts 中抽出共享的 buildHeaders/超时 AbortController 工具，streamChat 复用同一超时与错误归一化逻辑（流式可保留不重试，但应有连接建立超时与统一错误格式），避免两条请求路径的鲁棒性策略不一致。

#### [FT-6] 🟩 低 types/feedback.ts 把 UI 组件 Props/配置混入 API DTO 模块（职责不单一）

- **位置**：`frontend/src/types/feedback.ts:53-81`
- **原则**：SRP/高内聚
- **证据**：在以 API 响应/请求 DTO 为主的模块里定义了 RuntimeIntegrationContext、MonitoringIntegrationConfig、ExternalFeedbackWorkspaceProps（含 clientConfig/onRefreshVersions/refreshToken 等纯 UI 回调与展示属性），被 ExternalFeedbackWorkspace.tsx 等组件消费。
- **修复**：将组件 Props 与监控/集成上下文类型移至组件层（如 components/feedback-workspace/types.ts），types/feedback.ts 仅保留与 openapi 对应的 DTO/记录类型，明确‘传输层 schema’与‘表现层 viewmodel’边界。

#### [FT-7] 🟩 低 reviewOptimizationProposal 对未知 action 静默回退到 request-more-analysis

- **位置**：`frontend/src/api/feedback.ts:374-381`
- **原则**：鲁棒性/扩展性
- **证据**：const action = payload.action || "approve"; const routeByAction = { approve, reject, request_more_analysis: "request-more-analysis" }; const route = routeByAction[action] || "request-more-analysis"; —— 非法/未来新增的 action 会被静默路由到 request-more-analysis（语义截然不同），而非报错。
- **修复**：action 用类型 OptimizationProposalReviewAction 约束并对未命中分支显式 throw（或 exhaustive switch），新增 review 动作时强制更新映射，避免误把未知动作当成‘需更多分析’。

---

## 12. 项目目录结构与模块边界

> 范围：`app/{routers,runtime,services}, frontend/src/**, scripts/, tests/, 根目录`

**维度小结**：近期"上帝模块"拆分整体是健康的：claude_runtime 通过回调注入解耦了 orchestrator/eval_runner（无双向耦合、无循环导入），errors.py 异常分层清晰，routers 层薄且只委派，前端 types/feedback.ts 正确从生成的 api.ts 派生（单一事实来源），feedback-workspace 子组件拆分边界基本合理；但 app/runtime 已退化为 50 个文件平铺的"扁平大杂烩"——stores/schemas/models/orchestration/integrations 五类职责挤在同一层无子包，命名约定不一致（models vs schemas、feedback_jobs vs feedback_job_*）、状态机对 batch/task 只校验成员不校验转移、services 层单薄而同质的协调器却留在 runtime、根目录残留一次性调试脚本，这些是机械碎片化与边界模糊的残余债务，影响可导航性与扩展性。

#### [DS-1] 🟨 中 app/runtime 退化为 50 文件平铺包，5 类异质职责混居同层、无子包导航

- **位置**：`app/runtime/:目录共 50 个 .py；__init__.py 为 0 字节`
- **原则**：高内聚低耦合 / SRP / 可维护性(可导航)
- **证据**：ls 显示 app/runtime 下 50 个 .py 同层平铺，可机械分为：16 个 *_store.py（feedback_batch_store/feedback_case_store/feedback_job_store/...）持久化；9 个 schema 文件（schemas.py、feedback_schemas.py、7 个 *_response_schemas.py）；2 个 Pydantic *_models.py（external_governance_models/feedback_compensation_models）；编排簇（feedback_job_orchestrator/feedback_job_factory/feedback_jobs/agent_job_runner/feedback_eval_runner）；集成（runtime_langfuse/external_governance）。wc -c app/runtime/__init__.py = 0，无 __all__/无 re-export 聚合，无任何包级导航。
- **修复**：按职责拆子包：app/runtime/stores/（16 个 *_store + feedback_store facade）、app/runtime/schemas/（schemas.py + *_response_schemas + feedback_schemas + *_models）、app/runtime/orchestration/（job_orchestrator/job_factory/job_runner/eval_runner/feedback_jobs 提示词）、app/runtime/integrations/（runtime_langfuse/external_governance）。各子包 __init__.py 做显式 re-export 保持外部导入面不变，分轮迁移、每轮跑全量测试。

#### [DS-2] 🟨 中 state_machines 对 batch/task 只校验状态成员、不校验转移合法性，且文档声称已集中校验

- **位置**：`app/runtime/state_machines.py:75-120（_TRANSITIONS 仅含 job/execution_job）；调用点 feedback_batch_store.py:308、feedback_task_store.py:143`
- **原则**：鲁棒性(状态机合法转移) / 一致性(代码与文档)
- **证据**：_KNOWN_STATES 声明了 4 台状态机（job/execution_job/batch/task），但 _TRANSITIONS 只定义 job 与 execution_job 两台的转移规则。validate_transition 第 115-117 行：transitions = _TRANSITIONS.get(machine); if transitions is None: return —— 因此对 batch/task 直接返回，只校验 target 在 BATCH_STATES/TASK_STATES（line 109-110），不校验 current->target 是否合法。而 feedback_batch_store.py:308 validate_transition("batch", row.status, status) 与 feedback_task_store.py:143 validate_transition("task", row.status, status) 是状态最多（21/11 个）的两台机，恰恰没有转移护栏。docs/CODE_AND_DOCS_REVIEW.md:341 在【已完成】中称『job、execution job、batch、task 的关键状态转移已走集中校验』，与实现不符；tests/test_state_machines.py 也只覆盖 job/execution_job。
- **修复**：为 _TRANSITIONS 补齐 batch 与 task 的转移邻接表（至少覆盖 draft→attribution_running→...→completed、pending_execution→execution_planning→... 等关键路径），并在 tests/test_state_machines.py 增加 batch/task 的合法/非法转移用例；同时修正 CODE_AND_DOCS_REVIEW.md 的【已完成】表述以匹配实际覆盖范围。

#### [DS-3] 🟨 中 services 层仅 1 文件，结构同质的协调器 FeedbackJobOrchestrator 却留在 runtime，runtime/services 边界模糊

- **位置**：`app/services/ 与 app/runtime/feedback_job_orchestrator.py:app/services/ 仅 execution_application.py(+空 __init__)；feedback_job_orchestrator.py:25-39`
- **原则**：高内聚低耦合(模块边界清晰) / SRP / 一致性
- **证据**：app/services 只有 ExecutionApplicationService（execution_application.py:30 docstring『Coordinates execution-plan application across workspace files and store state.』）。而 feedback_job_orchestrator.py:26 的 FeedbackJobOrchestrator docstring『Coordinates feedback-loop Agent jobs while FeedbackStore owns persistence.』—— 同样是『协调 store + profiles + 运行』的应用服务职责，却放在 runtime 里；FeedbackEvalRunner、FeedbackJobFactory 同理。两个结构同质的协调器被分置于 services/ 与 runtime/，无判定准则，main.py 也分别从两处导入。
- **修复**：明确『application service = 跨 store/runtime/profile 的用例协调』归 app/services，『runtime = SDK 适配+持久化+schema』。将 FeedbackJobOrchestrator/FeedbackEvalRunner/FeedbackJobFactory 迁入 app/services（或在两者间二选一统一），并在 README/架构文档给出一句话边界定义，让新增协调逻辑有明确落点。

#### [DS-4] 🟨 中 Agent profile 名称已有常量却在多个 store 里硬编码字符串字面量，跨模块靠魔法串隐式耦合

- **位置**：`app/runtime/feedback_job_store.py 等:agent_profiles.py:12-15 定义常量；feedback_job_store.py:112,171；feedback_batch_plan_store.py:93,494；feedback_execution_store.py:67,88`
- **原则**：高内聚低耦合(禁止字符串字面量跨模块耦合) / DRY / 扩展性
- **证据**：agent_profiles.py:13-15 已定义 ATTRIBUTION_ANALYZER_PROFILE = "attribution-analyzer" / PROPOSAL_GENERATOR_PROFILE = "proposal-generator" / EXECUTION_OPTIMIZER_PROFILE = "execution-optimizer"，但 feedback_job_store.py:112 profile_name="attribution-analyzer"、:171 profile_name="proposal-generator"，feedback_batch_plan_store.py:93/494、feedback_execution_store.py:67/88 仍直接写裸字符串。重命名某个 profile 需 grep 全仓替换。docs/CODE_AND_DOCS_REVIEW.md:76 的 [B-M2]『魔法字符串…提取 app/runtime/constants.py』仍为未完成项，且 constants.py 不存在。
- **修复**：在上述 store 里 from .agent_profiles import ATTRIBUTION_ANALYZER_PROFILE, ... 复用常量替换裸串；并补一条单测断言 store 写入的 profile_name 取自常量，防止回归。

#### [DS-5] 🟩 低 命名约定不一致：Pydantic *_models.py vs ORM *Model vs *_schemas.py；feedback_jobs.py 名实不符与 job 簇撞名

- **位置**：`app/runtime/（多文件）:external_governance_models.py:18；feedback_compensation_models.py:20；runtime_db.py:33起(*Model)；feedback_jobs.py:92起`
- **原则**：可维护性(命名一致、可读、可导航) / 一致性
- **证据**：external_governance_models.py:18 class ExternalGovernanceNotificationRecord(BaseModel)、feedback_compensation_models.py:20 class ExecutionCompensationRecord(BaseModel) —— *_models.py 装的是 Pydantic；而 runtime_db.py:33 class SessionRecordModel(Base) 等才是 SQLAlchemy ORM（类名后缀 *Model）；同时 schemas.py / *_response_schemas.py 也是 Pydantic。三套词（models/schemas/Model 后缀）指代重叠又不一致，无文档约定。另：feedback_jobs.py 文件名像『jobs 实体』，实际内容是 attribution_prompt/proposal_prompt/...（line 92,120,155,194 提示词构建）+schema 字段集，与 feedback_job_store/feedback_job_factory/feedback_job_orchestrator/agent_job_runner 形成 6 个高度相似命名，按名定位困难。
- **修复**：统一约定：ORM 留在 runtime_db（保留 *Model）；所有 Pydantic 入参/出参统一归 schemas 子包并用 *_schemas.py 后缀，废弃含义含糊的 *_models.py 命名（内部记录可叫 *_records 或并入 schemas）。把 feedback_jobs.py 改名为 feedback_prompts.py（或 job_prompts.py）以名副其实，减少 job 簇撞名。

#### [DS-6] 🟩 低 根目录残留一次性调试脚本 main_check_attribution_agent.py，硬编码特定 case 且穿透私有内部

- **位置**：`main_check_attribution_agent.py:20-22,129,135,141,143,180,188`
- **原则**：SRP/边界清晰 / 可维护性 / 高内聚低耦合(穿透私有)
- **证据**：项目根目录的 main_check_attribution_agent.py:21 硬编码 DEFAULT_CASE_ID = "fbc-9b69d469-77ad-461a-aced-a8bd6c4b0120"、:22 DEFAULT_CASE_TITLE = "数据不全BBB"，是针对单个案例的调试脚本；grep 显示无任何 Makefile/tests/docs 引用它。脚本大量穿透私有方法：:129 feedback_store._materialize_evidence_files、:135 feedback_store._current_agent_version_id()、:141 feedback_store._write_job_input、:143 runtime._run_profile_json、:180/188 runtime._provider_configured()，违反封装且会随内部重构而碎裂。
- **修复**：将其移出根目录到 scripts/（如 scripts/debug_attribution.py），移除 DEFAULT_CASE_ID/TITLE 硬编码改为必填参数，并尽量改走公开 API（如 run_attribution_job）而非私有 _ 方法；若仅为历史排障可直接删除。

#### [DS-7] 🟩 低 feedback_store facade 残留拆分前 JSONL 时代死代码：no-op 方法 + 11 个无人引用的兼容路径属性

- **位置**：`app/runtime/feedback_store.py:88-139,241-245`
- **原则**：可维护性(DRY/无死代码) / SRP
- **证据**：feedback_store.py:88 注释自承『Compatibility-only paths. They are not authoritative and are not created.』，:89-98 定义 runs_dir/signal_dir/.../external_webhooks_path，:105-139 又派生 runs_path/signals_path/events_path/.../tasks_path 等属性；:241-245 _append_jsonl 直接 return None、_read_jsonl 直接 return []（空实现）。全仓 grep .runs_path/.signals_path/... 与 .runs_dir/.signal_dir/... 在 feedback_store.py 之外零引用，_append_jsonl/_read_jsonl 也只在本文件被空定义、无调用方。属拆分迁移后未清理的死代码。
- **修复**：删除这批兼容路径属性（runs_dir~external_webhooks_path 及对应 *_path property）与空实现的 _append_jsonl/_read_jsonl；若担心隐式外部依赖，先加一轮 deprecation 日志或确认无下游后直接移除。

#### [DS-8] 🟩 低 前端文件名后缀与主导出名不符（*Workspace.tsx 导出 *Panel），且 selectors.ts 是低内聚工具大杂烩

- **位置**：`frontend/src/components/feedback-workspace/:BatchesWorkspace.tsx:41；CasesWorkspace.tsx:64；ProposalWorkspace.tsx:263；EvalWorkspace.tsx:191；SignalsWorkspace.tsx:11；selectors.ts 全文 40+ 导出`
- **原则**：可维护性(命名一致/可导航) / 高内聚
- **证据**：BatchesWorkspace.tsx:41 export function BatchesPanel、CasesWorkspace.tsx:64 export function CasesPanel、ProposalWorkspace.tsx:263 export function ProposalsPanel、EvalWorkspace.tsx:191 export function EvalPanel、SignalsWorkspace.tsx:11 export function SignalsPanel —— 文件名一律 *Workspace 但主导出一律 *Panel，按符号名搜不到文件。selectors.ts（24KB，40+ 导出）名为 selectors 却混装领域行装配(buildSourceRows/buildBatchSourceRows)、状态文案与色调(attributionStatusTone/jobStatusTone)、校验错误解析(validationErrorItems/validationErrorPath)、eval 助手(evalCaseEditDraft/parseEvalCaseLabels) 与通用工具(latest/latestItem/rawString)，并非真正的 selector，低内聚。
- **修复**：统一文件名与主导出（要么文件改为 BatchesPanel.tsx，要么导出改名 BatchesWorkspace）。将 selectors.ts 按主题拆分为 sourceRows.ts / statusFormatters.ts / validationErrors.ts / evalHelpers.ts（+ 一个真正的 selectors 或 utils），降低单文件认知负荷。

---

## 13. 测试质量与覆盖

> 范围：`tests/**`

**维度小结**：总体测试质量明显高于一般产品：rollback/补偿/幂等路径覆盖非常扎实（cases_and_jobs、execution、batch_plans 几乎每个写操作都有"下游更新失败→全量回滚"用例，apply 失败→工作区恢复→补偿记录的闭环也被端到端验证），openapi 契约与 store 拆分边界对齐良好，monkeypatch 边界（_run_profile_json/query/sender）干净。主要短板集中在三类纵深缺失：batch/task 状态机的非法转移合法性从未被断言（生产代码调用了 validate_transition 但该机器无转移表，等于空校验且无测试）、编排器/评估器的异常与超时分支（AGENT_TIMEOUT / EVAL_CASE_RUNTIME_ERROR / fail_eval_run）未被触发、API 鉴权(401)与并发竞态(并发 apply/建 job)基本无测试。

#### [TS2-1] 🟧 高 batch/task 状态机的非法转移合法性既无生产约束也无测试纵深

- **位置**：`app/runtime/state_machines.py:36-73, 75-95, 115-120`
- **原则**：鲁棒性/一致性：状态机定义了状态集却未定义/未测试合法转移，调用点制造了"已校验"的假象（dead guard）。
- **证据**：BATCH_STATES(36-59)、TASK_STATES(61-73) 列出了 20+/11 个状态，但 _TRANSITIONS(75-95) 只为 "job" 和 "execution_job" 定义了转移表。validate_transition 中: `transitions = _TRANSITIONS.get(machine); if transitions is None: return`(115-117)。生产代码 feedback_batch_store.py:308 `validate_transition("batch", row.status, status)` 和 feedback_task_store.py:143 `validate_transition("task", row.status, status)` 因此只校验 target 是否已知，永不拒绝任何非法转移（如 completed->draft）。tests/test_state_machines.py 仅断言了 execution_job/job：`validate_transition("execution_job",...)`、`match="completed -> running"`、`match="Unknown job status"`，无任何 batch/task 非法转移用例。
- **修复**：在 _TRANSITIONS 中补齐 "batch"/"task" 的合法转移表（或在 validate_transition 中对有 _KNOWN_STATES 但无转移表的 machine 显式 raise 配置错误，避免静默放行）；并在 test_state_machines.py 增加 batch/task 的非法转移用例（如 completed->execution_planning、regression_passed->draft 应抛 StateTransitionError）。

#### [TS2-2] 🟧 高 编排器的 Agent 超时/运行时异常分支(AGENT_TIMEOUT/AGENT_RUNTIME_ERROR)未被任何测试触发

- **位置**：`app/runtime/feedback_job_orchestrator.py:65-68, 108-111, 146-149, 186-189`
- **原则**：鲁棒性：异常分层的关键产出（错误码区分超时与运行时错误、失败后 job/case 落到 failed）属于核心可恢复路径却未测。
- **证据**：四个 run_*_job 各自带有 `except asyncio.TimeoutError -> fail_job/fail_execution_job(error_code="AGENT_TIMEOUT")` 与 `except Exception -> ...(error_code="AGENT_RUNTIME_ERROR")` 分支（如 65-68）。超时来源于 agent_job_runner.py:116 `await asyncio.wait_for(collect(), timeout=profile.max_runtime_seconds)`。但所有 fake_query/_run_profile_json 的 monkeypatch 都返回正常 JSON 或断言不被调用（如 test_feedback_store_execution.py:717 fail_query 仅用于断言 query 不被调用）；grep `AGENT_TIMEOUT|raise.*Timeout` 在 tests/ 下无任何命中。store 级 fail_job 有回滚测试，但编排器把异常映射成 AGENT_TIMEOUT vs AGENT_RUNTIME_ERROR 的逻辑从未被验证。
- **修复**：新增编排器级用例：monkeypatch runtime._run_profile_json 抛 asyncio.TimeoutError 与普通 Exception，断言 run_attribution_job/run_proposal_job/run_execution_job 返回的 job error_json.error_code 分别为 AGENT_TIMEOUT 与 AGENT_RUNTIME_ERROR，且 case/task 状态为可重试态。
- **校验复核**：finding 表述准确。唯一可补正的细节：第四个分支位于 run_batch_optimization_plan（146-149 行）而非建议段所列的三个方法之一，建议新增的编排器级用例应覆盖全部四个入口（run_attribution_job/run_proposal_job/run_batch_optimization_plan/run_execution_job），通过 monkeypatch run_profile_json 分别抛 asyncio.TimeoutError 与普通 Exception，并需先令 provider_configured 返回 True 且绕过 deterministic/offline 短路径，断言 error_json.error_code 分别为 AGENT_TIMEOUT 与 AGENT_RUNTIME_ERROR。

#### [TS2-3] 🟨 中 评估器(FeedbackEvalRunner)的部分失败与整体失败补偿路径未测，且 e2e 用 fake 整体替换掉真实 runner

- **位置**：`app/runtime/feedback_eval_runner.py:47-89`
- **原则**：鲁棒性：'多用例中一个失败其余通过' 这种部分失败语义是评估闭环的关键正确性，未覆盖。
- **证据**：run_feedback_eval 有两层异常处理：单用例失败 `except Exception -> append_eval_run_item(status="failed", error_json={"error_code":"EVAL_CASE_RUNTIME_ERROR"...})`(73-82)，以及 run 级 `except Exception -> fail_eval_run(error_code="EVAL_RUN_RUNTIME_ERROR")`(84-89)。grep 显示 tests/ 中仅 test_feedback_store_eval_agents.py:124 `eval_run["result_status"]=="failed"`，但那是 checks 校验失败（requires_tool_use）而非运行时异常——run_chat 正常返回。EVAL_CASE_RUNTIME_ERROR / EVAL_RUN_RUNTIME_ERROR / fail_eval_run 在 tests/ 下无命中。而 test_api_execution_optimizer.py:574 `monkeypatch.setattr(module.runtime, "run_feedback_eval", fake_run_feedback_eval)` 在 e2e 中整体替换了真实 runner，因此回归闭环里真实评估器的失败处理完全未被执行。
- **修复**：增加用例：构造 2 个 active eval_case，monkeypatch run_chat 对其中一个抛异常，断言 eval_run 含一个 failed item(EVAL_CASE_RUNTIME_ERROR)且整体 finish 而非 fail；再单独 monkeypatch 使 create/append 之外的步骤抛异常，断言走 fail_eval_run(EVAL_RUN_RUNTIME_ERROR)。

#### [TS2-4] 🟨 中 API 鉴权(require_api_key 的 401 分支)从未被测试，越权/缺凭证输入无覆盖

- **位置**：`app/main.py:91-95`
- **原则**：鲁棒性/安全：异常/越权输入（缺凭证、错误 scheme、错误 key）是必须验证的边界。
- **证据**：require_api_key: `if not settings.api_key: return; if not credentials or credentials.scheme.lower()!="bearer" or credentials.credentials!=settings.api_key: raise HTTPException(401, "Invalid API key")`。该依赖挂在 chat/openai/sessions/eval/feedback 等几乎所有路由上(main.py:99-106)。但 test_api_execution_optimizer.py:_load_app 与 test_openapi_export.py 均 `monkeypatch.setenv("API_KEY", "")`，使 `if not settings.api_key: return` 永远短路；grep `401|require_api_key|Bearer` 在 tests/ 下仅命中 langfuse 的 base64 Authorization，与鉴权无关。即缺失/错误 Bearer token 的 401 行为零覆盖。
- **修复**：新增用例：以非空 API_KEY 构建 app，分别用无 Authorization 头、`Authorization: Basic xxx`、错误 token 请求任一受保护端点，断言 401；用正确 Bearer 断言 200。

#### [TS2-5] 🟨 中 并发纵深仅覆盖信号写入，未覆盖 job 去重/apply 等真正有竞态风险的路径

- **位置**：`tests/test_runtime_db.py:17-35`
- **原则**：鲁棒性：事务边界/幂等在并发下的正确性是闭环核心，单线程幂等断言不能证明并发安全。
- **证据**：唯一的并发测试 test_feedback_store_sqlite_handles_concurrent_signal_writes 用 ThreadPoolExecutor(8) 并发 create_signal 并断言 24 个唯一 id。但更高风险的并发路径未测：create_attribution_job/create_proposal_job/create_execution_job 都依赖'复用现有未完成 job'的幂等判定（test_feedback_store_cases_and_jobs.py:126/405 等只在单线程下断言复用同一 job_id），以及 execution apply 会写工作区文件并创建版本快照（test_api_execution_optimizer.py 的 apply 用例均为串行）。grep 显示 tests/ 下并发原语仅此一处 ThreadPoolExecutor。
- **修复**：至少补一个并发用例：对同一 feedback_case 并发调用 create_attribution_job(force=False)，断言只生成一个 queued job（其余复用），以验证 SQLite 事务+去重逻辑在并发下的幂等。

#### [TS2-6] 🟩 低 测试夹具重复：_settings/_store/_load_app 在多文件各写一份且签名分叉

- **位置**：`tests/test_agent_version_store.py:10-16`
- **原则**：DRY/易维护：相同夹具多份副本，新增 settings 字段或路径约定时需多处同步，易漂移（同名 _store 不同语义尤其有迷惑性）。
- **证据**：存在两个同名 _store 但签名不同：feedback_store_test_utils.py:50 `_store(tmp_path)->(FeedbackStore, settings)`；test_agent_version_store.py:10 `_store(tmp_path)->AgentVersionStore`。同时 _settings 在 feedback_store_test_utils.py:22 与 test_claude_runtime.py:45 各有一份，AppSettings 构造高度重复（WORKSPACE_DIR/CLAUDE_ROOT/CLAUDE_HOME 等多键拷贝）。test_api_execution_optimizer.py:13 _load_app 又被 test_api_error_handlers.py 跨文件 import 复用，但与 conftest 缺失（项目无 conftest.py，见 pytest.ini）形成共享/复制混用。
- **修复**：引入 tests/conftest.py，把 settings/store/app 夹具收敛为 pytest fixture（如 feedback_store、app_module），并将 test_agent_version_store 的 _store 重命名为 _agent_version_store 以消除同名歧义。

#### [TS2-7] 🟩 低 大量 store 测试经 `from feedback_store_test_utils import *` 通配导入，隐式依赖 __all__ 名单

- **位置**：`tests/feedback_store_test_utils.py:207`
- **原则**：高内聚低耦合/易维护：通配导入+动态 __all__ 让测试对工具模块产生隐式全量耦合，重命名/删除工具符号不会在测试文件触发可见的 import 错误，可读性与可追踪性下降。
- **证据**：feedback_store_test_utils.py 末尾 `__all__ = [name for name in globals() if not name.startswith('__')]`（动态导出一切非 dunder 名），test_feedback_store_sources/proposals/batch_plans/cases_and_jobs/eval_agents/execution 全部 `from feedback_store_test_utils import *`。这把 pytest、ValidationError、asyncio、ClaudeRuntime、各 schema 等都隐式注入测试模块命名空间，测试文件里直接用 pytest.raises/ValidationError 却看不到显式 import。
- **修复**：将 __all__ 收敛为显式的辅助函数与必要符号清单（_store/_settings/_record_run/_create_*），让各测试文件显式 import pytest、ValidationError 等；或将这些辅助迁入 conftest 以 fixture 暴露。

---

## 14. 代码与文档一致性

> 范围：`README.md, docs/*.md, docs/openapi.json ↔ 代码`

**维度小结**：双向核对后整体一致性良好：docs/openapi.json 与真实路由完全同步（70 条路径逐一比对，仅 include_in_schema=False 的 GET / 被正确排除），README 的路由索引/env 默认值/Agent profile/四套 workspace 命名均与 settings.py、.env.example、docker-compose.yml 一致，架构文档 §18 API 设计与代码逐条吻合；问题集中在三类「过期/未随重构校准」的文档：PRODUCT_ADJUSTMENT_PLAN 的 API 清单有错名+不存在的路由且状态机命名与权威 state_machines.py 完全不符、CODE_AND_DOCS_REVIEW 的总览/结论仍以已不存在的「5022 行上帝模块」为现状、架构文档 §5 目录树 claude-roots 命名与代码及其自身 §6 矛盾、AGENT_GOVERNANCE 对 .codex 现状的描述已被后续提交推翻。

#### [CD-2] 🟨 中 PRODUCT_ADJUSTMENT_PLAN §8 状态机命名与权威 state_machines.py 完全不符

- **位置**：`docs/FEEDBACK_OPTIMIZATION_PRODUCT_ADJUSTMENT_PLAN.md:528-556`
- **原则**：一致性
- **证据**：§8.2 优化批次状态列出 eval_cases_generating/eval_cases_ready/attribution_ready/plan_generating/pending_plan_approval/plan_rejected/execution_applied 等；§8.1 反馈信息状态列出 new/annotated/eval_case_ready/attribution_ready/included_in_batch/validated；§6.1 PATCH 示例用 status=selected_for_optimization。逐一 grep app/runtime 与 frontend/src，这些状态名命中均为 0。权威 app/runtime/state_machines.py:36-73 的 BATCH_STATES 实为 attribution_completed/optimization_plan_queued/pending_approval/rejected/applied_pending_regression 等；feedback_source_store.py 的反馈源 status 实为 matched/pending_correlation/resolved/triaged。
- **修复**：把 §8.1/§8.2 与 §6.1 示例的状态枚举改为引用 state_machines.py 的 BATCH_STATES/TASK_STATES 实际命名（或在文档中标注「目标态，未实现」），避免开发者按文档写出永远不会匹配的状态判断。最佳做法是文档状态表由 state_machines.py 反射生成。

#### [CD-3] 🟨 中 CODE_AND_DOCS_REVIEW 总览表与「重点结论」仍以已不存在的 4 个上帝模块为现状

- **位置**：`docs/CODE_AND_DOCS_REVIEW.md:14,31,321`
- **原则**：一致性
- **证据**：§0 评分表 line 14 与 §1.1[B-S1] line 31、§6 结论 line 321 仍称 `feedback_store.py 5022 行 / ExternalFeedbackWorkspace.tsx 5124 行 / main.py 1659 行 / claude_runtime.py 1403 行——4 个上帝模块`，line 321 结论「核心债务在 4 个上帝模块…第一优先是拆分这四个文件」。实测当前 wc -l：feedback_store.py=298、claude_runtime.py=791、main.py=133、ExternalFeedbackWorkspace.tsx=405，且同文档 §7.1（line 339-372）已逐轮记录这些文件均已拆到 800 行阈值以下。§1-§4 所有 main.py:NNN（如 line 32 main.py:259-1651、line 47 main.py:131-189、line 270 main.py:1350,1379,1419）行号也指向已不存在的 133 行 main.py。
- **修复**：在 §0 表格与 §6 结论顶部明确标注「以下为重构前基线，现状见 §7」，或直接重写 §6 结论第 1 条为「上帝模块已拆分，剩余债务为 dict→Pydantic 收口与跨 Session 事务」。§4.1[D-S1]（line 260 称 README:418 DEFAULT_ALLOWED_TOOLS 缺 Skill）也已被修复（README:424 现含 Skill）却仍列为未决严重项，应一并标记为已解决。

#### [CD-1] 🟩 低 PRODUCT_ADJUSTMENT_PLAN §6.3 API 清单含错名路由与不存在的路由

- **位置**：`docs/FEEDBACK_OPTIMIZATION_PRODUCT_ADJUSTMENT_PLAN.md:426,429`
- **原则**：一致性
- **证据**：文档 §6.3 列出 `POST /api/feedback-optimization-batches/{batch_id}/optimization-plans`（复数 plans）与 `GET /api/feedback-optimization-batches/{batch_id}/execution-jobs`。代码 app/routers/feedback_batches.py:101-102 实际路由为单数 `/feedback-optimization-batches/{batch_id}/optimization-plan`；对 batch 的 `GET .../execution-jobs` 在 app/ 与 docs/openapi.json 中均不存在（grep 命中 0）。
- **修复**：把 §6.3 第 426 行 `optimization-plans` 改为单数 `optimization-plan`，并删除第 429 行不存在的 `GET .../{batch_id}/execution-jobs`（execution-jobs 实际挂在 optimization-tasks 下）。该文档头部声明「v1 已实现并持续校准」，API 段应以 docs/openapi.json 为准逐条核对。

#### [CD-4] 🟩 低 架构文档 §5 目录树 claude-roots 命名与代码及其自身 §6 矛盾

- **位置**：`docs/FEEDBACK_OPTIMIZATION_MULTI_AGENT_ARCHITECTURE.md:318-324`
- **原则**：一致性
- **证据**：§5 容器目录结构里 claude-roots 只列三项且用短名：`main/`、`attribution/`、`proposal/`（缺 execution-optimizer，且无 -analyzer/-generator 后缀）。但同文档 §6 compose 片段 line 375-377 与代码 settings.py:55-57、docker-compose.yml:79-81、agent_profiles.py:74/89/107 一致使用四项全名 `claude-roots/attribution-analyzer`、`/proposal-generator`、`/execution-optimizer`。§3 统一命名表（line 127-130）也用全名，故 §5 与本文档其余部分及代码自相矛盾。
- **修复**：把 §5 line 321-324 的 claude-roots 子树改为 main/、attribution-analyzer/、proposal-generator/、execution-optimizer/ 四项全名，与 §3/§6 及 settings.py 对齐。

#### [CD-5] 🟩 低 AGENT_GOVERNANCE §5.C 称 .codex/rules 与 hooks 为空，已被后续提交推翻

- **位置**：`docs/AGENT_GOVERNANCE_REFLECTION_AND_PLAN.md:227,230`
- **原则**：一致性
- **证据**：§5.C line 227 `.codex/rules/ 当前为空（指令里引用了 project.rules 但仓库里没有），建立两类规则：architecture.rules…verify.rules`；line 230 `.codex/hooks.json 当前为空`。实际 .codex/rules/ 现已包含 architecture.rules、project.rules、verify.rules（即本节建议新建的文件），.codex/size-budget.yaml 也已存在；line 94 文档还声明「原文件不做即时修改…需用户审阅本文档后批准」，但 git 历史（126a2e6 加强 Codex 治理护栏、2210f95 完善反馈优化闭环治理）显示建议已落地。
- **修复**：在 §5.C/§5.E 增加「已落地」标注或改为过去式，说明 architecture.rules/project.rules/verify.rules/size-budget.yaml 已创建、hooks.json 已就位（hooks 数组仍为空待接入），避免读者误以为治理护栏尚未存在。

#### [CD-6] 🟩 低 架构文档 §18.11 Agent 版本路由清单遗漏 file-diff

- **位置**：`docs/FEEDBACK_OPTIMIZATION_MULTI_AGENT_ARCHITECTURE.md:1471-1478`
- **原则**：一致性
- **证据**：§18.11 列出 6 条 agent-versions 路由含 `GET /api/agent-versions/main/diff`，但未列 `GET /api/agent-versions/main/file-diff`。代码 app/routers/agent_versions.py 与 docs/openapi.json 均有 file-diff（GET /api/agent-versions/main/file-diff），README:145 也已列出。
- **修复**：在 §18.11 补上 `GET /api/agent-versions/main/file-diff`，与 README 和 openapi.json 对齐；或在 §18 顶部声明完整接口以 openapi.json 为准（README 已采用此声明）。

---

## 15. 已核查并剔除的疑似问题（透明记录）

对抗式校验阶段否决了 2 条 review 智能体提出但源码不支持的 finding，记录如下以示评审未"宁滥勿缺"：

- **[FO-6] AgentJobRunner 用 print 输出告警而非日志器** —— 剔除理由：逐条核对：agent_job_runner.py:139-141 确实是 `except Exception as exc:` / `print(f"[WARN] failed to format Agent output: {exc}", flush=True)` / `return None`，位于 format_agent_text 方法（def 在 118 行）。该模块 import 区（第 1-12 行）确无 import logging / logger，print 也仅此一处（第 140 行）。这些字面事实属实，行号正确。 但 finding 的核心定性与建议是误报。 …（详见对抗式校验记录）
- **[FC-9] clientConfig/actionId 在批次→任务详情链路上深层 prop 钻取（5-6 层）** —— 剔除理由：读源码核对后，finding 的核心证据链与机制描述存在多处实质性错误，不能在源码中确证其“5-6 层深层 prop 钻取”及“actionId 逐层透传”的表述。 1) clientConfig 并未经 BatchesPanel 透传。ExternalFeedbackWorkspace.tsx:198-207 中 renderBatchTasksDetails 是一个内联 render-prop 闭包，clientConfig 在第 200 行由闭包捕获后直接传给 <TasksDetails>，并非作为 prop 穿过 BatchesPanel。 …（详见对抗式校验记录）

---

## 16. 整改路线图（按优先级）

### P0 · 立即（鲁棒性红线，做了一半比不做更危险）

1. **[已完成] 修复状态机 dead guard**：`batch` / `task` 已补完整转移表；`validate_transition` 对缺失转移表改为显式错误；`tests/test_state_machines.py` 已覆盖非法转移。（SM-1 / FS-1 / DS-2 / TS2-1）
2. **[已完成] 状态机全实体覆盖**：`case` / `eval_run` / `proposal` / `external_governance_item` 已纳入 `state_machines.py`，关键写入路径统一调用 `validate_transition`。（SM-2 / SM-4）
3. **[已完成] 执行应用幂等与原子性**：`ExecutionApplicationService` 已加应用锁、ready 前置校验、基线版本校验、应用后快照、失败补偿记录与日志留痕；`mark_execution_job_applied` 已在 DB 事务内对 execution job / task 重新加锁校验，并通过 `status='ready'` 条件更新阻止重复应用覆盖第一次结果；批次路由已委派 `run_and_apply_execution_job`。（FO-1 / FO-5 / BA-1）
4. **[已完成] 收紧过期体积基线与自动门**：当前仓库不再使用 `.codex/size-budget.yaml`；治理硬门统一由 `scripts/check_codex_governance.py --mode fail` 对比 git base 检查行数、函数、类、路由和状态机缺表；Stop hook 与 GitHub Actions 已接入同一硬门。

### P1 · 近期（一致性 / 可维护性 / 测试纵深）

5. **[已完成主体] Schema 单源**：前端 `ChatRequest` 已派生自 OpenAPI，手写 `ChatResponse` 已删除；response schema / record / output normalizer 已拆包，`evidence_refs` 校验路径统一。`schemas.py` 与 `feedback_schemas.py` 保留为后端核心契约文件。（SC-1/2/6 / FT-1/2）
6. **[已完成收敛] 拆"换皮上帝模块"**：不可达 Cases/Eval/Proposal 旧面板已删除；`useFeedbackWorkspaceActions` 从 642 行收敛到 319 行，只保留反馈信息、批次、执行、回归主流程动作。若未来动作再次增长，再按领域拆 hook。（FC-2）
7. **[已完成] 编排器去样板**：`FeedbackJobOrchestrator` 已抽 `_run_profile_json_job`，四类 Agent job 共享异常映射、离线回退、schema 期望与完成/失败收口。（FO-2）
8. **[已完成] 事务边界**：`discard_current_attribution` / `reset_batch_attribution` 已在单事务内收集 DB 变更，提交后再清理临时目录，避免事务内 `rmtree`。（FS-3 / FS-4）
9. **[已完成] 补测试纵深**：已补状态机非法转移、Agent 编排异常、鉴权 401、并发 job 去重、执行应用与回滚补偿相关测试。（TS2-1~5）
10. **[已完成] 清死代码**：`ClaudeRuntime` 死委派、不可达前端面板、旧 debug 脚本、重名/失配模型与旧 facade 兼容面已清理。（RC-3/8 · FC-1/3 · SC-4/5 · DS-7 · FT-1）

### P2 · 结构与可导航性

11. **[已完成] `app/runtime` 拆子包**：`stores/`、`response_schemas/`、`records/`、`integrations/`、`prompts/`、`normalizers/` 已落地，`app/runtime` 顶层 Python 文件数已收敛到 25。（DS-1）
12. **[已完成] 协调器归位**：`FeedbackJobOrchestrator` / `FeedbackEvalRunner` 已移入 `services`；`FeedbackJobFactory` 保留在 store 侧作为持久化 job row 工厂。（DS-3）
13. **[已完成] 统一命名约定**：`*_models.py` 业务记录已改为 `*_records.py`，`feedback_jobs.py` 已改为 `prompts/feedback_prompts.py`，根目录一次性调试脚本已删除。（DS-5/6/8）
14. **[已完成] 文档校准**：`PRODUCT_ADJUSTMENT_PLAN`、架构文档目录树、README 与 OpenAPI/types 已同步到当前路由、状态机和目录结构。（CD-1~6）

---

## 17. 重点结论

1. **重构有效，方向正确**：R1 的产品级评审 → 治理护栏 → 拆分这条链真实清偿了体积债，项目从"架构级危机"降到"中等结构债"。
2. **债务换形态存活**：现有治理只覆盖"行数"这一个可机器判定维度，所以剩余债沿"行数看不见的维度"继续累积——状态机、schema 多轨、跨事务/幂等、隐式耦合、拆分残留死代码。
3. **最高优先级是状态机伪治理**：这是"做了一半的治理比不做更危险"的典型，且**违反项目自己在 R1 后立下的红线**。它同时出现在 SM/FS/DS/TS 四个维度，是本轮最强的交叉证据。
4. **根因不在某段代码，而在治理闭环**：详见配套的 [`AGENT_GOVERNANCE_REFLECTION_AND_PLAN_R2.md`](./AGENT_GOVERNANCE_REFLECTION_AND_PLAN_R2.md)——护栏需从"数行数"升级为"查结构"，并从"人工 warn"升级为"提交/CI 强制"。
