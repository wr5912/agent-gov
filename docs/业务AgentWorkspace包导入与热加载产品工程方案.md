# 业务 Agent Workspace 包与运行卷初始化工程契约

> 文档状态：当前产品工程契约。公开字段以 OpenAPI 为单一真相源。
>
> 本文取代旧“通用模板 + 声明 seed + 运行态 seed catalog + 直接创建 API”设计。旧设计只在
> [归档](./archive/design/业务Agent工作区资产闭环产品工程方案.md) 中保留审计价值。

## 1. 裁决

| 裁决 | 事实依据 | 删除的旧设计 | 验收 |
| --- | --- | --- | --- |
| 普通新 Agent 只通过 Workspace 包创建 | Agent 的可运行前提是完整 Claude 原生项目目录；仅填 name/ID 无法证明行为配置完整 | `POST /api/agent-registry`、`GET /api/agent-registry/templates`、`template_id`、`source_seed_id` | OpenAPI 不含旧路由/字段；新 ID 导入成功后进入注册表 |
| 只保留一个内置业务 Agent | 仓库只需提供一个可运行、可导出、可修改的起点 | `templates/business-agent/general` 和多个普通业务 Agent 出生副本 | 初始化源中的业务 Agent 集合严格等于声明的内置集合 |
| 内置、默认、受保护分开表达 | 三者分别回答“是否随版本提供”“兼容入口默认选谁”“是否可在线删除” | `origin=seed/user` 及由来源推导全部行为 | API 分别返回 `builtin`、`default`、`protected` |
| 初始化源不参与持续同步 | 运行态 Workspace 及其 per-Agent Git 才是当前行为事实 | 运行态 `data/seed-catalog`、删除标记、逐文件回灌 | 已存在 Workspace 整体跳过；重启不复活已删普通 Agent |
| Workspace 文件由包所有者明确维护，平台不改写 | 平台改写会让上传包、tree digest 和 Git commit 不一致；静默忽略来源 ID 又会把错误身份激活到目标 Agent | 身份文本渲染、endpoint renderer、权限覆盖、ID 忽略告警 | 包内 `agent.yaml.agent.id` 与目标 ID 完全一致时，普通文件字节与 executable bit 保持不变 |
| 导入请求同步返回，激活跨崩溃可恢复 | 单个包虽有资源上限，但 Git、session、audit 和 admission fence 是跨资源事实，不能把 HTTP 请求栈当成恢复边界 | 异步用户 job、仅依赖请求内补偿的旧流程 | 成功回执绑定 Git commit；未知状态保留 runtime fence，由启动/周期对账或精确运维恢复收口 |

当前唯一内置、默认且受保护的业务 Agent 是 `security-operations-expert`。这些是三个独立属性，
不是未来必须绑定在一起的单一类型。`main-agent` 是普通历史示例，不再享有默认、内置、保护或
模板语义。

## 2. 对象与路径

```text
仓库运行卷初始化源
docker/runtime-bootstrap/
├── governor-workspace/
└── business-agents/
    └── security-operations-expert/
        └── workspace/

宿主机运行卷
${HOST_RUNTIME_VOLUME_ROOT}/
├── governor-workspace/
└── data/business-agents/<agent_id>/
    ├── workspace/       # 当前 Claude 原生项目与 per-Agent Git 仓库
    ├── claude-root/     # Claude 会话状态，不属于 Workspace 包
    └── version/         # worktree/release 等版本治理状态，不属于 Workspace 包
```

`docker/runtime-bootstrap/` 是初始化源，不是模板 catalog、可在线编辑副本或普通 Agent 注册表。
运行态不存在 `data/seed-catalog/`。普通业务 Agent 的来源只在其导入回执和 Git 历史中审计，注册表
不持久化 `origin`。

## 3. Workspace 包

媒体类型为 `.tar.gz`，解压后必须恰好包含一个 `workspace/` 顶层目录：

```text
workspace/
  agent.yaml
  CLAUDE.md
  .mcp.json
  .claude/
  hooks/
  commands/
  tests/
    README.md
    test_*.py
  ...其他普通文件
```

