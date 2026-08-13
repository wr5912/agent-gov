# 业务 Agent Workspace 原生 pytest 测试资产实现方案

> 文档状态：当前实现基线与后续演进的权威工程契约。
>
> 适用对象：所有注册业务 Agent（含 `main-agent`）。治理 Agent 不进入业务 Agent
> 注册表、发布链或业务测试会话；其项目自测仍可使用同样的 `tests/` 目录约定。

## 1. 裁决与产品边界

业务 Agent 的测试必须与其 Runtime 原生 Workspace 一起开发、评审、版本化、导入、
导出和发布。`workspace/tests/` 是 Agent 自有可执行测试正文的唯一真相源，平台不另建
数据库测试集、全局用例池或通用 Registry 正文副本。

当前 Phase 7 的 `p0-exact-commit` lane 是工程回归门：它固定精确 Git commit、静态
Workspace 源码和 pytest 执行环境，验证已知契约与发布工程卫生。它不是业务 Agent
能力测评、评测基准或线上效果证明，也不产生可与其他 Agent 直接比较的能力总分。

| 层次 | 当前对象 | 作用 | 权威所有者 |
| --- | --- | --- | --- |
| Agent 自有回归 | `WorkspaceTestFile` + `AgentTestRun` | 单元/契约测试、已知问题回归、精确 commit 工程门 | 测试正文归 Workspace Git，执行事实归平台 |
| 评测基准 | `EvaluationBenchmark` + `EvaluationProtocolRevision` | 冻结能力维度、case/corpus、Ground Truth、scorer、安全门和环境约束 | 独立评测方或领域资产库，被测候选无权改写 |
| 平台发布评测 | `EvaluationExecution` / `Assessment` / comparison / safety gate | 在同协议下比较修复前与待发布版本，形成发布裁决证据 | 评测方与平台后端，不回写 Workspace 测试事实 |
| 线上效果 | `OnlineOutcome` | 观察发布后业务结果和回滚阈值 | 外部业务系统是事实源，AgentGov 保存受控引用或投影 |

`AgentTestRun` 后续可作为 `EvaluationExecution` 中一个 sample run 的 adapter，但不会因此升级为
`Assessment`。只有独立协议、评分、安全门、稳定性和版本比较证据齐备时，才能形成平台
发布评测结论。

平台在当前 lane 中只做确定性编排和证据投影，不解释或重写 pytest：

```text
业务 Agent Workspace tests/
  -> 完整 40 位 Git commit
  -> 确定性源码投影
  -> durable queued AgentTestRun
  -> 独立 worker + 一次性 sandbox
  -> typed execution receipt
  -> 当前待发布 commit 的工程发布条件
```

## 2. 对象与所有权

| 对象 | 权威所有者 | 说明 |
| --- | --- | --- |
| `tests/README.md` | Workspace 开发者 | 说明测试范围、依赖和人工复核边界；缺失只告警 |
| `tests/conftest.py` | Workspace 开发者 | 可选，本 Agent 的本地 fixture |
| `tests/test_*.py` | Workspace 开发者 | 可执行测试资产；当前只接受 `tests/` 下扁平文件 |
| `agentgov_testkit` | AgentGov 平台 | 小型、版本化 Python 库和 pytest plugin；不持有测试正文 |
| `AgentTestSuiteSummary` | 平台派生 | 从指定 commit 扫描文件、诊断和 `suite_digest`，不单独存正文 |
| `AgentTestRun` | AgentGov 平台 | 一次精确 commit 固定命令的 durable 队列与执行记录 |
| `AgentTestExecutionReceipt` | AgentGov worker | 绑定 source/tree/suite、容器、隔离、结果和 cleanup 证据的 strict typed 回执 |
| `AgentTestSchedule` | AgentGov 平台 | 每个业务 Agent 唯一定时策略；只产生 durable run，不保存测试正文 |
| `AgentTestScheduleEvent` | AgentGov 平台 | 一次计划窗口的触发审计；记录跳过、合并、入队或失败结果 |
| 回归测试代码候选 | 治理 Agent + 平台 | 治理 Agent 输出测试代码、意图和断言依据；用户确认前不是 Workspace 资产 |
| `change_set_id` | AgentGov 平台 | 关联同一未发布变更；不是测试身份或版本身份 |

