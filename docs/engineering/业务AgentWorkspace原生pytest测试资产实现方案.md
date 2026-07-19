# 业务 Agent Workspace 原生 pytest 测试资产实现方案

> 文档状态：当前实现与后续演进的权威工程契约。
>
> 适用对象：所有注册业务 Agent（含 `main-agent`）。治理 Agent 不进入业务 Agent 注册表、发布链或
> 业务测试会话；其项目自测仍可使用同样的 `tests/` 目录约定。

## 1. 裁决

业务 Agent 的测试必须与其 Claude 原生 Workspace 一起开发、评审、版本化、导入、导出和发布。
`workspace/tests/` 是测试资产唯一真相源，平台不再维护数据库测试集、全局用例池或第二套测试内容副本。

平台负责确定性执行和证据投影，不解释或重写 pytest：

```text
业务 Agent Workspace tests/
  -> 精确 Git commit
  -> 固定 pytest 命令
  -> 持久化 AgentTestRun
  -> 当前待发布 commit 的发布条件
```

## 2. 对象与所有权

| 对象 | 权威所有者 | 说明 |
| --- | --- | --- |
| `tests/README.md` | Workspace 开发者 | 说明测试范围、依赖和人工复核边界；缺失只告警 |
| `tests/conftest.py` | Workspace 开发者 | 可选，本 Agent 的本地 fixture |
| `tests/test_*.py` | Workspace 开发者 | 可执行测试资产；首版只接受 `tests/` 下扁平文件 |
| `agentgov_testkit` | AgentGov 平台 | 小型、版本化 Python 库和 pytest plugin |
| `AgentTestSuiteSummary` | 平台派生 | 从指定 commit 扫描文件、诊断和 `suite_digest`，不单独存内容 |
| `AgentTestRun` | AgentGov 平台 | 记录一次固定命令的状态、输出、条目和精确 commit |
| 回归测试代码候选 | 治理 Agent + 平台 | 治理 Agent 输出测试代码、测试意图和断言依据；平台确定目标路径。确认前不是 Workspace 资产 |
| `change_set_id` | AgentGov 平台 | 关联同一未发布变更；不是测试身份或版本身份 |

权威版本标识是 Git `commit_sha`。`suite_digest` 是测试文件内容的派生校验值；`change_set_id` 只表达
未发布变更的业务关联。三者职责不同，不组合成新的人工身份。

## 3. Workspace 契约

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

1. `tests/test_*.py` 必须是可解析 Python 文件，首版不递归发现子目录。
2. 每个测试文件、fixture 和辅助资产都随 Workspace Git 提交。
3. 包导入缺少 `tests/` 或 `tests/README.md` 时成功但返回结构化 warning；没有测试文件时不能满足普通发布条件。
4. 导入目标身份只取 URL 中的 `agent_id`。包文件名、压缩包名和 `agent.yaml` 中的 ID 都不是平台身份。
5. `security-operations-expert` 初始化源和 governor Workspace 均提供 `tests/`；普通业务 Agent 只存在于运行卷和其导出包中。

## 4. agentgov_testkit

`packages/agentgov-testkit` 提供公共 Python API：

```python
from agentgov_testkit import invoke_agent


def test_alert_triage():
    result = invoke_agent("请判断该告警是否需要升级")
    assert "升级" in result.text
    assert "证据" in result.text
```

pytest 测试可直接使用 `agent` fixture：

```python
def test_alert_triage(agent):
    result = agent.run("请判断该告警是否需要升级")
    assert "证据" in result.text
```

平台通过环境变量提供 `AGENTGOV_API_BASE`、`AGENTGOV_AGENT_ID`、可选的
`AGENTGOV_COMMIT_SHA` 和 `AGENTGOV_API_KEY`。pytest session 创建时只解析一次目标 commit，后续
全部用例固定使用该 commit，并显示 `resolved commit`；但 `agent` fixture 是 function scope，每个测试函数
创建并关闭独立 Agent 会话，避免历史消息、工具状态和上下文窗口在用例间污染。平台内部运行可额外注入
`AGENTGOV_CHANGE_SET_ID`；开发者不需要配置它。显式传入的 `AGENTGOV_TEST_SESSION_ID` 只供直接调用
`invoke_agent()` 的开发者管理会话使用，pytest fixture 不复用该会话。

开发者不需要 `AgentGovTestClient` 或独立 CLI。公共入口只有 `invoke_agent()` 和 pytest 的 `agent`
fixture；HTTP client 与会话对象均由 testkit 内部封装。

## 5. 平台 API

```text
GET    /api/agent-registry/{agent_id}/test-suite?commit_sha=<sha>
POST   /api/agent-test-runs
GET    /api/agent-test-runs
GET    /api/agent-test-runs/{test_run_id}
POST   /api/agent-test-runs/{test_run_id}/cancel
POST   /api/agent-change-sets/{change_set_id}/test-runs

POST   /api/agent-test-sessions
POST   /api/agent-test-sessions/{test_session_id}/messages
DELETE /api/agent-test-sessions/{test_session_id}
```

