# 自用工具整改执行记录

## 已确认范围

- 起始代码：`71a4c7452cbf45d46ed94d824d53ad3dff24d79b`，起始工作树干净。
- 审查报告：`docs/code_review_reports/898c2d5及当前更改第一性原理对抗审查报告.md`。
- 仅 16 条开放 Issue：11、12、15、17、18、24、30、48、51、55、58、72、74、75、76、77；已关闭 Issue 排除。
- 自用工具以功能、真实结果和维护便利为优先，不扩建安全平台、风险评分、多租户或审批机制。
- 用户授权持续实现、真实端到端验收、最终提交推送并按证据关闭相关 Issue。默认不改 VERSION、不创建 tag。
- 禁止 mock 数据、功能、API；所有真实容器验收走公共 Make 入口。
- 私有凭据、运行数据和原始模型/工具内容不进入本记录、源码或提交。

## 冻结的实现选择

1. R-01：实际路径规范化后匹配既有权限，合法 `..` 不一刀切拒绝。
2. R-02：默认只读导出已发布 commit，不推进 HEAD/index/绑定。
3. R-03：远端预检包含现有第三方依赖，无 site-packages 环境验证。
4. R-04～R-06：保留历史 run/Trace；正文和错误独立；找到 run 立即启动现有 monitor。
5. #77：原生三字段 chat、原生 ID 操作身份、保留原生响应；运行期权限仅确认动作表达。
6. #11/#18：通用 FeedbackEvent、entities 和来源工作台；旧在线 SOC 接口一次退出，历史数据保留。
7. #48/#51：删无效参数；Redis 按当前 Compose project/service 获取。
8. #55/#75：宿主机统一实际 URL；首次共享密钥自动初始化并复用有效旧值。
9. #76：候选临时 Workspace 继续使用停服维护 GC；普通 Session Workspace 由在线删除链和 Runtime 启动恢复负责，不共享可写 venv。
10. #58/#72：完整 pytest 结果和有效 shadow 证据，保持 shadow，不新建门禁平台。
11. #15：从已有发布/改进/反馈/Git 记录投影双向来源，不造资产副本。
12. #17/#74：小型 CLI 调现有 test-session 做真实同例对比，私有报告供人工参考；不改 AgentTestRun 或发布流程。

## 执行与验证矩阵

| 工作组 | 范围 | 实现状态 | 验证状态 |
| --- | --- | --- | --- |
| Runtime 规则 | R-01、#48、#51 | 首批实现 | 与初始化/settings 合并目标 162 passed；待完整回归与真实容器 |
| Workspace 导出 | R-02 | 已替换自动提交/维护锁 | 导出目标 12 passed，Git/游标/未发布目标 19 passed；未宣称 live 验收 |
| 远端预检 | R-03 | 工具链冻结、事务恢复和 owner/activity 双锁屏障已实现 | 完整目标 75 passed/1 skipped，loopback 边界 1 passed，真实 sshd 中段 command+rsync 竞争 2 passed；独立 fresh-host 完整 execute/recover 仍保留 #72 缺口 |
| Playground | R-04～R-06 | 已实现历史/错误/恢复 | 原生化后全前端 132 passed、build 通过；真实浏览器待验 |
| 测试完整性 | #58、#72 | 已补完整结果与真实调用要求 | pytest/testkit/maintenance 39 passed，真实 shadow 工件 2 passed；旧发布宿主套件仍待分层整改 |
| 原生接口/来源 | #11、#18、#30、#77 | 通用 entities/event、原生三字段与事务迁移已实现 | 路由/实体/来源 59 passed；精确 v3 迁移/epoch 三组 42 passed，补充后 v4 专项 12 passed；非 SOC 真闭环待验 |
| 环境/资源 | #55、#75、#76 | 手工候选 GC 与普通 Session 在线回收均已实现 | 在线回收组件、故障测试和独立复审已通过；正式 `runtime-workspace-reclaim-live-smoke` 报告生成前保持待 live 验收 |
| 来源/效果增强 | #15、#17、#74 | 只读来源投影与小型同例 CLI 已实现 | 来源/registry 25 passed；CLI 24 passed；真版本关联与同例对比待验 |
| 平台闭环 | #12、#24 | 待验收 | 双 Agent、Git/Harness、发布、新旧 Session |
| 文档 | D-02～D-06 | 合并现有权威文档，不新增平行方案 | 文档专项 14 passed；最终文档契约与总门复验中；历史报告不改写 |
| 已消退发现 | R-07、D-01 | 不重复整改 | 起始树 codex-guard/doc 检查通过 |

