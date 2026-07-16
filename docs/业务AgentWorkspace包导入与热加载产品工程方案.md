# 业务 Agent Workspace 包导入与热加载产品工程方案

> 文档角色：产品能力目标方案，面向产品、架构、后端、前端、测试和运维共同评审。
>
> 实现状态：本文定义目标产品契约和实施边界，不表示当前 OpenAPI、设置页或运行时已经提供该能力。
> 当前可用接口仍以运行容器 `/openapi.json` 和 [AgentGov 集成指南](./AgentGov集成指南.md) 为准。
>
> 权威关系：业务 Agent、版本治理和资产术语服从
> [项目目标愿景使命](./项目目标愿景使命.md) 与
> [AgentGov 术语与版本边界](./AgentGov术语与版本边界.md)；本文不改变四阶段改进治理工作台主流程。

## 1. 决策摘要

AgentGov 增加业务 Agent workspace 包导入能力，用于把一个完整、可版本化的 Claude Code
项目配置接入 AgentGov，并复用既有 Runtime、Feedback Loop 和 Version Governance。

首版采用以下已确认决策：

- 导入对象是业务 Agent 的 `workspace/` 行为包，不是整个运行卷，也不是 `claude-root/`、`version/`
  或 runtime data 的备份包。
- `agent_id` 是唯一身份。同 ID 覆盖既有业务 Agent；同名但不同 ID 创建为独立业务 Agent。
- 覆盖适用于 `main-agent`、其他 seed-origin 业务 Agent 和用户创建 Agent；治理 Agent `governor`
  永远不属于该入口。
- 覆盖采用“立即激活”策略：包通过结构、安全和受管策略校验后即替换当前 workspace；测试不作为
  导入硬门，也不触发自动回滚。
- 覆盖前必须把当前受管 workspace 固化为 Git 提交；导入内容形成独立提交，并提供可审计的人工回滚。
- 热加载发生在 turn 边界。运行中的 turn 不变；下一 turn 重新从当前 workspace 加载 project settings、
  prompt、skills、subagents、rules、hooks、commands 和 MCP 配置。
- 既有会话保留 AgentGov conversation、SDK session 和 transcript；下一 turn 的语义是“旧会话事实 +
  新 workspace 配置”，不清空或伪造新会话。
- workspace 可以携带可选的 pytest 测试资产。平台服务端不执行上传的 Python；开发者在本地手工调用
  AgentGov API 验证已激活的精确 commit，并根据结果决定是否回滚。
- 首版面向持有平台 API Key 的可信内部开发者。安全解包、敏感信息扫描和受管策略仍是硬门，但包签名、
  发布者 RBAC、杀毒和第三方不可信代码沙箱属于后续产品化增强。

该能力是 AGV-004、AGV-031、AGV-036、AGV-042 和 AGV-044 的增强，不创建脱离现有治理闭环的
第二套 Agent、版本或测试体系。

## 2. 产品目标与非目标

### 2.1 产品目标

1. **低成本接入**：开发者能把已有 Claude Code workspace 作为完整行为包接入 AgentGov，不必在 UI
   中逐项重建 prompt、skill、subagent、hook 和 MCP 配置。
2. **稳定身份**：导入、运行、反馈、评估和版本始终归属同一个 `agent_id`，包内声明不能篡改平台身份。
3. **即时生效**：导入后无需重启 API 或重建容器，下一 turn 使用新 workspace。
4. **可恢复**：任何覆盖都有导入前 Git 锚点、导入提交、审计回执和受并发保护的人工回滚。
5. **安全失败**：非法压缩包、敏感文件、越界路径、不受管配置、活跃 turn 和版本候选冲突均明确失败，
   不留下半成品。
6. **可验证**：测试与被测 commit 精确绑定，测试失败不会被展示成导入失败，也不会被误记为平台评估通过。
7. **可演进**：首版的 API、领域对象、状态机和审计模型允许后续增加签名、权限、异步处理和平台化验证，
   不需要推翻导入主契约。

### 2.2 非目标

- 不导入或恢复 SDK transcript、conversation、run、feedback、EvalRun、release archive 或 Langfuse 数据。
- 不把 workspace 包当作 Docker volume 备份，不修改 `docker/runtime-volume-seeds/`。
- 不在 API 进程中执行包内 pytest、安装包内依赖或联网下载依赖。
- 不把本地 pytest 结果伪造为 AgentGov `TestDataset` / `EvalRun` 结果。
- 不做运行中途的文件热替换，不保证一个已经开始的 turn 切换配置。
- 不用导入入口自动发布、拒绝或放弃既有 change set。
- 不为首版引入通用插件市场、第三方分发市场或跨租户共享。

## 3. 治理对象与所有权

### 3.1 治理对象矩阵

| 维度 | 结论 |
| --- | --- |
| 被治理对象 | 一个注册表业务 Agent 的 Git 受管 workspace 行为包 |
| 治理执行者 | 后端确定性校验、Git 版本事务、Runtime 维护栅栏和人工回滚 |
| 不参与者 | `governor` 不解析、不生成、不批准导入包，也不输出 backend-owned 字段 |
| 资产类型 | prompt/skill/hook/MCP/test 属执行资产；commit 属版本资产；import receipt 属审计资产 |
| 生命周期 | 导入不新增第二套 Agent 生命周期；新 Agent 创建为 active，既有 Agent 保留原 lifecycle |
| 反馈归属 | 后续 run、feedback、EvalRun 和 release 继续按 `agent_id + agent_version_id` 归属 |
| 当前实现边界 | 已有模板创建、per-Agent Git、维护栅栏和下一 turn 项目配置发现；没有上传、导入回执和 workspace 测试入口 |
| 目标能力边界 | 安全上传、完整替换、下一 turn 激活、手工测试、审计和回滚 |

