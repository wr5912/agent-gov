# AgentGov v2.7 UI 设计一致性核查与整改报告

> 文档定位：设计评审报告。
> 核查对象：当前 `feat/agentgov-cross-gen-v2.7` 分支前端实现、当前真实部署 UI、`docs/AgentGov_ASCII_UI_草图方案_v2.7.md`。
> 权威关系：本报告不替代 `AgentGov_ASCII_UI_草图方案_v2.7.md`；草图方案仍是 v2.7 UI 的权威设计源。本报告记录当前实现与草图的差距、根因和整改验收口径。
> 补充参考：已参考 `AgentGov_UI_差距报告_v2.7.md`，吸收其中关于内容实体、设置结构、旧 workspace 迁移策略和逐节差距的补充判断。
> 术语口径：本报告按 v2.7 规划术语评审 UI 一致性；当前实现旧名与 v2.7 术语映射见 [AgentGov术语与版本边界](../AgentGov术语与版本边界.md)。
> 本轮边界：只做核查报告输出，不改前端、后端、测试代码。

## 1. 核查结论

当前 UI 与 `AgentGov_ASCII_UI_草图方案_v2.7.md` 的差异不是单页样式问题，而是信息架构、入口收敛、用户任务模型、视觉主题、上下文导出和验收口径的系统性偏差。

核心判断：

- v2.7 草图要求前台围绕 `Playground / 改进 / 发布` 三个用户任务组织，当前顶栏仍保留独立 `反馈优化` 和 `资产` 入口，造成主流程双轨。
- v2.7 草图要求 Playground 主区只保留对话和少量动作，当前 Playground 仍是旧三栏结构，运行配置、会话、skills 和 runtime inspector 直接占据主界面。
- v2.7 草图要求反馈从“自然语言反馈 -> 系统理解 -> 确认保存 -> 改进事项”进入闭环，当前仍是旧的“提交反馈”表单和反馈优化工作台。
- v2.7 草图要求 `ContextPackage` 提供四种上下文类型，当前改进页只有简化字段文本，现有验收脚本也按简化文本断言。
- v2.7 草图要求 `Governance Light` 成为主工作台统一主题，当前全局仍是暖色旧主题，蓝色治理主题只局部作用在改进、发布、资产页面。
- v2.7 文档 §17.6 的完成状态存在过度宣称，当前真实 UI 仍有多个已知偏差，不应继续以“真实浏览器 + 真实后端截图验收通过”作为整体 UI 一致性结论。

因此，后续整改应按“草图为准”收敛当前实现，而不是把草图改写成适配旧实现的状态说明。

补充裁决：

- v2.7 新建的 `改进 / 发布 / 资产` surface 与顶栏外壳方向基本正确，但只覆盖了事项壳和部分治理动作，尚未覆盖草图中“反馈、系统理解、归因、Trace、回归保障、沉淀资产”的内容层。
- 旧反馈优化 workspace 不能在功能等价迁移前直接下线。正确顺序是：先把其归因、方案、执行、回归资产、版本能力迁移到 `ImprovementItem` 主闭环，再把旧入口降级或退役。
- 最大结构缺口不是单纯 UI 重排，而是缺少若干内容实体和聚合接口：`NormalizedFeedback`、带正文的 `Attribution`、一等 `Feedback` 内容对象、`TraceSummary`、改进事项级回归保障和资产沉淀视图。

## 2. 核查证据

