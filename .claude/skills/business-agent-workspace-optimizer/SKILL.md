---
name: "business-agent-workspace-optimizer"
description: "开发、配置和优化 AgentGov 业务 Agent 的 Claude 原生 Workspace。用户要求修改某个业务 Agent 的 CLAUDE.md、.mcp.json、.claude/settings.json、skills、agents、rules、hooks、commands、tests，或离线修改运行态及内置业务 Agent Workspace 时使用。"
---

# 业务 Agent Workspace 优化

> 本技能与 `.codex/skills/business-agent-workspace-optimizer/SKILL.md` 同源镜像，修改需两侧同步。

本技能供工程师、Codex 和 Claude 离线开发业务 Agent Workspace。它不提供产品内自优化能力，
不创建治理 Agent，也不替代 Workspace 包导入、change set、release 或反馈治理流程。

## 稳定边界

- 所有注册业务 Agent（含 `main-agent`）遵循同一运行态路径和治理机制；不得把 `main-agent`
  当作默认、模板或隐式兜底。
- 内置、默认和受保护是三个独立的平台属性；具体 Agent ID 以平台常量和注册表为准，
  不得在本通用 skill 中复制某个 Agent 的业务行为或用 `seed` / `origin` 合并表达。
- 普通业务 Agent 只能通过完整的业务 Agent Workspace 包导入创建。仓库不提供通用创建模板，
  也不存在运行态 seed catalog。
- `docker/runtime-bootstrap/` 是运行卷初始化源，不是模板 catalog，也不是运行态副本。它只承载
  `governor-workspace/` 和显式声明的内置业务 Agent Workspace。
- 导出的业务 Agent Workspace 包可以作为新 Agent 的修改起点；导入不会重写包内身份文本或
  权限配置，运行归属由目标 `agent_id` 和注册表决定。

## 适用范围

- 运行态业务 Agent Workspace：
  `${RUNTIME_ROOT}/data/business-agents/<agent_id>/workspace/`。
- 仓库内置业务 Agent Workspace：
  `docker/runtime-bootstrap/business-agents/<agent_id>/workspace/`。
- 用户明确要求时，可处理 `docker/runtime-bootstrap/governor-workspace/`，但必须说明它是治理
  Agent，不属于业务 Agent。

以下任务不适用：新增注册表/API/生命周期模型、修改反馈归属、让业务 Agent 运行时自行改配置、
修改 `version/` 或 Claude 会话状态、绕过 Workspace 包导入、自动发布生产版本。

## 目标解析矩阵

动文件前填写：

| 用户目标 | 对象 | 允许路径 | 证据 | 验收 |
| --- | --- | --- | --- | --- |
| 已注册业务 Agent | 运行态 Workspace | `${RUNTIME_ROOT}/data/business-agents/<agent_id>/workspace/` | 用户给定路径或 `GET /api/agent-registry` 的 `workspace_dir` | 配置、权限、目标测试、下一 turn |
| 内置业务 Agent | 运行卷初始化源 | `docker/runtime-bootstrap/business-agents/<agent_id>/workspace/` | 明确要求修改该内置配置 | 准入扫描、Agent 自测、空卷初始化 |
| governor | 治理 Agent Workspace | `${RUNTIME_ROOT}/governor-workspace/` 或仓库初始化源 | 用户明确指定 governor | 治理 Agent 专项验证 |
| runtime 父目录或并列层 | 非 Workspace | `data/`、`business-agents/`、`<agent_id>/version/`、`claude-root/` | 只是父目录或状态目录 | no-op 并重新定位 |

只说“业务 Agent”且无法唯一定位时先确认目标，不默认选择 `main-agent`。
不得把 `${RUNTIME_ROOT}/data` 或 `data/business-agents/` 父目录当作修改目标；必须定位到单个业务 Agent 的 Workspace。

## 路径硬门

允许修改单个已确认 Workspace 内的 `CLAUDE.md`、`.mcp.json`、`.claude/`、hooks、commands、
`tests/` 和业务文件。默认拒绝：

- 任意 `.../version/` 和其中的 per-Agent Git 管理文件；
- 任意 `.../claude-root/`、`claude-roots/`；
- `data/runtime.sqlite3*`、`data/agent-governance/`、`data/outputs/`、`data/transcripts/`、
  `data/uploads/`、`langfuse/` 和 `.git/`；
- 仓库初始化源中的 `.env*`、`.mcp.local.json`、`settings.local.json`、`CLAUDE.local.md`、
  `secrets/` 或任何真实凭据。

