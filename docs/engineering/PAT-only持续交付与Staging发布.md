# PAT-only 持续交付与 Staging 发布

本文是 `agent-gov` 当前 staging 发布控制面的工程契约。目标链路是：用户合并
`master` → 228 上的发布控制器确认 AID 与 `quality-gate` → 按精确 SHA 发布到
`172.16.112.232` → 健康验证 → 成功留痕或立即回滚。

## 边界

- 不配置原生 GitHub App，也不设置 `GITHUB_APP_SLUG` 或
  `GITHUB_WEBHOOK_SECRET`。Multica Web 显示 GitHub App 未配置属于预期现象。
- GitHub App 原有的 webhook/Issue 自动关联不参与本链路；控制器每 30 秒使用
  PAT 只出站轮询 GitHub API。
- 228 不注册为 GitHub self-hosted runner。公共仓库的 PR 代码不会在内网 runner
  上由 GitHub 事件直接执行。
- 用户从 GitHub 合并 `master` 是 staging 发布批准。控制器不能合并 PR。
- 自动回滚只切换代码和镜像，不反向修改数据。数据库演进必须保持前后版本兼容。

## Consumer × Mode × Boundary

| Consumer | Mode | 配置来源 | 数据边界 | 凭据边界 |
| --- | --- | --- | --- | --- |
| 228 发布控制器 | systemd oneshot/timer | `/etc/agent-gov-release-controller/controller.env` | `/var/lib/agent-gov-release-controller/state` | PAT 只由 systemd `LoadCredential` 注入 |
| 发布构建 | 精确 `master` SHA | 当前提交的 Compose 文件；本地选择一份完整 build env | Docker 本地镜像缓存 | 不继承 PAT 或 Multica secret |
| 232 Compose | staging container | `<release-root>/shared/docker.env` | `${HOME}/volume-agent-gov` | 私有 env 不复制进 release |
| Release SRE | 只读/受控恢复 | `releasectl` | 控制器状态和远端 release manifest | 不获得 PAT，不直接 SSH |

## GitHub 触发契约

每个 PR 的分支、标题和正文合并计算后必须且只能出现一个 `AID-N`；大小写会归一化。
`governance/quality-gate` 同时要求该元数据门、静态治理、后端全量和前端 UI 通道成功。

控制器首次启动只把当前 `master` 记录为游标，不补发历史版本。此后若两次轮询之间有多个
合并，它会审计游标到最新 head 的每一个提交，但只发布最新组合 head，避免把已经被后续
合并取代的中间版本逐个部署。只有同时满足下列条件才发布：

1. 提交来自 `master` 的严格向前历史；
2. 仓库只允许 squash merge，`master` 严格保护且必须通过 `quality-gate`；
3. lineage 中每个提交都是恰好一个已合并 PR 的最终 SHA，且合并者在允许清单中；
4. 每个 PR 关联恰好一个 AID；
5. `.github/workflows/governance.yml` 在 `master` push 事件、精确 head SHA 上的唯一
   `quality-gate` job 已成功；
6. `repository + commit SHA + environment` 尚未发布。

直接推送、无 AID、多个 AID、失败 CI、重复事件和非严格分支历史均 fail closed。

## 发布接口

预检与发布：

```bash
scripts/deploy_agent_gov_to_host \
  --preflight-only \
  --ref <40位master提交SHA> \
  --host 172.16.112.232 \
  --environment staging-232

scripts/deploy_agent_gov_to_host \
  --ref <40位master提交SHA> \
  --host 172.16.112.232 \
  --environment staging-232
```

诊断与把指定 release 重新激活为当前版本：

```bash
releasectl status
releasectl diagnose staging-232-<12位短SHA>
releasectl rollback staging-232-<12位短SHA> --approved-by <操作人>
```

退出码：`0` 表示健康发布或成功恢复；`1` 表示参数、预检、凭据或传输失败；`2`
表示新发布失败但旧版本已恢复；`3` 表示发布和恢复均失败。

每个 release 位于 `<release-root>/releases/<release-id>`，包括精确代码、镜像归档、
`.app-version` 和 `release.json`。`current` 是指向健康 release 的原子符号链接，
`shared/docker.env` 是唯一目标 Compose 私有 env。首次受管发布会把已有部署导入为
`legacy-bootstrap`，连同当时的完整镜像归档一起作为首个回滚点。目标既没有
`shared/docker.env`、也没有旧部署 `docker/.env` 时会直接失败，不会拿示例配置冒充
私有运行配置。

## 安装控制器

聊天或 Issue 中出现过的 PAT 必须先在 GitHub 撤销。新 PAT 只能在 228 本地写入临时
私有文件，再由管理员安装为 systemd credential；不得作为命令参数。

```bash
sudo install -d -m 700 /etc/agent-gov-release-controller
sudo install -m 600 /本机私有路径/新PAT文件 \
  /etc/agent-gov-release-controller/github_token

sudo scripts/install_agent_gov_release_controller \
  --multica-config "$HOME/.multica/profiles/luopeng/config.json" \
  --ssh-private-key "$HOME/.ssh/id_ed25519" \
  --ssh-known-hosts "$HOME/.ssh/known_hosts"
```

安装器创建 `agent-gov-release` 系统用户、复制 Multica 专属 profile、使用公共 URL
克隆 `master`、安装 timer 并立即执行一次只初始化游标的轮询。SSH 使用预先确认的
`known_hosts`，禁止 `accept-new`。安装前必须先在 GitHub 仓库设置中完成：仅允许
squash merge、关闭 merge commit/rebase merge、保护 `master`、严格要求
`quality-gate`、要求 PR、保护规则覆盖管理员并禁止 force push/删除；否则首次轮询会
fail closed，timer 不会被启用。`AGENT_GOV_ALLOWED_MERGERS` 必须列出获准合并者。

检查运行态：

```bash
systemctl status agent-gov-release-controller.timer
journalctl -u agent-gov-release-controller.service --since today
releasectl status
```

## 凭据与审计

- 控制器读取 PAT 后，不向部署脚本、`multica` 子进程、日志或 manifest 传播
  `GITHUB_TOKEN`、`GH_TOKEN` 或 `CREDENTIALS_DIRECTORY`。
- release 日志和 SQLite 状态目录权限为 `0700/0600`。
- GitHub PAT 只需要读取仓库、分支保护和 Actions 运行信息；控制器不创建 GitHub
  Deployment，也不写 Issue。SQLite 原子记录 SHA、环境、游标和待发送通知；Multica
  AID 评论记录 release、PR、CI 与回滚状态。通知失败会留在 durable outbox 重试，发布
  失败时 AID 保持进行中。
- Release SRE 子任务通过父 Issue 的 `release_sre_issue_id` 元数据精确定位；控制器只把
  `backlog` 推进到 `todo`，不会对已在运行或已完成的任务执行 `rerun`。
- 使用个人 PAT 时，GitHub 审计身份仍显示为个人账号，不能严格证明操作来自人还是
  Agent。该限制在引入独立机器身份前不得用于生产发布。
