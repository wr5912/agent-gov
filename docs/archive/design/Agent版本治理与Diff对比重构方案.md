# Agent 版本治理与 Diff 对比重构方案

> 归档于 2026-07-10。阶段 1-5 已完成，正文后半的 main-only、旧优化批次和未来式迁移步骤仅作历史设计记录。当前版本治理事实以运行时 OpenAPI、`docs/反馈闭环当前实现基线.md` 与集成指南为准。

> ⚠️ 部分实现细节已被「main 归一为预制业务 Agent」整改（v2.8.0，分支 `unify-business-agent-model`）取代，阅读下方旧描述时按此更正：
> - main 配置已从 `/main-workspace` 迁到 `data/business-agents/main-agent/workspace`；`HOST_WORKSPACE_MOUNT`/`HOST_CLAUDE_ROOT_MOUNT` 主挂载已删（main 落 `/data` 下）。
> - 版本治理改为**按 `agent_id` 的 per-agent 版本库**（repo 就地在各业务 Agent 的 `workspace/`，`claude-root/`、`version/` 去嵌套并列）；`repository_status`/`snapshot`/`current`/`diff` 及批次执行均按 agent_id 路由，不再恒走主库。
> - 用户面契约以 [AgentGov集成指南](../../AgentGov集成指南.md) 为准。
>
> 文档层级：当前实现基线（迁移前）上的版本治理重构方案。
> 术语口径：本文保留 `AgentVersionStore`、change set、release、main workspace、candidate worktree 等历史实现和设计事实；四阶段改进治理术语见 [AgentGov术语与版本边界](../../AgentGov术语与版本边界.md)。
> 归档边界：本文只用于追溯版本治理迁移和落地过程。
> 四阶段改进治理覆盖规则：改进治理工作台中的发布门禁、回归验证、Diff 查看入口和用户主动作，以 [AgentGov 四阶段改进治理工作台 UI 整改方案](../../AgentGov_四阶段改进治理工作台UI整改方案.md) 为准。

## 0. 评审采纳状态

本文为 2026-06-03 两份评审报告后的修订版，评审对象包括：

- `docs/archive/design/Agent版本治理与Diff对比重构方案评审报告.md`
- `docs/design_review_report/Agent版本治理与Diff对比重构方案评审报告v2.md`

整改前核查结论：

- GV-1 到 GV-19 均属实，未发现需要推翻的评审结论。
- 整改前实现仍是 `AgentVersionStore` tar/manifest 快照主流程，旧 `/api/agent-versions/main/*`、主 workspace 写入、主 profile 回归和 `docker/volume` 默认挂载仍存在。
- 本方案采纳全部评审意见。实施必须先完成“阶段 0：迁移契约与 GSD 对齐”，再进入代码改造。

本修订版优先级高于评审前旧草案。实现时不得按旧草案中的“Phase 1 直接删除旧 API/旧版本目录”执行。

## 0.1 阶段 1-5 落地状态

截至本轮整改，阶段 1-5 已按本方案完成主链路落地：

- 后端新增 Git-backed Agent repository、change set、event、release 和 restore 切换链路。
- 执行应用先写候选 worktree 并提交 candidate commit，不再直接修改主 workspace。
- 候选回归使用 candidate worktree 上下文，publish 后才推进主 workspace。
- 前端版本治理工作台、OpenAPI、生成类型和任务 diff 消费者已迁移到新 change set / release API。
- 旧 `/api/agent-versions/main/*` router、前端 helper、旧 restore/manifest API schema 和旧运行时 `current.json` payload 字段已删除。
- 当前前端主路径把“发布”放在优化批次动作区，把“切换到版本”放在版本列表行级“操作”菜单中；approve、reject 和 change set regression API 保留为后端治理能力、自动化或后续门禁扩展，不再作为默认用户操作按钮。

保留的 `AgentVersionStore` 仅用于历史快照能力和对应单元测试，不再作为运行时 HTTP API 主流程。

## 1. 背景与目标

反馈优化闭环已经具备反馈采集、证据包、归因分析、优化建议、执行方案、回归资产和批次回归能力，但主 Agent 配置版本治理仍停留在“快照包 + manifest + 文件 diff”的阶段。该模式可以保存配置状态，却不能完整支撑一次优化改动的工程化评审。