### 3.2 字段所有权矩阵

| 所有者 | 字段或内容 | 规则 |
| --- | --- | --- |
| 后端 | `import_id`、`agent_id`、registry status/origin、workspace 路径、package/tree digest、commit SHA、操作状态、时间戳、authenticated actor、审计结果 | 由鉴权、路由、注册表、Git 和运行态确定，不接受包内权威值 |
| 调用者 | `name`、`operator` 标签、`reason`、`Idempotency-Key`、上传文件 | 必须经鉴权、长度和格式校验；共享 API Key 模式下的 operator 只是调用者声明，不能替代授权身份 |
| workspace 包 | `CLAUDE.md`、`.claude/**`、`.mcp.json`、hooks、commands、skills、subagents、tests 等行为资产 | 只在 workspace 受管边界内生效 |
| Runtime | 当前 turn 的 `agent_version_id`、SDK session、run/session/trace 关联 | turn 准入后读取当前 commit，导入服务不得伪造 |
| HTTP 边界 | 导入响应、结构化错误、安全 findings、测试提示 | 只投影必要字段，不回显包内容、密钥或宿主私有路径 |

包内可选 `agent.yaml` 只是 workspace 描述文件。若包含 ID/name，导入器在 staging 中用后端选定值归一化；
包内 lifecycle、origin、测试通过声明和 commit 字段一律忽略或拒绝，不进入注册表。

### 3.3 闭环归属

```text
workspace package
  -> import operation / safety evidence
  -> business Agent + applied commit
  -> Runtime run / SDK session / trace
  -> feedback / attribution / optimization
  -> TestDataset / EvalRun / change set
  -> release / rollback
  -> Agent asset Registry
```

导入是业务 Agent 接入与版本变更入口，不替代后续反馈、评估和发布闭环。导入后的每次运行继续记录
精确 `agent_version_id`，从而能证明某条反馈和测试结果对应哪个导入版本。

## 4. 用户旅程与交互设计

### 4.1 新 Agent 导入

1. 用户进入“设置 → 业务 Agent → 导入 workspace 包”。
2. 选择 `.tar.gz`，填写 `agent_id`、名称、操作人和原因。
3. UI 明确显示“将创建新业务 Agent”，并展示包大小、测试目录是否存在和安全边界说明。
4. 用户提交导入。后端完成 staging、校验、隐藏 registry reservation、workspace 安装和初始 Git commit。
5. 全部完成后 Agent 才进入注册表可见状态，默认 lifecycle 为 active。
6. UI 刷新全局 Agent catalog，展示当前 commit、手工 pytest 命令和“尚未产生行为验证证据”。

### 4.2 同 ID 覆盖

1. 用户输入已经存在的 `agent_id`，UI 显示目标 Agent、origin、lifecycle、当前 commit 和覆盖警告。
2. UI 查询该 Agent 的未终结 change set。存在候选时阻止提交，列出 ID/status，并提供前往版本治理、拒绝、
   放弃等现有动作。平台不替用户自动处理候选。
3. 用户解决候选后重新提交。后端在真正替换前再次校验，消除 UI 查询与执行之间的竞态。
4. 后端提交当前 dirty 受管树，完整替换受管资产，再提交导入版本。
5. UI 展示 previous/applied commit、变更文件摘要、下一 turn 生效说明和人工回滚按钮。

### 4.3 手工验证与回滚

1. 用户在本地解开的源 workspace 中运行 `tests/remote` pytest。
2. pytest 使用导入回执中的 `agent_id` 和 applied commit 调用 `/v1/responses`。
3. 测试先断言响应的 `agent_version_id` 等于 applied commit，再验证业务输出、错误、工具活动和多轮行为。
4. 测试通过时，用户保留当前版本；首版不向平台回传或伪造“已通过”状态。
5. 测试失败时，用户查看 pytest 证据并主动点击回滚，填写操作人和原因。
6. 后端仅在当前 HEAD 仍等于该导入 commit 时恢复旧树；存在后续版本时返回冲突，引导用户进入版本治理处理。

### 4.4 动作、副作用与审计

| 用户动作 | 业务产物 | API 副作用 | 状态副作用 | 审计记录 |
| --- | --- | --- | --- | --- |
| 导入新 ID | 业务 Agent、初始 import commit、import receipt | 创建隐藏 reservation，安装并 finalize | 新 Agent 进入 active | digest、commit、actor/operator、reason、时间和校验摘要 |
| 覆盖同 ID | import commit、import receipt | 提交旧树并替换当前 workspace | 保留既有 Agent lifecycle/origin | previous/applied commit、变更摘要和冲突检查 |
| 运行 pytest | 本地测试报告 | 通过 `/v1/responses` 运行 Agent | 不修改 import 或 EvalRun 状态 | 平台保留正常 run/trace；pytest 报告由开发者持有 |
| 人工回滚 | tree 变化时生成 rollback commit，并更新 receipt | 恢复旧 Git tree 和可回滚的名称元数据 | Agent lifecycle 不变 | actor/operator、reason、被回滚 import、前后 commit |
| 处理候选 | 既有 change set/release 产物 | 调用现有发布、拒绝或放弃 API | 按既有状态机推进 | 复用 change set/release event |

## 5. Workspace 包契约

### 5.1 包格式

首版只接受 gzip 压缩的 tar 包，且必须恰好包含一个 `workspace/` 根目录：

```text
workspace/
├── CLAUDE.md
├── agent.yaml                         # 可选，身份字段由后端归一化
├── .mcp.json                          # 可选
├── .claude/
│   ├── settings.json
│   ├── agents/
│   ├── skills/
│   ├── rules/
│   └── commands/
├── hooks/                             # 可选
└── tests/
    ├── unit/                          # 可选，本地确定性测试
    └── remote/                        # 可选，调用 AgentGov API 的行为测试
        ├── conftest.py
        └── test_*.py
```