包内普通文件由包所有者负责，平台逐字节保留：

- 允许文本、二进制、executable bit、`.env`、真实 endpoint、本机路径和 MCP header；
- 不改写 `agent.yaml`、`CLAUDE.md`、settings、MCP、hook、skill 或 subagent；
- `agent.yaml.agent.id` 是导入身份确认字段，必须有效且与 URL 中的目标 `agent_id` 逐字一致；
- 包内 profile、name、status 或说明文字不成为平台注册表身份事实；
- 空目录不进入 Git，不承诺导出后保留；
- conversation、SDK session、run、feedback、平台测试运行、Langfuse、数据库和 `claude-root` 不进入包。

`tests/` 与其他 Workspace 文件一样按字节导入、导出和版本化。导入缺少 `tests/` 或
`tests/README.md` 不拒绝包，但成功回执中的 `test_suite.diagnostics` 会给出 warning；没有
`tests/test_*.py` 的版本不能通过发布测试门禁。测试文件的详细契约见
[业务 Agent Workspace 原生 pytest 测试资产实现方案](./engineering/业务AgentWorkspace原生pytest测试资产实现方案.md)。

平台最终身份由目标路由 `agent_id` 和注册表持有，但导入前必须用 `agent.yaml.agent.id` 明确证明
包所有者选择的来源身份与目标一致。导出 `security-operations-expert` 后，可将该包作为新 Agent 的
人工修改起点；调用方必须先把包内 `agent.id` 明确改为新目标 ID，再重新打包和导入。这不是“模板
实例化”，平台也不会代替调用方改写身份。

### 3.1 导入身份裁决

本期要解决的问题是：旧实现允许包内来源 ID 与请求目标 ID 不一致，并把冲突降级为 warning，导致
操作者可能在未察觉时把一个 Agent 的行为资产激活到另一个 Agent。

本期统一执行以下规则：

- 新建和覆盖导入都必须在包根目录提供 UTF-8、安全且结构明确的 `agent.yaml`；
- `agent.yaml` 顶层必须是对象，且只允许一个对象类型的 `agent` 和一个字符串类型的 `agent.id`；
- `agent.id` 必须符合 Agent ID 字符规则，不允许首尾空白，并与 URL 中的目标 `agent_id` 大小写、
  字符和长度完全一致；
- 缺失、格式错误、ID 无效或 ID 不一致，都必须在 Workspace、注册表、Git 和会话状态发生变化前拒绝；
- 平台不 trim、不纠正、不推断也不改写包内 ID。

本期不采用“继续导入并告警”，因为告警不能阻止错误资产激活；也不采用“平台自动改写 ID”，因为这会
掩盖包的真实来源并破坏按字节交换契约。后续若要支持显式克隆，应设计独立动作、来源审计和目标路径
检查，不能重新放宽当前导入接口。

## 4. 公开 API

### 4.1 查询

```text
GET /api/agent-registry
```

每个 Agent 返回稳定身份、生命周期、`workspace_dir`、`requires_web_hitl` 以及三个独立派生字段：

```json
{
  "agent_id": "security-operations-expert",
  "name": "security-operations-expert",
  "status": "active",
  "builtin": true,
  "default": true,
  "protected": true
}
```

不返回 `origin`，也不提供模板列表。

### 4.2 创建或覆盖

```text
POST /api/agent-registry/{agent_id}/workspace/import
Content-Type: multipart/form-data
```

| 字段 | 规则 |
| --- | --- |
| `package` | 必填 `.tar.gz` |
| `name` | 目标 Agent 不存在时必填；存在时不得借此改名 |
| `expected_current_commit_sha` | 覆盖已有 Agent 时必填；用于确认目标仍处于操作者看到的当前提交版本 |
| `reason` | 可选提交说明，不进入 Workspace |

身份和并发错误必须返回稳定 `error_code`、明确 `detail`、失败字段、导入动作、预期目标和可执行的
`remediation`。其中：

- 缺少 `agent.yaml` / `agent.id`、YAML 无效或 ID 字符无效返回 `422`；
- 包内来源 ID 与 URL 目标 ID 不一致返回 `409`，并同时返回 `actual_agent_id` 和
  `expected_agent_id`；
