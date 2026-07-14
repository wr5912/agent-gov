---
name: "business-agent-workspace-optimizer"
description: "开发、配置和优化 AgentGov 业务 Agent 自身 workspace 配置资产。用户要求提升某个业务 Agent 能力、修改该 Agent 的 CLAUDE.md、.mcp.json、.claude/settings.json、skills、agents、rules、hooks、commands、evals、templates，或要求离线修改 ${HOST_RUNTIME_VOLUME_ROOT} / docker/runtime-volume-seeds 中业务 Agent workspace 配置时使用。"
---

# 业务 Agent Workspace 优化

> 本技能与 `.codex/skills/business-agent-workspace-optimizer/SKILL.md` 同源镜像，修改需两侧同步。

本技能给工程师、Codex、Claude 在本仓库离线开发时使用：按用户需求直接开发/优化某个业务 Agent 的 workspace 配置资产（prompt、skill、agent、rule、hook、command、MCP、eval、template）。它不是产品内置的业务 Agent 自优化能力，不是新的治理 Agent，也不是业务 Agent 运行时可调用的工具。

## 适用与不适用

- 适用：离线修改业务 Agent 的 workspace 配置资产；以业务 Agent 为主目标，`main-agent` 仅作样板（不是长期唯一边界）。
- 不适用：新增产品 API、新增注册/生命周期/版本/反馈归属数据模型、让业务 Agent 运行时自改、修改治理 Agent（governor）合并方案、绕过 runtime-volume-seeds 脱敏边界、自动发生产。
- 治理 Agent（`governor`，见 `GOVERNANCE_AGENT_ROLES`）默认不作为目标；仅当用户明确要求改 governor 配置时纳入，并说明这是治理 Agent 而非业务 Agent。

## 治理对象预检（先做再改）

| 维度 | 结论 |
| --- | --- |
| 被治理对象 | 某业务 Agent 的 workspace 配置资产（执行资产为主） |
| 治理执行者 | 开发者 / Codex / Claude 离线执行，非运行时自改 |
| 资产类型 | prompt、skill、agent、rule、hook、command、MCP、eval、template |
| 反馈归属 | 若改动来自反馈，实施记录标明目标 agent_id、反馈来源、影响文件、验收方式；不改数据库归属模型 |
| 当前边界 | `/api/chat?agent_id=` 跑注册业务 Agent；业务 profile 从其 workspace 加载 `.mcp.json` / `.claude/settings.json`（`build_business_agent_profile`） |

闭环：业务 Agent → workspace 配置资产 → 离线修改 → 格式/安全验证 → 本地运行或模板渲染验证 → 后续版本治理/发布。

## 工作流（强制顺序）

### 1. 目标确认

- 明确目标是「运行态 workspace」还是「模板 workspace」。
- 明确目标 agent_id、workspace 路径、业务用途。
- 先填写目标解析矩阵，再动文件：

| 用户目标 | 对象类型 | 解析到的路径 | 是否可编辑 | 证据 | 验收 |
| --- | --- | --- | --- | --- | --- |
| 单个业务 Agent（含预制 main-agent） | 业务 Agent workspace | `${RUNTIME_ROOT}/data/business-agents/<agent_id>/workspace` | 是，必须精确到单个业务 Agent 的 `workspace/` 配置层 | `agent_id`、registry `workspace_dir` 或用户给出的绝对路径 | JSON/Markdown/权限/脱敏扫描 |
| 预制 main | 业务 Agent workspace | `${RUNTIME_ROOT}/data/business-agents/main-agent/workspace`（main 已归一为预制业务 Agent，不再是顶层样板） | 是 | 用户要求改 main | 同业务 Agent 验收 |
| governor | 治理 Agent workspace | `${RUNTIME_ROOT}/governor-workspace` 或 `docker/runtime-volume-seeds/governor-workspace` | 仅用户明确要求 | 说明这是治理 Agent，不是业务 Agent | 同步治理 Agent 相关验证 |
| runtime 父目录 / 并列层 | 非 workspace | `${RUNTIME_ROOT}/data`、`.../business-agents`、或 `<agent_id>/`（claude-root/version 并列层） | 否 | 只是父/并列目录，不是配置层 | no-op 并重新定位 |

- 离线解析 agent_id → workspace（不要直接读 `runtime.sqlite3`，不得把 `${RUNTIME_ROOT}/data` 或 `${RUNTIME_ROOT}/data/business-agents` 父目录作为修改目标）：
  - 运行态业务 Agent：约定路径 `${RUNTIME_ROOT}/data/business-agents/<agent_id>/workspace` 指向单个业务 Agent 配置层（容器 `/data/business-agents/<agent_id>/workspace`，本机调试 `/tmp/local-debug-volume-agent-gov/data/business-agents/<agent_id>/workspace`）；或经运行中的 `GET /api/agent-registry` 查 `workspace_dir`。`claude-root/`、`version/` 与 workspace 并列、勿改。
  - 种子：仓库内 `docker/runtime-volume-seeds/`（镜像运行卷）——`governor-workspace/`、预制业务 Agent `data/business-agents/<id>/workspace/`（当前 `main-agent`）、新业务 Agent 创建模板 `templates/business-agent/<template_id>/`。