本次重构目标是把主 Agent 配置从“备份式管理”升级为“可对比、可审批、可回归、可发布、可回滚”的治理链路，回答以下问题：

```text
这次优化改了什么
为什么要改
关联了哪些反馈、Trace、归因结果和优化建议
修改是否合理
是否通过候选版本回归
是否可以发布
发布后是否可追踪和回滚
```

## 2. 整改前实现与已验证问题

整改前主 Agent 配置位于容器内 `/main-workspace`。本地默认宿主机目录仍是 `docker/volume/main-workspace`，后续默认将迁到 `${HOME}/volume-agent-gov/main-workspace`。

整改前 `AgentVersionStore` 以 tar 包和 manifest 管理版本：

```text
/data/agent-versions/main/
  bundles/
  manifests/
  versions.jsonl
  current.json
```

整改前能力包括：

- 创建配置快照。
- 比较两个快照的文件增删改。
- 展示单文件 unified diff。
- 回滚到某个快照并生成恢复前快照。
- 在执行优化任务应用后记录 `pre_execution_agent_version_id` 和 `applied_agent_version_id`。

评审核查确认以下问题仍成立：

- 候选回归通过 `FeedbackEvalRunner.run_feedback_eval()` 调用普通 `run_chat`，会跑主 runtime，而不是 candidate worktree。
- `ExecutionApplicationService.safe_workspace_target()` 以 `settings.main_workspace_dir` 为根，执行应用只能写主 workspace。
- `AgentVersionStore` 被 runtime、worker、router、core health、execution service 和 `FeedbackStore` provider 多处具体方法耦合。
- 旧 `/api/agent-versions/main/*` 仍被后端 router、前端 helper、OpenAPI 测试和生成类型依赖。
- 历史 `optimization_tasks`、`execution_applications`、`eval_runs`、`regression_plans` 仍保存旧 agent version id。
- Docker Compose、`.env.example`、Makefile 和 README 仍围绕 `docker/volume`。

## 3. 目标架构

本次重构采用：

```text
Agent 配置文件仍是源码
后端受控 Git provider 负责 refs、Tag、Diff、分支、worktree、发布归档和回滚执行
SQLite 负责业务治理元数据
前端负责优化批次发布和已发布版本切换主操作；审批、拒绝和候选回归作为后台治理能力保留，不作为默认用户决策点
归档包作为发布产物保留
```

本轮默认采用 `local` Git provider：主 Agent workspace 本身是 Git 仓库，候选变更通过本地 worktree 隔离。`AGENT_GIT_SERVICE_PROVIDER=gitea`、`AGENT_GIT_SERVICE_URL` 和 `AGENT_GIT_SERVICE_PUBLIC_URL` 仅作为后续外部 Git 服务展示/发现配置，不作为本轮阶段 1-5 的硬完成条件。发布、回滚、后台候选回归和治理状态变更仍必须通过产品 API。

本轮只覆盖业务 Agent 版本链：

```text
main-agent
/main-workspace
```

治理执行者运行态已合并为单一 `governor`。本文不把 `governor` 自身的 workspace、claude root 和 profile 配置纳入业务 Agent Git 发布治理；`governor` 只按 job_type 承担归因、方案、执行、用例治理和回归影响分析职责。

```text
governor
/governor-workspace
/claude-roots/governor
```

历史 job 记录中的合并前旧 `profile_name` 只作为历史元数据保留，不再作为当前 profile/workspace 设计。候选执行和候选回归期间，`governor` 的执行类 job 与 main profile 必须临时读取 candidate worktree，不能读取线上 `/main-workspace`。

## 4. 评审意见采纳矩阵