| 证据类型 | 证据位置 | 说明 |
| --- | --- | --- |
| 真实浏览器截图 | `/tmp/agentgov-v27-ui-playground.png` | 当前 Playground 仍为旧三栏结构，左侧 session/skills，右侧 runtime/config/skills/events inspector。 |
| 真实浏览器截图 | `/tmp/agentgov-v27-ui-improvement.png` | 改进页已存在 `ImprovementItem` 工作台，但空态、列表和详情仍未完整覆盖草图中的来源反馈、证据、上下文类型和用户确认任务。 |
| 真实浏览器截图 | `/tmp/agentgov-v27-ui-release.png` | 发布页已可见门禁摘要，但缺少草图中的回归、变更、强制发布等完整门禁动作与 per-Agent 可靠归属证据。 |
| 真实浏览器截图 | `/tmp/agentgov-v27-ui-asset.png` | 资产 Registry 被做成一级管理页，与草图“Settings + 改进事项资产出口”的定位不一致。 |
| 真实浏览器截图 | `/tmp/agentgov-v27-ui-feedback.png` | 独立反馈优化工作台仍暴露 signals、batches、regression-assets、versions 等旧对象管理入口。 |
| 可见文本采样 | `/tmp/agentgov-v27-ui-audit-samples.json` | Playground 和反馈优化页面只有顶栏级 `data-testid`，缺少草图要求的领域级稳定选择器。 |
| 源码证据 | `frontend/src/App.tsx` | `chat` 分支仍挂载 `Sidebar + ChatPanel + Inspector`；`feedback` 分支仍挂载旧 `ExternalFeedbackWorkspace`。 |
| 源码证据 | `frontend/src/components/Topbar.tsx` | 顶栏仍有独立 `反馈优化` action，`资产` 仍在一级导航。 |
| 源码证据 | `frontend/src/components/ChatPanel.tsx` | Playground 主区仍展示 `Skills Mode / Max Turns / Alert ID / Case ID / Allowed Tools / Disallowed Tools` 控制条。 |
| 源码证据 | `frontend/src/styles.css` | 全局 token 仍是暖色旧主题，`Governance Light` 未提升为全局主工作台主题。 |
| 验收证据 | `scripts/verify_improvement_workbench.mjs` | 当前脚本验证现有实现可用，但上下文断言与草图四类型 ContextPackage 不一致。 |

## 3. 差距矩阵

| 界面 / 能力 | 草图要求 | 当前实现 | 影响 | 严重级别 |
| --- | --- | --- | --- | --- |
| 主导航 | 顶层收敛为 `Playground / 改进 / 发布`；高级能力进入 Settings。 | 顶层仍有 `资产`，右侧 action 仍有 `反馈优化`。 | 用户仍需要理解旧反馈优化、资产管理和改进事项之间的关系。 | P0 |
| Playground 信息架构 | 主区只留对话；运行配置进入右上“配置”抽屉。 | 左侧 session/skills、右侧 inspector、顶部 control strip 仍常驻。 | 用户任务被内部运行配置和调试信息淹没。 | P0 |
| Playground 消息动作 | 助手回复下提供 `创建反馈 / 查看 Trace / 获取上下文 / 打开 Langfuse / 重新运行`。 | 只有 `SDK 事件` 和 `提交反馈`，空态没有草图动作模型。 | 无法从回复自然进入反馈、Trace 和上下文闭环。 | P0 |
| 反馈 Drawer | 自然语言反馈 -> 整理反馈 -> 确认系统理解 -> 保存并进入改进事项。 | 仍是标签、动作、工具、备注的提交表单。 | 用户被迫填写系统字段，违背“把复杂留在系统内”。 | P0 |
| 反馈与改进收敛 | 反馈保存后自动创建或归并到 `ImprovementItem`。 | 反馈优化工作台与改进页并行存在。 | 形成两个主流程入口，闭环对象心智不统一。 | P0 |
| 改进详情 | 默认展示当前阶段、下一步、证据、来源反馈、唯一主动作。 | 已有阶段和主动作，但自动化策略、相似归并、闭环对象 ID 直接暴露在主区。 | 内部对象管理感仍重，用户需要理解系统流水线。 | P1 |
| ContextPackage | 四种类型：问题摘要、AI 分析上下文、Playwright 复现信息、完整 JSON；支持预览、复制、下载。 | 只有简化 Markdown 字段文本，无类型选择和下载。 | 不能稳定服务用户、AI 和 Playwright 复现。 | P0 |
| 发布页 | 回答“能不能发、为什么、发了包含什么”，并提供门禁动作。 | 已有摘要，但缺完整门禁动作；agent scoping 依赖响应是否带 `agent_id`。 | 发布判断和跨 Agent 归属仍不可靠。 | P1 |
| 资产 Registry | 作为 Settings 高级能力和改进事项资产出口。 | 做成一级导航管理页。 | 提前暴露资产对象管理，偏离用户确认型工作台。 | P1 |
| 视觉主题 | `Governance Light` 全站主工作台统一 token；深色只用于调试区。 | 全局仍是暖色旧主题，改进相关页面局部覆盖。 | 页面之间观感割裂，状态色语义不统一。 | P1 |
| 稳定选择器 | 关键区域有领域级 `data-testid / data-state / data-action`。 | Playground 和反馈优化页面几乎只有顶栏选择器。 | 无法用 Playwright 断言设计一致性。 | P0 |
| 验收脚本 | 验证草图一致性，而不仅是现有实现可用。 | 现有脚本验证局部功能，部分断言与草图方向相反。 | “测试通过”不能证明 UI 达到 v2.7 设计目标。 | P0 |
| 文档进度 | §17.6 应真实反映已完成与未完成边界。 | 已完成清单存在过度宣称。 | 后续评审和发布容易误判当前状态。 | P0 |

