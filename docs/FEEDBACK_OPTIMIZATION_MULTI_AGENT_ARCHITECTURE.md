# 反馈优化闭环多 Agent 架构

> 用途：指导 Claude Agent Runtime / 网络安全运营 AI 助手的反馈优化闭环编码实现。
> 状态：当前实现校准稿
> 版本：v1.1
> 日期：2026-05-26

本版本按当前代码实现校准。文档中的“当前实现”描述的是本仓库已落地能力；标注为“预留”或“待实现”的内容不得作为已完成验收项。

---

## 0. 替代声明与 Breaking Changes

本文档完全替代 `docs/FEEDBACK_OPTIMIZATION_LOOP_MVP.md`，旧版 MVP 文档删除后不再作为开发、测试、接口或产品验收依据。

不兼容变更如下：

```text
1. 旧版 POST /api/feedback 立即返回 attribution/proposal 的行为废弃。
2. 旧版 POST /api/feedback/events 直接触发规则归因或 proposal 的行为废弃。
3. 旧版 label -> attribution_type -> proposal 的确定性规则链废弃，不作为正式归因来源。
4. 旧版 /data/feedback/*.jsonl 和 /data/optimization-proposals/*.jsonl 不再作为新版接口契约。
5. 旧版 volume/workspace 和 volume/claude-root 的主目录语义废弃，新版固定使用 main-workspace 和 claude-roots/*。
6. 旧版前端“提交反馈后直接展示归因和 proposal 摘要”的交互废弃。
7. 旧数据不做自动迁移；如需保留，只能作为人工参考，不参与新版闭环计算。
8. 基于旧版 MVP 实现产生的后端接口、规则归因代码、前端入口、测试和数据目录必须在新版落地时一并清理。
9. 当前权威运行态数据存储为 /data/runtime.sqlite3；/data/feedback-signals/ 等目录仅为兼容路径或历史说明，不再作为权威存储契约。
```

新版唯一闭环如下：

```text
chat run
  -> feedback signal / SOC event
  -> feedback case
  -> evidence package
  -> attribution job
  -> proposal job
  -> optimization proposal
  -> proposal approval
  -> optimization task
  -> manual/external patch application
  -> mark-applied snapshot
  -> main-workspace version
  -> manual regression evaluation
```

---

## 1. 文档目标

本文档用于统一反馈优化闭环多 Agent 架构的实现口径，避免在编码过程中出现目录命名、运行态隔离、版本管理、权限边界、数据流、API、前端展示等方面的歧义、矛盾和冲突。

本文档明确以下问题：

1. 多个 Agent 如何在同一个容器内运行。
2. 主 Agent、归因 Agent、建议 Agent 的职责边界。
3. 为什么不共享 workspace 和 claude-root。
4. 如何通过 Runtime Profile 指定不同的 `cwd`、`HOME`、`CLAUDE_CONFIG_DIR`。
5. 哪些目录进入版本管理，哪些目录只作为运行态。
6. 反馈、Trace、工具调用、SOC 操作如何固化为 evidence package。
7. 归因分析和优化建议如何形成结构化结果。
8. 哪些建议可以进入优化任务，哪些必须成为外部治理建议或人工分析项。
9. 前后端需要实现哪些 API、数据结构和验证逻辑。

---

## 2. 核心结论

最终采用以下架构：

```text
同一个 API 容器
  同一个 FastAPI Runtime 服务
    主 Agent Claude Code 子进程
    归因分析 Agent Claude Code 子进程
    优化建议 Agent Claude Code 子进程
    执行优化 Agent，未实现，作为后续扩展
```

不是：

```text
三个 Agent = 三个容器
```

而是：

```text
当前已实现三个 Agent = 三套 Runtime Profile；execution-optimizer 仅保留命名和扩展位
```

核心规则如下：

```text
1. 三个 Agent 可以运行在同一个容器中。
2. 三个 Agent 可以由同一个 FastAPI 后端进程调度。
3. 三个 Agent 不共享 workspace。
4. 三个 Agent 不共享 claude-root。
5. 三个 Agent 必须使用不同的 cwd。
6. 三个 Agent 必须使用不同的 HOME。
7. 三个 Agent 必须使用不同的 CLAUDE_CONFIG_DIR。
8. 三个 Agent 可以共享 /data，但必须按 job_id、case_id、agent role 分区。
9. 主 Agent 是被反馈、被优化、被版本管理的主要对象。
10. 归因 Agent 和建议 Agent 只输出结构化结果，不直接修改主 Agent workspace。
```

---

## 3. 统一命名

本文档统一使用以下命名，后续代码、配置、目录、API、UI 文案应保持一致。

| 名称 | 含义 | 是否可变 |
| --- | --- | --- |
| `main` | 主 AI 助手 profile | 不建议变 |
| `feedback-attribution` | 归因分析 Agent profile | 不建议变 |
| `feedback-proposal` | 优化建议 Agent profile | 不建议变 |
| `execution-optimizer` | 执行优化 Agent profile，当前未实现 | backlog |
| `main-workspace` | 主 Agent workspace | 固定使用 |
| `attribution-workspace` | 归因 Agent workspace | 固定使用 |
| `proposal-workspace` | 建议 Agent workspace | 固定使用 |
| `claude-roots/main` | 主 Agent claude root | 固定使用 |
| `claude-roots/attribution` | 归因 Agent claude root | 固定使用 |
| `claude-roots/proposal` | 建议 Agent claude root | 固定使用 |
| `feedback case` | 一组反馈和关联证据形成的处置单 | 固定使用 |
| `evidence package` | 针对某个 case 固化后的证据包 | 固定使用 |
| `attribution job` | 归因分析任务 | 固定使用 |
| `proposal job` | 优化建议生成任务 | 固定使用 |
| `optimization proposal` | 待审批优化建议 | 固定使用 |
| `optimization task` | 已审批后的执行任务 | 固定使用 |
| `agent version` | Agent 行为包版本 | 固定使用 |

禁止再使用以下模糊叫法：

```text
workspace        # 应改为 main-workspace，除非指通用概念
root             # 应明确为 claude-root 或 container root
分析目录          # 应明确是 attribution-workspace 还是 job evidence 目录
建议目录          # 应明确是 proposal-workspace 还是 proposal job 输出目录
Agent 目录        # 应明确是 workspace、claude-root、job 目录还是版本快照目录
```

---

## 4. Agent 角色和职责

### 4.1 主 Agent：`main`

主 Agent 是面向最终用户的网络安全运营 AI 助手。

职责：

1. 处理用户聊天。
2. 进行告警研判、日志解释、攻击链分析、处置建议生成。
3. 调用安全运营数据 MCP、知识库 MCP、报告模板 MCP 等工具。
4. 产生可被反馈、追踪、复盘的 run/session/tool call 记录。
5. 作为反馈优化闭环的主要优化对象。

允许：

1. 读取主 Agent workspace 内的指令、skills、MCP 配置。
2. 调用被授权的安全运营工具。
3. 生成回答、报告和分析结论。

不允许：

1. 自行修改自己的核心配置文件，除非通过审批后的优化流程。
2. 直接写归因 Agent 或建议 Agent workspace。
3. 直接修改版本快照。

---

### 4.2 归因分析 Agent：`feedback-attribution`