被测版本的权威标识是 Git `commit_sha`。`source_tree_sha` 绑定该 commit 的完整 Git tree，
`source_digest` 绑定实际允许进入 sandbox 的确定性源码投影，`suite_digest` 只绑定其中的
`tests/` 套件内容；`change_set_id` 只表达未发布变更关联。这些摘要各自承载不同边界，
不组合成新的人工版本身份。

仓库测试与运行态测试按内容所有权分层，不按执行工具分层：

| 测试位置 | 内容所有者 | 仓库质量策略 |
| --- | --- | --- |
| 根 `tests/` | AgentGov 平台开发者 | 收集，验证平台代码、API、迁移和契约 |
| `docker/runtime-bootstrap/business-agents/<agent_id>/workspace/tests/` | 对应内置业务 Agent 开发者 | 不纳入系统静态 collection；按精确 commit 独立执行 |
| `docker/runtime-bootstrap/governor-workspace/tests/` | governor Workspace 开发者 | 原路径收集，不参与业务 Agent 发布条件 |
| `${HOME}/volume-agent-gov/data/business-agents/<agent_id>/workspace/tests/` | 对应业务 Agent 开发者 | 不做仓库静态扫描；按精确 commit 执行 |

`tests/quality_policy.json` 的根 collection 只声明平台测试与 governor 测试。无论业务 Agent
位于仓库初始化源还是运行卷，其自测都不进入平台源码提交门；平台按待发布 commit
执行完整 `workspace/tests/`，不按本次 Diff 选择叶子用例。

## 3. Workspace 与 suite 契约

```text
workspace/
├── CLAUDE.md
├── .claude/
├── .mcp.json
└── tests/
    ├── README.md
    ├── conftest.py       # 可选
    └── test_*.py
```

规则：

1. `tests/test_*.py` 必须是可解析 UTF-8 Python 文件，当前不递归发现子目录。
2. 每个测试文件、fixture 和辅助资产都随 Workspace Git 提交。
3. 包导入缺少 `tests/`、`tests/README.md` 或测试文件时可以生效，但返回结构化诊断；
   没有可运行文件的版本不能满足工程发布条件。
4. 导入目标由 URL 中的 `agent_id` 指定，包根目录 `agent.yaml.agent.id` 必须有效且与其逐字
   一致；缺失、无效、格式错误或来源 ID 不一致均在 Workspace、注册表、Git 和会话状态
   变更前拒绝，并保留失败审计。
5. 套件使用 `agent` live fixture 时，平台会设置 `requires_live_agent=true` 和诊断
   `AGENT_TEST_LIVE_FIXTURE_REQUIRES_P1`。当前 `p0-exact-commit` 静态 lane 必须整套拒绝，
   不会在无网络 sandbox 中伪装成真实 Agent 行为验证。
6. 单个 Agent 的 suite 检查异常不得拖垮 `/api/agent-test-assets` 整体列表。受影响 Agent 返回
   `AGENT_TEST_SUITE_INSPECTION_UNAVAILABLE` 及稳定原因码，并以不可运行 suite 投影；其他 Agent
   仍正常展示。修复并重新检查前，该 Agent 不得产生发布证据。

导入成功响应与审计记录同时保留 `test_suite_status=ready|warning|invalid`、suite 和
完整 diagnostics。如果候选 suite 检查不可用但该 Workspace 仍被允许激活，激活与 accepted audit
必须处于同一一致性边界；suite 记为 `invalid`，不泄露内部异常详情、不自动启动测试，也不得获得发布资格。

