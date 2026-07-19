# 《Agent 版本治理与 Diff 对比重构方案》评审报告 v2

> 评审对象：[`docs/archive/design/Agent版本治理与Diff对比重构方案.md`](../archive/design/Agent版本治理与Diff对比重构方案.md)
> 评审日期：2026-06-03
> 评审方式：使用外部 `claude` reviewer 重新评审 v1 报告与当前源码；Codex 复核新增 finding 的关键证据后整理成本报告。
> 对照版本：[`Agent版本治理与Diff对比重构方案评审报告.md`](../archive/design/Agent版本治理与Diff对比重构方案评审报告.md)

## 1. 一句话结论

v1 的 6 条 HIGH 和 7 条 MEDIUM 仍成立，Claude reviewer 未发现需要推翻的 v1 结论。v2 新增 2 条 HIGH、4 条 MEDIUM：最关键的是当前执行应用链路的写入目标仍硬编码到 `settings.main_workspace_dir`，以及 `AgentVersionStore` 已被多个消费者以具体方法和 bound method 方式耦合，不能只在方案里写“替换主实现”。因此当前方案仍不应直接进入实现阶段，必须先补阶段 0 的迁移契约和接口替换矩阵。

## 2. 总览

| 维度 | 评级 | v2 变化 |
| --- | --- | --- |
| 现状判断 | 可接受 | v1 判断仍成立，当前确实是 tar/manifest snapshot 主流程。 |
| 旧设计替换 | 高风险 | 新增 `AgentVersionStore` consumer 断裂面，需 facade 或 provider 协议。 |
| 候选执行/回归隔离 | 高风险 | 新增 `safe_workspace_target` 写入路径硬编码问题，强化 v1 的候选隔离风险。 |
| Git 发布一致性 | 高风险 | v1 保留，仍缺 publish/tag/archive/DB reconciliation 状态机。 |
| API/前端迁移 | 中高风险 | v1 保留，旧 API 消费者必须和删除旧契约同批迁移。 |
| Docker/运行态路径 | 中风险 | 新增 Compose fallback 仍指向 `./volume` 的路径一致性风险。 |

确认问题分布：HIGH 8 条，MEDIUM 11 条。

## 3. HIGH 阻断项

### [GV-1] 候选回归缺少真实 runtime 隔离入口，按现状会跑到当前 `/main-workspace`

证据：方案 §7.3 要求候选回归绑定 `candidate_commit_sha` 和 candidate worktree，且不切换全局 `/main-workspace`。当前 `FeedbackEvalRunner` 仍通过 `self.run_chat(...)` 执行普通 `ChatRequest`（`app/services/feedback_eval_runner.py:27`、`app/services/feedback_eval_runner.py:61`），创建 eval run 时记录的是 `current_agent_version_id()`（`app/services/feedback_eval_runner.py:106`）。`ClaudeRuntime` 构造 eval runner 时传入 `self.run` 和当前版本 provider（`app/runtime/claude_runtime.py:103`），主 profile 固定使用 `settings.main_workspace_dir`（`app/runtime/agent_profiles.py:73`、`app/runtime/agent_profiles.py:77`）。

影响：待发布版本回归会验证当前线上配置，而不是 candidate worktree。发布条件可能错误接受未被验证的 candidate。

建议：方案 §7.3 和阶段 3 必须新增候选运行入口，例如 `run_feedback_eval(candidate_worktree_path, candidate_commit_sha, change_set_id)`；临时 main profile 的 `workspace_dir`、`mcp_config_path`、`project_settings_path`、`readable_paths` 都要指向 candidate worktree。

### [GV-2] Phase 1 直接移除旧 `/api/agent-versions/main/*`，但消费者迁移没有被列为同一硬交付

证据：方案 §8.1 要删除旧 agent-versions API，阶段 1 验收要求 OpenAPI 不再包含旧路径。当前后端仍注册 `create_agent_versions_router`（`app/main.py:106`），前端 API helper 全量调用旧路径（`frontend/src/api/runtime.ts:61`、`frontend/src/api/runtime.ts:65`、`frontend/src/api/runtime.ts:73`、`frontend/src/api/runtime.ts:81`、`frontend/src/api/runtime.ts:93`、`frontend/src/api/runtime.ts:98`），OpenAPI 测试也显式断言旧路径存在（`tests/test_openapi_export.py:55`）。

影响：先删旧 API 会同时打断版本页、任务详情 diff、OpenAPI 导出、生成类型和相关测试；临时保留旧 API 又会形成方案反对的双轨。

