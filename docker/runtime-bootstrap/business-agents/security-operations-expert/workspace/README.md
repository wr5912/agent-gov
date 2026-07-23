# Security Operations Expert Workspace

本目录是 `security-operations-expert` 的 Claude 原生 Workspace，也是该业务 Agent 角色、工具、
权限、流程和测试资产的权威来源。AgentGov 项目级 README、docs 和通用 skill 不复制这些专属契约。

## 核心边界

- Agent 读取安全运营事实，完成分析、剧本筛选、生成和修订。
- Agent 不直接执行 SOC 副作用工具；确认、保存、执行和监控边界以当前 Workspace 配置为准。
- 平台只按注册表和路由确定运行归属，不根据 Agent ID 注入专用工具或授权逻辑。

## 资产入口

| 资产 | 职责 |
| --- | --- |
| `CLAUDE.md` | 角色、工作方式、输出和业务边界 |
| `agent.yaml` | Agent 能力、运行说明、Welcome Card 展示和审批责任声明 |
| `.mcp.json` | MCP 服务接入 |
| `.claude/settings.json` | Claude 原生权限、hooks 和 sandbox |
| `.claude/agents/` | 专属 subagents |
| `.claude/skills/` | 可复用业务流程 |
| `.claude/rules/`、`.claude/commands/` | 规则和显式命令入口 |
| `hooks/` | 工具调用前置防护、审计和会话初始化 |
| `tests/` | 该 Agent 的自测资产 |

修改前先读取上述实际文件，不从项目级通用文档推断本 Agent 的工具名、权限或处置步骤。

## 测试与发布

Agent 开发者负责维护 `tests/`。从 Workspace 根目录执行：

```bash
python -m pytest -q -p agentgov_testkit.pytest_plugin tests
```

AgentGov 系统源码的 `make test` 不收集本目录。平台在测试待发布 Agent 版本时，会检出精确
`commit_sha` 并执行完整 `tests/`；原有或新增用例任一失败，都不能发布该 Agent 版本。

## 运行态更新

仓库中的本目录只用于初始化整体缺失的内置 Workspace，不覆盖已有运行态实例。已有实例通过
Workspace 导出、修改、导入、测试和版本发布流程更新。
