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
| Workspace 普通文件按字节交换 | 平台改写会让上传包、tree digest 和 Git commit 不一致 | 身份文本渲染、endpoint renderer、权限覆盖 | 导出后跨 ID 导入，普通文件字节与 executable bit 一致 |
| 导入同步完成，下一 turn 生效 | 单个 Workspace 有明确资源上限；无需持久化第二套 operation 状态机 | 异步导入 job、导入历史表、多阶段激活状态 | 一次请求完整成功或完整失败；成功回执绑定 Git commit |

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
- 包内 ID、profile、name、status 或说明文字不成为平台身份事实；
- 空目录不进入 Git，不承诺导出后保留；
- conversation、SDK session、run、feedback、平台测试运行、Langfuse、数据库和 `claude-root` 不进入包。

`tests/` 与其他 Workspace 文件一样按字节导入、导出和版本化。导入缺少 `tests/` 或
`tests/README.md` 不拒绝包，但成功回执中的 `test_suite.diagnostics` 会给出 warning；没有
`tests/test_*.py` 的版本不能通过发布测试门禁。测试文件的详细契约见
[业务 Agent Workspace 原生 pytest 测试资产实现方案](./engineering/业务AgentWorkspace原生pytest测试资产实现方案.md)。

平台身份只来自目标路由 `agent_id` 和注册表。导出 `security-operations-expert` 后，可将该包作为
新 Agent 的修改起点；这不是“模板实例化”，也不会替换包内身份文本。调用方应在新 ID 上完成修改、
测试和回归，再决定是否覆盖目标 Agent。

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
| `expected_current_commit_sha` | 覆盖已有 Agent 时必填；执行 CAS |
| `reason` | 可选提交说明，不进入 Workspace |

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
  "test_suite_warnings": []
}
```

相同 tree 重试返回 `unchanged`，不制造空 commit。每次成功导入返回操作唯一的
`import_record_id`、测试套件状态、测试文件数和结构化 warning；完整测试清单通过
`GET /api/agent-registry/{agent_id}/test-suite?commit_sha=<sha>` 按精确提交查询。平台持久化同步
导入审计记录和 warning，但不建立异步 operation 状态机，也不复制测试内容。失败导入同样写入审计，
并保留原始结构化错误响应。

### 4.3 导出与恢复

```text
POST /api/agent-registry/{agent_id}/workspace/export
POST /api/agent-registry/{agent_id}/workspace/restore
```

导出返回当前 Git tree 的 `.tar.gz` 和 commit/package/tree digest headers。恢复使用
`target_commit_sha` 与 `expected_current_commit_sha`，把历史 tree 写成新 commit，不 hard reset 历史。

### 4.4 生命周期与删除

普通 Agent 可通过生命周期 API 管理，也可在线删除。删除清理该 Agent 的完整运行态根目录并写注册表
tombstone，响应只返回 `workspace_removed` 与 `cleanup_complete` 等实际结果；不再清理 catalog 或返回
`seed_removed`。受保护业务 Agent 删除返回业务规则错误。

## 5. 运行卷初始化

API 启动协调器读取 `docker/runtime-bootstrap/`：

1. 初始化必需运行目录和 governor Workspace；
2. 校验 `business-agents/` 的实际 ID 集合严格等于 `BUILTIN_BUSINESS_AGENT_IDS`；
3. 只在整个内置业务 Agent Workspace 不存在时复制；
4. 已存在 Workspace 整体跳过，不逐文件补缺、不覆盖、不产生隐式 commit；
5. 发现运行态所有合法 Workspace，并幂等同步到注册表；
6. 初始化各 Agent 的 Git 版本源，写入运行协调 receipt。

初始化源缺失、为空、含 symlink、内置集合多出或缺少任一 ID 时启动失败。`RUNTIME_BOOTSTRAP_HOST_DIR`
是 Compose 宿主机挂载入口，容器内路径为 `/app/docker/runtime-bootstrap`，必须只读。

普通 Agent 不放进初始化源。需要一个新的普通 Agent 时，导出已有 Agent 或在仓库外制作完整 Workspace
包，再走 import API。只有产品明确决定新增内置 Agent 时，才同时修改声明集合、初始化源、准入扫描、
文档和空卷验收。

## 6. Git、并发与热加载

新建复用 registry reservation、no-follow 文件发布、Git 初始化、finalize 和失败补偿 saga。覆盖与恢复：

1. 获取该 Agent 的维护栅栏；
2. 拒绝活跃 turn、未终结 change set 和 SDK session 失效冲突；
3. dirty Workspace 先形成包含普通文件的快照；
4. 在临时 worktree 形成候选 commit；
5. 校验 `expected_current_commit_sha` 后 CAS 激活；
6. 同一数据库事务清除 inactive SDK resume 映射；
7. 失败时补偿 Git、session mapping、注册表与自有文件。

当前 turn 的 HEAD、SDK mapping、active run 和 intent 在同一 admission 写屏障内绑定。导入成功后不重启
API；已有 API session ID 保留，新 turn 建立新的 SDK session 并读取回执中的 commit。

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

设置页只提供 Workspace 包入口：

- 新建时填写 Agent ID、name 并选择包；
- 既有 Agent 行提供导出、覆盖导入；
- 成功回执显示 action、previous/current commit、package/tree digest、测试状态、测试文件数和 warning；
- 覆盖后提供“恢复导入前版本”；
- 列表分别显示内置、默认、受保护和 HITL 观测；
- 不显示来源选择器、通用模板、seed 提示或直接创建表单。

失败态必须显示结构化错误代码和可执行动作，目标变化后清除旧文件与旧失败状态。

## 9. 验收

- OpenAPI、前端类型和 UI 中不存在旧直接创建、模板 catalog、`origin`、`template_id`、
  `source_seed_id`、`seed_removed`。
- 空运行卷只得到 governor 和 `security-operations-expert`；已有运行卷中的普通 Agent 保持原样。
- 导出内置 Agent 后以新 ID 导入，普通文件字节和 executable bit 一致，registry ID 为目标 ID。
- 导入身份只取 URL `agent_id`，不从包名或 `agent.yaml` 推断；缺少测试目录只告警。
- 所有业务 Agent Workspace 可携带 `tests/`，平台可按精确 commit 检查 suite 并运行固定 pytest 命令。
- 新建、覆盖、unchanged、恢复都绑定实际 Git commit；下一 turn 使用应用后的 commit。
- active turn、开放 change set、HEAD 竞争、恶意 tar、超限输入和部分失败明确失败且不暴露半成品。
- 删除普通 Agent 后重启不复活；重建同 ID 不继承旧 Workspace；受保护 Agent 不可删除。
- `make runtime-bootstrap-scan`、专项 pytest、前端浏览器验收、`make main-flow-test`、
  `make codex-guard` 和真实 Compose 空卷/已有卷验收通过。