### 3.1 按草图章节补充的细化差距

| 草图章节 | 当前遗漏的关键元素 | 依赖面 | 整改含义 |
| --- | --- | --- | --- |
| §2 导航与设置 | Settings 缺业务 Agent 管理、自动化策略、资产 Registry、Developer / Debug 子菜单；业务 Agent 后端 CRUD 已有但前端未接入。 | FE + 既有 API | Settings 需要从单一 Runtime API 配置弹窗升级为平台设置入口。 |
| §3 Playground | 标题未体现当前业务 Agent；配置未进入抽屉；回复动作缺 `查看 Trace`、`获取上下文`、`打开 Langfuse`、`重新运行`。 | FE | 需要替换旧三栏布局，而不是只调整样式。 |
| §4 创建反馈 | 缺自然语言反馈输入态、系统整理态、确认系统理解态、保存后生成或归并改进事项状态。 | BE + FE | 需要 `NormalizedFeedback` 和 feedback-to-improvement 编排。 |
| §5 改进列表 | 缺页内 `当前 Agent / 全部 Agent` scope 切换、状态计数 pills、列表项 `关联 N 条反馈`。 | FE + Feedback 内容 | 列表应从对象清单升级为用户确认型工作台入口。 |
| §6 归因待确认 | 缺归因正文、责任边界、修改、重新整理、查看证据。 | BE + FE | `improvement_stage=attribution` 不足以表达归因内容，需要带正文的 Attribution surface。 |
| §7 完整链路 | 只有 stepper，无独立完整链路展开、阶段状态文案和自动化详情。 | FE | Stepper 应保留，另补完整链路下钻。 |
| §8 多反馈归并 | 来源反馈只是裸 ref；缺反馈摘要、来源、状态表格、合并依据、置信度、移出反馈和标记合并不准。 | BE + FE | 当前 item-to-item 合并不能替代 feedback-to-item 归并。 |
| §9 Trace 摘要 | 缺关键观察、相关工具调用、关联运行和刷新摘要。 | BE + FE | 需要 `TraceSummary` 或等价聚合接口，前端以调试抽屉展示。 |
| §10 获取上下文 | 只有单一文本；缺四类型、自动包含清单、预览切换、下载入口和多页面入口。 | FE | 先用现有数据生成四类上下文，后续再补齐 Trace/证据字段。 |
| §11 回归保障与资产沉淀 | 资产 Registry 独立存在，但改进详情缺候选回归资产卡和本事项沉淀资产视图。 | BE + FE | W3 资产能力需要接回改进事项，而不是只做资产管理页。 |
| §12 发布 | 缺归因、优化、回归三门禁明细；缺去运行回归、查看变更、强制发布动作；per-Agent scoping 仍需后端字段。 | BE + FE | 发布页要从单门摘要升级为门禁判断台。 |
| §13 自动化策略 | 当前策略控件在改进详情内，草图要求 Settings 中管理全局或 per-Agent 策略。 | FE + 既有 API | 自动化策略应在 Settings 管理，详情页只显示当前策略和手动兜底动作。 |
| §14 文案术语 | 旧 feedback workspace 和 Playground 仍暴露 `优化批次`、`候选回归用例`、`提交反馈`、`SDK 事件` 等旧对象语言。 | FE | 旧入口迁移完成前应明确标注为开发者/调试入口，用户主流程不再使用这些文案。 |

## 4. 根因分析

### 4.1 旧入口替换不彻底

v2.7 新增了 `ImprovementWorkbench / ReleaseWorkbench / AssetRegistry`，但没有替换旧的 `ChatPanel / Sidebar / Inspector / ExternalFeedbackWorkspace` 主流程。结果是新旧工作台并存，用户侧仍看到旧对象管理模型。

### 4.2 验收标准偏向“可用”，没有覆盖“设计一致”

当前 Playwright 脚本主要验证 API mock 下的功能是否可点击、状态是否更新、页面是否可达。它没有断言主导航收敛、旧入口消失、配置抽屉替代 control strip、反馈 Drawer 两阶段、ContextPackage 四类型等设计一致性规则。