## 首批并行所有权

- `commit_scope_runtime`：策略 middleware、managed policy、Langfuse smoke 及对应测试。
- `commit_scope_ui`：Playground 历史/错误/恢复及对应 UI 测试；暂不切换 #77。
- `main_flow_final`：pytest plugin、runner 完整性、shadow 比较/历史及对应测试。
- root：Workspace 包导出、跨组集成；Makefile、quality_policy、文档与生成物统一收口。

所有工作在共享工作树中进行，子任务不提交、不部署、不修改其他组改动。最终完整测试、Compose 和浏览器验收串行。

## 交付门

目标测试 → 主流程 → 串行 make test、类型检查、前端构建、codex-guard → 当前树公共容器验收 → 正式环境验证 → 逐项完成审计 → 提交推送 → 对已证明完成的开放 Issue 留证据并关闭。

代码完成、宿主测试和真实验收分别记录；尚无证据的工作不标完成。

## 第二批集成注意

- #77 已切换公开三字段 chat、原生输入 ID 和原生响应；Runtime store/历史 epoch 迁移联调中。
- v4 当前树的物理 schema 摘要由实际 SQLAlchemy 建表计算，#11 已合并；摘要为 `22eb7ebec969d58def769bac1fba6f33d740934f5e02a910a34f57974568b56d`。精确旧库 fixture 来自起始提交真实建表导出，不用当前 ORM 假造旧库。
- 导出沿用既有活动 Git HEAD 作为发布指针（候选处于独立 worktree），固定 SHA 后只读；draft 不导出，不创建新“当前版本”数据库字段。
- 旧 hook mock 测试组与伪 HTTP 测试已按行为替换：纯投影继续测试，真实断连/恢复/竞争等场景待公共容器门补齐，删测不代表验收通过。
- 首批扩展旧发布套件失败主要是纯 host `assert True` 无真实 Agent invocation 却要求 `release_check` 通过；后续迁为维护/事务的真实契约或明确拒绝，真实发布正向留容器验证，禁止伪造 passed 记录。
- 二次独立审查补齐来源被改归后不能沿旧 run 重新归属的真实 SQLite 负向；相关三组 44 passed。迁移测试现通过公开 store 读取，不只检查裸 SQL。
- 发布宿主套件已完成分层：旧 73 项调整为 68 项，5 项缺乏合法真实调用条件的正向/后置用例迁至真实 lane，不伪造 passed；完整当前树结果以最终 main-full 为准。
- 生成的技术 Harness 和原生表单候选测试补真实 `agent.run()`；基础非空对话不等价于业务质量。修复 `agent_activity.tool_calls` 缺字段：只派生当前 run 工具元数据，未知为 null，不伪装空列表。
- 非 SOC 文档助手在仓库外准备完整包，真实资料从当前项目文档复制。发现 Runtime 不加载包内参考资料，按最小 `references/` 约定补物化和版本摘要，不复制完整敏感 Harness、不新增文件管理 API。当前正式注册表仅 SOC Agent 且无顶层 references，旧默认摘要不受新增根影响。
- `container-technical-live-smoke` 复用既有 runs/concurrency 控制变量，默认 1/1、显式可 50/10；仍要求真实独立场景和完整 Trace。专用 Python/Node native 调用器已退出旧 client operation 协议。
- 集成主流程首轮：1224 passed、9 failed，耗时 1680.69 秒；这是整改中诊断，不是最终门通过。Team/发布验收陈旧契约已同步，两文件 18 passed；真实一万路径 Git 单批读取断言同步后 1 passed。隔离 env 首次共享密钥缺初始化属于实现遗漏，正在复用既有初始化器修复，其余环境断言跟随首次初始化契约更新。
- 二次只读审查确认初始 POST 回执丢失且 run lookup 瞬时失败时，前端尚无 run_id，可能永久锁住发送。恢复循环将保持原生输入 ID 重试查询，只有明确 HTTP 404 才允许原请求幂等重试；真实浏览器故障验证仍待执行。
- 50 条通用无工具输入已从五份真实项目文档/代码准备，独立 AI 逐项复核来源与任务差异通过；记录实际 AI 审查身份，不冒充用户或人类审查。输入集 SHA-256 为 `9f270872de19fcfa61035fb14ff56481ac1f5a3a92f92d92d102e484cb8f1658`。尚未调用真实模型，不代表业务质量、HITL、MCP 或双 Agent 闭环验收。