创建运行时可省略 `commit_sha`。平台在创建请求内读取当前 commit 并只钉住一次，后续运行、重试和结果
均使用该 SHA，不把“当前版本”解释延迟到执行时。待发布变更测试入口从变更记录读取 `agent_id` 和
待发布 commit，不接受客户端重复提交身份字段。

平台唯一执行命令为：

```bash
python -m pytest -q -p agentgov_testkit.pytest_plugin tests
```

客户端不能提交命令、工作目录、测试结果、通过状态或任意安装步骤。首版不在 API 容器内执行
`pip install`，也不自动运行上传代码；测试只能由用户显式点击“运行测试”或调用对应运行 API 触发。

## 6. 运行生命周期与服务重启

状态集合：

```text
queued -> running -> passed | failed | error | cancelled
running --服务关闭/重启--> interrupted
```

状态是平台执行记录，不映射 Claude Agent SDK 的权限生命周期。服务重启时：

- 已经 `running` 的进程不能被假定继续存在，记录明确转为 `interrupted`；
- 尚未领取的 `queued` 记录由启动恢复器重新入队；
- `cancel_requested` 持久化，取消会终止整个进程组；
- `AGENT_TEST_RUN_TIMEOUT_SECONDS` 到期会终止整个进程组，并以 `error` 和
  `AGENT_TEST_RUN_TIMEOUT` 记录；
- 同一 Agent、同一 commit、同一待发布变更只能有一个 `queued/running` 记录，重复请求返回 `409`；
- 临时测试会话只存在于当前进程，重启后调用返回明确的 session unavailable 错误；
- stdout、stderr、结构化 pytest item 和错误详情均持久化，并有大小上限。

## 7. 反馈优化生成测试

该阶段固定拆为三个独立动作：

1. **生成回归测试**：治理 Agent 只输出完整 pytest 代码、测试意图和断言依据；后端校验 AST、依赖、
   `agent.run(...)` 调用以及面向 `result.text` / `result.raw` 的业务断言，并确定
   `tests/test_feedback_<id>_<digest>.py` 路径。生成结果以完整新增文件 Diff 展示，不写入 Workspace、
   不提交 Git、也不运行测试。一次生成只形成一个不超过 60 行、仅含一个同步 `test_*` 的单焦点 pytest
   模块；平台提供 `agent` fixture，生成代码不得定义或覆盖任何 fixture。该任务直接使用 Claude Agent SDK
   原生 `output_format/json_schema` 和 `ResultMessage.structured_output`，避免第二个模型改写代码；后端再用
   Pydantic 与 AST 做确定性校验。governor Trace 由后端投影完整 `sdk.tool.*` / `sdk.llm.*` I/O；其子进程
   不再重复上报 I/O 为空且会产生误导性 `tool.blocked_on_user` 计时事件的 Claude Code 原生 OTEL span。
   生成失败时返回结构化错误，不用启发式逻辑伪造测试。测试必须使用类型无关的
   `assert not result.errors` 断言运行无错误；`errors` 是 tuple，不接受 `result.errors == []`。
   自然语言回答中的标签可能因 Markdown 排版出现空格或换行，固定业务词先用
   `normalized_text = "".join(result.text.split())` 做最小空白规范化，再对每个预期业务结果分别断言；这只消除格式差异，不得将多个可选结果宽松化为通过。
   原始反馈、已确认整理和优化方案中每个独立可观察的修复结果必须分别有正向断言；`test_intent` 和 `assertion_rationale` 不能代替测试代码中的断言。
   已给出全部判断事实的自包含用例，输入必须明确「仅依据已给定事实、不调用工具或读文件」，并断言 `result.raw["agent_activity"]["tool_calls"] == []`；避免本地和平台复跑因未声明 MCP、文件或网络状态发生漂移。
   后端拒绝仅检查非空、恒等比较、嵌套死分支、辅助函数、`any(...)`
   和 `A or B` 候选关键词等可误通过写法，也不能只断言相反结果未出现而遗漏目标结果。原始反馈已经给出判断事实时，测试输入必须内嵌这些事实；
   除非上下文给出可运行的固定资源引用，不得把测试改写为依赖未声明 MCP、数据库或网络数据的查询。
2. **确认待发布变更**：校验事项、业务 Agent、归因、优化方案、执行记录和待发布变更仍属于同一链路；
   在隔离 worktree 新增已确认测试文件，不覆盖、删除或弱化已有测试；把配置修改和测试文件压缩为
   相对修复前版本的单一待发布 Git commit。任一步失败时恢复原待发布提交。
3. **运行测试**：由独立显式动作创建 `AgentTestRun`，平台 checkout 当前待发布 commit 并运行完整
   `tests/`。确认动作不得隐式排队或运行 pytest。

同一待发布变更可在返工后产生更新的待发布 commit。旧 commit 及其运行记录保持可审计，但只有
当前待发布 commit 的结果参与发布条件判断。