## 4. `agentgov_testkit` 与 live 测试边界

`packages/agentgov-testkit` 保留公共 Python API，供 Workspace 开发者编写业务行为测试：

```python
from agentgov_testkit import invoke_agent


def test_alert_triage():
    result = invoke_agent("请判断该告警是否需要升级")
    assert "升级" in result.text
    assert "证据" in result.text
```

pytest 测试也可使用 `agent` fixture：

```python
def test_alert_triage(agent):
    result = agent.run("请判断该告警是否需要升级")
    assert "证据" in result.text
```

开发者 live 测试使用 `AGENTGOV_API_BASE`、`AGENTGOV_AGENT_ID`、`AGENTGOV_COMMIT_SHA` 和可选的
`AGENTGOV_API_KEY`。pytest session 只解析一次精确 commit，并显示 `resolved commit`；`agent`
fixture 为 function scope，每个测试函数创建并关闭独立 Agent 会话，避免历史消息、工具状态
和上下文窗口在用例间污染。

该 live 开发入口不是 Phase 7 的 `p0-exact-commit` 平台运行，开发者本地通过不会产生
P0 typed receipt 或独立评测结论。使用 live fixture 的 Workspace suite 需要进入后续 P1 live lane；
在该 lane 落地前，当前平台静态运行和发布门会 fail-closed，不以本地结果替代。

平台内部可额外注入 `AGENTGOV_CHANGE_SET_ID`；开发者不需要配置它。显式传入的
`AGENTGOV_TEST_SESSION_ID` 只供直接调用 `invoke_agent()` 的开发者管理会话使用，pytest fixture
不复用该会话。公共入口只有 `invoke_agent()` 和 pytest 的 `agent` fixture；HTTP client
与会话对象由 testkit 内部封装。

## 5. 平台 API 与 durable enqueue

```text
GET    /api/agent-registry/{agent_id}/test-suite?commit_sha=<sha>
GET    /api/agent-registry/{agent_id}/test-suite/file?path=<path>&commit_sha=<sha>
GET    /api/agent-test-assets
POST   /api/agent-test-runs
GET    /api/agent-test-runs
GET    /api/agent-test-runs/history
GET    /api/agent-test-runs/{test_run_id}
POST   /api/agent-test-runs/{test_run_id}/cancel
POST   /api/agent-change-sets/{change_set_id}/test-runs
GET    /api/agent-registry/{agent_id}/test-schedule
PUT    /api/agent-registry/{agent_id}/test-schedule
GET    /api/agent-registry/{agent_id}/test-schedule/events

POST   /api/agent-test-sessions
POST   /api/agent-test-sessions/{test_session_id}/messages
DELETE /api/agent-test-sessions/{test_session_id}
```

对 `AgentTestRun` 执行链，API 只做身份/版本校验、suite 检查、指纹固定和 durable enqueue，
不在 API 进程或 API 容器内启动 pytest、子进程或 sandbox 容器。

### 5.1 手工运行和待发布运行

手工 `POST /api/agent-test-runs` 必须同时提交 `agent_id` 和完整小写 40 位 `commit_sha`；
缺失、短 SHA、不存在或不属于该 Agent 仓库的 commit 均在入队前拒绝。后端不会替手工
调用方解析移动的“当前版本”。

待发布入口 `POST /api/agent-change-sets/{change_set_id}/test-runs` 从后端已持久化的
`AgentChangeSet` 解析 `agent_id` 和 `candidate_commit_sha`，不接受客户端重复提交身份字段。
两个入口都会在入队前完成以下动作：

1. 解析并验证精确 commit；
2. 从 Git object 进行确定性投影，得到 `source_tree_sha` 和 `source_digest`；
3. 检查 suite，固定 `suite_digest`，拒绝不可运行或需要 live fixture 的套件；
4. 持久化 `status=queued`、固定命令和上述指纹。