归因分析 Agent 是反馈闭环中的“质量分析员 / 反馈归因员”。

职责：

1. 读取 feedback case。
2. 读取 evidence package。
3. 分析反馈背后的问题类型。
4. 判断责任边界：主 Agent、工具/MCP、Runtime、外部 SOC 流程、数据质量、用户误解等。
5. 输出结构化 attribution output。

允许：

1. 读取归因任务自己的 job 目录。
2. 读取 evidence package。
3. 读取主 Agent 当前版本快照或只读副本。
4. 调用只读型 trace / feedback / SOC 查询工具。
5. 向 attribution output 路径写结果。

不允许：

1. 直接修改 `main-workspace`。
2. 直接修改主 Agent claude-root。
3. 直接生成 optimization task。
4. 直接审批 proposal。
5. 把证据不足的问题包装成确定性结论。

---

### 4.3 优化建议 Agent：`feedback-proposal`

优化建议 Agent 是反馈闭环中的“Agent 配置架构师 / 优化建议设计师”。

职责：

1. 读取 attribution output。
2. 读取 evidence package 摘要。
3. 读取主 Agent 当前版本 manifest 和必要文件片段。
4. 判断问题是否可通过主 Agent workspace 修改解决。
5. 输出待审批 optimization proposal。
6. 对不可自动优化的问题输出 external guidance。

允许：

1. 读取 proposal job 输入目录。
2. 读取 attribution output。
3. 读取主 Agent 版本快照中的指定文件片段。
4. 向 proposal output 路径写结构化建议。

不允许：

1. 直接修改 `main-workspace`。
2. 直接修改 `.mcp.json`、`CLAUDE.md`、skills 等目标文件。
3. 直接审批 proposal。
4. 直接创建新 Agent 版本。
5. 将外部 MCP、SOC 系统、Runtime bug 问题伪装成主 Agent skill 修改。

---

### 4.4 执行优化 Agent：`execution-optimizer`，backlog

当前实现没有 `execution-optimizer` profile、workspace、API 或自动 patch 执行入口。它只作为后续扩展命名保留，不能在 UI 或 API 文案中描述为已实现能力。

当前已实现的优化执行方式是：

1. 已审批 proposal 创建 `optimization task`。
2. 开发人员或外部系统按 proposal 手动/外部 patch 修改目标文件。
3. 在 UI 或 API 中调用 `mark-applied`，系统创建主 Agent 版本快照。
4. 开发人员手动触发回归评估。

未来如实现 `execution-optimizer`，必须满足：

```text
1. proposal 状态为 approved。
2. target_path 在允许修改范围内。
3. 当前主 Agent 版本与 proposal 生成时版本一致，或冲突已人工确认。
4. 执行前已创建快照。
5. Agent 只在受控范围内修改主 Agent workspace。
6. 生成 diff、运行回归验证、生成新主 Agent 版本并提供回滚点。
```

---

## 5. 容器目录结构

最终目录结构如下：

```text
docker/volume/
  main-workspace/                  # 主 Agent workspace，被优化对象
    CLAUDE.md
    CLAUDE.local.md                # 可选，本地私有，不建议进入正式版本
    agent.yaml
    .mcp.json
    .worktreeinclude
    .claude/
      settings.json
      settings.local.json          # 可选，本地私有，不建议进入正式版本
      agents/
      skills/
      commands/
      rules/
      output-styles/
      hooks/
    hooks/
    mcp_servers/
    templates/
    docs/
    evals/

  attribution-workspace/           # 归因分析 Agent workspace
    CLAUDE.md
    agent.yaml
    .mcp.json
    .claude/
      settings.json
      agents/
      skills/
      commands/
      rules/
      output-styles/

  proposal-workspace/              # 优化建议 Agent workspace
    CLAUDE.md
    agent.yaml
    .mcp.json
    .claude/
      settings.json
      agents/
      skills/
      commands/
      rules/
      output-styles/

  claude-roots/
    main/                          # 主 Agent HOME
      .claude/                     # 主 Agent CLAUDE_CONFIG_DIR
    attribution/                   # 归因 Agent HOME
      .claude/                     # 归因 Agent CLAUDE_CONFIG_DIR
    proposal/                      # 建议 Agent HOME
      .claude/                     # 建议 Agent CLAUDE_CONFIG_DIR

  data/
    runtime.sqlite3                # 权威运行态数据库
    .runtime-tmp/
      jobs/<job_id>/...            # Agent job 临时 workspace，完成后可清理
    external-governance-webhooks.yaml
    agent-versions/
      main/
        current.json
        versions.jsonl
        manifests/<version_id>.json
        bundles/<version_id>.tar.gz
    feedback-signals/              # 兼容路径，不是权威存储
    soc-events/                    # 兼容路径，不是权威存储
    feedback-cases/                # 兼容路径，不是权威存储
    evidence-packages/             # 兼容路径，不是权威存储
    optimization-proposals/        # 兼容路径，不是权威存储
    optimization-tasks/            # 兼容路径，不是权威存储
```

重要规则：

```text
1. main-workspace 是主 Agent 的源码化行为包。
2. attribution-workspace 是归因 Agent 的源码化行为包。
3. proposal-workspace 是建议 Agent 的源码化行为包。
4. claude-roots/* 是运行态目录，不作为主 Agent 优化对象。
5. data 是业务数据、任务数据、证据包、版本快照和外部治理配置的挂载根目录。
6. runtime.sqlite3 是 feedback signal、SOC event、case、evidence package、job、proposal、task、eval 和 session 的权威存储。
7. .runtime-tmp/jobs 是 Agent job 临时 workspace，不是 Agent workspace，也不是长期事实源。
```

---

## 6. docker-compose 挂载

推荐挂载如下：

```yaml
services:
  api:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    volumes:
      - ./volume/main-workspace:/main-workspace
      - ./volume/attribution-workspace:/attribution-workspace
      - ./volume/proposal-workspace:/proposal-workspace
      - ./volume/claude-roots/main:/claude-roots/main
      - ./volume/claude-roots/attribution:/claude-roots/attribution
      - ./volume/claude-roots/proposal:/claude-roots/proposal
      - ./volume/data:/data
    environment:
      DATA_DIR: /data
      MAIN_WORKSPACE_DIR: /main-workspace
      ATTRIBUTION_WORKSPACE_DIR: /attribution-workspace
      PROPOSAL_WORKSPACE_DIR: /proposal-workspace
      MAIN_CLAUDE_ROOT: /claude-roots/main
      ATTRIBUTION_CLAUDE_ROOT: /claude-roots/attribution
      PROPOSAL_CLAUDE_ROOT: /claude-roots/proposal
```

禁止在容器级别固定：

```yaml
CLAUDE_CONFIG_DIR: /root/.claude
```

原因：一旦在容器级别固定，三个 Agent 容易默认共用同一套 Claude Code 运行态。

正确做法：在后端每次启动 Claude Code 子进程时，根据 profile 动态注入 `HOME` 和 `CLAUDE_CONFIG_DIR`。

---

## 7. Runtime Profile 设计

### 7.1 Profile 数据结构

后端必须引入 `AgentRuntimeProfile`。

建议 Python 结构如下：

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