| Finding | 是否采纳 | 落地要求 |
| --- | --- | --- |
| GV-1 候选回归缺少真实 runtime 隔离入口 | 采纳 | 新增 candidate profile builder 和 `run_feedback_eval` 候选入口，metadata 写入 `change_set_id`、`candidate_commit_sha`。 |
| GV-2 旧 API 删除未绑定消费者迁移 | 采纳 | 旧 `/api/agent-versions/main/*` 只能在最后阶段与后端、前端、OpenAPI、测试消费者原子删除。 |
| GV-3 旧版本目录删除会破坏历史投影 | 采纳 | 本轮不物理删除旧目录；旧 HTTP API 删除后，历史字段仍在任务/eval/run 投影中可解释展示。完整 tombstone 清理列为后续增强。 |
| GV-4 publish/tag/archive/DB 缺可恢复状态机 | 采纳 | 本轮落地 publish/rollback 状态、annotated tag 和 release archive；启动 reconciliation 列为后续增强。 |
| GV-5 Git provider 运维、安全初始化和 degraded 模式缺失 | 采纳 | 本轮落地 local Git provider、仓库状态和 degraded 响应；Gitea 安全初始化作为后续可选外部服务增强。 |
| GV-6 Change set 缺状态转移、并发锁和唯一性 | 采纳 | 新增完整状态转移表、唯一约束、状态前置条件和并发测试。 |
| GV-7 API 草案不足以支撑三栏工作台 | 采纳 | 补 change set 列表、events、regression-runs、release archive 下载接口。 |
| GV-8 operator/reason 未对齐认证现实 | 采纳 | v1 使用声明式 operator，同时记录 request source、API key alias 或部署身份，reason 必填。 |
| GV-9 host volume 迁移影响本地调试和脚本 | 采纳 | 默认迁到 `${HOME}/volume-agent-gov`，同步 Makefile、Compose、README、PyCharm 说明和迁移脚本。 |
| GV-10 `.worktreeinclude` overlay 可能污染候选回归 | 采纳 | 本轮候选回归绑定 candidate worktree；overlay manifest 自动检测列为后续增强。 |
| GV-11 `reset --hard` 缺场景保护 | 采纳 | Git 白名单拆成命令、场景、目标 worktree，`reset --hard` 只允许受控 cleanup/rollback。 |
| GV-12 验证矩阵缺旧数据、degraded 和 reconciliation | 采纳 | 增加旧 API 删除、Git provider、候选执行/回归、publish/rollback、浏览器 smoke 验证；reconciliation 压入后续增强。 |
| GV-13 缺 GSD artifacts | 采纳 | 新增 `.planning/PROJECT.md`、`ROADMAP.md` 和 phase `CONTEXT.md`/`PLAN.md`。 |
| GV-14 `safe_workspace_target` 硬编码主 workspace | 采纳 | 引入 `ExecutionTargetContext` 或显式 `workspace_dir`，候选执行写 worktree。 |
| GV-15 `AgentVersionStore` consumer 断裂面大 | 采纳 | 阶段 0 建 consumer 矩阵，定义 `AgentVersionProvider` 协议，过渡 facade 只作为迁移窗口。 |
| GV-16 合并前旧执行优化 profile 读写目标有歧义 | 采纳 | Governor 合并后，候选执行期间动态构建 governor 执行上下文，读 candidate worktree、写 candidate worktree。 |
| GV-17 candidate `.mcp.json` 和 settings 路径派生缺失 | 采纳 | candidate profile 统一派生 workspace、mcp、settings、readable paths、claude root。 |
| GV-18 provider 异常被 `FeedbackStore` 静默吞掉 | 采纳 | 版本治理写路径 provider 失败必须阻断；测试断言 eval run 记录合法 commit sha。 |
| GV-19 Compose fallback 仍是 `./volume` | 采纳 | 所有挂载 fallback 改为由 `HOST_RUNTIME_VOLUME_ROOT` 单根派生。 |

## 5. 运行态目录与 Git 仓库拓扑

宿主机默认运行态根改为：

```text
${HOME}/volume-agent-gov
```

容器内路径保持不变：

```text
/main-workspace
/data
/claude-roots/main
/governor-workspace
/claude-roots/governor
```

Compose 变量必须由单根派生：

```text
HOST_RUNTIME_VOLUME_ROOT=${HOME}/volume-agent-gov
HOST_WORKSPACE_MOUNT=${HOST_RUNTIME_VOLUME_ROOT}/main-workspace
HOST_DATA_MOUNT=${HOST_RUNTIME_VOLUME_ROOT}/data
HOST_CLAUDE_ROOT_MOUNT=${HOST_RUNTIME_VOLUME_ROOT}/claude-roots/main
```

