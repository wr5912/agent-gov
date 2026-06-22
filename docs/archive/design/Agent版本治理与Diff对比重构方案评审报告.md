# 《Agent 版本治理与 Diff 对比重构方案》评审报告

> 评审对象：[`docs/Agent版本治理与Diff对比重构方案.md`](../../Agent版本治理与Diff对比重构方案.md)
> 评审日期：2026-06-03
> 评审视角：GSD plan review、现状断言准确性、旧设计替换边界、数据/状态迁移、候选回归隔离、Git 服务运维、API/OpenAPI/前端契约、阶段排序与验证矩阵
> GSD review 状态：原生 `gsd-review <phase>` 需要 `.planning` phase/PLAN artifacts；当前仓库只有 `.planning/METHODOLOGY.md`，没有可解析 phase 目录。已探测 reviewer：`claude`、`codex` 可用；按独立性跳过本机 `codex`，调用 `claude -p` 时返回 429（GLM Coding Plan 套餐到期），本报告为本地 GSD 风格评审，未生成跨 AI `REVIEWS.md`。

## 1. 一句话结论

方案方向正确，且对当前 tar snapshot / manifest / file diff 的痛点判断基本准确；但目前还不能直接进入实施。阻断点集中在四类：候选回归如何真正跑在 candidate worktree、旧 API/旧版本目录删除与历史任务投影如何同一批次收口、Git push/tag/archive 与 DB 状态如何可恢复、引入 Gitea 后的离线部署和凭据边界如何落地。建议先修订方案和阶段边界，再进入代码阶段。

## 2. 总览

| 维度 | 评级 | 主要风险 |
| --- | --- | --- |
| 现状判断 | 可接受 | 当前实现确实是 `AgentVersionStore` 的 tar/manifest 模型，但历史引用面比方案写得更广。 |
| 目标架构 | 方向正确 | Git-backed change set / release 模型能解决候选隔离、审批和发布审计问题。 |
| 迁移与删除旧设计 | 高风险 | 方案允许删除旧契约，但没有给出同批迁移消费者、历史任务投影和失败恢复边界。 |
| 候选回归 | 高风险 | 文档要求 candidate worktree，但当前 runtime/eval runner 入口固定跑主 profile。 |
| Git 运维与一致性 | 高风险 | push/tag/archive/DB 多资源事务只有原则，没有具体状态机和 reconciler。 |
| API/前端 | 中风险 | 三栏工作台需要列表、下载、审计和权限接口，当前 API 草案不足以支撑。 |
| 阶段计划 | 高风险 | Phase 1 下线旧 snapshot store 太早，必须和迁移消费者、OpenAPI、前端类型同步。 |

确认问题分布：HIGH 6 条、MEDIUM 7 条。

## 3. HIGH 阻断项

### [GV-1] 候选回归缺少真实 runtime 隔离入口，按现状会跑到当前 `/main-workspace`

方案在 §7.3 要求候选回归绑定 `candidate_commit_sha` 和 candidate worktree，且“不切换全局 `/main-workspace`”（方案行 562-570）。但当前回归执行链路是 `FeedbackEvalRunner` 调用 `run_chat`，创建 eval run 时记录的是 `current_agent_version_id()`，每条 case 通过普通 `ChatRequest` 跑主 runtime（`app/services/feedback_eval_runner.py:27-64`、`app/services/feedback_eval_runner.py:94-113`）。`ClaudeRuntime` 构造 eval runner 时传入的是 `self.run` 和当前版本 provider（`app/runtime/claude_runtime.py:103-108`），主 profile 又固定使用 `settings.main_workspace_dir`（`app/runtime/agent_profiles.py:69-81`）。

影响：即使 change set 和 worktree 建好了，候选回归如果复用现有入口，也会验证当前线上配置，而不是候选 commit。发布门禁会产生假阳性，最坏情况下把未验证的 candidate 发布到 main。

