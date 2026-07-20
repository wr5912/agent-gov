# Agent 测试套件

本目录由该业务 Agent 的开发者维护，是 Agent 行为测试的权威来源。

在 AgentGov 源码仓库中，本目录由 `tests/quality_policy.json` 直接从当前路径收集；不得为了平台门禁
把测试正文复制到根 `tests/`。导出或导入后的运行态 Workspace 仍以自身 Git 中的本目录为准。

## 平台执行契约

平台始终以 `workspace/` 为工作目录，固定执行：

```bash
python -m pytest -q -p agentgov_testkit.pytest_plugin tests
```

- `test_*.py` 必须直接位于 `tests/`，不使用嵌套测试目录。
- 可选的 Agent 私有 fixture 放在 `tests/conftest.py`。
- 配置、hook、skill 或行为变化必须同步更新对应测试。
- 需要调用 Agent 时优先使用 `agent` fixture；直接使用
  `agentgov_testkit.invoke_agent()` 时由开发者负责测试会话。
- 测试不得安装依赖、修改平台命令或依赖修复前版本运行结果。
- `.venv/`、`.idea/`、`.pytest_cache/` 和 `__pycache__/` 是本机开发工件，
  不属于 Workspace 测试资产，也不得进入导入包。

## 本地 Python 环境

开发解压后的 Workspace 包时，推荐把虚拟环境放在 `workspace/` 的上层，避免重新打包时
误带入整个虚拟环境；禁止放在 `workspace/tests/`：

```text
<agent-dev-root>/
├── .env.agent-test       # 本机连接配置，不进入业务 Agent 包
├── .venv/                 # 本机 Python 环境，不进入业务 Agent 包
└── workspace/             # 业务 Agent 包根目录
    ├── CLAUDE.md
    ├── .claude/
    ├── hooks/
    └── tests/
```

`agentgov_testkit` 要求 Python 3.11 或更高版本。使用 `uv` 创建环境，并从 AgentGov
源码目录或平台提供的 wheel 安装 testkit：

```bash
cd <agent-dev-root>
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python pytest
uv pip install --python .venv/bin/python -e <agentgov-source>/packages/agentgov-testkit
```

在 `workspace/` 中运行与平台相同的命令：

```bash
cd <agent-dev-root>/workspace
../.venv/bin/python -m pytest -q -p agentgov_testkit.pytest_plugin tests
```

## PyCharm

- 使用 PyCharm 打开 `<agent-dev-root>/workspace`，不要把 `tests/` 单独作为项目打开。
- Python Interpreter 选择 `<agent-dev-root>/.venv/bin/python`。
- pytest Target 设为 `tests`，Working directory 设为 `<agent-dev-root>/workspace`。
- Additional Arguments 设为 `-q -p agentgov_testkit.pytest_plugin`。
- 可以把 `tests/` 标记为 Test Sources Root，但不能改变平台的固定发现规则。
- PyCharm 生成的 `.idea/` 必须保持为本机文件，重新打包前确认其未进入 `workspace/` 包。

## 连接 AgentGov

纯配置、hook 和静态资产测试不需要连接平台。使用 `agent` fixture 的行为测试需要在本地
pytest 或 PyCharm Run Configuration 中提供：

```text
AGENTGOV_API_BASE=<AgentGov API 地址>
AGENTGOV_AGENT_ID=<已导入的业务 Agent ID>
AGENTGOV_API_KEY=<启用 API 鉴权时提供>
AGENTGOV_COMMIT_SHA=<可选；省略时在 pytest session 开始时固定当前提交>
```

建议在 `workspace/` 上层创建仅供本机使用的 `.env.agent-test`：

```dotenv
AGENTGOV_API_BASE=replace-with-agentgov-api-base
AGENTGOV_AGENT_ID=replace-with-imported-agent-id
# AGENTGOV_API_KEY=
# AGENTGOV_COMMIT_SHA=
```

`agentgov_testkit` 只读取进程环境变量，不会隐式查找或加载 env 文件。本地命令行应在
`workspace/` 中显式加载：

```bash
chmod 600 ../.env.agent-test
set -a
source ../.env.agent-test
set +a
../.venv/bin/python -m pytest -q -p agentgov_testkit.pytest_plugin tests
```

PyCharm 中优先在仅本机保存的 Run Configuration 中导入
`<agent-dev-root>/.env.agent-test`；如当前版本不支持 env 文件，则在 `Environment variables`
中配置同样的变量。平台执行 pytest 时由测试运行器直接注入这些变量，不读取本地文件。

这些值只属于本机调试环境。不得在 `workspace/tests/` 中创建 `.env_agent_test`
或其他私有配置文件，也不得将配置写入测试代码、共享 PyCharm 配置或 Workspace 导入包。
