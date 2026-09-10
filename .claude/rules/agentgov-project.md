# agent-gov 项目覆盖说明

本文件只放本项目（智能体治理平台 AgentGov）的专属约束。团队通用行为以 `CLAUDE.md` 和 `.claude/rules/` 为准；复制模板到其他项目时，不要保留本文件的路径、命令、自动化流水线和产品不变量。

## 项目上下文

- 项目名称：智能体治理平台 AgentGov（Agent Runtime · Feedback Loop · Version Governance）。
- 主要目标：通用智能体治理平台，支持创建、运行、反馈优化和版本治理不同业务 Agent，并把运行、反馈、归因、优化、评估、发布过程沉淀为数据资产、方法论资产和执行资产。
- 关键模块：`app/`（FastAPI 治理控制面）、`app/runtime_gateway/`（AgentScope 薄网关与 run 关联）、`agentscope_runtime/`（独立 Runtime 组装）、`app/runtime/`（版本、schema、stores）、`app/services/`（跨 store/runtime/profile 应用服务）、`frontend/`（React/Vite 调试与治理观察界面）、`docker/`、`docs/`、`tests/`。
- 必读文档：`docs/项目目标愿景使命.md`、`docs/engineering/长程重构质量闭环.md`、`docs/engineering/GSD长程重构阶段清单.md`、`.planning/METHODOLOGY.md`。

## 核心架构原则：AgentScope 单运行时

生产数据面只使用固定版本的 AgentScope Runtime；AgentGov 后端负责鉴权、版本绑定、治理编排、反馈闭环和运行关联，不实现第二套 agent loop。开发时按此约束：

- **事实源分层**：AgentScope 持有 Session、Message、AgentState 与会话恢复；Git 持有 Harness、测试与发布版本；AgentGov 持有 `run_id`、版本绑定、反馈/审批/改进和 Trace 引用；Langfuse 持有 OTel 语义轨迹。
- **标识不可混用**：AgentScope `session_id`、AgentGov `run_id`、AgentScope `reply_id`、OTel `trace_id` 生命周期不同，不得互相冒充或复制正文来重新关联。
- **只用公共 API**：Runtime 只调用 AgentScope 公共 API，禁止私有模块导入、核心补丁、运行时 selector、fallback 或双写兼容层。
- **薄数据面**：AgentGov Gateway 只做准入和关联，SSE 保持 AgentScope 原生字节与未知事件透传；AgentGov 不保存会话消息正文。
- **受控 Harness**：权限、MCP、skills、subagents 和 Workspace 来自已发布 AgentScope Harness；活动 Harness 不原地自改，改进必须经候选版本、测试、人工确认和发布。
- 设计任何后端存储、schema 或解析前，先判断 AgentScope、Git 或 Langfuse 是否已经持有该事实；是则只保存稳定引用，不造副本。

## 项目专属质量策略

- 产品不变量：离线模式是产品不变量但始终提供本地化 LLM 模型；必需工作流不得依赖远程服务。
- 开发调试观测面：Playground/调试 UI 可在当前请求内展示业务内容，但这不授权 AgentGov 或 Langfuse 持久化原始 prompt、输出、tool/MCP 参数或 secret；OTel 默认只保留受控语义字段、精确 UTF-8 长度和 SHA-256。该边界同样适用于仓库、提交、公开文档和最终回复。
- 兼容边界：公开 API / OpenAPI / 前端生成类型属契约边界；持久化数据默认宿主机根 `${HOME}/volume-agent-gov`，`docker/volume/` 仅作迁移来源或显式兼容路径。
- 旧设计清理策略：执行“代码质量 > 新设计/框架/架构 > 旧模式兼容或保留”。命中旧 facade、兼容 shim、历史路径、重复实现、schema 双轨、状态分散、过期 API、不可达分支等信号时进入替换旧设计模式，先列删除/迁移/保留清单（按公开 API、配置/env、持久化数据、文档、测试、内部兼容层逐项说明）。

## 反复整改前置矩阵

用户指出“举一反三”“触类旁通”“还有许多问题”“最近反复整改”，或任务同时跨实现、docs、skill、runtime/env、测试、UI 设计一致性、部署生效中的两个及以上配置面时，Analyze 阶段先做短矩阵：

