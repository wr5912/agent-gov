# 产品级代码与文档评审报告

> 评审范围：`master` 分支（提交 `f7cd0b2` 之后的工作树）
> 评审视角：面向对象、高内聚低耦合、可维护性、扩展性、灵活性、鲁棒性、代码-文档一致性
> 评审方式：4 个独立审查智能体并行覆盖「后端 Runtime 核心层 / 反馈闭环后端 / 前端 / 文档一致性」，主线再做事实核查
> 总规模：后端 ~12K 行 Python，前端 ~9K 行 TS/TSX，文档 ~3K 行 Markdown

---

## 0. 总览与评分

| 维度 | 现状评分 | 主要风险 |
|---|---|---|
| 模块拆分（SRP） | 🟥 不及格 | `feedback_store.py` 5022 行 / `ExternalFeedbackWorkspace.tsx` 5124 行 / `main.py` 1659 行 / `claude_runtime.py` 1403 行——4 个上帝模块 |
| 内聚 | 🟧 偏低 | 单文件混合 9+ 业务域；状态机、序列化、事务、CRUD 散落 |
| 耦合 | 🟧 偏高 | Agent profile 名硬编码于多处；前端组件直连 fetch；schema 字段双轨 |
| 扩展性 | 🟧 偏低 | 新增一种 job 类型 / Agent profile / governance webhook 需改 5 处以上 |
| 鲁棒性 | 🟧 偏低 | SQLite 多线程未隔离；裸 `except Exception`；事务跨 Session；前端无重试/超时 |
| 安全 | 🟨 一般 | 路径遍历检查简单；`HTTPException(detail=str(exc))` 信息泄露 |
| 文档一致性 | 🟨 一般 | README/架构文档大体同步，存在若干 stale 字段值与遗漏 endpoint |
| 测试覆盖 | 🟨 一般 | happy path 充足，边界、状态机非法转移、并发场景缺失 |

四级标注：🟥 严重 / 🟧 重要 / 🟨 一般 / 🟩 建议。下文按"严重→建议"分级，每条均含 `file:line` 证据、影响、修复方向。

---

## 1. 后端 Runtime 核心层（`app/main.py` + `app/runtime/{claude_runtime,agent_*,policy,settings,runtime_db,session_store,output_formatter,schemas,message_utils,config_mapping}.py`）

### 1.1 🟥 严重

**[B-S1] `app/main.py` 1659 行 / 76 个路由——上帝路由文件**
- 证据：`app/main.py:259-1651` 共 76 个 `@app.{get|post|patch|delete}` 装饰器；其中 `_apply_execution_operations()`（`main.py:131-189`）、`_apply_ready_execution_job()`（`main.py:205-248`）等业务逻辑直接定义在路由文件
- 影响：路由层、验证层、业务层、存储层在同一文件混合，无法独立测试；新增能力必然继续膨胀；变更牵一发动全身
- 修复：按域拆分路由（`app/routers/{chat,agents,feedback,optimization,eval,version,governance}.py`），抽出 `ExecutionService` / `JobApplicationService` 等业务服务层；路由仅做参数校验与服务调度

**[B-S2] `app/runtime/claude_runtime.py` 1403 行 / 51 方法——上帝类**
- 证据：`claude_runtime.py:46-1404` 单一 `ClaudeRuntime` 同时承担：SDK 选项构建、Langfuse 集成、attribution/proposal/batch_plan/execution job 执行、Eval 运行、输出格式化、Telemetry；私有方法嵌套 4-5 层
- 影响：违反 SRP；Langfuse 配置改动需理解全类；无法替换 Job 执行后端
- 修复：拆分为 `ClaudeSDKAdapter`、`JobExecutor`（多态：`AttributionJobExecutor` / `ProposalJobExecutor` …）、`LangfuseIntegration`、`TelemetryCollector`、`OutputFormatProcessor`；用依赖注入装配

**[B-S3] Agent profile 标识符硬编码于多处**
- 证据：`agent_profiles.py:10` 定义 `AgentRole` literal；`claude_runtime.py:578, 799, 839, 886, 924` 等位置以字符串字面量分别比较 `"attribution-analyzer" / "proposal-generator" / "execution-optimizer"`
- 影响：新增一个 Agent profile 需修改 `build_profiles()` + `ClaudeRuntime` 至少 5 处方法；违反开闭原则
- 修复：建立 `AgentProfileRegistry`，profile 由 YAML/JSON 声明加载；路由层只引用 `profile_name`，运行时只引用 `Profile` 对象，禁用字符串比较