包不声明 `schema_version`。首版只有一种明确媒体类型和目录契约，没有多版本运行时或历史包迁移需求；
未来真正出现并行包协议时再引入版本协商，避免预先制造双轨 schema。

### 5.2 完整替换语义

完整替换针对“Git 受管树”，不是对 workspace 目录执行无差别删除：

- 导入前先把所有允许受管但尚未提交的文件纳入 pre-import snapshot。
- 导入包中缺失的旧 prompt、skill、subagent、rule、hook、command、MCP、test 和普通配置文件全部删除。
- `.git/`、本地私有覆盖、密钥、缓存和运行态工件不属于受管树，不被包覆盖，也不进入 snapshot。
- rollback 恢复受管树和允许回滚的 registry 名称，不修改本地私有文件、SDK 状态或运行数据。

必须集中定义并复用 protected/excluded policy，至少覆盖：

- `.git/`
- `.env` 及非 example 的 `.env.*`
- `secrets/`
- `CLAUDE.local.md`
- `.claude/settings.local.json`
- `.mcp.local.json`
- `.claude.json`
- `.cache/`、`.pytest_cache/`、`.mypy_cache/`、`.ruff_cache/`、`.venv/`、`__pycache__/`
- `dist/`、`node_modules/`、字节码和 runtime outputs

example 文件可以作为受管文档资产存在，但不得包含真实凭据、真实内网地址、本机路径或账号信息。若受保护文件
已经被 Git 跟踪，导入 fail closed，要求先完成安全处置，不能把它再次提交到 pre-import snapshot。

### 5.3 安全限额和内容规则

首版复用现有 runtime template 安全基线：

| 限制 | 值 |
| --- | --- |
| 压缩包大小 | 64 MiB |
| 单成员声明/解压大小 | 64 MiB |
| 总声明/解压大小 | 256 MiB |
| 成员数量 | 10,000 |
| 单路径字节数 | 4 KiB |
| 路径深度 | 32 |

安全解包必须拒绝：

- 绝对路径、`..`、空路径、NUL、目标越出 staging root。
- symlink、hardlink、device、FIFO、socket 和其他非普通文件/目录成员。
- 重复成员、目录/文件冲突、Unicode 或大小写归一化后冲突。
- 声明大小与实际解压大小不一致、压缩炸弹和超限输入。
- `.git`、runtime data、release/worktree、Claude root、数据库、日志和私有配置。
- 非 UTF-8 或首版 text workspace 契约不支持的二进制内容。
- API key、Bearer token、private key、密码、MCP header、本机私有路径和未参数化真实 endpoint。

文件权限仅保留普通读写和必要 executable 位，清除 setuid、setgid、sticky 等危险位。安全扫描采用 reject-only；
除 backend-owned 身份字段外，不静默修改调用者业务内容。

## 6. 热加载与会话语义

### 6.1 激活边界

Runtime 每个 turn 重新解析业务 Agent profile，并以目标 workspace 为 `cwd`、以 project settings 为配置来源。
因此目标契约不是“重启 Runtime”，而是：

| 场景 | 行为 |
| --- | --- |
| 导入前已经开始的 turn | 固定使用启动该 turn 时的 commit，不做中途替换 |
| 导入完成后的新会话 | 使用 applied commit 和新 workspace |
| 导入完成后的既有会话下一 turn | resume 原 SDK session，同时加载 applied commit 的 project 配置 |
| 导入期间到达的新 turn | 维护栅栏返回可重试 `409` |
| 导入时已有活跃 turn | 导入返回 `409`，不等待、不强杀运行 |

所有 workspace 切换类结果统一表达：

```json
{
  "activation_mode": "next_turn",
  "existing_session_action": "resume"
}
```

移除内部和公开投影中误导性的 `requires_runtime_restart=true`。不保留同义兼容字段，避免调用方同时维护
“需要重启”和“下一 turn 生效”两套相互冲突的判断。

### 6.2 版本事实

turn 准入、版本读取和 session run intent 必须属于同一个 fencing 语义：

1. Runtime 先声明本 Agent 的 turn admission。
2. admission 成功后读取当前 Git HEAD，写入 run 的 `agent_version_id`。
3. 导入维护租约只能在没有活跃 turn 时取得，并阻止新的 turn admission。
4. Git/DB 副作用完成并释放维护租约后，新 turn 才能读取新 HEAD。

这保证测试从 `/v1/responses` 看到的版本与实际 project discovery 使用的 workspace 一致。

## 7. 公开 API 设计

### 7.1 导入

`POST /api/agent-workspace-imports`

- 媒体类型：`multipart/form-data`
- 鉴权：沿用 `/api/*` Bearer API Key；首版定义为可信内部管理能力。
- Header：必填 `Idempotency-Key`，同 key + 同 package/agent 返回同一结果；同 key 不同输入返回 `409`。
- Form fields：
  - `package`：`.tar.gz` 文件。
  - `agent_id`：必填，平台唯一身份。
  - `name`：新 Agent 必填，覆盖时可选；提供时更新展示名。
  - `operator`：当前共享 API Key 模式下必填的操作者标签；审计 actor 仍由后端按认证凭据生成。
  - `reason`：必填操作原因。

审计记录同时保留 backend-owned actor 与调用者声明的 operator 标签。未来引入用户/RBAC 身份后，actor 直接来自
认证 principal，operator 标签可以退化为显示说明，不改变 import receipt 的主体归属。

成功响应使用 typed response model，核心字段为：

