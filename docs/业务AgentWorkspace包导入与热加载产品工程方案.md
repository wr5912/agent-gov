# 业务 Agent 候选创建、Workspace 包与发布工程契约

> 文档状态：当前产品工程契约。文件路径为兼容既有链接保留；“热加载”不再表示修改活动
> Workspace 或下一轮对话自动生效。公开字段以当前 OpenAPI 为单一真相源。

## 1. 核心裁决

业务 Agent 是 AgentGov 的稳定治理身份；AgentScope 原生 Agent 是某个已发布 Git commit 的不可变
运行实例。原生 schema 表单和完整 Workspace 包只是两种输入方式，二者都必须进入同一个 Git 候选
命令、测试/审批门禁和发布激活 saga，不能产生两套生命周期。

| 动作 | 产生的事实 | 不会发生的事情 |
| --- | --- | --- |
| 原生 schema 表单创建/配置 | `draft` 治理身份、隔离 change set、候选 commit | 不写活动 Workspace，不创建可运行 Session |
| Workspace 包创建/覆盖导入 | 同一种 `draft`/change set/候选 commit 及导入审计 | 不推进活动 HEAD，不让已有 Session 换版本 |
| 历史树恢复 | 从指定历史 commit 形成新的候选 commit | 不 `hard reset`，不直接回退生产 |
| 候选文件编辑 | 在一个 change set 内批量校验并形成一个 commit | 不调用旧 live config API，不逐文件暗中激活 |
| 测试、审批、发布 | 精确 commit 测试；必要人工审批；原生 Agent 创建与探测；Git 发布和 Runtime 绑定 | 不要求用户再点击“启用 Runtime” |

发布成功只影响之后创建的新 Session；已有 Session 始终保留其创建时绑定的
`agent_version_id + harness_digest + runtime_agent_id`。因此项目不再使用“保存后下一 turn 生效”语义。

## 2. 事实所有权与路径

```text
仓库运行卷初始化源
docker/runtime-bootstrap/
├── governor-workspace/
└── business-agents/security-operations-expert/workspace/

宿主机持久化根
${HOST_RUNTIME_VOLUME_ROOT}/
├── governor-workspace/
└── data/business-agents/<agent_id>/
    ├── workspace/       # 当前已发布 Git tree；不是在线编辑区
    └── version/
        ├── worktrees/   # 隔离候选
        └── releases/    # 发布归档
```

`docker/runtime-bootstrap/` 只负责空卷初始化，不是在线模板 catalog。运行态 Workspace、per-Agent Git、
AgentScope 数据库和 AgentGov SQLite 均属于持久化数据；部署、迁移或回退前必须单独盘点和备份。

## 3. 两种候选输入

### 3.1 AgentScope 原生 schema 表单

前端先读取：

```text
GET /api/runtime/agent-schema
```

该响应来自固定 AgentScope `GET /agent/schema/v2`，AgentGov 只允许当前评审过的原生字段：

- `name`
- `system_prompt`
- `context_config`
- `react_config`
- `invite_config`

提交入口：

```text
POST /api/agent-registry/{agent_id}/native-candidate
```

Provider credential、治理 ID、发布状态、版本字段、MCP secret、Workspace 路径等后端所属字段不可注入。
字段到 Git 的映射集中在一个边界：`system_prompt` 写入 `AGENT.md`，其余受支持字段写入
`agent.yaml`；未知字段和 schema 漂移必须 fail closed，不能静默丢弃。

### 3.2 完整 Workspace 包

```text
POST /api/agent-registry/{agent_id}/workspace/import
Content-Type: multipart/form-data
```

`.tar.gz` 解压后必须恰好包含一个 `workspace/` 顶层目录。包可包含：

```text
workspace/
  agent.yaml
  AGENT.md
  mcp/<name>.json
  skills/<name>/SKILL.md
  subagents/<name>/agent.yaml
  subagents/<name>/AGENT.md
  tests/README.md
  tests/test_*.py
  ...其他受策略允许的普通文件
```

