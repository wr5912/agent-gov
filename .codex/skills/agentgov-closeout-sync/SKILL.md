---
name: "agentgov-closeout-sync"
description: "在 AgentGov 阶段收尾、里程碑交接、发版前后，或用户明确说同步文档、同步知识、整理本阶段、交接给新人/其他 Agent 时使用；核对 README、docs、AGENTS/CLAUDE 项目规则、Codex/Claude skill 与记忆边界是否和本轮代码/方案变更一致。不要因普通单文件改动或裸“整理”自动触发。"
---

# AgentGov Closeout Sync

本技能用于把阶段性成果收束为可维护的项目知识。它只做收尾同步决策和执行编排，不替代 `docs-governance` 的文档容器治理，不替代 `agentgov-governance-preflight` 的产品治理对象建模，也不直接修改 Codex 记忆；只有用户明确要求更新记忆时，才按系统记忆规则处理。

## 工作流

1. 固定当前事实：读取 `git status --short --branch`、本轮 diff、最近提交和用户指定的阶段/范围。若没有代码或文档事实变化，输出 no-op 结论，不为了“收尾”新增叙事。
2. 建变更影响矩阵：
   - 产品定位、目标愿景、反馈闭环、多业务 Agent、prompt/skill/SOP/eval 资产：先用 `agentgov-governance-preflight`。
   - 新增、迁移、归档、拆分或索引 `docs/` 文档：用 `docs-governance`。
   - Codex/Claude 配置、hook、guidance/rules、skill 触发或上下文膨胀：Codex 侧用 `codex-config-optimizer`；Claude 侧沿用同一 `keep/delete/merge/move-*` 分类核查原生配置。
   - runtime/env、Docker、Langfuse、volume 或模型凭据：用 `runtime-env-governance`。
   - 功能行为或测试同步：用 `test-sync-governance`。
3. 分层同步：
   - `README.md` 与 `docs/` 面向人类接手者和下游集成，写当前可用事实、使用方式、运维和架构边界。
   - 唯一根 `AGENTS.md`、`.claude/rules/agentgov-project.md` 面向本仓库 AI，写必须遵守的项目红线、入口和硬门，不写历史流水账。
   - `.codex/skills/`、`.claude/skills/` 只放可复用流程；新增或修改项目专项 skill 时保持两侧镜像，治理脚本会按明确例外自动发现镜像范围。
   - 记忆只记录跨会话偏好或低频但高价值事实；稳定知识应进入 docs，记忆最多保留短指针。
4. 编辑原则：优先修改现有权威文档，少新增文件；优先合并和删除过期表述，少追加段落；使用绝对日期；不要在项目根规则文件加入“某日期上线某能力”这类事件记录。
5. 验证并报告：列出实际改动和未改原因。文档、skill 或治理配置有变更时至少运行：

```bash
git diff --check
.venv/bin/python scripts/check_docs_governance.py
.venv/bin/python scripts/check_codex_governance.py --mode fail
```

若同步触及代码、主流程、测试或前端可见页面，再按对应项目规则补目标测试、`make main-flow-test`、`make test`、前端构建或浏览器 smoke。

## 发版收尾检查矩阵

| 层面 | 检查项 | 验证 |
| --- | --- | --- |
| README / docs | 新能力、测试边界、环境边界和入口索引是否与当前实现一致 | `scripts/check_docs_governance.py` |
| `.codex` / `.claude` | 项目专项 skill 是否两侧镜像，例外是否明确 | `scripts/check_docs_governance.py` 动态镜像发现 |
| docs 归档/合并 | 归档原路径是否已移出活跃入口，文档契约测试是否不再读取旧路径 | `tests/test_documentation_contracts.py`、`scripts/check_orphan_tests.py` |
| 版本面 | `app/version.py`、`frontend/package.json`、Compose image tag 是否同步 | 版本引用检索与 Compose config |
| 测试面 | 是否按改动类型选择了定向测试、主流程、live 容器验收或全量 `make test` | 测试命令和结果写入最终报告 |
| 远端校验 | commit、branch、annotated tag 是否真的到远端 | `git ls-remote --heads` / `git ls-remote --tags` |
| 记忆边界 | 只有跨会话偏好或易忘边界进入 memory；稳定事实进 docs/skill | 用户明确要求时才更新 memory |

## 输出要求

最终回复按项目事实给出：

- 已同步：列出文件和同步原因。
- 未同步：列出评估过但无需修改的层，说明原因。
- 验证：列出已运行命令和结果。
- 风险：只列仍需用户决策或外部条件的事项。
