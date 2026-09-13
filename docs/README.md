# AgentGov 文档索引

本索引用于让 `docs/` 文档按版本、用途和权威性可发现，并配合 `scripts/check_docs_governance.py` 全量检查当前活跃/归档入口、相对链接、skill 镜像和治理标记。

阅读顺序建议：

1. 先读“权威入口”，明确产品边界、术语和验收锚点。
2. 需要评审尚未落地的长期能力时，读“产品能力目标方案”，不要将其当作当前实现。
3. 需要理解当前代码和运行态时，读“当前实现基线”。
4. 需要理解业务 Agent Workspace 包、创建入口和运行卷初始化时，读“当前实现基线”中的对应工程契约。
5. 需要评审四阶段改进治理目标方案时，读“四阶段改进治理工作台权威方案”。
6. 需要落地工程质量、GSD 或治理硬门时，读“工程治理”。
7. 需要追溯旧评审、旧补充方案或治理反思时，读“归档入口”。

文档状态说明：

- **权威入口**：长期产品口径、术语和验收锚点，优先级最高。
- **当前实现基线**：解释当前代码和运行态；历史 API、数据库和 UI 名称只在明确标注的迁移说明中出现。
- **产品能力目标方案**：定义尚未落地的单项产品能力、公开契约和工程边界；不表示当前 OpenAPI 已支持。
- **四阶段改进治理方案**：面向目标态的产品方案，不自动说明当前代码已经实现；其中四阶段改进治理工作台方案是改进治理工作台 UI、流程和效果图验收的绝对依据。
- **评审/复盘**：保留证据链和审查意见，不作为主实现方案；若与主方案冲突，以对应权威入口或主方案为准。
- **工程治理**：约束协作、测试、发布和治理硬门，不承载产品愿景。
- **归档**：已被替代但仍有审计价值的历史文档，从活跃阅读路径移入 `docs/archive/`。

## 权威入口

- [项目目标愿景使命](./项目目标愿景使命.md)：`docs/项目目标愿景使命.md`，定义长期定位、七个平台面、
  三类一级资产、评测关系、控制面边界与平台成功度量
- [AgentGov核心功能测试用例](./AgentGov核心功能测试用例.md)：`docs/AgentGov核心功能测试用例.md`
- [AgentGov术语与版本边界](./AgentGov术语与版本边界.md)：`docs/AgentGov术语与版本边界.md`
- [文档治理与归档策略](./文档治理与归档策略.md)：`docs/文档治理与归档策略.md`，文档治理入口和权威地图

## 产品能力目标方案

这些文档定义尚未落地的长期产品能力和工程边界，不能作为当前 OpenAPI、数据库或运行态已经支持对应能力的依据。

- [网络安全智能体测评工程需求](./网络安全智能体测评工程需求文档.md)：
  `docs/网络安全智能体测评工程需求文档.md`，定义网络安全垂域业务 Agent 的分层测评、事实图、
  工具轨迹、安全门槛、回归闭环、MVP 范围和实施决策门；领域概念不构成当前 API 或存储 schema
- [Governor 自研究与受控自学习能力需求](./Governor自研究与受控自学习能力需求.md)：
  `docs/Governor自研究与受控自学习能力需求.md`，定义 Governor 以内生证据为主、联网研究为辅，
  通过独立评估和人工启用持续提升反馈分析优化闭环能力的产品需求、治理边界、成功指标和验收场景

## 下一阶段实施方案

这些文档是 2026-07-30 基于当时实现证据形成、并于 2026-08-05 完成长远平台边界复核的工程
评审稿。旧 P0/P0-MCP 与 P2A Claude Runtime 路线已被 AgentScope 原子切换取代并移入归档；
其余方案只描述各自尚未落地的治理能力，不得用旧阶段依赖解释当前 Runtime。当前运行态以根
`README.md` 的 AgentScope 公共契约为准。

- [AgentGov 下一阶段实施方案索引](./AgentGov下一阶段实施方案索引.md)：
  `docs/AgentGov下一阶段实施方案索引.md`，统一说明阶段结论、权威关系、准入证据、治理对象、
  依赖顺序、全局裁决和评审清单
- [P1 网络安全测评纵向闭环实施方案](./engineering/AgentGov下一阶段P1网络安全测评纵向闭环实施方案.md)：
  `docs/engineering/AgentGov下一阶段P1网络安全测评纵向闭环实施方案.md`，以 8 个静态 L2 案例
  建立 Agent 自有可见回归包与 evaluator-owned 发布基准分权、typed 评分、安全否决、版本比较和
  精确发布闭环；本阶段只称协议化回归/发布准入，不以通过结果宣称整体能力提升
- [P2B Governor 受控学习基础实施方案](./engineering/AgentGov下一阶段P2BGovernor受控学习基础实施方案.md)：
  `docs/engineering/AgentGov下一阶段P2BGovernor受控学习基础实施方案.md`，建立不可变证据、
  方法候选、不可变 capability build、ApplicabilityScope 和盲化隔离评估，候选保持 shadow
- [P3 扩展组合准入框架](./engineering/AgentGov下一阶段P3扩展准入实施方案.md)：
  `docs/engineering/AgentGov下一阶段P3扩展准入实施方案.md`，分别约束统一 EvalOps、资产关系与
  能力包、单组织控制面、数据治理、SLO/成本，以及安全完整 MVP、Runtime 公共迁移、Governor
  受控激活和通用外部 adapter 的启动门

## 当前实现基线

