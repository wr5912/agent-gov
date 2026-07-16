# AgentGov 文档索引

本索引用于让 `docs/` 文档按版本、用途和权威性可发现，并配合 `scripts/check_docs_governance.py` 全量检查当前活跃/归档入口、相对链接、skill 镜像和治理标记。

阅读顺序建议：

1. 先读“权威入口”，明确产品边界、术语和验收锚点。
2. 需要理解当前代码和运行态时，读“当前实现基线”。
3. 需要评审尚未落地的单项产品能力时，读“产品能力目标方案”。
4. 需要评审四阶段改进治理目标方案时，读“四阶段改进治理工作台权威方案”。
5. 需要落地工程质量、GSD 或治理硬门时，读“工程治理”。
6. 需要追溯旧评审、旧补充方案或治理反思时，读“归档入口”。

文档状态说明：

- **权威入口**：长期产品口径、术语和验收锚点，优先级最高。
- **当前实现基线**：解释当前代码和运行态；历史 API、数据库和 UI 名称只在明确标注的迁移说明中出现。
- **产品能力目标方案**：定义尚未落地的单项产品能力、公开契约和工程边界；不表示当前 OpenAPI 已支持。
- **四阶段改进治理方案**：面向目标态的产品方案，不自动说明当前代码已经实现；其中四阶段改进治理工作台方案是改进治理工作台 UI、流程和效果图验收的绝对依据。
- **评审/复盘**：保留证据链和审查意见，不作为主实现方案；若与主方案冲突，以对应权威入口或主方案为准。
- **工程治理**：约束协作、测试、发布和治理硬门，不承载产品愿景。
- **归档**：已被替代但仍有审计价值的历史文档，从活跃阅读路径移入 `docs/archive/`。

## 权威入口

- [项目目标愿景使命](./项目目标愿景使命.md)：`docs/项目目标愿景使命.md`
- [AgentGov核心功能测试用例](./AgentGov核心功能测试用例.md)：`docs/AgentGov核心功能测试用例.md`
- [AgentGov术语与版本边界](./AgentGov术语与版本边界.md)：`docs/AgentGov术语与版本边界.md`
- [文档治理与归档策略](./文档治理与归档策略.md)：`docs/文档治理与归档策略.md`，文档治理入口和权威地图

## 当前实现基线

这些文档解释当前代码、API、数据库、测试和用户可见运行态。当前反馈闭环主对象是 `ImprovementItem`；文档中若出现 `优化批次`、`proposal` 等历史术语，只能作为迁移来源或归档证据阅读，不作为当前 API 或 UI 主流程依据。四阶段改进治理用户主流程术语以 [AgentGov术语与版本边界](./AgentGov术语与版本边界.md) 为准；与旧设计冲突时，以 [AgentGov 四阶段改进治理工作台 UI 整改方案](./AgentGov_四阶段改进治理工作台UI整改方案.md) 和四张效果图为准。

- [反馈闭环当前实现基线](./反馈闭环当前实现基线.md)：`docs/反馈闭环当前实现基线.md`
- [反馈闭环长期回归治理升级方案](./反馈闭环长期回归资产升级方案.md)：`docs/反馈闭环长期回归资产升级方案.md`

## 产品能力目标方案

这些文档定义尚未落地的单项产品能力。实现完成前不能据此推断当前 API、UI 或运行容器已提供对应功能；
字段级当前契约仍以 OpenAPI 为准。

- [业务 Agent Workspace 包导入与热加载产品工程方案](./业务AgentWorkspace包导入与热加载产品工程方案.md)：
  `docs/业务AgentWorkspace包导入与热加载产品工程方案.md`，定义可信内部 workspace 包导入、同 ID 覆盖、
  下一 turn 热加载、手工 pytest、Git 审计与人工回滚的目标产品和工程契约

## 四阶段改进治理工作台权威方案

这些文档面向跨代重建和设计评审，不自动替代当前实现基线。对于“改进治理工作台”的 UI、用户主链路、决策卡、面板入口、处理记录和效果图验收，四阶段整改方案是绝对依据；旧 ASCII 草图已归档，只能作为历史设计证据追溯。

实现整改的阅读路径是：

