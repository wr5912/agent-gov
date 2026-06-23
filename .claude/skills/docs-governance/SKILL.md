---
name: "docs-governance"
description: "当用户提到新增 docs 文档、迁移文档、重命名、旧文档归档、拆分文档策略、决定文档放哪里、补 docs 索引入口或检查文档引用关系时使用；治理 AgentGov docs 的位置、入口、归档和镜像同步。"
---

# Docs Governance

> 本技能与 `.codex/skills/docs-governance/SKILL.md` 同源镜像，修改需两侧同步。

本技能用于治理 AgentGov 的 `docs/` 文档容器：判断文档放置、入口索引、拆分、归档、删除和引用迁移。它不评审产品目标、反馈闭环方法论或多业务 Agent 治理内容本身；涉及这些内容时，先使用 `agentgov-governance-preflight` 做治理对象建模。

## 使用边界

- 使用本技能：新增文档、文档重复治理、旧文档归档或删除、拆分大文档、移动/重命名文档、修复 `docs/README.md` 或 `docs/archive/README.md`。
- 不使用本技能：判断 AgentGov 产品方向是否正确、评审 v2.7 UI 方案是否合理、设计反馈闭环业务模型、修改运行时代码。
- 详细判断矩阵见 [docs-boundary.md](references/docs-boundary.md)；`SKILL.md` 只保留执行入口和动作流程。

## 工作流

1. 读取 `docs/README.md`、`docs/archive/README.md`、涉及的源文档和必要的权威文档。
2. 给每个候选文档标注角色：权威入口、当前实现基线、v2.7 规划、工程治理、评审/复盘、归档历史或临时材料。
3. 给每个候选文档选择一个动作：`keep`、`merge`、`split`、`move-to-skill`、`archive`、`delete` 或 `no-op`。
4. 按动作更新入口和引用：活跃文档进入 `docs/README.md`，归档文档进入 `docs/archive/README.md`，移动或删除前先用 `rg` 检查旧路径引用。
5. 对拆分或合并后的原文档，只保留短入口、替代关系和权威链接，不继续维护重复正文。

## 权威查找顺序

- 活跃文档入口：`docs/README.md`。
- 归档文档入口：`docs/archive/README.md`。
- 术语和版本层级：`docs/AgentGov术语与版本边界.md`。
- 改进治理工作台 UI、四阶段主链路和效果图验收：`docs/AgentGov_v2.7_四阶段改进治理工作台UI整改方案.md`。
- 文档治理入口说明：`docs/文档治理与归档策略.md`。
- 机械治理规则：`scripts/check_docs_governance.py` 与 `scripts/check_codex_governance.py`。

## 验证

文档或 skill 改动完成后至少运行：

```bash
git diff --check
.venv/bin/python scripts/check_docs_governance.py
.venv/bin/python scripts/check_codex_governance.py --mode fail
```

`check_docs_governance.py` 会覆盖 tracked diff 和 untracked 新文件，检查 docs 入口、归档索引、`SKILL.md` 镜像漂移和未完成标记。

## 最小验收场景

- 新增 `docs/*.md` 但未加入 `docs/README.md` 时，治理检查必须失败。
- 新增 `docs/archive/**/*.md` 但未加入 `docs/archive/README.md` 时，治理检查必须失败。
- 修改自动发现范围内任一项目专项 skill 的 `.codex/skills/*/SKILL.md` 或 `.claude/skills/*/SKILL.md` 单侧文件后，镜像漂移必须被硬门发现。
- 新增或修改的 docs/skill Markdown 包含未完成标记时，治理检查必须失败。

## 注意事项

- 不因文档变旧就移动；必须先确认替代关系、引用链和审计价值。
- 不把当前线程里的临时判断固化为长期文档；只有可复用的流程、标准或证据链才进入 `docs/` 或 skill reference。
- 不把长篇目录表、完整历史归档清单或产品权威正文塞进 `SKILL.md`。
- 新增文档后应能回答：它解决什么治理问题、谁会读、取代了什么、从哪里能发现。
