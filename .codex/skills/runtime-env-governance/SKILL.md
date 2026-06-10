---
name: "runtime-env-governance"
description: "治理 agent-gov 的 runtime/env、本机 PyCharm 调试、Docker/Compose 部署、Langfuse 地址、volume 路径和后台 Agent job 模型凭据边界。用户提到 RUNTIME_VOLUME_MODE、RUNTIME_CONTAINER、docker/.env、docker/.env.local-debug、覆盖、PyCharm 调试、Langfuse、本机/容器模式或 MODEL_PROVIDER_API_KEY 时使用。"
---

# Runtime / Env 治理

本技能用于防止 runtime/env 改动再次出现“配置写了但不生效”“本机和容器数据混用”“把 env 文件叫覆盖”“Agent job 缺模型凭据才在运行中失败”等问题。

## 必做矩阵

执行前先写 Consumer x Mode x Boundary 矩阵：

| Consumer | Mode | Env source | Runtime root | Secret boundary | Verification |
| --- | --- | --- | --- | --- | --- |
| API / worker container | container | `docker/.env` + Compose `RUNTIME_CONTAINER=1` | `${HOME}/volume-agent-gov` | private `docker/.env`, no real value in examples | `AppSettings`, Compose config sanitized check, startup log |
| Host Python / PyCharm | local-debug | `docker/.env.local-debug` selected by non-container process | `/tmp/local-debug-volume-agent-gov` by default | private `docker/.env.local-debug`, no real value in examples | settings tests, bootstrap test, startup log |
| Vite frontend dev | frontend-local | `frontend/.env.local` | none | `VITE_*` only | frontend build or browser smoke |
| Langfuse self-hosted | container profile + host browser | Compose service URL in containers, `localhost:53000` on host/browser | `${HOME}/volume-agent-gov/langfuse` | Langfuse keys stay private | health fields and docs tests |
| Background Agent job | same as API/worker process | selected runtime env file | selected runtime root | `MODEL_PROVIDER_API_KEY` required privately | auth precheck tests and main-flow test |

## 术语规则

- 这里不是 layered override。除非代码真实叠加读取多个 env 文件，否则不要写“覆盖文件”“私有覆盖”“覆盖配置”。
- 用“选择 env 文件”“本机调试 env 文件”“容器部署 env 文件”“私有 env 文件”描述当前实现。
- `RUNTIME_VOLUME_MODE` 不应出现在官方 env 示例中；模式选择由运行环境和 `RUNTIME_CONTAINER` 决定。

## 设计规则

- `.env.local-debug` 的内容不能承担“选择 local-debug”的职责；它只在已被选择后提供配置值。
- 本机后台 Agent job 不复用交互式 Claude `/login`；没有 `MODEL_PROVIDER_API_KEY` 时应在启动 Agent 前失败，并投影稳定错误码。
- local-debug 和 container env 示例应保持 Runtime/API/worker key 同构；Compose、前端容器端口、Langfuse infra 和初始化账号只属于 container env。
- API 与 worker 应统一使用 `LOG_LEVEL` 控制应用日志级别；container 默认 `info`，local-debug 默认 `debug`，不要新增 worker 专属日志级别变量。
- 真实 API key、MCP header、数据库凭据、本机私有路径和运行态 SQLite 不得提交。
- 数据库和 workspace 路径必须随 mode 分离：container 默认 `${HOME}/volume-agent-gov`，local-debug 默认 `/tmp/local-debug-volume-agent-gov`。

## 验证清单

- `tests/test_settings.py` 覆盖 env 文件选择、`runtime_volume_mode`、`LOG_LEVEL`、路径派生和启动日志字段。
- `tests/test_repository_env_policy.py` 覆盖 root `.env` 禁止、官方 env 示例不含 `RUNTIME_VOLUME_MODE`、local-debug 与 container key 差异、模型 key 示例为空。
- `tests/test_documentation_contracts.py` 覆盖 README 术语、PyCharm 环境变量留空、`AGENT_AUTH_REQUIRED` 和启动日志字段说明。
- 影响 Agent job 时运行 `make main-flow-test`；提交、CI、发版或用户要求完整验证时运行 `make test`。
- 提交前确认 `docker/.env`、`docker/.env.local-debug`、`frontend/.env.local`、runtime volume、SQLite、logs、dist 和 cache 都未进入 staged diff。

## 配置面选择

- 常驻入口只放在 `AGENTS.override.md` 和 `.codex/rules/*.rules`。
- 详细矩阵和 checklist 保留在本技能，避免常驻上下文膨胀。
- 可机械检查的内容优先放入 pytest 或治理脚本；不要只写成人工自报规则。