- 已有目标未携带 `expected_current_commit_sha` 返回 `422`；携带的版本已经过期返回 `409`；
- 错误详情不得回显无效 ID 中可能携带的路径或其他不可信原文。

来源 ID 不一致的错误示例：

```json
{
  "error_code": "WORKSPACE_MANIFEST_AGENT_ID_MISMATCH",
  "detail": "导入被拒绝：包内来源 Agent ID “source-agent”与请求目标 Agent ID “target-agent”不一致；系统不会改写包内身份。请确认导入目标，并将 agent.yaml.agent.id 改为与 URL 中的 agent_id 完全一致后重新打包。",
  "field": "agent.yaml.agent.id",
  "import_action": "overwrite",
  "expected_agent_id": "target-agent",
  "actual_agent_id": "source-agent",
  "remediation": "确认导入目标，使 agent.yaml.agent.id 与 URL 中的 agent_id 完全一致后重新打包。"
}
```

成功响应中的 `action` 只有 `created`、`overwritten`、`unchanged`。新建响应示例：

```json
{
  "action": "created",
  "agent": {
    "agent_id": "customer-support",
    "name": "Customer Support",
    "status": "active",
    "builtin": false,
    "default": false,
    "protected": false
  },
  "previous_commit_sha": null,
  "current_commit_sha": "40-character-sha",
  "package_sha256": "sha256",
  "tree_sha256": "sha256",
  "rollback_target_commit_sha": null,
  "activation_mode": "next_turn",
  "import_record_id": "awi-...",
  "test_suite_status": "ready",
  "test_file_count": 2,
  "test_suite_diagnostics": []
}
```

相同 tree 重试返回 `unchanged`，不制造空 commit。每次成功导入返回操作唯一的
`import_record_id`、测试套件状态、测试文件数和完整结构化 diagnostics（warning 与 error）；完整测试清单通过
`GET /api/agent-registry/{agent_id}/test-suite?commit_sha=<sha>` 按精确提交查询。平台持久化同步
导入审计记录、`suite_status` 和完整 diagnostics，也不复制测试内容。新建使用不可见的 provisioning
reservation 与完成 token；覆盖导入和恢复则使用 migration 0055 建立的 durable activation journal。

activation 不是另一个用户可操作的异步 job。它在任何 live Git 变更前记录原始 HEAD、raw index
bytes、Workspace 指纹、候选 commit/tree、durable refs 和精确 maintenance claim。完成路径先持久化
`accepted` audit、inactive SDK session 失效和 `completion_outcome`；拒绝路径先持久化同一
`import_id` 的 `failed` audit 与 `rejection_outcome`。只有终态验证再次确认精确 audit、admission tuple
`(maintenance_token, maintenance_generation, generation, kind)`、无活动 session/turn/HITL、最终
HEAD/index/Workspace 字节和 durable refs 全部一致，才能进入 `completed` 或 `rejected`、清理 refs 并
释放 fence。restore 不产生 import audit，但其他终态证据和栅栏规则不变。任一证据缺失、冲突或
回执不明都进入 `recovery_required`，不得返回“失败”却留下已放行版本。

journal 记录的 graph identity 在进入 `prepared` 后不可修改；只允许集中状态机声明的
转移，`completed` / `rejected` 整行终态证据不可再改，activation journal 不得删除。
对象 ID 必须是规范小写 40/64 位值；original/base/candidate 必须是 commit，
candidate/original-index 必须指向 tree，snapshot 与 base/original 关系一致，overwrite/restore
candidate 只能有唯一 base parent，restore target 必须是与 candidate 同 tree 的 commit。commit
的 tree 与 parents 直接从 raw commit object header 解析，不使用会被 grafts 或 replace ref
重写的图展开结果。