### 4.3 文档状态先行，真实 UI 证据滞后

`AgentGov_ASCII_UI_草图方案_v2.7.md` §17.6 已把多个能力标成完成，但真实截图仍显示 Playground、反馈优化、全局主题、上下文导出等明显偏差。文档状态没有被真实浏览器主页面证据约束。

### 4.4 对“资产”和“反馈优化”的产品定位没有严格回到用户任务

资产 Registry 和反馈优化能力本身有价值，但草图定位是：用户主流程围绕改进事项，资产和旧闭环对象进入 Settings、下钻或出口。当前实现把它们继续放在一级入口，导致主流程被系统对象拆散。

### 4.5 内容实体没有跟上事项壳

当前 `ImprovementItem` 已有阶段、状态、agent scoping 和轻量链接，但草图中的关键页面并不只需要“阶段”。创建反馈、确认系统理解、归因待确认、来源反馈归并、Trace 摘要、回归保障和资产沉淀都需要可展示的内容实体。

当前缺口集中在：

- `NormalizedFeedback`：承载系统整理后的问题、原因、可能对象、影响、建议、回归价值和用户原话。
- `Feedback` 一等内容对象：承载反馈摘要、来源、状态、原文、Run/Trace 关系。
- `Attribution` 内容 surface：承载归因正文、责任边界、证据和确认状态。
- `TraceSummary`：承载关键观察、工具调用摘要、关联运行和 Langfuse 链接。
- 改进事项资产聚合：按改进事项汇总回归、方法论、执行、审计资产。

没有这些内容层，只调整前端布局会形成“看起来像草图，但详情没有内容”的空壳。

## 5. 整改方案

### P0：先修正信息架构、文档状态和验收口径

- 主导航目标收敛为 `Playground / 改进 / 发布`；`资产` 与旧 `反馈优化` 不再作为用户主流程目标形态。
- 旧反馈优化工作台采用“先迁能力再下线”：在归因、方案、执行、回归资产、版本能力被 `ImprovementItem` 主闭环等价承接前，保留可达但标注为 Developer / Debug 或旧流程诊断入口。
- Settings 先补信息架构：业务 Agent 管理、自动化策略、资产 Registry、Developer / Debug；其中业务 Agent 管理复用已有 `/api/agent-registry` CRUD 能力。
- 新增 `scripts/verify_v27_ui_design_parity.mjs`，断言：
  - 一级导航只含 `Playground / 改进 / 发布`。
  - Playground 不出现旧 `Sidebar / Inspector / control-strip`。
  - Playground 配置在抽屉内。
  - 助手回复动作齐全。
  - 反馈 Drawer 是两阶段确认流程。
  - ContextPackage 有四种类型、复制和下载。
  - 发布页有门禁摘要和关键动作状态。
- 更新 `tests/coverage_policy.json`，把 v2.7 UI 设计一致性脚本纳入主流程覆盖清单。
- 修正 `docs/AgentGov_ASCII_UI_草图方案_v2.7.md` §17.6，将已完成状态改为基于真实验收证据的状态。

### P1：重构 Playground 和反馈 Drawer

- 新建或重构 Playground shell：
  - 主内容只显示对话、回复动作、输入框。
  - session、subagent、skills、tools、max turns、alert/case 等配置进入“配置”抽屉。
  - Runtime 状态与 model 信息保留在顶栏或配置抽屉，不占主对话区。
- 重构 `MessageBubble`：
  - `SDK 事件` 改为 `查看 Trace` 或调试下钻。
  - `提交反馈` 改为 `创建反馈`。
  - 补 `获取上下文 / 打开 Langfuse / 重新运行`。
- 新建反馈 Drawer：
  - 输入态只让用户描述“哪里不对”和“希望以后怎么处理”。
  - 整理态展示系统理解卡片。
  - 保存态显示归属业务 Agent、系统理解、生成或归并的改进事项，并提供“查看改进事项”。

### P2：补齐 ContextPackage、发布页和改进详情

- 实现统一 `ContextPackage` view model：
  - `problem_summary`
  - `ai_analysis_context`
  - `playwright_reproduction`
  - `full_json`
