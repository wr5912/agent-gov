# 业务 Agent Workspace 包导入与热加载产品工程方案

> 文档状态：当前产品工程契约。公开字段仍以 OpenAPI 为单一真相源。
>
> 本文依据
> [AgentGov 工程宪法 2.1、2.6、2.7](./engineering/CI-CD宪法与交付链两阶段整改计划.md)
> 定义“原样导入、Git 版本化、下一 turn 生效、可恢复”的最小闭环。复杂安全旧稿见
> [归档](./archive/design/业务AgentWorkspace包导入与热加载产品工程方案_复杂安全旧稿.md)。

## 1. 裁决与理由

| 裁决 | 最小事实依据 | 本期不做 | 验证与退出条件 |
| --- | --- | --- | --- |
| workspace 普通文件原样导入 | live workspace 才是 Agent 实际行为资产，平台改写会让上传包与 Git commit 不一致 | endpoint 脱敏、身份字段改写、内容扫描 | 导出后导入，tree digest 与文件字节一致 |
| 导入同步完成 | 单个业务 Agent workspace 规模有明确资源上限，无需引入 job/operation 状态机 | 异步队列、导入历史 API、十一状态状态机 | API 在一次请求内完成或完整失败 |
| per-Agent Git 是版本事实 | 当前运行、反馈和评估已经绑定 `agent_version_id` | 第二套 import active-version 字段 | 成功响应返回实际 Git commit，下一 turn 读取同一 commit |
| 首版只做基础输入保护 | 当前调用者是持有 API Key 的内部开发者，核心风险是半成品和文件系统越界 | 包签名、发布者 RBAC、杀毒、第三方代码沙箱 | 公网、多租户、客户敏感数据、合规或真实攻击发生时重审 |

导入不恢复 conversation、SDK session、run、feedback、EvalRun、Langfuse、数据库或
`claude-root`。已有 API session id 保留；成功激活前按 Agent 批量清除其 inactive
`sdk_session_id` 映射，使下一 turn 建立新的 SDK session 并读取新的 workspace commit。

## 2. Workspace 包

媒体类型固定为 `.tar.gz`，解压后必须恰好包含一个顶层目录：

```text
workspace/
  CLAUDE.md
  .mcp.json
  .claude/
  hooks/
  commands/
  tests/
  ...任意其他普通文件
```

`workspace/` 内普通文件由包所有者负责，平台逐字节保留：

- 允许文本和二进制；
- 允许 `.env`、真实 endpoint、本机路径、MCP header 和凭据形态内容；
- 不修改 `agent.yaml`、`CLAUDE.md`、settings、MCP、hook、skill 或 subagent；
- 包内 ID/name/status/origin 不成为平台身份事实。
- 空目录不进入 per-Agent Git，也不承诺在导出后保留。

平台身份只来自路由和 registry。新建 Agent 的 name 来自请求字段；覆盖既有 Agent 时 registry
name、origin 和 lifecycle 不变。

这里的“原样”止于 live workspace 与其 per-Agent Git 边界，不代表上传包可原样提交到
`docker/runtime-volume-seeds/`。repo generic template 与声明 seed/builtin 是源码仓库资产，
仍须满足“不提交真实密钥、私有 header、数据库凭据和本机私有路径”的仓库边界。导入包内权限
配置也原样保留，不因 generic template 采用保守权限而被平台改写。

## 3. 基础输入保护

首版保留会直接造成未授权访问、资源耗尽或文件系统破坏的低成本保护：

- `/api/*` Bearer API Key；
- 压缩包最大 64 MiB；
- 单成员最大 64 MiB；
- 解压总量最大 256 MiB；
- 最多 10,000 个成员；
- 单个 PAX/GNU tar 元数据记录最大 64 KiB，并在 `tarfile` 解析前流式预检；
- 单路径最大 4 KiB，路径深度最大 32；
- 拒绝绝对路径、`..`、NUL、非 UTF-8 路径、重复项以及文件/目录前缀冲突；
- 拒绝 symlink、hardlink、device、FIFO、socket；
- 拒绝任何 `.git` 成员；
- 已存在的 `.mcp.json`、`.claude/settings.json` 必须是合法 JSON；
- 错误、日志和回执不得回显包正文。

不执行上传包中的 Python、测试、安装脚本或网络请求。包内测试只作为开发者本地资产。

## 4. 公开 API

### 4.1 导入

```text
POST /api/agent-registry/{agent_id}/workspace/import
Content-Type: multipart/form-data
```

字段：

| 字段 | 规则 |
| --- | --- |
| `package` | 必填 `.tar.gz` |
| `name` | 目标 Agent 不存在时必填；已存在时不修改 name |
| `expected_current_commit_sha` | 覆盖已有 Agent 时必填；用于 CAS |
| `reason` | 可选说明，不进入 workspace |

成功响应：