建议：在方案 §7.3 和阶段 3 明确新增候选运行接口，例如 `run_feedback_eval(candidate_worktree_path, candidate_commit_sha, change_set_id)`，由 runtime 临时构建只读 main-agent profile 覆盖 `workspace_dir/mcp_config_path/project_settings_path/readable_paths`，并强制 metadata 写入 `source=regression_eval`、`change_set_id`、`candidate_commit_sha`。禁止通过全局 settings mutation 切换 profile。补测试：同一 eval case 分别跑 main workspace 和 candidate worktree，证明读到的配置不同。

### [GV-2] Phase 1 直接移除旧 `/api/agent-versions/main/*`，但消费者迁移没有被列为同一硬交付

方案 §8.1 要直接删除旧 agent-versions API（行 627-636），阶段 1 验收要求 OpenAPI 不再包含旧路径（行 850-870）。当前后端仍注册 `create_agent_versions_router`（`app/main.py:100-107`），前端 API helper 全量调用旧路径（`frontend/src/api/runtime.ts:61-100`），OpenAPI 导出测试也显式断言这些路径存在（`tests/test_openapi_export.py:55-61`），任务详情仍通过旧 file diff API 展示已生效差异（`frontend/src/components/feedback-workspace/TasksDetails.tsx:1-18`、`frontend/src/api/runtime.ts:98-100`）。

影响：如果先删旧 API 而新 change set/release API 和前端消费者还没同批迁移，前端版本页、任务详情 diff、OpenAPI 生成类型和多条 API 测试会同时断裂。反过来，如果旧 API 临时保留，又会形成方案明确反对的双轨。

建议：删除旧契约可以成立，但阶段边界必须改成“同一阶段原子替换所有消费者”。Phase 1 验收不应只写“OpenAPI 不再包含旧路径”，还要列出：删除 `app/routers/agent_versions.py` 注册、删除或重写 `frontend/src/api/runtime.ts` 的旧 helper、更新 `AgentVersionsWorkspace` 与 `TasksDetails` diff loader、更新 `tests/test_openapi_export.py` 和生成类型、全仓 `rg "/api/agent-versions/main"` 只允许出现在迁移说明或废弃测试中。

### [GV-3] 删除旧 `/data/agent-versions/main/` 的启动迁移会让历史任务引用失去可解释投影

方案 §6.3 和 §10.2 写明启动时发现旧 `current.json` 后不迁移旧 tar 版本，直接删除旧 bundles/manifests/versions/current（行 516-521、806-812），§14.3 又说旧任务引用旧 `agent-version-*` 时展示为废弃旧 id、不提供新 Git diff（行 986-996）。当前库中 `optimization_tasks`、`execution_applications`、`regression_plans`、`eval_runs` 都持有 `applied_agent_version_id`、`pre_execution_agent_version_id` 或 `agent_version_id`（`app/runtime/runtime_db.py:253-270`、`app/runtime/runtime_db.py:366-380`、`app/runtime/runtime_db.py:393-405`），回归计划 fingerprint 也包含 applied version（`app/runtime/stores/feedback_regression_asset_store.py:596-612`）。

影响：旧版本包删除后，历史任务详情、补偿记录、回归计划和 eval run 仍会展示旧 id，但如果没有明确的“legacy projection”响应，UI 很容易继续尝试 diff 或 rollback，最终变成 404/500。更严重的是，启动迁移一旦在 bootstrap 或 DB migration 失败前删除旧目录，用户连人工核对旧快照的机会都没有。

建议：方案坚持“不迁移旧 tar 到 Git 历史”没问题，但要补一个一次性 schema/data migration：把旧 id 投影为 `legacy_agent_snapshot_id` 或 `legacy_agent_version_ref`，详情接口只展示 deprecated 状态、旧 id、创建时间和“不可 diff/不可 rollback”的明确原因。旧目录删除必须在 Git bootstrap、DB migration、legacy projection 写入全部成功后执行；失败时不得删除。建议先重命名到受控 tombstone 目录并写 deletion audit，再由后续清理任务物理删除。

### [GV-4] Git push/tag/archive 与 DB 写入的多资源一致性没有可恢复状态机