**[B-S4] SQLite 在多线程 FastAPI 下并发未隔离**
- 证据：`runtime_db.py:326-336`、`session_store.py:33` 使用单一 `create_engine()` + WAL + `busy_timeout`，但 FastAPI 异步路由 + sync handler 会跨线程访问；`_apply_execution_operations()`（`main.py:131-189`）的文件写入与 DB 写入未在同一事务
- 影响：高并发下 `apply-execution` 等可能重复执行；session 数据丢更；执行 Job 在 DB 与文件系统间出现中间态
- 修复：每线程独立连接（`NullPool` + 线程本地）或迁移到 PostgreSQL；对"执行+落库"用 saga / outbox / 应用级幂等键

### 1.2 🟧 重要

**[B-H1] `except Exception:` 大量出现，吞掉系统异常**
- 证据：`claude_runtime.py:38, 589, 784, 1169` 等
- 影响：`KeyboardInterrupt` / `SystemExit` 不会被排除；监控/调试困难
- 修复：捕获具体类型；保留必要的"最外层兜底"位置时使用 `except Exception as e: logger.exception(...); raise`

**[B-H2] `HTTPException(detail=str(exc))` 信息泄露**
- 证据：`app/main.py:231, 366, 514-516`；`claude_runtime.py:785, 825-827`
- 影响：内部路径、数据库错误细节、Langfuse 端点等暴露给客户端
- 修复：定义 `APIError(code, user_message, internal_detail)`；客户端只回 `user_message`，`internal_detail` 走日志

**[B-H3] `_safe_workspace_target()` 路径校验不充分**
- 证据：`main.py:192-202` 仅检查 `.is_absolute()` 与 `".." in rel.parts`
- 影响：未对符号链接、TOCTOU、`resolve()` 失败做处理；潜在写出 workspace 之外的文件
- 修复：`(base / rel).resolve().relative_to(base.resolve())`；对父目录 `os.path.realpath` 二次验证；新增单元测试覆盖符号链接 / TOCTOU / `%2e%2e`

**[B-H4] `_apply_execution_operations` 回滚链不安全**
- 证据：`main.py:178-189`、`claude_runtime.py:1169-1171`
- 影响：回滚自身失败时无嵌套 try-except，文件系统残留半成品
- 修复：写临时目录 → 校验通过 → `os.replace()` 原子切换；回滚失败时记录 `rollback_error`，向调用方抛 `PartialApplyError`

### 1.3 🟨 一般

- **[B-M1]** 类型注解使用 `Any` 过多（`claude_runtime.py:106, 318, 338`）；建议 TypedDict / Protocol；启用 `mypy --strict`
- **[B-M2]** 魔法字符串（`"main-agent" / "attribution-analyzer-v0.1.0" / "execution-plan-output/v1"`）散落；提取 `app/runtime/constants.py`
- **[B-M3]** 路由层一会儿 `raise ValueError` 一会儿 `HTTPException`，错误映射不一致；统一异常映射中间件

### 1.4 🟩 建议

- **[B-N1]** Langfuse 客户端延迟初始化但 lifespan 未清理；在 `app/main.py` `lifespan` 中显式 close
- **[B-N2]** 并发测试缺失；用 `pytest-asyncio` + `asyncio.gather` 模拟 10 并发的 `apply-execution`
- **[B-N3]** Job 执行可观测性弱；引入结构化日志（输入大小、时长、重试次数、operator）

---

## 2. 反馈闭环后端（`feedback_store.py` / `feedback_jobs.py` / `feedback_schemas.py` / `agent_version_store.py`）

### 2.1 🟥 严重

**[F-S1] `feedback_store.py` 5022 行 / 229 方法——超级类**
- 证据：`feedback_store.py:66-5022` 单一 `FeedbackStore` 类混合：signal/event/correlation、case、evidence、jobs（attribution/proposal/batch-plan/execution）、optimization batch、external governance、eval、agent version、JSON 序列化
- 影响：变更扩散；测试需重型 fixture；新增 job 类型需在 `create_*_job` / `complete_*_job` / `_latest_reusable_job` / `_normalize_*_output` 复制粘贴
- 修复（推荐拆分）：
  ```
  feedback_store.py                 # Facade，保留对外接口
  feedback_signal_store.py          # signal / event / pending correlation
  feedback_case_store.py            # case + 元数据
  evidence_store.py                 # evidence package + 文件
  feedback_jobs/
    __init__.py                     # JobFactory，多态创建
    base.py                         # BaseJobService（状态机、IO、审计）
    attribution_job_service.py
    proposal_job_service.py
    batch_plan_job_service.py
    execution_job_service.py
  optimization_batch_store.py       # batch + plan + approval
  external_governance_store.py
  eval_store.py
  ```

