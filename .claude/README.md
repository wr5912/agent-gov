# .claude 分层说明

本目录保存 Claude Code 项目配置。通用规则只承载团队统一工作方式、环境硬约束和可迁移治理规则；项目专属路径、脚本、自动化门禁、base-ref 策略、端口、运行态数据路径和产品不变量放在目标项目 `.claude/rules/<project>.md`，确保从仓库子目录启动时也会加载。

## 项目共享内容

- `settings.json`: 可提交的 Claude Code 项目级设置；当前对内建 Read 工具限制私有 env 读取、
  要求 env 写入确认，并在 SessionStart 注入语言约束、在 Stop 运行项目治理硬门。
  Read deny 不约束 Bash/Python 子进程，不应表述为 OS 安全边界；严格隔离需另行启用
  Claude sandbox 或组织级 managed policy。
- `rules/project.md`: 通用工作流和执行顺序。
- `rules/architecture.md`: 通用架构卫生阈值。
- `rules/verify.md`: 通用验证要求。
- `rules/agentgov-project.md`: AgentGov 项目专属红线、治理入口和运行边界。
- `skills/project-skill/SKILL.md`: 团队开发通用技能。
- `agents/project-worker.md`: 可选项目 worker 子代理。

## 启动边界

当前 Claude Code 2.1.206 从仓库子目录启动时不会向上加载根 `.claude/settings.json`，
因此项目权限规则与 Stop hook 会缺失。需要项目硬门的开发会话统一通过
`python3 scripts/run_claude.py` 启动；该 launcher 会先切到仓库根，再原样传递 CLI 参数。
项目专属模型约束放在 `rules/agentgov-project.md`，根目录和子目录启动均能发现。

## 本仓库专项技能镜像

以下技能是本仓库项目专项能力的 Claude 侧镜像，修改时需与 `.codex/skills/` 中的同名技能同步：

- `skills/agentgov-governance-preflight/SKILL.md`: AgentGov 产品定位、目标愿景使命、反馈闭环治理、多业务 Agent 和 prompt/skill/SOP/eval 资产沉淀的治理对象预检。
- `skills/runtime-env-governance/SKILL.md`: 治理本仓库 runtime/env、本机调试、容器部署、Langfuse 和后台 Agent job 模型凭据边界。
- `skills/docs-governance/SKILL.md`: 治理本仓库 `docs/` 文档新增、迁移、归档、入口索引和 Codex/Claude skill 镜像同步；只处理文档容器治理，产品内容建模仍走 `agentgov-governance-preflight`。
- `skills/agentgov-closeout-sync/SKILL.md`: 阶段收尾、里程碑交接或发版前后，同步 README、docs、项目规则、skill 镜像和记忆边界；触发范围窄于普通文档治理，不替代各专项 skill。
- `skills/business-agent-workspace-optimizer/SKILL.md`: 离线开发、配置和优化单个业务 Agent workspace 配置资产；不代表产品内置自优化能力。
- `skills/test-sync-governance/SKILL.md`: 迭代功能时治理测试的增删改，识别陈测与脆测；配合 `scripts/check_orphan_tests.py` 孤儿检测，覆盖率门只防欠测。
- `skills/improvement-workbench-contract-preflight/SKILL.md`: 整改四阶段改进治理工作台、反馈闭环 UI、Diff、执行优化、测试用例或 Trace/Langfuse 前，固定业务产物归属、字段所有权、动作副作用和负向验收。

## 复用要求

- 新项目应删除 `rules/agentgov-project.md`，并创建自己的项目规则文件。
- 默认模板不得硬编码项目私有脚本名、自动化门禁、端口、volume 路径、API key 名称或产品不变量。
- 复制到新项目时必须删除本仓库 Stop hook，再由目标项目用自己的稳定、无交互、可重复命令重新配置。
- 个人偏好、本机路径、私有 MCP server 和真实凭据不得进入本目录。
