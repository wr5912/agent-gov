# Runtime Template

本目录保存可复用的 Agent Runtime 初始配置模板，用于从零部署时填充运行态目录。

本目录是 repo-tracked 的 generic template 与声明 seed/builtin，不是 live workspace。真实
API key、token、Authorization/MCP header、数据库凭据、本机私有路径和 Claude 本地状态都
不能进入本目录。generic template 的环境绑定 MCP 地址、IP、端口和 URL 必须使用部署环境变量引用；
声明 seed 中的非秘密真实 endpoint、内网地址等值会被扫描器提示复核，但可由提交者明确保留。
该仓库边界不限制 live workspace 原样保存业务运行配置。

## 使用方式

- 初始化运行态目录：`make runtime-bootstrap`
- 校验准备提交的 repo seed/template：`make runtime-volume-seeds-scan`
- 清理运行态备份和模板临时产物：`make clean-runtime-artifacts`

`runtime-bootstrap` 通过 API 同款启动协调器执行。业务 Agent 只在整个 workspace 缺失时播种出生配置；已有 workspace 不逐文件补齐或覆盖。真实部署值应写入 `docker/.env`、部署环境变量或不提交的本地私有配置文件。

live workspace 由产品 workspace 导出 API 按精确 Git commit 原样导出，不通过本目录的脚本从
整个 runtime volume 反向生成模板。声明 seed 的归档先在仓库外保留逐字节候选，再由提交者人工
选择 `workspace/` 内容：真实密钥、MCP 私有 header、数据库凭据和本机私有路径必须删除或改为
私有环境注入；非秘密 endpoint、内网地址和较宽权限只提示复核，可在确认后原样保留。写入本目录后
运行 `make runtime-volume-seeds-scan`；流程见
`docs/业务Agent工作区资产闭环产品工程方案.md`。

## 预置业务 Agent

- `main-agent`：默认安全运营样板业务 Agent。
- `response-disposal`：响应处置业务 Agent。
- `security-data-standardization-review`：安全数据标准化审查业务 Agent，审查原始安全数据到 OCSF、OCSF 到 STIX 的映射质量，并输出修正建议与回归用例。
- `ai-soc-gap-analyzer`：AI SOC 差距评估业务 Agent，基于能力模型和证据快照输出成熟度评分、差距、风险和下一步行动。
- `security-operations-expert`：网络安全运营旗舰样板，面向告警分流、事件调查、威胁狩猎和响应处置闭环；可按字节导出并以任意 Agent ID 导入迭代，来源 ID 不构成运行准入条件。

> 业务 Agent 的权限只由 workspace `.claude/settings.json` 声明，`agent.yaml` 不保存第二份 HITL 布尔状态。通用业务 Agent 的 `Bash(*)` 基线放在 `ask`；流式 Web HITL 只可按本次 run 的低风险命令类别授权，高风险或未分类请求不得整轮放行。`security-operations-expert` 也使用同一原生机制：精确的 `mcp__sec-ops__soc_api__create` / `manual` 逐次 `allow_once` 或拒绝，输入不可修改，`execute` 和其他 mutation 始终拒绝；后端不按 Agent ID 或 seed 来源增加授权锁。非流式入口对权限询问 fail-closed；关闭 HITL 时流式入口也显式拒绝 ask 请求。

API 是共享运行卷的启动协调者；持久化 Agent job 队列和独立 worker 已退役。bootstrap、
缺失 workspace 播种和 receipt 更新持有独占租约。启动只读校验已有 workspace 中的 JSON、
MCP endpoint 形态和被 settings 引用的 hook 文件；不会按 seed 回灌、修复或生成隐式 Git commit。
已有 workspace 的业务配置由该 Agent 自己的 per-Agent Git 和版本治理负责。

## 原生环境变量引用

seed `.mcp.json` 中的 `${MCP_SERVER_URL}`、`${SEC_OPS_MCP_URL}` 等引用由 Claude Code
子进程使用 Runtime 传入的完整环境原生解析。AgentGov 不在落盘前替换这些值，container 与
local-debug 播种得到相同文件字节。真实秘密仍只放私有 env；不要提交回 seed。声明 seed 的
权限配置是该 seed 自身资产，实例化时原样保留；generic template 的保守权限不覆盖它。

## 安全规则

seed 扫描只读检查准备提交的仓库内容，不自动脱敏或改写。高风险秘密会令扫描失败；环境绑定值和
权限警告由提交者在 PR 中明确确认或调整。`make runtime-volume-seeds-clean` 只清理由已退役旧工具
留下的 staging、备份和 `.bak-*` 产物，不是 seed 导出或恢复入口。
