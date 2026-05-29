# 产品级代码与文档评审报告

> ⚠️ **本报告为第一轮（重构前）基线，已被取代。** 文中"4 个上帝模块（`feedback_store.py` 5022 行 / `ExternalFeedbackWorkspace.tsx` 5124 行 / `main.py` 1659 行 / `claude_runtime.py` 1403 行）"等结论描述的是**重构前**状态；这些模块已于提交 `2210f95` 系统性拆分（现分别为 298 / 405 / 133 / 791 行）。**当前现状请以第二轮报告 [`CODE_AND_DOCS_REVIEW_R2.md`](./CODE_AND_DOCS_REVIEW_R2.md) 为准。** 本文保留作为"评审 → 拆分"因果链的历史记录。

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

---

## 7. 整改进展

> 更新时间：2026-05-28。以下记录用于承接原始评审结论；原始评审内容保留为问题基线。

### 7.1 已完成

- **[B-S1] 路由拆分**：`app/main.py` 已拆为 `app/routers/*`，当前 `main.py` 仅保留应用装配、依赖、lifespan、CORS、鉴权与 router 注册。
- **[B-H3]/[B-H4] 执行应用服务与路径策略**：新增 `app/services/execution_application.py` 与 `app/runtime/execution_targets.py`，执行方案应用、workspace 目标校验、符号链接逃逸防护和回滚错误收集已从路由/Store 中抽离。
- **[F-S4] 状态机集中化**：新增 `app/runtime/state_machines.py`，job、execution job、batch、task 的关键状态转移已走集中校验，并补充 `tests/test_state_machines.py`。
- **[F-H1] Job 创建重复逻辑第一轮收敛**：新增 `app/runtime/feedback_job_factory.py`，`create_attribution_job/create_proposal_job/create_batch_plan_job` 的 queued job input 写入、记录生成、落库已统一。
- **[F-S1] FeedbackStore 低耦合模块拆分第一轮**：新增 `app/runtime/external_governance.py` 和 `app/runtime/execution_targets.py`，外部治理/Webhook 与 workspace 执行目标策略已从 `FeedbackStore` 抽离，`FeedbackStore` 继续作为兼容 facade。
- **[F-S1] FeedbackStore 低耦合模块拆分第二轮**：新增 `app/runtime/feedback_source_store.py`，run、feedback signal、SOC event、pending correlation、feedback source annotation 与从反馈源生成评估用例的存储方法已从 `FeedbackStore` 抽离，外部调用面保持兼容。
- **[F-S1] FeedbackStore 低耦合模块拆分第三轮**：新增 `app/runtime/feedback_case_store.py`，反馈处置单创建、查询、状态更新与 case 模型转换已从 `FeedbackStore` 抽离；run 查询方法也归并到 `feedback_source_store.py`，与 run 写入保持同域。
- **[F-S1] FeedbackStore 低耦合模块拆分第四轮**：新增 `app/runtime/feedback_evidence_store.py` 与 `feedback_privacy.py`，证据包生成、证据文件查询、证据物化和敏感字段常量已从 `FeedbackStore` 抽离，证据包 SQLite 表结构与 API 调用面保持不变。
- **[F-S1] FeedbackStore 低耦合模块拆分第五轮**：新增 `app/runtime/feedback_eval_store.py`，反馈回归评估用例同步/编辑、评估运行、运行项记录和 eval case 构建/转换已从 `FeedbackStore` 抽离，`FeedbackStore` 继续作为兼容 facade。
- **[F-S1] FeedbackStore 低耦合模块拆分第六轮**：新增 `app/runtime/feedback_external_governance_store.py`，外部治理/Webhook facade 方法与外部治理任务 upsert helper 已从 `FeedbackStore` 抽离，继续复用 `ExternalGovernanceService` 作为通知实现。
- **[F-S1] FeedbackStore 低耦合模块拆分第七轮**：反馈源规范化、source row 组装、source annotation 查询和 source case 标题等 helper 已归并到 `app/runtime/feedback_source_store.py`，反馈源子域实现进一步收口。
- **[F-S1] FeedbackStore 低耦合模块拆分第八轮**：新增 `app/runtime/feedback_batch_store.py`，优化批次创建、查询、归因记录、执行记录、回归记录和批次状态更新已从 `FeedbackStore` 抽离；批次方案生成与任务归一化逻辑仍留在 facade 中，作为下一轮拆分边界。
- **[F-S1] FeedbackStore 低耦合模块拆分第九轮**：新增 `app/runtime/feedback_job_store.py`，attribution/proposal job 创建、启动、完成、失败、查询、复用判断、错误记录、临时目录清理和当前归因丢弃已从 `FeedbackStore` 抽离；batch plan 与 execution 继续复用该 job 子域的通用 helper。
- **[F-S1] FeedbackStore 低耦合模块拆分第十轮**：新增 `app/runtime/feedback_proposal_store.py`，优化方案列表、详情、审批记录、proposal 模型转换和旧方案 supersede 逻辑已从 `FeedbackStore` 抽离，任务创建链路继续通过 facade 复用 proposal 查询。
- **[F-S1] FeedbackStore 低耦合模块拆分第十一轮**：新增 `app/runtime/feedback_task_store.py`，优化任务创建、查询、状态更新、执行 job 反挂和回归运行反挂已从 `FeedbackStore` 抽离，继续复用集中状态机校验。
- **[F-S1] FeedbackStore 低耦合模块拆分第十二轮**：新增 `app/runtime/feedback_execution_store.py`，execution-optimizer 执行 job 创建、完成、失败、查询、离线执行输出和执行方案安全校验已从 `FeedbackStore` 抽离。
- **[F-S1] FeedbackStore 低耦合模块拆分第十三轮**：新增 `app/runtime/feedback_batch_plan_store.py`，批次优化方案生成、proposal-generator job、方案完成/审批/拒绝、批次任务执行准备和外部通知入口已从 `FeedbackStore` 抽离。
- **[F-S1] FeedbackStore 低耦合模块拆分第十四轮**：新增 `app/runtime/feedback_plan_task_store.py`，批次方案任务归一化、任务摘要、外部系统上下文抽取、任务标题/描述/目标/验收标准清洗已从 `FeedbackStore` 抽离，`FeedbackStore` 已降至 800 行阈值以下。
- **[B-S3] Agent profile 字符串收敛第一轮**：`app/runtime/agent_profiles.py` 提供 profile 名和 profile version ID 常量，`ClaudeRuntime` 不再散落这些字面量。
- **[B-S2] ClaudeRuntime 拆分第一轮**：新增 `app/runtime/agent_job_runner.py`，feedback-loop Agent profile 的 options 构建、SDK query、schema JSON 提取与 DSPy 输出格式化协调已从 `ClaudeRuntime` 抽离；`ClaudeRuntime` 保留兼容 wrapper。
- **[B-S2] ClaudeRuntime 拆分第二轮**：新增 `app/runtime/runtime_activity.py`、`runtime_langfuse.py`、`feedback_job_orchestrator.py`、`feedback_eval_runner.py`，Agent 活动提取、Langfuse 适配、反馈 Agent job 编排和回归评估运行已从 `ClaudeRuntime` 抽离，`claude_runtime.py` 已降至 800 行阈值以下。
- **[FE-S1] 前端工作台组件化第一轮**：新增 `frontend/src/components/feedback-workspace/common.tsx`，通用状态胶囊、指标、详情 tab、JSON 预览和 Markdown/表格文本渲染组件已从 `ExternalFeedbackWorkspace.tsx` 抽离。
- **[FE-S1] 前端工作台组件化第二轮**：新增 `frontend/src/components/feedback-workspace/selectors.ts`，source/batch 构造、过滤、状态 tone、ID/日期格式化、diff/path helper 等纯函数已从 `ExternalFeedbackWorkspace.tsx` 抽离。
- **[FE-S1] 前端工作台组件化第三轮**：新增 `frontend/src/components/feedback-workspace/BatchesWorkspace.tsx`，优化批次列表、结果导航、批次反馈/归因/方案/回归详情已从主工作台抽离，主文件通过渲染回调复用现有归因结果和任务详情组件。
- **[FE-S1] 前端工作台组件化第四轮**：新增 `frontend/src/components/feedback-workspace/CasesWorkspace.tsx`，反馈处置单列表、当前处置单操作区、详情面板标题栏和归因复核提示已从主工作台抽离，详情内容通过渲染回调继续复用现有组件。
- **[FE-S1] 前端工作台组件化第五轮**：新增 `frontend/src/components/feedback-workspace/TasksDetails.tsx`，优化任务详情、执行方案、回归验证、版本差异对比等共享任务组件已从主工作台抽离，供批次与处置单详情复用。
- **[FE-S1] 前端工作台组件化第六轮**：新增 `frontend/src/components/feedback-workspace/EvalWorkspace.tsx`，回归评估工作区、评估用例详情、评估用例编辑/归档表单已从主工作台抽离。
- **[FE-S1] 前端工作台组件化第七轮**：新增 `frontend/src/components/feedback-workspace/ProposalWorkspace.tsx`，优化方案详情、建议卡片、未入库建议排查和外部治理通知卡片已从主工作台抽离。
- **[FE-S1] 前端工作台组件化第八轮**：新增 `frontend/src/components/feedback-workspace/EvidenceRunsDetails.tsx`，证据包文件浏览、完整性标识、Langfuse trace 链接和关联运行详情已从主工作台抽离。
- **[FE-S1] 前端工作台组件化第九轮**：新增 `frontend/src/components/feedback-workspace/AttributionDetails.tsx`，归因结果、归因复核卡片、原始输出和执行记录列表已从主工作台抽离，并继续供批次详情和优化方案详情复用。
- **[FE-S1] 前端工作台组件化第十轮**：新增 `frontend/src/components/feedback-workspace/SignalsWorkspace.tsx`，反馈信息列表、批次选择动作和原始数据详情面板已从主工作台抽离。
- **[FE-S1] 前端工作台组件化第十一轮**：新增 `frontend/src/components/feedback-workspace/FeedbackModals.tsx`，重新生成指令弹窗、执行方案应用确认和人工应用确认已从主工作台抽离，并删除主工作台中已不再使用的任务面板旧副本。
- **[FE-S1] 前端工作台组件化第十二轮**：新增 `frontend/src/components/feedback-workspace/BatchFeedbackDetails.tsx`，批次反馈信息列表与原始数据详情已从 `BatchesWorkspace.tsx` 抽离，`BatchesWorkspace.tsx` 已降至 800 行阈值以下。
- **[FE-S1] 前端工作台组件化第十三轮**：新增 `frontend/src/components/feedback-workspace/useFeedbackWorkspaceState.ts` 与 `useFeedbackWorkspaceActions.ts`，反馈工作台的数据加载、选择派生、详情加载和业务动作编排已从 `ExternalFeedbackWorkspace.tsx` 抽离，主工作台降至 800 行阈值以下。
- **[FE-S3] 请求层健壮性第一轮**：`frontend/src/api/runtime.ts` 已加入请求超时、GET 轻量重试和 `AbortSignal` 处理。
- **[FE-S3] 请求层组件化第二轮**：新增 `frontend/src/api/request.ts` 与 `frontend/src/api/feedback.ts`，公共请求封装和反馈优化域 API 已从 `runtime.ts` 抽离，`runtime.ts` 降至 800 行阈值以下并继续兼容原导入路径。
- **[D-S1]/[D-H1]/[D-H2] 文档一致性**：`README.md` 与反馈优化架构/产品调整文档已同步当前接口和默认工具配置。
- **[TEST-M1] FeedbackStore 测试拆分第一轮**：新增 `tests/feedback_store_test_utils.py`，并将原 `tests/test_feedback_store.py` 按 sources、batch plans、cases/jobs、execution、proposals、eval agents 拆为多个小测试文件，单文件均低于 800 行阈值。
- **[F-H2] 反馈优化异常层次第一轮**：新增 `app/runtime/errors.py`，反馈闭环域内的业务规则错误、配置错误和状态机错误已从裸 `ValueError` 迁移到 `FeedbackStoreError` 子类；现有路由仍可按 `ValueError` 兼容捕获，后续再接入统一 exception handler。
- **[B-S4]/[F-H4] SQLite 连接治理第一轮**：`app/runtime/runtime_db.py` 已按 DB 路径复用进程内 engine，显式设置 QueuePool、连接超时、WAL、`synchronous=NORMAL` 和 `busy_timeout=30000`；新增 `tests/test_runtime_db.py` 覆盖同路径 engine 复用和并发 feedback signal 写入。
- **[B-H2]/[F-H2] 统一 exception handler 第一轮**：新增 `app/routers/error_handlers.py` 并在 `app/main.py` 注册 `FeedbackStoreError` 处理器；反馈信号、反馈源、评估用例和外部治理 Webhook 的 400 类反馈域错误已改由统一 handler 返回 `error_code` 与 `detail`，前端 `readError()` 已兼容结构化错误响应。
- **[D-N2] OpenAPI 文档真相来源第一轮**：新增 `scripts/export_openapi.py` 与生成文件 `docs/openapi.json`，README 已说明接口详情以运行时 `/openapi.json` 或导出的 `docs/openapi.json` 为准；新增 `tests/test_openapi_export.py` 覆盖导出脚本。
- **[F-S3] SQLite 写事务收口第一轮**：`resolve_pending()` 已从跨 Session 先读后写改为单个 `Session.begin()` 内完成 `pending_id/event_id` 查找、状态更新和 payload 写回；新增重复解析测试覆盖 `event_id` 别名、重复 resolve 和缺失 pending。
- **[FE-H1] OpenAPI 生成 TS 类型第一轮**：前端新增 `openapi-typescript` devDependency 和 `generate:api-types` 脚本，已由 `docs/openapi.json` 生成 `frontend/src/types/api.ts`；治理脚本已识别自动生成文件头，避免把 OpenAPI 派生物计入手写文件超限；本轮先建立生成物和命令入口，不直接替换既有手写 `types/feedback.ts`。
- **[FE-H1] OpenAPI 生成 TS 类型第二轮**：`ConfigMappingItem` / `ConfigMappingResponse` 与 `FeedbackSignalCreateRequest` / `FeedbackSignalRecord` 已改为由 `frontend/src/types/api.ts` 派生；对后端默认值字段保留前端可省略语义，避免把 OpenAPI 默认值误变成 UI 必填。
- **[FE-H1] OpenAPI 生成 TS 类型第三轮**：`SocEventCreateRequest`、`SocEventCreateResponse`、`PendingCorrelationResolveRequest`、`FeedbackSourceRef`、`FeedbackSourceUpdateRequest` 和 `FeedbackEvalCaseGenerateRequest` 已改为由 OpenAPI schema 派生；当前 OpenAPI 仍返回 `dict` 的聚合记录保留前端细化类型。
- **[D-N2]/[FE-H1] OpenAPI schema 完整化第一轮**：`/health`、SOC event、pending correlation、feedback source、eval case/run 已补明确 response model，`tests/test_openapi_export.py` 已防止关键接口退回泛型 `dict`；前端 `RuntimeHealth`、catalog/session/config、反馈源、SOC、pending、eval case/run 类型继续由 OpenAPI 生成物派生。
- **[D-N2]/[FE-H1] OpenAPI schema 完整化第二轮**：反馈优化批次主流程接口已补明确 response model，覆盖批次列表/详情、归因任务、优化方案审批、任务执行和回归运行；`blocked_items` 已从泛型 `dict` 收口为 `FeedbackOptimizationBlockedItemResponse`，前端批次/方案/任务响应类型继续由 OpenAPI 生成物派生。
- **[D-N2]/[FE-H1] OpenAPI schema 完整化第三轮**：优化任务执行接口已补明确 response model，覆盖 execution job 生成/列表/应用与任务级回归运行；执行方案、执行操作和应用结果的前端类型已改为由 OpenAPI 生成物派生，避免执行器输出结构继续停留在手写 `dict` 类型。
- **[D-N2]/[FE-H1] OpenAPI schema 完整化第四轮**：Agent 版本管理接口已补明确 response model，覆盖当前版本、版本列表、快照创建、manifest、版本 diff、文件 diff 和回滚响应；前端版本管理类型已由 OpenAPI 生成物派生，并保留 manifest/diff 文件条目的扩展字段兼容。
- **[D-N2]/[FE-H1] OpenAPI schema 完整化第五轮**：优化方案接口已补明确 response model，覆盖 proposal 列表、详情、批准、拒绝和要求补充分析；proposal/review 前端类型已由 OpenAPI 生成物派生，并通过扩展字段兼容历史 proposal payload。
- **[D-N2]/[FE-H1] OpenAPI schema 完整化第六轮**：反馈分析输出接口已补明确 response model，覆盖归因 validated output 与建议 validated output；反馈 case、证据包、分析 job、归因输出和建议输出的前端类型继续由 OpenAPI 生成物派生，外部治理补充字段保留兼容。
- **[D-N2]/[FE-H1] OpenAPI schema 完整化第七轮**：外部治理任务公开 schema 已补齐任务名称、描述、目标、目标对象、上下文、验收标准和通知状态等字段，避免 FastAPI response model 过滤批次方案任务详情；前端运行记录、外部治理、优化任务和评估编辑请求类型继续迁移为 OpenAPI 派生。
- **[D-N2]/[FE-H1] OpenAPI schema 完整化第八轮**：反馈优化工作流响应模型已从 `schemas.py` 拆入独立 `feedback_workflow_response_schemas.py`，并补齐优化任务、执行 job、批次执行结果中的嵌套 `$ref`，使 `proposal/latest_execution_job/latest_regression_run/execution_job/apply_result` 不再停留在泛型 `dict` 契约。
- **[D-N2]/[FE-H1] OpenAPI schema 完整化第九轮**：证据包 manifest 响应已补齐 `source_refs`、`included_files`、`redaction`、`completeness` 的结构化 schema，前端 `EvidencePackageRecord` 直接使用 OpenAPI 派生类型，不再手写泛型覆盖。
- **[D-N2]/[FE-H1] OpenAPI schema 完整化第十轮**：回归评估响应已补齐 eval case 摘要、生成结果、检查结果和运行摘要的结构化 schema，`EvalRun.summary` 与 `EvalRunItem.check_results` 不再使用泛型 `dict` 契约。
- **[D-N2]/[FE-H1] OpenAPI schema 完整化第十一轮**：Agent 版本 manifest 响应已补齐 `included_roots`、`excluded_paths`、`skipped_paths`、`related_data` 的结构化 schema，版本快照策略、排除项和跳过项不再以泛型 `dict` 暴露给前端。
- **[D-N2]/[FE-H1] OpenAPI schema 完整化第十二轮**：优化执行结果中的 `applied_diff` 已改为复用 `AgentVersionDiffResponse`，前端优化任务和执行应用响应改用 `AgentVersionSummary` / `AgentVersionDiff` 派生类型，不再把版本对象与 diff 当作泛型 `Record`。
- **[D-N2]/[FE-H1] OpenAPI schema 完整化第十三轮**：`FeedbackAnalysisJobResponse` 已迁入独立响应模块，`validated_output_json` 明确为归因输出、优化方案输出或批次优化方案输出 union；批次优化方案响应模型抽出为无环依赖模块，前端分析 job 输出不再使用泛型 `Record`。
- **[D-N2]/[FE-H1] OpenAPI schema 完整化第十四轮**：批次优化方案与任务响应已补齐任务上下文、证据引用、归因摘要、验收标准、执行提示和风险/验证字段的结构化 schema，前端 `FeedbackOptimizationPlanRecord` 与 `FeedbackOptimizationPlanTaskRecord` 删除对应手写字段覆盖。
- **[D-N2]/[FE-H1] OpenAPI schema 完整化第十五轮**：反馈分析 job 错误、批次跳过源、批次归因统计和优化方案错误已补齐结构化 schema，前端 `FeedbackAnalysisJobRecord.error_json` 删除手写字段覆盖。
- **[D-N2]/[FE-H1] OpenAPI schema 完整化第十六轮**：执行 job、评估运行和评估运行项的 `error_json` 已复用 `FeedbackJobErrorResponse` 结构化 schema，前端 `EvalRunRecord` 删除运行字段手写覆盖。
- **[B-H2]/[F-H2] 统一 exception handler 第二轮**：新增路由错误 helper 与 `ConflictError`，反馈 case、反馈工作台、优化和回归评估路由中的 400/404/409 业务错误已进一步收口为 `FeedbackStoreError` 结构化响应。
- **[B-H2] 统一 exception handler 第三轮**：Agent 版本路由的 manifest、diff、file-diff 和 rollback 404/409 已复用统一路由错误 helper，避免继续返回裸 `HTTPException` 响应。
- **[B-H2]/[B-H4] 执行应用错误边界第一轮**：`ExecutionApplicationService` 的执行方案校验、路径校验和文件应用失败已从裸 `ValueError` 收口为 `ExecutionApplicationError`，并继续在失败时写入 execution job 错误记录。
- **[B-H2] Agent 版本完整性错误边界第一轮**：`AgentVersionStore.restore_version()` 的 bundle hash 校验和 tar archive path 校验已从裸 `ValueError` 收口为 `AgentVersionIntegrityError`，rollback 路由直接复用统一异常 handler。
- **[F-H2] Agent 输出解析错误边界第一轮**：`feedback_jobs.extract_json_object()` 与 `read_json()` 的空输出、无 JSON 对象和非对象 JSON payload 已从裸 `ValueError` 收口为 `AgentOutputParseError`；Pydantic validator 内部 `ValueError` 保留为 schema 校验机制。
- **[B-H2]/[F-H2] 反馈工作台路由异常边界第一轮**：批次已进入执行链路、方案目标不可执行等状态冲突已细化为 `ConflictError`；`feedback_workbench.py` 不再捕获 `ValueError` 后二次改写错误码，领域异常直接由统一 handler 返回结构化响应。
- **[F-S3] 优化批次创建事务收口第一轮**：`create_optimization_batch()` 已把反馈 case 创建、source annotation 标记、默认 eval case 创建和 batch 写入收口到同一个 SQLite 事务；新增失败回滚测试，避免 batch 写入失败时留下半成品 case/eval/annotation。
- **[F-S3] 反馈源评估用例生成事务收口第一轮**：`generate_eval_cases_for_sources()` 已复用统一 source→feedback case 准备 helper，并把缺失 case 创建、eval case 创建/更新收口到同一个事务；新增 eval 写入失败回滚测试，避免直接生成评估用例时留下孤立反馈 case。
- **[F-H4] Job 创建部分失败清理第一轮**：反馈归因/建议/批次方案 job 与 execution job 创建时，如果 input 文件写入、DB 落库或反挂 case/batch/task 失败，会清理临时 job 目录和孤立 job 记录；新增失败注入测试覆盖归因 job 与 execution job。
- **[F-S3] 证据包创建事务收口第一轮**：`create_evidence_package()` 已把证据包 manifest/file rows 写入和反馈 case 当前证据包反挂收口到同一个 SQLite 事务；新增失败注入测试，避免 case 反挂失败时留下不可见的孤儿证据包。
- **[F-S3] 归因/建议 Job 完成事务收口第一轮**：`complete_attribution_job()`、`complete_proposal_job()`、`revalidate_proposal_job()` 和归因/建议类 `fail_job()` 已把 job 输出/错误/状态、proposal rows、external guidance rows 与 feedback case 状态更新收口到同一个 SQLite 事务；新增失败注入测试，避免 job 已完成但 case 或 proposal rows 未同步的中间态。
- **[F-S3] 批次方案 Job 完成事务收口第一轮**：`complete_batch_plan_job()` 与批次方案类 `fail_job()` 已把 batch plan job 输出/错误/状态和 batch payload 状态更新收口到同一个 SQLite 事务；新增失败注入测试，避免方案 job 已完成或失败但批次仍停留旧状态的中间态。
- **[F-S3] 执行 Job 完成事务收口第一轮**：`complete_execution_job()` 与 `fail_execution_job()` 已把 execution job 输出/错误/状态、optimization task 状态和来源 batch/plan task 执行状态同步收口到同一个 SQLite 事务；新增失败注入测试，避免执行方案已 ready/failed 但任务或批次任务仍停留旧状态的中间态。
- **[F-S3] 执行应用标记事务收口第一轮**：`mark_execution_job_applied()` 与 `mark_task_applied()` 已把 execution job completed、optimization task applied 和来源 batch/plan task applied 状态同步收口到同一个 SQLite 事务；新增失败注入测试，避免应用已标记但任务或批次任务未同步的中间态。文件写入和 Agent 版本快照仍属于 DB + 文件系统一致性补偿的后续治理项。
- **[B-H4]/[F-S3] 执行应用补偿第一轮**：`ExecutionApplicationService.apply_ready_execution_job()` 已在 workspace 文件写入后、应用后版本快照或 DB 状态同步失败时，自动恢复到 `pre_execution` 快照并把 execution job 标记为 failed；新增 API 级失败注入测试覆盖 DB applied 标记失败和应用后版本快照失败，避免文件已修改但任务未进入 applied 状态的半应用结果。
- **[B-H4]/[F-S3] 执行应用补偿记录第一轮**：新增 `execution_compensations` SQLite 表与 `FeedbackCompensationStoreMixin`，执行应用在 post-write 状态同步失败后会记录补偿事项；自动恢复成功记录为 `resolved`，自动恢复失败记录为 `pending_manual_recovery`，为后续 UI 展示、后台重试或人工恢复提供可查询 outbox。
- **[FE-H1]/[B-H4] 执行应用补偿可见性第一轮**：`OptimizationExecutionJobResponse` 已挂载 `compensations`，OpenAPI/前端类型同步生成；优化任务详情的执行方案区域会展示应用补偿记录，区分“已自动恢复”和“待人工恢复”，避免补偿状态只藏在后端 error_json 或 SQLite 表中。
- **[B-H4]/[FE-H1] 执行应用人工恢复第一轮**：新增 `POST /api/execution-compensations/{compensation_id}/restore`，开发人员可对 `pending_manual_recovery` 补偿记录触发恢复到 `pre_execution` 版本；前端补偿记录卡片已提供“恢复到应用前版本”按钮，并在恢复后刷新工作台与 Agent 版本。
- **[B-S1] 反馈工作台路由拆分第二轮**：新增 `app/routers/feedback_batches.py`，`/api/feedback-optimization-batches*` 批次创建、归因、方案生成、任务执行和批次回归路由已从 `feedback_workbench.py` 拆出；`feedback_workbench.py` 回到反馈源/工作台入口职责，两个路由文件均低于 20 个路由阈值。
- **[F-H5] 列表查询下推第一轮**：`list_runs/list_signals/list_events/list_pending/list_eval_cases/list_eval_runs/list_proposals/list_tasks/list_optimization_batches` 已把精确列过滤和 `limit` 下推到 SQLAlchemy 查询，避免先全表读取再 `_filter_records()`；`list_cases(q=...)` 等 JSON 模糊搜索和跨源聚合仍保留在后续治理范围。
- **[F-H5] 列表查询下推第二轮**：`ExternalGovernanceService.list_items()` 已把 `feedback_case_id/proposal_job_id/status/limit` 下推到 SQLAlchemy 查询，并删除该模块局部 `_filter_records()`；新增测试确保外部治理任务在 materialize 前完成过滤。
- **[F-S2] dict/Pydantic 双轨治理第一轮**：新增 `ExecutionCompensationRecord` 作为执行应用补偿记录的内部真相来源，补偿创建、读取、状态更新均先经过 Pydantic 校验再落库或返回；新增非法 `restore_status` 负向测试，避免 `execution_compensations.payload_json` 写入脏状态。
- **[F-S2] dict/Pydantic 双轨治理第二轮**：新增 `ExternalGovernanceItemRecord` 与 `ExternalGovernanceNotificationRecord`，外部治理项创建/upsert、Webhook 通知回写和方案重生成废弃均先经过 Pydantic 校验；新增通知失败与非法 item 状态测试，避免外部治理任务状态继续以手写 dict 分散更新。
- **[B-H4]/[FE-H1] 执行应用补偿查询闭环第一轮**：新增 `GET /api/execution-compensations` 和 `GET /api/execution-compensations/{compensation_id}`，支持按状态、优化任务和执行 job 筛选补偿记录；现有 restore 入口继续作为人工恢复重试入口，并补充列表筛选、详情 404、重复恢复幂等和缺失应用前版本 409 测试。

### 7.2 当前仍待处理

- `app/runtime/feedback_store.py` 已降至 800 行阈值以下；后续应继续把 dict 内部表示迁移到 Pydantic 模型边界，并收口跨 Session/跨文件系统事务。
- `frontend/src/components/ExternalFeedbackWorkspace.tsx`、`BatchesWorkspace.tsx` 与 `frontend/src/api/runtime.ts` 已降至 800 行阈值以下；后续前端整改重点应转向统一 list-detail 抽象、ARIA/键盘可访问性和 TanStack Query 缓存策略。
- SQLite 写事务收口、剩余 409/404 错误映射收口、执行应用补偿记录后台重试闭环，以及把现有手写前端类型逐步迁移到 OpenAPI 生成类型仍属于后续阶段整改项。
