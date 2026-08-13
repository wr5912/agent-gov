---
phase: 07-per-agent-lane
status: passed
score: 4/4
requirements_completed: [LANE-01, LANE-02, LANE-03, LANE-04, LANE-05, LANE-06, LANE-07, LANE-08]
---

# Phase 7：per-Agent 最小权限隔离测试 lane 验收报告

## Verification Passed

**Phase Goal：** 平台在不信任业务 Agent 测试代码的前提下，隔离执行精确 commit 的固定 Workspace pytest，并以 backend-owned execution provenance 作为发布必要的工程卫生证据。
**Status：** `passed`
**Verified：** 2026-08-13
**Commit：** 当前 Phase 7 收尾提交（不在提交正文中自引用哈希）

实现、宿主机门与七个公共入口的历史功能收口轮已通过；各入口保留独立 durable receipt，不能用其中一张替代其他入口。本文已同步最终冻结候选的 collect-only 基数；Phase 7 收尾提交仍由本文冻结后的 exact-tree durable final gate 原子约束，最终回执不回填本文以避免修改已验候选。

## Goal-backward 状态

| # | 可观察事实 | 当前状态 | 已有证据与缺口 |
| ---: | --- | --- | --- |
| 1 | 只选择 Agent 与精确 commit 即可执行固定 pytest，Workspace leaf 不进入根 collection，receipt 只声明 execution provenance | VERIFIED | raw Git materializer、durable queue、fixed verifier、2225-leaf 最终冻结 manifest 与 agent-test 功能轮 receipt 一致 |
| 2 | 执行进程非 root、无继承 secret、无可写 live data、无宿主机控制权，hostile 测试不能越界 | VERIFIED | inspect-first sandbox、caller-env scrub、defensive boundary、host no-Docker 契约与真实隔离容器观察通过 |
| 3 | 每次运行回执绑定 commit/suite/source/image/invocation/isolation/cleanup，且不包含敏感值 | VERIFIED | candidate v5、fixed toolchain、`reserved -> prepared -> single terminal`、lifecycle lock/stale/signal 与七张独立 fresh receipt 通过 |
| 4 | 历史或 digest 错配记录不能放行，前置缺失严格失败，终态不遗留临时资产 | VERIFIED | publication gate、0059、fd/Git temp authority、串行全量、cleanup/signal 负向契约与公共入口 teardown 通过 |

**Score：4/4。**

## Requirement 状态

| Requirement | 状态 | 当前证据 | 剩余门 |
| --- | --- | --- | --- |
| LANE-01 | COMPLETE | 后端解析 Agent、raw Git exact commit、固定 verifier/invocation 与 agent-test 公共入口通过 |
| LANE-02 | COMPLETE | 非 root/readonly/network-none/cap-drop/no-new-privileges typed inspect 与真实 container inspect 通过 |
| LANE-03 | COMPLETE | caller env scrub、hostile source/secret/path/socket/network 负向契约与隔离执行通过 |
| LANE-04 | COMPLETE | candidate v5、toolchain、receipt/lifecycle/signal authority 与各入口 fresh receipt 通过 |
| LANE-05 | COMPLETE | 最终冻结候选 root manifest 为 2225 leaves；Workspace leaf 不进入根 collection；提交级串行全量仍由 exact-tree 门执行 |
| LANE-06 | COMPLETE | P0/P0-MCP/P1 lane 分类与 TIA 精确绑定通过 |
| LANE-07 | COMPLETE | current commit/suite/source/worker/container eligibility 与同候选 receipt 通过 |
| LANE-08 | COMPLETE | fail-closed、lock/stale/signal/exact cleanup 与公共入口 teardown 通过 |

## 冻结 authority 核对

- **Profile → verifier：** 公共 target 唯一映射固定 verifier identity/exact argv；caller 不能注入 runner、私有 Make target、command、profile 或 receipt identity。
- **Caller env / toolchain：** 首次 fd bootstrap 前清理内部 authority env；绝对 Python/Git/Docker/Node/pnpm/Make 与实际加载源码被固定并在执行前复核。
- **Candidate snapshot v5：** source、Git/index、依赖、Docker daemon 与 runtime projection 形成不可变候选；path restore、ancestor swap、第二次读取 drift 和 sourceless fallback 均拒绝。
- **Receipt lifecycle：** `reserved -> prepared -> single terminal`；候选 publication、receipt CAS、cleanup 与 terminal commit 归属同一 lineage。
- **Lock / stale / signal：** receipt 绑定原 lifecycle lock；stale recovery 持锁且 cleanup-first；signal 只有一个 commit point，不产生双终态。
- **Image-contained bootstrap：** Dockerfile 包含 runtime-bootstrap；Compose 与 runner 禁止宿主 bind 和 `RUNTIME_BOOTSTRAP_HOST_DIR`。
- **Deletion authority：** 0059 安装 append-only 状态/转移/terminal/no-delete triggers，保留精确 witness recovery。
- **Filesystem/Git authority：** Workspace fingerprint、Git common metadata 与临时目录使用 fd/device/inode/type/mount 身份复核，替换目标不被清理或跟随。
- **Settings race：** request context/lane generation 阻止旧 modal context、旧 registry/package/deletion 请求的 late success/error/finally 覆盖当前状态。
- **Defensive/TIA：** 防御性工具/task/asset 边界和精确 TIA nodeid 已进入机器策略；TIA/xdist 仍是 shadow，未知路径回退全量。