平台唯一执行命令为：

```bash
/usr/local/bin/python -I -P -m pytest -q --import-mode=importlib -p agentgov_testkit.pytest_plugin tests
```

`-I -P` 防止业务 commit 根目录的 `pytest.py` 或 `pytest/` 抢占平台 pytest，
`--import-mode=importlib` 避免 pytest 把测试目录前插到 `sys.path`。客户端不能提交命令、
工作目录、环境变量、测试结果、通过状态或任意安装步骤。

### 5.2 定时策略

每个业务 Agent 最多一条策略，支持常用频率和自定义五字段 Cron（分、时、日、月、周）。
时区使用 IANA 名称，前端默认采用浏览器时区；两次计划窗口最短间隔为 15 分钟。保存策略
只修改配置，不立即运行测试。

API lifespan 中的 scheduler 只持久化计划事件、解析触发时当前有效 commit 并通过同一
`create_run` 契约入队；它不执行 pytest、不创建 sandbox。所有
`manual` / `release_check` / `scheduled` 运行都由独立 worker 消费同一 durable queue。

每个计划窗口先持久化唯一 `(schedule_id, scheduled_for)` 事件，再入队：

1. 仅 `active`、`evaluating` Agent 可触发；终态 `archived` 或已删除 Agent 会停用策略并保留审计；
2. 触发时只解析一次当前有效 commit，不读取候选 worktree，不绑定或推进待发布变更；
3. 创建 `source=scheduled`、`change_set_id=null` 的 `AgentTestRun`，记录 `schedule_id` 和 `scheduled_for`；
4. 同 Agent、同 commit 已有 `queued/running` 时不重复执行，事件记为 `coalesced`；
5. API 停机错过多个窗口时只补一次，并把 `next_run_at` 推进到当前时间之后；
6. 定时事件的状态为 `pending -> enqueued | coalesced | skipped | failed`，不会推进、批准、发布
   或回滚待发布变更。

## 6. 独立 worker、源码投影与 sandbox

### 6.1 worker-only run 根

Compose 使用独立 `agent-test-worker` 服务。worker 挂载运行数据 `/data`、Docker socket 和专用
Docker-managed named volume `/agent-test-runs`；API 只挂载 `/data`，不挂载也不能写该运行卷。
`AGENT_TEST_RUNS_DIR=/agent-test-runs` 是 worker 对运行卷的唯一读写视图。worker 启动时通过自身
Docker inspect 固定 volume identity，并拒绝非 `local` driver、带 driver options 的 bind 型 volume、
只读或与 `/data` 混淆的挂载。sandbox 不再接收会被 Docker daemon 二次解析的宿主路径，而是复用
同一 volume identity，仅把 `<test_run_id>/workspace` subpath 只读挂到 `/workspace`。

Docker socket 只授予 worker 控制平面；sandbox 本身不挂载 Docker socket。每次运行使用
`/agent-test-runs/<test_run_id>/workspace` 作为独立投影，完成后删除。Docker create 后、start 前还会
核对 HostConfig 中的 volume name、只读标记与 subpath，以及实际 mount 的同一 volume name，防止
路径别名重定向造成 worker 校验 A、sandbox 执行 B。

该 named volume 是可清理的执行 scratch，不是新的业务持久化真源；队列、终态回执和调度审计仍在
`/data/runtime.sqlite3`，测试正文仍在 per-Agent Git。worker 启动恢复与每次终态清理都删除 run
投影，隔离验收使用独立 Compose project 并通过 `down --volumes` 删除整个临时 volume。

### 6.2 完整 tree 与确定性源码投影

worker 从 per-Agent Git 解析入队时的精确 commit，先校验完整 Git tree，再为当前静态 lane
生成确定性源码投影：

