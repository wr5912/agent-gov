# AgentGov 下一阶段 P3 扩展准入实施方案

> 文档状态：评审稿。
>
> 工程阶段说明：P3 是多个后续扩展方向的独立准入门集合，不是一次性并行交付清单。
>
> 前置入口：[AgentGov 下一阶段实施方案索引](../AgentGov下一阶段实施方案索引.md)。

## 1. 目标

P3 不默认启动任何扩展。它要求网络安全完整 MVP、Runtime 公共迁移、Governor 人工启用和第二
Runtime 分别证明必要性、迁移安全和真实验收能力，防止 P1/P2 的窄纵切片被直接外推为规模化能力。

```text
P1/P2 证据
  -> 每条扩展线独立 readiness review
  -> 通过才建立实施里程碑
  -> 真实容器与人工/外部证据
  -> 更新当前实现基线与 AGV 状态
```

## 2. 通用启动门

任一扩展线开始前必须满足：

- P0、P1 退出门全部通过；
- 若依赖 Runtime 或 Governor，则对应 P2A/P2B 退出门通过；
- 本次扩展绑定明确 AGV 用例和真实业务场景；
- 公开 API、DB、OpenAPI、前端、env、Workspace、历史数据有删除/迁移/保留清单；
- 有回滚点、幂等、部分失败和真实容器验收设计；
- 不把目标文档、mock 或 local-debug 结果当作当前能力；
- 发布点由用户确认，不因进入 P3 自动 bump `VERSION` 或创建 tag。

P0 的
[模拟 MCP 平台回执](./AgentGov下一阶段P0模拟MCP平台验收实施方案.md)
只证明 AgentGov 能在隔离合成夹具中完成工具发现、调用和证据采集。它不是 P3-A 动态安全场景、
MCP 认证、生产网络、领域评分或高风险动作审批的 readiness 证据。

## 3. P3-A：网络安全完整 MVP

### 3.1 启动条件

- P1 的 8 case 静态纵切片稳定；
- 已形成不少于 50 个高质量案例；
- 核心案例由至少两名独立安全专家复核；
- 开发集与隐藏集分离；
- 数据来源、授权、脱敏、保留和销毁规则已批准；
- 动态工具测试具有隔离环境、审批和停止条件。

以上启动条件必须来自 P1 领域证据和本阶段独立设计。不能用 P0-MCP 的两个 GET tools、固定
seed 数据或临时 token 变量替代安全专家、隐藏集、数据授权、认证和审批条件。

### 3.2 扩展顺序

1. 静态标准化案例从 8 扩展到 30–50；
2. 增加人工 ReviewDecision 与争议处理；
3. 增加 20–30 个告警研判/多跳调查案例；
4. 接入只读或模拟工具，并采集原生 tool trace；
5. 增加高风险动作审批与安全沙箱；
6. 最后评审 L3 多智能体工作流和 L4 运营闭环。

### 3.3 硬门

- 完整 MVP 仍复用 Workspace 测试、`AgentTestRun`、ImprovementItem 和 Release；
- 动态环境只保存受控引用和证据，不复制外部系统权威数据；
- 评审者身份、修订和争议记录由后端或外部审批系统提供；
- 模型辅助裁判不能覆盖确定性安全失败；
- 空态、成功态、评分失败、人工复核、工具错误和安全阻断均有 UI 场景证据。

第 4 步可以复用 P0-MCP 已验证的“OpenAPI → MCP → Runtime 原生 facts”技术认识，但必须新建
领域协议、工具 allowlist、认证/授权或明确的模拟边界、场景 Ground Truth、失败注入和独立
评分。不得直接把 P0 fixture 镜像、过滤 spec 或回执晋级为 P3 生产资产；若仍只需要平台 smoke，
P3-A 保持未启动。

## 4. P3-B：Runtime 公共契约迁移

### 4.1 启动条件

- P2A Claude gateway 和真实容器行为等价；
- P1 闭环已完整运行在 gateway 上；
- 历史 SQLite 中 Claude session 数据完成只读兼容分析；
- OpenAPI、SSE、前端和上层客户端 breaking change 已获批准。

### 4.2 目标契约

公共会话引用统一为：

```text
RuntimeSessionRef:
  runtime_kind
  native_session_id
  native_project_key?
```

迁移要求：

- Claude 旧 `sdk_session_id` 一次性迁移为 `native_session_id`；
- DB、records、API response、SSE、OpenAPI、前端生成类型和 ContextPackage 同一阶段原子切换；
- 不保留长期 alias、双写或请求双字段；
- 历史快照通过明确 projection 读取，不放宽当前 response schema；
- 重复 migration、历史脏数据、回滚和真实数据列表/详情/UI 必须验证；
- run 仍不可重开，session resume 创建新 `run_id`。

### 4.3 Claude adapter 物理抽取

公共迁移稳定后，再把现有 Claude 执行、session、HITL、事件和 telemetry 逻辑按 P2A 小端口迁入
`runtime_adapters/claude_code`。每移一个端口先跑等价 suite，再删除旧 facade；生产路径旧 symbol
最终清零，历史 migration 和负向测试可保留旧名。

## 5. P3-C：Governor 人工启用与回退

### 5.1 启动条件

- P2B 至少一个 candidate 完成 current-vs-candidate 隔离评估；
- candidate、安全门、评估包和版本均可追溯；
- 认证中间件提供 backend-owned `AuthenticatedPrincipal`；
- 角色和职责分离获批。

