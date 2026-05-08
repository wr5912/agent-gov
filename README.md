# Claude Agent Runtime API

一个极简的 **Docker 化 Claude Agent Runtime API** 项目。

目标：

- 不重写 Claude Agent loop。
- 通过 Docker 容器封装 Claude Agent SDK / Claude Code Runtime。
- 容器内 Claude Code 配置路径与原生 Claude Code 保持一致：`/root/.claude/*`、`/root/.claude.json`、`/workspace/*`。
- 容器对外提供 HTTP API，供 Web UI、业务系统、Agent 平台控制面调用。

## 目录结构

```text
.
├── app/
│   ├── main.py                         # FastAPI API
│   └── runtime/
│       ├── claude_runtime.py            # Claude Agent SDK 适配层
│       ├── agent_loader.py              # 从 .claude/agents 解析 subagents
│       ├── policy.py                    # SDK hook / tool permission guard
│       ├── session_store.py             # API session -> Claude SDK session 映射
│       └── schemas.py
├── docker/
│   ├── Dockerfile
│   ├── Dockerfile.dockerignore
│   ├── docker-compose.yml
│   ├── .env.example
│   ├── .env                           # 本地环境变量，不提交
│   └── volume/
│       ├── workspace/
│       │   ├── CLAUDE.md                # 主 Agent 指令
│       │   ├── CLAUDE.local.md          # 本地私有指令，默认 gitignored
│       │   ├── agent.yaml               # 平台自定义元配置
│       │   ├── .mcp.json                # 项目级 MCP 配置
│       │   ├── .worktreeinclude         # Claude Code worktree 复制规则
│       │   ├── .claude/
│       │   │   ├── settings.json        # Claude Code 权限配置
│       │   │   ├── settings.local.json  # 本地私有配置，默认 gitignored
│       │   │   ├── agents/              # subagents
│       │   │   ├── skills/              # skills
│       │   │   ├── commands/            # custom commands
│       │   │   ├── rules/
│       │   │   └── output-styles/
│       │   ├── hooks/                   # 可选外部 hook 脚本
│       │   └── mcp_servers/             # 示例 MCP server
│       ├── claude-root/
│       │   ├── .claude/                 # 用户级 Claude Code 配置
│       │   └── .claude.json             # Claude Code 全局状态，不提交
│       └── data/
│           ├── sessions/                # API session -> Claude SDK session 映射
│           ├── transcripts/             # 预留 transcript 持久化目录
│           ├── uploads/                 # 预留用户上传文件目录
│           ├── outputs/                 # 预留 Agent 输出文件目录
│           └── agent-memory/            # 预留 API 侧 Agent 记忆/缓存目录
└── requirements.txt
```

## 快速启动

```bash
make setup
```

编辑 `docker/.env`：

```bash
MODEL_PROVIDER_API_KEY=sk-ant-xxxx
API_KEY=change-me
HOST_PORT=58080
API_PORT=8080
AGENT_MODEL=claude-sonnet-4-5
```

`docker/.env.example` 已包含端口、模型提供商、Claude Agent SDK 运行参数、路径、权限、skills、MCP、hooks、session 等配置项的注释。默认端口映射为 `58080:8080`，符合项目端口规则 `50000 + 容器端口`。

Docker 构建默认使用国内镜像源：Debian apt 使用阿里源，uv/pip 使用阿里 PyPI 源，npm 使用 npmmirror。需要切换源时修改 `docker/.env` 中的 `PYTHON_IMAGE`、`APT_MIRROR`、`APT_SECURITY_MIRROR`、`PIP_INDEX_URL`、`PIP_TRUSTED_HOST`、`NPM_REGISTRY`。`PYTHON_IMAGE` 默认使用官方 Python 镜像；如果你的环境提供国内基础镜像，可在 `docker/.env` 中覆盖。