**[F-S2] 字典 / Pydantic Schema 双轨制——字段持续漂移**
- 证据：
  - `feedback_store.py:1605, 1622` 调 `validate_attribution_output / validate_proposal_output` 返回 dict
  - `feedback_store.py:2588, 3281` 又用手写函数 `_normalize_proposal_output / _normalize_batch_plan_output` 重新拼装 dict
  - 写入 SQLite 时存 `payload_json`，读出后再用 dict 操作
- 影响：新增 schema 字段需要同步修改 schema 定义 + `_normalize_*` + 读取处共 3 处；typo 只在运行时暴露
- 修复：以 Pydantic 模型作为内部唯一表示，`store.create_xxx()` 接受/返回 Pydantic 模型，仅在持久化边界 `.model_dump()` / `.model_validate()`

**[F-S3] 事务边界跨 Session，存在悬挂中间态**
- 证据：`feedback_store.py:557-614` `create_optimization_batch` 中 `with self.Session.begin()` 内 commit 后再调 `update_feedback_source_annotation`（另一个 Session）；`resolve_pending`（`feedback_store.py:358-386`）分两次 Session
- 影响：第二步失败时 batch 已建但 source 标记未更新；恢复需要人工扫库
- 修复：把跨表更新放进同一 `Session.begin()`；批量 `db.add_all`；必要时引入 outbox pattern

**[F-S4] 状态机分散——无集中、无校验**
- 证据：
  - batch 状态推断散在 `feedback_store.py:630-644`
  - plan job 状态流程在 `feedback_store.py:788-815`
  - approval 流程在 `feedback_store.py:868-924`
  - execution 流程在 `feedback_store.py:1089-1113`
- 影响：非法转移（如 `approved` → `pending_approval`）无校验；测试盲点
- 修复：建立 `JobStateMachine` / `BatchStateMachine` / `PlanStateMachine`，集中 `VALID_TRANSITIONS` 表 + `validate_transition()`；改写为状态对象（State Pattern）或 enum + 转移表

### 2.2 🟧 重要

**[F-H1] Job 创建逻辑重复 200+ 行**
- 证据：`create_attribution_job`（`feedback_store.py:1419-1487`）/ `create_proposal_job`（`:1489-1554`）/ `create_batch_plan_job`（`:708-784`）三处复制粘贴：查 case / force / input_payload / `_write_job_input` / `_job_record` / `Session.begin() + add` / `_append_case_update`
- 影响：DRY 违反；同一 bug 需修三处
- 修复：抽 `JobFactory.create_job(job_type, feedback_case_id, **kwargs)`；按 job_type 分发到 `_build_*_input`，其余流程共用

**[F-H2] 异常无层次——全部 `ValueError`**
- 证据：`feedback_store.py:187, 664, 872, 956, 1071, 2023, 3936` 等，业务规则违反 / 状态机违反 / 配置缺失 / 数据缺失 全部 `ValueError`
- 影响：路由层无法区分 400 / 404 / 409 / 500
- 修复：分层异常 `FeedbackStoreError` → `BusinessRuleViolation` / `StateTransitionError` / `ConfigurationError` / `DataIntegrityError` / `NotFoundError`；FastAPI exception handler 统一映射

**[F-H3] Schema 校验与业务规范化混淆**
- 证据：`feedback_store.py:1605` 调 schema 校验，紧接着 `_normalize_*_output`（`:2588`）做业务字段映射
- 影响：修改 confidence 取值这种纯 schema 变更，要改 schemas 文件 + store 内部 `_normalize_*`
- 修复：`feedback_schemas.py` 仅做类型校验返回模型；`feedback_store._enrich_*` 仅做业务丰富

**[F-H4] 缺乏应用级并发控制**
- 证据：`FeedbackStore.__init__`（`feedback_store.py:69-88`）无锁；`create_evidence_package`（`:1268`）与 `create_attribution_job`（`:1419`）对同一 case 无原子保障
- 影响：多 worker / 后台任务竞争同一 case 产生重复 evidence / job
- 修复：DB 级 `SELECT ... FOR UPDATE`（PG）；或乐观锁 `case.version` 字段 + 失败重试；或应用层 `case_id` 锁

