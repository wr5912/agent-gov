---
phase: 07-per-agent-lane
status: complete
created: 2026-08-09
completed: 2026-08-13
requirements: [LANE-01, LANE-02, LANE-03, LANE-04, LANE-05, LANE-06, LANE-07, LANE-08]
---

# Phase 7：per-Agent 最小权限隔离测试 lane 上下文

## 阶段目标

把现有由 API 进程直接启动 pytest 的实现替换为持久化队列、独立 Docker authority worker 和一次性最小权限 sandbox。每个通过记录必须绑定精确 Agent commit、raw Git 物化结果、suite digest、固定镜像/命令、隔离检查和完整清理回执；旧的 `status=passed` 或缺失回执记录只能读取，不能继续作为发布门证据。

阶段实施中还暴露了同一 per-Agent 权威路径上的两个崩溃一致性缺口：Workspace 覆盖/恢复激活及业务 Agent 删除。Phase 7 因此同步建立 0055 activation journal、0056 deletion journal、0057 operator recovery attempt 和用于存量卷约束刷新的 0058 authority hardening migration，并让创建、导入、测试入队、版本 writer 与删除共用 layout 之外的稳定 Agent 锁。这是原阶段安全目标的必要收口，不是新的并行产品流程。

## 已锁定裁决

### Authority 与职责

| 对象 | 唯一 authority | 允许动作 | 禁止动作 |
| --- | --- | --- | --- |
| API | 请求契约、Agent/change set 解析、精确 commit 校验、持久化入队 | 返回 `202 queued`、读取运行与回执、设置 cancel flag | 持有 Docker socket、启动 pytest/容器、构造结果或清理证明 |
| `agent-test-worker` | durable queue claim、Docker container lifecycle、结果摄取与 cleanup | 只消费后端持久化的 typed run target | 接受客户端 command/image/env/mount/user/network/result |
| Agent Workspace Git | 指定业务 Agent 的源码 commit | 由 raw Git object 只读物化 | 使用 dirty live worktree、Git worktree/filter/hook 作为被测源码 |
| Docker daemon/inspect | 实际 sandbox 配置与生命周期 | 返回 image ID、container config/state/labels | 以计划参数或日志声明替代实际 inspect |
| typed receipt | Workspace 工程卫生门所需隔离执行证据 | 保存公开、无敏感信息的 provenance/Docker 观察结果/cleanup | 冒充独立评测结论、混入开放 `report_json`、接收客户端污染、隐藏 cleanup 失败 |
| activation journal | 覆盖/恢复的 Git、audit、session 和 admission 事实 | 按精确证据完成、拒绝或保留 fence | 在证据缺失时强行终态或只因 lease 过期放行 |
| deletion journal | 已公开 Agent 的实例 CAS、tombstone、影响面和文件系统清理 | 以幂等键和 inode/mount CAS 对账 `cleanup_pending` | 客户端提交影响面、复用已公开 ID 或将 `202` 表述为清理完成 |
| operator recovery | 本机只读现场指纹和 append-only 0057 attempt | 精确 reconcile、strict-subset 缺失 ref 修复，或按既有 reserved recovery ID 续跑 | HTTP/UI 写入、force complete/reject、clear fence、任意 SHA |
| migration 0058 | 已应用旧版 0055/0057 的持久卷 authority triggers | 幂等重装完整转移、不可变、禁止删除与终态证据约束 | 新建第二状态表、手工改行或要求重放旧 migration |

### 字段所有权

- 客户端手工运行只提交 `agent_id` 与完整 40 字符 `commit_sha`；省略 commit 的旧手工请求退出。
- change-set 与 schedule 入口由后端解析并持久化相同的精确 target。
- command、image、image ID、env、mount、user、network、limits、container ID、平台状态投影、receipt 和 cleanup 均为 backend/worker-owned；pytest report/items/invocations 是 Agent-owned unverified diagnostics。
- receipt 与 pytest report 分列存储。receipt 固定 `assurance_level=execution_provenance`，report 固定 `workspace_report_authority=agent_owned_unverified`；历史 `receipt=null` 保持只读兼容，但永远不满足 v3.1 Workspace 工程卫生门。
- receipt 只证明精确提交在固定隔离环境中按固定命令执行及 Docker 观察结果，不等于独立证明业务正确性、能力质量或真实 Agent 行为；独立结论由 Phase 10 evaluator-owned holdout 产生。
- 发布门校验当前 Agent、commit、suite digest、受支持 receipt contract、固定 invocation、完整 isolation 与 cleanup；`force` 不能绕过上述安全门。