方案 §7.4 的 publish 流程是 fetch、ff-only merge、push main、tag、push tag、archive、写 `agent_releases`、更新 task/batch（行 591-601），§14.2 只给出原则性缓解：分步记录补偿项、引入 `pending_manual_recovery`（行 971-984）。这不足以指导实现，因为 Git remote、工作副本、归档文件和 SQLite 都不是一个事务域。

影响：典型失败点包括：main 已 push 但 tag push 失败、tag 已 push 但 archive 写入失败、archive 已写但 DB 写入失败、DB 标记 published 但 `/main-workspace` fast-forward 失败。没有精确状态机和 reconciler 时，系统重启后无法判断 release 是否可补偿、可重试还是需要人工接管。

建议：在数据模型中把 publish 拆成可重试状态，例如 `publish_fetching`、`publish_merging`、`main_pushed`、`tag_created`、`tag_pushed`、`archive_created`、`db_recorded`、`workspace_updated`、`published`、`pending_manual_recovery`。每一步记录 remote sha、tag sha、archive sha256、错误和幂等键。启动时新增 reconciliation job：对比 DB、origin/main、tag、archive manifest 和 `/main-workspace`，生成可重试/需人工恢复的明确结果。rollback 同理。

### [GV-5] 引入 Gitea 作为新硬依赖，但离线部署、安全初始化和降级模式没有进入验收

方案选择默认引入 Gitea（§3.1、§10.1），启动迁移第 2 步要求 Git 服务可用（行 506-508），§14.5 承认 Git 服务会成为新运行依赖（行 1010-1023）。本仓库产品不变量要求离线模式必须可用且不得依赖远程服务；自托管 Gitea 符合方向，但方案没有说明最小安全初始化和服务不可用时系统行为。当前 Docker 配置也还完全是 `docker/volume` 本地 bind mount，没有 Git 服务、凭据和 healthcheck 入口（`docker/docker-compose.yml:68-110`、`docker/.env.example:36-52`）。

影响：如果 Gitea 初始化凭据、注册开关、服务账号 token、root URL、绑定地址、数据目录权限和健康检查不被纳入验收，发布治理会变成“能跑 Git 命令但运维不可控”。如果 Git 服务短暂不可用时直接阻断整个 API 启动，也会影响只读反馈、历史查看和非版本治理能力。

建议：把 Git 服务边界拆成硬验收：默认禁用公开注册、只创建后端服务账号、token 只从 secret/env 注入、宿主机默认绑定 `127.0.0.1`、healthcheck 验证仓库和 refs、UI 只展示 public URL 不暴露 token。定义降级模式：Git 服务不可用时允许健康检查、历史只读和非版本治理查询，禁止 create change set / publish / rollback，并在 `/api/agent-repositories/main/status` 返回 degraded 状态。

### [GV-6] Change set 状态枚举有了，但缺少允许转移、并发锁和唯一性约束

方案 §5.1 列出 `agent_change_sets` 状态，但没有状态转移表、唯一约束和并发策略。当前旧执行服务至少有进程内 `_apply_lock` 和 baseline 检查（`app/services/execution_application.py:64`、`app/services/execution_application.py:137-140`），新方案切成 branch/worktree 后，风险从“一个任务写主 workspace”变成“多个 change set 并发创建、审批、回归、发布、废弃和清理”。

影响：同一个 optimization task 可以重复创建多个 active change set；审批后候选 branch 还可能被改写；publish 与 abandon 或 regression 同时发生；两个 publish 同时 fast-forward main 会产生 race。仅靠 Git ff-only 会阻止一部分冲突，但业务状态仍会出现悬挂 worktree、重复 release 或 UI 显示不一致。

建议：补状态机表和 DB 约束：`optimization_task_id` 上最多一个非 terminal change set；`branch_name`、`candidate_commit_sha`、`worktree_path` 唯一；publish 使用 DB row lock 加 Git remote sha 条件；terminal 状态包括 `rejected/abandoned/published/rolled_back`；所有写操作要求当前状态前置条件。验收补并发测试：重复 create、重复 approve、并发 publish、publish 中 abandon 均可预测失败。