AgentRole = Literal[
    "main",
    "feedback-attribution",
    "feedback-proposal",
    "execution-optimizer",
]

@dataclass(frozen=True)
class AgentRuntimeProfile:
    name: str
    role: AgentRole
    workspace_dir: Path
    claude_root: Path
    claude_config_dir: Path
    data_dir: Path
    mcp_config_path: Path
    project_settings_path: Path
    langfuse_observation_name: str
    readable_paths: tuple[Path, ...]
    writable_paths: tuple[Path, ...]
    denied_paths: tuple[Path, ...]
    allowed_mcp_servers: tuple[str, ...]
    permission_mode: str = "default"
    max_runtime_seconds: int = 300
    max_output_bytes: int = 2_000_000
```

---

### 7.2 Profile 注册表

```python
from pathlib import Path

DATA_DIR = Path("/data")

PROFILES: dict[str, AgentRuntimeProfile] = {
    "main": AgentRuntimeProfile(
        name="main",
        role="main",
        workspace_dir=Path("/main-workspace"),
        claude_root=Path("/claude-roots/main"),
        claude_config_dir=Path("/claude-roots/main/.claude"),
        data_dir=DATA_DIR,
        mcp_config_path=Path("/main-workspace/.mcp.json"),
        project_settings_path=Path("/main-workspace/.claude/settings.json"),
        langfuse_observation_name="runtime.main_agent",
        readable_paths=(Path("/main-workspace"), DATA_DIR),
        writable_paths=(DATA_DIR,),
        denied_paths=(Path("/claude-roots/attribution"), Path("/claude-roots/proposal")),
        allowed_mcp_servers=("sec-ops-data", "security-kb"),
    ),
    "feedback-attribution": AgentRuntimeProfile(
        name="feedback-attribution",
        role="feedback-attribution",
        workspace_dir=Path("/attribution-workspace"),
        claude_root=Path("/claude-roots/attribution"),
        claude_config_dir=Path("/claude-roots/attribution/.claude"),
        data_dir=DATA_DIR,
        mcp_config_path=Path("/attribution-workspace/.mcp.json"),
        project_settings_path=Path("/attribution-workspace/.claude/settings.json"),
        langfuse_observation_name="runtime.feedback_attribution_agent",
        readable_paths=(DATA_DIR,),
        writable_paths=(Path("/data/.runtime-tmp/jobs"),),
        denied_paths=(Path("/main-workspace"), Path("/claude-roots/main")),
        allowed_mcp_servers=("feedback-evidence", "readonly-trace"),
    ),
    "feedback-proposal": AgentRuntimeProfile(
        name="feedback-proposal",
        role="feedback-proposal",
        workspace_dir=Path("/proposal-workspace"),
        claude_root=Path("/claude-roots/proposal"),
        claude_config_dir=Path("/claude-roots/proposal/.claude"),
        data_dir=DATA_DIR,
        mcp_config_path=Path("/proposal-workspace/.mcp.json"),
        project_settings_path=Path("/proposal-workspace/.claude/settings.json"),
        langfuse_observation_name="runtime.feedback_proposal_agent",
        readable_paths=(DATA_DIR,),
        writable_paths=(Path("/data/.runtime-tmp/jobs"),),
        denied_paths=(Path("/main-workspace"), Path("/claude-roots/main")),
        allowed_mcp_servers=("feedback-evidence", "agent-version-store"),
    ),
}
```

注意：

```text
1. 当前实现中 profile 固定了 cwd、HOME、CLAUDE_CONFIG_DIR、settings、mcp_config、allowed_tools 和 disallowed_tools。
2. readable_paths、writable_paths、denied_paths 和 allowed_mcp_servers 是安全设计字段；当前尚未实现通用路径拦截器，只在 target_path allowlist、工具权限和独立 workspace/root 上落地。
3. 归因和建议 Agent 不直接读写 live main-workspace；如需主 Agent 信息，应读取 evidence package、SQLite 中的 job 输入、版本 manifest 或提示词嵌入上下文。
```

---

## 8. Claude Agent SDK 启动规则

### 8.1 环境变量构造

每次启动 Claude Code 子进程时，必须按 profile 注入环境变量。

```python
import os

def build_profile_env(profile: AgentRuntimeProfile) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "HOME": str(profile.claude_root),
        "CLAUDE_CONFIG_DIR": str(profile.claude_config_dir),
        "AGENT_PROFILE": profile.name,
        "CLAUDE_AGENT_SDK_CLIENT_APP": f"secops-runtime/{profile.name}",
    })
    return env
```

### 8.2 ClaudeAgentOptions 构造

```python
from claude_agent_sdk import ClaudeAgentOptions


def build_claude_options(profile: AgentRuntimeProfile) -> ClaudeAgentOptions:
    profile.claude_root.mkdir(parents=True, exist_ok=True)
    profile.claude_config_dir.mkdir(parents=True, exist_ok=True)

    return ClaudeAgentOptions(
        cwd=str(profile.workspace_dir),
        env=build_profile_env(profile),
        mcp_servers=str(profile.mcp_config_path),
        strict_mcp_config=True,
        setting_sources=["user", "project"],
    )
```

### 8.3 后端调用规则

```text
POST /api/chat
  -> 固定使用 profile = main

POST /api/feedback-cases/{id}/attribution-jobs
  -> 固定使用 profile = feedback-attribution

POST /api/feedback-cases/{id}/proposal-jobs
  -> 固定使用 profile = feedback-proposal
```

前端不允许任意传入 profile。profile 必须由后端接口语义固定映射。

---

## 9. 配置和权限边界

### 9.1 主 Agent 配置

主 Agent 配置位于：

```text
/main-workspace/
  CLAUDE.md
  agent.yaml
  .mcp.json
  .claude/settings.json
  .claude/skills/
  .claude/agents/
  .claude/commands/
  .claude/rules/
  .claude/output-styles/
```

主 Agent 可以使用真实安全运营数据 MCP，但生产处置类工具必须具备审批或 dry-run 机制。

---

### 9.2 归因 Agent 配置

归因 Agent 配置位于：

```text
/attribution-workspace/
  CLAUDE.md
  agent.yaml
  .mcp.json
  .claude/settings.json
  .claude/skills/
```

归因 Agent 默认只读。

建议 `.claude/settings.json` 中禁止：

```text
1. 写 /main-workspace
2. 写 /claude-roots/main
3. 读取 secret、token、.env、credentials
4. 调用生产变更类 MCP 工具
5. 执行危险 Bash 命令
```

---

### 9.3 建议 Agent 配置

建议 Agent 配置位于：

```text
/proposal-workspace/
  CLAUDE.md
  agent.yaml
  .mcp.json
  .claude/settings.json
  .claude/skills/
