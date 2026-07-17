# AgentGov 工程宪法与当前交付边界

> 文档状态：当前工程宪法。
>
> 本文只定义产品工程、业务 Agent workspace、CI/CD 的稳定原则、规范裁决、事实认定和修订边界。
> 当前执行实现和运行事实若与本文冲突，应作为后续 issue 的整改对象；整改完成前不得用
> 文档把目标冒充成现状。ReleaseBundle、签名供应链、生产晋级和灾备等旧远期设想只作
> 历史追溯，不构成当前门禁。

## 1. 目标

当前工程交付按以下顺序解决三件事：

1. 把 AgentGov 自身的业务 Agent 创建、workspace 资产、运行、反馈优化、评估回归和版本治理
   核心闭环做强。
2. GitHub PR 与 `master` push 持续执行同一套真实 `quality-gate`，结果能够按 AID
   进入 Multica 研发协作会话。
3. 人工选择通过 CI 的精确 `master` SHA，使用
   `scripts/deploy_agent_gov_to_host` 部署到联调环境，并能健康检查、诊断和回退。

Multica 当前只承担本仓库持续 CI 的研发协作与结果展示，不代表长期协作平台选型。
当前不建设正式 Release 平台、生产发布审批、同一制品晋级体系或产品内通用协作模型。

## 2. 不可违背的原则

### 2.1 价值优先与阶段适配

在产品价值尚未验证的阶段，工程投入优先保障核心业务闭环完整、可用和可恢复。
安全治理必须与真实暴露面和已发生风险相匹配；除基础鉴权、网络访问边界、输入资源限额、
路径与文件系统边界、凭据不进入源码仓库和日志等低成本基础防护外，不得以签名供应链、
零信任、多方审批、复杂隔离或恶意可信维护者模型作为核心功能交付前置。

出现以下任一条件时，必须重新评估并升级安全基线：

- 对公网开放；
- 接入外部租户或真实客户敏感数据；
- 承诺生产 SLA；
- 出现合规要求；
- 已发生真实攻击、安全事故或明确可复现的利用路径。

### 2.2 机器事实优先

- CI 结论只认 GitHub Actions 的目标 workflow/run/attempt/job 事实。
- Multica 是 CI 通知投影；通知失败不能把红 CI 改成绿，也不能触发部署。
- 联调环境当前运行版本只认目标机实际激活的部署快照。
- 文档、AID 评论、tag、缓存和人工口头说明不能替代运行事实。

### 2.3 单一 CI 与单一部署入口

- PR 与 `master` push 继续由稳定名称 `quality-gate` 汇总阻塞通道。
- `master` 必须经 PR、唯一工作项追踪标识和 required `quality-gate`；当前实现使用
  `AID-N`，但具体前缀和协作平台不属于长期产品不变量。
- 绿色 CI 不自动部署。
- 联调部署只允许显式调用 `scripts/deploy_agent_gov_to_host`。
- 部署必须绑定完整 SHA、同一工作项追踪标识、PR 和成功 workflow URL。
- workflow URL 只是机器事实的定位符，不是成功证明；部署前必须只读回查同仓库
  `master` push 的 run/attempt、`quality-gate` 成功结论、完整 SHA、已合并 PR 和一致的
  工作项追踪标识。任一事实不一致都应拒绝部署。

工作项标识只承担 GitHub 变更的追踪和通知路由，不参与 CI 判定。当前 `AID-N` 由 GitHub
branch/PR 元数据校验，`quality-gate` 不查询 Multica 中的 issue 状态或可用性；未来切换协作
平台时，可以替换当前命名约定和通知 adapter，而不改变 SHA、PR、workflow 与目标快照这些
机器事实。

### 2.4 版本证据不等于双轨发布

平台源码 SHA 与业务 Agent 的 `agent_id + agent_version_id` 治理不同对象：

```text
平台工程证据：commit SHA + GitHub workflow run
Agent 行为证据：agent_id + per-Agent Git commit
一次实际运行：平台 SHA + agent_id + agent_version_id
```

它们是一次运行的两个事实维度，不是两套 Release 状态机，也不需要“双链路、双证明”
产品机制。任何对象只能有一个当前版本真相。

### 2.5 Env 与运行边界

- Docker Compose 每次选择一份完整 env；不是 layered override。
- 容器运行态根目录保持 `${HOME}/volume-agent-gov`。
- 本机调试根目录保持 `/tmp/local-debug-volume-agent-gov`。
- `docker/runtime-volume-seeds/` 是只读出生配置；已有 live workspace 不被重启或部署回灌。
- 本机调试结果不能冒充真实 Compose 或联调环境验收。

### 2.6 业务 Agent workspace 原样原则

live workspace 与它自己的 per-Agent Git 是该业务 Agent 当前行为资产的真相源。对
workspace 包的导入、导出、恢复和 Git 版本切换，平台必须保留普通文件字节、二进制内容、
executable bit 和权限配置，不做身份散文改写、endpoint 脱敏、权限收紧或其他静默修复。