建议：删除旧契约必须和消费者迁移原子化：替换 `AgentVersionsWorkspace`、`TasksDetails` diff loader、`frontend/src/api/runtime.ts` helper、OpenAPI 测试和生成类型后，再删除旧 router。

### [GV-3] 删除旧 `/data/agent-versions/main/` 的启动迁移会让历史任务引用失去可解释投影

证据：方案 §6.3 和 §10.2 允许直接删除旧 bundles/manifests/versions/current；但当前 `optimization_tasks`、`execution_applications`、`eval_runs`、`regression_plans` 仍持有旧版本 id（`app/runtime/runtime_db.py:253`、`app/runtime/runtime_db.py:259`、`app/runtime/runtime_db.py:373`、`app/runtime/runtime_db.py:399`），回归计划 fingerprint 也把 applied version 纳入稳定输入（`app/runtime/stores/feedback_regression_asset_store.py:596`）。

影响：旧版本包删除后，历史任务详情、补偿记录、回归计划和 eval run 仍可能引用旧 id。没有 legacy projection 时，UI 容易继续尝试 diff/rollback 并出现 404/500。

建议：不导入旧 tar 到 Git 历史可以保留，但必须新增 legacy projection：旧 id 只展示 deprecated 状态、创建时间、不可 diff/不可 rollback 原因。物理删除旧目录必须在 Git bootstrap、DB migration、legacy projection 全部成功后执行。

### [GV-4] Git push/tag/archive 与 DB 写入的多资源一致性没有可恢复状态机

证据：方案 §7.4 把 publish 写成 fetch、merge、push、tag、archive、写 DB、更新 task/batch 的线性流程；§14.2 只给出 pending manual recovery 的原则。Git remote、工作副本、归档文件和 SQLite 不是同一事务域。

影响：main 已 push 但 tag 失败、tag 已 push 但 archive 失败、archive 已写但 DB 失败、DB 标记 published 但 `/main-workspace` 更新失败等状态无法自动判断和恢复。

建议：把 publish 拆成可重试状态：`publish_fetching`、`main_pushed`、`tag_created`、`tag_pushed`、`archive_created`、`db_recorded`、`workspace_updated`、`published`、`pending_manual_recovery`。启动 reconciliation 对比 DB、origin/main、tag、archive manifest 和 `/main-workspace`。

### [GV-5] 引入 Gitea 作为新硬依赖，但离线部署、安全初始化和降级模式没有进入验收

证据：方案默认引入 Gitea，并要求启动时确保 Git 服务可用。当前 Compose 仍围绕本地 `./volume` bind mount，没有 Git 服务、凭据和 healthcheck（`docker/docker-compose.yml:68`、`docker/docker-compose.yml:98`、`docker/.env.example:36`）。

影响：若 Gitea 初始化凭据、注册开关、服务账号 token、root URL、绑定地址和健康检查未纳入验收，发布治理会变成“能跑 Git 命令但不可运维”。Git 服务不可用时如果阻断整个 API，也会影响反馈历史只读能力。

建议：默认禁用公开注册，只创建后端服务账号；token 从 secret/env 注入；宿主机默认绑定 `127.0.0.1`；Git 服务不可用时，版本治理写接口 degraded/503，非版本治理只读接口继续可用。

### [GV-6] Change set 状态枚举有了，但缺少允许转移、并发锁和唯一性约束

证据：方案列出 `agent_change_sets` 状态，但没有状态转移表、唯一约束和并发策略。旧执行服务至少有进程内 `_apply_lock`（`app/services/execution_application.py:64`）和 baseline 检查（`app/services/execution_application.py:137`）。

影响：同一个 optimization task 可以重复创建 active change set；publish 与 abandon/regression 可能并发；两个 publish 同时 fast-forward main 会产生 race。

建议：补状态机表和 DB 约束：`optimization_task_id` 最多一个非 terminal change set，`branch_name`、`candidate_commit_sha`、`worktree_path` 唯一；publish 使用 DB row lock 加 Git remote sha 条件。

### [GV-14] 新增：`safe_workspace_target` 硬编码写 `main_workspace_dir`，切到 worktree 后写入路径不会自动重定向

证据：`ExecutionApplicationService.safe_workspace_target()` 直接以 `self.settings.main_workspace_dir.resolve(strict=True)` 作为 base（`app/services/execution_application.py:366`、`app/services/execution_application.py:373`）。`apply_execution_operations()` 对每个 operation 调用 `safe_workspace_target(target_path)`（`app/services/execution_application.py:291`、`app/services/execution_application.py:301`），因此当前执行应用只能写主 workspace。方案 §7.1 要求 execution operations 写入候选 worktree，且不修改 `/main-workspace`。