包所有者负责普通文件内容；平台保留字节与 executable bit，不改写身份、endpoint 或凭据引用。
`agent.yaml.agent.id` 必须与 URL 的 `agent_id` 逐字一致。路径逃逸、绝对路径、NUL、非 UTF-8 名称、
重复项、`.git`、symlink/hardlink/device/FIFO/socket、资源上限和无效 YAML/JSON 均在产生候选前拒绝。

运行态 Session/Message、AgentGov run/feedback、Langfuse 数据、数据库和 Runtime 可写状态不得进入包。
敏感 live Workspace 可以按字节导入/导出，但回流源码仓库初始化源前必须在仓库外形成候选并通过
`make runtime-bootstrap-scan`；项目仓库、日志和回执不得暴露 secret。

## 4. 候选回执与编辑

两种输入都返回候选回执，关键字段为：

```json
{
  "agent": {"agent_id": "customer-support", "status": "draft"},
  "change_set_id": "change-set-id",
  "change_set_status": "pending_approval",
  "base_commit_sha": "40-character-base-sha",
  "candidate_commit_sha": "40-character-candidate-sha",
  "changed_paths": ["AGENT.md", "agent.yaml"],
  "published": false
}
```

包导入还返回 package/tree digest、导入审计 ID 和测试资产诊断。`published=false` 是固定事实，响应中
不存在 `activation_mode`、`current_commit_sha`、`rollback_target_commit_sha` 等旧直接激活字段。

候选文件 API：

```text
GET /api/agent-change-sets/{change_set_id}/files?path=<relative-path>
PUT /api/agent-change-sets/{change_set_id}/files
```

PUT 使用 `expected_candidate_commit_sha` 做 CAS，并在一个命令内校验全部文件后只创建一个 commit。
旧 `/api/agent-config-file` 已删除；活动 Workspace 没有在线文件写入口。

新 Agent 创建先持久化不可运行的 `draft` 身份和空 Git 基线，再建立候选。这样 change set 有稳定基线，
而半成品不会进入正常 Session/run 准入。若候选阶段失败，安全 draft 可由相同 CAS 命令重试或显式清理；
不得伪装成 active。

## 5. 测试、审批和发布激活

标准流程：

1. 对 `candidate_commit_sha` 检查 `tests/` 并创建
   `POST /api/agent-change-sets/{change_set_id}/test-runs`。
2. 等待该精确 `agent_id + change_set_id + commit_sha` 的平台测试结果为 `passed`。
3. `agent.yaml`、`mcp/`、`subagents/` 等敏感路径必须经
   `POST /api/agent-change-sets/{change_set_id}/approve` 明确审批。
   设置中的候选治理展示完整文件 Diff；全部 Diff 和精确测试证据加载后，点击一次“确认审批”即表示
   用户已审阅全部文件，无需逐项勾选。请求仍绑定完整文件审阅指纹、候选 commit、Diff 和测试证据；
   审批成功不自动发布。
4. 调用 `POST /api/agent-change-sets/{change_set_id}/publish`。同一命令负责：校验候选与维护租约、
   从精确 commit 物化只读 Harness、创建/定位原生 Agent、创建可恢复探测 Session、验证模板和运行就绪、
   持久化版本绑定、推进 Git 活动指针并完成发布记录。
5. 通过 `GET /api/runtime/agents/{agent_id}/current` 核对发布 commit、digest、原生 Agent ID 和
   `provisioned=true`。客户端不能另走 provision 旁路。

Runtime 调用和 Git/SQLite 不是虚假分布式事务。发布使用持久 locator、唯一版本约束、响应丢失后的
原生资源查找和按所有权补偿来实现幂等恢复。Git 切换前失败保留旧活动版本，新 Agent 仍为 draft；
不确定结果必须重试同一个发布命令。陌生或已有引用的 Runtime 资源不能当作本次孤儿删除。