- `source_tree_sha` 绑定原始 commit 的完整 tree，不因投影排除私有文件而改变；
- `source_digest` 绑定实际写入 worker run 根、允许进入 sandbox 的文件模式、路径和字节；
- `.env`、local settings、私钥/凭据文件、凭据存储与 `secret` / `credential` 目录不进入投影；
- `.mcp.json` 与 `.claude/settings.json` 使用结构化静态校验；安全字面量和环境变量引用可保留，
  凭据字面量会使整个配置文件不进入投影，语法或结构歧义则 fail-closed；
- live Workspace 和 per-Agent Git 仍按字节保留这些私有运行资产，投影不回写或脱敏源仓库。

这是针对路径与两类结构化配置的确定性投影规则，不是通用 DLP。Agent-owned 测试源码和
其他公开文件不因进入该 lane 就获得“全文无敏感信息”证明。

### 6.3 真实 volume 目录观察

在启动 sandbox 前，worker 使用 dir-fd、`O_NOFOLLOW` 和 `lstat/fstat` 身份校验，从实际
named volume 目录重算摘要。容器终止并完成清理后，worker 对同一 run 根再次重算；不以两次
读取 Git commit 代替执行期的实际 volume 目录校验。typed receipt 使用四态观察：

| `source_observation` | 证据语义 | 允许的结果 |
| --- | --- | --- |
| `not_observed` | 没有完成执行前实际源摘要，例如执行前取消或 worker 重启接管历史 `running` | `cancelled`、`interrupted` 或 `error` |
| `pre_only` | 执行前摘要已验证，但无法取得可信的执行后摘要 | 只允许 `error` |
| `stable` | 执行前、执行后与入队 `source_digest` 三者相等 | 才可表达 pytest `passed` 或 `failed` |
| `changed` | 执行前等于预期，执行后摘要不同 | 只允许 `error` |

`post_source_digest` 只在真实观察到执行后源时存在，不会用入队摘要伪造“执行后稳定”。

### 6.4 最小隔离与 typed receipt

一次性 sandbox 只将当前 run 的 volume subpath 挂载为唯一源码挂载 `/workspace:ro`；`/output` 与 `/tmp` 都是
限额 tmpfs，不存在可写 host output bind。容器固定无网络、非特权、非 root 用户、只读根文件系统、
`cap_drop=ALL`、`no-new-privileges`、无设备、无端口、无 Docker socket，并限制 PID、内存、CPU、
shared memory 和日志大小。Docker inspect 任一约束不匹配都 fail-closed。

pytest plugin 将同一份有界 JSON report 先写入容器 tmpfs 临时报告，再从专用单行
stdout envelope 发出。worker 只用 Docker logs `tail` 读取最后一行，严格限长和 schema 校验，
并从诊断 stdout 中剔除 envelope。`receipt.result.workspace_report_authority` 固定为
`agent_owned_unverified`，由回执声明 report/items/invocations 不能注入 status、worker、score、approval
或发布事实。

终态回执使用 `assurance_level=execution_provenance`，绑定精确 Agent/commit、tree/source/suite、worker fence、
image ID、固定 argv/环境、Docker inspect 隔离观察、pytest 退出、输出摘要、四态源观察、
容器删除、label 残留和临时路径清理。回执自身带完整性 digest。它只证明精确提交在
该固定隔离环境中按固定命令执行及 Docker 观察结果，不等于独立证明业务正确性、
能力质量、安全性或后续真实 Agent 行为。

## 7. 运行生命周期与重启所有权

状态集合：

```text
queued -> running -> passed | failed | error | cancelled
running --worker 重启恢复--> interrupted | error
```

状态是平台执行记录，不映射 Claude Agent SDK 的权限生命周期。重启所有权固定为：

- API 重启只清理它拥有的临时 test session 和检查 materialization，并恢复 durable
  schedule `pending` 事件；它不接管、执行或改写 `queued/running` 运行；