## 全量回归与验收入口收口

- 首次 `make test` 的前置治理门通过，串行后端 2337 passed、9 failed，2154.19 秒；行覆盖率 75.81%，低于现有 75.85%，因此整体失败。保留门槛，不把 72.00% 的行分支综合值误作行覆盖率。
- 九项失败已分别修正并通过目标测试：原生输入/通用事件/部署 bundle/v4 epoch 的陈旧断言同步；OpenAPI 原生多模态字段缺少注释及示例被治理同名字段污染，现仅补语义文档并按原生 schema 校验示例，不复制或修改 SDK schema。
- 全链复审再发现：刷新初载历史遇瞬态错误时，没有可用历史，detached monitor 尚未启动且无重试。现保持原历史加载门，增加同 Agent/Session 的可取消重试；不可恢复错误可见，恢复只清除自身提示，不清掉其他操作错误。
- 自用双 Agent 入口复用既有导入、四阶段、测试、完整 Diff、一次审批与发布；没有新 API、存储或审批状态。真实来源事件只记录客户端观察到的 run 终态与回答摘要，明确不是 SOC 业务事件。未观察到基线缺口时不得声明改进效果。
- `--projected-trace-id` 为 Langfuse 只读精确查询，不要求创建 Runtime run 的 live 场景授权；会创建真实 run 的 smoke 仍保留完整显式授权。

| 本次行为 | 旧测试处置 | 增补与验收边界 |
| --- | --- | --- |
| 原生输入、通用事件和 v4/bundle 契约 | REFACTOR 陈旧字段与实现字符串断言 | 保留真实维护阻断、错误 reply 拒绝、部署顺序和原生身份 |
| 初始 lookup / 历史加载恢复 | KEEP 原恢复纯测试，复用读错误分类 | 新分类、真实 AbortController 和组件错误投影；断网行为仍需真实浏览器 |
| 只读 Trace 查询与原生 OpenAPI 说明 | KEEP 原生 schema；追加正确字段示例 | 真实 CLI 参数拒绝及原生 schema 验证，不构造 HTTP 替身 |
| 双 Agent 公共验收入口 | REFACTOR 既有脚本为共用 helper | 真实私有文件/子进程收尾测试；完整发布与同输入效果留现场验收 |

当前工具草稿已经进入源码，但没有据此宣称真实环境验收通过；后续完整回归与部署证据继续单列。

### 接入复审与正式数据副本演练

- 现场只读核验仍为 16 条开放 Issue，未扩大范围。既有部署 9 服务正常，映射仍为 50400～50404；此时尚未部署本轮工作树，版本号 4.0.1 不作为源码一致证据。
- 前端主流程公共入口通过：24 个文件、201 项测试。AGV-046 的文档措辞检查迁至独立文档契约测试，按既有工程治理分类；实际非 SOC 闭环另行验收。
- 正式数据库的一致副本已用当前 `make_session_factory` 完成精确 v3→v4 迁移；24 个 run、24 条聊天操作、1 条发布和 1 个 Agent 的未变列字节及身份保持，旧业务引用正确迁入 entities，外键和完整性检查通过，并产生 1 份迁移前备份。仅修改仓库外副本，未改 live 数据库。
- 首次副本演练的验证器未传已知数据迁移 marker，且误将预期变更的实体列纳入全列字节比较，导致校验失败；修正验证器后重新从正式库取一致副本验证通过，不将该次误判写成生产迁移缺陷或删去失败记录。
- 接入复审发现首次 CLI 空版本参数误拒、失败阶段/实际候选 ID 丢失及新增 subagent 模板需 Runtime 重启的衔接缺口。按实际问题修复固定参数解析、安全诊断与既有公开维护入口复用，不新增审批门或权限平台。
- 后续复审发现 pytest teardown 会删除 `awaiting_restart` 模板源，使重启仍无法注册模板；现按原状态保留至既有 TTL 回收。通过真实 Git、SQLite、AgentScope HTTP 与实际计时的目标测试证明模板仍可发现、到期后精确清理，未依赖模型/API 替身；该组件证据不替代正式发布。
- 结构化重启码贯通 Gateway→测试服务→现有 testRun.error 投影→HTTP/UI；仅服务端当前测试、精确 commit 的异常能写入，普通 409、pytest 自报错误和取消不触发重启。目标两批 41 与 27 项通过（有重叠，不相加宣称唯一总数）。
- 新浏览器恢复、候选维护和双 Agent 工具确认入口已完成无替身扫描及目标测试，仍未在本轮部署执行。所有组冻结，进入第二次整树串行回归；不因脚本已经完成就关闭 Issue。

