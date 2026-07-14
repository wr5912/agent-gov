# Runtime Template

本目录保存可复用的 Agent Runtime 初始配置模板，用于从零部署时填充运行态目录。

模板只保存结构、说明和安全默认值；真实环境里的 API key、token、Authorization header、数据库凭据、MCP 地址、IP、端口、URL、邮箱、账号、本机路径和 Claude 本地状态都不能进入模板。

## 使用方式

- 初始化运行态目录：`make runtime-bootstrap`
- 从当前运行态保存模板：`make runtime-volume-seeds-export`
- 清理运行态备份和模板临时产物：`make clean-runtime-artifacts`

`runtime-bootstrap` 通过 API 同款启动协调器执行。业务 Agent 只在整个 workspace 缺失时播种出生配置；已有 workspace 不逐文件补齐或覆盖。真实部署值应写入 `docker/.env`、部署环境变量或不提交的本地私有配置文件。

已从模板退役的托管文件登记在 `workspace-policy/retired-seed-assets.json`。容器启动只会删除内容 SHA256 与登记值完全一致的旧 seed 副本，并先在运行卷 `data/.retired-seed-assets/` 下生成私有备份和审计；用户修改内容会保留，符号链接、非普通文件或路径安全异常会阻断自动清理。

## 预置业务 Agent

- `main-agent`：默认安全运营样板业务 Agent。
- `response-disposal`：响应处置业务 Agent。
- `security-data-standardization-review`：安全数据标准化审查业务 Agent，审查原始安全数据到 OCSF、OCSF 到 STIX 的映射质量，并输出修正建议与回归用例。
- `ai-soc-gap-analyzer`：AI SOC 差距评估业务 Agent，基于能力模型和证据快照输出成熟度评分、差距、风险和下一步行动。
- `security-operations-expert`：网络安全运营专家业务 Agent，面向告警分流、事件调查、威胁狩猎和响应处置闭环；响应处置部分融合 `response-disposal` 的 MCP、skill、subagent、权限和审计配置。

> 业务 Agent 的权限只由 workspace `.claude/settings.json` 声明，`agent.yaml` 不保存第二份 HITL 布尔状态。通用业务 Agent 的 `Bash(*)` 基线放在 `ask`；流式 Web HITL 只可按本次 run 的低风险命令类别授权，高风险或未分类请求不得整轮放行。`security-operations-expert` 是受控例外：只有 RO 认证的 `approved_execution` 可对精确的 `mcp__sec-ops__soc_api__create` / `manual` 请求逐次 `allow_once`，`execute` 和其他 mutation 始终拒绝。非流式入口对权限询问 fail-closed；关闭 HITL 时流式入口也显式拒绝 ask 请求。

API 是共享运行卷的启动协调者；持久化 Agent job 队列和独立 worker 已退役。API 活跃期持有共享租约，bootstrap 与受管策略迁移持有独占租约。已有 workspace 只有在 Git 工作树干净、且没有未终结 change set 时才自动迁移，并为每个受影响 Agent 创建 Git 快照；脏工作树或开放 change set 会阻断启动。专用响应处置契约与通用 Bash/MCP/sandbox 基线共用这一策略入口，SDK 执行、配置编辑、候选发布和回滚也会 fail-closed 校验。

## 占位符

模板中的 `${...}` 是部署占位符，例如 `${MCP_SERVER_URL}`、`${SOC_API_URL}`、`${API_TOKEN}`、`${SERVICE_HOST}`、`${SERVICE_PORT}`。部署时按环境注入，不要把真实值提交回模板。

## 安全规则

保存模板会先进入 staging 目录，执行脱敏和校验，通过后才替换本目录。成功后会自动清理 staging、临时备份和旧式 `.bak-*` 文件；无法判断是否安全的内容会阻断导出，正式模板保持不变。