旧 `docker/volume` 作为迁移来源和显式兼容路径保留。用户可以显式设置 `HOST_RUNTIME_VOLUME_ROOT=./volume` 临时沿用旧路径，但默认部署不再把运行态目录放在源码仓库内。

本轮 Git 拓扑：

```text
/data/business-agents/<agent_id>/workspace
  .git
  master/main
  tags/agent-release-...

/data/business-agents/<agent_id>/version/worktrees/
  agc-<change_set_id>/

/data/business-agents/<agent_id>/version/releases/
  agr-<release_id>.tar.gz
```

后端 Git 执行层必须显式隔离 Git 发现边界：

- 使用 `GIT_DIR` 和 `GIT_WORK_TREE`，或设置 `GIT_CEILING_DIRECTORIES`。
- 所有 Git 命令使用 subprocess 参数数组，不通过 shell 拼接。
- 不接收用户传入任意 remote URL，内部 Git 服务地址只能来自后端配置。

## 6. 数据模型与状态机

### 6.1 `agent_change_sets`

关键字段：

```text
change_set_id
agent_profile = main-agent
optimization_task_id
source_batch_id
source_plan_task_id
feedback_case_ids_json
proposal_ids_json
attribution_job_ids_json
execution_job_id
base_commit_sha
candidate_commit_sha
branch_name
worktree_path
status
diff_summary_json
planned_diff_json
review_json
latest_regression_run_id
publish_release_id
overlay_manifest_json
operator
reason
created_at
updated_at
```

状态转移：

```text
created -> candidate_ready | failed | abandoned
candidate_ready -> pending_approval | abandoned
pending_approval -> approved | rejected | request_changes | abandoned
request_changes -> candidate_ready | abandoned
approved -> regression_running | rejected | abandoned
regression_running -> regression_passed | regression_failed | abandoned
regression_failed -> regression_running | rejected | abandoned
regression_passed -> publish_fetching | abandoned
publish_fetching -> publish_merging | pending_manual_recovery
publish_merging -> main_pushed | pending_manual_recovery
main_pushed -> tag_created | pending_manual_recovery
tag_created -> tag_pushed | pending_manual_recovery
tag_pushed -> archive_created | pending_manual_recovery
archive_created -> db_recorded | pending_manual_recovery
db_recorded -> workspace_updated | pending_manual_recovery
workspace_updated -> published | pending_manual_recovery
published -> rolled_back
rejected -> terminal
abandoned -> terminal
rolled_back -> terminal
pending_manual_recovery -> published | failed
failed -> terminal
```

约束：

- `optimization_task_id` 最多允许一个非 terminal change set。
- `branch_name`、`candidate_commit_sha`、`worktree_path` 唯一。
- 所有写操作必须校验当前状态前置条件。
- publish 必须使用 DB row lock 加 Git remote sha 条件。

### 6.2 `agent_releases`

关键字段：

```text
release_id
agent_profile = main-agent
tag_name
commit_sha
parent_release_id
source_change_set_id
source_batch_id
optimization_task_id
regression_run_id
gate_result_json
archive_path
archive_sha256
manifest_json
rollback_of_release_id
status
operator
reason
created_at
published_at
```

状态：

```text
published
rollback_published
superseded
pending_manual_recovery
failed
```

### 6.3 Legacy projection

旧 tar snapshot 不导入 Git 历史，避免制造伪历史；但旧 id 必须有可解释投影。

历史投影语义：

```text
legacy_agent_snapshot_id
legacy_agent_version_ref
legacy_created_at
legacy_status = deprecated
diff_supported = false
rollback_supported = false
deprecation_reason
```

本轮不自动移动或删除旧 `/data/agent-versions/main/`，避免破坏历史运行数据。后续如需物理清理，必须先补 tombstone deletion audit 和历史数据 UAT。

## 7. Provider 与 runtime 隔离

### 7.1 `AgentVersionProvider`

阶段 0 必须定义 provider 协议：

```text
current_version_id() -> str
current_ref() -> release tag | commit sha
current_commit_sha() -> full commit sha
is_ready() -> bool
status() -> repository status read model
```

规则：

