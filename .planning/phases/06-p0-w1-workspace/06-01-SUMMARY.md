---
phase: 06-p0-w1-workspace
plan: 01
subsystem: business-agent-workspace
tags: [claude-hooks, workspace, fail-closed, runtime-bootstrap, security]

requires:
  - phase: roadmap-v3.1
    provides: P0-W1 准入需求 P0W-01 至 P0W-08 与阶段边界
provides:
  - 高风险执行意图及畸形 PreToolUse 输入的结构化 deny
  - 受批准 runtime data 根约束的最小化 PostToolUse 审计
  - 与当前权限、路径、职责和身份权威一致的 Workspace 回归测试
  - 完整 Workspace suite 与 runtime bootstrap 扫描证据
affects: [07-p0-w2-isolation, 08-p0-mcp, business-agent-workspace]

tech-stack:
  added: []
  patterns:
    - Claude hook 安全边界对畸形输入统一结构化 fail-closed
    - 运行时审计路径必须由批准 data 根证明且审计内容最小化

key-files:
  created: []
  modified:
    - docker/runtime-bootstrap/business-agents/security-operations-expert/workspace/hooks/pre_tool_guard.py
    - docker/runtime-bootstrap/business-agents/security-operations-expert/workspace/hooks/post_tool_audit.py
    - docker/runtime-bootstrap/business-agents/security-operations-expert/workspace/tests/test_hooks.py
    - docker/runtime-bootstrap/business-agents/security-operations-expert/workspace/tests/test_native_config.py

key-decisions:
  - "保留 Runtime 现有 CLAUDE_HOOK_AUDIT_LOG 契约，但只接受等于 DATA_DIR/transcripts/claude-hook-audit.jsonl 的路径。"
  - "安全 Bash、非 Bash 与 MCP 工具不由 hook 返回 allow，继续由 Claude 原生权限流裁决。"
  - "实际 58 个测试 leaf 只作为本次回执，不写入长期测试契约。"

patterns-established:
  - "Hook boundary: 解析失败、错误顶层类型、非法 tool_input 与缺失 Bash command 走同一结构化 deny。"
  - "Audit boundary: 日志路径必须由 DATA_DIR 或严格 runtime Workspace 布局推导，无法证明时拒绝写入。"

requirements-completed: [P0W-01, P0W-02, P0W-03, P0W-04, P0W-05, P0W-06, P0W-07, P0W-08]

duration: 72h
completed: 2026-08-09
---

# Phase 6：P0-W1 安全 Workspace 基线修复总结

> 2026-08-11 边界更新：下述数字是 Phase 6 当时的历史回执，不代表当前候选。
> 当前内置 Agent 不启用文件读取、shell、Web 或 MCP；业务 Workspace tests 仅通过
> `make container-workspace-pytest-test` 的 exact-commit 隔离 lane 执行，不进入宿主 root collection。

**内置安全 Workspace 的高风险执行分类 hook、审计路径与原生配置回归已收口，完整 58-leaf suite 和仓库初始化源扫描均通过。**

## 执行数据

- **开始：** 2026-08-06T10:08:08+08:00
- **完成：** 2026-08-09T10:08:11+08:00
- **耗时：** 72h（含两轮串行全量测试、对抗性复审与跨轮续作）
- **任务：** 3/3
- **代码文件：** 4
- **代码净差异：** +287 / -47

## 交付结果

- `PreToolUse` 对非 JSON、错误 JSON 顶层、非法 `tool_input`、Bash 缺失/空白/非字符串 `command` 以及已分类高风险执行意图统一返回 Claude 可识别的结构化 `deny`，不再依赖异常或历史退出码作为安全契约。
- 破坏性文件系统操作、可用性中断、编排状态破坏、容器环境全局清理和外部远程访问等风险类别得到覆盖；低风险 Bash、仅含风险关键词但无副作用的文本、非 Bash 与 MCP 工具继续进入 Claude 原生权限流程。持久文档不保存可执行高风险样本。
- `PostToolUse` 只允许写入批准 data 根下的 `transcripts/claude-hook-audit.jsonl`；任意日志文件名、越界路径或无法证明的目录布局均 fail-closed。
- 审计记录只保留时间、事件、工具名、输入字段名等最小元数据，不持久化 `tool_input` 或 `tool_response` 值。
- 原生配置测试已与当前 `.claude/settings.json`、`CLAUDE.md` 和 `agent.yaml.agent.id` 权威同步，没有通过改写权限配置迁就测试。

## 提交

三个相互依赖的任务共享同一组 hook 与回归文件，因此作为一个聚焦代码提交交付：

1. **Task 1–3：安全 hook、审计路径及原生配置回归收口** — `eff4762`
2. **对抗性复审修正：阻断等价 Bash 绕过并收口 PostToolUse 畸形输入** — `be02782`

**计划元数据：** `7f4f07b`

## 文件变更

- `hooks/pre_tool_guard.py`：统一畸形输入与高风险执行意图的结构化拒绝边界。
- `hooks/post_tool_audit.py`：验证批准 data 根、固定审计文件及最小化记录。
- `tests/test_hooks.py`：补齐高风险/低风险类别、畸形输入、路径越界和敏感值不落盘回归。
- `tests/test_native_config.py`：改为验证当前只读权限、绝对输出路径、职责所有权与唯一 manifest identity。