为减少 bind mount 权限问题，Compose 中的 API 容器默认以 root 运行，方便直接写入 `docker/volume/data/`、`docker/volume/claude-root/` 和其他挂载目录。生产环境如果需要收紧权限，可以再切换到非 root 用户并配套处理宿主机目录 owner/ACL。

启动：

```bash
make build
make up
make logs
```

健康检查：

```bash
make smoke
```

## API 文档

容器启动后，FastAPI 自动提供详细 OpenAPI 文档：

- Swagger UI: `http://localhost:58080/docs`
- ReDoc: `http://localhost:58080/redoc`
- OpenAPI JSON: `http://localhost:58080/openapi.json`

如果你在 `docker/.env` 中修改了 `HOST_PORT`，把上面的 `58080` 替换成对应端口。`/health` 响应也会返回这些文档 URL。
当 `API_KEY` 非空时，Swagger UI 里先点击 `Authorize`，输入 `docker/.env` 中的 `API_KEY`；curl 请求则添加 `Authorization: Bearer <API_KEY>`。

## 聊天 API

```bash
export API_BASE=http://localhost:58080

curl -X POST "$API_BASE/api/chat" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer change-me" \
  -d '{
    "message": "请说明当前 workspace 中有哪些 subagents 和 skills",
    "skills_mode": "all"
  }'
```

指定 subagent 和 skill：

```bash
curl -X POST "$API_BASE/api/chat" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer change-me" \
  -d '{
    "agent": "security-triage",
    "skills": ["threat-triage"],
    "message": "分析这个告警：rundll32.exe 加载 WININET.dll，父进程为 EdgeUpdate，命令行为 DispatchAPICall 1",
    "allowed_tools": ["Read", "Grep", "Glob"],
    "permission_mode": "dontAsk"
  }'
```

流式接口：

```bash
curl -N -X POST "$API_BASE/api/chat/stream" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer change-me" \
  -d '{"message":"你好，先介绍你的能力", "skills_mode":"all"}'
```

## OpenAI Compatible 简易接口

项目额外提供了一个最小的非流式 OpenAI Compatible shim：

```bash
curl -X POST "$API_BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer change-me" \
  -d '{
    "model": "claude-sonnet-4-5",
    "messages": [
      {"role": "user", "content": "请介绍当前 Agent Runtime 的能力"}
    ]
  }'
```

注意：这是兼容接入用的轻量 shim，不是完整 OpenAI API 实现；真正的 Agent 参数，如 `agent`、`skills`、`allowed_tools`，建议使用 `/api/chat`。

## 管理 API

```bash
curl -H "Authorization: Bearer change-me" "$API_BASE/api/agents"
curl -H "Authorization: Bearer change-me" "$API_BASE/api/skills"
curl -H "Authorization: Bearer change-me" "$API_BASE/api/config"
curl -H "Authorization: Bearer change-me" "$API_BASE/api/sessions"
```

## 配置挂载说明

`docker/docker-compose.yml` 默认挂载：

```yaml
volumes:
  - ./volume/workspace:/workspace
  - ./volume/data:/data
  - ./volume/claude-root:/root
```

你可以只改宿主机目录，不需要改镜像：

- `docker/volume/claude-root/.claude/settings.json`
- `docker/volume/claude-root/.claude/CLAUDE.md`
- `docker/volume/claude-root/.claude/agents/*.md`
- `docker/volume/claude-root/.claude/skills/*/SKILL.md`
- `docker/volume/claude-root/.claude.json`
- `docker/volume/workspace/CLAUDE.md`
- `docker/volume/workspace/CLAUDE.local.md`
- `docker/volume/workspace/.claude/settings.json`
- `docker/volume/workspace/.claude/settings.local.json`
- `docker/volume/workspace/.claude/agents/*.md`
- `docker/volume/workspace/.claude/skills/*/SKILL.md`
- `docker/volume/workspace/.claude/commands/*.md`
- `docker/volume/workspace/.claude/rules/*`
- `docker/volume/workspace/.claude/output-styles/*.md`
- `docker/volume/workspace/.mcp.json`
- `docker/volume/workspace/.worktreeinclude`
- `docker/volume/workspace/agent.yaml`

