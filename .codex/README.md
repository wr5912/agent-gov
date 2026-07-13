# .codex 分层说明

本目录保存团队通用模板。复制到其他项目时，通用层只承载团队统一工作方式、环境硬约束和可迁移治理规则；项目专属路径、脚本、CI、base-ref 策略和产品不变量必须放在项目覆盖层。

## 可作为团队模板复用

- `config.toml`：安全、通用的 Codex 项目配置模板。
- `guidance/project.md`：模型治理入口和执行顺序。
- `guidance/architecture.md`：通用架构卫生阈值。
- `guidance/verify.md`：通用验证要求。
- `rules/*.rules`：仅用于 Codex `prefix_rule(...)` 命令执行策略；当前仓库没有项目级策略。
- `skills/project-skill/SKILL.md`：团队开发通用技能。
- `agents/worker.toml`：通用项目 worker 画像。

## 本仓库专项技能

- `skills/codex-config-optimizer/SKILL.md`：治理 Codex 配置、AGENTS/rules/hooks 和 skill 膨胀问题；首版为本仓库本地技能，验证稳定并泛化后再考虑提取为团队通用 skill。
- `skills/agentgov-governance-preflight/SKILL.md`：AgentGov 产品定位、目标愿景使命、反馈闭环、多业务 Agent 和 prompt/skill/SOP/eval 资产沉淀的治理对象预检。
- `skills/runtime-env-governance/SKILL.md`：治理本仓库 runtime/env、本机调试、容器部署、Langfuse 和后台 Agent job 模型凭据边界；这是项目专项技能，不应作为团队通用模板直接复制。
- `skills/docs-governance/SKILL.md`：治理本仓库 `docs/` 文档新增、迁移、归档、入口索引和 Claude/Codex skill 镜像同步；只处理文档容器治理，产品内容建模仍走 `agentgov-governance-preflight`。
- `skills/agentgov-closeout-sync/SKILL.md`：阶段收尾、里程碑交接或发版前后，同步 README、docs、项目规则、skill 镜像和记忆边界；触发范围窄于普通文档治理，不替代各专项 skill。
- `skills/business-agent-workspace-optimizer/SKILL.md`：离线开发、配置和优化单个业务 Agent workspace 配置资产；不代表产品内置自优化能力。
- `skills/test-sync-governance/SKILL.md`：迭代功能时治理测试增删改，识别陈测与脆测；配合 `scripts/check_orphan_tests.py` 孤儿检测，覆盖率门只防欠测。
- `skills/improvement-workbench-contract-preflight/SKILL.md`：整改四阶段改进治理工作台、反馈闭环 UI、Diff、执行优化、测试用例或 Trace/Langfuse 前，固定业务产物归属、字段所有权、动作副作用和负向验收。

## 通用入口配置

- `hooks.json`：本仓库已按根 `AGENTS.md` 接入 Stop 治理硬门；复制为通用模板前应清空项目命令，各项目在自己的根指令中重新声明并配置本地命令。

## 通用质量优先策略

通用层已包含“替换旧设计模式”：当用户要求重构、去除旧设计、优化架构、提高代码质量，或 Analyze 阶段发现旧 facade、兼容 shim、历史路径、重复实现、schema 双轨、状态分散、过期 API、不可达分支等旧债信号时，默认优先级为代码质量 > 新设计/框架/架构 > 旧模式兼容或保留。

涉及 Agent、DSPy、LLM 输出格式化或提示词输出要求时，通用层要求先区分 backend-owned、agent-owned 和 boundary-owned 字段；后端已知字段不得让 LLM 复述后作为权威输出。

项目专属兼容边界、迁移策略和治理硬门应写入目标仓库唯一有效的根 `AGENTS.md`；不得在同一目录并列创建会遮蔽它的 `AGENTS.override.md`。

`.codex` 不存放项目债务账本。治理通过 git base 对比执行：既有超限未增长输出 `BASELINE`，新增超限或旧债增长输出 `FAIL`。

## 复用要求

- 新项目应在自己的有效根 `AGENTS.md` 中声明治理命令、CI 的 base-ref 策略和产品不变量。
- 新项目必须保留团队统一环境硬约束：`.venv`、`uv`、`pnpm`、Python 3.10+、Node.js 20+、国内下载源和 Docker 目录规范。
- 旧债清单如需记录，应放在普通 docs 或 issue，不得放入 `.codex` 作为机器豁免。
