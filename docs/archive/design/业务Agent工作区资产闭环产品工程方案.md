# 业务 Agent 工作区资产闭环产品工程方案

> 文档状态：已归档的旧设计。本文保留通用模板、seed catalog 和直接创建 API 的历史决策，
> 不再描述当前实现。当前契约见
> [业务 Agent Workspace 包与运行卷初始化工程契约](../../业务AgentWorkspace包导入与热加载产品工程方案.md)。
>
> 本文定义“同步导出 → 本地修改 → 原样导入 → 下一 turn 生效”以及“seed 跨 ID
> 原样实例化”的资产闭环。稳定裁决来自
> [AgentGov 工程宪法 2.6、2.7](../../engineering/CI-CD宪法与交付链两阶段整改计划.md)；
> 导入包和应用事务以
> [Workspace 包导入方案](../../业务AgentWorkspace包导入与热加载产品工程方案.md)为准。

## 1. 裁决与理由

| 裁决 | 最小事实依据 | 本期不做 | 验证与退出条件 |
| --- | --- | --- | --- |
| 导出当前 live workspace | 用户真正调优的是运行卷中的 per-Agent Git tree | 另造按忽略规则复制工作树 | 导出产物绑定实际 commit，包可直接导入 |
| 同步返回 gzip | 包大小已有明确上限，两步 export job 只会增加状态和清理负担 | export_id、异步状态、产物数据库 | 一次请求返回完整包和 digest |
| seed 跨 ID 不渲染 | 平台无法可靠改写散文身份，内容改写会破坏原样资产语义 | 内容身份推断、模板变量替换 | source seed 与目标 workspace 普通文件字节一致 |
| MCP 环境值由 Claude Code 原生解析 | `.mcp.json` 原生支持 `${VAR}`，无需平台落盘改写 | runtime 文件 renderer | container/local-debug 使用同一 seed bytes |
| live workspace 可带真实 endpoint | endpoint 是 Agent 可执行配置的一部分 | 导出脱敏、导入参数化 | 含真实 endpoint 的往返 tree digest 一致 |

## 2. 对象边界与闭环

“原样”只描述某条复制链路是否改写文件，不代表所有目标存储具有相同的保密和权限边界：

| 对象 | 是否原样 | 真实秘密与私有配置 | 权限语义 |
| --- | --- | --- | --- |
| live workspace 导入、导出、恢复 | 是；普通文件、二进制和 executable bit 不被平台改写 | 可包含业务运行所需真实值；包按敏感资产保管，不在日志或回执中回显 | 保留包内声明，不套用 generic template 基线 |
| generic template → live workspace | 除明确的 `{{AGENT_ID}}` / `{{AGENT_NAME}}` 外原样 | repo template 不含真实密钥、私有 header、数据库凭据或本机私有路径 | generic template 提供保守出生基线 |
| repo 声明 seed/builtin → 运行态 seed catalog → live workspace | 是；同 ID、跨 ID 都逐字节复制；已在线删除的 seed 不再填充 | seed 本身必须先满足可提交仓库边界；“实例化原样”不等于“可把任意 live workspace 原样提交进 Git” | 保留该 seed 自己的权限配置，不强制改成 generic template 权限 |
| live workspace → repo 声明 seed/builtin | 先在仓库外生成逐字节候选；写入 repo 时分级检查，不静默改写 | 明确秘密和本机私有路径必须移除；非秘密 endpoint、内网地址等环境绑定值只警告，由提交者决定保留或参数化 | 是否调整权限由该 builtin 的产品定位与评审决定；宽权限提示复核，不自动收紧 |

因此，原样导入与 repo template 的安全基线不冲突；真正冲突的是“带真实秘密的 live
workspace 还能逐字节纳入当前 Git builtin”这一说法，本文不作该承诺。若未来需要这种私有
builtin，应设计仓库外的私有运行态 catalog，而不是放宽源码仓库边界。

```text
repo-safe declared seed / generic template
  -> 创建业务 Agent
  -> live workspace + per-Agent Git
  -> 同步导出 workspace.tar.gz
  -> 本地任意修改
  -> 原样导入
  -> 新 Git commit
  -> 下一 turn 生效
```

seed 只负责出生配置。已有 live workspace 是当前行为事实，启动、重启、部署和 seed 更新都不得
逐文件回灌或自动修复。

## 3. 同步导出

```text
POST /api/agent-registry/{agent_id}/workspace/export
  -> application/gzip
```