- worker 以专用 run 根的独占锁保证同一 Runtime 卷只有一个消费者。启动时它通过 worker fence
  接管遗留 `running`，按 Docker labels 清理容器与 run 目录，再将记录收口为 `interrupted`
  或 cleanup `error`；
- worker 只在证明全局 label 残留和临时路径已清理后继续领取 durable `queued`；
  清理无法证明时停止消费，不带病继续；
- `cancel_requested` 持久化。已运行的 sandbox 由 worker 终止，超过
  `AGENT_TEST_RUN_TIMEOUT_SECONDS` 同样由 worker 终止并记录 `AGENT_TEST_RUN_TIMEOUT`；
- 同一 Agent、commit 和待发布目标只允许一个 `queued/running` 记录，重复请求返回 `409`；
- 临时 testkit 会话属于 API 进程，API 重启后调用返回明确 session unavailable，不伪造恢复。

已完成的 stdout、stderr、结构化 pytest item、错误详情和 typed receipt 均持久化且有大小上限。

## 8. 资产复利中心投影

“资产复利中心”默认展示“测试资产”，并保留“治理资产”页签中的方法论、执行资产和审计
关系投影；当前 `audit` 过滤值属于横切审计维度，不定义长期第四类资产。

测试页按业务 Agent 展示当前有效 commit 的 suite、文件数、完整诊断、最近运行和定时状态；
详情分为“测试文件”“运行历史”和“定时策略”。单 Agent suite 不可用时，该导航项展示失效
诊断，整个资产列表仍可用。源码查看满足以下约束：

- 只接受当前 suite 已声明的 `tests/test_*.py` 路径，拒绝绝对路径和目录穿越；
- 通过 Workspace Git 在指定 commit 读取 UTF-8 正文，提供行号、语法高亮、搜索、复制和符号定位；
- 不把源码复制到 `governance_assets`、运行记录或新的数据库测试集；
- 历史列表只返回轻量摘要并分页，点击单次运行后再读取 stdout、stderr、pytest item、
  invocation、四态源观察、隔离和错误详情；
- 不提供跨 Agent“继承测试代码”动作；需要复用时仍通过 Workspace Git 评审和提交。

该页是 Agent 自有工程回归资产入口，不管理 evaluator-owned holdout 正文，不代替“业务 Agent
详情 → 测评”的长期单 Agent 能力入口，也不以测试页的局部 state 构造独立评测中心。

## 9. 反馈优化生成测试

该阶段固定拆为三个独立动作：

1. **生成回归测试**：治理 Agent 只输出完整 pytest 代码、测试意图和断言依据；后端校验
   AST、依赖、`agent.run(...)` 调用与业务断言，并确定
   `tests/test_feedback_<id>_<digest>.py` 路径。生成结果只以完整新文件 Diff 展示，
   不写入 Workspace、不提交 Git、不运行测试。该候选使用 live fixture，属于后续 P1 live lane，
   不得伪装为 P0 静态回执。
2. **确认待发布变更**：校验事项、业务 Agent、归因、优化方案、执行记录和待发布变更仍属于
   同一链路；在隔离 worktree 新增已确认测试文件，不覆盖、删除或弱化已有测试；
   把配置修改与测试文件压缩为相对修复前版本的单一待发布 Git commit。
3. **运行测试**：由独立显式动作创建 `AgentTestRun`；确认动作不得隐式排队、运行
   pytest 或推进发布。当套件包含 live fixture 时，当前 P0 入口必须 fail-closed，由 P1
   live lane 承载真实行为执行与评测证据。

生成代码继续受限为一个不超过 60 行、只含一个同步 `test_*` 的单焦点模块。后端拒绝只检查
非空、恒等比较、嵌套死分支、辅助函数、`any(...)` 和 `A or B` 候选关键词等可误通过写法。
原始反馈、已确认整理和优化方案中每个独立可观察修复结果必须分别有正向断言；
`test_intent` 和 `assertion_rationale` 不能代替代码断言。

