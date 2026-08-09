---
phase: 07-per-agent-lane
status: ready-for-planning
created: 2026-08-09
requirements: [LANE-01, LANE-02, LANE-03, LANE-04, LANE-05, LANE-06, LANE-07, LANE-08]
---

# Phase 7：per-Agent 最小权限隔离测试 lane 上下文

## 阶段目标

把现有由 API 进程直接启动 pytest 的实现替换为持久化队列、独立 Docker authority worker 和一次性最小权限 sandbox。每个通过记录必须绑定精确 Agent commit、raw Git 物化结果、suite digest、固定镜像/命令、隔离检查和完整清理回执；旧的 `status=passed` 或缺失回执记录只能读取，不能继续作为发布门证据。

## 已锁定裁决

### Authority 与职责

| 对象 | 唯一 authority | 允许动作 | 禁止动作 |
| --- | --- | --- | --- |
| API | 请求契约、Agent/change set 解析、精确 commit 校验、持久化入队 | 返回 `202 queued`、读取运行与回执、设置 cancel flag | 持有 Docker socket、启动 pytest/容器、构造结果或清理证明 |
| `agent-test-worker` | durable queue claim、Docker container lifecycle、结果摄取与 cleanup | 只消费后端持久化的 typed run target | 接受客户端 command/image/env/mount/user/network/result |
| Agent Workspace Git | 指定业务 Agent 的源码 commit | 由 raw Git object 只读物化 | 使用 dirty live worktree、Git worktree/filter/hook 作为被测源码 |
| Docker daemon/inspect | 实际 sandbox 配置与生命周期 | 返回 image ID、container config/state/labels | 以计划参数或日志声明替代实际 inspect |
| typed receipt | 发布门所需隔离证据 | 保存公开、无敏感信息的 provenance/结果/cleanup | 混入开放 `report_json`、接收客户端污染、隐藏 cleanup 失败 |

### 字段所有权

- 客户端手工运行只提交 `agent_id` 与完整 40 字符 `commit_sha`；省略 commit 的旧手工请求退出。
- change-set 与 schedule 入口由后端解析并持久化相同的精确 target。
- command、image、image ID、env、mount、user、network、limits、container ID、status、report、receipt 和 cleanup 均为 backend/worker-owned。
- receipt 与 pytest report 分列存储。历史 `receipt=null` 保持只读兼容，但永远不满足 v3.1 发布门。
- 发布门校验当前 Agent、commit、suite digest、受支持 receipt contract、固定 invocation、完整 isolation 与 cleanup；`force` 不能绕过上述安全门。

### 执行与隔离

- worker 是 Compose 中唯一挂载 `/var/run/docker.sock` 的服务；API 明确不得挂载。
- worker 不使用 `env_file`，不接收 API key、模型 key、MCP header 或其他业务 secret，且自身 `network_mode: none`。
- sandbox 固定使用解析后的 image ID、固定 pytest argv、`65532:65532`、`network=none`、只读 rootfs、`cap_drop=ALL`、`no-new-privileges`、无 privileged/device/socket。
- sandbox 仅挂载当前 run 的 `/workspace:ro` 和 `/output:rw`；不挂载 `/data`、live runtime root 或其他 host path。
- env 只允许固定非敏感键；不复制 worker/API 的 `os.environ`。
- 每个 terminal path 都按 create/inspect/start/poll/log+report ingest/remove/label verify/temp cleanup/finish 顺序收口。cleanup 失败必须把 pytest 0 反转为 `error`。

### 精确 commit 与测试类型

- materializer 只允许 `rev-parse --verify`、`ls-tree -rz --full-tree`、`cat-file --batch`，并关闭 replace objects、system/global Git config、terminal prompt；不运行 checkout、worktree、archive、filter 或 hook。
- symlink、gitlink、非法 UTF-8/路径、越界、文件/字节上限都 fail-closed。
- API 创建前、worker 执行前和执行后 receipt 中的 commit/suite/source digest 必须一致。
- P0 lane 是无网络静态 Workspace suite。发现 `agent` live fixture 时整套拒绝并给出稳定诊断，不能只跑静态子集，也不能回退旧 runner；P1 live lane 在 Phase 10-11 接管该类用例。

## 配置与数据边界

- 不改变 `${HOME}/volume-agent-gov` 的产品卷布局；worker 复用 Compose 已解析的 data bind，并只在其 `.agent-testing/runs/<run-id>` 下创建一次性目录。
- 真实验收使用独立临时 runtime root、独立 Compose project/labels 和当前工作树镜像，不读写现有 live volume。
- 不新增第二份 security manifest；三类阶段 lane 都登记在 `tests/quality_policy.json`。
- 不新增版本号、tag 或远程依赖；必需闭环保持离线可用。

## 测试同步裁决

| 分类 | 处置 |
| --- | --- |
| KEEP | suite 布局/解析、exact target 去重、schedule 业务语义、session/testkit、import audit、根 collection 排除 Workspace leaf |
| REFACTOR | dirty Workspace suite inspection、API restart recovery、run enqueue、publication gate、schedule runner monkeypatch、前端 status-only 门禁 |
| DELETE-CANDIDATE | 宿主机 `Popen`/process-group timeout 实现测试，以及任何允许 force 绕过安全回执的成功测试 |
| GAP | raw Git hostile materialization、worker claim、Docker inspect、所有终态 cleanup、receipt tamper、0053 migration、真实 sandbox hostile acceptance |

## 回滚与退出

- 不保留旧 `AgentTestRunner` 或 in-process fallback。部署回滚只能回滚 API/worker 同一候选；不能恢复不安全 Popen 作为降级路径。
- worker/镜像/socket 前置缺失时 queued run 终结为稳定 `error` 并完成可完成的 cleanup；公共 lane 返回非零，不 skip。
- Phase 7 退出时，API 无执行 authority、真实安全 Workspace exact commit 在 sandbox 全绿、hostile 验收全绿、无遗留 container/temp asset，且发布门拒绝所有旧或错配回执。