行为：

1. 解析 registry 中的真实 workspace 路径，不自行拼接用户输入。
2. 取得维护栅栏，确保快照与实际文件 tree 一致。
3. dirty workspace 自动创建 Git snapshot；快照强制纳入普通文件，包括被 `.gitignore`
   忽略的 workspace 自有文件；clean workspace 使用当前 HEAD。
4. 使用 `git ls-tree` 读取该 commit 的普通 blob 清单，再用 `git cat-file blob` 读取原始
   字节；按路径排序后构造 `workspace/` PAX tar，并固定 uid、gid、mtime 与 gzip mtime，
   得到确定性 `.tar.gz`。不调用 `git archive`，因此包内 `.gitattributes` 的
   `export-ignore`、`export-subst` 或文本换行规则不能静默删改导出内容。
5. 返回后清理临时包，不在 release 目录留下在线导出状态。

当前 OpenAPI 的成功响应头包含：

- `Content-Disposition`；
- `X-Agent-Commit-SHA`；
- `X-Workspace-Package-SHA256`；
- `X-Workspace-Tree-SHA256`。

导出不做 endpoint、凭据形态或二进制内容扫描。它返回快照 commit 的完整普通文件 tree，
保留文件字节和 executable bit；symlink、submodule 等非普通 blob 明确失败，`.git` 不进入
产物。选择 commit blob 而不是直接打包工作目录，是为了让响应头中的 commit 与 tree digest
能共同证明下载内容，同时避免工作目录在归档期间变化。

## 4. 从 seed 跨 ID 实例化

`AgentCreateRequest` 增加 `source_seed_id`：

```json
{
  "name": "SOC Assistant Variant",
  "agent_id": "soc-assistant-2",
  "source_seed_id": "main-agent"
}
```

契约：

- `source_seed_id` 与 `template_id` 互斥，同时提供返回 `422`；
- source 必须是声明 seed catalog 中的真实目录，拒绝 symlink、空目录和非法 ID；
- source seed 的普通文件层级、文件字节和 executable bit 原样复制；空目录不属于 Git/workspace 包资产；
- 不替换源 Agent ID、name、散文描述、endpoint 或路径；
- registry 身份为目标 `agent_id`，origin 为 `user`；
- source 不持久化为第二套 active-version/provenance 状态。

两者都不提供时保持现有简单行为：目标 ID 有同名声明 seed 时使用它，否则使用 `general`
generic template。generic template 仍可替换明确的 `{{AGENT_ID}}` 和 `{{AGENT_NAME}}`；
声明 seed 不调用该逻辑。

## 5. 去除文件渲染

删除 AgentGov 自定义 runtime template renderer：

- bootstrap 只复制缺失 workspace，不改文件内容；
- seed 创建同 ID/跨 ID 均原样复制；
- Runtime 启动只读校验，不回灌 settings/MCP；
- receipt 变化不产生 workspace Git commit。

repo seed 的 `.mcp.json` 继续使用 Claude Code 原生 `${MCP_SERVER_URL}`、
`${SEC_OPS_MCP_URL}`。应用必须把已选择的完整 runtime env 传入 Claude 子进程。

`.claude/settings.json` 改为 mode-neutral：

- 文件路径使用项目相对路径；
- network allowed domains 使用 container/local-debug 所需静态并集；
- 不再出现 `${SERVICE_HOST}`、`${INTERNAL_DOMAIN}`；
- 不按 mode 改写 sandbox 字段。

已存在卷中的真实 endpoint、旧绝对路径和 mode-specific settings 原样保留，只要当前 Runtime
能够读取就不迁移。

## 6. 离线归档为 seed

归档仍是开发者离线仓库动作：

1. 从 live Agent 导出精确 commit 包。
2. 在仓库外把该包保留为逐字节 builtin 候选，不直接解包覆盖 repo seed。
3. 运行只读扫描：真实密钥、私有 header、数据库凭据和本机私有路径属于硬阻断，必须删除或改为
   私有环境注入；非秘密 endpoint、内网地址、账号形态与较宽权限属于警告，由提交者决定保留或替换。
4. 把 `workspace/` 内容放入
   `docker/runtime-volume-seeds/data/business-agents/<seed_id>/workspace/`。
5. 运行 seed 扫描、目标测试和真实 Compose 空卷初始化。
6. 经受保护 PR 合并。