### 第二次完整回归结果与单项测试同步

- 第二次 `make test`：2479 passed、1 failed、4 warnings，2168.27 秒；唯一失败是 `test_policy_keeps_deployed_smoke_outside_isolated_runner_allowlist` 仍将正式部署入口集合写死为旧的单入口。治理前置检查均通过，失败结果未用目标测试拼接成全量通过。
- 真实行覆盖率为 28617 / 37658，即 75.9918%，已高于原 75.85% 门槛；未修改覆盖率门槛。报告表格的 72.22% 为行分支综合值，不与行覆盖率混淆。
- 首次和第二次失败的 JUnit、coverage 与 evidence 工件均已在仓库外私有目录保留，不由第三次回归覆盖。首次 JUnit 摘要 `50e0413427adaa191105a73c4c7613511aee95507e3368733a8b21dbbd85c8e4`。
- 仅同步该测试：明确三个既有正式入口，保持它们与隔离 runner 清单互斥，并逐一删除入口验证缺失仍被拒绝；未放宽 scanner 或新增安全范围。接着执行目标文件、主流程及整树串行回归。
- `make typecheck` 与前端 `pnpm run build` 通过；`make ui-build` 通过，UI 镜像源码摘要与工作树同为 `45cd2cf19881f39e2d88febe28934445f29eaecdabf9abe822cbdbe1a018c287`。此时仅构建镜像，正式运行服务尚未替换，不能作为 50401 最新代码验收。
- 真实双 Agent 输入只读预检与 50 条技术场景选择校验通过；Playwright、Chromium 和 Firefox 依赖已确认安装。上述准备未执行模型或正式浏览器旅程，真实验收仍待后续公共入口。

### 主流程通过与取消验收窗口修正

- 第二次 `make main-flow-test` 完整通过：后端 1404 passed、4 warnings，1750.79 秒；前端 24 个文件、201 项通过。该结果发生在下述取消脚本补丁应用前，不声称包含新补丁。
- 取消验收复审发现 early 已受理事件污染后续 partial/retry 的累计计数、停止按钮点击未证明首文本前窗口，以及空输入框与取消文案断言陈旧。补丁仅调整验收脚本、纯元数据断言和现有 Python→Node 测试桥接；不改业务 API、数据库或 Docker 卷。
- early 通过真实 DOM 观察和实际停止点击记录时序，不改页面内容或网络响应；没有命中目标窗口就失败。后续按完整网络事件的数组基线切片，不按 run 过滤而遗漏重复请求；原始事件继续保留供归属清理。
- 保持 Chromium、Firefox 各三轮。取消后同 Session 新消息只证明可继续对话，不冒充同输入幂等重放。独立 AI 复核的三条技术输入尚未调用模型，不能替代正式 50401 或业务质量验收。
- 新桥接沿用现有 `tests/quality_policy.json` 的主流程绑定；完成目标验证后继续第三次串行 `make test`，不下调覆盖率或放宽无替身检查。

### API 镜像依赖遗漏与第三次回归中止

- 只读镜像链复审确认新增 `native_chat_input.py` 顶层引用 AgentScope 公共模型，但 API 镜像只安装的 API requirements/testkit 均未声明 AgentScope。宿主机完整开发环境安装了 Runtime 依赖，掩盖干净 API 镜像的启动缺包；属于本轮项目实现遗漏，不是外部 MCP 问题。
- 第三次 `make test` 的治理前置全部通过，收集 2481 项；发现上述遗漏后主动 SIGINT 当前 pytest，305 passed、1 warning、469.02 秒，整体退出 2。中断造成不完整 JUnit，不能算完整通过；工件已在仓库外保留。
- 最小修复在 API requirements 固定 `agentscope==2.0.8`，不加 service/storage/full extras，不复制 schema 或加入第二套 agent loop。Dockerfile 新增无模型调用的真实请求模型导入/schema 构建检查；主流程已有测试文件补依赖与构建检查契约，README/实施基线同步。
- 此改动仅影响 API 镜像依赖与构建验证，无新增路由、状态、数据迁移或 Docker 卷变化；不修改私有 env，不向 API 注入 Provider 凭据。后续先通过公共构建验证真实依赖，再以修正后的整树重跑完整测试与公共容器验收。
- API 依赖目标文件 10 项通过，Ruff/format 通过。公共 `make build` 完整退出 0：三个镜像均构建成功，API 镜像中的真实 `RuntimeChatRequest` 导入与 schema 生成检查通过；源码标签一致为 `5f516eaabcb58311eb4e3ae28834c9855caaa40693fb1fa58339c97b6e81cf95`。此时仅构建，没有替换正式运行容器，也没有执行模型验收。
- API 镜像 ID 为 `sha256:761a5b362150db1fa599dd133d5041708522f84b40a105aefd72461e18e751dc`，Runtime 为 `sha256:b4ec7708aabe12b40a01ea766365163d739b5f60d9cc2869bb780734ad723f01`，UI 为 `sha256:4e304b0853d91f150f267b5e903d564ae06d02995ec5959cfdfd0d7c9dbf296f`；后续正式部署仍重新核对最终容器身份。

