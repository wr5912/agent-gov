---
name: "runtime-env-governance"
description: "治理 agent-gov 的 runtime/env、本机 PyCharm 调试、Docker/Compose 部署、Langfuse 地址、volume 路径和治理模型凭据边界。用户提到 RUNTIME_VOLUME_MODE、RUNTIME_CONTAINER、docker/.env、docker/.env.local-debug、覆盖、PyCharm 调试、Langfuse、本机/容器模式或 MODEL_PROVIDER_API_KEY 时使用。"
---

# Runtime / Env 治理

> 本技能与 `.codex/skills/runtime-env-governance/SKILL.md` 同源镜像，修改需两侧同步。

本技能用于防止 runtime/env 改动再次出现“配置写了但不生效”“本机和容器数据混用”“把 env 文件叫覆盖”“治理模型请求缺凭据才在运行中失败”“外部 provider 降级被误报成 API 启动失败”等问题。

## 必做矩阵

执行前先写 Consumer x Mode x Boundary 矩阵：

| Consumer | Mode | Env source | Runtime root | Secret boundary | Verification |
| --- | --- | --- | --- | --- | --- |
| API container | container | 默认 `docker/.env`，自动化可由 `COMPOSE_ENV_FILE` 选择一份完整 env；Compose 注入 `RUNTIME_CONTAINER=1` | `${HOME}/volume-agent-gov`，隔离验收使用临时根 | private env, no real value in examples | `AppSettings`, Compose config sanitized check, startup log |
| Host Python / PyCharm | local-debug | `docker/.env.local-debug` selected by non-container process | `/tmp/local-debug-volume-agent-gov` by default | private `docker/.env.local-debug`, no real value in examples | settings tests, bootstrap test, startup log |
| Vite frontend dev | frontend-local | `frontend/.env.local` | none | debug UI can show full runtime input/output; `VITE_*` only in env | frontend build or browser smoke |
| Langfuse self-hosted | container profile + host browser | Compose service URL in containers, `localhost:53000` on host/browser | `${HOME}/volume-agent-gov/langfuse` | dev trace keeps full prompt/tool/job I/O; Langfuse keys stay private | health fields and docs tests |
| Governance model request | same as API process | selected runtime env file | selected runtime root | `MODEL_PROVIDER_API_KEY` required privately | auth precheck tests and main-flow test |

## 术语规则

- 这里不是 layered override。除非代码真实叠加读取多个 env 文件，否则不要写“覆盖文件”“私有覆盖”“覆盖配置”。
- 用“选择 env 文件”“本机调试 env 文件”“容器部署 env 文件”“私有 env 文件”描述当前实现。
- `COMPOSE_ENV_FILE` 只选择一份完整 Compose env，不能把它实现或描述成与 `docker/.env` 叠加。
- `RUNTIME_VOLUME_MODE` 不应出现在官方 env 示例中；模式选择由运行环境和 `RUNTIME_CONTAINER` 决定。

## 设计规则

- `.env.local-debug` 的内容不能承担“选择 local-debug”的职责；它只在已被选择后提供配置值。
- 本机后台 Agent job 不复用交互式 Claude `/login`；持久化队列退役后，API 内直接执行的治理模型请求仍遵守同一凭据边界。没有 `MODEL_PROVIDER_API_KEY` 时应在启动 Agent 前失败，并投影稳定错误码。
- local-debug 和 container env 示例应保持 Runtime/API key 同构；Compose、前端容器端口、Langfuse infra 和初始化账号只属于 container env。
- 用户要求“启动、重启、重建、部署、生效最新代码”时，默认在原 Docker Compose 容器服务中生效，优先重建/重启既有 `agent-gov-ui` / `agent-gov-api` / `agent-gov-litellm-sidecar` 服务并验证既有端口；除非用户明确要求本机调试，不另起临时 Vite 或旁路 API 服务。
- API 使用 `LOG_LEVEL` 控制应用日志级别；container 默认 `info`，local-debug 默认 `debug`。
- Compose liveness 只访问 `/health/live`，不得等待 Git、CLI、数据库或外部模型服务；provider readiness 由后台探测写入缓存，并通过 `/health/ready` 和 `/health` 返回结构化诊断。
- 真实 API key、MCP header、数据库凭据、本机私有路径和运行态 SQLite 不得提交。
- 当前前端调试界面、Playground 证据面板和自托管 Langfuse 不是生产安全边界；开发期优先保留完整 prompt、tool input/output、job input/output、raw text 和 trace I/O，不为这些面默认做脱敏、遮蔽或摘要化。需要生产化时另起 production redaction 方案，不反向污染当前 dev/debug 默认。
- 数据库和 workspace 路径必须随 mode 分离：container 默认 `${HOME}/volume-agent-gov`，local-debug 默认 `/tmp/local-debug-volume-agent-gov`。

