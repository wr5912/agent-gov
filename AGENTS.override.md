# agent-gov 项目覆盖说明

本文件只放本仓库专属约束。团队通用行为仍以 `AGENTS.md` 和 `.codex/rules/` 为准；复制通用模板到其他项目时，不应把本文件中的路径、命令和 CI 当作默认配置。

## 必读项目文档

- `docs/codex_setting_review_reports/智能体治理反思与改进方案第二轮.md`：解释本仓库治理硬门的来源、失效点和落地目标。
- `docs/engineering/长程重构质量闭环.md`：沉淀长程整改的边界定义、验证矩阵和治理硬门复用方法。
- `docs/engineering/GSD长程重构阶段清单.md`：GSD 阶段规划、执行、验证和发布前的质量清单。
- `.planning/METHODOLOGY.md`：GSD discuss/plan 阶段应读取的项目级方法论 lenses。

## 本仓库代码质量优先策略

本仓库执行“代码质量 > 新设计/框架/架构 > 旧模式兼容或保留”的优先级。凡用户提出重构、去除旧设计、优化架构、提高代码质量、引入更优秀设计方案/框架/架构，或 Analyze 阶段发现旧 facade、兼容 shim、历史路径、重复实现、schema 双轨、状态分散、过期 API、不可达分支等信号时，必须进入替换旧设计模式。

替换旧设计模式下，默认先规划删除、迁移或统一旧设计，不得继续沿用旧入口作为“匹配现有风格”。旧实现只有在逐案确认后才可保留；计划必须按以下边界说明：

- 公开 API / OpenAPI / 前端生成类型是否保留、改名、删除或迁移。
- 配置、环境变量、Docker 路径和 `config/*` 是否保留、改名、删除或迁移。
- 持久化数据、SQLite schema、`${HOME}/volume-agent-gov` 默认路径、`docker/volume/` 历史路径和迁移脚本如何处理。
- README、docs、测试和治理硬门如何验证新契约。
- 内部兼容 facade、shim、旧工作流分支、不可达 UI 和一次性调试脚本是否删除；如保留，必须写明明确期限或后续清理条件。

## 反复整改前置矩阵

当用户指出“举一反三”“触类旁通”“还有许多问题”“最近反复整改”，或任务同时涉及实现、docs、skill、runtime/env、测试、UI 设计一致性、部署生效中的两个及以上配置面时，Analyze 阶段必须先做短矩阵，再决定改哪层：

- 治理对象矩阵：区分业务 Agent、治理 Agent、`main` 样板、runtime data、template workspace 和开发者离线工具。
- 配置面矩阵：区分当前 prompt、`AGENTS.override.md` / `CLAUDE.project.md`、Codex/Claude rules、skill、script、hook、docs 和 memory；能按需触发的流程不写成长篇常驻规则。
- 验收路径矩阵：区分 docs/skill 治理、专项测试、主流程测试、真实容器验收和发版完整硬门；不得用 local-debug 结果声明容器验收通过。
- UI 语义矩阵：涉及 v2.7 或用户可见交互时，先确认按钮名称、抽屉/modal 容器、Trace/反馈/上下文/运行设置/会话管理的信息归属，并验证“不该混入的内容不存在”。

## 反馈闭环 / Agent Job / DSPy 契约专项要求

反馈优化、归因、批次优化方案、agent job、DSPy formatter、提示词、OpenAPI 和前端生成类型相关问题，默认按产品级契约问题处理，不得只按局部 bug 修补。

排查和整改必须同时核查：

- 用户可见页面状态、错误详情、重试按钮、tab 空状态和 API response 是否一致。
- `agent_jobs` 状态、`error_json`、`raw_output_json`、`validated_output_json`、store 投影和 batch/case 聚合字段是否在一个契约下收口。
- DSPy Signature、结构化提示词、输出 schema、normalizer、Pydantic record 和 response schema 是否存在双轨或重复约束。
- 单条反馈优化方案和批次优化方案是否复用同一优化方案生成契约；如果保留分支，必须说明不可合并原因。
- 旧 proposal job、旧 proposal 路由、旧 prompt、旧 formatter signature、旧前端入口和旧生成类型是否已经从活跃主流程清零。
- DSPy 输出成功后是否以内层 Pydantic OutputModel 实例继续流转；不得在 formatter 边界立即 dump 成 `JsonObject` 再靠 normalizer/schema version 二次证明结构。
- Agent 输出结构契约默认由 DSPy Signature + Pydantic OutputModel 表达；无外部协议、多版本运行时或历史迁移需求时，不得新增 `schema_version` / `output_schema_version` / `*_SCHEMA_VERSION`。
- job type、profile、prompt builder、DSPy Signature 和 OutputModel 必须收口到集中注册表，避免在 worker、runner、formatter、store 中重复 if/else。

历史 job 和历史 payload 不作为产品契约长期保留；失败记录如果会干扰当前批次或用户页面，应进入清理或迁移策略。任务失败必须投影到用户可见 tab/API 状态，不能只留在后端日志。

`docs/开放接口规范.json` 属于生成物边界：如果保留为离线 API 快照，必须有重新生成后 diff 干净的漂移检查；如果只服务前端类型生成，应改为临时生成文件而不是长期提交静态 JSON。

## 测试覆盖与主流程保障

本仓库区分本地开发验证和正式硬门，避免把全量 coverage 放进每次调试内循环：