### 已完成发布与新测试门的阶段边界

- 第四次 `make test` 前置治理全部通过、收集 2482 项；兼容复审确认新的完整报告门会在启动复核未隔离的历史 published 时被回溯应用，且公开投影丢失原测试引用，故主动中止。已有 917 passed、3 warnings，669.05 秒，整体退出 2；中断工件已在仓库外保留，不能计作完整通过。
- 真实迁移副本中的旧 SOC 报告含两次匹配 invocation，但缺当时未记录的 `collected_nodeids` 与 `items[].phase_outcomes`。原 testRun、批准、intent、release 的 commit/diff/suite/身份相符，不能通过补写报告伪造新版证据。
- 必须区分因果：该 SOC 记录在迁移前后均已有发布异常标记，时间为 `2026-09-13T09:00:43Z`，原因为 Git diff 无法核验；不是本轮报告新门导致。本轮不自动清除此标记，也没有证据它直接阻断 Runtime current/Playground。
- 修复按业务阶段区分：新候选/approved/publishing 保持完整 pytest 与真实 invocation 门；已完成 published 只在原批准、测试、release 和真实 Git/tag 一致时保留历史事实及 UI 精确引用。无新增 legacy 开关、双 schema 或持久化状态，不修改旧报告。
- 新专项测试进入既有 Harness 生命周期主流程绑定；测试区分真实历史记录的读取契约与新候选发布验收，不以宿主记录投影声称真实 Agent 新运行通过。目标验证后先通过公共隔离容器入口确认当前树启动，再完成整体验证和正式端到端验收。
- 本轮只读 GitHub 刷新仍为原 16 条开放 Issue，远端 master 仍为起始提交。#15/#17 等明确后续增强不扩建；#58 的 20 组/14 天仅适用于 shadow 晋级，保持 shadow 不要求等待该时间；#74 不要求持续评测平台，但仍需要真实同例对照与质量复核才能关闭。
- 历史边界、完整报告和发布 intent 目标共 83 passed，Ruff/format、Pyright、治理和无替身检查通过。真实迁移副本以 SQLite `mode=ro` 与 `query_only` 核验：原发布测试可读，同一报告仍被新候选完整门拒绝；报告与相关记录字节保持，未清除原异常标记。原报告 SHA-256 为 `c8705edb24ea355315c4a8b4c3724153456d6729c3974aabec6379ea3c96c295`。该副本核验只覆盖测试记录 helper，不代表完整 Git、启动或正式发布验收。
- 旧发布文件回归为 67 passed、1 failed，669.79 秒；唯一失败是提前 Git 身份校验合并了既有明确错误提示。修正 release/Git 诊断分支并保留 Git 读取异常转 409 的原契约后，新专项和原失败节点 30 passed，68.60 秒；静态检查通过，旧行为测试未改。该目标复验不拼接宣称完整套件通过。文档部署命令同步为公共 Make 支持的 `COMPOSE_UP_FLAGS=--force-recreate`。

### 隔离容器启动前诊断

