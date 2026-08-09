---
phase: 07-per-agent-lane
status: complete
researched: 2026-08-09
---

# Phase 7：per-Agent 最小权限隔离测试 lane 研究

## 当前实现证据

1. `app/agent_testing/runner.py` 在 API 进程内创建单线程 executor，并以 `subprocess.Popen` 直接运行 pytest。
2. `_test_environment()` 从 `dict(os.environ)` 开始构造执行环境，还主动加入 API base 与 API key；测试进程因此继承 API/模型/MCP 等不相关 secret。
3. checkout 使用 `git worktree add`，会消费 live repository/worktree 配置；suite inspection 对当前 commit 直接扫描 live Workspace，dirty 文件可能改变同一 commit 的 digest。
4. cleanup 异常只写 warning，不反转已保存的 pytest 结果；`status=passed + agent_id + commit_sha + suite_digest` 即可进入当前发布门。
5. API startup/shutdown 负责 reconcile、requeue、cancel pytest，说明执行 authority 与 Web/API 生命周期错误耦合。
6. Compose API 读取完整 `docker/.env` 并挂载可写 `/data`。当前没有独立 test worker；给 API 增加 Docker socket 会把完整 runtime secret 与 host authority 聚合到同一容器。
7. `AgentTestRunResponse.report` 是开放 JSON，没有隔离、镜像和清理的 typed receipt；数据库也没有对应列。
8. `tests/quality_policy.json` 的 Lane 只有 id/description/enforcement，无法表达 LANE-06 要求的 owner/capabilities/resources。

## 推荐架构

```text
client
  -> API validate(agent, full commit) -> SQLite queued row
                                        |
                                        v
                          agent-test-worker (only Docker authority)
                            -> raw Git object materializer
                            -> create + inspect one-shot sandbox
                            -> poll/cancel/timeout + bounded ingest
                            -> remove container + temp paths + label audit
                            -> atomic terminal row + typed receipt
                                        |
                                        v
                         publication gate validates current receipt
```

### Raw Git 物化

- 用绝对 `git` binary 和固定最小环境运行命令，不继承 Git config/env。
- `rev-parse` 证明请求确为该 repository 内的 commit；`ls-tree -rz` 获得完整 typed tree；`cat-file --batch` 读取 blob。
- 在写文件前校验所有 entry，拒绝 symlink/gitlink/异常 mode/路径/体积，避免部分物化后才发现 hostile entry。
- 按 path/mode/content 计算 source digest；再由当前 suite inspector 计算 tests digest。API 与 worker 各自独立重算并比较。

### Docker Engine 边界

- 复用已安装的 `httpx` 通过 Unix domain socket 调 Docker Engine HTTP API，避免新增 Docker SDK 或 CLI 依赖。
- 先按 tag inspect 镜像并取得 immutable image ID；create 请求和 receipt 只使用 ID。
- container create 后、start 前读取 Docker inspect 并逐字段验证实际配置。任何 daemon 默认值或改写导致不一致时先清理再失败。
- 不采用 `AutoRemove`，因为平台必须在终态读取 state/log 并独立证明 remove；所有容器用 run-specific label 便于 crash recovery 与零残留检查。

### Durable worker

- API 只插入 queued row；worker 以条件更新原子 claim，多个 worker 竞争时只有一个从 queued 进入 running。
- cancel 只设置 durable flag；持有 container authority 的 worker 执行 terminate/kill/remove。
- worker restart 先按 label 清理遗留 sandbox，再把对应 running row终结为 `interrupted`；API restart 不改变 queued/running ownership。
- report/log 限长摄取。receipt 使用 strict typed model 与 canonical SHA-256，和 report 分列持久化。

### 发布门

一个 run 只有同时满足以下条件才 eligible：

- terminal `passed`；
- receipt contract 受支持且 canonical digest 自洽；
- receipt target 与 row/current Agent commit、suite digest 一致；
- source materialization pre/post digest 一致；
- image ID、固定 argv 和 safe env fingerprint 完整；
- Docker isolation assertions 全部为真；
- container/temp/label cleanup 全部成功；
- 非历史 `receipt=null`，且没有 force bypass。

## 被拒绝的方案

| 方案 | 拒绝原因 |
| --- | --- |
| API 直接挂 Docker socket | 将完整 env、live `/data`、HTTP attack surface 与 host authority 聚合，blast radius 不可接受 |
| 在 API 内使用 bubblewrap/subprocess | 仍由 API 持有执行与取消 authority，且不能提供 Docker image/inspect/cleanup 证据 |
| worker 继续 `git worktree add` | 会消费 repository config/hooks/filter，且共享 worktree lifecycle 与 candidate governance 冲突 |
| 复制 live Workspace 后运行 | dirty/未跟踪文件可污染 exact commit，无法证明 provenance |
| sandbox 访问 API 以运行 `agent` fixture | 与 `network=none`、无 API key 不变量冲突；会把 P1 live 评测偷渡进 P0 |
| cleanup best effort | 允许通过记录与 host 残留同时存在，receipt 不可信 |
| 把 receipt 塞进 report JSON | 无法严格校验 backend-owned 字段，也不能区分历史 pytest payload 与安全证据 |
| 保留旧 runner 作为 Docker 缺失 fallback | 让最危险环境在前置缺失时反而被启用，违反 LANE-08 fail-closed |

## 主要实现文件

- 新增：`execution_contracts.py`、`materializer.py`、`docker_engine.py`、`container_executor.py`、`worker.py`。
- 替换：删除 `runner.py` 活跃路径；重构 service/router/main/store/models/suite。
- 持久化：`runtime_db_migrations_0053.py` 与 runtime migration registry。
- 部署：`service_launcher.py`、`docker/docker-compose.yml`、公共 Make/acceptance script。
- 契约：OpenAPI schemas、生成前端类型、quality policy Lane model/manifest。
- 测试：materializer、execution contracts、worker/store、container executor、migration、Compose 与真实 Docker hostile acceptance。

## 风险与缓解

- Docker socket 本身等价 host authority：只给无网络、无 env file 的专用 worker，并把所有输入收敛为持久化 typed target 与固定 executor contract。
- SQLite 与 worker 并发：claim 用状态条件更新和现有 SQLite write transaction；terminal write 只允许当前 running owner。
- Docker daemon API 漂移：只使用稳定的 image/container create/inspect/start/log/kill/remove/list surface，保留可诊断 HTTP status，真实容器门验证当前 daemon。
- 旧数据兼容：migration 只加 nullable provenance/receipt/worker 字段，不改历史 report；gate 明确拒绝 null。
- 用户可见行为：手工 run 要求精确 commit；前端从当前 suite/target 传值。OpenAPI 与生成类型同次更新。
