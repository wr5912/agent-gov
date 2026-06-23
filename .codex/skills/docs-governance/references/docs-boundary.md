# Docs Boundary Reference

本参考用于 `docs-governance` 执行时判断文档保留、拆分、归档、删除或移入 skill 的边界。它只治理文档容器，不替代产品方案、工程设计或测试验收本身。

## 文档角色矩阵

| 角色 | 应放位置 | 判定标准 | 常见动作 |
| --- | --- | --- | --- |
| 权威入口 | `docs/` 顶层 | 定义长期产品口径、术语、核心验收或主设计依据 | `keep`、`merge` |
| 当前实现基线 | `docs/` 顶层 | 解释当前代码、API、数据库、测试和运行态事实 | `keep`、`merge`、`split` |
| v2.7 规划 | `docs/` 顶层 | 定义下一大版本用户主流程、UI、领域模型或代码整改原则 | `keep`、`merge` |
| 工程治理 | `docs/engineering/` | 约束协作、重构、验证、发布和质量门 | `keep`、`move-to-skill` |
| 评审/复盘 | 既有评审目录或 `docs/archive/` | 保留证据链、差距清单、事故复盘或治理反思 | `keep`、`archive` |
| 归档历史 | `docs/archive/` | 已被替代，但仍有审计或迁移价值 | `archive` |
| 临时材料 | 当前线程、PR、issue 或删除 | 只服务一次性讨论，没有长期读者和可复用证据 | `delete`、`no-op` |

## 权威链路矩阵

新增、拆分、归档、删除或替换权威文档前，先填这张短矩阵；矩阵说不清时先补事实，不直接移动文件。

| 检查项 | 必须回答 |
| --- | --- |
| 权威来源 | 哪个文档或效果图是当前唯一依据？ |
| 活跃入口 | `docs/README.md` 中应如何发现新入口？ |
| 旧冲突文档 | 哪些旧文档会与新权威并列或冲突？ |
| 归档索引 | 旧文档是否应进入 `docs/archive/README.md`，替代文档是什么？ |
| 旧路径引用 | `README.md`、活跃 `docs/`、`.planning`、AGENTS/CLAUDE、`.codex`、`.claude` 是否仍引用旧路径？ |
| 测试契约 | `tests/test_documentation_contracts.py` 是否仍读取旧路径或断言旧口径？ |
| skill 镜像 | Codex/Claude 同名 skill 与 reference 是否需要同步？ |

## 动作定义

| 动作 | 何时使用 | 必做检查 |
| --- | --- | --- |
| `keep` | 文档仍是活跃入口或仍解释当前实现事实 | 确认 `docs/README.md` 中的角色和阅读顺序准确 |
| `merge` | 多篇文档重复表达同一权威结论 | 选一个主文档，其余改短入口或归档 |
| `split` | 同一文档混合产品权威、工程流程、历史复盘或 skill SOP | 按读者和权威关系拆分，原文只保留入口 |
| `move-to-skill` | 内容是可复用操作流程、决策动作或验证步骤 | 放入对应 skill 或一级 reference，不放长篇产品正文 |
| `archive` | 文档已被替代但仍有审计、迁移或历史证据价值 | 更新旧路径引用、`docs/archive/README.md` 和相关文档契约测试 |
| `delete` | 文档没有审计价值、长期读者或可复用证据 | 先用 `rg` 确认没有活跃引用 |
| `no-op` | 本轮不改更清晰，或改动收益低于扰动 | 记录原因，不扩大整改范围 |

## 权威冲突处理

- 与 `docs/AgentGov_v2.7_四阶段改进治理工作台UI整改方案.md` 冲突的改进治理工作台 UI、流程或效果图描述，以四阶段方案为准。
- 与 `docs/AgentGov术语与版本边界.md` 冲突的长期术语，以术语边界文档为准；当前实现基线可保留真实 API、数据库和 UI 名称。
- 与 `docs/README.md` 阅读路径冲突的文档状态，先修 README，使活跃入口可发现。
- 与 `docs/archive/README.md` 归档索引冲突的历史文档，先修归档索引和替代关系。
- 涉及产品定位、反馈闭环、多业务 Agent 或 prompt/skill/SOP/eval 资产沉淀时，先用 `agentgov-governance-preflight` 建模，再决定文档动作。

## 归档和删除边界

可以归档：

- 已被新权威文档完整吸收，仍有历史证据价值。
- 仍能解释迁移来源、旧决策、旧评审意见或事故复盘。
- 需要保留给审计，但不应继续出现在活跃阅读路径。

可以删除：

- 没有唯一信息，已被新文档完整覆盖，且没有审计或迁移价值。
- 只是当前线程的临时想法、空壳文件或重复草稿。
- 引用检查确认没有活跃路径依赖。

不要仅因“旧”而归档或删除：

- 当前代码仍依赖其解释的实现基线。
- 它是设计评审、代码审查或治理反思证据链。
- 它解释了迁移边界、失败原因或质量门来源。

## 当前仓库反例

- `docs/AgentGov_ASCII_UI_草图方案_v2.7.md` 已归档，只能追溯旧草图，不再定义 v2.7 UI。
- 旧的 `attribution-analyzer`、`proposal-generator`、`execution-optimizer`、`eval-case-governor`、`regression-impact-analyzer` 设计若与 Governor 合并设计冲突，只能作为历史来源或迁移线索。
- 旧反馈闭环长文已收敛为 `docs/反馈闭环当前实现基线.md`，不得继续作为活跃主设计并列入口。
- 目录细则、归档日期明细和术语正文不应复制进 `SKILL.md`。

## 引用迁移检查

移动、归档或删除前，至少检查：

```bash
rg -n "旧文件名|旧相对路径" README.md docs .planning AGENTS.md AGENTS.override.md .codex .claude
```

完成后确认：

- 活跃文档不再指向过时路径。
- `docs/README.md` 能发现新的活跃入口。
- `docs/archive/README.md` 能追溯原路径、归档路径、替代文档和归档日期。
- 文档契约测试不再读取已归档原路径；如果只检查旧路径不存在或已归档，必须断言新权威入口。
- skill 与 docs 没有互相复制长篇正文，只保留链接和职责边界。