- 两次公共 `make container-core-smoke` 均成功构建三个当前树镜像，但在 Compose 启动前因验收输入目录变更退出 2，未运行健康、页面或 OpenAPI 验收。两次 run ID 为 `1789340862-5c18003fc804` 与 `1789341620-47d52d1084a0`；正式 50401 的原服务未被替换。
- 第二次在同一公共入口外使用现有 `strace`，日志仅保存在仓库外。真实系统调用确认 BuildKit 在受监视的 `execution-toolchain/docker-config` 创建 `.token_seed.lock` 并写入 `.token_seed`；这是验收工具配置遗漏，不是业务 Agent 或 MCP 故障。无原始业务内容或凭据进入本记录。
- 正式 selected-env 部署工具已处理该缓存行为，隔离工具物化只锁定了插件文件而未锁定其目录。最小修复只对两个现有插件目录设只读，保持已捕获插件文件 identity 不变；不放宽输入检查、不扩建安全机制，不改私有 env、业务运行卷或审批流程。后续仍以修复后的公共容器入口和最终整树测试为准。
- 隔离工具修复目标完整通过：36 passed，80.06 秒；真实系统插件复制、文件 identity 保持、缓存写入拒绝、真实 Compose version 和正常只读不误触检查均通过，实际目录被修改仍拒绝。Ruff/format、Pyright、无替身和差异检查通过。该证据不替代修复后的 BuildKit 构建及容器启动验收。
- 第三次公共 core（`1789342362-8d02789fa63f`）已通过原 BuildKit 误触点，完成发布 Harness 准备并启动三个健康容器；随后验收工具把 `ContainerIdentity` TypedDict 按对象属性读取，抛 `AttributeError`，整体退出 2。入口已精确清理本次隔离容器，正式环境未改；不能据健康状态声明 core 整体通过。
- 后段复审还发现封存回执的目录模式与消费端预期不符，以及完整 daemon 文件系统探测被放进无变更监视窗口、会把自身探测容器当作变更。整改仅收口既有表示与阶段顺序；该模块单独启用属性/索引类型诊断，避免项目当前宽松 Pyright 配置继续漏掉这类明确契约错误。不新增安全机制或探测豁免。
- 只读采集本机过去一分钟 Docker 事件还确认，正常健康检查会持续产生 `exec_create/exec_start/exec_die`。现有监视将所有 container 事件都视为变更，必然误报；现明确普通 `exec` 事件不等于容器生命周期或镜像身份变更，保留原有创建、重启、删除、网络、卷及完整 inventory 检查。没有具体命令豁免或新命令审查机制，真实 Langfuse Redis 查询也可按其现有公开路径执行。
- 回执目录与探测顺序两文件组目标 69 passed，123.84 秒，静态检查通过；普通 exec 事件分类另补目标，不拼接成最终 core 通过。对旧 TypedDict 点号访问的仓库外真实 Pyright 对照成功报错，正确键访问通过，说明新模块规则确实能捕获本次缺陷，而不是沿用全局关闭该诊断的“0 错误”。
- core 后段还发现 OpenAPI 导出重新构造子环境时丢弃已物化 Python 必需的 `PYTHONHOME` 和依赖路径，宿主 `.venv` 测试未覆盖该布局。修复仅传递既有已核验的 Python 运行环境，不整包继承业务 env、不切回宿主解释器；完整效果仍由后续公共 core 入口确认。
- 普通 exec 分类与原生命周期负向定向 13 passed（与前述 69 项有重叠），独立复审确认探测、监视、回执读取及异常清理顺序。监视结论只覆盖 Docker 拓扑、镜像与挂载身份稳定，不宣称能够发现任意 `docker exec` 容器内文件写入；本轮不扩此类自用工具之外的对抗能力。
- OpenAPI 子环境修复目标完整 23 passed，214.57 秒：真实复制并核验工具链、证明冻结解释器实际加载其 encodings/FastAPI、同一解释器导出 app schema；污染部署环境未传入，原运行目录 sentinel 字节与 mtime 未变。Ruff/format、Pyright、差异检查通过；没有 Docker、模型或 API 替身。至此已确认的工具问题冻结，统一治理与公共 core 待复验。
- 上述修复冻结后的 `make codex-guard` 完整通过：无替身 0、治理、stage language、版本一致、AgentScope 原生契约、OpenAPI 及生成类型、docs 与测试资产清单均通过（2521 叶，清单检查不代表执行通过）。VERSION 仍为 4.0.1，未创建 tag；开始第四次公共 core 重验。
- 第四次公共 `make container-core-smoke` 完整退出 0，run ID `1789344151-b01b7f69fe83`：从冻结工作树构建并启动隔离镜像，API/Runtime 就绪、真实前端根页面与 123 项 OpenAPI 契约通过，执行后输入检查及精确清理完成。隔离页面为 50406，正式 50401 未替换；该 core 结果不等于模型、业务质量或正式浏览器验收。接下来以此修复后的整树重新运行主流程和串行完整回归。
- 当时第四次冻结快照的 `make main-flow-test` 完整退出 0：后端 1435 passed、4 warnings，1756.20 秒；前端 24 个文件、201 项通过。该快照冻结期间 `make typecheck` 及前端生产构建也完整通过；Vite 大包体提示仅记录，不扩本轮重构范围。上述历史宿主结果不作为当前工作树、正式 50401 或模型验收。
- 当时第四次冻结快照的串行 `make test` 完整退出 0：2521 passed、4 warnings，pytest 1671.14 秒，main-full lane 1681.66 秒；前置治理、完整证据校验和末尾文档收集均通过。行覆盖率 28722 / 37758，即 76.0686%，高于原 75.85% 门槛；表格 72.3192% 为行分支综合指标。完整工件已另存仓库外；该历史快照源码摘要为 `01391d4ee3a64540d0556b01c0b35dd33dddf96f5ed87c99afd0588f35cb0590`。后续补丁已使该结果失效，不能拼接为当前整树通过；未据宿主通过关闭 Issue。