- 治理对象矩阵：业务 Agent、治理 Agent、`main` 样板、runtime data、template workspace、开发者离线工具。
- 配置面矩阵：当前 prompt、唯一根 `AGENTS.md` / `.claude/rules/agentgov-project.md`、Codex/Claude guidance/rules、skill、script、hook、docs、memory。
- 验收路径矩阵：docs/skill 治理、专项测试、主流程测试、真实容器验收、发版完整硬门；不得用 local-debug 结果声明容器验收通过。
- UI 语义矩阵：涉及四阶段改进治理方案或用户可见交互时，确认按钮名称、业务产物、API 副作用、状态推进、抽屉/modal 容器、Trace/反馈/上下文/运行设置/会话管理的信息归属，并验证“不该混入的内容不存在”；决策卡主按钮不得只推进状态，状态推进只能作为业务动作的副作用。

## 项目验证入口

- 局部开发验证：`.venv/bin/python -m pytest -q tests/test_xxx.py::test_xxx`。
- 主流程验证：`make main-flow-test`（改动反馈优化主流程、治理模型任务、formatter、store 投影、API response 或用户可见 tab 状态时必须运行，并确认 `tests/quality_policy.json` 已绑定对应 nodeid 或 UI verification script）。
- 完整验证硬门：`make test`（依赖 `codex-guard`，先运行 Agent 配置审计、Codex/docs 治理、阶段语言、版本一致性和 OpenAPI 契约检查，再跑 `main-full`、coverage 与可信证据校验）。
- 测试资产单一入口：`tests/quality_policy.json`，统一管理覆盖率、双维分类、owner、lane、主流程、TIA、并行晋级、mutation 和 GAP；旧 coverage-only manifest 不得恢复。
- TIA 与 xdist 在至少 20 组同 SHA 配对样本、跨越 14 天且零漏测/并行特有失败前只允许 shadow；提交前始终运行完整串行后端 lane。
- 依赖真实 Docker Compose 运行态的验收只能通过公共 Make 入口；入口必须基于当前工作树重建本地镜像、force-recreate 服务并加载所选配置。纯宿主机测试不触发该流程。

`warn`、dry-run 或只读审计类命令只能用于 Analyze 阶段观察；Verify 阶段必须运行正式 `--mode fail`，不得以 `warn` 作为通过标准。

## 测试与差异治理

- 目标分支差异检查使用目标分支作为 base。
- 主分支本地增量检查使用 `HEAD^`（本地 hook 默认对比 `HEAD`）。
- 旧债处理策略：治理输出 `BASELINE` 表示既有超限未增长、不阻断；`FAIL` 表示新增超限或旧债增长、阻断。旧债清单只放普通 docs 或 issue，不得作为治理放行豁免。

## 版本与 tag 纪律

- VERSION（仓库根）是版本唯一真相源：`app/version.py` 的 `APP_VERSION`、`frontend/package.json` version、docker-compose 镜像 tag 都派生它；`scripts/check_version_consistency.py` 守一致性（"HEAD 若打 v* tag 必须 == v+VERSION" 硬断言 + "VERSION 领先最新 tag" 软告警）。
- 不在每个功能 commit 频繁 bump VERSION：版本号只在「发布点 / 里程碑收口」升一次，避免并行分支与多 Agent 各自 bump 造成版本 churn 与 tag 漂移。
- 升版即打 tag、保持一致：bump VERSION 并发布时用 `make tag`（从 VERSION 打 `v<VERSION>` 并推 origin）创建匹配 tag；不得让 VERSION 长期领先 origin 最新 release tag。
- Agent 行为：Claude Code/Codex 默认不主动 bump VERSION、不主动创建/推送 tag（属人工发布决策）；任务确需改版本先确认是否到发布点；发现 VERSION 已领先最新 tag（一致性门软告警）时提示用户用 `make tag` 补，不擅自补打。

## 提交说明纪律

用户要求“提交”或“提交、推送”时，先按当前 staged/unstaged diff 判断提交复杂度，再生成提交说明。普通小改可只有标题；以下 AgentGov 场景属于非琐碎提交，必须写正文并包含 `Compatibility` 和 `Verification`：

- 修改 SQLite schema、migration、runtime data 布局或 `${HOME}/volume-agent-gov` 兼容策略。
- 修改公开 API、OpenAPI、前端生成类型、用户可见 UI 主流程或容器部署行为。
- 修改反馈优化主流程、治理模型任务、formatter、store 投影、业务 Agent workspace 模板或 runtime/env。
- 删除旧 router/store/schema/test/UI 入口，或一次提交跨后端、前端、docs、tests、配置中的两个及以上配置面。