同一待发布变更在返工后可产生更新的待发布 commit。旧 commit 和运行记录保持可审计，但只有
当前待发布 commit 的结果参与发布条件判断。

## 10. 工程发布条件

当前 Workspace pytest 门只表达工程准入，必须同时满足：

- 归因、执行与待发布版本 provenance 完整；
- 待发布变更有精确的 `candidate_commit_sha`；
- 该 commit 的 Workspace 存在当前静态 lane 可运行的测试文件；
- 存在同 Agent、同待发布 `commit_sha` 的 `passed` 运行；
- 后端重新物化精确 commit 后，suite/source/tree 与持久化指纹一致；
- typed receipt 完整性通过，worker/container 绑定一致，`assurance_level=execution_provenance`，
  `receipt.result.workspace_report_authority=agent_owned_unverified`，cleanup 完整；
- `source_observation=stable`，且 pre/post/queued `source_digest` 三者相等；
- 没有其他发布阻塞项。

仅 `stable` 的 `passed` typed receipt 可作为发布工程证据。`not_observed`、`pre_only`、`changed`、
旧历史无回执记录、旧 commit 的通过、空测试目录、只有测试设计、`failed`、`error`、
`cancelled` 或 `interrupted` 均不能放行。

反馈发布工作台不提供强制绕过入口。受保护 API 的 `force=true` 也只表示全部门禁已满足
后的管理员加急与审批审计，不能绕过测试、provenance 或其他阻塞项。UI 使用“修复前版本”
和“待发布版本”，不使用含义不清的“基线”“候选”作为用户标签。

该门不得被文档或 UI 宣称为业务能力提升、评测基准通过或安全已独立证明。P1
发布评测落地后，最终发布裁决还必须组合 evaluator-owned `Assessment`、同协议 comparison、
safety gate 和所需人工决定。

## 11. 远程开发与调试

远程开发者可导入 Workspace 包，再在本地 pytest 中连接平台。为了使用与评审对象一致，
文档与调试配置均显式固定完整 commit：

```bash
export AGENTGOV_API_BASE=http://agent-gov.example
export AGENTGOV_AGENT_ID=customer-support
export AGENTGOV_COMMIT_SHA=<full-40-character-commit-sha>
export AGENTGOV_API_KEY=...
python -I -P -m pytest -q --import-mode=importlib -p agentgov_testkit.pytest_plugin tests
```

本地断言在开发者 pytest 进程执行，平台只提供被测 Agent 调用和会话固定。该路径不会将
本地代码上传给平台执行，也不产生 `p0-exact-commit` typed receipt。平台工程门只认可
`POST /api/agent-test-runs` 或待发布入口经独立 worker 形成的稳定回执。

## 12. 迁移与旧设计删除

本契约已替换数据库 `TestDataset`、`EvalRun`、逐 case review API 和通用资产中的测试正文类型。
这里删除的是以数据库测试正文副本为权威的旧链路，不禁止后续建立协议中立的
`EvaluationExecution` / `Assessment` 领域对象。新对象只引用 Workspace 或 evaluator-owned 资产与
运行证据，不得借新名称恢复正文双轨。

- migration `0048` 归档旧行后删除旧表和待发布变更上的历史评测字段，建立平台测试运行表；
- migration `0049` 把四阶段产物统一为 `RegressionTestDesign` 命名；
- migration `0050` 收敛重复活跃测试运行，建立精确目标唯一索引；
- migration `0051` 原样归档旧自然语言测试设计，删除旧表并建立测试代码、测试意图和断言依据契约；
- migration `0052` 增加每 Agent 唯一定时策略、调度事件和运行触发来源字段；
- migration `0053` 为运行增加 worker fence、source/tree、container 引用与 nullable typed receipt；
  历史运行不回填回执，不获得发布资格；
