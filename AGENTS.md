# AgentGov Codex 项目说明

本文件是本仓库唯一的根级 Codex 指令入口。Codex 在同一目录只加载
`AGENTS.override.md` 或 `AGENTS.md` 中优先命中的一个文件，因此根目录不得再创建
同级 `AGENTS.override.md`。团队通用约束和本仓库专属约束在这里提供有效入口，详细
流程按需进入 `.codex/guidance/`、`.codex/skills/`、脚本和 docs，避免并行真相源。

## 读取顺序

开始非琐碎任务前按顺序读取：

1. `.codex/guidance/project.md`，并继续读取其中声明的 `architecture.md` 与 `verify.md`。
2. 涉及代码、配置、测试、构建、重构、调试或评审时，读取
   `.codex/skills/project-skill/SKILL.md`。
3. 根据任务触发对应专项 skill；不要把专项矩阵复制回本文件。
4. 长程重构、GSD 或跨层契约收口还要读取 `.planning/METHODOLOGY.md`。

本仓库治理硬门来源与长程方法论：

- `docs/codex_setting_review_reports/智能体治理反思与改进方案第二轮.md`
- `docs/engineering/长程重构质量闭环.md`
- `docs/engineering/GSD长程重构阶段清单.md`

专项 skill 触发入口：

- Codex 配置、AGENTS/rules/hooks/skill 噪声或重复：`codex-config-optimizer`
- docs 新增、迁移、归档、删除、索引或引用：`docs-governance`
- runtime/env、Docker、PyCharm、Langfuse、volume 或模型凭据：`runtime-env-governance`
- 功能增删改、重构或测试同步：`test-sync-governance`
- AgentGov 定位、反馈治理、多业务 Agent 或治理资产沉淀：`agentgov-governance-preflight`
- 四阶段工作台、反馈闭环 UI、Diff、执行、测试用例或 Trace：
  `improvement-workbench-contract-preflight`
- 单个业务 Agent workspace 配置资产：`business-agent-workspace-optimizer`
- 阶段收尾、里程碑交接或发版同步：`agentgov-closeout-sync`

## 协作与工作流

- 所有对话、文档、代码注释和提交说明默认使用中文；代码标识符、命令、日志关键字
  和第三方 API 名称保留原文。
- 非琐碎变更遵循 Analyze -> Plan -> Execute -> Verify。
- 写文件前说明涉及文件、逻辑变化、架构阈值、验证方式、Docker 卷影响和测试分层。
- 能从仓库和官方文档确认的事实先只读确认。只有存在不可发现的关键歧义、破坏性操作、
  安全风险或公开契约风险时才停下来请求确认；普通实现任务按已确认目标持续完成。
- 评审以缺陷、风险、行为回归和缺失测试为主，结论必须有当前文件、命令或运行态证据。

## 核心架构原则

本仓库以 `claude-agent-sdk` 及其捆绑的 Claude Code agent 为中心。后端只是交互接口、
确定性编排和反馈优化闭环的薄层。

- Agent/SDK 是会话、消息、trace、session 元数据和子 Agent 行为事实的单一真相源。
- 优先使用 SDK 原生 `get_session_messages`、`get_session_info`、`list_sessions`、
  `get_subagent_messages`、`SessionStore` 等能力，不手解析 CLI transcript，也不另建并行副本。
- 后端只做 API 契约、证据投影和治理编排；交互与优化共享同一份 Agent 行为事实。
- 不重写或绕过 agent loop。权限、MCP、hooks、skills、subagents 以 Claude Code 原生发现
  和配置为准，后端不通过 Options 接管。
- 新增后端 schema、存储或解析前，必须先证明 SDK/agent 没有持有或暴露同一事实。

## 质量优先与旧设计替换

优先级是：代码质量 > 新设计/框架/架构 > 旧模式兼容或保留。用户要求重构、去旧设计、
优化架构或提高质量，或分析发现旧 facade、兼容 shim、历史路径、重复实现、schema 双轨、
状态分散、过期 API、不可达分支时，进入替换旧设计模式。