```json
{
  "action": "created",
  "agent": {
    "agent_id": "customer-support",
    "name": "Customer Support",
    "status": "active",
    "origin": "user"
  },
  "previous_commit_sha": null,
  "current_commit_sha": "40-character-sha",
  "package_sha256": "sha256",
  "tree_sha256": "sha256",
  "rollback_target_commit_sha": null,
  "activation_mode": "next_turn"
}
```

`action` 只有：

- `created`：新建 registry Agent、workspace 和初始 Git commit；
- `overwritten`：既有 tree 被新 commit 替换；
- `unchanged`：导入 tree 与当前 tree 相同，不制造空 commit。

不新增 `import_id`、operation 查询、幂等表或持久化导入状态。相同 tree 重试自然返回
`unchanged`。

### 4.2 恢复

```text
POST /api/agent-registry/{agent_id}/workspace/restore
```

请求：

```json
{
  "target_commit_sha": "导入前或其他历史 commit",
  "expected_current_commit_sha": "当前 commit",
  "reason": "restore previous workspace"
}
```

恢复把目标 tree 写成新的 Git commit，不 hard reset 历史。响应返回 previous、target 和新的
current commit，激活方式仍是 `next_turn`。

## 5. Git 与并发事务

覆盖既有 Agent：

1. 取得该 Agent 的维护栅栏；栅栏已被其他维护操作占用时返回
   `409 WORKSPACE_MAINTENANCE_CONFLICT`。
2. 有活跃 turn，或激活前 SDK session 失效发生冲突时返回
   `409 WORKSPACE_SESSION_INVALIDATION_CONFLICT`。
3. 有未终结 change set 时返回 `409 WORKSPACE_CHANGE_SET_ACTIVE`。
4. dirty workspace 先生成包操作快照；快照强制纳入普通文件，包括被 `.gitignore` 忽略的
   workspace 自有文件。
5. 在临时 Git worktree 中清空旧业务文件、复制 staging tree 并提交。
6. 以 `expected_current_commit_sha` 校验主 HEAD。
7. CAS 成功后把候选 commit 应用到主 workspace；失败时主 workspace 不变。
8. 清理临时 worktree，释放维护栅栏。

新建 Agent 复用 registry reservation、文件落盘、Git 初始化和失败补偿 saga。任一步失败都不能留下
可见 registry 行、无归属目录或半个 Git 仓库。

## 6. 热加载

- 当前 turn 的 Git HEAD 解析、SDK mapping 选择、active run 与 intent 创建必须处于同一个
  SQLite admission 写屏障；因此它绑定的 `agent_version_id` 与实际执行 workspace 线性一致，
  不能在维护切换前读旧 HEAD、切换后再登记 turn。
- 导入与恢复只在没有活跃 turn 时应用。
- 候选 commit 激活前，必须在同一数据库事务中清除该 Agent 所有 inactive SDK resume
  映射；失效失败时不得 merge，也不得返回 `next_turn` 成功。
- 普通异常和数据库提交失败先执行 Git 补偿；若进程恰好在 Git 激活后、数据库提交前退出，
  系统无法仅凭过期 claim 判断是否已经跨过激活边界，因此下一次准入在清理过期的
  `workspace_import` / `workspace_restore` claim 时保守清除 inactive SDK mappings。最坏结果
  是多建一个新 SDK session，不允许旧 transcript resume 到可能已经变化的 workspace。
- 维护结束后，新 turn 保留原 API session id，但不携带旧 SDK resume；它读取新的 Git HEAD
  和原生 Claude Code workspace 配置，并绑定 applied `agent_version_id`。
- 不需要重启 API，也不输出 `requires_runtime_restart`。

## 7. UI

设置页提供：

- “导入 Agent workspace”：选择包，填写 Agent ID；新建时填写 name。
- 既有 Agent 行的“覆盖导入”。
- 成功回执中的 previous/current commit。
- 覆盖成功后的“恢复导入前版本”按钮。

覆盖前明确说明“workspace 将原样替换，下一 turn 生效”。失败态必须显示结构化错误和可执行动作，
不能只显示通用上传失败。

## 8. 验收

- 含真实 endpoint、`.env`、MCP header 形态和二进制文件的包可原样导入。
- 导出同一 workspace 后再导入，tree digest 不变。
- 新建、覆盖、unchanged 和恢复均绑定实际 commit。
- active turn、开放 change set、HEAD 竞争、恶意 tar 和超限输入明确失败。
- Git 提交、registry finalize 或文件替换任一点故障后，原 workspace 和 HEAD 可证明未改变或已恢复。
- 新 turn 使用 applied commit，当前 turn 和 session 身份不被静默改写。

## 9. 后续升级触发

只有出现公网导入、外部租户、客户敏感数据、合规要求或真实攻击事件时，才重新评估包签名、
发布者身份、恶意代码隔离和内容治理。它们不是首版上线前置。