- migration `0054` 为 Workspace 导入审计增加 suite status 和完整 diagnostics 投影，不复制测试正文；
- migration `0055` 为 Workspace 导入/恢复增加持久化激活日志，绑定原状态、候选提交、完整诊断与
  runtime fence；prepared 后 graph identity 不可变，恢复使用平台自有 Git 环境校验 canonical
  commit/tree/parent 关系和完整 index 语义，崩溃恢复无法证明一致时保持 `recovery_required`；
- migration `0058` 不增加测试正文或新运行对象；它幂等重装 0055/0057 authority triggers，
  避免已应用旧版迁移的持久卷遗漏 graph 冻结、合法转移、终态不可变/禁止删除与
  recovery terminal-evidence 约束；
- `make runtime-migrate-workspace-tests-scan` 只读扫描运行卷，确认后使用
  `make runtime-migrate-workspace-tests` 将旧 `evals/` 归档到 Workspace 外，并为缺测试的内置业务
  Agent 提交产品自带测试；普通业务 Agent 缺测试时仍只告警；
- 旧 API 内执行链、本地执行 facade、旧 service/store、前端旧类型和 E2E 路径
  从活跃代码删除；历史迁移和归档文档可保留旧名，但不构成兼容入口。

## 13. 验收

- 测试权威：业务 Agent 的测试正文只来自 Workspace Git，数据库和 Registry 只保存指纹、证据和关系。
- 版本绑定：手工运行缺少完整 commit 或提交短 SHA 时失败；待发布与定时入口由后端在入队前固定精确 commit。
- 入队：API 只持久化已校验的 `queued` 运行，不在本进程启动 pytest 或 Docker sandbox。
- 源码：完整 tree 和沙箱投影摘要分开绑定；私有路径与凭据字面量不进入投影，不宣称通用 DLP。
- Git authority：受管命令固定 git-dir/work-tree，不执行 repository-local 外部驱动，不消费
  alternates/非规范 commondir/grafts/partial clone；commit tree/parents 从 raw object header 解析，
  activation refs 与 HEAD 保持 direct-ref canonical topology。
- worker：独占锁、worker-only named volume `/agent-test-runs`、实际 volume 目录 pre/post 四态观察、claim fence、取消、
  超时、残留清理和重启恢复都有正向与负向测试。
- sandbox：固定命令与精确三项环境、同一 named volume 的 per-run `/workspace:ro` subpath、
  `/output` / `/tmp` tmpfs、无网络、无 Docker socket、资源上限、
  stdout envelope + `tail` 读取、Docker inspect 不匹配和 cleanup 失败都 fail-closed；父 orchestrator
  的 SIGINT/SIGTERM 必须终止并 reap 当前子进程组，随后完成 exact teardown 并以 130/143 退出。
- 回执与发布：只有同 commit、`stable`、完整性通过的 `passed` typed receipt 可满足工程门；
  历史无回执记录和其他源观察状态不放行。
- 资产列表：任一单 Agent suite 检查失败时只降级对应项，`/api/agent-test-assets` 其他 Agent
  仍可见，受影响项显示结构化诊断。
- 产品边界：工程回归运行不被声称为能力 benchmark、独立 Assessment、安全通过或线上业务效果。
- 真实容器验收：公共 `make container-workspace-pytest-test` 基于当前工作树构建 API、worker 和 sandbox，
  在独立临时 Runtime 根、Compose project 与临时 named volume 中执行，不读写现有 live volume，
  入口先形成候选 Git tree 和 materialized snapshot，再用同一候选构建全部镜像；
  `docker/runtime-bootstrap` 已构建进 API 镜像，Compose 不为 `/app/docker/runtime-bootstrap` 配置 host bind；
  完成后验证容器、网络、卷、label 和临时目录无残留。
- 工程门：目标 pytest、文档契约、OpenAPI/生成类型、前端构建、`make main-flow-test`、
  `make codex-guard` 和提交前串行 `make test` 按风险分层通过。
