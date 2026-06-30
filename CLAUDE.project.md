# agent-gov 项目覆盖说明

本文件只放本项目（智能体治理平台 AgentGov）的专属约束。团队通用行为以 `CLAUDE.md` 和 `.claude/rules/` 为准；复制模板到其他项目时，不要保留本文件的路径、命令、CI 和产品不变量。

## 项目上下文

- 项目名称：智能体治理平台 AgentGov（Agent Runtime · Feedback Loop · Version Governance）。
- 主要目标：通用智能体治理平台，支持创建、运行、反馈优化和版本治理不同业务 Agent，并把运行、反馈、归因、优化、评估、发布过程沉淀为数据资产、方法论资产和执行资产。
- 关键模块：`app/`（FastAPI Runtime 控制面）、`app/runtime/`（Claude SDK 适配、profile、版本、schema、stores）、`app/services/`（跨 store/runtime/profile 应用服务）、`frontend/`（React/Vite 调试与治理观察界面）、`docker/`、`docs/`、`tests/`。
- 必读文档：`docs/项目目标愿景使命.md`、`docs/engineering/长程重构质量闭环.md`、`docs/engineering/GSD长程重构阶段清单.md`、`.planning/METHODOLOGY.md`。

## 核心架构原则：以 claude-agent-sdk / Claude Code 为中心

本项目的根基是 claude-agent-sdk 及其捆绑的 Claude Code agent；后端只是在其外面包了一层交互接口与反馈优化闭环。一切都应围绕 Claude Code agent——它既是交互的中心，也是被治理 / 被优化的对象。开发时按此约束：

- **单一真相源是 agent**：会话、消息、trace、session 元数据等的权威家是 SDK / agent（如 SDK session transcript、SDK session API），后端不另建并行存储或副本造成双轨 / 同步漂移。
- **优先复用 SDK 原生能力**：需要读取或管理会话、消息、子 Agent、session 元数据时，先用 claude-agent-sdk 暴露的能力（如 `get_session_messages` / `get_session_info` / `list_sessions` / `get_subagent_messages` / `SessionStore`），不手解析 CLI 内部 transcript 格式、不在后端重新实现一份。
- **后端是薄投影 / 编排层**：把 agent 的事实投影成 API 契约、反馈闭环证据与治理视图；交互与优化共享同一份「agent 行为事实」，不各存各的。
- **不重写、不绕过 agent loop**：不重写 Claude Agent loop；工具权限、MCP、hooks、skills、subagents 以 Claude Code 官方配置与原生发现为准，后端不通过 Options 接管。
- 设计任何「后端自建存储 / schema / 解析」前先自问：这是不是 SDK / agent 已持有或已暴露能力的东西？是则用 agent 的，不造副本。

## 项目专属质量策略

- 产品不变量：离线模式是产品不变量但始终提供本地化 LLM 模型；必需工作流不得依赖远程服务。真实 API key、MCP header、数据库凭据、本机私有路径和运行态数据不得提交。
- 兼容边界：公开 API / OpenAPI / 前端生成类型属契约边界；持久化数据默认宿主机根 `${HOME}/volume-agent-gov`，`docker/volume/` 仅作迁移来源或显式兼容路径。
- 旧设计清理策略：执行“代码质量 > 新设计/框架/架构 > 旧模式兼容或保留”。命中旧 facade、兼容 shim、历史路径、重复实现、schema 双轨、状态分散、过期 API、不可达分支等信号时进入替换旧设计模式，先列删除/迁移/保留清单（按公开 API、配置/env、持久化数据、文档、测试、内部兼容层逐项说明）。

## 反复整改前置矩阵

用户指出“举一反三”“触类旁通”“还有许多问题”“最近反复整改”，或任务同时跨实现、docs、skill、runtime/env、测试、UI 设计一致性、部署生效中的两个及以上配置面时，Analyze 阶段先做短矩阵：

- 治理对象矩阵：业务 Agent、治理 Agent、`main` 样板、runtime data、template workspace、开发者离线工具。
- 配置面矩阵：当前 prompt、`AGENTS.override.md` / `CLAUDE.project.md`、Codex/Claude rules、skill、script、hook、docs、memory。
- 验收路径矩阵：docs/skill 治理、专项测试、主流程测试、真实容器验收、发版完整硬门；不得用 local-debug 结果声明容器验收通过。
- UI 语义矩阵：涉及四阶段改进治理方案或用户可见交互时，确认按钮名称、业务产物、API 副作用、状态推进、抽屉/modal 容器、Trace/反馈/上下文/运行设置/会话管理的信息归属，并验证“不该混入的内容不存在”；决策卡主按钮不得只推进状态，状态推进只能作为业务动作的副作用。

## 项目验证入口