- 只说「业务 Agent」而无法唯一定位时先提问，不得默认改 `main-agent`。

### 2. 路径边界检查

允许目标解析到以下其一：

- `${RUNTIME_ROOT}/data/business-agents/<agent_id>/workspace/`（含预制 main-agent；可编辑配置：`CLAUDE.md`、`.claude/`、`.mcp.json` 等；**勿改**并列的 `claude-root/`、`version/`）
- 仓库 `docker/runtime-volume-seeds/`（`governor-workspace/`、预制 `data/business-agents/<id>/workspace/`、`templates/business-agent/`）
- 用户明确给出的业务 Agent workspace 绝对路径

默认拒绝（即使在允许根之下也不得改）：

- 任意 `.../version/`（业务 Agent 的 per-agent git 版本库，B3；直接改会破坏版本治理）
- 任意 `.../claude-root/`、`claude-roots/`（运行态 Claude 状态）
- `data/runtime.sqlite3`、`data/agent-governance/`（worktrees/releases）、`data/outputs/`、`data/transcripts/`、`data/uploads/`、`langfuse/`、`.git/`
- `.env*`、`.mcp.local.json`、`settings.local.json`、`CLAUDE.local.md`、`secrets/`

注：业务 Agent workspace 在 `data/` 下，故不能整目录拒绝 `data/`；但 `data/` 和 `data/business-agents/` 父目录本身也不是优化目标，只能改已确认的单个 workspace，并拒绝上面列出的子路径。

### 3. 现状读取

- 读 `CLAUDE.md`、`.mcp.json`、`.claude/settings.json`。
- 查 `.claude/skills/`、`.claude/agents/`、`.claude/rules/`、hooks、commands、evals、templates。
- 产出简短资产清单：已有能力 / 工具 / 权限边界 / 缺口。

### 4. 需求拆解（按资产类别）

- prompt / 角色边界 → `CLAUDE.md`
- 能力流程 → `.claude/skills/<skill>/SKILL.md`
- 子角色 → `.claude/agents/*.md`
- 工具接入 → `.mcp.json`（同步 `.claude/settings.json` 权限）
- 强约束 → `.claude/rules/*` 或 hooks
- 验收 → evals / 示例输入 / 验证命令

### 5. 直接修改

- 只改目标 workspace 内资产，不跨 Agent。
- 不把业务 Agent 私有配置写入 `docker/runtime-volume-seeds`。
- 改前输出：目标路径 + 预计修改文件清单。
- 回滚依据：repo-tracked（模板）用 `git diff`；运行态用文件清单 + 变更摘要，高风险修改先备份到 `/tmp/agentgov-workspace-optimizer-backups/<timestamp>/`。
- 运行态业务 Agent workspace 同时是其 git 版本源（B3 `GitAgentVersionStore`）：直接改会在该仓库工作树留未提交改动；不要碰 `version/`；如需固化为版本，提示用户走现有 change set / release 流程。

### 6. 修改后验证

- JSON：`.mcp.json`、`.claude/settings.json` 可解析。
- Markdown：`SKILL.md` 有合法 frontmatter（`name` / `description`）。
- 权限模型：通用业务 Agent 的项目基线把 `Bash(*)` 放在 `ask`，仅将经过审计的具体低风险 Bash 规则放入 `allow`；run 级授权必须按低风险类别隔离，高风险或未分类请求不得整轮放行。`security-operations-expert` 是专用例外：只有 RO `approved_execution` 下的精确 `soc_api__create` / `soc_api__manual` 进入逐次 `allow_once`，其他处置 mutation 与 `AskUserQuestion` 均拒绝。普通优化不得随意放大 allow。
- 安全：无 api_key / token / Authorization / header / 数据库凭据 / 本机绝对私有路径。
- 模板改动：复用 `make runtime-volume-seeds-scan`（`scripts/runtime_template_safety.py` 的 `scan_path` / `sanitize_path`）做脱敏扫描，不要另写一套扫描。
- 运行态生效：修改 seed 后通过 `make up` 走 API 启动协调器；本机调试用 `make local-debug-bootstrap`。已有 workspace 的受管策略只有在 Git 工作树干净且无未终结 change set 时才自动迁移并生成 Git 快照；不得绕过 receipt/租约直接运行旧 repair/reconcile 脚本。
- 报告：输出「已改文件 / 未改文件 / 需人工配置项 / 验证结果 / 后续启动或渲染步骤」。

## 为什么不走产品 change set

本任务是开发者离线工作流，优化的是 workspace 配置资产本身；先用 skill 收敛流程比先改产品 API 成本低。若该流程稳定，再升级为产品内「业务 Agent 配置变更集」能力。

## 与其他治理 skill 的关系

- 涉及 MCP / env / volume / 本机 vs 容器边界时，按 `runtime-env-governance` 的 Consumer × Mode × Boundary 口径。
- 本技能为 `.codex` / `.claude` 镜像同源，改动两侧同步；镜像范围由 `check_docs_governance.py` 自动发现。