```json
{
  "import_id": "agi_...",
  "status": "completed",
  "action": "overwritten",
  "agent": {
    "agent_id": "customer-support",
    "name": "Customer Support",
    "status": "active",
    "origin": "user"
  },
  "package_sha256": "...",
  "tree_sha256": "...",
  "previous_commit_sha": "...",
  "applied_commit_sha": "...",
  "rollback_available": true,
  "activation_mode": "next_turn",
  "existing_session_action": "resume",
  "tests": {
    "remote_tests_present": true,
    "manual_command": "python -m pytest workspace/tests/remote",
    "expected_commit_sha": "..."
  },
  "warnings": []
}
```

`action` 的公开取值固定为：

- `created`：创建新 Agent 和初始 commit。
- `overwritten`：workspace tree 发生变化并形成 import commit。
- `metadata_updated`：workspace tree 相同，只更新允许的 registry 名称。
- `unchanged`：tree 和允许的 registry 元数据均无变化，不创建空 commit。

### 7.2 查询与回滚

- `GET /api/agent-workspace-imports?agent_id=<id>&limit=<n>`：按 Agent 查询导入历史。
- `GET /api/agent-workspace-imports/{import_id}`：查询一次操作的校验、Git 和回滚结果。
- `POST /api/agent-workspace-imports/{import_id}/rollback`：请求体包含 `operator`、`reason`、
  `expected_current_commit_sha`。

回滚约束：

- 仅 `completed + overwritten/metadata_updated` 且尚未回滚的操作可回滚。
- 当前 HEAD 必须等于 applied commit；否则返回 `409 IMPORT_HEAD_CHANGED`。
- `overwritten` 回滚创建新 commit 恢复 previous tree，不使用破坏历史的分支重写。
- `metadata_updated` 只恢复 previous name，不制造空 Git commit；仍校验当前 HEAD 与 receipt 记录一致。
- 重复同一回滚请求返回同一 receipt；不同回滚输入争抢时只有一个获胜。
- 新建 Agent 不通过 import rollback 删除，继续使用已有 Agent 删除/影响面流程。

### 7.3 冲突与错误投影

| HTTP | error code | 用户动作 |
| --- | --- | --- |
| 409 | `AGENT_TURN_ACTIVE` | 等当前 turn 完成后重试 |
| 409 | `AGENT_MAINTENANCE_ACTIVE` | 等维护完成后重试 |
| 409 | `UNRESOLVED_CHANGE_SETS` | 查看结构化 change set 列表并发布、拒绝或放弃 |
| 409 | `IMPORT_HEAD_CHANGED` | 刷新当前版本，进入版本治理决定恢复方式 |
| 409 | `AGENT_NOT_IMPORTABLE` | 处理 archived/tombstone/incomplete provisioning 状态 |
| 413 | `PACKAGE_TOO_LARGE` | 缩小包并移除构建/运行工件 |
| 415 | `UNSUPPORTED_PACKAGE_MEDIA_TYPE` | 使用 `.tar.gz` workspace 包 |
| 422 | `PACKAGE_STRUCTURE_INVALID` | 修复根目录、成员或编码 |
| 422 | `PACKAGE_SECURITY_REJECTED` | 根据脱敏 findings 删除敏感/越界内容 |
| 422 | `WORKSPACE_POLICY_REJECTED` | 修复 Claude Code project 配置或受管权限策略 |

错误响应必须包含 `detail`、`error_code`、`import_id`（已建立操作记录时）、可执行 `action` 和经过脱敏的
findings。不得回显文件正文、token、header、宿主绝对路径或完整内部异常栈。

### 7.4 相关公开契约收口

- `GET /api/agent-change-sets` 增加 `agent_id` 过滤，UI 和导入服务使用同一终态集合判断冲突。
- 手工创建 change set 的请求显式携带 `agent_id`，删除隐式落到 `main-agent` 的新调用路径。
- 导入、publish、restore 和 rollback 统一使用下一 turn 激活字段。
- OpenAPI 是 response schema 的单一真相源，前端类型从 OpenAPI 生成，不手写第二套导入 DTO。
- 首版不新增输出 `schema_version`；SQLite row、领域 record 和 HTTP response 分开建模，不因字段相似互相继承。

## 8. 领域模型与状态机

### 8.1 WorkspaceImportOperation

持久化操作至少包含：

- 身份：`import_id`、`idempotency_key`、`agent_id`、`action`。
- 调用证据：authenticated actor、调用者声明的 operator 标签、`reason`、脱敏后的源文件名。
- 内容证据：`package_sha256`、`tree_sha256`、受管文件数、测试目录存在性。
- Git 证据：`previous_commit_sha`、`applied_commit_sha`、`rollback_commit_sha`。
- registry 证据：previous/requested name、origin/lifecycle 快照。
- 协调证据：claim token/generation/expiry、当前 status、失败 code/detail。
- 时间：created/updated/completed/rolled_back 时间。

不持久化上传包正文、文件正文、API key、MCP header 或测试输出全文。校验摘要只保留 finding 类型、严重度、
相对路径和可公开说明。

### 8.2 状态机

合法状态和完整转移表集中在项目状态机模块；所有写入通过统一 transition helper：

```text
received -> validating -> ready -> applying -> git_applied -> completed
    |           |          |          |
    +-----------+----------+----------+-> failed

completed -> rollback_reserved -> rollback_git_applied -> rolled_back
                         |
                         +-> rollback_failed
```

完整转移如下：

| 当前状态 | 允许的下一状态 |
| --- | --- |
| `received` | `validating`、`failed` |
| `validating` | `ready`、`failed` |
| `ready` | `applying`、`failed` |
| `applying` | `git_applied`、`failed` |
| `git_applied` | `completed` |
| `completed` | `rollback_reserved`，且仅限 action 支持回滚、HEAD 仍匹配时 |
| `failed` | `validating`，且仅限相同输入、可证明没有 Git/registry 副作用时 |
| `rollback_reserved` | `rollback_git_applied`、`rollback_failed` |
| `rollback_git_applied` | `rolled_back` |
| `rollback_failed` | `rollback_reserved`，且仅限重新验证 CAS 和无未对账副作用时 |
| `rolled_back` | 无 |