该模式下先列删除、迁移、保留清单，并覆盖：

- 公开 API、OpenAPI、前端生成类型；
- 配置、环境变量、Docker 路径和 `config/*`；
- SQLite、`${HOME}/volume-agent-gov`、迁移前 `docker/volume/` 与迁移脚本；
- README、docs、测试、治理硬门和内部兼容层。

历史 job/payload 不作为永久产品契约。保留旧实现必须有公开契约、安全或迁移依据，并写明
退出条件；不得以“精准改动”“匹配现有风格”或“外部行为不变”延续已确认的旧设计。

## 反复整改前置矩阵

任务同时涉及实现、docs、skill、runtime/env、测试、UI 或部署中的两个以上配置面，或用户
要求“举一反三”“触类旁通”时，Analyze 阶段先给短矩阵：

- 治理对象矩阵：业务 Agent、治理 Agent、`main` 样板、runtime data、template workspace、
  开发者离线工具。
- 配置面矩阵：当前 prompt、根 `AGENTS.md` / `.claude/rules/agentgov-project.md`、guidance/rules、skill、script、
  hook、docs、memory。
- 验收路径矩阵：docs/skill、专项测试、主流程测试、真实容器、发版硬门；不得用 local-debug 结果声明容器验收通过。
- UI 语义矩阵：按钮、业务产物、API 副作用、状态推进、容器和信息归属，以及不应混入的内容。
  决策卡主按钮必须完成业务动作，状态推进只能是副作用。

## 反馈闭环与 Agent Job 契约

反馈优化、归因、批次方案、Agent job、DSPy formatter、提示词、OpenAPI 或前端类型默认按
产品级契约问题处理。排查必须贯通 UI/API -> `agent_jobs` -> store 投影 -> formatter ->
持久化 payload，并核查空态、成功态、失败详情和重试入口。

- `error_json`、`raw_output_json`、`validated_output_json`、聚合字段和页面状态必须属于同一契约。
- 单条与批次优化方案复用同一生成契约；保留分支必须证明不可合并。
- 旧 proposal job/路由/prompt/signature/UI/生成类型必须从活跃主流程清零。
- DSPy 成功输出以内层 Pydantic OutputModel 实例继续流转，不在 formatter 边界立即 dump。
- 无外部协议、多版本运行时或历史迁移需求时，不新增输出 `schema_version`。
- job type、profile、prompt builder、Signature 和 OutputModel 收口到集中注册表。
- prompt/Signature 只要求 agent-owned 业务语义。job id、workflow/case ids、时间戳、scope、
  provenance 等 backend-owned 字段由后端注入或覆盖；JSON 只在 DB/HTTP/文件/日志边界生成。
- Formatter、projection 和 boundary payload 的类型与命名必须区分
  `raw_agent_text -> formatter_output -> projected_output -> raw_output_json`。

## 架构卫生

以下任一命中都必须在计划中先处理边界，不能以治理脚本未报警代替架构判断：

- 手写代码文件超过 800 行、类超过 30 个公开方法、函数超过 80 行或圈复杂度超过 15；
- 路由文件超过 20 个路由，或即将复制超过 40 行现有代码；
- 同一职责在 3 个以上文件靠字符串字面量耦合；
- 同一实体存在多套手写 schema/DTO/ORM/序列化表示且无派生关系；
- 持久化生命周期状态缺集中状态集合、完整转移表、统一校验 helper 或非法转移测试；
- 跨表、跨文件、跨服务或 DB + 文件系统更新缺幂等、回滚或部分失败处理；
- 事务块内执行 `rmtree`、外部通知或远程调用等不可回滚副作用；
- 旧 facade、shim、历史路径/格式、过期 API、不可达 UI 或调试脚本仍参与主流程；
- 修改 API、settings、Docker env、公开 schema 或默认值而不更新 README/docs。

治理硬门按 git base 只阻断新增债或旧债增长；`BASELINE` 不是“无问题”，普通 docs/issue
也不能作为机器放行豁免。

