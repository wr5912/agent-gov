---
name: "improvement-workbench-contract-preflight"
description: "当整改 AgentGov 四阶段改进治理工作台、反馈闭环 UI、tab/card/drawer、Diff、执行优化、测试用例、Trace/Langfuse，或用户要求反复整改、举一反三时使用；改代码前固定业务产物归属、字段所有权、动作副作用和负向验收。"
---

# 改进治理工作台契约预检

本技能用于四阶段改进治理工作台和反馈闭环用户可见链路的执行前预检。目标是在改代码前固定事实、归属和验收，避免只修当前症状。

## 适用范围

使用本技能：

- 改 `ImprovementWorkbench`、阶段工作面板、决策卡、处理记录、详情抽屉或来源管理抽屉。
- 改优化方案、执行优化、Diff / 变更预览、回滚方案、执行记录、测试用例详情或测试发布 tab。
- 改 Trace / Langfuse 打开链接、生成 trace 展示、运行证据面板。
- 改 governor、Agent job、formatter、store projection 或 API response 中会投影到上述 UI 的字段。
- 用户指出“反复整改”“举一反三”“怎么还有”“为什么提示无 diff”“输入怎么是描述类信息”等同类问题。

不使用本技能：

- 纯样式微调且不改变信息归属、按钮动作、状态或接口。
- 只改 runtime/env、Docker、模型凭据；这类问题使用 `runtime-env-governance`。
- 只治理 docs 容器；这类问题使用 `docs-governance`。

## 五行预检

改文件前输出这五行；缺任何一行不得进入实现：

```text
1. 用户动作/对象：用户点击什么，正在处理哪个改进事项阶段，期望看到哪个业务产物。
2. 证据链：UI state -> API response -> agent_jobs/store -> formatter output -> persisted payload 当前卡在哪层。
3. 字段所有权：backend-owned、agent-owned、boundary-owned 分别是什么；LLM 不应输出哪些后端权威字段。
4. 容器归属：决策卡、tab、card、drawer 各自承载什么；哪些内容明确不得出现。
5. 验证绑定：目标 pytest/UI verification nodeid 是什么；需要补哪些负向断言，是否同步 `tests/coverage_policy.json`。
```

## 字段所有权

- backend-owned：改进事项 ID、业务 Agent ID、source feedback refs、run/session/task/case IDs、真实用户输入、status/stage、change_set_id、applied_agent_version_id、applied_diff、Langfuse browser URL、候选版本绑定、时间戳和 provenance。
- agent-owned：归因摘要、证据解释、优化方案正文、业务假设、风险说明、预期输出、检查点、回滚建议和验证语义。
- boundary-owned：HTTP response、SQLite JSON、文件 diff、日志、Langfuse metadata 和浏览器链接。

规则：

- backend-owned 字段可以作为 Agent 输入上下文，不进入 Agent 输出契约。
- 如果 Agent 或 heuristic 输出污染 backend-owned 字段，后端必须覆盖或忽略。
- 原始用户输入必须从 run message、feedback raw_text 或等价权威来源投影，不得由 Agent 生成“复现场景”类描述。

## 动作与状态

- 决策卡只承载当前主业务动作，例如生成归因分析、生成优化方案、执行优化、执行回归测试。
- 状态推进只能作为业务动作副作用；不得把 `/lifecycle` 当作主动作验收。
- 阶段卡主职责是展示对应业务产物；浏览详情、查看 Diff、查看 Trace、查看日志等入口下沉到面板或抽屉。
- fallback/no-action 是一等状态：没有候选版本、变更集或文件 diff 时，文案必须说明“未生成/未绑定/需执行优化”，不能写成已应用成功。
- “重新生成优化方案”只保留一个当前主入口；如果决策卡已经提供，A 优化方案卡不得重复提供。

## 容器归属

优化执行阶段默认归属：

| 容器 | 只应展示 | 明确不得混入 |
| --- | --- | --- |
| A 优化方案 | 方案摘要、风险、来源、方案状态 | 变更项列表、文件 diff、执行记录 |
| B Diff / 变更预览 | 变更摘要、文件列表、可展开 unified diff | 方案确认按钮、执行记录 |
| C 执行计划 | 执行对象、风险、步骤、执行准备状态 | 已执行结果 |
| D 回滚方案 | 当前/目标版本、回滚策略、步骤 | Diff 正文、测试用例详情 |
| E 执行记录 | 执行摘要、应用来源、候选版本/变更集绑定、已应用变更 | 优化方案正文的重复编辑入口 |

测试发布阶段默认归属：

- 测试数据集卡展示数据集和资产关系。
- 回归执行卡展示 run 和执行状态。
- 测试用例详情卡展示名称、原始输入、期望输出和检查点。
- 执行环境 / 基线卡展示 baseline/candidate/environment。
- 发布门禁预览卡展示 gate、阻断和发布风险。

## 验证要求

至少补一条负向断言，优先放入 `scripts/verify_improvement_ui_design_parity.mjs`：

- 不出现旧文案，例如“自动执行优化”。
- 不出现重复主动作，例如同一阶段两个“重新生成优化方案”入口。
- A 优化方案卡不包含“变更项”、Diff 或执行记录。
- Diff 抽屉在有 `change_set_id` 和文件路径时能展示 unified diff。
- 测试用例输入不包含“复现场景：”类生成描述。
- Langfuse 浏览器链接不含容器内 host，且包含具体 `/project/.../traces/...` URI。

影响主流程、UI tab 状态、API response 或 coverage manifest 时，运行 `make main-flow-test`；仅 docs/skill 改动按项目 docs/skill 治理命令验证。