约束：

- `failed` 可在同一个 `Idempotency-Key`、相同输入和可证明无副作用时重新进入 validating；否则新建 import。
- `git_applied` 和 `rollback_git_applied` 不允许转入通用失败态；Git 副作用已经存在时必须保持可恢复状态，
  直至 DB/registry 对账完成。
- `completed` 是可查询的稳定导入结果，`rolled_back` 是终态；`rollback_failed` 只有在重新证明当前 HEAD 和
  副作用边界后才允许重试。
- crash 后通过 operation row + Git HEAD/commit metadata + registry reservation 对账，不根据临时目录猜测成功。
- 非法转移、重复竞争、过期 claim 和副作用部分完成均有负向测试。

### 8.3 三层模型边界

| 边界 | 模型职责 |
| --- | --- |
| DB row | 表达完整持久化列、claim 和恢复所需不变量 |
| Domain record | 表达导入服务内部的 typed operation、package plan、validation result、Git result |
| API response | 只公开调用者需要的状态、证据、warnings 和安全错误 |

内部主流程不传递裸 `dict` / `JsonObject`。JSON 只在 SQLite JSON 列、HTTP、日志和观测边界生成。

## 9. 架构设计

### 9.1 组件边界

```text
Agent Workspace Import Router
        |
        v
AgentWorkspaceImportService -------------- WorkspaceImportStore
        |                                         |
        +--> WorkspacePackageReader               +--> SQLite operation/audit
        |        |
        |        +--> archive limits / safe extraction
        |
        +--> WorkspaceImportPolicy
        |        |
        |        +--> secrets / path / managed policy / typed findings
        |
        +--> AgentVersionMaintenanceCoordinator
        |
        +--> WorkspaceImportGitTransaction ------ per-Agent Git repository
        |
        +--> AgentRegistryStore ----------------- hidden create/finalize or metadata update
```

职责规则：

- Router 只处理鉴权、multipart、typed request/response 和领域错误映射。
- `WorkspacePackageReader` 是纯输入边界，产出不可变 `WorkspacePackagePlan`，不操作 registry 或 Git。
- `WorkspaceImportPolicy` 统一包安全、受保护路径和最终 workspace 策略，scripts 只作为薄 CLI 包装。
- Application service 编排 DB/FS/Git saga，不持有 tar 解析和 Git 命令细节。
- Git transaction 只操作当前 Agent 的受管 tree，要求显式 expected HEAD，并返回 typed result。
- Import store 负责状态转移、幂等键和恢复查询，不执行文件系统副作用。
- Runtime 继续通过 registry/profile resolver 和 Claude SDK project discovery 加载配置，不引入配置缓存或第二套 loader。

### 9.2 架构阈值处理

当前相关区域已经接近项目阈值：

- `app/runtime/agent_git_store.py` 约 760 行。
- `app/runtime/business_agent_workspace.py` 约 700 行。
- `app/runtime/claude_runtime.py` 约 740 行。
- `frontend/src/App.tsx` 约 800 行。

实现前先进行职责拆分：

- 从 Git store 抽出通用 Git command/working-tree 事务基础设施，使 import transaction 不访问私有方法，也不把原文件推过 800 行。
- 从现有 scripts 抽出安全 tar 和模板内容扫描公共库；restore/export 脚本改为调用公共库，不复制第二套限额和正则。
- 导入路由、应用服务、持久化 store、schema 和状态机分模块，不塞入 `agents.py` 或中心治理 service。
- 设置页使用独立 `AgentWorkspaceImportPanel` 和 hook；`App.tsx` 只复用既有 catalog refresh，不增加业务流程。
- 同一 protected path、终态 change set、operation status 和 error code 只在集中常量/状态机定义一次。

### 9.3 一致性与补偿

#### 新 Agent

```text
persist received operation
-> safe staging and validation
-> invisible registry reservation
-> materialize owned workspace
-> initialize Git and initial import commit
-> validate final bytes
-> finalize registry
-> complete operation
```

失败时只删除本次 operation 证明拥有的 staging/workspace，补偿 reservation；不删除预存目录、不误伤其他 Agent。

#### 覆盖 Agent

```text
safe staging and validation
-> acquire per-Agent maintenance lease
-> recheck active turn / unresolved change set / expected HEAD
-> scan current dirty tree for protected content
-> create pre-import snapshot when dirty
-> replace Git-managed tree
-> validate final bytes
-> commit import
-> persist git_applied
-> update registry metadata and complete
```

如果 import commit 之前失败，hard reset 到 pre-import commit 并保留私有 excluded 文件；如果 Git 已提交但 DB
尚未完成，由恢复器根据 import ID/commit/HEAD 完成对账，不能盲目回退一个已经成功激活的版本。

维护租约和 Git 仓库跨进程锁共同保证同 Agent 串行；不同 Agent 可以并行导入。事务块内不执行 `rmtree`、
远程调用或其他不可回滚副作用。

### 9.4 删除、迁移与保留清单

**删除**

- workspace 切换结果中的 `requires_runtime_restart` 旧语义。
- scripts 与应用层重复的 tar 安全限额、敏感文件名单和扫描实现。
- 新 change set 调用对隐式 `main-agent` 的依赖。

**迁移**

- 现有 Git `info/exclude` 写入逻辑改为每次幂等合并缺失规则，而不是见到 marker 后停止更新。
- publish/restore/rollback、OpenAPI 和前端生成类型迁移到统一 activation 字段。
- `GET /api/agent-change-sets` 调用方迁移为显式 Agent 过滤。
- 实现完成后，把当前事实和使用旅程合并进 README、集成指南及核心功能测试用例。