index 证据同时包含普通 stage、`assume-unchanged`、`skip-worktree` 和 fsmonitor flag。
受管 Git 命令使用绝对 executable，并显式固定 `--git-dir` 与 `--work-tree`；平台
自有环境清除继承的 `GIT_*`，不执行 repository/global/system config 提供的
filter/diff/merge/signing/credential 外部命令，也不接受 `core.worktree`、object alternates、
非规范 `commondir`、grafts、partial clone/promisor、replace objects、lazy fetch、hook 或仓库
fsmonitor command 改写证据。平台生成的 linked worktree 只接受双向 backlink 与 canonical
common-dir topology。activation operation refs 必须是限定 namespace 下的 direct refs；HEAD 只能
detached 或指向 `refs/heads/*`，不得指向 operation namespace，ref 清理前后必须保持同一
HEAD topology。这样，同一 journal 不会因调用方环境或仓库配置改变而得到两种
“精确”解释。

migration 0058 不引入新状态表，而是幂等重装 0055 activation 与 0057 operator-recovery
authority triggers。因此，已记录“0055/0057 已应用”的旧持久卷也会获得完整合法转移、
prepared 后 graph 冻结、terminal 不可变/禁止删除和 recovery terminal-evidence 约束；不依赖
重放旧 migration 名称修复存量卷。

`migration 0059 trigger authority` 对 durable deletion journal 幂等重装完整合法转移、固定
身份快照、终态不可变/禁止删除与受限 witness cleanup 更新约束，包括已应用
旧迁移的持久卷；手工改状态、删行或重创 trigger 不是恢复入口。

### 4.3 导出与恢复

```text
POST /api/agent-registry/{agent_id}/workspace/export
POST /api/agent-registry/{agent_id}/workspace/restore
```

导出返回当前 Git tree 的 `.tar.gz` 和 commit/package/tree digest headers。恢复使用
`target_commit_sha` 与 `expected_current_commit_sha`，把历史 tree 写成新 commit，不 hard reset 历史。

### 4.4 生命周期与删除

普通 Agent 可通过生命周期 API 管理，也可在线删除；受保护业务 Agent 删除返回业务规则错误。

```text
DELETE /api/agent-registry/{agent_id}
If-Match: "<instance_etag>"
Idempotency-Key: agent-delete:<instance_etag>

GET /api/agent-deletion-operations/{operation_id}
GET /api/agent-deletion-operations?state=cleanup_pending&limit=20
```

`If-Match` 只接受一个带引号的强 entity tag，不接受裸值、`W/`、`*` 或多值。同一
`Idempotency-Key` 只能绑定同一 Agent 实例；默认键只由 `instance_etag` 派生为固定长度，
不拼入可变 Agent ID；跨 Agent 或跨实例复用返回 `409`。Agent ID 限 1–128 个 ASCII 字符，
只允许字母、数字、`.`、`_`、`-`，并拒绝 `.`、`..`、空白和路径穿越。删除在同一
SQLite 写事务中校验活动 turn/session、HITL、平台测试、待发布变更、release、cleanup、activation
和 maintenance blocker，然后写入 tombstone、`cleanup_pending` operation、删除前身份投影与治理影响面。
客户端不得提交 `deleted` 或 `impact`。

文件系统清理在同一个 Agent 稳定锁内，使用 device/inode/mount CAS 先将整棵
`data/business-agents/<agent_id>` 原子移入同卷隔离区，再以 no-follow fd walk 清理。完整清理并
持久化确认后返回 `200 completed`；Agent 已从可用列表移除、但磁盘清理尚未确认时返回
`202 cleanup_pending` 和脱敏 `Location`，客户端通过单项 GET 查询。页面重载或回执丢失时，
客户端通过有 API key 保护的有界列表恢复最近 operation：默认查询最新 20 条
`cleanup_pending`，也可查 `completed`，`limit` 范围为 1–100。单项与列表只投影稳定的
`last_error_code`、`attempt_count`、`updated_at` 及删除前公开摘要，不输出 Workspace/quarantine
路径、供给 token、device/inode 或 mount 证据。`workspace_removed` 与
`cleanup_complete` 只在 durable state 为 `completed` 时为真；不再清理 catalog 或返回 `seed_removed`。