```

建议 Agent 只生成 proposal，不直接改文件。

建议 Agent 的输出必须明确：

```text
1. 建议标题
2. 归因来源
3. 可执行性 actionability
4. 目标类型 target_type
5. 目标路径 target_path
6. 修改建议 recommendation
7. 预期效果 expected_effect
8. 验证方式 validation
9. 风险 risk
10. 是否需要审批 requires_approval
```

---

### 9.4 后端强制权限

不能只依赖 Claude Code settings。

当前实现已落地：

```text
1. 后端接口固定映射 profile，不允许前端任意传入 profile。
2. 归因/建议 profile 使用独立 cwd、HOME、CLAUDE_CONFIG_DIR 和 project settings。
3. 归因/建议 profile 默认禁用写入类工具；proposal profile 不授予 Read/Grep/Glob/Bash/Edit/Write。
4. job input、raw output、validated output、error 以 SQLite 为权威存储；必要时仅临时物化到 /data/.runtime-tmp/jobs/<job_id>/。
5. proposal target_path 必须命中 main-workspace 可优化路径 allowlist，否则降级为 external guidance/needs_human_analysis。
6. external_guidance 不得创建 workspace 修改任务。
7. task mark-applied 会记录 applied_agent_version_id 并创建主 Agent 版本快照。
```

待补强：

```text
1. profile 级 readable_paths/writable_paths/denied_paths 通用路径拦截器。
2. 所有写操作统一记录 actor、profile、job_id、version_id。
3. execution-optimizer 的受控 patch 执行和 diff 审计。
```

---

## 10. 版本管理边界

### 10.1 主 Agent 版本

主 Agent 的版本管理对象是：

```text
/main-workspace 的可控行为包
```

当前实现采用 `main-workspace-managed-config-v2` 策略：对 `main-workspace` 做全量受管快照，并排除缓存、构建产物和运行态目录。以下目录通常会被纳入版本 bundle：

```text
CLAUDE.md
agent.yaml
.mcp.json
.worktreeinclude
.claude/settings.json
.claude/skills/
.claude/agents/
.claude/commands/
.claude/rules/
.claude/output-styles/
hooks/
mcp_servers/
templates/
docs/
evals/
```

不纳入主 Agent 版本：

```text
/data/
/claude-roots/main/
/claude-roots/attribution/
/claude-roots/proposal/
/attribution-workspace/
/proposal-workspace/
.cache/
.npm/
.venv/
node_modules/
dist/
.claude/projects/
.claude/sessions/
.claude/session-env/
.claude/telemetry/
.claude/backups/
.claude.json
.env
credentials
```

### 10.2 归因 Agent 和建议 Agent 版本

归因 Agent 和建议 Agent 也记录可复现版本信息，但它们不属于主 Agent 的优化版本。

当前实现不为归因/建议 workspace 创建独立 bundle，而是在每个 job 中记录 profile version snapshot：

```text
profile_name
profile_role
agent_version
claude_md_hash
skills_hash
mcp_config_hash
settings_hash
```

每个 attribution job 必须记录：

```json
{
  "attribution_agent_version": "feedback-attribution-v0.1.0",
  "profile_name": "feedback-attribution",
  "claude_md_hash": "...",
  "skills_hash": "...",
  "mcp_config_hash": "...",
  "settings_hash": "..."
}
```

每个 proposal job 必须记录：

```json
{
  "proposal_agent_version": "feedback-proposal-v0.1.0",
  "profile_name": "feedback-proposal",
  "claude_md_hash": "...",
  "skills_hash": "...",
  "mcp_config_hash": "...",
  "settings_hash": "..."
}
```

### 10.3 Runtime 版本

所有 job 还应记录：

```text
runtime_version
schema_version
main_agent_version_id
```

否则无法复现“同一条反馈为什么这次归因和上次不同”。

---

## 11. Evidence Package 设计

### 11.1 定义

Evidence package 是归因分析的事实源。

它是针对某个 feedback case 固化出来的一组证据对象，包含反馈、run、session、tool call、trace 摘要、SOC 操作事件、主 Agent 版本信息等。

归因 Agent 不应直接四处查询散落数据，而应优先读取 evidence package。

### 11.2 存储方式

```text
/data/runtime.sqlite3
  evidence_packages.manifest_json
  evidence_files.content_json

/data/.runtime-tmp/jobs/<job_id>/<job_type>/evidence/
  manifest.json
  feedback.json
  runs.json
  sessions.json
  tool_calls.json
  soc_events.json
  trace_summary.json
  main_agent_version.json
  redaction_report.json
  messages.json              # debug evidence 开启时
  agent_activity.json        # debug evidence 开启时
  langfuse_trace_refs.json   # debug evidence 开启时
```

SQLite 是权威存储；临时 evidence 文件只为 Agent job 读取而物化，job 完成后可清理。

### 11.3 Manifest Schema

```json
{
  "schema_version": "evidence-package/v1",
  "evidence_package_id": "evp-20260521-000001",
  "feedback_case_id": "fbc-20260521-000001",
  "created_at": "2026-05-21T10:00:00Z",
  "created_by": "system",
  "main_agent_version_id": "main-v1.2.0",
  "source_refs": {
    "feedback_ids": [],
    "run_ids": [],
    "session_ids": [],
    "trace_ids": [],
    "alert_ids": [],
    "case_ids": []
  },
  "included_files": [
    {
      "path": "feedback.json",
      "sha256": "...",
      "type": "feedback"
    },
    {
      "path": "tool_calls.json",
      "sha256": "...",
      "type": "tool_calls"
    }
  ],
  "redaction": {
    "enabled": false,
    "policy": "debug-evidence-raw-v1",
    "redacted_fields": ["token", "secret", "credential", "raw_payload"]
  },
  "completeness": {
    "has_feedback": true,
    "has_runs": true,
    "has_tool_calls": true,
    "has_trace_summary": true,
    "has_main_agent_version": true
  }
}
```

### 11.4 规则

```text
1. evidence package 创建后不可原地修改。
2. 当前实现对同一 feedback case 默认复用当前 evidence_package_id，防止重复点击产生脏数据。
3. 如未来需要补证据，应新增显式 regenerate/force 机制并生成新的 evidence_package_id。
4. 开发调试模式默认 `ENABLE_FEEDBACK_DEBUG_EVIDENCE=true`，允许保留 messages、agent_activity 和 Langfuse trace refs，便于开发人员排查。
5. 生产或受控环境应关闭 debug evidence，启用脱敏策略后不得包含明文 token、secret、credential。
6. trace 原文可选，trace 摘要和 trace refs 取决于运行时采集可用性。
7. 主 Agent 版本信息应尽量记录；无法获取时允许为空，但 completeness 必须反映缺失。
```

---

## 12. 反馈优化主链路

完整链路如下：

```text
用户聊天 / SOC 操作
  -> run_id / session_id / alert_id / case_id
  -> feedback signal / SOC event / pending correlation
  -> feedback case
  -> evidence package
  -> attribution job
  -> attribution output
  -> proposal job
  -> optimization proposal
  -> 人工审批
  -> optimization task
  -> 开发人员或外部系统应用 patch
  -> 标记已应用并创建新主 Agent 版本
  -> 手动触发回归验证
```

当前阶段不实现自动改文件；`execution-optimizer` 自动 patch 执行属于 backlog。

### 12.1 反馈评估集用例治理

反馈处置单沉淀出的 `eval_case` 面向开发人员调试和持续回归，允许在 UI 中直接编辑：

```text
1. 可编辑 prompt、expected_behavior、checks_json、labels 和 status。
2. 当前实现采用直接覆盖策略，不创建 revision，也不复制新的 eval_case_id。
3. status=active 的用例参与自动和批量回归；draft、archived 不参与自动选择。
4. eval_run、eval_run_item、模型回答、检查结果和历史运行摘要保持只读，不允许编辑。
```

直接覆盖会影响后续回归输入，但不会回算或修改历史回归结果。

---

## 13. Job 状态机

所有 attribution/proposal job 使用统一状态字段。当前实现直接从 `queued` 开始创建 job；`created` 和 `evidence_packaging` 是早期设计状态，不作为当前必经状态。

```text
queued
  -> running
  -> schema_validating
  -> completed
