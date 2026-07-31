# AgentGov 下一阶段实施方案索引

> 文档状态：评审稿。
>
> 评审基准：2026-07-30，仓库版本 `3.0.3`。
>
> 文档角色：连接长期产品目标、三份产品能力目标方案与下一轮工程实施的唯一阶段索引。
> 本文和下列阶段方案不表示对应能力已经进入当前 OpenAPI、数据库、UI 或运行态。

## 1. 阶段结论

下一阶段采用 **Conditional Go**：

- 先完成 P0 准入收口，包括 29/29 Workspace 静态绿测和独立 P0-MCP 平台回执；
- 以 P1 网络安全测评纵向闭环作为业务证据主线；
- P1 通过后，P2A Runtime 中立核心与 P2B Governor 受控学习基础可以并行；
- P3 只定义规模化扩展的启动门，不在本轮提前接入第二 Runtime、外部 CLI、联网研究或
  Multica。

不采用三条路线同时全面铺开的方案。当前平台底座已经足以支撑窄纵切片，但旗舰业务 Agent
测评基线、跨层 Runtime 契约和 Governor 二阶治理证据仍未同时达到规模化建设条件。

## 2. 权威关系

| 文档 | 角色 | 本索引如何使用 |
| --- | --- | --- |
| [项目目标愿景使命](./项目目标愿景使命.md) | 长期产品权威 | 固定 `Agent Runtime · Feedback Loop · Version Governance` 核心定位 |
| [AgentGov 核心功能测试用例](./AgentGov核心功能测试用例.md) | 长期验收锚点 | 每个阶段必须绑定并增强具体 AGV 用例 |
| [网络安全智能体测评工程需求](./网络安全智能体测评工程需求文档.md) | 产品能力目标方案 | 定义安全测评目标、对象、评分、安全门和完整 MVP |
| [Governor 自研究与受控自学习能力需求](./Governor自研究与受控自学习能力需求.md) | 产品能力目标方案 | 定义 Governor 二阶治理闭环与安全边界 |
| [多 Runtime 适配、外部 CLI 旁路与 Multica 协作边界方案](./engineering/多Runtime适配与外部CLI旁路及Multica协作边界方案.md) | 目标架构方案 | 定义 Runtime 长期边界和迁移方向 |
| [反馈闭环当前实现基线](./反馈闭环当前实现基线.md) | 当前实现基线 | 解释当前代码、API、存储、四阶段流程和发布门 |
| 本索引及下列阶段方案 | 当前实施评审入口 | 决定下一轮先做什么、暂不做什么以及如何验收 |
| [P0 模拟 MCP 平台验收实施方案](./engineering/AgentGov下一阶段P0模拟MCP平台验收实施方案.md) | P0 配套工程方案 | 只定义隔离合成夹具、真实 Runtime 工具闭环和阶段回执 |

三份目标方案继续 `keep`，不合并、不归档；阶段方案只把已选切片落实为可执行边界，不复制其
完整长期正文。已归档的旧《AgentGov 目标达成分阶段执行计划》继续只承担历史审计价值，
不恢复为活跃入口。

## 3. 当前准入证据

截至评审基准日：

- `make codex-guard`、`make typecheck`、`make main-flow-test` 通过；
- 根质量策略能收集 1351 个 pytest leaf，但当前 collection 不包含内置
  `security-operations-expert` Workspace 测试；
- 该 Workspace 的首轮完整独立测试为 15 通过、14 失败：6 个危险 Bash 未 deny、3 个畸形输入
  契约、1 个审计 fallback 路径错误、4 个原生配置/身份陈测；
- 静态测试之外，当前还没有“真实 Runtime 加载模拟 MCP → Agent 调用两个限定 GET tools →
  AgentGov 投影原生 tool facts → 清理”的可复现阶段回执；
- 核心功能测试用例为 28 个 `current`、22 个 `gap`，其中 AGV-002、AGV-043、AGV-045
  分别对应端到端治理链、内置业务 Agent 闭环和跨 Agent 方法评估缺口；
- `ClaudeRuntime`、流式 Runtime 和 Governor 中心服务已接近 800 行架构阈值，新增职责必须进入
  独立子域，不能继续在中心文件中堆叠分支；
- `sdk_session_id` 已跨越 DB、OpenAPI、SSE、前端和测试，当前不具备低风险局部改名条件；
- 当前 Governor 有受控生成、Trace、变更和发布基础，但没有不可变学习证据、能力版本、
  独立评估和可信人工启用链。

这些证据支持“开始阶段工程”，不支持“宣布三项长期能力已经就绪”。

## 4. 治理对象矩阵