该边界只排序平台内全部 Workspace/Git/测试/发布/删除 writer：它们共用 layout 外的 stable
per-Agent lock，并在 DB、runtime、HITL、测试和版本治理 fence 下复核；device/inode/mount CAS 与
no-follow 检查用于发现两个检查点之间的目录替换。它不声称抵御已取得 runtime volume 同 UID 或 root
权限的宿主机进程；该权限本就能够复制或改写 live Workspace、私有配置和运行数据，必须由主机与
Compose 运维权限另行控制。

曾经公开完成供给的 Agent ID 在删除后永久保留，不允许同 ID 重建。这是在 generation/CAS
尚未贯穿所有运行、反馈、测试和发布事实前防止新旧身份混同的安全边界。只有从未公开过的
provisioning 崩溃隔离例外：注册行必须无完成 token、携带精确
`workspace_must_be_absent` 恢复标记，且稳定锁下整棵 Agent layout 已不存在，才能清除隔离行并重试。
未来如要支持已公开 ID 的新一代实例，必须先让 generation 进入所有事实键和 CAS，再重审本限制。

## 5. 运行卷初始化

API 启动协调器读取 `docker/runtime-bootstrap/`：

1. 初始化必需运行目录和 governor Workspace；
2. 校验 `business-agents/` 的实际 ID 集合严格等于 `BUILTIN_BUSINESS_AGENT_IDS`；
3. 只在整个内置业务 Agent Workspace 不存在时复制；
4. 已存在 Workspace 整体跳过，不逐文件补缺、不覆盖、不产生隐式 commit；
5. 发现运行态所有合法 Workspace，并幂等同步到注册表；
6. 初始化各 Agent 的 Git 版本源，写入运行协调 receipt。

初始化源缺失、为空、含 symlink、内置集合多出或缺少任一 ID 时启动失败。
`docker/Dockerfile` 使用 `COPY docker/runtime-bootstrap /app/docker/runtime-bootstrap` 将该源内置 API 镜像；
Compose 不为该容器路径配置 host bind。初始化源变更只有在重建 API 镜像并
recreate 后才生效，不得用旧镜像启动结果声称候选已同步。

普通 Agent 不放进初始化源。需要一个新的普通 Agent 时，导出已有 Agent 或在仓库外制作完整 Workspace
包，再走 import API。只有产品明确决定新增内置 Agent 时，才同时修改声明集合、初始化源、准入扫描、
文档和空卷验收。

## 6. Git、并发与热加载

所有会修改或删除 per-Agent 运行态的平台 writer，包括新建、覆盖、恢复、版本治理、配置、bootstrap、
测试入队与删除，共用位于 Agent layout 之外的稳定 per-Agent 锁。普通 writer 在锁内、首个副作用前通过
统一 `BusinessAgentMutationPrecondition` 精确校验实例 generation、deletion fence 与 activation fence；
只有 activation/recovery 专用 authority 可在仍校验实例与删除边界的前提下跨越自身 activation fence。
普通 writer 在 `preparing`、`prepared`、`completing`、`rejecting` 或 `recovery_required` 期间均被平台拒绝，
只能在 operation 精确终态且围栏释放后重试。锁路径不会因整棵 layout 被原子隔离而消失，等待锁的旧
store 和新构造的 store 都必须在锁内重新校验 Agent 公开状态与目录身份，不得复活已删除根目录。

新建复用 registry reservation、no-follow 文件发布、Git 初始化、finalize 和失败补偿 saga，并在公开前对
canonical layout、HEAD、清洁状态和包 tree 做最终校验。整棵 Agent layout、`.git` 或 `version`
预先存在（包括 symlink）时拒绝，不接管、不删除外部写入者的残留。覆盖与恢复：

1. 获取该 Agent 的维护栅栏；
2. 在任何 live Git 变更前持久化 `preparing` intent 与原始 HEAD/status/index bytes/Workspace 指纹；
3. 拒绝活跃 turn、未终结 change set 和 SDK session 失效冲突；
4. dirty Workspace 使用临时 index 形成快照，在候选完整后记录 `prepared` 及 original/base/candidate/target refs；
   此后 graph identity 由 0055 数据库约束冻结；