```

异常状态：

```text
failed
cancelled          # 预留
timeout            # 预留；当前超时以 failed + AGENT_TIMEOUT 表达
needs_human_review
```

### 13.1 Job 基础字段

```json
{
  "job_id": "fba-20260521-000001",
  "job_type": "attribution",
  "feedback_case_id": "fbc-20260521-000001",
  "evidence_package_id": "evp-20260521-000001",
  "status": "running",
  "profile_name": "feedback-attribution",
  "created_at": "2026-05-21T10:00:00Z",
  "started_at": "2026-05-21T10:00:03Z",
  "completed_at": null,
  "timeout_seconds": 300,
  "retry_count": 0,
  "input_path": "/data/.runtime-tmp/jobs/fba-.../attribution/input.json",
  "raw_output_path": "sqlite://feedback_jobs/fba-.../raw_output_json",
  "validated_output_path": "sqlite://feedback_jobs/fba-.../validated_output_json",
  "error_path": "sqlite://feedback_jobs/fba-.../error_json",
  "langfuse_trace_id": null
}
```

### 13.2 错误码

统一错误码：

```text
FEEDBACK_CASE_NOT_FOUND
EVIDENCE_PACKAGE_NOT_FOUND
EVIDENCE_INCOMPLETE
AGENT_PROFILE_NOT_FOUND
AGENT_TIMEOUT
AGENT_RUNTIME_ERROR
MCP_UNAVAILABLE
PERMISSION_DENIED
SCHEMA_VALIDATION_FAILED
LOW_CONFIDENCE
NO_ACTIONABLE_PROPOSAL
VERSION_CONFLICT
TARGET_PATH_NOT_ALLOWED
```

---

## 14. Attribution Job

### 14.1 输入

```json
{
  "schema_version": "attribution-input/v1",
  "job_id": "fba-20260521-000001",
  "feedback_case_id": "fbc-20260521-000001",
  "evidence_package_id": "evp-20260521-000001",
  "main_agent_version_id": "main-v1.2.0",
  "evidence_manifest_path": "/data/.runtime-tmp/jobs/fba-.../attribution/evidence/manifest.json",
  "allowed_evidence_paths": [
    "/data/.runtime-tmp/jobs/fba-.../attribution/evidence/feedback.json",
    "/data/.runtime-tmp/jobs/fba-.../attribution/evidence/tool_calls.json",
    "/data/.runtime-tmp/jobs/fba-.../attribution/evidence/trace_summary.json"
  ],
  "task": "analyze_feedback_attribution"
}
```

### 14.2 输出

```json
{
  "schema_version": "attribution-output/v1",
  "feedback_case_id": "fbc-20260521-000001",
  "attribution_job_id": "fba-20260521-000001",
  "status": "completed",
  "problem_type": "evidence_gap",
  "optimization_object_type": "skill",
  "actionability": "direct_workspace_change",
  "confidence": "medium",
  "human_review_required": true,
  "evidence_refs": [
    {
      "type": "tool_call",
      "id": "tool-001",
      "reason": "回答未引用关键查询结果"
    }
  ],
  "responsibility_boundary": {
    "owner": "main_agent_workspace",
    "reason": "主 Agent 的告警研判 skill 未要求输出关键证据链"
  },
  "rationale": "归因说明",
  "recommended_next_step": "generate_proposal"
}
```

### 14.3 枚举值

`problem_type`：

```text
evidence_gap
tool_misuse
tool_unavailable
tool_data_quality
output_style_issue
instruction_gap
skill_gap
mcp_description_gap
runtime_error
external_soc_process_issue
user_misunderstanding
insufficient_information
```

`optimization_object_type`：

```text
main_agent_claude_md
skill
subagent
mcp_config
mcp_description
output_style
eval_case
runtime_code
external_mcp_service
soc_process
not_actionable
```

`actionability`：

```text
direct_workspace_change
workspace_config_change
eval_only
external_guidance
runtime_fix
needs_human_analysis
not_actionable
```

---

## 15. Proposal Job

### 15.1 输入

```json
{
  "schema_version": "proposal-input/v1",
  "job_id": "fbp-20260521-000001",
  "feedback_case_id": "fbc-20260521-000001",
  "evidence_package_id": "evp-20260521-000001",
  "attribution_job_id": "fba-20260521-000001",
  "attribution_output_path": "/data/.runtime-tmp/jobs/fbp-.../proposal/attribution_validated_output.json",
  "main_agent_version_id": "main-v1.2.0",
  "main_agent_manifest_path": "/data/agent-versions/main/current.json",
  "allowed_target_paths": [
    "CLAUDE.md",
    ".mcp.json",
    ".claude/settings.json",
    ".claude/skills/",
    ".claude/agents/",
    ".claude/output-styles/",
    "evals/"
  ],
  "task": "generate_optimization_proposals"
}
```

### 15.2 输出

```json
{
  "schema_version": "proposal-output/v1",
  "feedback_case_id": "fbc-20260521-000001",
  "proposal_job_id": "fbp-20260521-000001",
  "status": "completed",
  "proposals": [
    {
      "proposal_id": "opp-20260521-000001",
      "title": "增强告警研判 skill 的证据链要求",
      "actionability": "direct_workspace_change",
      "target_type": "skill",
      "target_path": ".claude/skills/alert-triage/SKILL.md",
      "recommendation": "在输出规范中增加关键证据链字段，包括告警来源、关键进程、网络连接、工具查询结果引用。",
      "expected_effect": "降低证据不足类反馈，提高回答可核查性。",
      "validation": "新增 3 条证据不足类回归样例，验证回答必须包含 evidence_refs。",
      "risk": "回答长度可能增加，需要通过 output style 控制摘要长度。",
      "requires_approval": true
    }
  ],
  "external_guidance": [],
  "no_action_reason": null
}
```

### 15.3 规则

```text
1. proposal output 必须经过 JSON Schema 校验。
2. proposal 不得直接包含大段未脱敏敏感数据。
3. target_path 必须是相对 main-workspace 的路径。
4. 如果 target_path 不在 allowlist 内，proposal 必须降级为 external_guidance 或 needs_human_analysis。
5. actionability=external_guidance 时，不允许创建 workspace 修改任务。
```

---

## 16. 输出校验规则

所有 Agent 输出分为两层：

```text
raw_output_json          # Agent 原始输出，SQLite 字段
validated_output_json    # 通过 schema 校验后的结构化输出，SQLite 字段
error_json               # 校验或运行错误，SQLite 字段
```

规则：

```text
1. 后续流程只能读取 validated_output_json。
2. raw_output_json 只用于排错、审计和手动 revalidate。
3. 当前实现先尝试抽取完整 schema JSON；未命中时可启用 DSPyOutputFormatter 将 Agent 文本或片段 JSON 转换为目标 Pydantic 输出模型。
4. DSPyOutputFormatter 只能做格式转换，不能补充证据中没有的业务事实；证据不足时应输出 needs_human_review / insufficient_information。
5. DSPyOutputFormatter 输出后仍必须经过 Runtime 的 Pydantic schema 最终校验。
6. 校验失败时 job 状态改为 needs_human_review，并写入 error_json。
7. 当前 proposal job 提供手动 revalidate 接口；attribution job 提供显式 regenerate 接口。
8. schema_version 必填。
```

建议使用 Pydantic 定义 schema。

---

## 17. 反馈信号和 SOC 事件采集

### 17.1 采集定位

反馈信号和 SOC 事件采集只负责写入新版信号池，不直接归因、不直接生成 proposal、不直接创建 optimization task。

```text
POST /api/feedback-signals
  -> 写入 feedback signal pool
  -> 可关联 run/case 时允许进入 feedback case 候选池
  -> 不启动 attribution job
  -> 不生成 proposal