- 日常局部开发默认运行目标 pytest，例如 `.venv/bin/python -m pytest -q tests/test_xxx.py::test_xxx`。
- 修改反馈优化主流程、Agent job、formatter、store 投影、API response 或用户可见 tab 状态时，必须运行 `make main-flow-test`，并确认 `tests/coverage_policy.json` 已绑定对应 pytest nodeid 或 UI verification script。
- 提交、CI、发版或用户明确要求完整验证时运行 `make test`；该命令会触发全量 pytest、coverage JSON 和 coverage policy 硬门。
- 专门调整覆盖率策略或提升覆盖率阈值时运行 `make coverage`。
- UI 空态、成功态、失败态错误详情属于主流程验收，不得只用后端 store 测试或 coverage 百分比代替。

`tests/coverage_policy.json` 是主流程覆盖清单和全局覆盖率基线的单一入口。新增或修改产品主流程时，必须同步更新该文件；全局覆盖率阈值先以当前实测基线禁止下降，再逐步提高，不追求一次性全仓库 100% 造成无意义测试或排除项。

迭代功能（新增/修改/删除行为、改契约、重构、删模块）时，先按 `.codex/skills/test-sync-governance/SKILL.md` 做测试增删改判断：填测试同步矩阵、按删测判定清理陈测、避免脆测；覆盖率门只防欠测，删功能必须同步删测。`scripts/check_orphan_tests.py` 是机械信号——检测 `tests/` 对 `app`/`scripts` 已删除模块或符号的 import 引用，已接入治理硬门。

## 本仓库治理硬门

本仓库的非琐碎代码、配置、测试和治理文档变更，必须运行：

```bash
.venv/bin/python scripts/check_codex_governance.py --mode fail
```

- `--mode warn` 只允许在 Analyze 阶段观察问题，不允许作为 Verify 通过标准。
- `make test` 已依赖 `codex-guard`，会先运行上述 fail 模式治理检查。
- `.codex/hooks.json` 的 `Stop` hook 会运行同一条治理硬门。
- `.github/workflows/governance.yml` 是本仓库 CI 入口，会在 PR 以及 `main` / `master` push 上运行治理硬门与测试。

## 本仓库差异治理

治理脚本通过 git base 对比约束新增债务和旧债增长：

- 本地 hook 默认对比 `HEAD`。
- PR CI 对比目标分支。
- `main` / `master` push CI 对比 `HEAD^`。
- 治理输出 `BASELINE` 表示既有超限未增长，不阻断；`FAIL` 表示新增超限或旧债增长。
- 旧债清单如需人工跟踪，只能放在普通 docs 或 issue 中，不得作为治理放行豁免。

## 本仓库产品与环境不变量

- 离线模式是产品不变量但始终会提供本地化LLM模型；必需工作流不得依赖远程服务。
- Docker 持久化默认宿主机根目录统一为 `${HOME}/volume-agent-gov`；`docker/volume/` 只作为迁移来源或显式兼容路径。
- 环境变量放在 `docker/.env`；应用配置放在 `config/*.yaml` 或 `config/*.json`。
- 不得将 API key、MCP header、数据库凭据或本机私有路径写入仓库。

## Runtime / Env 治理专项

涉及 `RUNTIME_CONTAINER`、`RUNTIME_VOLUME_MODE`、`docker/.env`、`docker/.env.local-debug`、`frontend/.env.local`、Docker volume、Langfuse、本机 PyCharm 调试或后台 Agent job 模型凭据的改动，必须先按 `.codex/skills/runtime-env-governance/SKILL.md` 做 Consumer x Mode x Boundary 矩阵，再执行。

- `docker/.env` 是 Compose/API/worker 容器部署环境文件；`docker/.env.local-debug` 是宿主机 Python/PyCharm 调试环境文件；`frontend/.env.local` 只服务 Vite 本机前端。
- `docker/.env.local-debug` 不参与“选择模式”；宿主机进程通过非容器环境自动选择它，容器进程由 Compose 注入 `RUNTIME_CONTAINER=1` 自动选择 `docker/.env`。
- 不要把上述文件关系描述成“覆盖”。除非代码真实实现 layered override，否则文档、计划和提交说明都应使用“选择 env 文件”“私有 env 文件”或“本机调试 env 文件”。
- 本机后台 Agent job 不复用交互式 Claude `/login` 状态；调试或生成回归用例前必须确认私有 `docker/.env.local-debug` 配置了 `MODEL_PROVIDER_API_KEY`，但真实 key 不得进入仓库。
- 修改 runtime/env 选择、默认路径、模型凭据或 Langfuse 地址时，必须同步 README、env 示例、settings/env policy 测试和启动日志字段验证。

## AgentGov 产品治理预检

涉及 AgentGov 产品定位、目标愿景使命、反馈闭环治理方案、多业务 Agent 创建与治理、prompt/skill/SOP/eval 资产沉淀类任务时，Analyze 阶段先按 `.codex/skills/agentgov-governance-preflight/SKILL.md` 做治理对象预检：区分业务 Agent 与治理 Agent、资产类型、反馈归属、生命周期、当前实现边界和目标能力边界，先给治理对象矩阵和闭环链路，再写文档或方案正文。普通代码实现、bug 修复、单文件改动不触发。

## 本仓库专属边界

- 不要把 `agent-gov` 的脚本名、CI workflow 或 hook 命令硬编码回 `AGENTS.md`、通用 `.codex/rules/` 或通用 skill 文本。
- 如果其他项目复用本仓库的团队模板，必须在自己的 `AGENTS.override.md` 中重新声明治理命令、base-ref 策略和产品不变量。