**[F-H5] `list_*` 客户端侧过滤——内存膨胀**
- 证据：`feedback_store.py:4835-4979` `_scrub_record / _filter_records` 全量取 payload_json 再过滤
- 影响：大规模数据时一次拉满 RAM；无法用 SQL `WHERE` 推下过滤
- 修复：把过滤条件 push 到 ORM `where()`；分页强制 cursor / 时间范围

### 2.3 🟨 一般

- **[F-M1]** `tests/test_feedback_store.py` 2335 行集中在 happy path；缺少非法状态转移、并发、schema 不匹配的负向测试
- **[F-M2]** Schema 版本字符串散落（`"attribution-output/v1"` 等）；建立 `SchemaVersion` 常量类与 v1→v2 迁移器
- **[F-M3]** `target_path` 映射硬编码于 `_target_path_for_type`（`feedback_store.py:3900+`）；改 `Enum + 映射表`，给新增 object_type 加 lint

### 2.4 🟩 建议

- **[F-N1]** 缺审计日志：approve/reject/apply 没有 `operator + before_status + after_status` 的结构化日志
- **[F-N2]** Pydantic 默认 `model_validate` 性能较低；高频路径用 `model_validate_json` 直接吃 JSON 字符串
- **[F-N3]** 5022 行单文件 + 2335 行单测试文件均超过 IDE 友好范围；拆分同时拆测试

---

## 3. 前端（`App.tsx` + `components/*` + `api/runtime.ts` + `types/*` + `styles.css`）

### 3.1 🟥 严重

**[FE-S1] `ExternalFeedbackWorkspace.tsx` 5124 行——巨型组件**
- 证据：`frontend/src/components/ExternalFeedbackWorkspace.tsx:202-5124` 同时承载 Signals / Batches / Cases / Evals / Proposals / Tasks / External Governance / Attribution / Execution 共 9 个工作台
- 影响：任一 Tab 修改都影响其他；难以测试；TypeScript 类型推导慢；hot-reload 时间长
- 修复方案：
  ```
  components/feedback-workspace/
    ExternalFeedbackWorkspace.tsx     # 仅 Tab 路由
    SignalsWorkspace.tsx
    BatchesWorkspace.tsx
    CasesWorkspace.tsx
    ProposalsWorkspace.tsx
    TasksWorkspace.tsx
    EvalWorkspace.tsx
    ExternalGovernanceWorkspace.tsx
    hooks/
      useFeedbackWorkbench.ts
      useCaseDetails.ts
      useActionState.ts
      useListSelection.ts
      useModalDraft.ts
    components/
      ListDetailPanel.tsx
      ModalBase.tsx
      StatusPill.tsx
      MetricGrid.tsx
  ```

**[FE-S2] 状态管理失控——18+ `useState` 集中于顶层**
- 证据：`ExternalFeedbackWorkspace.tsx:214-235` 同时持 `activeMenu / data / query / selectedSourceIds / selectedCaseId / selectedBatchId / caseDetailView / caseDetails / detailsLoading / runtimeStatus / actionId / toast / proposalRegenerateDraft / batchPlanGenerateDraft / executionApplyDraft / manualApplyDraft / …`
- 影响：派生 useMemo 50+，依赖数组失同步风险；切 Tab 时旧状态污染新 Tab
- 修复：拆解到子 workspace 各自的 `useReducer`；引入 `useActionState`（discriminated union）替代 `actionId` 字符串前缀技巧

**[FE-S3] 数据获取直写组件，无缓存/重试/超时**
- 证据：
  - `api/runtime.ts:86-116` `requestJson` 仅 `fetch + throw`，无 `AbortController`、无 timeout、无 retry
  - `ExternalFeedbackWorkspace.tsx:321-360` `useEffect` 内 `Promise.all([...5 个 API 调用])`，用 `cancelled` 旗标抗竞态
- 影响：网络抖动失败；切 Tab/重新选 case 时旧请求覆盖新请求结果
- 修复：引入 TanStack Query（缓存 + 取消 + 失效 + 重试），将业务请求收敛到 `hooks/useFeedbackQueries.ts`；`api/runtime.ts` 仅留 fetch 适配

### 3.2 🟧 重要

