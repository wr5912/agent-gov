---
phase: 07-per-agent-lane
plan: 03
subsystem: lane-integration-and-acceptance
status: complete
tags: [quality-policy, openapi, frontend, container-acceptance, authority]

requires:
  - phase: 07-per-agent-lane
    plan: 01
    provides: exact-commit run 与 receipt-aware release gate
  - phase: 07-per-agent-lane
    plan: 02
    provides: worker/sandbox 隔离执行与 cleanup authority
provides:
  - P0 exact-commit lane 的冻结实现与 P0-MCP/P1 live planned 分类契约
  - OpenAPI/前端 typed receipt 及 receipt-aware 发布展示
  - 固定 profile/verifier、候选快照、生命周期回执与容器验收 authority
  - deletion、Workspace/Git、Settings、defensive boundary 与 TIA 收口
affects: [07-verification, 08-p0-mcp, 09-p0-admission, 10-independent-evaluation]

key-files:
  created:
    - scripts/container_acceptance_candidate.py
    - scripts/container_acceptance_receipt.py
    - scripts/container_acceptance_toolchain.py
    - scripts/container_acceptance_verifier_process.py
    - app/runtime/runtime_db_migrations_0059.py
    - app/services/agent_workspace_fingerprint.py
    - frontend/src/components/settingsRequestContext.ts
    - scripts/check_defensive_security_boundary.py
  modified:
    - tests/quality_policy.json
    - app/openapi_contract.py
    - frontend/src/types/api.ts
    - docker/Dockerfile
    - docker/docker-compose.yml
    - Makefile

key-decisions:
  - "公共 profile 只能映射登记过的固定 verifier；caller 不能选择私有 Make target、command、env、toolchain 或 receipt 身份。"
  - "验收候选由 agentgov.container-acceptance-candidate.v5 冻结；runtime-bootstrap 进入镜像，Compose 不再接受宿主 bind 或 RUNTIME_BOOTSTRAP_HOST_DIR。"
  - "receipt 只允许 reserved -> prepared -> single terminal，并绑定同一 lifecycle lock；stale recovery 与 signal 只能清理后提交一个终态。"
  - "P0 exact-commit、P0-MCP 和 P1 live 各有独立 owner/capability/resource/enforcement，任一 lane 不能冒充另一 lane。"

patterns-established:
  - "Frozen verifier: profile、verifier identity、exact argv 与后端 receipt 绑定。"
  - "Durable acceptance: candidate/receipt/lock/signal/cleanup 形成单一恢复 lineage。"
  - "Collection boundary: 业务 Agent pytest leaf 留在各自 commit，平台根测试只验证 lane 契约。"

requirements-completed: [LANE-01, LANE-02, LANE-03, LANE-04, LANE-05, LANE-06, LANE-07, LANE-08]
---

# Phase 7 Plan 03：质量策略、公开契约与最终验收收尾

**实现与文档冻结前功能收口轮已完成；本计划和 Phase 7 已关闭。收尾提交仍由文档冻结后的 exact-tree durable final gate 约束，P0-MCP 继续由 Phase 8 独立交付。**

## 当前状态

- **实现任务：** 3/3 已落地并验收
- **阶段总计：** 3/3 plans、12/12 tasks complete
- **需求：** LANE-01 至 LANE-08 complete
- **提交：** 由本次 Phase 7 收尾提交承载；文档不写提交自身哈希
- **完成日期：** 2026-08-13

## 冻结实现

### 验收入口与候选 authority

- profile registry 将每个公共入口固定到唯一 verifier identity 与 exact argv；控制面只创建 durable run，执行 authority 只属于登记过的 verifier/worker。
- 首次 bootstrap 在 fd exec 前清理 caller 内部 env，并固定 Python、Git、Docker、Node、pnpm 与 Make 工具链；caller 的 `HOME`、NVM/PATH、Docker routing 或私有 overlay 不能改变候选。
- 候选使用 `agentgov.container-acceptance-candidate.v5`，绑定实际加载源码、Git/index/source projection、Python/Node/pnpm 依赖与 Docker daemon 摘要；path/ancestor、第二次读取或依赖 drift 均 fail-closed。
- `docker/runtime-bootstrap` 已复制进候选镜像；Compose 与 runner 拒绝指向 `/app/docker/runtime-bootstrap` 的宿主 bind，`RUNTIME_BOOTSTRAP_HOST_DIR` 不再是运行配置。