5. 确认 `expected_current_commit_sha`、maintenance token/generation/kind 与候选 tree 仍精确后激活；
6. 在终态 outcome 事务中写入或校验 import audit（restore 无 import audit）、清除 inactive SDK resume 映射，并转入
   `completing` 或 `rejecting`；
7. 重新校验 audit、admission、活动工作、Git 字节和 durable refs，再提交 `completed` 或 `rejected`；
8. 任一补偿、清理或持久化证据不明时转入 `recovery_required`，保留 runtime fence。

当前 turn 的 HEAD、SDK mapping、active run 和 intent 在同一 admission 写屏障内绑定。导入成功后不重启
API；已有 API session ID 保留，新 turn 建立新的 SDK session 并读取回执中的 commit。

Workspace 字节证据使用 `fd-relative bounded Workspace fingerprint`：no-follow dir-fd 遍历不跟随
symlink，对条目数、单文件和总字节设置上限，并以双扫描 dev/inode 身份复核拒绝祖先或
文件替换。Git 对象证据使用 `fd-relative Git metadata/temp authority`：common-dir metadata 在已固定
fd 下有界读取，临时 ref/index/worktree 只能在平台所有且身份稳定的临时根中创建、
发布和清理精确对象；父目录或叶子被替换时 fail-closed，不删除替换者。

启动与周期 reconciler 只按 journal 中的精确证据幂等完成或拒绝。如果自动对账因证据不完整而长期保留
fence，运维人员只能通过 migration 0057 支撑的本机只读 `list`/`inspect`、精确 `apply`，以及仅按
既有 reserved recovery ID 续跑的 `resume` 入口处理；只允许 exact reconcile 或 journal 已记录对象的
strict-subset 缺失 ref 修复，不提供
force complete/reject、clear fence、任意 SHA、HTTP 或 UI 写入。具体操作见
[业务 Agent Workspace 激活故障恢复 Runbook](./engineering/业务AgentWorkspace激活故障恢复Runbook.md)。
0057 的 completed attempt 只接受精确四字段 result，failed attempt 只接受一个稳定错误码；空证据、
冲突结果、额外字段、错误布尔类型、未知或重复 ref 名都会在 store、SQLite trigger 以及投影/resume
三层 fail-closed，不以“状态已是 terminal”替代证据校验。
0058 在当前 Runtime 升级中重装上述 0055/0057 trigger；旧卷不需要也不得通过手工删行、
改状态或重创 trigger 补齐。

## 7. 输入保护与仓库边界

首版保护直接针对文件系统越界和资源耗尽：

- `/api/*` API Key；压缩包最大 64 MiB；解压总量最大 256 MiB；单成员最大 64 MiB；
- 最多 10,000 个成员；路径最大 4 KiB、深度最大 32；tar 元数据单记录最大 64 KiB；
- 拒绝绝对路径、`..`、NUL、非 UTF-8、重复项、文件/目录前缀冲突和任何 `.git` 成员；
- 拒绝 symlink、hardlink、device、FIFO、socket；
- `.mcp.json`、`.claude/settings.json` 如存在，必须是 JSON object；
- import 请求本身不执行上传包中的代码、测试、安装脚本或网络请求；只有用户后续显式发起平台测试时，
  才在固定 commit 的隔离 checkout 中执行固定 pytest 命令。

运行态 Workspace 和导出包是敏感运行数据，可按字节保留真实配置。回流仓库初始化源前必须在仓库外
形成候选，并通过 `make runtime-bootstrap-scan`；真实密钥、凭据型 header、数据库凭据和本机私有
路径硬阻断。非秘密 endpoint 与宽权限只提示复核，不静默改写。

## 8. UI 契约

设置页以一张业务 Agent 表作为唯一管理入口，不再并列展示 Workspace 清单和 Agent 管理清单：

- 表格一行对应一个注册业务 Agent，固定展示 Agent 身份、Workspace/测试状态、生命周期和操作；
- 生命周期保留行内选择器，归档终态继续禁止回转；
- “操作”使用对象级菜单，只包含导出 Workspace、覆盖导入和删除 Agent；受保护 Agent 的删除项
  必须禁用并显示原因；