这些文档解释当前代码、API、数据库、测试和用户可见运行态。当前反馈闭环主对象是 `ImprovementItem`；文档中若出现 `优化批次`、`proposal` 等历史术语，只能作为迁移来源或归档证据阅读，不作为当前 API 或 UI 主流程依据。四阶段改进治理用户主流程术语以 [AgentGov术语与版本边界](./AgentGov术语与版本边界.md) 为准；与旧设计冲突时，以 [AgentGov 四阶段改进治理工作台 UI 整改方案](./AgentGov_四阶段改进治理工作台UI整改方案.md) 和四张效果图为准。

- [反馈闭环当前实现基线](./反馈闭环当前实现基线.md)：`docs/反馈闭环当前实现基线.md`
- [AgentGov AgentScope Runtime 替换实施基线与验收](./engineering/AgentGov_AgentScope_Runtime替换实施基线与验收.md)：
  `docs/engineering/AgentGov_AgentScope_Runtime替换实施基线与验收.md`，吸收替换方案中的事实源、
  Harness、切换恢复和验收要求，标明当前实现与 50-run、浏览器等完整验收的边界
- [AgentScope 与 Langfuse 观测契约及验收](./engineering/AgentScope与Langfuse观测契约及验收.md)：
  `docs/engineering/AgentScope与Langfuse观测契约及验收.md`，说明 run/Trace 关联、安全出口、
  完整性对账及验收，并区分 AgentScope 示例应用原始需求与 AgentGov 当前实现
- [业务 Agent Workspace 原生 pytest 测试资产实现方案](./engineering/业务AgentWorkspace原生pytest测试资产实现方案.md)：
  `docs/engineering/业务AgentWorkspace原生pytest测试资产实现方案.md`，定义测试资产唯一真相、
  `agentgov_testkit`、精确提交运行、服务重启恢复和发布条件
- [业务 Agent Workspace 包导入与热加载产品工程方案](./业务AgentWorkspace包导入与热加载产品工程方案.md)：
  `docs/业务AgentWorkspace包导入与热加载产品工程方案.md`，定义原生 schema 表单与 Workspace 包统一
  进入 Git 候选、同 ID 候选导入、测试审批发布、不可变 Session 绑定、Git 审计、删除与运行卷初始化；
  字段级真相源仍是 OpenAPI

## 四阶段改进治理工作台权威方案

这些文档面向跨代重建和设计评审，不自动替代当前实现基线。对于“改进治理工作台”的 UI、用户主链路、决策卡、面板入口、处理记录和效果图验收，四阶段整改方案是绝对依据；旧 ASCII 草图已归档，只能作为历史设计证据追溯。

实现整改的阅读路径是：

1. 先读 [AgentGov 四阶段改进治理工作台 UI 整改方案](./AgentGov_四阶段改进治理工作台UI整改方案.md)，它定义改进治理工作台的四阶段主链路、四张效果图、决策卡、面板入口、处理记录和代码整改原则。
2. 如需追溯旧 UI 草图、旧 UI 补充方案或历史核查报告，读 [归档入口](./archive/README.md)。

- [AgentGov 四阶段改进治理工作台 UI 整改方案](./AgentGov_四阶段改进治理工作台UI整改方案.md)：`docs/AgentGov_四阶段改进治理工作台UI整改方案.md`

## 接口与示例

- [AgentGov集成指南](./AgentGov集成指南.md)：`docs/AgentGov集成指南.md`，上层业务系统集成 AgentGov 底座的权威集成参考（认证、概念模型、集成旅程、边界归属、反模式；契约真相源是 OpenAPI）

## 工程治理

- [AgentScope API 最大复用与单轨整改实施与验收记录](./engineering/AgentScope_API最大复用与单轨整改计划.md)：
  `docs/engineering/AgentScope_API最大复用与单轨整改计划.md`，记录基于 AgentScope 2.0.8 公共 API
  完成的 P0–P4 单轨整改、schema 迁移边界及 P5 真实容器／浏览器验收状态；实现完成不等于真实验收通过
- [测试资产组合治理](./engineering/测试资产组合治理.md)：`docs/engineering/测试资产组合治理.md`，
  测试分类、业务 Agent 自有回归与独立发布评测包分权、生命周期、执行通道、可信证据、TIA/xdist
  晋级和 mutation 的权威工程契约
- [长程重构质量闭环](./engineering/长程重构质量闭环.md)：`docs/engineering/长程重构质量闭环.md`
- [GSD长程重构阶段清单](./engineering/GSD长程重构阶段清单.md)：`docs/engineering/GSD长程重构阶段清单.md`
- [AgentScope Runtime 当前架构与公共契约](../README.md#agentscope-runtime-公共契约)：以仓库
  `README.md` 的三服务架构、Runtime 公共契约、标识映射、Harness、验收和 OTel/Langfuse
  章节为当前权威入口；它已取代 Claude/LiteLLM Sidecar、Speech Summary 和旧 Responses
  执行链路设计。实施决策和验收门槛见“当前实现基线”中的 Runtime 替换与观测专项文档；
  被取代的方案仅保留在 [归档入口](./archive/README.md) 供审计追溯

## 评审与复盘

- 代码与文档评审报告：`docs/code_review_reports/`
  - [898c2d5 及当前更改第一性原理对抗审查报告](./code_review_reports/898c2d5及当前更改第一性原理对抗审查报告.md)：`docs/code_review_reports/898c2d5及当前更改第一性原理对抗审查报告.md`，区分提交与当前缺陷，记录只读取证、门禁与验收边界；第 10 节补充文档权威、时效、阶段依赖和重复契约审查
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