## 测试与验证

功能迭代先按 `test-sync-governance` 填测试同步矩阵。删除行为同步删陈测；重构优先测行为和
契约，不测私有实现细节。测试深度随风险增加：

- 日常红绿循环运行目标 pytest/前端检查。
- 修改反馈主流程、Agent job、formatter、store、API response 或用户可见 tab 时，运行
  `make main-flow-test`，并同步 `tests/coverage_policy.json` 的场景绑定。
- UI 空态、成功态、失败详情必须有场景证据，不能用后端单测或 coverage 百分比替代。
- 生命周期加非法转移测试；并发资源加重复/竞争/部分失败测试；外部输入加异常/恶意/越权测试；
  Agent 输出契约加 hostile backend-owned 字段污染测试。
- 提交、CI、发版或用户要求完整验证时运行 `make test`；专项覆盖率调整运行 `make coverage`。

本仓库非琐碎代码、配置、测试和治理文档变更必须运行：

```bash
make codex-guard
```

`warn`/dry-run 只用于 Analyze，不能作为 Verify 通过标准。`.github/workflows/governance.yml`
是 PR 以及 `main`/`master` push 的 CI 入口。

## Runtime / Env 与安全边界

- 离线模式是产品不变量，但始终会提供本地化 LLM；必需工作流不得依赖远程服务。
- Docker 持久化宿主机根目录统一为 `${HOME}/volume-agent-gov`；`docker/volume/` 只作迁移来源。
- `docker/.env` 服务 Compose/API/worker 容器；`docker/.env.local-debug` 服务宿主机 Python/
  PyCharm；`frontend/.env.local` 只服务 Vite。它们是按运行环境选择，不是 layered override。
- 宿主机进程自动选择本机调试 env，容器由 Compose 注入 `RUNTIME_CONTAINER=1` 选择容器 env。
- 本机后台 Agent job 不复用交互式 Claude `/login`；真实运行前私有 env 必须提供
  `MODEL_PROVIDER_API_KEY`，但真实值不得进入仓库、日志、文档、提交说明或最终回复。
- API key、MCP header、数据库凭据、本机私有路径、运行态 SQLite 和私有日志不得提交。
- 当前 Playground、调试 UI 和自托管 Langfuse 是开发观测面，可保留完整 prompt/tool/job/trace
  I/O；该例外不放宽仓库和对外输出边界。
- 修改 env 选择、路径、模型凭据或 Langfuse 地址时，同步 README、示例、policy 测试和启动日志。

## 文档与配置治理

- 活跃文档入口是 `docs/README.md`，归档入口是 `docs/archive/README.md`。
- 新增、移动、归档或删除 docs 前使用 `docs-governance`；先检查相对链接、旧路径引用、
  文档契约测试和 Codex/Claude skill 镜像。
- 常驻根指令只保留稳定不变量和触发入口；按需流程进 skill，可机械判定的规则进 script/hook。
- `.codex/guidance/*.md` 承载根 `AGENTS.md` 显式引用的模型治理说明；
  `.codex/rules/*.rules` 只保留 Codex `prefix_rule(...)` 命令执行策略，不放散文说明。
- 不把本仓库脚本名、CI、路径或产品不变量复制到团队通用 skill/rules 模板。

## 版本与提交

- 根 `VERSION` 是版本唯一真相源；`app/version.py`、前端 version 和 Compose 镜像 tag 由它派生。
- 默认不 bump VERSION、不创建或推送 tag。发布点由用户确认后用 `make tag`，并确保 `v<VERSION>`
  与 VERSION 一致；发现 VERSION 领先远端 tag 时只提示，不擅自补 tag。
- 用户要求提交时先审 diff。普通小改可只有标题；涉及 API/schema/runtime data/部署/主流程、
  删除旧入口或跨两个以上配置面的提交必须有 `Compatibility` 和 `Verification` 正文。
- 提交说明只写工程意图、兼容影响和已执行验证，不写密钥、私有路径或 prompt 流水账。