live workspace 可以包含 `.env`、真实 endpoint、凭据型 header、数据库连接配置和本机路径；
这些内容随 workspace 包和 per-Agent Git 一并视为敏感运行资产。允许保存不等于允许在日志、
错误回执、公开摘要或项目源码仓库中回显。平台仍须保留低成本基础保护：API 鉴权、包大小与
成员数上限、路径穿越与特殊文件拒绝、关键 JSON 可解析、并发维护栅栏和 CAS。

本原则解决的是“平台是否应改写业务 Agent 自有资产”，不授权执行上传包中的代码，也不放宽
运行时工具权限、网络权限或宿主机挂载边界。

### 2.7 Repo template、声明 seed 与 builtin 准入

“原样”必须说明复制方向，不能把不同对象混称为模板：

| 对象与方向 | 稳定裁决 |
| --- | --- |
| generic template → live | 除明确的身份 token 外原样；generic template 采用保守出生权限，不绑定真实秘密或具体私有环境 |
| repo 声明 seed/builtin → live | 同 ID、跨 ID 都按字节和权限配置原样复制；generic template 的保守权限不得覆盖专用 seed |
| live workspace ↔ workspace 包/per-Agent Git | 原样往返，可含真实私有运行配置；按敏感资产保管 |
| live workspace → repo seed/builtin | 先在项目仓库外生成逐字节候选，再执行仓库准入；不是无条件原样提交 |

项目源码仓库的准入采用分级门，而不是对 live workspace 统一脱敏：

- 明确 API key/token、凭据型 Authorization/MCP header、数据库密码、私钥、带凭据 URL、
  本机个人路径、`.env`/local override、运行态数据库、日志和 Claude 私有状态属于硬阻断；
- 声明 seed 中不带凭据的真实 endpoint、IP、端口、内网域名以及宽权限属于可移植性或
  能力风险，必须提示提交者复核，但不冒充泄密问题、不自动替换；generic template 仍须使用
  可由部署环境解析的变量引用和保守出生权限；
- 扫描默认只读。只有用户明确选择替换时才能运行 sanitize，并必须复核 diff；
- 通过仓库准入后的声明 seed 才成为后续“原样实例化”的来源。

若未来确实需要含真实秘密、仍须逐字节复用的 builtin，应设计 Git 仓库外的私有只读 catalog；
不得通过放宽项目源码仓库边界实现。是否建设该 catalog 由真实使用需求触发，当前不预埋。

## 3. 最小充分论证

新增、删除或修改不可豁免原则时，必须同时说明：

1. 它解决的实际问题；
2. 可由代码、机器策略或运行态核实的事实依据；
3. 主要替代方案未采用的原因；
4. 何种条件下可以修订或退出。

论证服务于可复核性，不额外创造隐含门禁。调查流水账、临时命令和阶段性实现细节进入
实施方案、runbook、issue 或归档，不进入宪法。

活跃产品与工程文档必须让未参与讨论的读者大致理解“为什么这样做、边界在哪里、如何验证”；
只罗列结论、字段或步骤而没有最小事实依据，不满足本文档治理要求。该原则不要求堆砌背景，
也不允许用长篇论证掩盖当前事实、未实现能力或明确 gap。

## 4. 规范裁决与事实认定

### 4.1 规范冲突裁决

对“应该如何设计和执行”的规范冲突，按以下顺序裁决：

1. 产品目标与术语；
2. 本宪法；
3. `tests/quality_policy.json` 等机器策略；
4. workflows、Makefile、scripts 和实施文档。

下层必须实现上层要求；不一致表示待整改 gap，不能由下层重新解释上层语义。

### 4.2 当前事实认定

对“当前是否已实现、已发生或已通过”的事实判断，按以下顺序取证：

1. GitHub、Multica、目标主机和实际运行态的直接证据；
2. 可绑定同一 SHA、run、attempt 或目标快照的机器输出与日志；
3. 文档、issue 和人工说明中的叙述。

高层规范定义目标和约束，但不能单独证明当前事实。实际证据与规范不一致时，应如实登记 gap
并整改对应实现，不能用规范声明覆盖运行现状。

机器策略声明了执行器并不具备的 trigger、预算或验收能力时，必须修正机器策略或登记明确
gap，不能用文档把目标态冒充成当前事实。

## 5. 当前非目标

- ReleaseBundle、Cosign、SBOM/provenance 和外置信任锚；
- Staging 到生产的制品晋级；
- 蓝绿、PostgreSQL、PITR、RPO/RTO；
- seed catalog 签名与恶意 env 重定向防护；
- 仓库外私有 seed/builtin catalog；
- AgentGov 产品内的通用协作 adapter；
- 自动部署、自动生产发布和多方审批。

这些能力只有命中 2.1 的升级条件并重新评审后，才能从远期蓝图进入当前路线。

## 6. 修宪

修宪必须通过受保护 PR、完整 `quality-gate`，并同步：

- 本宪法及相关当前实施文档；
- 机器策略与执行器；
- README/docs 索引；
- 测试和回退说明。

普通 workflow 参数、测试预算和联调目标机调整不属于修宪，但不得违反本节之前的稳定原则。