1. 先读 [AgentGov 四阶段改进治理工作台 UI 整改方案](./AgentGov_四阶段改进治理工作台UI整改方案.md)，它定义改进治理工作台的四阶段主链路、四张效果图、决策卡、面板入口、处理记录和代码整改原则。
2. 如需追溯旧 UI 草图、旧 UI 补充方案或历史核查报告，读 [归档入口](./archive/README.md)。

- [AgentGov 四阶段改进治理工作台 UI 整改方案](./AgentGov_四阶段改进治理工作台UI整改方案.md)：`docs/AgentGov_四阶段改进治理工作台UI整改方案.md`

## 接口与示例

- [AgentGov集成指南](./AgentGov集成指南.md)：`docs/AgentGov集成指南.md`，上层业务系统集成 AgentGov 底座的权威集成参考（认证、概念模型、集成旅程、边界归属、反模式；契约真相源是 OpenAPI）

## 工程治理

- [CI/CD 宪法与交付链两阶段整改计划](./engineering/CI-CD宪法与交付链两阶段整改计划.md)：`docs/engineering/CI-CD宪法与交付链两阶段整改计划.md`，待评审、尚未实施的目标整改方案；定义可信 Staging、同一制品、数据恢复、外置信任锚和生产蓝绿准入，不表示当前运行态已具备这些能力
- [PAT-only 持续交付与 Staging 发布](./engineering/PAT-only持续交付与Staging发布.md)：`docs/engineering/PAT-only持续交付与Staging发布.md`，228 轮询控制器、精确 SHA 发布、凭据边界和 232 自动回滚的当前工程契约
- [测试资产组合治理](./engineering/测试资产组合治理.md)：`docs/engineering/测试资产组合治理.md`，测试分类、生命周期、执行通道、可信证据、TIA/xdist 晋级和 mutation 的权威工程契约
- [长程重构质量闭环](./engineering/长程重构质量闭环.md)：`docs/engineering/长程重构质量闭环.md`
- [GSD长程重构阶段清单](./engineering/GSD长程重构阶段清单.md)：`docs/engineering/GSD长程重构阶段清单.md`
- [vLLM 模型网关与 Sidecar 整改方案](./engineering/vLLM模型网关与Sidecar整改优化方案.md)：`docs/engineering/vLLM模型网关与Sidecar整改优化方案.md`
- [OpenAI 兼容接口能否替代原生 Chat 端点评估](./engineering/OpenAI兼容接口能否替代原生Chat端点评估.md)：`docs/engineering/OpenAI兼容接口能否替代原生Chat端点评估.md`，已落地的 Responses-first ADR 与当前工程契约；`/v1/responses` + `/v1/conversations` 为 canonical，chat/completions 与旧 `/api/chat*` 只作兼容面

## 评审与复盘

- 代码与文档评审报告：`docs/code_review_reports/`
  - [代码与文档评审报告第二轮](./code_review_reports/代码与文档评审报告第二轮.md)：`docs/code_review_reports/代码与文档评审报告第二轮.md`
  - [项目第一性原理对抗审查报告第三轮](./code_review_reports/项目第一性原理对抗审查报告第三轮.md)：`docs/code_review_reports/项目第一性原理对抗审查报告第三轮.md`
- 设计评审报告：`docs/design_review_report/`
  - [Agent版本治理与Diff对比重构方案评审报告v2](./design_review_report/Agent版本治理与Diff对比重构方案评审报告v2.md)：`docs/design_review_report/Agent版本治理与Diff对比重构方案评审报告v2.md`
- Codex/Claude 配置治理复盘：`docs/codex_setting_review_reports/`
  - [智能体治理反思与改进方案第二轮](./codex_setting_review_reports/智能体治理反思与改进方案第二轮.md)：`docs/codex_setting_review_reports/智能体治理反思与改进方案第二轮.md`
  - [四阶段改进治理工作台反复整改复盘](./codex_setting_review_reports/四阶段改进治理工作台反复整改复盘.md)：`docs/codex_setting_review_reports/四阶段改进治理工作台反复整改复盘.md`

## 归档入口

- [归档文档索引](./archive/README.md)：`docs/archive/README.md`

## 图片资产

- 闭环机制图片：`docs/imgs/`