不能整目录拒绝 `data/`，因为运行态业务 Agent Workspace 位于其下；但 `data/` 和 `data/business-agents/` 父目录本身也不是优化目标。

运行态 Workspace 可以按字节保留真实 endpoint、header、凭据型配置和本机路径。除非用户明确
要求，不修改这些值，也不在日志、diff 摘要或最终回复中回显。该例外不延伸到仓库初始化源。

## 工作流

### 1. 读取现状

- 读取 `CLAUDE.md`、`.mcp.json`、`.claude/settings.json`。
- 检查 `.claude/skills/`、`.claude/agents/`、`.claude/rules/`、hooks、commands 和 `tests/`。
- 简要列出已有能力、工具、权限边界、缺口以及目标文件。

### 2. 映射需求

- 角色、行为边界和输出契约：`CLAUDE.md`。
- 可复用流程：`.claude/skills/<skill>/SKILL.md`。
- 子角色：`.claude/agents/*.md`。
- 工具接入：`.mcp.json`，同时核对 `.claude/settings.json` 权限。
- Playground 静态 Welcome Card：`agent.yaml.presentation`；只配置摘要、开场内容、输入框提示和建议任务，
  不创建会话、不伪装 assistant 消息，也不替代 Claude 原生 `AskUserQuestion`。
- 硬拒绝或审计：`.claude/rules/*` 或 hooks。
- 行为验收：Workspace `tests/test_*.py`、专项测试或可重复验证命令。

### 3. 修改

- 只改已确认的单个 Workspace，不跨 Agent 搬运私有配置。
- 修改前说明目标路径、文件清单、权限变化、验证方式和运行卷影响。
- 运行态高风险修改先备份到 `/tmp/agentgov-workspace-optimizer-backups/<timestamp>/`。
- 运行态 Workspace 同时是其 Git 版本源。直接修改会留下未提交变更；需要固化时走现有
  change set/release 流程，不直接编辑 `version/`。

### 4. 验证

- `.mcp.json` 与 `.claude/settings.json` 必须是 JSON object。
- `SKILL.md` 必须有合法 `name` 和 `description` frontmatter。
- 通用业务 Agent 的宽泛 Bash 默认进入 `ask`；只允许审计过的具体低风险规则进入 `allow`。
  run 级授权必须按低风险类别隔离，高风险或未分类请求不得整轮放行。
- 工具、权限、确认和业务流程契约只从目标 Workspace 的 `README.md`、`CLAUDE.md`、`agent.yaml`、
  `.mcp.json` 与 `.claude/settings.json` 读取；本通用 skill 不硬编码具体工具名或领域流程，后端不得
  按 Agent ID 添加第二套授权。
- `agent.yaml.presentation` 若存在，必须保持静态展示语义；Agent 身份和名称仍以平台注册表为准，
  建议任务只能填入输入框，不能自动发送。
- 仓库初始化源改动运行 `make runtime-bootstrap-scan`。唯一扫描实现是
  `scripts/runtime_bootstrap_safety.py` 的 `scan_path`；不得新建并行扫描器。只有用户明确要求
  替换敏感值时才使用其 `sanitize`，并复核 diff。
- 初始化行为用空运行卷验证；已有业务 Agent Workspace 必须整体跳过，不逐文件回灌，不生成
  隐式 Git commit。
- 普通新 Agent 通过 `POST /api/agent-registry/{agent_id}/workspace/import` 验证创建；不得调用或
  恢复旧 `POST /api/agent-registry`、模板列表或 seed 来源参数。
- Workspace 测试使用 `agentgov_testkit` 的 pytest `agent` fixture、`agent.run()` 和结果
  `.text`；本机和平台运行都通过 `AGENTGOV_API_BASE`、`AGENTGOV_API_KEY`、
  `AGENTGOV_AGENT_ID` 绑定远程业务 Agent，省略 `AGENTGOV_COMMIT_SHA` 时只在 pytest session
  开始时解析一次当前提交。
- 手工平台运行调用 `POST /api/agent-test-runs`；待发布变更调用
  `POST /api/agent-change-sets/{change_set_id}/test-runs`。客户端不得提交命令、状态、报告或
  `change_set_id` 归属。

### 5. 报告

报告已改文件、未改边界、需人工配置项、验证结果，以及配置从下一 turn 生效还是需要空卷初始化。

## 关联治理

- MCP、env、volume、本机与容器边界遵循 `runtime-env-governance`。
- 功能行为变化先按 `test-sync-governance` 决定测试增删改。
- 本技能由 `.codex` 与 `.claude` 同源镜像；修改后运行 docs/config 治理检查。