**保留**

- 现有模板创建 API：适合从平台 catalog 创建，不与完整 workspace 导入重复。
- 现有 release/change set 版本治理：导入冲突处理和后续治理继续复用。
- 现有 Agent delete/lifecycle：导入不复制删除、归档或激活状态机。
- SDK session transcript：继续作为会话事实唯一来源。
- runtime seeds：继续只承担出生配置和离线预置，不成为在线导入目标。

## 10. Workspace 测试契约

### 10.1 两类测试资产

| 类型 | 位置 | 目的 | 平台行为 |
| --- | --- | --- | --- |
| 确定性单元测试 | `workspace/tests/unit` | 配置解析、hook/helper、纯函数和固定输入输出 | 随 workspace 版本化；服务端不执行 |
| 远程行为测试 | `workspace/tests/remote` | 调用已激活 Agent，验证响应、工具、错误和多轮行为 | 随 workspace 版本化；开发者本地手工执行 |

平台 `TestDataset` / `EvalRun` 继续承担候选版本回归和发布门禁。workspace pytest 是接入者自测，二者在
对象、执行者、证据可信度和发布副作用上保持分离。

### 10.2 pytest client 环境

标准 fixture 只从环境变量读取连接信息：

```text
AGENTGOV_BASE_URL
AGENTGOV_API_KEY
AGENTGOV_AGENT_ID
AGENTGOV_EXPECTED_COMMIT
```

测试规则：

- API key 不写入测试文件、pytest 参数、JUnit property、截图或提交记录。
- 首个 fixture 必须验证当前 Agent 存在、可运行，并确认 response `agent_version_id` 等于 expected commit。
- 新会话、多轮续聊、工具活动和错误投影分别使用公开 `/v1/responses` / conversation 契约，不调用内部 Chat 接口。
- 断言以可观察行为为主，不依赖私有函数、模型措辞完全相等或不可控时间。
- 测试缺失只产生 `REMOTE_TESTS_MISSING` warning；导入成功状态不得显示“测试已通过”。
- pytest 失败不调用 rollback API。回滚必须是用户明确动作。

### 10.3 产品化演进

后续可在不改变 import operation 的前提下增加独立 `WorkspaceVerificationRecord`，接收签名的 JUnit/远程执行
证据并绑定 `agent_id + applied_commit_sha + package_sha256`。只有该能力落地后，平台 UI 才能展示“已验证”
或将验证作为策略门；首版不预留虚假的 passed 字段。

## 11. 设置页设计

导入入口位于现有“设置 → 业务 Agent”，不新增一级导航，不把开发调试前端升级成生产业务门户。

### 11.1 表单

- Workspace 包：仅选择 `.tar.gz`，显示文件名和大小。
- Agent ID：必填；输入时查询 registry 和未终结 change set。
- 名称：新 Agent 必填，覆盖时默认沿用并允许修改。
- 操作人和原因：必填，进入审计记录。
- 主按钮：新 ID 显示“导入并创建”，同 ID 显示“确认覆盖”。

### 11.2 状态展示

- 上传/校验中：显示单一业务动作进度，允许取消尚未进入 Git 副作用的请求。
- 冲突：显示 Agent、active turn 或 change set 的具体原因和可执行下一步。
- 安全失败：按相对路径和 finding 类型展示脱敏摘要，不展示文件正文。
- 成功：显示 action、previous/applied commit、变更计数、下一 turn 生效和测试提示。
- 历史：按 Agent 显示最近 import receipt、operator、reason、commit 和 rollback 状态。
- 回滚：只有 receipt 明确可回滚且当前 HEAD 未变化时启用；点击后再次确认并填写原因。

UI 不提供“测试通过”开关，不允许用户手工把本地测试结果改成平台可信状态。

### 11.3 可用性与无障碍

- 文件选择、冲突、错误和成功信息同时具备文本、图标和 ARIA live 反馈，不只依赖颜色。
- 长文件名和 digest 可复制，默认截断展示但保留完整 accessible label。
- 上传请求使用独立超时和 AbortController；取消后后端依赖 idempotency/operation 状态收口，不假设连接断开等于撤销。
- Agent catalog 在成功和回滚后统一刷新，避免设置页、Topbar 和 Playground 选择器漂移。

## 12. 安全与信任边界

### 12.1 首版信任模型

首版只允许已经获得 AgentGov API Key 的可信内部开发者导入。该假设降低的是发布者身份和恶意代码执行隔离
范围，不降低输入安全、敏感信息、路径边界、受管权限和审计要求。

导入包中的 prompt、hook、skill 和 MCP 配置会影响 Agent 后续行为，因此：

- API Key 必须通过 Bearer header 传递，不能进入包、reason、日志或截图。
- 所有 hook、MCP、settings 和 sandbox 配置仍需通过现有 managed workspace policy。
- 包不得声明新的模型凭据、MCP header 或宿主路径；运行环境通过既有 env/volume 边界提供。
- findings、日志和 metrics 只记录类型、大小、耗时、digest 和相对路径，不记录 prompt 或文件正文。
- 导入/回滚均记录 backend-owned actor、operator 标签、reason、agent、commit 和时间，满足审计与追责。

### 12.2 后续生产增强

在对第三方或多租户开放前增加：

- 发布者身份、细粒度 import/overwrite/rollback 权限和高风险 Agent 额外确认。
- 包签名、签名者信任策略、不可抵赖 provenance 和可选制品存储。
- 恶意内容/依赖扫描、隔离的验证执行器和网络/CPU/内存/PID/文件配额。
- 租户隔离、配额、审计导出、保留策略和大包异步处理。
- 策略化验证门：按 Agent 风险等级决定立即激活、先验证后激活或双人确认。