## 验证清单

- `tests/test_settings.py` 覆盖 env 文件选择、`runtime_volume_mode`、`LOG_LEVEL`、路径派生和启动日志字段。
- `tests/test_repository_env_policy.py` 覆盖 root `.env` 禁止、官方 env 示例不含 `RUNTIME_VOLUME_MODE`、local-debug 与 container key 差异、模型 key 示例为空。
- `tests/test_documentation_contracts.py` 覆盖 README 术语、PyCharm 环境变量留空、`AGENT_AUTH_REQUIRED` 和启动日志字段说明。
- 影响治理模型主流程时运行 `make main-flow-test`；提交、发版或用户要求完整验证时运行 `make test`。
- 提交前确认 `docker/.env`、`docker/.env.local-debug`、`frontend/.env.local`、runtime volume、SQLite、logs、dist 和 cache 都未进入 staged diff。

## 测试模式选择矩阵

| 改动类型 | 测试环境 | 推荐验证命令 | 不使用 |
| --- | --- | --- | --- |
| docs / skill / README 术语同步 | 宿主机仓库环境 | `git diff --check`、`scripts/check_docs_governance.py`、`scripts/check_codex_governance.py --mode fail`、相关 skill 单测 | 不默认跑 `make test`，不使用 `local-debug` |
| settings/env 选择代码 | 宿主机仓库环境 | `tests/test_settings.py`、`tests/test_repository_env_policy.py`、`tests/test_documentation_contracts.py` | 不用 `docker/.env.local-debug` 伪装容器 |
| provider 健康降级回归 | Docker Compose 容器 | `make container-health-e2e`；隔离验收由示例生成临时 `COMPOSE_ENV_FILE`，使用真实 API/UI/LiteLLM 容器、临时 runtime 根和 Playwright | 不使用 local-debug、真实宿主卷或后端单测替代浏览器证据 |
| live 模型或真实运行态验收 | Docker Compose 容器 | `make container-live-test`，使用 Compose 注入的 `docker/.env` 和容器路径 | 不使用 `docker/.env.local-debug` |
| 启动 / 重启 / 重建 / 部署生效 | Docker Compose 既有服务 | `make ui-build && make ui-up && make ui-smoke`，必要时追加 API `/health` 与 `docker ps` | 不另起临时 Vite 服务，不用 local-debug 代替容器 |
| local-debug 专项能力 | 宿主机 Python / PyCharm | 明确命名的 local-debug 专项测试和 bootstrap/repair 命令 | 不把结果声明为容器验收 |
| 发版或用户要求完整验证 | 发布前工作区 | `make test`，必要时追加 `make container-live-test` | 不用单一 coverage 百分比替代主流程或 live 证据 |

## 配置面选择

- 常驻入口只放在唯一根 `AGENTS.md`；展开说明放 `.codex/guidance/*.md`，命令执行策略才放 `.codex/rules/*.rules`。
- 详细矩阵和 checklist 保留在本技能，避免常驻上下文膨胀。
- 可机械检查的内容优先放入 pytest 或治理脚本；不要只写成人工自报规则。