## 4. MEDIUM 问题

### [GV-7] API 草案不能完整支撑三栏治理工作台

方案 §9.1 需要 change set / release / rollback 列表、关联反馈/Trace/归因/建议/回归、Git 服务链接和 release archive/tag/commit（行 692-713）。但 §8 只有按 task 获取 change set、按 id 获取 change set、release list/detail 和 rollback（行 638-678）。缺少按 status/source_batch/release/tag 查询 change set 的列表接口、release archive 下载接口、Git web URL 字段的权限边界、审批事件列表、回归运行列表与最近 gate 聚合。

建议：补足 UI 所需 read model：`GET /api/agent-change-sets?status=&batch_id=&task_id=&limit=`、`GET /api/agent-change-sets/{id}/events`、`GET /api/agent-change-sets/{id}/regression-runs`、`GET /api/agent-releases/main/{release_id}/archive`，并在响应中明确哪些字段只用于展示，哪些字段可作为下一步写操作的前置条件。

### [GV-8] `operator` / `reason` 模型没有和当前认证现实对齐

方案多处要求 `operator`、`reason`、`comment`（行 550-558、680-688、583-589），但当前 API 只有一个 Bearer API key 校验（`app/main.py:93-97`），没有用户身份、角色或审计主体。让调用方直接提交 `operator` 字符串只能提供弱审计，不能防止“替别人审批/发布”。

建议：如果本轮不做多用户 RBAC，方案应明确 v1 是“API key 环境下的声明式 operator”，并要求后端记录 `operator`、request source、时间、API key alias 或部署身份。高权操作至少要校验 `reason` 非空、可追踪 `request_id`，为后续 RBAC 留字段。

### [GV-9] host volume 默认迁移会影响 Makefile、本地调试和权限脚本

方案把默认宿主机运行态根从 `docker/volume` 改为 `${HOME}/volume-agent-gov`（行 760-772、953-969）。当前 Makefile、README、`.env.example`、Compose、PyCharm 调试说明和权限修复脚本都围绕 `docker/volume`（`Makefile:10-15`、`docker/.env.example:36-52`、`README.md:514-521`）。方案只列出需要更新文档和配置，没有把本地调试兼容作为验收。

建议：新增迁移验收：`make setup` 不再创建旧主路径或会按新变量创建；`docker/.env.local` / PyCharm 调试说明使用同一个 `HOST_RUNTIME_VOLUME_ROOT`；权限修复脚本支持新根目录；用户可以显式设置 `HOST_RUNTIME_VOLUME_ROOT=./volume` 继续旧路径。还要验证 Docker Compose 对 `.env` 中 `${HOME}` 的展开行为，避免不同环境空展开。

### [GV-10] `.worktreeinclude` overlay 可能让候选回归读到“不在 commit/release 中”的本地文件

方案保留 `.worktreeinclude`，语义改为只 overlay 本地非 Git 文件到候选 worktree，不参与 commit（行 270-278、842-845）。这对本地 MCP 或私有配置有用，但也会制造一个重要差异：候选回归实际读取的文件集可能大于 candidate commit，发布 archive 里又没有这些文件。

建议：为 overlay 增加 manifest 和 diff 摘要：候选回归记录 overlay 文件路径、hash、来源和排除原因；发布前确认 release 不依赖 overlay-only 文件；禁止 overlay 进入 secrets/private path；UI 标记“候选回归包含本地 overlay”以免误判。

### [GV-11] Git 命令白名单含 `reset --hard`，但缺少调用场景和保护条件

方案 §6.2 允许 `git reset --hard`（行 468-489），同时要求不接收任意 Git 参数、路径受限（行 496-498）。`reset --hard` 是必要但危险的恢复操作，尤其在 `/main-workspace` 当前生效目录上执行时会直接覆盖运行态文件。

建议：把白名单拆成“允许命令 + 允许场景 + 目标 worktree”。例如 `reset --hard` 只允许在临时 worktree cleanup 或 release rollback 的受控步骤中执行，必须先确认无未归档用户变更、目标 commit 已解析且来自内部 remote，执行后写审计事件。不要只用命令名白名单表达安全边界。

