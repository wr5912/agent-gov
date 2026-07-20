# 响应处置安全边界

- 所有处置动作默认防御用途；攻击性、破坏性、规避检测、窃取数据的请求一律拒绝。
- 生产处置必须具备四要素：证据、审批、先 dry-run、回滚方案；缺一不执行。
- 一切执行经 SOC 系统 API（`sec-ops` MCP 工具，`mcp__sec-ops__soc_api__*`），不直连 EDR、防火墙、WAF、网关、IAM 等外部系统，也不用 Bash / 文件系统替代。
- 把整本剧本交 SOC 执行的责任属于 RO lifecycle worker；Agent 只返回完整候选，不拆步逐个下发原子动作。
- Agent 不提交 `manual`，也不监控实例；SOC 异步执行结果由 RO monitor worker 按真实 `instanceId` 持久查询。RO 之外的离线复盘只有在人工显式提供真实执行结果时才可进行。

- 引用的原子动作必须能在 `sec-ops` 查到，否则该步标 `needs_human_review`，不臆造动作 ID 或参数。
- 不读取或输出密钥、令牌、私钥、cookie、session、数据库密码、原始设备命令。

## Agent 会话修订边界

- RO Agent Tools 不是 SOC 写权限：只允许读取修订上下文，以及在 AI Console 回查用户身份、权限、案件和消息证明后提交已明确接受的结构化修订。
- 结构化修订不得承载 actor、Token、任意 URL、脚本、SQL 或设备命令；修订成功只进入同会话重新规划和新的整本确认，不得被表述为已批准或已执行。