不提供在线 API 写 seed：新增一个 seed 只能经仓库准入，运行态不能绕过评审自造声明基线。
归档过程中发生的仓库边界处理不改变 live workspace，也不削弱导入/导出的原样契约。

出生身份由仓库声明目录派生，但**运行态存在性**由运行态 seed catalog（`data/seed-catalog/`）裁决：
bootstrap 把仓库出生配置填充进 catalog，业务 Agent 的出生与 `origin` 判定读 catalog。删除一个
业务 Agent 会一并移除它的 catalog 条目并留下删除标记，使删除跨重启保持——否则仓库出生配置会在
每次启动把它填回来。仓库出生配置不受在线删除影响：换一个空运行卷时，该 seed 仍会重新出生。
受保护 Agent（`security-operations-expert`）例外：其配置与 seed 在仓库维护，bootstrap 强制确保
catalog 条目存在，在线删除一律拒绝。

## 6.1 删除业务 Agent

```text
DELETE /api/agent-registry/{agent_id}
  -> {deleted, impact, workspace_removed, seed_removed, cleanup_complete}
```

删除是一次注册表 tombstone + 一次磁盘清理，二者分处事务前后：

1. 取维护租约（与导入/导出/恢复共用同一把），因此删除与它们、与活跃 turn 天然互斥——租约获取
   本身就拒绝存在活跃 run 的 Agent，不会删掉正在被使用的 workspace。
2. 事务内置 `deleted_at`：Agent 立即不可见，`sync`/磁盘发现均跳过，重启不复活。
3. **事务提交后**才清理磁盘：删除 `data/business-agents/<id>/` 整个目录（workspace、claude-root、
   version），移除运行态 seed catalog 条目并写删除标记。rmtree 不可回滚，放进事务块意味着事务
   回滚后磁盘已经回不来。
4. 清理结果如实回报（`workspace_removed` / `seed_removed` / `cleanup_complete`）。部分失败时
   注册表已删除但磁盘有残留，同 id 重建会被安全供给流程拦住，不会静默继承残留内容。

边界：

- **受保护 Agent 拒删**（400）：`security-operations-expert` 的配置与 seed 在仓库维护，只能经
  受保护 PR 移除。保护认受保护名单，不认 `origin`——origin 随 catalog 内容漂移。
- **main-agent 可删**：它只是出厂默认，不是平台组件。删除后未指定 `agent_id` 的 `/v1` 请求会得到
  明确的 404 并提示重新配置出口 Agent，而不是跑在幽灵 profile 上。
- **治理记录保留**：runs、feedback、change set、release 是已发生事实，不随 Agent 消失；删除前以
  影响面计数提示，避免无声删除治理对象。
- **同 id 可重建**：清 tombstone 后重新创建，得到全新 workspace（磁盘已清），不继承被删 Agent 的
  prompt/skills/MCP 配置。

## 7. UI

设置页创建 Agent 时提供两种互斥来源：

- generic template；
- 已声明 seed。

每个 Agent 行提供：

- 导出当前 workspace；
- 覆盖导入 workspace；
- 查看导入后的 previous/current commit；
- 恢复导入前 commit。

跨 ID 创建时明确提示：“工作区内容按 source seed 原样复制，身份相关表述不会自动修改。”

## 8. 验收

- source seed X 创建 Agent Y 后，普通文件字节与 executable bit 一致，registry ID 为 Y。
- container/local-debug bootstrap 生成相同 workspace bytes。
- MCP 环境变量引用在两种运行环境中由 Claude Code 正确解析。
- 含真实 endpoint 和二进制文件的 live workspace 能导出并原样导回。
- 含真实密钥或本机私有路径的 live workspace 可以原样往返，但不能绕过 seed 扫描直接进入 repo builtin。
- 声明 seed 中经评审保留的非秘密真实 endpoint 只产生警告，不被扫描器静默替换。
- 声明 seed 跨 ID 实例化保留源 seed 权限；generic template 的保守权限不得覆盖它。
- dirty workspace 导出先形成可追溯 snapshot；并发 export/import 不覆盖共享临时文件。
- 已有 workspace 不因 seed 变化、receipt 更新或 API 重启产生隐式 commit。
- 归档 seed 后只影响后续缺失 workspace，不回灌已有 Agent。

## 9. 后续升级触发

模板市场、第三方分发、在线晋升 seed、包签名和跨租户共享均在出现真实用户与分发需求后另行设计，
不预埋状态机或兼容字段。