### [GV-12] 验证矩阵缺少数据库迁移失败、服务降级和真实历史引用投影

方案 §13 已包含治理硬门、测试、OpenAPI、前端 build、browser smoke、Git 服务、bootstrap、候选执行、发布回滚和真实数据（行 936-949）。但对本轮最高风险的“旧数据 + 旧 API 删除 + Git bootstrap + 服务不可用”组合还不够。

建议：增加三类测试：一是从含旧 `agent-versions/main` 和旧任务引用的 runtime.sqlite3 启动，验证 legacy projection 不 500；二是 Git 服务不可用时版本治理写接口 503/degraded、其他只读接口可用；三是 publish/tag/archive/DB 任一步失败后，重启 reconciliation 能给出可恢复状态。

### [GV-13] 文档阶段缺少 GSD artifact 对齐，后续无法直接使用原生 `gsd-review`

本次评审发现当前 `.planning` 下只有 `METHODOLOGY.md`，没有 phase 目录、`PROJECT.md`、`ROADMAP.md`、`PLAN.md`。这不影响当前方案文档评审，但会影响后续使用原生 `$gsd-review`、`$gsd-plan-phase --reviews` 的闭环。

建议：如果该重构要进入长程 GSD 执行，先把本方案拆成正式 phase artifacts：更新 `.planning/PROJECT.md` / `ROADMAP.md`，为 5 个阶段生成 `*-PLAN.md`，再跑 `gsd-review`。否则就把本报告作为普通设计评审输入，不宣称已完成跨 AI phase review。

## 5. 建议阶段重排

建议把当前 5 阶段改成以下硬边界：

1. 阶段 0：方案修订与迁移契约。补 legacy projection、候选回归 runtime 注入、Git publish 状态机、Gitea 安全初始化、API read model。
2. 阶段 1：Git 服务和 Git-backed store 骨架，但不暴露 publish。完成 Gitea health、bootstrap、Git CLI wrapper、schema migration、legacy projection。
3. 阶段 2：Change set 候选执行。迁移 execution apply 到 candidate worktree，保证不写 `/main-workspace`。
4. 阶段 3：候选审批与候选回归。新增 candidate profile 运行入口，gate 只绑定 candidate commit。
5. 阶段 4：发布、归档、回滚和 reconciliation。先实现可恢复状态机，再开放发布按钮。
6. 阶段 5：前端治理工作台和旧 API 消费者原子删除。完成 OpenAPI、generated types、browser smoke 后再删除旧路由。

## 6. 必须补充到方案的验收清单

- `rg "/api/agent-versions/main" app frontend tests docs` 只允许出现在废弃说明或迁移测试中。
- 旧 `agent-version-*` 历史任务详情可打开，不提供 diff/rollback，且说明 deprecated 原因。
- 候选回归 metadata 同时包含 `source=regression_eval`、`change_set_id`、`candidate_commit_sha`，eval run 顶层 `agent_version_id` 等于 candidate commit。
- 候选回归读取 candidate worktree，不读取 `/main-workspace`。
- Git 服务不可用时，change set/publish/rollback 写接口阻断，历史只读接口不崩溃。
- publish 任意步骤失败后重启，reconciliation 给出确定状态。
- `make test`、治理 fail、OpenAPI export、前端类型生成、前端 build、browser smoke 全部通过。

## 7. 可以保留的方案决策

- 本轮只覆盖 `main-agent`，其他 profile 继续用 `profile_version_snapshot`，范围合理。
- 旧 tar snapshot 不导入 Git 历史，避免制造伪历史，方向合理。
- Git 服务只作为底层托管和辅助查看，审批/发布/回滚仍走产品 API，边界正确。
- 运行态目录迁出源码仓库，能降低误把 runtime workspace 当源码子目录的风险。
- 删除旧快照主流程符合本仓库“替换旧设计”的质量优先策略，但必须和迁移、消费者删除、历史投影同批闭环。