这些增强复用 `WorkspaceImportOperation`、digest、状态机和 receipt，不改变“workspace 包 → commit → next turn”
核心模型。

## 13. 可观测性与运营指标

### 13.1 结构化日志

记录：`import_id`、`agent_id`、backend-owned actor、operator 标签、action、status、package/tree digest 前缀、
文件数、字节数、耗时、previous/applied commit、error code、claim generation。

禁止记录：压缩包正文、文件正文、prompt、token、MCP header、私有 endpoint、宿主绝对路径和 pytest 输出全文。

### 13.2 Metrics

- import 请求数与 `created/overwritten/unchanged/failed` 分布。
- validation、active turn、change set、maintenance、CAS 冲突次数。
- package 大小、文件数、staging/validation/apply 总耗时分位数。
- rollback 次数、rollback 率和 HEAD changed 冲突率。
- `remote_tests_present` 比例；不把存在测试误计为测试通过。
- startup reconciliation 处理数和未能自动收口的 operation 数。

### 13.3 告警

- 持续出现 `git_applied` 未完成 operation。
- rollback_failed 或 maintenance claim 频繁丢失。
- 同 Agent 高频覆盖/回滚。
- 安全拒绝率异常上升或同一 idempotency key 输入漂移。
- staging 临时目录或操作记录超过保留期限未清理。

## 14. 测试同步矩阵

### 14.1 后端单元与安全测试

- tar 限额、路径穿越、绝对路径、symlink/hardlink/device/FIFO、重复/大小写冲突、压缩炸弹。
- 非 UTF-8、二进制、`.git`、runtime data、私有文件、密钥、endpoint、本机路径和危险 mode。
- protected/excluded policy、example 例外、tracked private file fail closed。
- `agent.yaml` backend-owned 字段归一化和 hostile identity/status/test result 污染。
- operation 状态集合、完整转移表、每条非法转移。

### 14.2 应用服务与持久化测试

- 新 Agent hidden reservation、成功 finalize、每个副作用点失败后的补偿。
- active/main/seed/user Agent 覆盖，name 更新与 lifecycle/origin 保留。
- 同名不同 ID、新 ID、governor、archived、tombstone 和 incomplete provision 边界。
- dirty workspace 先提交，旧受管文件完整删除，私有 excluded 文件保留。
- package/tree 相同的 no-op、metadata-only 更新、同 idempotency key 重试和输入漂移。
- crash 位于 Git 前、Git 后/DB 前、registry finalize 前、rollback Git 后的启动恢复。
- migration 在真实历史 SQLite 上读取旧数据，导入列表和详情不出现 500。

### 14.3 并发与版本测试

- 活跃 turn 与导入竞争，双方各自先取得 lease 的结果均确定。
- 同 Agent 两次导入、导入与 publish、导入与 restore/rollback、导入与 change set 创建竞争。
- 不同 Agent 并行导入互不阻塞。
- unresolved change set 在 UI 查询后新建时，后端最终校验仍阻断。
- rollback CAS、重复 rollback、后续 HEAD 已变化、metadata-only rollback。
- 导入/回滚 commit 历史可追溯，不删除旧 import commit。

### 14.4 API/OpenAPI 测试

- multipart、鉴权、`Idempotency-Key`、201/200/4xx 和结构化错误。
- response model、OpenAPI 导出和前端生成类型完全一致。
- `agent_id` change set filter 与手工创建显式 Agent 归属。
- publish/restore/rollback 不再暴露旧 restart 语义。
- findings 脱敏、API key/宿主路径不回显。

### 14.5 Runtime 和 pytest 契约测试

- 新会话下一 turn 使用 applied commit。
- 既有会话保持 SDK session ID，并在下一 turn 使用新 project 配置。
- in-flight turn 保持旧 commit，导入不改变该 run 的版本。
- `/v1/responses` 返回的 `agent_version_id` 与实际 workspace HEAD 一致。
- canonical pytest fixture 使用公开 Responses API、校验 expected commit、不会自动回滚。
- 缺少 tests 仅 warning；测试存在不等于 passed。

### 14.6 前端与真实容器测试

- 新建、覆盖、no-op、无测试、结构失败、安全失败和网络失败状态。
- unresolved change set 提示与发布/拒绝/放弃入口。
- active turn、maintenance、HEAD changed 冲突和重试。
- import history、复制 digest/命令、人工 rollback 和 catalog 刷新。
- 空态、成功态、失败详情均有浏览器证据，console error 和 failed request 为零或有明确预期。
- 使用真实 Compose API/UI 和隔离 `${HOST_RUNTIME_VOLUME_ROOT}` 验证 next-turn、既有 session、Git 历史、
  回滚及容器重启后 receipt 恢复；local-debug 结果不能替代容器验收。

### 14.7 质量策略与命令

- 将导入场景绑定到 `tests/quality_policy.json` 的 Agent 生命周期主流程、安全 lane 和前端契约 lane。
- 内循环：目标 pytest、OpenAPI 类型生成、前端 typecheck/build。
- 主流程：`make main-flow-test`，并运行真实容器 import/next-turn/rollback smoke。
- 治理硬门：`make codex-guard`。
- 提交、CI 或发版：`make test`；安全专项按质量策略运行 coverage/mutation。

## 15. 验收标准

### 15.1 功能验收

- 新 ID 导入后只出现一个稳定业务 Agent，且运行、反馈和版本均使用该 ID。
- 同 ID 覆盖后旧受管资产被完整替换，current HEAD 是独立 import commit。
- dirty workspace 内容先固化，任何失败都能回到可证明的 pre-import 状态。
- 无需重启 API；新会话及既有会话下一 turn 使用 applied commit。
- 手工 pytest 能证明测试请求命中了 applied commit；平台不虚报测试通过。
- 人工 rollback 在无后续 HEAD 变化时恢复旧行为包并留下新审计 commit。