### Durable receipt、锁与信号

- 回执生命周期收口为 `reserved -> prepared -> succeeded | child_failed | failed`；`reserved` 只可在候选发布前失败，`prepared` identity 冻结后只能单次原子终态。
- receipt 绑定 lifecycle lock digest；同一次恢复必须继承相同 kernel lock，错误 descriptor/cookie/address 或替换后的 lock authority 均拒绝。
- stale reserved/prepared 与中断临时文件只在持锁、精确 cleanup 成功后终态；cleanup 失败保留可恢复状态，不伪造 terminal receipt。
- SIGINT/SIGTERM 在 verifier、exec handoff、freshness barrier 或 terminal transition 中均由单一 signal controller 收口；cleanup 完成后才提交终态，终态 commit point 之后的信号不能重写回执。

### 相邻 authority 与负向边界

- deletion journal 由 0059 重装状态枚举、完整转移、authority/terminal 不可变与禁止删除约束；合法 witness cleanup replay 仍保持幂等。
- Workspace fingerprint 使用 fd-relative、no-follow、身份复核和有界读取；Git common metadata 与临时 authority 在 replace/unlink 前后校验 device/inode/type，不跟随替换路径。
- SettingsModal 使用 context/lane generation 处理 registry、OpenAI compatibility、Workspace package 与 deletion discovery 竞态；旧 context 的 success/error/finally/chained reload 均不能覆盖新状态。
- `check_defensive_security_boundary.py` 与测试固定只读工具、批准 task/asset、无 shell/本地 MCP/敏感读取边界；quality policy 为上述模块登记精确 nodeid/TIA，未知改动仍 fail-closed 到全量。

## 已确认宿主机证据

| 验证项 | 文档冻结前功能收口结果 |
| --- | --- |
| quality manifest | 最终冻结候选 collect-only 2225 pytest leaves；manifest 通过 |
| acceptance host no-Docker 契约 | 最终冻结候选 collect-only 402 leaves；exact-tree 执行由提交级门完成 |
| OpenAPI / type drift | 134 operations；契约与生成类型无漂移 |
| main-flow backend | 最终冻结候选 collect-only 694 leaves / 640 个精确 selector |
| main-flow UI bindings | 11 bindings 通过 |
| `make codex-guard` | 通过 |
| `make typecheck` | 通过 |
| 串行 `make test` / main-full | 最终冻结候选选择 2220 leaves；exact-tree 执行由提交级门完成 |
| 公共容器功能收口轮 | core、agent-test、health、speech、ui-cancel、live、langfuse 的历史功能轮均生成独立 fresh terminal receipt；该轮已被最终 authority 修复候选超越，不能充当提交级回执 |

上述结果只证明 Phase 7 的 exact-commit 执行来源、隔离、回执与 cleanup，不替代 Phase 8 P0-MCP 或独立业务能力测评。

## 最终硬门

| 门 | 状态 | 结果 |
| --- | --- | --- |
| 最终冻结候选静态基数 | READY | manifest 2225、main-full 2220、acceptance 402、main-flow backend 694 leaves / 640 selectors、UI 11、OpenAPI 134 |
| 历史公共功能收口轮 | PASS / SUPERSEDED | 七个入口分别保留 terminal receipt；共享 candidate tree/env 摘要且 teardown 后无 scoped residue，但该轮已被最终 authority 修复候选超越 |
| 提交级 durable final gate | ENFORCED | 文档冻结后对 exact tree 重跑 `make test` 与七个公共入口；任一失败阻止提交，最终回执不回填本文以避免改变已验候选 |
| Phase/requirement 翻转 | PASS | ROADMAP、STATE、PROJECT 与 REQUIREMENTS 同步为 Phase 7 complete / Phase 8 ready |

## 边界核对

- execution provenance 只证明 exact-commit 执行来源、隔离契约与 cleanup，不替代 Phase 10 evaluator-owned 基准、比较或安全否决。
- P0-MCP 仍由 Phase 8 独立计划和验收；Phase 7 已关闭，但 Workspace lane 不能替代精确两工具的真实 MCP 证据。
- 本收尾不 bump `VERSION`、不创建 tag；提交和推送由同一次 Phase 7 Git 收尾完成。
- 当前发布版仍为 3.0.3，v3.1 状态保持 executing。

---
*Phase: 07-per-agent-lane / Plan: 03*
*Status: complete*