### 5.2 认证与角色

- 认证 middleware 从服务端私有配置解析 principal，不接受请求体 `operator` 作为权威；
- `governor_learning_reviewer` 可查看证据、提交评审意见；
- `governor_learning_admin` 可在评估通过后启用或回退；
- 候选生成者不能成为该候选唯一 evaluator 或唯一 activator；
- 开发单 key 模式也必须由服务端映射到稳定 principal，不把 key、映射或私有路径写入仓库。

### 5.3 用户动作与 API

Governor 能力治理使用独立管理面，不进入四阶段工作台：

| 用户动作 | 业务产物 | API | 状态/版本副作用 |
| --- | --- | --- | --- |
| 查看候选 | 无 | 复用 P2B read API | 无 |
| 提交评审意见 | `GovernorReviewDecision` | `POST /api/governor/method-candidates/{id}/reviews` | 不切 active |
| 启用候选 | `GovernorCapabilityActivation` | `POST /api/governor/method-candidates/{id}/activate` | 原子切换 active version |
| 回退版本 | `GovernorCapabilityActivation` | `POST /api/governor/capability-versions/{id}/rollback` | 原子恢复指定旧版本 |

Activation/rollback：

- 只接受服务端认证 principal；
- 使用 idempotency key 和 active version compare-and-swap；
- 评估失败、安全失败、证据不足、候选过期或角色不足时 fail-closed；
- 并发请求最多产生一次 active pointer 切换和一次审计事件；
- 旧版本和历史产物不可改写；
- 新 Governor run 立即绑定新 active version，进行中的 run 保持启动时版本。

### 5.4 UI 归属

新增“Governor 能力治理”只读/管理视图，展示候选 diff、适用条件、反证、current-vs-candidate
评估、安全门、版本和回退条件。它不成为业务 Agent、不进入改进事项阶段条，也不复制四阶段产物
编辑入口。

## 6. P3-D：外部 CLI 与第二 Runtime

### 6.1 Claude 外部 CLI observer

只有出现无法通过受管 API 获得的真实旁路证据需求时启动。observer 必须：

- 显式配对已有 Agent；
- 严格只读，不能写配置、批准工具、取消、恢复或控制进程；
- durable spool、ack、sequence、幂等和 coverage 完整；
- adapter allowlist 排除 env、认证存储和未知文件；
- 断网、重复、乱序、重启和撤销配对均有真实 CLI 验收。

### 6.2 第二 Runtime

- 首选顺序沿目标架构从 Qwen Code 开始，但只有真实业务需求和版本 spike 通过才实施；
- 先做 managed adapter，再决定是否需要 observer；
- 使用独立原生 Workspace/Governor 包，不转换 Claude 配置；
- 运行同一 contract suite，并对不支持能力 fail-closed；
- 一个部署仍只运行一个 Runtime；需要并存时部署独立 AgentGov 实例。

Codex、Kimi、CodeWhale 只能在前一个非 Claude adapter 稳定后逐个评审，不并行承诺兼容。

## 7. P3-E：Multica 边界

Multica 不属于上述阶段的前置依赖。只有同时出现至少两个独立真实场景，且通用 AgentGov API 与
只读 observer 均无法满足必要的关联、审计或取消语义时，才新建产品方案。

任何 Multica 评审必须保持：

- AgentGov 不复制 workspace、issue、member、queue 或任务状态；
- 不增加 Multica 专用 Compose 强依赖；
- Multica 作为普通客户端或外部 CLI 上层调度者；
- AgentGov 继续只负责 Runtime、反馈闭环、评测和版本治理。

## 8. 测试与验收矩阵

| 扩展线 | 必需测试 | 必需真实证据 |
| --- | --- | --- |
| 安全完整 MVP | 数据授权、隐藏集、评分一致、人工争议、安全审批、工具失败 | 专家复核、隔离环境、真实容器；P0-MCP 回执不替代 |
| Runtime 公共迁移 | fresh/历史 DB、重复/回滚 migration、OpenAPI/type、非法状态 | 真实数据列表/详情/UI、Claude live |
| Governor 启用 | 未授权、过期、评估失败、并发、幂等、回退、hostile 字段 | 认证 principal、真实 activation/rollback trace |
| observer | pairing、spool、重复/乱序、coverage、秘密排除 | 真实 CLI、断网和进程重启 |
| 第二 Runtime | 共用 contract suite、能力降级、原生 package | 固定 CLI/SDK 版本、真实 managed run |
| Multica | 边界负向断言 | 未配置时核心能力完整 |

每条扩展线均需运行目标测试、`make codex-guard`、`make typecheck`、`make main-flow-test`、
串行 `make test`，并通过对应公共真实容器入口。需要真实专家、外部审批或 CLI 的验收不能用 mock
替代。

## 9. 退出、更新与停止条件

- 每条扩展线独立退出，不要求 P3 全部完成；
- 完成一条线后，只更新其真实实现基线、AGV 状态、README/OpenAPI/前端和质量策略；
- 若真实需求消失、上游 CLI 无稳定结构化接口、评估无法独立或认证不足，应停止该线而不是降低门槛；
- 若出现秘密泄露、跨 Agent 污染、不可回滚迁移或双重控制，立即回退并重新评审；
- 版本发布仍遵循根 `VERSION` 单一真相和用户确认，不因文档阶段完成自动创建 tag。