影响：如果只把 version store 换成 Git store，而不把执行应用的目标根从 `settings.main_workspace_dir` 改为 `change_set.worktree_path`，候选执行仍会写线上主目录，候选隔离失效。

建议：`safe_workspace_target()` 和 `apply_execution_operations()` 必须接收显式 `workspace_dir` 或 `ExecutionTargetContext`。候选执行传 `change_set.worktree_path`，主流程不再默认写 `settings.main_workspace_dir`。验收增加 inode/mtime 快照：候选执行期间 `/main-workspace` 不发生文件变更。

### [GV-15] 新增：`AgentVersionStore` 已被多消费者以具体方法耦合，替换为 Git store 时接口断裂面大

证据：`app/main.py` 构造 `AgentVersionStore` 并把 `current_version_id` bound method 传给 `FeedbackStore`（`app/main.py:34`、`app/main.py:42`），同时把实例传给 `ClaudeRuntime` 和 `ExecutionApplicationService`（`app/main.py:46`、`app/main.py:48`）。lifespan 调用 `ensure_bootstrap()`（`app/main.py:56`）。旧 router 调用 `ensure_bootstrap/list_versions/create_snapshot/restore_version/diff_versions/diff_version_file/get_manifest`（`app/routers/agent_versions.py:35`、`app/routers/agent_versions.py:44`、`app/routers/agent_versions.py:52`、`app/routers/agent_versions.py:64`、`app/routers/agent_versions.py:73`、`app/routers/agent_versions.py:82`、`app/routers/agent_versions.py:91`）。执行应用调用 `create_snapshot/current_version_id/diff_versions/restore_version` 多处（`app/services/execution_application.py:107`、`app/services/execution_application.py:138`、`app/services/execution_application.py:142`、`app/services/execution_application.py:159`、`app/services/execution_application.py:165`、`app/services/execution_application.py:209`、`app/services/execution_application.py:272`）。worker 也单独构造 store 并传入 provider（`app/worker/agent_jobs.py:29`、`app/worker/agent_jobs.py:38`）。

影响：方案只写“`AgentVersionStore.current_version_id()` 调用迁移为 Git 当前 commit/tag provider”不够。删除旧 store 会同时影响 runtime、worker、router、execution service、core health 和测试。尤其 `FeedbackStore` 接收的是 callable provider，若新 provider 签名或空值语义变化，多个 mixin 会静默写入 `None`。

建议：阶段 0 增加 consumer 矩阵，定义 `AgentVersionProvider` 协议：`current_version_id()` 返回完整 commit sha 还是 release tag，空仓库如何处理，是否允许抛错。阶段 1 用过渡 facade 承接旧调用，所有消费者迁移后再删除旧 router 和旧 store。

## 4. MEDIUM 问题

### [GV-7] API 草案不能完整支撑三栏治理工作台

证据：方案 §9.1 需要 change set / release / rollback 列表、关联反馈/Trace/归因/建议/回归、Git 服务链接和 release archive/tag/commit；§8 只有按 task 获取 change set、按 id 获取 change set、release list/detail 和 rollback。

建议：补 `GET /api/agent-change-sets?status=&batch_id=&task_id=&limit=`、`GET /api/agent-change-sets/{id}/events`、`GET /api/agent-change-sets/{id}/regression-runs`、`GET /api/agent-releases/main/{release_id}/archive`。

### [GV-8] `operator` / `reason` 模型没有和当前认证现实对齐

证据：方案要求治理写操作包含 `operator`、`reason`、`comment`；当前 API 只有 Bearer API key 校验（`app/main.py:93`），没有用户身份和角色。

建议：v1 可声明为 API key 环境下的声明式 operator，但必须记录 request source、时间、API key alias 或部署身份，并校验 reason 非空。

### [GV-9] host volume 默认迁移会影响 Makefile、本地调试和权限脚本

证据：方案把默认根改为 `${HOME}/volume-agent-gov`；当前 Makefile、README、`.env.example`、Compose 和权限修复脚本仍围绕 `docker/volume`（`Makefile:10`、`docker/.env.example:36`）。

建议：`make setup`、`.env.local`、PyCharm 调试说明和权限修复脚本必须一起迁移或兼容；允许显式 `HOST_RUNTIME_VOLUME_ROOT=./volume` 保持旧路径。

