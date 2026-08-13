# Agent 测试套件

本目录由该业务 Agent 的开发者维护，是 Agent 行为测试的权威来源。

在 AgentGov 源码仓库中，本目录不进入 root pytest collection，也不得为了平台门禁把测试正文
复制到根 `tests/`。导出或导入后的运行态 Workspace 仍以自身 Git 中的本目录为准。

## 平台执行契约

平台在无网络、无凭据、只读源码的 exact-commit sandbox 中，以 `workspace/` 为工作目录固定执行：

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

## 唯一执行入口

- 在 AgentGov 源码仓中，只能通过公共 `make container-workspace-pytest-test` 入口验证当前工作树。
- 发布候选由平台绑定完整 `commit_sha`，在独立 sandbox 中执行完整 suite 并生成 typed receipt。
- 开发者不得直接从源码仓或 live Workspace 用宿主 Python 导入本目录，也不得向测试注入 live
  volume、凭据、私有配置或宿主路径。
- 需要调用 Agent 的行为测试只使用平台提供的 `agent` fixture；隔离 runner 注入最小、固定、
  非敏感环境，测试资产不能自行加载 env 文件。