- 版本治理写路径中 provider 失败必须阻断并记录错误，不允许静默写入 `None`。
- `FeedbackStore` 的通用历史读取可以保留 nullable 投影，但涉及 eval run、change set、publish 和 execution application 的写路径必须拿到合法 commit sha。
- 过渡期允许 `AgentVersionStore` facade，但只能为旧消费者迁移服务，必须在旧 API 删除阶段一并删除。

### 7.2 `ExecutionTargetContext`

执行应用不得默认写 `settings.main_workspace_dir`。新增显式目标上下文：

```text
ExecutionTargetContext(
  workspace_dir=change_set.worktree_path,
  base_commit_sha=...,
  candidate_branch=...,
  change_set_id=...
)
```

候选执行要求：

- `safe_workspace_target(target_path, context)` 只允许落在 `context.workspace_dir`。
- `apply_execution_operations(..., context)` 写 candidate worktree。
- 执行期间通过 inode/mtime 或 hash 快照证明 `/main-workspace` 未变化。

### 7.3 Candidate profile builder

候选执行和候选回归必须动态构建 profile：

```text
workspace_dir = change_set.worktree_path
mcp_config_path = change_set.worktree_path / ".mcp.json"
project_settings_path = change_set.worktree_path / ".claude" / "settings.json"
readable_paths = (change_set.worktree_path, data_dir)
claude_root = isolated candidate claude root
```

`governor` 执行类 job 在候选阶段必须读取 candidate worktree，不读取线上 `/main-workspace`。候选回归必须读取 candidate `.mcp.json` 和 candidate `.claude/settings.json`。

## 8. Git 服务与命令安全

local Git provider degraded 模式：

- `/health`、release/change set 详情和 `/api/agent-repository` 只读状态可用。
- create change set、approve/reject、regression-runs、publish、rollback、abandon 等写接口在 provider 不可用时阻断并返回错误。
- `/api/agent-repository` 返回 `status=degraded` 和 `degraded_reason`。

Git 命令白名单必须绑定场景。`git reset --hard` 只允许：

- 临时 worktree cleanup。
- 受控 rollback。

执行前必须确认目标 commit 来自受控仓库，目标 worktree 不含未归档用户变更，执行后写审计事件。

## 9. API 设计

### 9.1 最后阶段删除旧契约

旧 API 不在早期删除。只有新治理 API、前端、OpenAPI、测试和历史投影全部迁移后，才删除：

```text
GET  /api/agent-versions/main/current
GET  /api/agent-versions/main
POST /api/agent-versions/main/snapshots
GET  /api/agent-versions/main/{version_id}
GET  /api/agent-versions/main/diff
GET  /api/agent-versions/main/file-diff
POST /api/agent-versions/main/{version_id}/rollback
```

验收命令：

```bash
rg "/api/agent-versions/main" app frontend tests docs
```

仅允许出现在废弃说明或迁移测试中。

### 9.2 仓库与 release API

```text
GET  /api/agent-repository
POST /api/agent-repository/discard-changes
POST /api/agent-repository/snapshot
GET  /api/agent-repository/current

GET  /api/agent-releases
GET  /api/agent-releases/{release_id}
POST /api/agent-releases/{release_id}/restore
```

### 9.3 Change set API

```text
POST /api/agent-change-sets
GET  /api/agent-change-sets?status=&optimization_task_id=&limit=
GET  /api/agent-change-sets/{change_set_id}
GET  /api/agent-change-sets/{change_set_id}/events
GET  /api/agent-change-sets/{change_set_id}/diff
GET  /api/agent-change-sets/{change_set_id}/file-diff?path=
POST /api/agent-change-sets/{change_set_id}/approve
POST /api/agent-change-sets/{change_set_id}/reject
POST /api/agent-change-sets/{change_set_id}/abandon
POST /api/agent-change-sets/{change_set_id}/regression-runs
POST /api/agent-change-sets/{change_set_id}/publish
```

治理写操作请求体：

```json
{
  "operator": "reviewer",
  "reason": "已确认候选 Diff 与回归结果，可发布。",
  "comment": "可选补充说明"
}
```

v1 不引入多用户 RBAC。后端必须记录声明式 `operator`、request source、API key alias 或部署身份、request_id、时间和 reason。`reason` 不能为空。

## 10. 前端设计