## 已运行证据

| 验证项 | 结果 |
| --- | --- |
| quality manifest | 最终冻结候选 collect-only 2225 pytest leaves；通过 |
| acceptance host no-Docker | 最终冻结候选 collect-only 402 leaves；exact-tree 执行由提交级门完成 |
| OpenAPI / frontend type drift | 134 operations；通过 |
| main-flow backend | 最终冻结候选 collect-only 694 leaves / 640 个精确 selector |
| main-flow UI bindings | 11 bindings 通过 |
| `make codex-guard` | 通过 |
| `make typecheck` | 通过 |
| 串行 `make test` / main-full | 最终冻结候选选择 2220 leaves；exact-tree 执行由提交级门完成 |
| 公共容器入口 | 历史功能收口轮全部通过；独立 receipt 且 candidate tree/env 摘要一致，但已被最终 authority 修复候选超越 |

当前表只登记文档冻结前功能收口证据；其他候选、旧 Trace 或历史 receipt 不能证明本候选。

### 已被最终 authority 修复超越的历史公共功能收口轮

共同 candidate tree：`05d304b43aef479ed43a957d2fff4ee382eef169`
共同 env 摘要：`a01d8e674d0468c930916a7ec667f4c5b416aaf7af8aa6038fb8942daad4849c`

| 公共入口 | run id | receipt id | images | 结果 |
| --- | --- | --- | ---: | --- |
| `container-core-smoke` | `1786558884-f88037faf226` | `117239329d56715eb84f3b515f01aed27875f72044e6ad63621dabcab19009aa` | 5 | succeeded / child 0 |
| `container-workspace-pytest-test` | `1786559277-af43cb050735` | `d9d9930cfeb0490356a37a48433a9e545801a221040993d64e49e9464a91a384` | 4 | succeeded / child 0 |
| `container-health-e2e` | `1786559626-cc8eca7d6d5e` | `a1cb9d87df5e26c7170939a0516cb200b6802bed1e17340179fc4e2fa65a10ed` | 4 | succeeded / child 0 |
| `container-speech-summary-test` | `1786559926-cf3231012cd4` | `10dfa753240513dac2d36c096fcdd6a7f2ae065e79cdb28f1f07f9ce1679ad16` | 5 | succeeded / child 0 |
| `ui-playground-cancel-smoke` | `1786560297-e9f3a90b0756` | `d1fa2cc5747829d4575cdd0511faa9725e6a111e6fe18a75f3565a8cfed0ccf5` | 5 | succeeded / child 0 |
| `container-live-test` | `1786560617-d956c75b965f` | `2712525ae870cf1a6a86ed70491cc08aa46f5a170f0a78f0a03ec9fc99e90a73` | 5 | succeeded / child 0 |
| `langfuse-smoke` | `1786560948-5f1f1e0839a8` | `263ee6571a664b1e9cd80ed5a5c9ad937400ec8db8621e66305d1a594d5b1a59` | 11 | succeeded / child 0 |

这七张回执是历史功能收口轮证据，已被后续 terminal authority 修复候选超越，不冒充本文冻结后的最终 exact-tree 回执；表中只有摘要和后端生成标识，不记录私有 env 路径或值。

## Final Gate Audit

1. 最终文档冻结候选的 collect-only 基数为 manifest 2225、main-full 2220、acceptance 402、main-flow backend 694 leaves / 640 selectors、UI 11、OpenAPI 134。
2. 上表历史七入口分别生成 fresh terminal receipt；顺序以 langfuse 最后恢复完整观测栈，镜像内 runtime-bootstrap、实际 image/source/config authority、适用 inspect/hostile/signal 场景、exact cleanup 与隔离 residue 均已核对，但其候选已被后续 authority 修复超越。
3. 收尾流程在本文冻结后必须对 exact tree 重新运行 `make test` 与全部七个公共入口；只有 durable final gate 全部成功才允许产生 Phase 7 提交。最终 tree/receipt 不回写本文，避免验证后再次改变候选。
4. ROADMAP、STATE、PROJECT 与 REQUIREMENTS 同步为 Phase 7 complete / Phase 8 ready，3 个 plans / 12 个 tasks 与 LANE-01 至 LANE-08 全部关闭。

## 范围

- Phase 7 不证明 P0-MCP，也不证明 Phase 10 evaluator-owned 业务能力或安全结论。
- 外层容器验收 receipt 与业务 `AgentTestExecutionReceipt` 分权，二者都不冒充业务正确性或独立安全测评。
- 本轮不修改版本或 tag；Git 提交推送只发布 Phase 7 已验收候选。
- 当前发布版仍为 3.0.3，v3.1 保持 executing；Phase 8 P0-MCP 为 ready/planned。

---
*Status: passed*
