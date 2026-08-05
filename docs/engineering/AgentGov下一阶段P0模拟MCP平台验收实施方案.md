# AgentGov 下一阶段 P0 模拟 MCP 平台验收实施方案

> 文档状态：评审稿。
>
> 工程阶段说明：P0-MCP 是 P0 的阻断型平台验收工作包，不是网络安全业务 Agent 开发、
> 生产 MCP 建设或 P1/P3 领域测评范围。
>
> 上游方案：[P0 准入收口实施方案](./AgentGov下一阶段P0准入收口实施方案.md)。

## 1. 结论与目标

可以使用固定版本的
[`wr5912/openapi-mcp-server`](https://github.com/wr5912/openapi-mcp-server/tree/fb3d79c05cfaf70067a170af42a04503c619b688)
把固定版本
[`wr5912/mock_service`](https://github.com/wr5912/mock_service/tree/8ff758a9d4867325b83ef5dbff6025288aed62f7)
中的两个只读模拟接口发布为 MCP tools，供 `security-operations-expert` 完成 AgentGov 平台级
工具发现、调用、证据采集和可复现性验收。

该组合只按**一次性、隔离、合成的测试夹具**接入。它用于回答：

- AgentGov 能否在真实 Claude Runtime 会话中加载 Workspace MCP 配置；
- Agent 能否发现并调用限定工具；
- AgentGov 能否从 SDK/Agent 原生事实中投影参数、结果、顺序和最终回答；
- 固定依赖、固定 seed 和固定 Agent commit 能否得到可重复回执。

它不回答 MCP 认证、生产网络隔离、写操作审批、真实安全研判质量或业务 Agent 研发是否完成。
上游项目当前明确不含认证、tag 过滤、生产 URL allowlist 等能力，因此不能直接作为生产安全
边界；这一限制只因本方案使用内部合成数据和测试专网而可接受。事实依据见
[固定提交 README](https://github.com/wr5912/openapi-mcp-server/blob/fb3d79c05cfaf70067a170af42a04503c619b688/README.md)
和
[MCP Resource 范围说明](https://github.com/wr5912/openapi-mcp-server/blob/fb3d79c05cfaf70067a170af42a04503c619b688/docs/openapi-mcp-resource-sketch.md)。

### 1.1 本回执的精确 capability tuple

P0-MCP 只对以下精确组合作出肯定结论：

| 维度 | 本次覆盖值 |
| --- | --- |
| Runtime | `claude-code` 受管 Runtime |
| MCP transport | Streamable HTTP |
| MCP surface | `tools/list` + `tools/call` |
| Operation | 过滤 spec 中的两个 GET-only/read-only tool |
| Authentication | `not-tested` / no-auth 合成夹具 |
| Data/network | 合成数据 + 隔离 fixture network |
| Evidence | SDK/Agent 原生 tool facts 与 AgentGov canonical 投影 |

因此回执中的能力标识必须等价表达为
`claude-code / streamable-http / tools / GET-read-only / no-auth-fixture`，不得缩写为
“MCP 已验证”或“生产工具能力已就绪”。

以下能力在 P0-MCP 退出后仍保持 `GAP`：认证与授权、凭据/header 传递、多组织隔离、
写操作与人工审批、resources/templates 消费、生产 endpoint/allowlist、第二 Runtime、
动态安全场景和真实业务质量。空 resources/templates 只是本次工具面收缩断言，不是对该
能力的支持证据。

## 2. 实际问题、替代方案与退出条件

### 2.1 实际问题

P0 当前有两类互不替代的 GAP：

1. Workspace 29 个静态测试中 14 个失败，需要按安全缺陷、环境缺陷和陈测分别收口；
2. 即使静态测试全绿，仍缺一条对第 1.1 节 capability tuple 的“真实 Runtime
   加载 MCP → Agent 调用 → AgentGov 采集原生证据”平台验收。

模拟 MCP 只关闭第二类 GAP，不能抵消第一类失败，也不能把静态测试的红灯改判为通过。

### 2.2 本期选择

- 使用上游原始实现，不建设面向生产的加固分支；
- 在平台拥有的过滤后 OpenAPI 中只保留两个 GET operation；
- 仅启动 `mock_service` 的 FastAPI 合成数据核心；
- 使用单独 Compose project、临时数据根和内部网络执行；
- 将 P0-MCP 回执设为 P0 阶段阻断证据，但不并入离线 `main-full`。

未选择“先建设生产 MCP 网关”是因为当前目标只是验证 AgentGov 平台能力，认证、多租户、
写操作审批和生产 allowlist 没有真实业务需求支撑。未选择直接发布 `mock_service` 完整
OpenAPI，是因为上游 MCP 原始实现不提供方法或 tag 过滤，完整 spec 会扩大工具面。

### 2.3 退出或重审条件

出现以下任一需求时，停止沿用本夹具并转入 P3 独立评审：

- 使用真实凭据、客户数据、内网 endpoint 或生产系统；
- 暴露 POST、PUT、PATCH、DELETE 或其他有副作用的 operation；
- 声明或验证 MCP 认证、授权、多租户、审计合规或生产可用性；
- 评估真实告警研判、多跳调查、高风险动作审批或动态 Cyber Range；
- 上游固定提交出现不可接受的供应链、安全或许可证风险。

## 3. 治理对象与资产归属

| 维度 | 裁决 |
| --- | --- |
| 被验对象 | AgentGov Runtime、Workspace MCP 加载、SDK/Agent 原生 tool facts 和证据投影 |
| 测试载体 | 经 P0-W1 绿测后固定 commit 的 `security-operations-expert` |
| 外部夹具 | 固定 commit 的 OpenAPI MCP 转换器和模拟 HTTP 服务 |
| 治理执行者 | 确定性 fixture 编排器、MCP 协议断言、AgentGov live 验收和阶段评审 |
| 数据/证据资产 | tool list、tool calls、参数、结果摘要、Agent 活动、最终回答和验收回执 |
| 方法论资产 | capability tuple、精确工具面、超界拒绝、回执与清理规程 |
| 执行资产 | 过滤 OpenAPI、Compose fixture、固定镜像、测试 prompt 和验收脚本 |
| 横切治理维度 | Agent/upstream commit、suite/image/OpenAPI digest、provenance、scope、审计时间、cleanup 结果与夹具生命周期 |
| 能力回执 | `claude-code / streamable-http / tools / GET-read-only / no-auth-fixture` |
| 不归属内容 | 生产 MCP、真实安全数据、业务 Agent 领域能力、P1 Scorecard、P3 动态场景及第 1.1 节所列 GAP |

`security-operations-expert` 在本工作包中只是受版本约束的测试载体。失败应先归因到平台、
Workspace 配置、外部夹具或模型行为，不能默认转成安全业务 Agent 的产品需求。

## 4. 固定依赖与供应链边界

| 依赖 | 固定值 | 允许用途 |
| --- | --- | --- |
| `openapi-mcp-server` | commit `fb3d79c05cfaf70067a170af42a04503c619b688` | 将过滤 spec 动态暴露为 Streamable HTTP MCP |
| `mock_service` | commit `8ff758a9d4867325b83ef5dbff6025288aed62f7` | 生成可复现的合成 alerts/assets |
| Agent Workspace | P0-W1 通过后锁定的精确 commit | 执行 AgentGov live MCP 验收 |
| OpenAPI | 平台维护的静态过滤 artifact + SHA256 | 只描述两个 GET operation |

执行约束：

- 不使用分支名、`latest` tag 或运行时 `git pull`；
- 不把两个外部仓库 vendoring 到 AgentGov 源码，也不在其上维护私有业务功能；
- 准备阶段从固定提交构建内部 fixture image，写入 OCI revision label 并记录 image digest；
- 阶段验收只消费已固定 digest 的镜像，不在验收过程中联网拉取源码或重建浮动依赖；
- `mock_service` 仅安装并运行 FastAPI 核心，跳过 Git LFS 数据、Elasticsearch、Kibana、
  独立威胁情报服务和 DeepSeek 场景生成，不设置 `DEEPSEEK_API_KEY`。

`mock_service` 的固定 seed 能生成稳定合成数据，且普通列表接口不依赖外部网络，见
[固定提交 README](https://github.com/wr5912/mock_service/blob/8ff758a9d4867325b83ef5dbff6025288aed62f7/README.md)。

## 5. 受控拓扑与网络

```text
acceptance-runner
  -> 临时 AgentGov API / Claude Runtime
       -> 固定 commit 的 security-operations-expert
            -> sec-ops HTTP MCP
                 -> 固定镜像的 openapi-mcp-server
                      -> test-only filtered OpenAPI
                           -> 固定镜像的 mock_service（count=3, seed=7）
```

环境约束：

- 使用独立 Compose project name，不能加入日常 AgentGov Compose project；
- AgentGov runtime root、SQLite、Claude 配置根和 MCP SQLite 均为本次运行临时资产；
- 不挂载 `${HOME}/volume-agent-gov`，不读取或回灌 live Workspace；
- MCP SQLite 使用 `tmpfs`；其余临时卷或目录在验收结束时一并删除；
- MCP、mock service 和静态 spec server 不映射宿主机端口，只加入 fixture internal network；
- AgentGov API 同时加入既有模型访问网络与 fixture network；MCP、mock 和 spec server 不能加入
  模型网络；
- Workspace 通过内部 DNS 获得 `SEC_OPS_MCP_URL`；
- 可注入仅服务 Workspace 配置解析的临时 `SEC_OPS_MCP_TOKEN`，但上游原始 MCP 不校验该 token，
  因此回执必须标明“认证未测试”，不能把变量存在误写为认证通过；
- 不写入真实 key、MCP header、私有 endpoint、运行态 SQLite 或原始敏感日志。

Claude Code 支持项目级 `.mcp.json`、环境变量展开和 HTTP MCP；验收必须使用 Runtime 当前已批准的
原生发现与权限流程，后端不按 Agent ID 接管 MCP。配置依据见
[Claude Code MCP 文档](https://code.claude.com/docs/en/mcp)。

## 6. 过滤 OpenAPI 契约

平台维护一份测试专用 OpenAPI artifact，MCP resource 名固定为 `soc-api`，Workspace 现有
`.mcp.json` server key 保持 `sec-ops`，只包含：

| 方法 | 路径 | 参数 | MCP `tools/list` 名称 | Claude Runtime 名称 |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/alerts` | `count`、`seed` | `soc_api__list_alerts_api_v1_alerts_get` | `mcp__sec-ops__soc_api__list_alerts_api_v1_alerts_get` |
| GET | `/api/v1/assets` | `count`、`seed` | `soc_api__list_assets_api_v1_assets_get` | `mcp__sec-ops__soc_api__list_assets_api_v1_assets_get` |

过滤 artifact 的 `servers` 只指向 fixture network 内的 `mock_service`。禁止直接把上游完整
`/openapi.json` 注册到 MCP；验收必须对 artifact SHA256 和 `tools/list` 同时断言，防止 spec
漂移或动态 registry 意外扩大工具面。

以下能力必须为空：

- POST、PUT、PATCH、DELETE、OPTIONS、HEAD、TRACE tools；
- MCP resources；
- MCP resource templates；
- 未列入表格的第三个 tool。

## 7. 工作包

### 7.1 P0-MCP-W1：Fixture 准备

- 从两个固定 commit 构建内部镜像；
- 生成只含两个 GET operation 的 OpenAPI artifact；
- 固定资源名、operationId、镜像 digest 和 artifact SHA256；
- 在临时 MCP 管理 API 中创建并启用 `soc-api`；
- 等待 mock、MCP 和 AgentGov 健康检查，不以固定 sleep 代替 readiness。

### 7.2 P0-MCP-W2：直接协议验收

由独立 runner 对 MCP 执行：

1. `initialize`；
2. `tools/list`；
3. 分别对两个 tool 执行 `tools/call`，参数固定为 `{"count": 3, "seed": 7}`；
4. `resources/list` 与 `resources/templates/list`。

成功标准：

- tool 集合与第 6 节两个名字精确相等；
- 两次调用均返回 3 条记录；
- alerts 至少包含稳定标记 `alert-0001`；
- assets 至少包含 `asset-0001` 和 `db-core-01`；
- resources 与 templates 均为空；
- 请求没有越过 fixture network。

### 7.3 P0-MCP-W3：AgentGov Live 验收

- 通过 `/api/agent-test-sessions` 创建并固定测试载体的 Agent commit；
- prompt 明确要求分别查询 3 条 alerts 和 3 条 assets，参数均为 seed `7`，再基于结果给出摘要；
- 只允许两个 MCP tools，最大 tool call 数为 4；
- 等待 run 终态，再从 AgentGov 对外证据投影读取 `agent_activity` 和最终回答；
- 如需诊断，原始事实只从 SDK/Agent 原生 session/tool 消息读取，不解析 CLI transcript。

成功标准：

- `agent_activity` 同时出现第 6 节两个精确 Claude Runtime tool name；
- 每个 tool 的参数均为 `count=3`、`seed=7`；
- tool result 与直接协议验收的稳定标记一致；
- 最终回答包含 `alert-0001`、`asset-0001`、`db-core-01`；
- 没有 Bash、文件写入、未批准工具、虚构 tool result 或敏感内容；
- Agent commit、Runtime session、run 和活动证据可相互关联。

模型未按要求调用工具、调用超限或答案缺少稳定标记均按失败处理，不能重试到偶然通过后只保留
最后一次结果；每次尝试都必须有独立 run 和回执。

### 7.4 P0-MCP-W4：回执与清理

每次验收生成机器可读回执，至少包含：

- AgentGov source/worktree fingerprint；
- Agent ID、精确 commit、suite digest；
- 两个外部 source commit、OCI revision 和 image digest；
- 过滤 OpenAPI SHA256、资源名和 seed；
- 精确 capability tuple 以及未覆盖 `GAP` 清单；
- 实际 tool list、协议调用摘要、AgentGov run/session/trace 引用；
- 每项断言结果、总结果、开始/结束时间；
- Compose project、临时资产清单和 cleanup 结果。

回执不得包含 token、headers、私有 endpoint、完整环境变量、原始 secret 或运行态数据库。

无论成功、失败还是中断，runner 都必须执行等价于
`docker compose down -v --remove-orphans` 的清理并删除明确解析出的临时目录。清理失败使整次验收
失败；不得使用宽目录、未解析变量或 glob 作为删除目标。

## 8. 测试与质量策略

| 层级 | 断言 | Lane |
| --- | --- | --- |
| Fixture contract | 固定提交、镜像 digest、过滤 spec、精确两 tools | hermetic contract |
| MCP protocol | initialize/list/call、空 resources/templates、稳定数据 | container fixture |
| AgentGov live | 固定 Agent commit、双 tool activity、最终标记、无越界行为 | container live acceptance |
| Cleanup | project、volume、network、临时目录均已清除 | blocking teardown |

实施时新增公共入口 `make container-security-mcp-test`，并在
`tests/quality_policy.json` 中登记 owner、capability、resource class、lane 和阶段门。该入口：

- 是 P0 阶段阻断验收；
- 不进入离线 `main-full`，也不由宿主机单测替代；
- 被显式调用后严格失败：模型凭据、固定镜像、fixture 或 Docker 缺失时均不得 skip；
- 必须基于当前 AgentGov 工作树重建 AgentGov 镜像并 force-recreate；
- 不创建 `ImprovementItem`、`AgentTestRun` 发布证据、产品 API、UI 或数据库迁移。

## 9. P0-MCP 退出门

P0-MCP 只有同时满足以下条件才通过：

1. 两个外部依赖和 Agent Workspace 均被精确版本固定；
2. 直接协议验收全部通过；
3. AgentGov live 验收全部通过；
4. 回执字段完整且不含敏感信息；
5. cleanup 成功且宿主机没有遗留端口、容器、网络、卷或临时运行根；
6. `make codex-guard`、相关 fixture contract 测试和公共真实容器入口通过。

本回执证明的是“AgentGov 在第 1.1 节精确 capability tuple 下可完成 MCP 工具闭环”，
未覆盖能力继续保持 `GAP`；它不证明 P1 的 8 个静态案例、
P3 动态安全 MVP、生产 MCP 或该安全 Agent 的业务能力已经通过。