- 页面级“导入 Agent”与行内“覆盖导入”复用同一个右侧抽屉。创建模式填写 Agent ID、name
  并选择包，覆盖模式锁定目标 Agent 的 ID 和名称，只选择包；
- 成功后抽屉保持打开，回执显示 action、previous/current commit、package/tree digest、测试状态、
  测试文件数和 warning；覆盖后在同一抽屉提供“恢复导入前版本”；
- 删除前用当前行的 `instance_etag` 做精确实例确认；`cleanup_pending` 时立即从可用列表移除，
  显示“后台清理待完成”并使用 operation 状态入口刷新，不得声称磁盘已彻底删除；
- 列表分别显示内置、默认、受保护状态；不显示来源选择器、通用模板、seed 提示或直接创建表单。

Settings 异步请求使用 `Settings request-context`：打开弹窗时绑定当前 apiBase/apiKey 与
上下文世代，registry、OpenAI compatibility 和 feedback 各 lane 独立递增请求世代。切换配置、
关闭/重开弹窗或新请求发布后，旧请求的 `late success/error/finally` 与链式 reload 全部丢弃；
删除 operation 的 pending discovery/action 也绑定同一类上下文，不得回写新配置或新一轮用户操作。

菜单必须支持 `Escape` 关闭、外部点击关闭和键盘方向键导航，并使用脱离滚动容器的浮层定位，避免
在表格底部或移动端被裁切。导入失败只在抽屉内显示结构化错误代码和可执行动作；关闭抽屉或切换
覆盖目标后清除旧文件、回执和失败状态。

## 9. 验收

- OpenAPI、前端类型和 UI 中不存在旧直接创建、模板 catalog、`origin`、`template_id`、
  `source_seed_id`、`seed_removed`。
- 空运行卷只得到 governor 和 `security-operations-expert`；已有运行卷中的普通 Agent 保持原样。
- 导出内置 Agent 后，先由包所有者把 `agent.yaml.agent.id` 设置为新目标 ID，再导入；平台不改写
  普通文件，字节和 executable bit 一致，registry ID 为目标 ID。
- 新建和覆盖导入都要求 `agent.yaml.agent.id` 有效且与 URL `agent_id` 逐字一致；缺失、无效、
  格式错误或不一致均在目标 Workspace、注册表、Git 和会话状态变更前拒绝，同时保留失败审计；
  缺少测试目录仍只告警。
- 所有业务 Agent Workspace 可携带 `tests/`，平台可按精确 commit 检查 suite 并运行固定 pytest 命令。
- 新建、覆盖、unchanged、恢复都绑定实际 Git commit；下一 turn 使用应用后的 commit。
- 覆盖、unchanged 与恢复在崩溃和 DB commit 回执丢失后仍只能凭精确 audit、admission、
  HEAD/index/Workspace 字节和 durable refs 进入终态；不明状态持续 fence runtime。
- hostile repository config/path 不能改写 Git 命令作用域、执行外部驱动、引入其他 Agent
  对象图、重写 raw commit parent/tree 或让 HEAD 借 activation ref 清理变更分支。
- 设置页只渲染一份业务 Agent 行，Workspace 测试状态和生命周期属于同一行；创建与覆盖导入模式
  不得混用目标身份或遗留上一次选择的文件、回执和错误。
- active turn、开放 change set、HEAD 竞争、畸形 tar、超限输入和部分失败明确失败且不暴露半成品。
- 删除普通 Agent 后重启不复活；已公开 ID 永久保留且不可重建，从未公开的 provisioning
  quarantine 只有在精确标记和整棵 layout 缺失同时成立时可重试；受保护 Agent 不可删除。
- 删除使用强 `If-Match` 和 `Idempotency-Key`；`202` 只表示 Agent 已下线且 durable cleanup 待完成，
  `Location`/GET 状态直到 `completed` 前都不得声称磁盘清理完成。
- `make runtime-bootstrap-scan`、专项 pytest、前端浏览器验收、`make main-flow-test`、
  `make codex-guard` 和真实 Compose 空卷/已有卷验收通过。