执行优化也受同一事项范围约束：确认后的优化方案决定本次可写目标，执行任务只能读取和修改这些目标；
规范化反馈明确“仅修改”某个 Workspace 路径时，治理 Agent 不得扩展到 Skill、settings、MCP 或其他文件。
对已有 Markdown 做整文件替换时，后端要求至少保留原文件一半非空行；该保真门只阻止明显截断，不能替代
完整 Diff 的人工审查。

## 8. 发布条件

普通发布必须同时满足：

- 归因与执行 provenance 完整；
- 待发布变更有精确的待发布 commit；
- 该 commit 的 Workspace 存在可运行测试文件；
- 存在同 Agent、同待发布 `commit_sha` 的 `passed` 运行记录；
- 没有其他发布阻塞项。

`suite_digest` 作为该提交测试内容的派生完整性摘要，`change_set_id` 作为未发布改动的关联元数据；
两者都不取代 `commit_sha`，也不组合成新的版本身份。旧 commit 的通过结果、仅有设计候选、空
`tests/`、失败、错误、取消或重启中断均不能放行。UI 使用
“修复前版本”“待发布版本”，不使用含义不清的“基线”“候选”作为用户标签。

强制发布是显式例外：必须通过受保护 API、提供非空原因，并把原阻塞项、原因、操作人和警告持久化到
release 与审计事件。provenance 不完整时不得强制绕过。

## 9. 导入、远程开发与调试

远程开发者可把 Workspace 包导入为一个此前不存在的 URL `agent_id`，再在本地 pytest 中连接平台：

```bash
export AGENTGOV_API_BASE=http://agent-gov.example
export AGENTGOV_AGENT_ID=customer-support
export AGENTGOV_API_KEY=...
python -m pytest -q -p agentgov_testkit.pytest_plugin tests
```

省略 commit 时，创建测试会话或运行会固定当时的当前版本。本地预检不绑定待发布变更；平台发布检查
统一调用待发布变更测试入口。测试断言在开发者本地 pytest 进程执行；平台只提供被测 Agent 调用、
版本固定和运行证据，不上传或反向执行任意本地测试代码。

## 10. 迁移与删除

该设计直接替换数据库 `TestDataset`、`EvalRun`、逐 case review API 和通用资产中的测试类型：

- 迁移 `0048` 归档旧行后删除旧表和待发布变更上的历史评测字段，并建立平台测试运行表；
- 迁移 `0049` 把四阶段产物统一为 `RegressionTestDesign` 命名；
- 迁移 `0050` 收敛重复活跃测试运行，并建立精确目标唯一索引；
- 迁移 `0051` 原样归档旧的自然语言 `expected_behavior + checks_json` 回归设计，删除旧表并建立
  测试代码、测试意图和断言依据的新契约；不把旧设计猜测转换为可执行测试；
- `make runtime-migrate-workspace-tests-scan` 只读扫描运行卷，确认后使用
  `make runtime-migrate-workspace-tests` 将旧 `evals/` 归档到 Workspace 外，并为缺测试的内置安全运营
  Agent 提交产品自带测试；已有普通业务 Agent 缺测试时仍只告警；迁移同时删除内置或运行态
  `agent.yaml.agent.id`，身份始终由 Agent Registry 决定；能够精确识别的历史平台弱断言测试会按内容
  摘要归档到 Workspace 外的 `data/archived-legacy-test-assets/`，并从活跃 `tests/` 删除。迁移不把
  `agent.invoke(...)` 机械改名为 `agent.run(...)`，不猜测业务断言；开发者编写的测试保持原样，带旧标记
  但结构不明的文件失败关闭并要求人工处理；
- 外部导入包若仍声明 `agent.yaml.agent.id`，平台保留包内容但返回
  `AGENT_MANIFEST_ID_IGNORED` 警告，明确该字段不参与身份解析；
- 旧 API、service、store、前端 hook、生命周期控件和 E2E 路径从活跃代码删除；
- 历史迁移文件和归档文档可保留旧名，用于升级与审计，不构成兼容层。

## 11. 验收

- 新建或导入 Agent：URL ID 生效；缺少测试只告警；包含测试时 suite 可按 commit 检查。
- testkit：显式/环境变量调用、commit 一次固定、每用例会话隔离、错误透传和 pytest 报告均有单测。
- 平台运行：固定命令、精确 commit、取消、输出限制、失败详情和服务重启恢复有专项测试。
- 反馈闭环：生成只形成代码 Diff；确认只新增扁平测试文件并形成配置与测试同一 commit；运行由独立动作排队。
- 发布：旧 commit 通过不能放行；当前 commit 通过可普通发布；强制发布必须有原因和持久化警告。
- UI：设计、测试文件、平台运行和发布条件分层呈现；桌面、平板和移动端无重叠或横向溢出。
- 工程门：专项 pytest、前端构建、`verify:design-parity`、`make main-flow-test`、`make codex-guard`
  和真实 Compose `ui-feedback-smoke` 通过。