提交正文只写工程意图、兼容影响和已完成验证；不要复述大段 diff，不写 prompt 流水账，不写真实密钥、本机私有路径或运行态数据。

## 配置与环境边界

- 环境变量文件：私有 `docker/.env`（容器部署）、`docker/.env.local-debug`（宿主机 Python/PyCharm 调试）、`frontend/.env.local`（Vite 本机前端）；可提交示例 `docker/.env.example`。不要把这些文件关系描述成“覆盖”，用“选择 env 文件 / 私有 env 文件 / 本机调试 env 文件”。
- 应用配置文件：约定放 `config/*.yaml` 或 `config/*.json`（当前未创建）。
- Docker/Compose 入口：`docker/`，`docker/docker-compose.yml`。
- 持久化数据路径：容器默认 `${HOME}/volume-agent-gov`，本机调试默认 `/tmp/local-debug-volume-agent-gov`；`docker/volume/` 仅迁移来源。
- 项目源码仓库边界：真实 API key、MCP header、数据库凭据、本机私有路径和运行态数据不得进入 AgentGov 项目源码仓库、公开文档、日志或提交说明。业务 Agent 的 live Workspace 与其 per-Agent Git 是敏感运行资产，可按字节保留 `.env`、真实 endpoint、凭据型 header、数据库配置和本机路径；该例外不延伸到项目源码仓库。live Workspace 回流仓库内置运行卷初始化源前先在仓库外形成候选，再通过 `runtime-bootstrap` 准入扫描。
- 涉及 `RUNTIME_CONTAINER`、`RUNTIME_VOLUME_MODE`、上述 env 文件、Docker volume、Langfuse 或治理模型凭据的改动，先按 `.claude/skills/runtime-env-governance/SKILL.md` 做 Consumer x Mode x Boundary 矩阵。
- `.claude/settings.json` 的 Read deny 只约束内建文件工具；未启用 sandbox 时不能把它当作 Bash/Python 子进程的 OS 级密钥隔离。

## Claude Code 专项

- 必须按需使用的项目 skill：`.claude/skills/runtime-env-governance/SKILL.md`（runtime/env 治理）、`.claude/skills/agentgov-governance-preflight/SKILL.md`（AgentGov 产品/治理方案预检；产品定位、愿景使命、反馈闭环治理、多 Agent 创建治理、prompt/skill/SOP/eval 沉淀类任务先做治理对象建模）、`.claude/skills/docs-governance/SKILL.md`（`docs/` 文档容器治理）、`.claude/skills/test-sync-governance/SKILL.md`（迭代功能时测试增删改判断；删功能同步删测，配合 `scripts/check_orphan_tests.py` 孤儿检测）、`.claude/skills/business-agent-workspace-optimizer/SKILL.md`（开发者离线开发/优化业务 Agent 自身 AgentScope Harness 资产：`AGENT.md`、`agent.yaml`、MCP、skills、subagents、evals、templates）、`.claude/skills/improvement-workbench-contract-preflight/SKILL.md`（四阶段改进治理工作台、反馈闭环 UI、Diff、执行优化、测试用例或 Trace/Langfuse 反复整改前，固定业务产物归属、字段所有权、动作副作用和负向验收）。这些 skill 与 `.codex/skills/` 同名 skill 同源镜像，修改需两侧同步。
- 推荐子代理：`.claude/agents/project-worker.md`。
- 项目 MCP：未配置（无 `.mcp.json`）。
- 项目 Stop hook：`.claude/settings.json` 复用 `.codex/hooks/codex_governance_stop.py`，失败时最多自动续跑一次，避免重入循环。
- Claude Code 开发会话应从仓库根启动，以加载项目 settings/hooks；已删除的 Runtime launcher 不得作为开发入口。
- 本地私有说明：个人偏好放 `CLAUDE.local.md`，模型等本机设置放 `.claude/settings.local.json`，二者已被 git ignore。

## 专属边界

- 不要把本项目的脚本名、自动化流水线、私有路径、端口、产品不变量或临时迁移策略写回通用 `CLAUDE.md`、`.claude/rules/` 或通用 skill。
- 如果其他项目复用本模板，必须重新填写自己的覆盖层、治理命令、base-ref 策略和环境边界。