- `ContextPackage` 自动包含当前页面、改进事项、来源反馈、系统理解、阶段、证据、Trace/Langfuse、Agent/Model/Version。
- 改进页默认只展示用户需要确认的当前阶段内容；自动化策略、相似归并、闭环对象 ID 进入折叠区或更多菜单。
- 发布页补全“去运行回归 / 查看变更 / 强制发布”动作的可用、禁用和风险提示。
- 后端响应若缺 `agent_id`，应补齐 `AgentChangeSet / AgentRelease` 的 `agent_id` 字段，发布页不得靠“缺字段不过滤”维持 scoping。

### P3：补齐内容实体并迁移旧 feedback workspace 能力

- 新增或收口 `NormalizedFeedback`：支持反馈 Drawer 的系统整理、用户确认和保存后关联改进事项。
- 将 feedback signal 升级为改进闭环可展示的一等反馈内容：至少能提供摘要、来源、状态、原文、Run/Trace 关系。
- 将归因结果以改进事项子资源或聚合视图呈现，支持归因正文、责任边界、证据、确认、修改和重新整理。
- 建立 Trace 摘要聚合能力：关键观察、工具调用摘要、关联运行、Langfuse 链接。
- 将旧 feedback workspace 的归因、优化方案、执行、回归资产、版本能力逐步迁移到 `ImprovementItem` 详情和发布门禁中。
- 改进详情新增回归保障候选卡和本事项沉淀资产区，资产 Registry 保留为高级管理视图。

### P4：统一视觉主题和旧样式收口

- 将 `Governance Light` token 提升为全局主工作台 token。
- 迁移 Playground、反馈 Drawer、改进、发布、Settings 到统一 token。
- 深色仅用于 Trace、Raw JSON、Logs、Diff、Playwright 复现和 Developer Debug。
- 清理或隔离旧暖色主题，避免新旧页面混用造成状态色语义分裂。

### P5：退役旧入口

- 当 `ImprovementItem` 主闭环已覆盖旧 feedback workspace 的反馈收集、归因、方案、执行、回归和版本能力后，再移除或隐藏旧 `ExternalFeedbackWorkspace` 用户入口。
- 退役前必须有迁移验收清单：旧入口每项用户可见能力在新主流程中有对应入口、状态、错误投影和测试覆盖。
- 退役后清理旧文案与旧 selector，避免 `Batch / 信号 / 优化批次` 等旧事项层概念继续出现在用户主界面。

## 6. 验收标准

### 6.1 专项验收

- `pnpm --dir frontend build`
- `node scripts/verify_v27_ui_design_parity.mjs`
- `node scripts/verify_improvement_workbench.mjs`
- `node scripts/verify_asset_registry.mjs`（入口调整后改为 Settings/资产出口专项）
- `node scripts/verify_feedback_optimization_ui_states.mjs`（仅作为旧调试工作台专项，不再作为用户主 UI 通过标准）

### 6.2 主流程验收

- `make main-flow-test`
- `.venv/bin/python scripts/check_docs_governance.py`
- `.venv/bin/python scripts/check_codex_governance.py --mode fail`

### 6.3 发布前验收

- `make test`
- 重建并部署真实容器环境。
- 验收测试使用真实部署容器和 `docker/.env`。
- 功能验收不使用 `docker/.env.local-debug`，除非测试目标明确是 local-debug 本身。
- 真实浏览器截图保存到 `/tmp/agentgov-v27-ui-after-*.png`，覆盖 Playground、改进、发布、Settings/资产、ContextPackage、反馈 Drawer。

## 7. 文档同步要求

后续实现整改时，应同步更新：

- `docs/AgentGov_ASCII_UI_草图方案_v2.7.md`：修正 §17.6 的完成状态，按真实验收证据标注。
- `docs/README.md`：保留本报告入口。
- `tests/coverage_policy.json`：绑定 v2.7 UI 设计一致性脚本。
- 相关 Playwright 脚本注释：明确哪些脚本验证用户主 UI，哪些脚本验证旧调试工作台。
- API / DTO 文档：若新增 `NormalizedFeedback`、`Attribution`、`TraceSummary` 或发布页 `agent_id` 字段，需要同步 OpenAPI、前端生成类型和对应主流程测试。

## 8. 当前裁决

本轮只输出核查与整改报告，不动代码。下一轮若进入实现，应按 P0 到 P5 分批推进，并以真实容器环境和真实浏览器证据作为验收口径。

旧 feedback workspace 的处理原则是“能力先迁移，入口后退役”。在新 `ImprovementItem` 主闭环等价覆盖旧能力前，不做破坏性下线；但用户主 UI 的目标形态仍以 v2.7 草图为准。