`AgentVersionsWorkspace` 替换为三栏治理工作台：

```text
左侧：change set / release / rollback 列表
中间：文件列表 + 单栏/双栏 Diff
右侧：反馈、Trace、归因、建议、回归记录、发布记录、回滚记录
```

前端实现约束：

- 新增 `frontend/src/components/agent-governance/`，不继续膨胀 `BatchesWorkspace.tsx`。
- 新增 `DiffViewer` 组件，被版本治理工作台、优化任务详情和批次计划任务详情复用。
- 前端字段以 OpenAPI 生成类型为准，不手写长期漂移 schema。
- UI 标记候选回归是否包含 overlay-only 文件。

## 11. 旧设计删除、迁移、保留清单

### 11.1 删除

- 旧 `/api/agent-versions/main/*` API，最后阶段原子删除。
- 旧 `/data/agent-versions/main/` 版本包主流程不再作为运行时主流程；本轮不物理删除历史目录。
- UI 中“创建快照”的主流程按钮。
- 基于 tar manifest 的版本 diff 主流程。
- execution apply 直接写 `/main-workspace` 并创建前后快照的主流程。

### 11.2 迁移

- `AgentVersionStore.current_version_id()` 消费者迁移为 `AgentVersionProvider`。
- `applied_agent_version_id` 语义迁移为 release commit/tag。
- `pre_execution_agent_version_id` 语义迁移为 base commit。
- `applied_diff` 来源迁移为 Git diff。
- OpenAPI 和前端生成类型迁移到新治理 API。
- Docker host volume 默认从 `docker/volume` 迁移到 `${HOME}/volume-agent-gov`。

### 11.3 保留

- `profile_version_snapshot`，用于非主 Agent profile 的可复现 hash。
- `.worktreeinclude`，但仅作为候选 worktree overlay 清单，不进入 Git 版本真相。
- 现有回归资产、RegressionPlan、EvalRun gate_result、gate override 和 impact analysis 机制。
- 过渡 facade 仅在消费者迁移窗口保留，最后阶段删除。

## 12. 阶段计划

### 阶段 0：迁移契约与 GSD 对齐

交付：

- `AgentVersionStore` consumer 矩阵。
- `AgentVersionProvider` 协议。
- 历史版本字段解释契约。
- candidate worktree runtime context 契约。
- `ExecutionTargetContext` 契约。
- publish/rollback 状态机契约。
- Git provider degraded 模式和 Docker 单根挂载契约。
- `.planning` phase artifacts。

验收：

- 方案文档覆盖 GV-1 到 GV-19。
- `.planning/PROJECT.md`、`ROADMAP.md`、phase `CONTEXT.md`、`PLAN.md` 存在。
- 后续阶段可以直接基于 PLAN 执行，不需要重新决策。

### 阶段 1：Git provider 与 bootstrap

交付：

- local Git provider 和统一 Git 操作入口。
- `AgentVersionProvider` 与过渡 facade。
- `/main-workspace` bootstrap commit。
- 仓库状态、dirty 检查和 degraded 响应。
- `HOST_RUNTIME_VOLUME_ROOT` 默认外部根目录。

验收：

- `/main-workspace/.git` 存在且可解析当前 commit。
- `/api/agent-repository` 返回仓库、worktree、release 目录和 dirty 状态。
- provider 失败时版本治理写路径阻断；只读状态返回 degraded 原因。

### 阶段 2：Change set 与候选执行

交付：

- `agent_change_sets` 表、状态机、唯一约束。
- 创建 branch/worktree。
- `ExecutionTargetContext` 和 candidate write path。
- execution optimizer 候选 profile。
- candidate commit 和 diff。

验收：

- 候选执行不修改 `/main-workspace`。
- 多任务可并行创建不同 worktree。
- 重复 create、路径逃逸、baseline 冲突和并发 publish 可预测失败。

### 阶段 3：审批与候选回归

交付：

- approve/reject API。
- candidate worktree runtime context。
- candidate regression run。
- EvalRun 绑定 `candidate_commit_sha`。

验收：

- 候选回归读取 candidate worktree。
- metadata 包含 `change_set_id`、`candidate_commit_sha`、`candidate_worktree_path`。
- 回归失败不能 publish。
- provider 失败不会静默写入空版本。