### 执行与隔离

- worker 是 Compose 中唯一挂载 `/var/run/docker.sock` 的服务；API 明确不得挂载。
- worker 不使用 `env_file`，不接收 API key、模型 key、MCP header 或其他业务 secret，且自身 `network_mode: none`。
- sandbox 固定使用解析后的 image ID、`/usr/local/bin/python -I -P -m pytest -q --import-mode=importlib -p agentgov_testkit.pytest_plugin tests`、`65532:65532`、`network=none`、只读 rootfs、`cap_drop=ALL`、`no-new-privileges`、无 privileged/device/socket；`-I -P` 阻止业务 commit 根目录的 `pytest.py` 或 `pytest/` 抢占平台 pytest，`--import-mode=importlib` 避免 pytest 把测试目录前插到 `sys.path`。
- worker 独占 Compose 声明的 Docker-managed local named volume `agent-test-runs`；sandbox 复用同一 volume identity，只将当前 run 的 workspace subpath 以 `/workspace:ro` 挂载，不向 daemon 传递可被别名重解析的 host path。`/output` 与 `/tmp` 都是限额 tmpfs，不挂载 `/data`、live runtime root 或其他 host path。平台 pytest 插件把 tmpfs 中的同一份单行 JSON 通过稳定 stdout envelope 发出，worker 只 tail 读取并严格限长、校验和剔除该 envelope，不把 `/output` 映射回 host；该报告仍是 `agent_owned_unverified`。
- env 只允许固定非敏感键；不复制 worker/API 的 `os.environ`。
- 每个 terminal path 都按 create/inspect/start/poll/log+report ingest/remove/label verify/temp cleanup/finish 顺序收口。cleanup 失败必须把 pytest 0 反转为 `error`。

### 精确 commit 与测试类型

- materializer 只允许 `rev-parse --verify`、`ls-tree -rz --full-tree`、`cat-file --batch`，并关闭 replace objects、system/global Git config、terminal prompt；不运行 checkout、worktree、archive、filter 或 hook。
- symlink、gitlink、非法 UTF-8/路径、越界、文件/字节上限都 fail-closed。
- API 创建前、worker 执行前和执行后 receipt 中的 commit/suite/source digest 必须一致。
- P0 lane 是无网络静态 Workspace suite。发现 `agent` live fixture 时整套拒绝并给出稳定诊断，不能只跑静态子集，也不能回退旧 runner；P1 live lane 在 Phase 10-11 接管该类用例。
- activation journal 在 `prepared` 后冻结 original/base/candidate/target 与 original-index graph identity；
  canonical object type、唯一 parent/tree 关系、restore target 和 `assume-unchanged`/`skip-worktree`/fsmonitor
  index flags 由共享 Git authority 校验。该 authority 使用绝对 Git executable 且显式固定
  `--git-dir` / `--work-tree`，清除继承的 `GIT_*`，禁用 replace objects、lazy fetch、system/global
  config、hook 与仓库 fsmonitor command，并拒绝 repository-local 外部命令驱动、`core.worktree`、
  object alternates、非规范 `commondir`、grafts 和 partial clone/promisor。commit tree/parents 只从 raw
  object header 解析；operation refs 必须为 direct refs，HEAD 不得指向 activation namespace。核心
  reconciler 和只读 operator 不得形成两套解释。
- 0057 completed/failed attempt 使用互斥的精确 result/error 结构；raw SQL、投影与 resume 都拒绝空、冲突、
  额外字段或类型伪造的终态证据。