POST /api/soc-events
  -> 写入 SOC event pool
  -> 按关联规则匹配 run/case
  -> 无法关联时进入 pending correlation
  -> 不启动 attribution job
  -> 不生成 proposal
```

### 17.2 Feedback Signal Schema

`POST /api/feedback-signals` 用于聊天显式反馈、人工标注和系统捕捉的隐式反馈。

```json
{
  "signal_id": "可选；不传由 Runtime 生成",
  "source_type": "explicit_feedback | implicit_feedback | analyst_annotation",
  "timestamp": "2026-05-22T10:00:00Z",
  "run_id": "可选",
  "session_id": "可选",
  "alert_id": "可选",
  "case_id": "可选",
  "labels": [],
  "comment": "可选",
  "confidence": "low | medium | high",
  "auto_captured": false,
  "requires_review": false,
  "metadata": {}
}
```

规则：

```text
1. 显式用户反馈默认 auto_captured=false。
2. 隐式反馈必须 auto_captured=true。
3. 隐式反馈默认 requires_review=true。
4. 隐式反馈不得直接生成可执行 workspace 修改建议。
5. 缺少 run_id 时，必须至少提供 session_id 或 alert_id/case_id，供后续关联。
```

### 17.3 SOC Event Schema

`POST /api/soc-events` 用于外部网络安全运营系统推送有反馈价值的业务事件。

```json
{
  "event_id": "soc-case-evt-20260522-000001",
  "source_system": "sec-ops-ui",
  "event_type": "case.verdict_changed",
  "timestamp": "2026-05-22T10:00:00Z",
  "run_id": "可选",
  "session_id": "可选",
  "alert_id": "可选",
  "case_id": "可选",
  "actor_id": "可选",
  "before": {},
  "after": {},
  "entities": {
    "asset_ids": [],
    "iocs": [],
    "hostnames": []
  },
  "confidence": "medium",
  "auto_captured": true,
  "requires_review": true,
  "metadata": {}
}
```

规则：

```text
1. event_id 必须由 source_system 保证幂等。
2. 重复 event_id 返回已存在记录，不重复写入、不重复关联。
3. SOC event 默认 auto_captured=true、requires_review=true。
4. SOC event 只能进入 evidence package，不能直接生成 proposal。
5. before/after 在生产模式应只保留归因所需字段，不保存密钥、凭据、MCP header 或大段原始日志；开发调试模式允许保留更完整证据，依赖环境开关控制。
```

### 17.4 SOC 事件白名单

只采集有明确反馈价值的业务事件。

```text
case.verdict_changed
case.severity_changed
recommendation.accepted
recommendation.rejected
recommendation.modified
evidence.added
tool.manual_query_after_agent
```

不采集：

```text
普通点击
页面浏览
表格排序
筛选切换
鼠标悬停
无业务语义的打开/关闭面板动作
```

### 17.5 关联规则

采集后按以下优先级关联 run/case：

```text
1. run_id 精确关联。
2. session_id + alert_id/case_id 关联。
3. alert_id/case_id + 时间窗口关联。
4. IOC / asset / hostname 等实体相似关联。
5. 仍无法关联则进入 pending correlation。
```

`pending correlation` 不能直接进入 attribution job。必须先经过人工确认或后续事件补齐关联信息，形成 feedback case 后才允许生成 evidence package。

### 17.6 新版数据路径

当前实现的数据层以以下路径为准：

```text
/data/runtime.sqlite3
/data/.runtime-tmp/jobs/
/data/agent-versions/main/current.json
/data/agent-versions/main/versions.jsonl
/data/agent-versions/main/manifests/
/data/agent-versions/main/bundles/
/data/external-governance-webhooks.yaml
```

`/data/feedback-signals/`、`/data/soc-events/`、`/data/pending-correlations/`、`/data/feedback-cases/`、`/data/evidence-packages/`、`/data/optimization-proposals/`、`/data/optimization-tasks/` 等目录仅为兼容路径或历史说明，不是权威事实源。旧版 `/data/feedback/*.jsonl` 和旧版规则生成的 proposal 不参与新版闭环。

---

## 18. API 设计

### 18.1 Feedback Signal / Agent Run

```text
GET  /api/agent-runs
POST /api/feedback-signals
GET  /api/feedback-signals
GET  /api/feedback-signals/{signal_id}
```

### 18.2 SOC Event

```text
POST /api/soc-events
GET  /api/soc-events
GET  /api/soc-events/{event_id}
```

### 18.3 Feedback Case

```text
POST /api/feedback-cases
GET  /api/feedback-cases/{feedback_case_id}
GET  /api/feedback-cases
```

### 18.4 Evidence Package

```text
POST /api/feedback-cases/{feedback_case_id}/evidence-packages
GET  /api/evidence-packages/{evidence_package_id}
GET  /api/evidence-packages/{evidence_package_id}/files/{file_name}
```

### 18.5 Attribution Job

```text
POST /api/feedback-cases/{feedback_case_id}/attribution-jobs
POST /api/feedback-cases/{feedback_case_id}/attribution-jobs/regenerate
GET  /api/feedback-analysis/jobs/{job_id}
GET  /api/feedback-analysis/jobs/{job_id}/attribution
```

### 18.6 Proposal Job

```text
POST /api/feedback-cases/{feedback_case_id}/proposal-jobs
POST /api/feedback-cases/{feedback_case_id}/proposal-jobs/regenerate
GET  /api/feedback-analysis/jobs/{job_id}/proposal
POST /api/feedback-analysis/jobs/{job_id}/proposal/revalidate
```

### 18.7 Optimization Proposal

```text
GET  /api/optimization-proposals
GET  /api/optimization-proposals/{proposal_id}
POST /api/optimization-proposals/{proposal_id}/approve
POST /api/optimization-proposals/{proposal_id}/reject
POST /api/optimization-proposals/{proposal_id}/request-more-analysis
```

### 18.8 Optimization Task

```text
GET  /api/optimization-tasks
POST /api/optimization-proposals/{proposal_id}/tasks
GET  /api/optimization-tasks/{task_id}
POST /api/optimization-tasks/{task_id}/mark-applied
POST /api/optimization-tasks/{task_id}/regression-runs
GET  /api/optimization-tasks/{task_id}/regression-runs
```

### 18.9 External Governance

```text
GET  /api/external-governance-webhooks
GET  /api/external-governance-items
POST /api/external-governance-items/{external_item_id}/notify
```

### 18.10 Feedback Eval

```text
POST /api/eval-datasets/feedback/sync
GET  /api/eval-cases
PATCH /api/eval-cases/{eval_case_id}
POST /api/eval-runs
GET  /api/eval-runs
GET  /api/eval-runs/{eval_run_id}
```

### 18.11 Agent Versions

```text
GET  /api/agent-versions/main/current
GET  /api/agent-versions/main
GET  /api/agent-versions/main/{version_id}
POST /api/agent-versions/main/snapshots
POST /api/agent-versions/main/{version_id}/rollback
GET  /api/agent-versions/main/diff
```

---

## 19. 前端工作台展示要求

Feedback 工作台必须能清楚展示四层关系：

```text
反馈信息
  -> feedback signal / SOC event / pending correlation
  -> 反馈处置单
    -> 证据包
      -> 归因分析
        -> 优化建议
          -> 审批
            -> 优化任务
              -> 新 Agent 版本
```

Playground 回复上的反馈按钮只提交 feedback signal，不展示最终归因或 proposal。Feedback 工作台负责从信号池创建处置单、启动证据包和 job，并展示完整闭环状态。

### 19.1 反馈信息页面

必须展示：

```text
1. feedback signal 列表。
2. SOC event 列表。
3. pending correlation 列表。
4. run/session/alert/case 关联状态。
5. 批量选择信号创建 feedback case 的入口。
```

### 19.2 反馈处置单页面

必须展示：

```text
1. feedback_case_id
2. 包含的显式反馈
3. 包含的隐式 SOC 操作事件
4. 关联 run/session/trace/tool call
5. 当前 evidence package 状态
6. 当前归因 job 状态
7. 当前 proposal job 状态
```

### 19.3 归因分析页面

必须展示：

```text
1. 问题类型 problem_type
2. 责任边界 responsibility_boundary
3. 可优化对象 optimization_object_type
4. 可执行性 actionability
5. 置信度 confidence
6. 证据引用 evidence_refs
7. 是否需要人工复核 human_review_required
```

### 19.4 优化建议审批页面

必须展示：

```text
1. 建议来源 feedback_case_id
2. 归因摘要
3. 目标类型 target_type
4. 目标文件 target_path
5. 修改建议 recommendation
6. 预期效果 expected_effect
7. 验证方式 validation
8. 风险 risk
9. 审批动作：批准、拒绝、要求补充分析、转外部治理
```

### 19.5 外部治理建议展示

当问题属于外部 MCP、SOC 流程、数据质量或 Runtime bug 时，UI 必须明确标记：

```text
该建议不能自动修改主 Agent workspace。
```

并展示责任对象：

```text
external_mcp_service
soc_process
runtime_code
data_quality
needs_human_analysis
```

外部治理通知采用人工分发：

```text
1. 多个外部系统通过 /data/external-governance-webhooks.yaml 配置 webhook alias。
2. UI 在 external_guidance 卡片中由开发人员手动选择 alias 后发送。
3. 通知记录必须落库，包含目标 alias、HTTP 状态、响应摘要和失败原因。
4. 第一版不做 owner 到 webhook 的自动智能分发。
```

---

## 20. 实施计划

### Phase 1：旧版 MVP 实现清理

清理范围：

```text
1. 删除旧版 POST /api/feedback 和 POST /api/feedback/events 的正式实现路径。
2. 删除旧版 label -> attribution_type -> proposal 的规则归因生成链路。
3. 删除旧版前端“提交反馈后直接展示归因和 proposal 摘要”的入口和状态。
4. 删除或重写只验证旧版行为的测试。
5. 清理 /data/feedback、旧版 /data/optimization-proposals/*.jsonl 等旧数据目录；如需留档，只允许人工备份到新版闭环之外。
6. 清理 README、API 文档、UI 文案中对旧版接口语义的引用。
```

成功标准：

```text
1. 项目 active 代码不再暴露旧版反馈归因闭环行为。
2. 旧数据不会被新版 evidence package、attribution job、proposal job 自动读取。
3. grep 检查不到旧接口作为正式接口的文档描述。
4. 测试只覆盖新版 signal/event/case/job/proposal 链路。
```

### Phase 2：目录和配置骨架

创建目录：

```text
docker/volume/main-workspace/
docker/volume/attribution-workspace/
docker/volume/proposal-workspace/
docker/volume/claude-roots/main/
docker/volume/claude-roots/attribution/
docker/volume/claude-roots/proposal/
docker/volume/data/runtime.sqlite3
docker/volume/data/.runtime-tmp/jobs/
docker/volume/data/agent-versions/main/
```

成功标准：

```text
1. 三个 workspace 都有 CLAUDE.md、agent.yaml、.mcp.json、.claude/settings.json。
2. docker-compose 挂载路径全部使用 main-workspace。
3. 不再出现 volume/workspace 作为主目录。
4. 反馈信号、SOC 事件、待关联事件、case、job、proposal、task、eval 和 session 使用 SQLite 权威存储。
```

### Phase 3：反馈信号和 SOC 事件采集

实现：

```text
1. FeedbackSignalStore。
2. SocEventStore。
3. pending correlation 存储。
4. event_id 幂等。
5. run/session/alert/case 关联规则。
6. 采集 API 不触发 attribution/proposal。
```

成功标准：

```text
1. POST /api/feedback-signals 只写入 signal pool。
2. POST /api/soc-events 对 event_id 幂等。
3. 无法关联的事件进入 pending correlation。
4. 隐式信号默认 requires_review=true。
```

### Phase 4：Runtime Profile 支持

实现：

```text
1. AgentRuntimeProfile。
2. ProfileRegistry。
3. build_profile_env。
4. build_claude_options。
5. API 与 profile 固定映射。
```

成功标准：

```text
1. main profile 可正常聊天。
2. feedback-attribution profile 可启动并读取 attribution-workspace。
3. feedback-proposal profile 可启动并读取 proposal-workspace。
4. 三个 profile 的 CLAUDE_CONFIG_DIR 不同。
```

### Phase 5：Evidence Package

实现：

```text
1. EvidencePackageStore。
2. evidence manifest。
3. 开发调试证据策略和生产脱敏策略。
4. hash 校验。
5. evidence package 创建 API。
```

成功标准：

```text
1. 每个 feedback case 可以生成 evidence package。
2. evidence package 可重复读取。
3. debug evidence 开启时可保留完整证据用于开发调试；关闭后应脱敏 token、secret、credential。
```

### Phase 6：Attribution Job

实现：

```text
1. AttributionJobStore。
2. attribution input 生成。
3. 调用 feedback-attribution profile。
4. raw output 保存。
5. schema 校验。
6. validated output 保存。
```

成功标准：

```text
1. 输入 feedback_case_id 可以生成 attribution job。
2. job 输出 attribution-output/v1。
3. 输出包含 problem_type、optimization_object_type、actionability、evidence_refs。
```

### Phase 7：Proposal Job

实现：

```text
1. ProposalJobStore。
2. proposal input 生成。
3. 调用 feedback-proposal profile。
4. proposal schema 校验。
5. 写入 SQLite 中的 optimization_proposals 数据。
```

成功标准：

```text
1. proposal 明确 target_path 或 external_guidance。
2. external_guidance 不会生成 workspace 修改任务。
3. direct_workspace_change 必须 target_path 合法。
```

### Phase 8：版本管理收敛

实现：

```text
1. 主 Agent 版本只针对 main-workspace 可控行为包。
2. 归因 Agent 和建议 Agent 在 job 中记录 profile version snapshot。
3. 每个 job 记录 main_agent_version_id、profile_version、attribution/proposal_agent_version、runtime_version。
```

成功标准：

```text
1. 主 Agent 快照不包含 claude-root。
2. 主 Agent 快照不包含运行态、cache、sessions、telemetry。
3. proposal 可关联生成时的主 Agent 版本。
```

### Phase 9：Feedback 工作台集成

实现：

```text
1. 反馈处置单详情。
2. feedback signal / SOC event / pending correlation 展示。
3. evidence package 展示。
4. 归因结果展示。
5. proposal 审批页面。
6. external guidance 展示。
7. 版本关联展示。
```

成功标准：

```text
用户能回答：哪些反馈 -> 哪些证据 -> 哪些归因 -> 哪些建议 -> 是否审批 -> 修改了哪个版本。
```

---

## 21. 测试计划

### 21.1 后端单元测试

```text
1. profile 加载测试。
2. profile 路径隔离测试。
3. build_profile_env 测试。
4. ClaudeAgentOptions 构造测试。
5. feedback signal 写入测试。
6. SOC event event_id 幂等测试。
7. pending correlation 测试。
8. 隐式信号 requires_review 默认值测试。
9. 采集 API 不生成 attribution/proposal 测试。
10. target_path allowlist 测试。
11. target_path 非法路径拒绝测试。
12. evidence package manifest 校验测试。
13. attribution output schema 校验测试。
14. proposal output schema 校验测试。
15. job 状态机测试。
```

### 21.2 集成测试

```text
1. 主 Agent 聊天正常。
2. 提交 feedback signal。
3. 推送 SOC event。
4. 创建 feedback case。
5. 创建 evidence package。
6. 启动 attribution job。
7. 启动 proposal job。
8. 审批 proposal。
9. 创建 optimization task。
10. 生成新主 Agent 版本。
11. 回归验证通过。
```

### 21.3 安全测试

```text
1. 归因 Agent 尝试写 main-workspace，应失败。
2. 建议 Agent 尝试写 main-workspace，应失败。
3. proposal target_path 指向 .env，应失败。
4. debug evidence 关闭时，evidence package 包含 secret 应脱敏或拒绝创建。
5. external_guidance 不得创建 workspace 修改任务。
```

---

## 22. 明确禁止事项

禁止以下实现方式：

```text
1. 三个 Agent 共用 /root/.claude。
2. 三个 Agent 共用同一个 CLAUDE_CONFIG_DIR。
3. 三个 Agent 共用同一个 workspace。
4. 用 job 目录代替 attribution-workspace 或 proposal-workspace。
5. 建议 Agent 直接修改 main-workspace。
6. 归因 Agent 直接生成 optimization task。
7. 把 Langfuse 当成唯一事实源。
8. 把 claude-root 整体纳入主 Agent 版本。
9. 把 session/cache/telemetry/credentials 纳入版本快照。
10. 把外部 MCP 服务问题包装成主 Agent 自动优化任务。
11. 前端允许用户任意选择 profile。
12. 未经过 schema 校验的 raw output 进入下一步。
13. 未经审批的 proposal 修改主 Agent 文件。
```

---

## 23. 编码落地检查清单

开发完成后，必须逐项确认：

```text
[ ] 旧版 POST /api/feedback 和 POST /api/feedback/events 不再作为正式接口暴露。
[ ] 旧版规则归因和即时 proposal 生成代码已删除。
[ ] 旧版前端反馈摘要入口已删除或改为新版 signal 采集。
[ ] 旧版数据目录不会被新版闭环读取。
[ ] 旧版测试已删除或改写为新版链路测试。
[ ] docker/volume/main-workspace 已替代 workspace。
[ ] attribution-workspace 和 proposal-workspace 已创建。
[ ] claude-roots/main、attribution、proposal 已创建。
[ ] 三个 profile 的 cwd 不同。
[ ] 三个 profile 的 HOME 不同。
[ ] 三个 profile 的 CLAUDE_CONFIG_DIR 不同。
[ ] 后端实现 AgentRuntimeProfile。
[ ] 后端接口固定映射 profile，不由前端任意传入。
[ ] POST /api/feedback-signals 只采集信号，不生成 proposal。
[ ] POST /api/soc-events 对 event_id 幂等。
[ ] pending correlation 已实现。
[ ] evidence package 已实现 manifest、hash、debug evidence 开关和生产脱敏策略。
[ ] attribution output 已实现 schema 校验。
[ ] proposal output 已实现 schema 校验。
[ ] proposal target_path 已实现 allowlist。
[ ] external_guidance 不能创建修改任务。
[ ] 主 Agent 版本只包含 main-workspace 可控行为包。
[ ] 归因 Agent 和建议 Agent 的自身版本已记录。
[ ] UI 能展示反馈信号、SOC 事件、待关联、证据、归因、建议、审批、版本关系。
[ ] 文档明确 execution-optimizer 未实现，当前为 manual_or_patch + mark-applied + regression 流程。
```

---

## 24. 最终架构决议

最终决议如下：

```text
1. 采用“三套 Runtime Profile，同容器运行”的架构。
2. 主 Agent 使用 main-workspace。
3. 归因 Agent 使用 attribution-workspace。
4. 建议 Agent 使用 proposal-workspace。
5. 三个 Agent 使用独立 claude-root、HOME、CLAUDE_CONFIG_DIR。
6. /data 作为共享业务数据区；当前以 runtime.sqlite3 为权威存储，临时 job 文件按 job_id 分区。
7. 主 Agent 是反馈优化的主要版本管理对象。
8. 归因 Agent 和建议 Agent 有自身版本，但不纳入主 Agent 优化版本。
9. feedback signal 和 SOC event 是新版唯一采集入口。
10. 采集入口不直接归因、不直接生成 proposal。
11. evidence package 是归因事实源。
12. Langfuse 是观测和跳转工具，不是唯一事实源。
13. 归因 Agent 和建议 Agent 读取 evidence package、job input 和版本 manifest，不直接修改 main-workspace。
14. 当前不自动修改 main-workspace；优化任务由人工或外部系统应用 patch 后，通过 mark-applied 创建版本快照，并手动触发回归验证。
15. 外部 MCP、SOC 流程、Runtime bug 等问题必须通过 external_guidance、runtime_fix 或 needs_human_analysis 表达。
```

---

## 25. 一句话总结

本架构把反馈优化闭环拆成三类对象：

```text
main-workspace
  = 主 Agent 的可版本化行为源码包。

claude-roots/*
  = 各 Agent 的隔离运行态，不进入主 Agent 版本。

/data/runtime.sqlite3 + /data/.runtime-tmp/jobs + /data/agent-versions/main
  = 反馈闭环的权威数据、临时任务工作区和版本记录。
```

通过这种边界，系统可以做到：

```text
可运行、可隔离、可审计、可复现、可审批、可回滚、可持续优化。
```