### [GV-10] `.worktreeinclude` overlay 可能让候选回归读到不在 commit/release 中的本地文件

证据：方案保留 `.worktreeinclude`，语义改为 overlay 到候选 worktree 但不参与 commit。

建议：候选回归记录 overlay manifest：路径、hash、来源、排除原因；发布前确认 release 不依赖 overlay-only 文件。

### [GV-11] Git 命令白名单含 `reset --hard`，但缺少调用场景和保护条件

证据：方案 §6.2 允许 `git reset --hard`，但只给出命令白名单和路径约束，没有场景约束。

建议：白名单拆成“命令 + 场景 + 目标 worktree”。`reset --hard` 仅允许临时 worktree cleanup 或受控 rollback，执行前后写审计事件。

### [GV-12] 验证矩阵缺少数据库迁移失败、服务降级和真实历史引用投影

证据：方案 §13 有治理、测试、OpenAPI、前端 build、Git 服务和真实数据，但没有覆盖旧数据 + 旧 API 删除 + Git bootstrap + 服务不可用组合。

建议：增加三类测试：旧 `agent-versions/main` 和旧任务引用启动不 500；Git 服务不可用时写接口 degraded、只读接口可用；publish 任一步失败后 reconciliation 可恢复。

### [GV-13] 文档阶段缺少 GSD artifact 对齐，后续无法直接使用原生 `gsd-review`

证据：当前 `.planning` 下只有 `METHODOLOGY.md`，没有 phase 目录、`PROJECT.md`、`ROADMAP.md`、`PLAN.md`。

建议：若进入长程 GSD 执行，先把本方案拆成正式 phase artifacts，再跑原生 `gsd-review` 和 `gsd-plan-phase --reviews`。

### [GV-16] 新增：旧 `execution-optimizer` 读写目标有歧义，方案未说明其 workspace 如何与 candidate worktree 协调

> 后续边界：Governor 合并后，`execution-optimizer` 不再是当前运行态 profile/workspace 名；本 finding 中的旧名仅指合并前执行优化职责。当前应理解为 `governor` 执行类 job 的 candidate worktree 读写边界。

证据：方案 §3.2 暂不把 `execution-optimizer` 纳入 Git 发布治理；当前 execution optimizer profile 的 `readable_paths` 包含 `settings.data_dir` 和 `settings.main_workspace_dir`（`app/runtime/agent_profiles.py:125`、`app/runtime/agent_profiles.py:133`）。但方案要求 execution operations 写入 candidate worktree。

影响：执行优化器可能“读线上主 workspace，写候选 worktree”。一旦线上 main 与 candidate base 不一致，生成的操作基于错误上下文。

建议：候选执行期间动态构建 governor 执行上下文，`readable_paths` 和 target file context 都来自 `change_set.worktree_path`，不读 `/main-workspace`。

### [GV-17] 新增：worktree profile 的 `.mcp.json` 和 `.claude/settings.json` 路径派生规则缺失

证据：主 profile 的 `mcp_config_path` 和 `project_settings_path` 都从 `settings.main_workspace_dir` 派生（`app/runtime/agent_profiles.py:77`、`app/runtime/agent_profiles.py:78`）。候选回归如果仅改 workspace 但不改这些路径，会继续读取线上配置。

影响：candidate worktree 中的 MCP 或 settings 变更不会被候选回归验证。

建议：新增 candidate profile builder，统一派生 `workspace_dir`、`mcp_config_path`、`project_settings_path`、`readable_paths`、`claude_root`，并用测试证明候选 `.mcp.json` 被读取。

### [GV-18] 新增：`FeedbackStore.agent_version_provider` 是 callable，替换 Git provider 后空值会被静默吞掉

证据：`FeedbackStore` 接收 `agent_version_provider`（`app/runtime/stores/feedback_store.py:72`、`app/runtime/stores/feedback_store.py:86`），`_current_agent_version_id()` 捕获所有异常并返回 `None`（`app/runtime/stores/feedback_store.py:99`、`app/runtime/stores/feedback_store.py:102`、`app/runtime/stores/feedback_store.py:104`）。

影响：新 Git provider 如果抛错或返回空值，反馈、执行、回归等记录会静默写入 `agent_version_id=None`，破坏可追踪性。

建议：新 provider 的异常不能被静默吞掉。至少在版本治理相关写路径中，provider 失败应阻断并记录错误；测试断言 `eval_runs.agent_version_id` 等于 `git rev-parse HEAD`。