| 维度 | 网络安全测评 | Runtime 中立化 | Governor 受控学习 |
| --- | --- | --- | --- |
| 被治理对象 | `security-operations-expert` 精确 Git commit 及其场景表现 | Agent 运行、会话、原生事实和能力覆盖 | Governor 的归因方法、能力版本和实际治理效果 |
| 治理执行者 | SDK/Agent 事实源、确定性评分器、安全门、人工评审、发布门 | Runtime adapter、后端 registry/gateway、契约测试 | Governor 候选生成、隔离评估器、后端门禁、授权人员 |
| 数据资产 | run、trace、case result、scorecard、violation | native event、canonical event、coverage、session ref | 原始产物、人工修订 diff、评估结果、启用与回退记录 |
| 方法论资产 | 评分规程、安全门槛、评测协议 | capability 语义、降级和生命周期规则 | 归因方法、适用条件、反证和淘汰规则 |
| 执行资产 | Workspace pytest、case、fixture、Ground Truth | Runtime 原生包、adapter、contract suite | prompt、Signature、OutputModel、方法候选、冻结评估包 |
| 版本资产 | 业务 Agent commit、suite/protocol digest、Release | Runtime kind、adapter/CLI 版本区间 | capability version、method revision、active pointer |
| 当前边界 | 测试运行和发布门已存在，领域测评资产未落地 | Claude 原生实现成熟，中立入口未收口 | 事项级执行已存在，二阶学习闭环未实现 |
| 本轮目标 | 形成首条可复现安全测评到发布证据 | 建立 Claude-only 中立内部边界 | 形成不改变线上行为的 shadow 学习证据 |

P0-MCP 不改变上表的业务治理对象。其被验对象是 AgentGov Runtime/MCP/证据投影，固定 commit 的
`security-operations-expert` 只是测试载体，两个外部仓库只是可丢弃夹具。该回执不属于 Agent
领域能力、P1 Scorecard 或生产 MCP 版本资产。

## 5. 总体闭环与依赖

```text
P0 准入收口
  ├─ Workspace 29/29 静态绿测
  └─ 隔离 P0-MCP：两只读 tools + AgentGov live facts + cleanup 回执
  ↓
P1 安全静态测评 → 失败进入 ImprovementItem → 候选 commit → 完整测试 → 精确发布
  ↓
  ├─ P2A Runtime 中立核心 + Claude 委托 adapter
  └─ P2B Governor 不可变证据 + 方法候选 + 隔离评估（shadow）
       ↓
P3 规模化扩展准入：完整安全 MVP / Runtime 公共迁移 / Governor 人工启用
```

P2A 与 P2B 只在 P1 产出至少一条真实闭环证据后并行。P2B 的账本、候选和评估开发可以与
P2A 并行，但接入统一 `ManagedExecutionDriver` 和阶段退出必须等待 P2A gateway 就绪。P3 中的
每条扩展线单独满足启动门后才能实施，不能用另一条线的完成状态代替自身准入。

## 6. 阶段方案

| 阶段 | 方案 | 核心产物 | 退出结果 |
| --- | --- | --- | --- |
| P0 | [准入收口实施方案](./engineering/AgentGov下一阶段P0准入收口实施方案.md) | 安全 Workspace 绿测、质量策略可见性、Runtime 生命周期裁决 | 形成可信开发基线 |
| P0-MCP 配套 | [模拟 MCP 平台验收实施方案](./engineering/AgentGov下一阶段P0模拟MCP平台验收实施方案.md) | 固定上游、过滤 OpenAPI、直接协议与 AgentGov live 回执 | 关闭平台 MCP 工具闭环 GAP，不产生领域结论 |
| P1 | [网络安全测评纵向闭环实施方案](./engineering/AgentGov下一阶段P1网络安全测评纵向闭环实施方案.md) | 8 个静态案例、typed Scorecard/Violation、改进事项闭环 | 首条业务证据闭环 |
| P2A | [Runtime 中立核心与 Claude Adapter 实施方案](./engineering/AgentGov下一阶段P2ARuntime中立核心与ClaudeAdapter实施方案.md) | 小端口、registry、gateway、Claude 委托 adapter | 内部依赖中立、外部行为等价 |
| P2B | [Governor 受控学习基础实施方案](./engineering/AgentGov下一阶段P2BGovernor受控学习基础实施方案.md) | 不可变证据、能力版本、方法候选、隔离评估 | 候选可评估但不自动生效 |
| P3 | [扩展准入实施方案](./engineering/AgentGov下一阶段P3扩展准入实施方案.md) | 各扩展线的独立启动门、迁移和回退要求 | 决定哪些能力进入后续实施 |

## 7. 已选实施裁决

1. **P0 双门独立收口**：Workspace 29/29 和 P0-MCP 回执分别阻断，不能相互抵消。
2. **模拟 MCP 只验平台**：固定原始上游提交，只暴露两个 GET tools；不验证认证、生产安全或
   业务 Agent 研发。
3. **业务证据先行**：先证明安全 Agent 的测评—改进—版本闭环，再扩展 Runtime 和 Governor。
4. **首切片只做静态 L2**：P1 的 8 个高密度案例固定 `allowed_tools=[]`、精确 commit；
   P0-MCP 不把它升级为动态工具测评，也不把 8 个案例称为完整 MVP。
