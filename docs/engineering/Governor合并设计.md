# 治理 Agent 合并为单一 Governor 设计（Issue #3 第3点）

把 attribution-analyzer、proposal-generator、execution-optimizer、eval-case-governor、
regression-impact-analyzer 五个治理 Agent 合并为单一 `governor`，workspace 合并为
`governor-workspace`，降低开发/管理/配置成本。本文是落地前的治理对象预检 + 可执行迁移设计；
按 `.claude/rules/architecture.md` 对"替换旧设计"的要求给出删除/迁移/保留清单与风险回滚。

## 治理对象预检

| 维度 | 结论 |
| --- | --- |
| 被治理对象 | 五个**治理 Agent**（闭环执行者），不是业务 Agent；main agent 与业务 Agent 不受影响 |
| 治理执行者 | 后端 `feedback_job_orchestrator` 按 job_type 选 spec → profile → prompt |
| 资产类型 | 执行资产（profile、workspace、prompt、settings/mcp 配置容器） |
| 当前边界 | 6 个 workspace（main + 5 治理）、5 套 claude_root、5 套 settings/mcp；5 个 profile 各自 builder |
| 目标边界 | main + 单一 `governor`：1 个 governor-workspace、1 套 claude_root/settings/mcp；prompt 仍按 job_type 选 |

闭环链路不变（仍是 反馈→归因→优化→评估→版本）；本次只合并"执行者身份与配置容器"，不改各 job 的 prompt 与输出契约。

风险自检：五个治理 workspace 结构同构（均 agent.yaml + CLAUDE.md + .claude/settings.json + .mcp.json，只读 feedback profile）；合并是配置层去重，不削弱隔离语义（治理 Agent 仍只读、仍 deny main workspace/claude_root）。

## 关键去风险结论（无需 DB 迁移）

执行期 profile 由 **job_type → `spec.profile_name`** 解析（`feedback_job_orchestrator.py:54/90/122` 用 `self.profiles[spec.profile_name]` 与 `PROFILE_VERSION_IDS[spec.profile_name]`），**不读持久化 job 的 `profile_name`**（`agent_job_worker.py:97` 传入的 `job["profile_name"]` 仅为透传/展示）。因此：

- 历史 job 记录里的 `profile_name`（"attribution-analyzer" 等）与 `profile_version` 是历史元数据，合并后仍是合法字符串，可正常读取/展示，**不需要数据迁移**。
- 合并后新 job 一律 `profile_name=governor`；未执行的旧 pending job 也按其 job_type 走新 governor spec，不依赖存储的旧名。

风险等级：中（触面广但无数据迁移、无契约破坏）。

## 迁移清单（删除 / 迁移 / 保留）

代码：
- `app/runtime/agent_profiles.py`：5 个 `_*_profile` builder → 1 个 `_governor_profile`；`AgentRole` 5 个治理 literal → `governor`；`GOVERNANCE_AGENT_ROLES` → `{governor}`；`PROFILE_VERSION_IDS` 5→1；`build_profiles()` 返回 `{main-agent, governor}`。
- `app/runtime/agent_job_types.py`：`AGENT_JOB_SPECS` 5 个 `profile_name` 全部指向 `GOVERNOR_PROFILE`（prompt_builder/output_model 保持按 job_type 不变）。
- `app/runtime/settings.py`：新增 `governor_workspace_dir` / `governor_claude_root`（env `GOVERNOR_WORKSPACE_DIR` / `GOVERNOR_CLAUDE_ROOT`）；删除 5 组旧 workspace_dir/claude_root 字段与 env alias。
- `app/services/feedback_job_orchestrator.py`：无逻辑改动（已用 `spec.profile_name`，合并后天然指向 governor）。

配置/部署：
- `docker/docker-compose.yml`：6 workspace 卷 + 5 claude_root 卷 → main + governor 两套 workspace/claude_root；删除 5 组 `*_WORKSPACE_DIR/CLAUDE_ROOT` env 与 mount。
- `docker/runtime-template/`：5 个 `*-workspace/` 合并为 `governor-workspace/`（agent.yaml + CLAUDE.md 通用治理执行者说明 + .claude/settings.json 只读边界 + .mcp.json）；删除 5 个旧目录。
- `scripts/bootstrap_runtime_volume.py` / `runtime_template_safety.py` / entrypoint：workspace 列表 6→2。

文档：
- `README.md`：「六套 Runtime Profile…六个 *-workspace」→ main + governor 两套；配置挂载示例同步。
- 反馈闭环架构文档：把五治理 Agent 表述为"单一 governor 按 job_type 执行归因/方案/执行/用例/回归影响"。

测试：
- `tests/test_agent_profiles_category.py`：`GOVERNANCE_AGENT_ROLES` 5→1；遍历断言改 governor；`denied_paths` 含 governor claude_root。
- 其余断言 5 profile 名/路径的测试（grep `attribution_analyzer_claude_root` 等）同步。
- 新增：job_type→governor profile 解析回归；历史旧 profile_name job 记录仍可读（无迁移回归）。

保留（不动）：
- main agent / 业务 Agent / per-agent 版本治理（B3）；各 job 的 prompt 与 typed-output 契约；持久化 job 记录（旧 profile_name 作为历史元数据）。

## 验证

- `make test` 全绿（含 codex 治理硬门、coverage policy）；
- bootstrap/local-debug 渲染出 main + governor 两套 workspace，settings/mcp 占位渲染正确；
- 反馈闭环主流程（`make main-flow-test`）四类 job（归因/方案/执行/用例/回归影响）均走 governor profile 成功；
- 旧 `${HOME}/volume-agent-gov` 历史 job 记录列表/详情不 500（旧 profile_name 可读）。

## 落地方式

本合并为原子重构（半合并的 profile 系统会破坏闭环），须一次性改完上述清单再过 `make test`，不推半成品。建议作为独立专注提交执行（不与其他 issue 混提）。

## 落地状态

已落地。五个治理 profile 合并为单一 `governor`（profile 名、`AgentRole`、`GOVERNANCE_AGENT_ROLES`、`PROFILE_VERSION_IDS`、`build_profiles` 均收敛为 main + governor）；workspace 合并为 `governor-workspace`，claude_root 合并为 `claude-roots/governor`。`AGENT_JOB_SPECS` 五个 job_type 统一指向 `GOVERNOR_PROFILE`，prompt/output_model/formatter 仍按 job_type 选择，闭环链路与输出契约不变。settings、docker-compose、Dockerfile、entrypoint、runtime-template、bootstrap/export/renderer 脚本、env 示例与 README 同步收敛为两套 profile，历史 job 记录的旧 `profile_name` 作为历史元数据保留、无需 DB 迁移。