- 局部开发验证：`.venv/bin/python -m pytest -q tests/test_xxx.py::test_xxx`。
- 主流程验证：`make main-flow-test`（改动反馈优化主流程、Agent job、formatter、store 投影、API response 或用户可见 tab 状态时必须运行，并确认 `tests/coverage_policy.json` 已绑定对应 nodeid 或 UI verification script）。
- 完整验证硬门：`make test`（依赖 `codex-guard`，先运行 Codex 治理、阶段语言和版本一致性检查，再跑全量 pytest、coverage JSON 和 coverage policy 硬门）。
- 覆盖清单或测试 manifest：`tests/coverage_policy.json`（主流程覆盖清单与全局覆盖率基线的单一入口）。

`warn`、dry-run 或只读审计类命令只能用于 Analyze 阶段观察；Verify 阶段必须运行正式 `--mode fail`，不得以 `warn` 作为通过标准。

## CI 与差异治理

- PR 对比基线：目标分支。
- 主分支 push 对比基线：`HEAD^`（本地 hook 默认对比 `HEAD`）。
- 旧债处理策略：治理输出 `BASELINE` 表示既有超限未增长、不阻断；`FAIL` 表示新增超限或旧债增长、阻断。旧债清单只放普通 docs 或 issue，不得作为治理放行豁免。
- CI workflow：`.github/workflows/governance.yml`（PR 及 `main`/`master` push 上运行治理硬门与测试）。

## 版本与 tag 纪律

- VERSION（仓库根）是版本唯一真相源：`app/version.py` 的 `APP_VERSION`、`frontend/package.json` version、docker-compose 镜像 tag 都派生它；`scripts/check_version_consistency.py` 守一致性（"HEAD 若打 v* tag 必须 == v+VERSION" 硬断言 + "VERSION 领先最新 tag" 软告警）。
- 不在每个功能 commit 频繁 bump VERSION：版本号只在「发布点 / 里程碑收口」升一次，避免并行分支与多 Agent 各自 bump 造成版本 churn 与 tag 漂移。
- 升版即打 tag、保持一致：bump VERSION 并发布时用 `make tag`（从 VERSION 打 `v<VERSION>` 并推 origin）创建匹配 tag；不得让 VERSION 长期领先 origin 最新 release tag。
- Agent 行为：Claude/Codex 默认不主动 bump VERSION、不主动创建/推送 tag（属人工发布决策）；任务确需改版本先确认是否到发布点；发现 VERSION 已领先最新 tag（一致性门软告警）时提示用户用 `make tag` 补，不擅自补打。

## 配置与环境边界

- 环境变量文件：私有 `docker/.env`（容器部署）、`docker/.env.local-debug`（宿主机 Python/PyCharm 调试）、`frontend/.env.local`（Vite 本机前端）；可提交示例 `docker/.env.example`。不要把这些文件关系描述成“覆盖”，用“选择 env 文件 / 私有 env 文件 / 本机调试 env 文件”。
- 应用配置文件：约定放 `config/*.yaml` 或 `config/*.json`（当前未创建）。
- Docker/Compose 入口：`docker/`，`docker/docker-compose.yml`。
- 持久化数据路径：容器默认 `${HOME}/volume-agent-gov`，本机调试默认 `/tmp/local-debug-volume-agent-gov`；`docker/volume/` 仅迁移来源。
- 密钥边界：真实 API key、MCP header、数据库凭据、本机私有路径和运行态数据不得提交。涉及 `RUNTIME_CONTAINER`、`RUNTIME_VOLUME_MODE`、上述 env 文件、Docker volume、Langfuse 或后台 Agent job 模型凭据的改动，先按 `.claude/skills/runtime-env-governance/SKILL.md` 做 Consumer x Mode x Boundary 矩阵。

## Claude Code 专项

- 必须按需使用的项目 skill：`.claude/skills/runtime-env-governance/SKILL.md`（runtime/env 治理）、`.claude/skills/agentgov-governance-preflight/SKILL.md`（AgentGov 产品/治理方案预检；产品定位、愿景使命、反馈闭环治理、多 Agent 创建治理、prompt/skill/SOP/eval 沉淀类任务先做治理对象建模）、`.claude/skills/docs-governance/SKILL.md`（`docs/` 文档容器治理）、`.claude/skills/test-sync-governance/SKILL.md`（迭代功能时测试增删改判断；删功能同步删测，配合 `scripts/check_orphan_tests.py` 孤儿检测）、`.claude/skills/business-agent-workspace-optimizer/SKILL.md`（开发者离线开发/优化业务 Agent 自身 workspace 配置资产：CLAUDE.md/MCP/settings/skills/agents/rules/hooks/evals/templates）。这些 skill 与 `.codex/skills/` 同名 skill 同源镜像，修改需两侧同步。
- 推荐子代理：`.claude/agents/project-worker.md`。
- 项目 MCP：未配置（无 `.mcp.json`）。
- 本地私有说明：个人偏好放 `CLAUDE.local.md`，模型等本机设置放 `.claude/settings.local.json`，二者已被 git ignore。

## 专属边界

- 不要把本项目的脚本名、CI workflow、私有路径、端口、产品不变量或临时迁移策略写回通用 `CLAUDE.md`、`.claude/rules/` 或通用 skill。
- 如果其他项目复用本模板，必须重新填写自己的覆盖层、治理命令、base-ref 策略和环境边界。