### 阶段 4：发布、归档和回滚

交付：

- publish 状态机。
- annotated tag。
- release archive。
- rollback release。

验收：

- publish 失败进入可解释错误状态，不静默污染任务状态。
- release archive hash 可校验。
- rollback 生成新的 rollback release，不删除历史 release。
- `/main-workspace` 更新到目标 commit。

### 阶段 5：前端治理工作台与旧契约删除

交付：

- Agent 版本治理工作台。
- DiffViewer 复用。
- 关联反馈、Trace、归因、建议、回归、发布状态展示。
- OpenAPI、generated types、README 和产品文档同步。
- 删除旧 `/api/agent-versions/main/*` router、前端 helper、生成类型和测试断言。

验收：

- `rg "/api/agent-versions/main" app frontend tests docs` 仅命中废弃说明或迁移测试。
- 浏览器中可在批次页发布回归通过的候选版本，并在版本页切换到任意已发布版本；审批、拒绝和候选回归不作为默认用户入口。
- TypeScript build 通过。
- feedback browser smoke 无 console error、failed request 和 4xx/5xx。

## 13. 验证矩阵

| 验证项 | 命令或动作 | 成功标准 |
| --- | --- | --- |
| 治理硬门 | `.venv/bin/python scripts/check_codex_governance.py --mode fail` | 无 `FAIL` |
| 后端全量 | `make test` | pytest 全部通过 |
| OpenAPI 导出 | `.venv/bin/python scripts/export_openapi.py` | 运行时 `/openapi.json` 或临时导出 OpenAPI JSON 与代码一致 |
| 前端类型 | `pnpm --dir frontend generate:api-types` | `frontend/src/types/api.ts` 已同步 |
| 前端构建 | `pnpm --dir frontend build` | TypeScript 和 Vite build 通过 |
| 浏览器 smoke | `pnpm --dir frontend verify:feedback-browser` | console error、failed request、4xx/5xx 为 0 |
| Git provider | API probe + pytest | `/api/agent-repository` 可返回 local Git 仓库状态 |
| Git bootstrap | pytest | 不误发现源码仓库，bootstrap commit 正确 |
| 候选执行 | pytest | 不修改 `/main-workspace`，worktree commit 正确 |
| 候选回归 | pytest + API 测试 | 读取 candidate worktree，metadata 与 eval run 版本正确 |
| 发布回滚 | pytest + API 测试 | tag/archive/release/rollback 可追踪 |
| Degraded 模式 | pytest + API 测试 | Git provider 不可用时写接口阻断，只读状态不崩溃 |
| 真实数据 | 使用迁移后的历史数据 | 列表、详情和新 change set/release 详情不触发 500 |

## 14. 关键验收清单

- 候选执行写入目标根等于 `change_set.worktree_path`，不是 `/main-workspace`。
- 候选回归 metadata 包含 `source=regression_eval`、`change_set_id`、`candidate_commit_sha`。
- EvalRun 顶层 `agent_version_id` 等于 candidate commit。
- 候选回归读取 candidate worktree。
- `FeedbackStore` 在版本治理写路径中不再吞掉 provider 异常。
- Git provider 不可用时，change set/publish/rollback 写接口阻断，仓库状态只读接口不崩溃。
- `HOST_RUNTIME_VOLUME_ROOT` 未设置时使用 `${HOME}/volume-agent-gov`；显式设置后所有挂载统一指向该根。
- `.venv/bin/python scripts/check_codex_governance.py --mode fail`、`make test`、OpenAPI export、前端类型生成、前端 build、browser smoke 全部通过。

## 15. 仍需后续实现确认的问题

- 发布门禁是否允许 `passed_with_notes` 默认发布。当前方案允许，但要求 gate result 明确记录 notes；如产品希望更严格，可在阶段 3 调整为仅 `passed`。
- break-glass override v1 仍基于 API key 环境下的声明式 operator，不提供真实多用户 RBAC。若未来引入 RBAC，应在 `operator` 审计字段基础上扩展。
- `.worktreeinclude` overlay 允许候选回归读取本地非 Git 文件；release 不依赖 overlay-only 文件的自动检测列为后续增强。
