# Agent 测试套件

本目录由该业务 Agent 的开发者维护，是 Agent 行为测试的权威来源。平台固定执行：

```bash
python -m pytest -q -p agentgov_testkit.pytest_plugin tests
```

- `test_*.py` 必须直接位于本目录，不使用嵌套测试目录。
- 配置、hook、skill 或行为变化必须同步更新对应测试。
- 需要调用 Agent 时使用 `agent` fixture 或 `agentgov_testkit.invoke_agent()`。
- 测试不得安装依赖、修改平台命令或依赖修复前版本运行结果。