### 15.2 负向验收

- 不能导入 `governor`、archived/tombstone Agent、含未终结 change set 的 Agent 或有活跃 turn 的 Agent。
- 不能把 `.git`、密钥、私有路径、runtime data、SDK state 或 release archive 带入包。
- 不能在 API 服务端执行包内 Python、安装依赖或自动联网。
- 不能在 turn 中途切换 workspace，也不能把旧 session 静默变成新 session。
- 不能因导入请求断线留下可见半成品、无归属目录或无法解释的 Git HEAD。
- 不能在后续版本已经产生时用旧 import receipt 强制覆盖当前 HEAD。
- 不能让 UI、日志、OpenAPI 或导入回执泄露 API key、MCP header、包正文或宿主私有路径。

### 15.3 架构验收

- 新模块、类、函数和路由不触发项目行数、方法数、复杂度和路由数阈值。
- 没有复制 tar 安全逻辑、protected path、change set 终态或 operation 状态字符串。
- DB row、domain record、API response 和前端生成类型各有单一真相源，无手写 schema 双轨。
- DB + FS + Git 更新具备 idempotency、maintenance fencing、补偿和启动恢复。
- 旧 restart 字段和隐式 main change set 路径完成迁移，不留活跃兼容 shim。
- README、集成指南、核心测试用例、OpenAPI、前端类型和质量策略在实现阶段同步。

## 16. 分阶段交付

### 阶段 A：安全与领域基础

- 抽取安全 archive/content policy 公共库并让现有 scripts 复用。
- 集中 protected/excluded policy、change set 终态和 activation contract。
- 建立 typed import models、DB migration、完整状态机、idempotency 和恢复器。
- 先拆 Git store 边界，提供 expected-HEAD 的受管 tree 事务能力。

退出标准：纯 parser/policy、状态机、migration、Git transaction 和 hostile 输入测试通过，架构硬门无新增债。

### 阶段 B：导入、热加载与回滚 API

- 实现新建/覆盖 saga、维护栅栏、pre-import snapshot、完整替换、receipt 和人工 rollback。
- 收口 change set Agent 过滤与显式归属。
- 统一 workspace 切换 activation 响应并生成 OpenAPI。

退出标准：目标后端测试、并发/恢复测试、API 契约和 next-turn Runtime 测试通过。

### 阶段 C：设置页与 workspace pytest

- 增加独立导入面板、冲突处理、历史、命令复制和回滚交互。
- 在集成指南提供 canonical pytest client 和包结构说明。
- 增加前端契约、浏览器和真实容器 smoke。

退出标准：空态、成功态、失败详情、既有 session 热加载和人工 rollback 均有容器证据。

### 阶段 D：产品化收口

- 更新 README、集成指南、核心功能测试用例、质量策略和运行手册。
- 用真实历史 SQLite/运行卷验证 migration、列表、详情、重启恢复和审计。
- 完成 `make main-flow-test`、`make codex-guard`、`make test` 和阶段末架构审计。

退出标准：文档从“目标方案”更新为可追溯的已实现契约入口，OpenAPI 与运行容器一致，未实现增强仍明确留在
产品演进边界而不伪装为当前能力。

## 17. 运行环境、数据与发布边界

- 容器模式继续使用 `docker/.env` 和宿主 `${HOME}/volume-agent-gov`；本机调试使用
  `docker/.env.local-debug` 和 `/tmp/local-debug-volume-agent-gov`，二者是按运行环境选择，不是覆盖关系。
- 不增加新的 env 选择规则或 Docker volume。staging 和 operation 数据位于现有 data root 的受控临时/SQLite 边界。
- 在线导入只修改 `data/business-agents/<agent_id>/workspace` 和对应审计数据；不修改
  `docker/runtime-volume-seeds/`、`claude-root/`、`version/` 目录布局或 Langfuse 数据。
- 包和测试不得携带 `MODEL_PROVIDER_API_KEY`、MCP header 或其他私有 env；真实容器测试从私有运行环境注入。
- DB migration 为 additive；上线前先备份 runtime SQLite，启动恢复器处理已知 operation 状态后 API 才 ready。
- 本方案本身不 bump `VERSION`、不创建 tag。功能实现完成、真实验证通过并由用户确认发布点后再走版本发布流程。

## 18. 文档治理与实现完成后的同步

本文是独立目标方案，因为该能力跨产品旅程、包协议、Runtime、Git、SQLite、UI、安全与测试，无法仅靠集成指南
中的一节安全承载。它不取代现有文档：

| 文档 | 当前动作 | 实现完成后的动作 |
| --- | --- | --- |
| `docs/项目目标愿景使命.md` | keep | 仅在长期产品边界变化时更新；本能力已经被“创建/配置/版本治理”目标覆盖 |
| `docs/AgentGov术语与版本边界.md` | keep | 不新增与既有业务 Agent、版本、测试冲突的术语 |
| `docs/AgentGov集成指南.md` | no-op | 实现完成后合并包契约、API 旅程、错误和 pytest 使用说明 |
| `docs/AgentGov核心功能测试用例.md` | no-op | 实现完成后增强 AGV-004/031/036/042/044 证据和状态 |
| `docs/README.md` | update | 增加本目标方案入口并明确尚未实现边界 |
| `docs/archive/README.md` | no-op | 当前没有被本文替代且需要归档的旧文档 |

实现完成前，任何 README、UI 或集成说明都不能把 workspace 包导入写成当前已支持能力；实现完成后以 OpenAPI、
自动测试和真实容器证据为准同步当前事实，而不是复制本文中的计划性描述。
