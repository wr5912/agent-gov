# 业务 Agent Workspace 激活故障恢复 Runbook

本文用于处理 Workspace 导入或恢复在崩溃、持久化回执丢失等情况下留下的
`recovery_required` 等激活围栏。它是本机平台运维入口，不是业务 API，也不提供 UI、HTTP
写入口或通用 Git 修复能力。

## 适用条件

只有同时满足以下条件时才进入恢复：

- 已暂停目标 Agent 的 Workspace 导入、恢复、发布、测试和运行请求；
- 已结合 API/worker 日志、进程、活动 turn、HITL 和测试运行确认没有仍在执行的原操作；
- 故障对象是 `list` 或 `inspect` 返回的既有 Workspace activation operation；
- 操作者能够说明恢复原因，并保留本次命令、输出和关联事故记录。

命令会再次核对 admission claim、活动 session/turn/test、Workspace 字节、index、HEAD、Git
对象图、durable refs 和导入审计。0055 journal 的 graph identity 在 `prepared` 后不可变；对象类型、
唯一 parent、tree 关系和 restore target 都在平台自有 Git authority 中验证。commit 的 tree/parents
直接从 raw commit object header 解析，不依赖可被 grafts 或 replace ref 重写的图展开。

受管命令使用绝对 Git executable 并显式固定 `--git-dir` 与 `--work-tree`，不继承调用方
`GIT_*`。repository-local filter/diff/merge/signing/credential 外部命令驱动、`core.worktree`、
object alternates、非规范 `commondir`、grafts、partial clone/promisor、lazy fetch、hook 与
fsmonitor command 均 fail-closed；平台 linked worktree 只接受 canonical backlink/common-dir topology。
repository-local `include.path` 不得跟随 FIFO 或其他非普通文件，并必须在有界 timeout 内拒绝；
`tar.<format>.command` archive command 不得执行；主 Workspace 的 `.git/commondir` 不得重定向
另一 Agent authority。activation HEAD mutation 与 ref cleanup 都要重做下述 topology 校验。
index 指纹保留 `assume-unchanged`、`skip-worktree` 与 fsmonitor flag 的语义。operation refs 必须是
限定 namespace 下的 direct refs，HEAD 只能 detached 或指向 `refs/heads/*`，不能指向
activation namespace。任一证据发生变化都会安全拒绝，不应通过手工改库、改 ref 或使用通用 Git 命令绕过。

migration 0058 会在升级时幂等重装 0055/0057 authority triggers，使已应用旧版迁移的持久卷
也获得完整合法转移、prepared 后 graph 冻结、terminal 不可变/禁止删除和 recovery
terminal-evidence 约束。如当前数据库尚未记录 0058，先使用当前 Runtime 正常执行迁移，
不要手工重创 trigger 或删改 journal 行。

## 1. 只读发现

只使用仓库公开的 Make 入口；它按 `COMPOSE_ENV_FILE` 选择完整 Compose 配置并先校验 runtime
contract。先列出当前围栏候选。默认入口只读，不构造 Git store，也不会创建缺失的 Agent Workspace：

```bash
make workspace-activation-recovery \
  WORKSPACE_ACTIVATION_RECOVERY_ARGS='list'
```

选择一个明确的 `operation_id` 后执行只读检查：

```bash
make workspace-activation-recovery \
  WORKSPACE_ACTIVATION_RECOVERY_ARGS='inspect --operation-id <wao-operation-id>'
```

保存本次输出中的 `operation_id`、`recovery_id` 和 `state_digest`。输出只投影状态、计数、
ref 名称和摘要，不输出 commit SHA、文件路径、Git status/index 原文、诊断内容或维护 token。

## 2. 精确 reconcile

证据确认无误后，使用刚才同一次 `inspect` 的三个标识执行默认 reconcile：

```bash
make workspace-activation-recovery \
  WORKSPACE_ACTIVATION_RECOVERY_ARGS='apply --operation-id <wao-operation-id> --recovery-id <war-recovery-id> --state-digest <sha256-state-digest> --operator <operator-id> --reason "confirmed exact activation evidence after original process exit"'
```

`apply` 会先获取该 Agent 的 stable per-Agent lock，再预留 append-only durable attempt 并重算摘要。
只有 attempt、operation 和摘要完全匹配时，activation 状态机才继续完成或拒绝原操作；0057 对
reserve、started observation 和 terminal evidence 的 commit ack-loss 都只接受精确 durable reread。
该动作不会强制选择完成或拒绝结果。