### [GV-19] 新增：Docker Compose volume fallback 仍是 `./volume`，与方案的 `HOST_RUNTIME_VOLUME_ROOT` 单根模型容易产生双路径

证据：方案新增 `HOST_RUNTIME_VOLUME_ROOT=${HOME}/volume-agent-gov`，但当前 Compose fallback 仍是 `${HOST_WORKSPACE_MOUNT:-./volume/main-workspace}`、`${HOST_DATA_MOUNT:-./volume/data}` 等（`docker/docker-compose.yml:68`、`docker/docker-compose.yml:98`）。

影响：如果只设置 `HOST_RUNTIME_VOLUME_ROOT` 而忘记同步派生变量，Compose 仍会挂载旧 `./volume`，形成新旧运行态并存。

建议：Compose fallback 改为 `${HOST_RUNTIME_VOLUME_ROOT:-./volume}/main-workspace` 这类单根派生形式。验收覆盖未设置时保持当前行为、设置后所有挂载统一到新根。

## 5. v1 对照

| Finding | v2 状态 | 说明 |
| --- | --- | --- |
| GV-1 到 GV-13 | 保留 | Claude reviewer 复核后未推翻 v1 证据。 |
| GV-14 | 新增 HIGH | 强化候选执行隔离风险。 |
| GV-15 | 新增 HIGH | 强化旧 store 替换和消费者迁移风险。 |
| GV-16 到 GV-19 | 新增 MEDIUM | 补足 execution optimizer、candidate profile、provider callable、Compose fallback 风险。 |

## 6. 建议阶段重排

1. 阶段 0：方案修订与迁移契约。补 legacy projection、candidate runtime/profile builder、Git publish 状态机、Gitea 安全初始化、API read model、`safe_workspace_target` worktree 注入接口、`AgentVersionStore` consumer 矩阵。
2. 阶段 1：Git 服务和 Git-backed provider 骨架，但不开放 publish。完成 Gitea health、bootstrap、Git CLI wrapper、schema migration、legacy projection、provider 协议替换验证。
3. 阶段 2：Change set 候选执行。迁移 execution apply 到 candidate worktree，保证不写 `/main-workspace`，并动态化 execution optimizer profile。
4. 阶段 3：候选审批与候选回归。新增 candidate profile 运行入口，gate 只绑定 candidate commit。
5. 阶段 4：发布、归档、回滚和 reconciliation。先实现可恢复状态机，再开放发布按钮。
6. 阶段 5：前端治理工作台和旧 API 消费者原子删除。OpenAPI、generated types、browser smoke 通过后再删除旧路由。

## 7. v2 必补验收清单

- `rg "/api/agent-versions/main" app frontend tests docs` 只允许出现在废弃说明或迁移测试中。
- 旧 `agent-version-*` 历史任务详情可打开，不提供 diff/rollback，且说明 deprecated 原因。
- 候选执行写入目标根等于 `change_set.worktree_path`，不是 `/main-workspace`。
- 候选回归 metadata 包含 `source=regression_eval`、`change_set_id`、`candidate_commit_sha`，eval run 顶层 `agent_version_id` 等于 candidate commit。
- 候选回归读取 candidate `.mcp.json` 和 `.claude/settings.json`，不读取主 workspace 配置。
- `FeedbackStore._current_agent_version_id()` 在新 Git provider 下返回合法 commit sha，provider 失败不会静默污染记录。
- Git 服务不可用时，change set/publish/rollback 写接口阻断，历史只读接口不崩溃。
- publish 任意步骤失败后重启，reconciliation 给出确定状态。
- `HOST_RUNTIME_VOLUME_ROOT` 未设置时 Compose 行为与当前一致；设置后所有挂载和数据路径统一指向新根。
- `.venv/bin/python scripts/check_codex_governance.py --mode fail`、`make test`、OpenAPI export、前端类型生成、前端 build、browser smoke 全部通过。

## 8. 可以保留的方案决策

- 本轮只覆盖 `main-agent`，其他 profile 继续用 `profile_version_snapshot`，范围合理。
- 旧 tar snapshot 不导入 Git 历史，避免制造伪历史，方向合理。
- Git 服务只作为底层托管和辅助查看，审批/发布/回滚仍走产品 API，边界正确。
- 运行态目录迁出源码仓库，能降低误把 runtime workspace 当源码子目录的风险。
- 删除旧快照主流程符合本仓库“替换旧设计”的质量优先策略，但必须和迁移、消费者删除、历史投影、worktree 路径注入、`AgentVersionStore` 接口迁移同批闭环。
