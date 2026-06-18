# AgentGov 文档索引

本索引用于让 `docs/` 文档按版本、用途和权威性可发现，并配合 `scripts/check_docs_governance.py` 检查新增文档是否进入治理入口。

阅读顺序建议：

1. 先读“权威入口”，明确产品边界、术语和验收锚点。
2. 需要理解当前代码和运行态时，读“当前实现基线（pre-v2.7）”。
3. 需要评审下一大版本时，读“下一大版本 v2.7 规划与审查”。
4. 需要落地工程质量、GSD 或治理硬门时，读“工程治理”。

## 权威入口

- [项目目标愿景使命](./项目目标愿景使命.md)：`docs/项目目标愿景使命.md`
- [AgentGov核心功能测试用例](./AgentGov核心功能测试用例.md)：`docs/AgentGov核心功能测试用例.md`
- [AgentGov术语与版本边界](./AgentGov术语与版本边界.md)：`docs/AgentGov术语与版本边界.md`
- [文档治理与归档策略](./文档治理与归档策略.md)：`docs/文档治理与归档策略.md`

## 当前实现基线（pre-v2.7）

这些文档解释当前代码、API、数据库、测试和用户可见运行态。文档中出现的 `main-agent`、`反馈信息`、`优化批次`、`proposal` 等名称属于当前实现事实；v2.7 用户主流程术语以 [AgentGov术语与版本边界](./AgentGov术语与版本边界.md) 为准。

- [反馈优化产品调整方案](./反馈优化产品调整方案.md)：`docs/反馈优化产品调整方案.md`
- [反馈优化闭环多智能体架构](./反馈优化闭环多智能体架构.md)：`docs/反馈优化闭环多智能体架构.md`
- [反馈闭环机制全景画像](./反馈闭环机制全景画像.md)：`docs/反馈闭环机制全景画像.md`
- [反馈闭环长期回归资产升级方案](./反馈闭环长期回归资产升级方案.md)：`docs/反馈闭环长期回归资产升级方案.md`
- [Agent版本治理与Diff对比重构方案](./Agent版本治理与Diff对比重构方案.md)：`docs/Agent版本治理与Diff对比重构方案.md`
- [多业务Agent治理基座设计](./多业务Agent治理基座设计.md)：`docs/多业务Agent治理基座设计.md`

## 下一大版本 v2.7 规划与审查

这些文档面向跨代重建和设计评审，不自动替代当前实现基线。实现整改应以草图方案为目标，以设计一致性报告作为差距清单。

- [AgentGov ASCII UI 草图方案 v2.7（跨代重建）](./AgentGov_ASCII_UI_草图方案_v2.7.md)：`docs/AgentGov_ASCII_UI_草图方案_v2.7.md`
- [AgentGov v2.7 UI 设计一致性核查与整改报告](./design_review_report/AgentGov_v2.7_UI_设计一致性核查与整改报告.md)：`docs/design_review_report/AgentGov_v2.7_UI_设计一致性核查与整改报告.md`

## 接口与示例

- [外部治理Webhook示例](./外部治理Webhook示例.yaml)：`docs/外部治理Webhook示例.yaml`

## 工程治理

- [长程重构质量闭环](./engineering/长程重构质量闭环.md)：`docs/engineering/长程重构质量闭环.md`
- [GSD长程重构阶段清单](./engineering/GSD长程重构阶段清单.md)：`docs/engineering/GSD长程重构阶段清单.md`
- [AgentGov目标达成分阶段执行计划](./engineering/AgentGov目标达成分阶段执行计划.md)：`docs/engineering/AgentGov目标达成分阶段执行计划.md`
- [治理 Agent 合并为单一 Governor 设计](./engineering/Governor合并设计.md)：`docs/engineering/Governor合并设计.md`

## 评审与复盘

- 代码与文档评审报告：`docs/code_review_reports/`
  - [代码与文档评审报告](./code_review_reports/代码与文档评审报告.md)：`docs/code_review_reports/代码与文档评审报告.md`
  - [代码与文档评审报告第二轮](./code_review_reports/代码与文档评审报告第二轮.md)：`docs/code_review_reports/代码与文档评审报告第二轮.md`
- 设计评审报告：`docs/design_review_report/`
  - [反馈闭环长期回归资产升级方案评审报告](./design_review_report/反馈闭环长期回归资产升级方案评审报告.md)：`docs/design_review_report/反馈闭环长期回归资产升级方案评审报告.md`
  - [Agent版本治理与Diff对比重构方案评审报告](./design_review_report/Agent版本治理与Diff对比重构方案评审报告.md)：`docs/design_review_report/Agent版本治理与Diff对比重构方案评审报告.md`
  - [Agent版本治理与Diff对比重构方案评审报告v2](./design_review_report/Agent版本治理与Diff对比重构方案评审报告v2.md)：`docs/design_review_report/Agent版本治理与Diff对比重构方案评审报告v2.md`
- Codex/Claude 配置治理复盘：`docs/codex_setting_review_reports/`
  - [智能体治理反思与改进方案](./codex_setting_review_reports/智能体治理反思与改进方案.md)：`docs/codex_setting_review_reports/智能体治理反思与改进方案.md`
  - [智能体治理反思与改进方案第二轮](./codex_setting_review_reports/智能体治理反思与改进方案第二轮.md)：`docs/codex_setting_review_reports/智能体治理反思与改进方案第二轮.md`

## 图片资产

- 闭环机制图片：`docs/imgs/`