completed attempt 的 `result` 必须且只能包含 `activation_state`、`resolution`、`already_applied`、
`repaired_ref_names`，其中两个状态字段必须一致、布尔值类型必须精确、ref 名称必须来自允许集合且不重复；
failed attempt 的 `error` 只能包含一个稳定 `code`。空、冲突、额外或类型伪造的终态证据会以
`RECOVERY_ATTEMPT_EVIDENCE_INVALID` 拒绝，SQLite raw write、读取投影和 `resume` 也不能绕过。

## 3. 严格补齐缺失 durable ref

仅当 `inspect` 明确返回 `repair_missing_refs` 可用时，才允许追加该 action：

```bash
make workspace-activation-recovery \
  WORKSPACE_ACTIVATION_RECOVERY_ARGS='apply --operation-id <wao-operation-id> --recovery-id <war-recovery-id> --state-digest <sha256-state-digest> --operator <operator-id> --reason "confirmed strict-subset durable ref loss after original process exit" --action repair-missing-refs'
```

该动作只能把 journal 中已记录的期望对象以 CAS 方式补到缺失 ref；实际 ref 必须是期望集合的
严格子集，既有 ref、对象类型、提交关系、tree、HEAD、index、Workspace 和审计均须精确。
命令不接受任意 SHA，也不会覆盖冲突 ref。

## 4. 续跑已预留 attempt

如果 `apply` 进程中断，而新的 `list` 或 `inspect` 仍显示
`active_recovery_attempt.state=reserved`，只能从该对象复制既有 `recovery_id` 并续跑：

```bash
make workspace-activation-recovery \
  WORKSPACE_ACTIVATION_RECOVERY_ARGS='resume --recovery-id <reserved-war-recovery-id>'
```

`resume` 从 0057 journal 读取原 operation、action、state digest、operator 和 reason，在 stable lock
内重新确认同一个 reserved attempt 后继续；它不接受也不允许重新提供或修改这些字段。缺失、错误、
failed 或 completed 的 recovery ID 都会拒绝。若 `resume` 回执再次丢失，先重新 `list`/`inspect`：仍为
reserved 就再次 resume，即使 activation 已为 terminal，也由 resume 幂等补齐 attempt 终态；只有
attempt 已为 completed 时才只做结果核验。

## 重试与失败处置

- `STATE_DIGEST_MISMATCH` 或其他证据变化：重新执行 `inspect`，重新判断现场，使用新生成的
  `recovery_id`；不要沿用旧摘要。
- apply 进程中断且 attempt 仍为 `reserved`：只执行 `resume --recovery-id`，不重复提交原 apply 字段，
  也不要另开 attempt 抢占。
- attempt 已为 `failed`：保留失败记录，重新 inspect 后使用新的 recovery ID。
- attempt 已为 `completed`：不要 resume；再次 inspect/list 验证 activation 终态和围栏释放。
- 无法确认原进程已经停止、存在活动工作、证据不精确或 reserved attempt 身份已丢失：保留围栏
  和现场并升级事故，不直接编辑 SQLite、删除 attempt、清理 fence 或执行 `git update-ref`。

恢复成功后，确认 activation operation 已进入 `completed` 或 `rejected`，活动 admission fence 已按
原状态机释放，再重试原业务动作并恢复上游流量。平台的普通 repository、config 与 bootstrap writer
会在同一 stable per-Agent lock 内统一复核 exact instance、deletion fence 和 activation fence；它们在
围栏期间必然拒绝。只有 activation/recovery 专用 authority 可跨越自身 activation fence，并且仍不得
绕过实例或删除边界。

## 明确禁止

本入口没有也不得增加 `force-complete`、`force-reject`、`clear-fence`、任意 SHA、任意数据库路径
或 HTTP mutation。宿主机同 UID/root 仍可绕过应用保护，因此 Compose/主机执行权限本身必须按
平台运维权限管理，命令输出也应进入受控事故记录。

## 文档边界

- 当前事实：公共 Make CLI、受管 Git authority、0057 append-only attempt、0058 trigger refresh、
  exact reconcile、strict-subset ref repair 和 reserved attempt resume 的运行方式。
- 未选择的替代：独立恢复 HTTP/API、UI 按钮、通用 Git/SQLite 修复器；这些会扩大误操作和越权面。
- 退出条件：若未来引入集中式 operator control plane、强身份审计和等价的 exact authority 契约，
  本机 CLI 可降级为该控制面的受控执行器，并同步改写本文。
