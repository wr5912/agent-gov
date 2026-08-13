# Security Operations Expert Workspace

本目录是 `security-operations-expert` 的 Claude 原生 Workspace，也是该业务 Agent 角色、工具、
权限、流程和测试资产的权威来源。AgentGov 项目级 README、docs 和通用 skill 不复制这些专属契约。

## 核心边界

- Agent 仅处理已授权环境中的防御性安全运营材料，读取事实并完成研判与响应方案规划。
- Agent 不使用通用文件读取、shell、Web 或 MCP 工具；文件写入仅限专属输出目录。
- Agent 不产生 SOC 副作用；所有系统变更由平台在独立权限、审批和审计边界内完成。
- 平台只按注册表和路由确定运行归属，不根据 Agent ID 注入专用工具或授权逻辑。

## 资产入口

| 资产 | 职责 |
| --- | --- |
| `CLAUDE.md` | 角色、工作方式、输出和业务边界 |
| `agent.yaml` | Agent 能力、运行说明、Welcome Card 展示和审批责任声明 |
| `.mcp.json` | 空的 MCP 发现面；精确 capability manifest 与 P0-MCP 回执落地前保持禁用 |
| `.claude/settings.json` | Claude 原生权限、hooks 和 sandbox |
| `.claude/agents/` | 专属 subagents |
| `.claude/skills/` | 可复用业务流程 |
| `.claude/rules/`、`.claude/commands/` | 规则和显式命令入口 |
| `hooks/` | 工具调用前置防护、审计和会话初始化 |
| `tests/` | 该 Agent 的自测资产 |

开发者修改前先核对上述实际文件；运行中的 Agent 不读取原始配置，也不从项目级通用文档推断工具名、权限或处置步骤。

## 测试与发布

Agent 开发者负责维护 `tests/`。AgentGov 系统源码的 root pytest 与 `make test` 不收集本目录；
源码仓只能通过公共 `make container-workspace-pytest-test` 入口，在隔离容器内检出精确
`commit_sha` 并执行完整 `tests/`。原有或新增用例任一失败，都不能发布该 Agent 版本。

## 运行态更新

仓库中的本目录只用于初始化整体缺失的内置 Workspace，不覆盖已有运行态实例。已有实例通过
Workspace 导出、修改、导入、测试和版本发布流程更新。