**[FE-H1] 类型与后端 schema 漂移风险——无运行时校验**
- 证据：`types/feedback.ts` 797 行手工维护，与 `app/runtime/feedback_schemas.py` 766 行无生成机制；`ExecutionPlanOperation` 等类型分散
- 影响：后端字段改了前端不报错，UI 静默崩 / 显示空白
- 修复：用 `datamodel-code-generator` 或 OpenAPI（FastAPI 自带）生成 TS 类型；高频入口加 Zod 校验

**[FE-H2] 模态框状态管理重复 4 处**
- 证据：`ExternalFeedbackWorkspace.tsx:1012-1121` 与 `CasesPanel` 内部 4 个 `[draft, setDraft]` + `actionId.startsWith()` 判忙
- 影响：维护成本翻倍；前缀碰撞隐患（`proposal-regenerate` vs 未来 `proposal-regenerate-v2`）
- 修复：`useModalDraft<T>(initial)` 通用 Hook；`type PendingAction = { type: "proposal-regenerate"; targetId: string } | ...`

**[FE-H3] List + Detail 布局重复实现**
- 证据：`SignalsPanel`（`:1128`） / `BatchesPanel`（`:1270`） / `CasesPanel`（`:2071`）三处列表+详情+搜索同构代码
- 修复：抽 `<ListDetailLayout items renderListItem renderDetail filterFn />`

**[FE-H4] 长列表无分页/虚拟化**
- 证据：`ExternalFeedbackWorkspace.tsx:239, 694` `getFeedbackWorkbenchData({ limit: 500 })`
- 影响：500 项渲染 DOM 卡顿
- 修复：游标分页 + `react-window`

### 3.3 🟨 一般

- **[FE-M1]** 派生状态过度 `useMemo`（顶层 50+），多数廉价计算；保留只用于真正昂贵的（如 `buildTaskByProposalId`）
- **[FE-M2]** Prop drilling 4 层（`BatchesPanel → BatchPlanDetails → BatchPlanTaskCard`）；建立 `BatchActionContext`
- **[FE-M3]** `styles.css` ~5K 行无模块化；按 workspace 拆 CSS Module 或 Tailwind utility 化
- **[FE-M4]** 无 `.test.tsx` 测试；E2E（Playwright）+ 关键 hook 单测（Vitest）

### 3.4 🟩 建议

- **[FE-N1]** 缺 ARIA：列表项虽有 `role="button"` 但缺 `aria-pressed/aria-expanded`；键盘导航
- **[FE-N2]** Type guard / discriminator 细化 `sourceRow.raw` 联合类型

---

## 4. 代码-文档一致性

### 4.1 🟥 严重

> 经主线核查：之前流传的"workspace 仍叫旧名"问题已修正（`README.md:9, 24-26, 327-334, 351-356, 479-481` 与 `docs/FEEDBACK_OPTIMIZATION_MULTI_AGENT_ARCHITECTURE.md:126-129, 373-376, 384-387` 均已使用 `attribution-analyzer-workspace / proposal-generator-workspace / execution-optimizer-workspace` 与对应 claude-roots）。本节仅保留实际仍存在的不一致。

**[D-S1] `README.md:418` `DEFAULT_ALLOWED_TOOLS` 示例与代码默认不一致**
- 文档：`README.md:418` `DEFAULT_ALLOWED_TOOLS=Read,Grep,Glob,mcp__sec-ops-data__*`
- 代码：`app/runtime/settings.py:74` 默认 `"Read,Grep,Glob,Skill"`；`docker/.env.example:145` 为 `"Read,Grep,Glob,Skill,mcp__sec-ops-data__*"`
- 影响：用户照搬 README 会丢 `Skill`，调用 Skill 失败
- 修复：将 README 示例改为与 `.env.example` 同步的 `Read,Grep,Glob,Skill,mcp__sec-ops-data__*`

### 4.2 🟧 重要

**[D-H1] `README.md:133-138` 接口清单缺优化任务级回归路由**
- 文档：未列 `POST/GET /api/optimization-tasks/{task_id}/regression-runs`、`POST /api/optimization-tasks/{task_id}/mark-applied`
- 代码：`app/main.py:1350, 1379, 1419` 均存在
- 修复：补齐 README 第 137-138 行"优化任务"分组

**[D-H2] `docs/FEEDBACK_OPTIMIZATION_PRODUCT_ADJUSTMENT_PLAN.md` 头部状态滞后**
- 文档：仍标为"开发调整方案"
- 代码事实：执行优化、批次方案、回归运行接口已落地（`main.py:709-1430`，`tests/test_api_execution_optimizer.py` 412 行用例覆盖）
- 修复：更新文档头部，明确"v1 已实现 / 已校准与代码一致" 或 划分"已实现 vs 计划中"小节