### Langfuse 真实入口的健康命令误判

- 首次公共 50/10 `container-technical-live-smoke`，run ID `1789348589-98e0d23b2fe2`，已构建当前源码的三个镜像，九个隔离服务均健康；但在模型子命令前因 PostgreSQL 完整配置不匹配退出 2，精确清理隔离栈。未执行模型验收、未替换正式 50401，不把健康状态算作技术验收通过。
- 只读对比真实 Compose 与 PostgreSQL 容器：健康命令原字符串不等，Compose 含两个 `$$`，实际容器为 `$`，按 Compose 语义转换一次后完全相等。对正式六个 Langfuse 服务的分层诊断中，基础配置比较均无差异；补充比较只有 PostgreSQL、Redis 误报，其余四服务通过。诊断仅输出字段名与布尔值，未打印配置有效值或修改服务。
- 根因是补充 healthcheck 比较器遗漏基础比较器已有的 `$$`→`$` 转换。最小修复仅规范化 Compose 显式健康命令，保持 image 自带字符串不变，并补真实差异仍拒绝的纯契约测试。该补丁晚于上述 2521 项通过快照；待真实验收工具稳定后统一重跑完整门，不拼接旧全量结果宣称新树通过。
- 转义补丁目标完整 47 passed，53.69 秒；Ruff/format、Pyright、无替身检查和独立复审通过。修复后对同六个真实 Langfuse 服务的只读基础与补充比较全部通过，无服务或配置变动。该现场诊断不冒充隔离入口通过，继续重跑公共技术验收；新增四个测试叶进入既有测试资产。
- 补丁后 `make codex-guard` 完整通过，资产清单为 2525 叶（未视作执行结果）。再次冻结源码并复跑同一公共 50/10 真实技术入口。
- 第二次公共 50/10 run ID `1789349570-5b3caa3ba7f8` 已通过九服务健康与 `CONTAINER_ACCEPTANCE_CONTEXT_OK`，候选 Workspace 真导入后在精确 commit 平台测试门退出 2，50 条主运行未开始。原提示把测试未通过、未完成、身份或 suite 不符合并表达，不能据此断言是 commit 错误。子命令清理亦报次级 `TechnicalIntegrationSeedError`，外层公共 runner 已精确移除九个隔离容器和临时目录；原候选测试报告随隔离目录清理，故当前没有可信的具体测试失败阶段证据。
- 下一步在既有验收工具只输出有限、无正文的测试状态/阶段/身份匹配元数据及清理阶段，再从原 50 场景文件选 1 条作公共最小复现。该诊断不跳过候选真测试、不伪造通过、不修改正式数据。只有确认并修正实因后才重新跑 50/10。
- 诊断补丁仅动技术 seed 与原测试文件，原通过条件保持；身份仅输出匹配布尔、状态/exit、已知节点及固定失败类别，原始报告和输出不回显。清理次级错误现在标 `abandon` 或 `delete`，仍保留主异常。目标 15 passed、Ruff/format、Pyright、无替身检查和独立复审通过；该结果不代表真实 Agent 候选测试通过。
- 第三次公共 1/1（run ID `1789350779-fe8e961d2a74`）完成九服务健康及上下文门后，真实候选测试在 `call` 失败：身份和 suite 匹配，pytest 报 `HTTPStatusError`，状态与 API 错误码尚未被旧 testkit 安全投影；外层隔离清理完成，正式服务未改。未据此归因业务 Agent 或模型答案。
- testkit 仅将非 2xx 的 HTTP 状态与严格大写 `error_code` 带入私有测试异常，技术 seed 只投影这两个字段，不输出响应正文、URL 或凭据；候选测试通过条件不变。两目标文件 25 passed、Ruff/format/Pyright、`make codex-guard` 完整通过（2541 叶为资产清单，不是执行数）。
- 第四次公共 1/1（run ID `1789351665-efdfa454873d`）的真实候选调用返回 HTTP 503、`RUNTIME_UPSTREAM_ERROR`；隔离 API 日志同轮记录约 250 次 `/internal/runtime-boots` 409。代码核对确认隔离配置把镜像标签设为 `APP_VERSION=acceptance-…`，Compose 又用此标签作为 `AGENTGOV_RUNTIME_VERSION`，与 API 从根 `VERSION=4.0.1` 读取的协议版本不符；Runtime boot 始终不能确认，chat 返回 503。隔离栈已精确清理，正式 50401 未改。修复将隔离镜像标签与 Runtime 协议版本分离，后续再以同一公共入口重验，不把日志推断或目标测试算成 live pass。
- 版本修复将隔离镜像 tag 保持 `acceptance-…`，Runtime 握手版本从冻结根 `VERSION` 注入；正式 selected-env 也显式注入根版本，Compose 保留普通 `APP_VERSION` 回退。真实 Compose 展开同时验证了隔离与普通部署值；隔离目标与相邻测试 73 passed，最终小结构调整后 startup 35 passed、Pyright 0、完整 `make codex-guard` 退出 0。原项目 80 行函数/弱字典返回门禁触发后已改为精确版本 TypedDict，而非豁免治理。
- 第五次公共 1/1（run ID `1789353378-f71a8c9a33d1`）以当前源码构建并启动九个隔离服务，现场 `/internal/runtime-boots` 从前次的持续 409 变为一次 200，候选平台测试的真实消息 POST 为 200；模型调用、原生 SSE、完整 Langfuse Trace 与本轮精确清理最终由公共入口 `CONTAINER_ACCEPTANCE_OK`、exit 0 确认。此项仅证明单条通用技术场景，不代表 50/10、业务质量或正式 50401 已验收。
- 随后的公共 50/10（run ID `1789354145-4d0e6b965441`）在运行中因 `Server disconnected without sending a response` 退出 2。现场已观察 48 次 chat 200；API 与 Runtime 均未退出、未 OOM、无重启，候选测试和发布已成功，故不能归因于此前版本握手错误。原异常未保留请求类别，具体连接边界尚待确认；隔离清理已完成，正式环境未改。仅补 HTTP 异常的类型、方法、固定请求类别与状态投影，保持原失败条件，继续真实复验。
- HTTP 诊断补丁后的目标 43 passed，Ruff/format、Pyright 和 `make codex-guard` 通过（2547 叶为资产清单）。下一轮 50/10 的进程句柄在续接时已不存在，隔离容器和目录亦已清理；没有保留下来的最终回执，故本轮结果记为未核实，不计通过。后续运行将标准输出保存在仓库外私有目录。