## 配置与数据边界

- 不改变 `${HOME}/volume-agent-gov` 的业务持久卷布局；worker 复用 Compose 已解析的 data bind，另独占只保存一次性 run 投影、没有业务真源地位的 named volume。
- stable per-Agent lock、fence 与 inode/no-follow CAS 只约束平台内 writer 和检查点竞态；拥有 runtime volume 同 UID/root 权限的宿主进程不在应用层威胁边界内，由主机与 Compose 运维权限控制。
- 历史草案（已退出）曾计划在 agent-test profile 用 `RUNTIME_BOOTSTRAP_HOST_DIR` 把宿主机 `docker/runtime-bootstrap` 只读 bind 给隔离 API；该方案会绕过 frozen image/source authority，不属于 Phase 7 最终契约。
- 当前冻结实现只通过 `make container-workspace-pytest-test` 从当前工作树构建 API/worker/sandbox 镜像，`runtime-bootstrap` 已包含在候选镜像内；Compose 与 runner 拒绝宿主 bind 和 `RUNTIME_BOOTSTRAP_HOST_DIR`。验收仍使用独立临时 runtime root、Compose project/labels 与 named volume，不读写现有 live volume。
- 不新增第二份 security manifest；三类阶段 lane 都登记在 `tests/quality_policy.json`。
- 不新增版本号、tag 或远程依赖；必需闭环保持离线可用。

## 测试同步裁决

| 分类 | 处置 |
| --- | --- |
| KEEP | suite 布局/解析、exact target 去重、schedule 业务语义、session/testkit、import audit、根 collection 排除 Workspace leaf |
| REFACTOR | dirty Workspace suite inspection、API restart recovery、run enqueue、publication gate、schedule runner monkeypatch、前端 status-only 门禁 |
| DELETE-CANDIDATE | 宿主机 `Popen`/process-group timeout 实现测试，以及任何允许 force 绕过安全回执的成功测试 |
| GAP | raw Git hostile materialization、worker claim、Docker inspect、所有终态 cleanup、receipt tamper、0053 migration、真实 sandbox hostile acceptance |

Phase 7 扩展实施后，`GAP` 还包括 0055 终态 audit/admission/bytes 不变量与 commit ack-loss、0056
删除 blocker/隔离/竞态/永久 ID，以及 0057 不可信 Git 配置、attempt ack-loss、reserved resume、
operator/periodic 与 apply/resume 竞态和无 force 表面；
它们已转为行为测试与 `tests/quality_policy.json` 主流程绑定，不只停留在阶段总结。

最终对抗性复核又补充了 graph identity 篡改、非规范对象类型/merge parent、replace ref、继承
`GIT_INDEX_FILE`、fsmonitor flag/hook、外部 Git 驱动、仓库作用域/对象图重定向、raw commit
tree/parents、direct operation refs/HEAD topology、腐败 0057 terminal evidence 和旧卷遗漏新 trigger 约束；
这些已分别进入 `tests/test_agent_git_authority.py`、`tests/test_runtime_db_0058.py`、
`tests/test_agent_workspace_activation_recovery_resilience.py`、同一主流程场景和 `workspace-activation-recovery`
TIA 规则，而不是新增并行测试清单。

## 回滚与退出

- 不保留旧 `AgentTestRunner` 或 in-process fallback。部署回滚只能回滚 API/worker 同一候选；不能恢复不安全 Popen 作为降级路径。
- worker/镜像/socket 前置缺失时 queued run 终结为稳定 `error` 并完成可完成的 cleanup；公共 lane 返回非零，不 skip。
- Phase 7 退出时，API 无执行 authority、真实安全 Workspace exact commit 在 sandbox 全绿、hostile 验收全绿、无遗留 container/temp asset，且发布门拒绝所有旧或错配回执。
- activation 只在精确终态证据完整时释放 fence；删除只在 quarantine/purge 持久确认后声称 completed；
  已公开 ID 不复用；operator 恢复仅有本机 exact 入口且全部不变量及竞态测试通过。