`docker/volume/data/` 中的运行态文件默认不提交到 git；仓库只保留 `.gitkeep` 以固定目录结构。

## subagent 文件格式

示例：`docker/volume/workspace/.claude/agents/security-triage.md`

```markdown
---
name: security-triage
description: 用于分析安全告警、日志、IOC、资产上下文，并给出处置建议。
tools: Read, Grep, Glob
model: sonnet
permissionMode: dontAsk
maxTurns: 8
skills:
  - threat-triage
  - ocsf-mapping
memory: project
---

# Role

你是一个安全运营告警研判子 Agent。
```

默认情况下，项目依赖 Claude Code 原生发现机制加载 `.claude/agents/*.md`。如需 SDK-only 显式注入，可把 `docker/.env` 中的 `ENABLE_PROGRAMMATIC_AGENTS` 改为 `true`。

## skill 文件格式

示例：`docker/volume/workspace/.claude/skills/threat-triage/SKILL.md`

```markdown
---
name: threat-triage
description: 当用户需要分析安全告警、IOC、攻击链、主机行为、进程链或安全工单时使用。
allowed-tools:
  - Read
  - Grep
  - Glob
---

# Threat Triage Skill

## 工作流程

1. 识别输入数据类型。
2. 提取关键实体。
3. 判断攻击阶段。
4. 组织证据链。
5. 给出风险等级和处置建议。
```

## 权限与安全

`SKILL.md` 的 `allowed-tools` 字段兼容 Claude Code CLI。通过 Agent SDK 调用时，最终工具边界以请求中的 `allowed_tools` 和 `docker/.env` 的 `DEFAULT_ALLOWED_TOOLS` 为准。

默认 `docker/.env.example` 中设置：

```bash
DEFAULT_ALLOWED_TOOLS=Read,Grep,Glob
DEFAULT_DISALLOWED_TOOLS=Bash,WebFetch,WebSearch
PERMISSION_MODE=dontAsk
ENABLE_POLICY_HOOKS=true
```

这意味着：

- 默认只允许读文件、搜索文件。
- 默认不允许 Bash、WebFetch、WebSearch。
- 任何未预先允许的工具不会弹交互式确认，而是拒绝。

`app/runtime/policy.py` 还提供了 SDK 级 PreToolUse hook，用于阻断高危 Bash 命令。

不要把宿主机敏感目录挂入容器，例如：

```yaml
# 不要这样做
- /:/host
- /var/run/docker.sock:/var/run/docker.sock
- ~/.ssh:/root/.ssh
```

## 会话机制

API 维护一个轻量 JSON session store：

```text
docker/volume/data/sessions/*.json
```

它保存：

- API 层 `session_id`
- Claude SDK 返回的 `sdk_session_id`
- 创建/更新时间
- turns

下一次请求传入同一个 `session_id` 时，运行时会尝试使用 SDK `resume` 继续 Claude Code 会话。

## 生产化建议

这个项目是 MVP，不是完整企业平台。生产化前建议补充：

1. 更严格的鉴权和租户隔离。
2. 每个租户独立 workspace/data volume。
3. 独立 MCP Gateway，不让 Agent 直连高危 MCP。
4. OpenTelemetry / Langfuse / Phoenix trace。
5. 高危工具 Human-in-the-loop 审批。
6. 上传文件病毒扫描和敏感信息检测。
7. 容器 seccomp/AppArmor/gVisor/Firecracker 隔离。
8. Agent package 签名与审核机制。

## 本地开发

```bash
make setup
source .venv/bin/activate
export WORKSPACE_DIR=$PWD/docker/volume/workspace
export DATA_DIR=$PWD/docker/volume/data
export CLAUDE_HOME=$PWD/docker/volume/claude-root/.claude
.venv/bin/python -m uvicorn app.main:app --reload --host "${API_HOST:-127.0.0.1}" --port "${API_PORT:-8080}"
```