### 正式部署与双 Agent 浏览器验收

- 首次正式双 Agent 公共入口在 build 前失败，未替换服务。直接复验及真实系统调用表明第一个 `inotify_add_watch` 即返回 `ENOSPC`；主机 `max_user_watches=65536` 的配额主要被桌面进程占用。这是主机资源限制，未据此修改产品或关闭 Issue。已将当前开机期间上限临时调为 131072，未终止其他进程、未修改持久 sysctl 配置；原值记录为 65536。
- 第二次正式入口完整完成公共 `build` 与 `all-up --force-recreate`，九个服务均已重建；三个应用运行容器源码摘要与当前树同为 `1b86662cc04819412ef5f06ee5df9ef8f4bd5e624feea374b52aef246bcde00b`。正式 UI 50401 与 API readiness 均返回 200，Runtime boot 请求一次；正式环境已更新，不再仅是隔离环境结果。
- 该轮整体仍失败：真实浏览器导入文档助手包返回 422，阶段 `bootstrap_workspace_import`，尚未生成候选/发布回执，退出时无活动工作。回执 `formal-dual-governance-02.json` 保存在仓库外，不能据部署成功声明双 Agent 验收完成。
- 只读检查与真实包解析确认自备验收包错误地使用 `./` 根目录，当前接口要求 `workspace/`；原包触发 `WORKSPACE_PACKAGE_PATH_INVALID`。保留原包与失败回执，重新包装为 v2，七项文件及清单身份校验通过，树摘要为 `d15b1fdbc69c35c5ab85cb6ad5c5f8c01e04fccfcd4de671c4b14a51dd4b8bca`。只改仓库外验收输入，不放宽导入规则；继续公共入口真实复验。
