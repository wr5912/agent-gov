---
name: "docs-governance"
description: "当用户提到新增 docs 文档、迁移文档、重命名、旧文档归档、拆分文档策略、决定文档放哪里、补 docs 索引入口或检查文档引用关系时使用；治理 AgentGov docs 的位置、入口、归档和镜像同步。"
---

# Docs Governance

> 本技能与 `.codex/skills/docs-governance/SKILL.md` 同源镜像，修改需两侧同步。

本技能用于新增、迁移或归档 `docs/` 文档前做容器治理预检，避免产品目标、核心测试、目录治理、设计方案和历史复盘互相混杂。

规则权威来源是 `docs/文档治理与归档策略.md`。涉及 AgentGov 产品定位、目标愿景使命、反馈闭环、多业务 Agent、prompt/skill/SOP/eval 资产沉淀时，先使用 `agentgov-governance-preflight` 做内容建模，本技能只处理文档放置、入口、引用和归档。

## 工作流

1. 先读取 `docs/文档治理与归档策略.md`、`docs/README.md` 和涉及的源文档。
2. 判断文档类型：产品权威、核心测试用例、设计方案、评审报告、工程流程、历史复盘或示例配置。
3. 判断权威关系：新增、补充、替代、拆分、迁移、归档或仅作历史记录。
4. 选择位置：长期权威文档放 `docs/` 顶层；工程流程放 `docs/engineering/`；评审报告放现有评审目录；只有满足归档条件才放 `docs/archive/`。
5. 对新增活跃 Markdown 文档，必须把仓库相对路径加入 `docs/README.md`。
6. 对移动或归档，先用 `rg` 检查引用，再更新链接；需要归档时补充 `docs/archive/README.md`。
7. 对拆分文档，在原文档只保留简短引用，避免重复维护同一策略。

## 验证

文档改动完成后至少运行：

```bash
git diff --check
.venv/bin/python scripts/check_docs_governance.py
.venv/bin/python scripts/check_codex_governance.py --mode fail
```

`check_docs_governance.py` 会覆盖 tracked diff 和 untracked 新文件，检查 docs 入口、归档索引、skill 镜像漂移和未完成标记。

## 最小验收场景

- 新增 `docs/*.md` 但未加入 `docs/README.md` 时，`check_docs_governance.py` 必须失败。
- 新增 `docs/archive/**/*.md` 但未加入 `docs/archive/README.md` 时，`check_docs_governance.py` 必须失败。
- 修改 `.codex/skills/docs-governance/SKILL.md` 或 `.claude/skills/docs-governance/SKILL.md` 任一侧后，两侧镜像漂移必须被硬门发现。
- 新增或修改的 docs/skill 文件包含未完成标记时，`check_docs_governance.py` 必须失败。

## 注意事项

- 不把 `docs/` 目录治理策略塞进功能测试、产品愿景或实现方案文档，只保留链接。
- 不用本技能评审产品目标、治理对象或闭环方法论是否正确；这属于 `agentgov-governance-preflight`。
- 不因文档变旧就移动；必须先确认替代关系、引用链和审计价值。
- 不把当前线程里的临时判断固化为长期文档；只有可复用的流程、标准或证据链才进入 `docs/`。
- 新增文档后应能回答：它解决什么治理问题、谁会读、取代了什么、从哪里能发现。
