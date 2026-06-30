---
name: "agentgov-governance-preflight"
description: "在重写或评审 AgentGov 产品定位、目标愿景使命、反馈闭环治理方案、多业务 Agent 创建与治理方案，或 prompt/skill/SOP/eval 资产沉淀方案前，先做治理对象建模再写正文。仅用于 AgentGov 产品/治理方案类任务；普通代码、bug 修复、单文件改动不触发。"
---

# AgentGov 治理对象预检

> 本技能与 `.codex/skills/agentgov-governance-preflight/SKILL.md` 同源镜像，修改需两侧同步。

本技能防止 AgentGov 产品/治理方案类任务再次出现“先按现有实现做功能补全、没先抽象治理对象和闭环系统”的根因偏差，典型表现是：用功能清单替代系统模型、用当前实现替代产品边界、用数据资产替代治理资产、用 main agent 替代多业务 Agent，导致建议越写越长仍要靠用户连续纠偏。

## 触发场景

任务涉及以下内容时，在 Analyze/Plan 阶段先执行本预检，再写文档或方案正文：

- AgentGov 产品定位、目标、愿景、使命。
- 反馈闭环、归因、优化、评估、版本治理的产品级方案。
- 创建、配置、优化或治理业务 Agent。
- prompt / skill / SOP / playbook / eval case / regression dataset 的沉淀方案。
- 把 main agent 的闭环扩展到更多 Agent。

不触发：普通代码实现、bug 修复、单文件重构、纯工程配置改动。

## 必须先输出的预检

不要先写文案或方案。先输出下面三块。

### 1. 治理对象矩阵

| 维度 | 结论 |
| --- | --- |
| 被治理对象 | 是 main agent、某个业务 Agent、某类能力域，还是整个 AgentGov 平台？ |
| 治理执行者 | 是后端规则、治理 Agent、人工审批、外部系统，还是组合？ |
| 资产类型 | 是数据资产、方法论资产、执行资产、版本资产，还是审计资产？ |
| 生命周期 | 对象是否有 draft / active / evaluating / deprecated / archived 阶段？ |
| 反馈归属 | 反馈、评估、版本、prompt、skill 归属到哪个 Agent / version / 场景？ |
| 当前实现边界 | 当前代码真实实现到哪一步？ |
| 目标能力边界 | 文档或方案要表达的目标能力是什么？ |

### 2. 闭环链路

```text
对象 -> 运行 -> 反馈 -> 归因 -> 优化 -> 评估 -> 版本 -> 资产 Registry
```

链路中任何一环说不清，先补清楚再写正文，不要急着加章节或加配置。

### 3. 风险自检

- 是否把当前实现误当长期边界？（main agent 是样板，不是边界）
- 是否把数据资产误当全部资产？（还有方法论资产、执行资产）
- 是否混淆业务 Agent 和治理 Agent？
- 是否缺反馈路由和资产归属？

### 4. 改进事项决策卡动作门

涉及四阶段改进治理工作台、决策卡、主按钮或阶段推进时，必须补一张动作矩阵：

| 用户动作 | 业务产物 | API 副作用 | 状态副作用 | 审计记录 |
| --- | --- | --- | --- | --- |
| 生成归因分析 | Attribution | `/attribution/generate` | 可推进到 attribution | 确认当前反馈整理结果 |
| 生成优化方案 | OptimizationPlan | `/optimization-plan/generate` | 可推进到 optimization | 确认当前归因结论 |
| 自动执行优化 | ExecutionRecord / change set | `/execution/apply` | 可推进到 execution | 确认当前优化方案 |
| 执行回归测试 | RegressionAssessment / test candidates | `/regression-assessment/generate` | 可推进到 regression | 确认当前执行结果 |

规则：决策卡主按钮必须执行真实业务动作；`/lifecycle` 只能作为上述动作的副作用，不能作为主按钮的唯一效果。若按钮文案含“生成/执行/纳入”，验收脚本必须断言对应业务 API 被调用。

## 四类 Agent 与三层资产参照

| Agent 类型 | 文档中应如何表达 |
| --- | --- |
| main agent | 第一阶段样板，不是长期边界 |
| 业务 Agent | AgentGov 的长期治理对象 |
| 治理 Agent | 支撑闭环的工具型 Agent，不等同于被治理对象 |
| subagent / skill | 某个 Agent 内部的能力组织方式，属执行资产或行为配置 |

| 资产层级 | 典型产物 |
| --- | --- |
| 数据资产 | run、trace、feedback、evidence、eval result、release event |
| 方法论资产 | 归因方法、优化 SOP、评估规程、发布策略 |
| 执行资产 | prompt、skill、profile、playbook、eval case、workflow |

## 正文落地约束

- 只有前面三块清楚后，才写文档结构或方案正文。
- 正式 `docs/项目目标愿景使命.md` 用产品定义口吻，不写“建议/应该/当前文档仍/用户确认/这版更准确”等评审过程语气；直接写“AgentGov 是…/负责…/不负责…/治理闭环包括…”。
- 产品主口径以“智能体治理平台 AgentGov”为准；“开发平台”只作为子能力表述，不作为主定位。
