# Runtime Template

本目录保存可复用的 Agent Runtime 初始配置模板，用于从零部署时填充运行态目录。

模板只保存结构、说明和安全默认值；真实环境里的 API key、token、Authorization header、数据库凭据、MCP 地址、IP、端口、URL、邮箱、账号、本机路径和 Claude 本地状态都不能进入模板。

## 使用方式

- 初始化运行态目录：`make runtime-bootstrap`
- 从当前运行态保存模板：`make runtime-volume-seeds-export`
- 清理运行态备份和模板临时产物：`make clean-runtime-artifacts`

`runtime-bootstrap` 默认只补齐缺失文件，不覆盖已有本地配置。真实部署值应写入 `docker/.env`、部署环境变量或不提交的本地私有配置文件。

## 预置业务 Agent

- `main-agent`：默认安全运营样板业务 Agent。
- `response-disposal`：响应处置业务 Agent。
- `security-data-standardization-review`：安全数据标准化审查业务 Agent，审查原始安全数据到 OCSF、OCSF 到 STIX 的映射质量，并输出修正建议与回归用例。
- `ai-soc-gap-analyzer`：AI SOC 差距评估业务 Agent，基于能力模型和证据快照输出成熟度评分、差距、风险和下一步行动。
- `security-operations-expert`：网络安全运营专家业务 Agent，面向告警分流、事件调查、威胁狩猎和响应处置闭环；响应处置部分融合 `response-disposal` 的 MCP、skill、subagent、权限和审计配置。

> 所有业务 Agent 的 Bash 由 `.claude/settings.json` 直接放行，不走 Web HITL；风险由 sandbox、PreToolUse hook、deny 规则和审计兜底。`response-disposal` 与 `security-operations-expert` 的 agent.yaml 声明 `requires_web_hitl: true`（部署契约，被运行时读取+强制）：其响应处置执行/入库（settings ask 层的 `mcp__soc-playbook-execution__*` / `mcp__soc-playbook-registry__*`）依赖 `ENABLE_CLAUDE_WEB_HITL=true` 的人审。关闭时运行时对这些 ask 型工具 fail-loud（流式明确报错、非流式不再 bypass 静默放行），启动日志告警，`GET /api/agent-registry` 的 `requires_web_hitl` 字段透出该要求。真实执行处置须在开启 HITL 的交互式会话进行。

## 占位符

模板中的 `${...}` 是部署占位符，例如 `${MCP_SERVER_URL}`、`${SOC_API_URL}`、`${API_TOKEN}`、`${SERVICE_HOST}`、`${SERVICE_PORT}`。部署时按环境注入，不要把真实值提交回模板。

## 安全规则

保存模板会先进入 staging 目录，执行脱敏和校验，通过后才替换本目录。成功后会自动清理 staging、临时备份和旧式 `.bak-*` 文件；无法判断是否安全的内容会阻断导出，正式模板保持不变。