5. **测试资产只在 Workspace Git**：测试正文、case、fixture 和 Ground Truth 不进入数据库副本。
6. **复用 `AgentTestRun`**：增加 typed 评测投影，不恢复已删除的 `TestDataset/EvalRun`。
7. **安全门独立否决**：高综合分不能抵消工具越界、虚构证据、敏感泄露或高风险动作越权。
8. **一次 run 不重开**：`interrupted` 是该 run 的终态；恢复同一 Runtime session 创建新 `run_id`。
9. **Runtime 首期仅 `claude-code`**：部署级选择，禁止客户端逐请求切换；公共
   `sdk_session_id` 暂不迁移。
10. **Governor 候选默认不生效**：P2B 只产出 shadow 证据；联网研究、自修改和全局自动传播禁用。
11. **四阶段工作台保持四阶段**：测评失败只通过真实业务动作进入改进事项；Governor 元治理不成为
   第五个用户阶段。
12. **Multica 不是前置依赖**：只有通用 API 和只读旁路不能满足两个以上真实场景时才另立评审。

## 8. 全局字段所有权

| 所有者 | 字段或语义 | 约束 |
| --- | --- | --- |
| Backend-owned | ID、Agent/commit/runtime/session 引用、状态、时间、trace、suite/protocol digest、评分结果、门禁、认证 principal、provenance | 不进入 Agent/Governor 输出要求；外部污染值必须忽略或覆盖 |
| Agent/Governor-owned | 回答内容、分析结论、业务假设、风险说明、方法正文、适用条件和反证 | 必须经 typed OutputModel 或明确的原生文本边界进入后端 |
| Workspace/test-owned | case 语义、Ground Truth、确定性断言、允许/禁止行为 | 随 Workspace Git 版本化，不能被运行请求改写 |
| Boundary-owned | SQLite JSON、HTTP、SSE、文件报告、日志、Langfuse metadata | 只在边界序列化；内部主流程传递 typed model |

## 9. 全局环境与数据边界

- API 容器继续选择完整的 `docker/.env`；自动化可由 `COMPOSE_ENV_FILE` 选择另一份完整 env，
  不使用 layered override 术语。
- 宿主机 Python/PyCharm 继续选择 `docker/.env.local-debug`；其结果不能声明为容器验收。
- Vite 只使用 `frontend/.env.local`，不增加独立 Runtime 选择器。
- 容器持久化根继续为 `${HOME}/volume-agent-gov`；P0、P1、P2A、P2B 均不改变卷布局。
- P0-MCP 是上述卷规则之外的隔离验收：使用独立 Compose project 和临时 Runtime/MCP 数据，
  明确不得挂载 `${HOME}/volume-agent-gov`，结束后必须清理。
- 必需闭环保持离线可用；外部研究、Multica 和远程服务不得成为通过条件。
- 真实 key、MCP header、私有 endpoint、运行态 SQLite 和敏感样本不得进入实施文档、源码或测试
  fixture。

## 10. 文档动作与维护

| 对象 | 动作 | 原因 |
| --- | --- | --- |
| 三份产品能力目标方案 | `keep` | 继续定义长期目标和边界 |
| 当前实现基线与核心功能测试用例 | `keep` | 继续提供当前事实和验收锚点 |
| 本索引、五份阶段主方案及一份 P0 配套方案 | 新增活跃入口 | 提供本轮可执行节奏和阶段硬门 |
| 旧分阶段执行计划 | `no-op` | 已归档且仍有历史审计价值，不恢复、不删除 |
| README 活跃索引 | 更新 | 保证所有新增文档可发现 |

阶段实现完成后，只更新真实完成部分：当前实现变化进入基线文档，AGV 状态按证据升级；未实现的
后续阶段继续保持目标方案或评审稿状态。

## 11. 评审清单

评审本索引时应一次确认：

- 是否接受“P0 → P1 → P2A/P2B → P3”的依赖顺序；
- 是否接受 P0 必须同时关闭 14 个 Workspace 失败和独立 P0-MCP GAP；
- 是否接受固定原始 `openapi-mcp-server`/`mock_service` commit 只作为隔离合成夹具，且
  P0-MCP 不验证认证、生产安全或业务 Agent 能力；
- 是否接受 P1 的 8 案例、无工具、确定性评分和独立安全否决范围；
- 是否接受失败测评通过幂等业务动作创建一个改进事项，而不是自动推进后续阶段；
- 是否接受 P2A 不迁移公开会话字段、不接第二 Runtime；
- 是否接受 P2B 只做 shadow 候选，不提供自动启用；
- 是否接受 P3 的认证、公共字段迁移和完整 MVP 都必须单独评审；
- 是否接受本轮不 bump `VERSION`、不建 tag、不变更运行卷。

上述裁决发生实质变化时，应先修订本索引和对应阶段方案，再进入实现。
