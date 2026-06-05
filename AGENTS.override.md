# claude-agent-runtime 项目覆盖说明

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
- 持久化数据、SQLite schema、`${HOME}/volume-agent-runtime` 默认路径、`docker/volume/` 历史路径和迁移脚本如何处理。
- README、docs、测试和治理硬门如何验证新契约。
- 内部兼容 facade、shim、旧工作流分支、不可达 UI 和一次性调试脚本是否删除；如保留，必须写明明确期限或后续清理条件。

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
- Docker 持久化默认宿主机根目录统一为 `${HOME}/volume-agent-runtime`；`docker/volume/` 只作为迁移来源或显式兼容路径。
- 环境变量放在 `docker/.env`；应用配置放在 `config/*.yaml` 或 `config/*.json`。
- 不得将 API key、MCP header、数据库凭据或本机私有路径写入仓库。

## 本仓库专属边界

- 不要把 `claude-agent-runtime` 的脚本名、CI workflow 或 hook 命令硬编码回 `AGENTS.md`、通用 `.codex/rules/` 或通用 skill 文本。
- 如果其他项目复用本仓库的团队模板，必须在自己的 `AGENTS.override.md` 中重新声明治理命令、base-ref 策略和产品不变量。