## 关键决策

- 没有删除 Runtime 当前注入的 `CLAUDE_HOOK_AUDIT_LOG`。运行时实现与既有测试证明它仍是活跃契约，因此将其收紧为“只能等于批准 data 根下固定日志文件”，既保留运行兼容性，也关闭任意路径旁路。
- Hook 只负责拒绝已识别高风险执行意图，不返回显式 `allow`；未命中输入仍由 Claude 原生权限、deny 和 HITL 边界裁决。
- Workspace 测试 leaf 数量是阶段证据，不是永久产品契约；对抗性用例补强后的本次实际结果为 `58 passed`。

## 与计划的偏差

### 自动修正

**1. 保留并限制活跃审计环境变量契约**

- **发现位置：** Task 2
- **问题：** 早期候选方向曾考虑移除 `CLAUDE_HOOK_AUDIT_LOG`，但 `app/runtime/claude_runtime.py` 与运行时测试证明该变量仍由平台主动注入。
- **处理：** 保留变量，只接受与 `${DATA_DIR}/transcripts/claude-hook-audit.jsonl` 完全一致的值；越界路径与任意文件名稳定拒绝。
- **验证：** 合法 Runtime 路径、越界路径、错误文件名与敏感值不落盘测试均通过。
- **提交：** `eff4762`

**2. 对抗性代码审查补齐等价高风险执行意图绕过**

- **发现位置：** 阶段代码审查
- **问题：** 首轮实现会放过路径前缀、权限包装、参数重排、链式结构等规范化变体；原测试只覆盖了单一文本形态。
- **处理：** 在同一四文件边界内收紧语义边界、包装形式与参数规范化分类，并对缺失/非法工具名和畸形 PostToolUse payload 结构化拒绝。
- **验证：** 新增高风险规范化变体与低风险帮助类反例后，完整 Workspace `58 passed`；同一候选重新通过所有仓库门。
- **提交：** `be02782`

**偏差总计：** 2 项安全/兼容性修正。

**影响：** 没有扩大文件/产品范围或形成双轨；两项修正分别补强 P0W-02 的对抗性覆盖与 P0W-06 的活跃 Runtime 契约。

## 执行中问题

- 第一次 `runtime-bootstrap-scan` 发现测试生成的 `tests/__pycache__`。仅删除该初始化源下两个精确 `.pyc` 与空目录后重跑，扫描通过；没有清理用户目录或真实运行卷。
- 第一次 `make main-flow-test` 的后端、设计一致性与资产检查均通过，但本地前端依赖中缺少可执行 `vitest`。按现有 lockfile 执行 `pnpm --dir frontend install --frozen-lockfile` 后完整重跑通过，`package.json` 与 lockfile 均无 tracked 变化。

## 验证证据

| 验证项 | 结果 |
| --- | --- |
| 完整安全 Workspace suite | `58 passed`，零失败、零 error |
| Ruff check / format check | 通过 |
| `git diff --check` | 通过 |
| `make runtime-bootstrap-scan` | 通过，`ok: true`；保留既有 `mcp__sec-ops__*` medium review，无 high finding |
| `make codex-guard` | 通过 |
| `make typecheck` | 通过，Pyright 0 error |
| `make main-flow-test` | 通过；后端 522 passed、Vitest 38/38、浏览器与设计流通过，汇总 `pytest=483 ui=11` |
| `make test` | 修正后同一候选通过；`main-full` 1347 passed、12 warnings，总覆盖率 77.46% |
| 最终初始化源扫描 | 通过，`ok: true` |

## 边界核对

- tracked 代码差异只包含计划 allowlist 的四个初始化源文件。
- 未修改 live Workspace、`${HOME}/volume-agent-gov`、迁移前 `docker/volume/`、runtime SQLite、`.env*`、版本号、Docker 卷布局、公开 API、OpenAPI 或前端类型。
- 未实现 Phase 7 的 exact-commit runner、隔离容器或 receipt，也未把业务 Agent leaf 复制到平台根 collection。
- 用户未跟踪的 `.obsidian/` 保持原状且未进入提交。

## 用户配置

无需配置；本阶段没有新增依赖、环境变量或外部服务。

## 下一阶段准备度

- P0-W1 的初始化源已经具备可重复的安全回归基线，可作为 Phase 7 exact-commit 隔离 lane 的首个锁定候选。
- Phase 7 仍需独立解决 API 与 pytest 执行 authority 分离、精确 Git commit 物化、无凭据/无网络 sandbox、typed receipt 和彻底清理证明；本阶段没有以当前宿主机 suite 冒充这些能力。

## Self-Check

**PASSED**

- `eff4762` 与 `be02782` 存在，合计仍只修改四个计划文件。
- 所有 P0W-01 至 P0W-08 均有对应实现与自动化证据。
- 完整测试、治理门与 bootstrap fail-mode 扫描均通过。

---
*Phase: 06-p0-w1-workspace*
*Completed: 2026-08-09*