### 4.3 🟨 一般

- **[D-M1]** README 缺 "执行优化智能体" 调用约束（`max_turns=12`，详见 `agent_profiles.py:108`）以及与 attribution / proposal 的链路图描述
- **[D-M2]** README 未说明前端"评估"模块如何 sync feedback / 手动编辑 eval case，而后端 `POST /api/eval-datasets/feedback/sync`、`PATCH /api/eval-cases/{id}` 已就绪
- **[D-M3]** `docs/FEEDBACK_OPTIMIZATION_MULTI_AGENT_ARCHITECTURE.md` 1930 行，建议提取每条流程的状态机/字段表为独立小节，便于做 schema 同步

### 4.4 🟩 建议

- **[D-N1]** 在 `docs/` 增加自动化：CI 脚本扫描 README 接口清单 vs `app/main.py` 路由列表，差异即失败
- **[D-N2]** 用 FastAPI 的 OpenAPI 自动 dump 一份 `docs/openapi.json` 作为文档真相来源，README 只链过去

---

## 5. 推荐改造路线图

按"价值高 / 风险低"优先排序，可平滑迭代：

### 第 1 阶段（2 周，重构地基）
1. **拆 `feedback_store.py`** 为 8 个模块（[F-S1]）——不改 API，保留 `FeedbackStore` Facade 类
2. **拆 `app/main.py`** 为 `app/routers/*`（[B-S1]）；抽出 `services/{execution,job_application}.py`
3. **统一异常层次**（[F-H2]、[B-H2]）+ FastAPI exception handler
4. **修复 `README.md:418`** `DEFAULT_ALLOWED_TOOLS` 与 PRODUCT_ADJUSTMENT_PLAN 状态（[D-S1]、[D-H2]）；补齐缺失接口清单（[D-H1]）

### 第 2 阶段（2 周，状态机与一致性）
5. **集中状态机**（[F-S4]）：建 `state_machines.py`，所有 transitions 走 `validate_transition()`
6. **统一 dict→Pydantic**（[F-S2]）：内部传 model，边界再 dump
7. **事务收口**（[F-S3]）：跨表更新归入同 `Session.begin()`
8. **路径校验加固**（[B-H3]）+ 单测

### 第 3 阶段（2 周，前端组件化）
9. **拆 `ExternalFeedbackWorkspace.tsx`**（[FE-S1]）+ 抽 `useFeedbackWorkbench`、`useActionState`
10. **引入 TanStack Query**（[FE-S3]）替换 useEffect+fetch
11. **OpenAPI 生成 TS 类型**（[FE-H1]、[D-N2]）

### 第 4 阶段（持续）
12. Agent profile 配置化（[B-S3]）
13. SQLite → PG 或连接池隔离（[B-S4]、[F-H4]）
14. 负向 / 并发 / E2E 测试补齐（[F-M1]、[FE-M4]）
15. 结构化日志、审计、可观测性（[B-N3]、[F-N1]）

---

## 6. 重点结论

1. **核心债务在 4 个上帝模块**：`feedback_store.py` (5022) / `ExternalFeedbackWorkspace.tsx` (5124) / `main.py` (1659) / `claude_runtime.py` (1403)。任何后续功能都会被它们放大成本。**第一优先是拆分这四个文件**。
2. **代码-文档一致性整体良好**，仅遗留 1 处严重（`DEFAULT_ALLOWED_TOOLS` 示例）、2 处重要（接口清单遗漏、PRODUCT_ADJUSTMENT_PLAN 状态滞后）需要修订；之前怀疑的工作区改名问题已修正。
3. **鲁棒性短板集中在两个层面**：后端的 SQLite 并发 + 跨 Session 事务，前端的无重试无超时无缓存。这些都不是"风格问题"，是真实可见的故障源。
4. **扩展性瓶颈来自硬编码字符串**：Agent profile、job_type、object_type、状态字面量。引入 Enum / 注册表 / 配置文件后才能撑住下一波新需求。
5. **不建议一次性大重构**。按上面的 4 阶段路线图渐进，每阶段都能独立交付且降低后续工作量。

---

> 本报告所有结论已基于 `master` 分支当前工作树进行交叉核查，纠正了"工作区目录名未更新"等已修正项的失效结论。如对某条 finding 需进一步定位或实施，建议从对应的 `file:line` 切入。