若新版本包含 Runtime 启动时才可注册的 subagent template，发布必须明确进入受控维护流程；完成在途
运行处理、Runtime 重启、readiness、旧会话恢复和候选探测前，不得标记发布可用。
Runtime 对此返回 HTTP 409 和稳定 `error_code: RUNTIME_TEMPLATE_RESTART_REQUIRED`，不依赖
异常文本匹配。控制面将准备资源记为 `awaiting_restart`，清理探测 Session，但保留精确不可变快照和
Runtime Agent 定位信息；变更集维持 `publishing` 并保留原发布意图、审批证据及发布占用记录。
受控重启后重试同一发布命令，重新探测已有资源并完成激活。尚未完成绑定的准备阶段，其他错误仍按
所有权补偿，清理失败保留定位信息等待重试，活动 Git 与版本绑定不得提前切换。
已完成绑定的 `ready` / `active` 资源可能对应已成功切换、但发布记录尚待收尾的 Git 版本；重试只读
核对原生 Agent 身份，断网、查询失败或身份歧义不得触发资源补偿。`active` 资源明确禁止进入发布前
失败清理；业务 Agent 的显式删除仍走独立删除流程。

## 6. 导出、恢复和删除

```text
POST /api/agent-registry/{agent_id}/workspace/export
POST /api/agent-registry/{agent_id}/workspace/restore
DELETE /api/agent-registry/{agent_id}
```

导出只读取当前已发布 Git tree。恢复读取目标历史 commit，但产物仍是新 change set，活动 HEAD 和已有
Session 保持不变。删除先停止新准入、处理引用和 Runtime 资源，再 tombstone 治理身份并在事务外清理
文件；持久 locator 支持重入。受保护的内置 Agent 不可在线删除。

## 7. UI 契约

- “创建 Agent”提供原生 schema 表单与 Workspace 包两个输入页签，结果区使用同一候选回执组件。
- 成功文案固定为“候选已保存，尚未发布”，显示 change set、基准/候选 commit、changed paths、测试资产
  状态，并给出“运行测试 → 人工审批 → 发布”的下一步。
- 覆盖导入和历史恢复不得确认“下一 turn 生效”；发布前活动版本和已有 Session 不变。
- Playground 运行设置只显示当前 Session 的真实 Workspace status、MCP 连接/工具和 skill 投影。
  MCP 连接成功只证明可连接和可列举工具，不证明本次业务回复实际调用成功。
- `/api/agents`、`/api/skills` 的目录扫描，`/api/config` 的路径推断和 live 配置编辑器不再出现在 UI。

## 8. 机器与真实验收

- OpenAPI、生成类型、前端和在线路由中不存在旧直接激活字段、live config API、运行目录 scanner 或
  独立 provision；对旧 URL 的真实请求返回 404。
- 表单和包导入对新/既有 Agent 都产生同一种 candidate receipt；活动 HEAD、活动 Workspace 与已有
  Session 在发布前不变。
- 覆盖竞争、过期 CAS、候选响应丢失、并发写、Git/SQLite/Runtime 部分失败均有可重入或补偿证据。
- 平台测试必须精确绑定候选 commit；敏感路径没有审批时发布失败；发布成功的 release/current/Session
  四元组一致。
- 真实 Compose 验收从当前工作树重建镜像并 force-recreate；浏览器完成 Agent 选择、Session 创建、
  重命名、发送、SSE、终态、历史恢复、资源状态与删除，不以页面打开或 HTTP 200 代替。
- 正式验收不使用 mock 数据、mock API、请求拦截或伪造 Runtime；单元/组件回归中的受控测试数据不得
  被记作真实业务验收证据。

相关入口：

- [AgentScope API 最大复用与单轨整改计划](./engineering/AgentScope_API最大复用与单轨整改计划.md)
- [AgentGov 集成指南](./AgentGov集成指南.md)
- [业务 Agent Workspace 原生 pytest 测试资产实现方案](./engineering/业务AgentWorkspace原生pytest测试资产实现方案.md)
- [AgentGov AgentScope Runtime 替换实施基线与验收](./engineering/AgentGov_AgentScope_Runtime替换实施基线与验收.md)
