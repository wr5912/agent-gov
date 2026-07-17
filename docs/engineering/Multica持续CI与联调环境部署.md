# Multica 持续 CI 与联调环境部署

> 文档状态：当前工程实施与运维契约。
>
> 稳定原则见 [AgentGov 工程宪法与当前交付边界](./CI-CD宪法与交付链两阶段整改计划.md)。
> 本文只描述当前可执行链路，不定义正式 Release 或生产 CD。

## 1. 当前拓扑

```text
GitHub PR / master push
  -> governance / quality-gate
  -> 228 agent_gov_ci_status_relay 定时轮询
  -> Multica 对应 AID 评论

通过 CI 的精确 master SHA
  -> 人工执行 scripts/deploy_agent_gov_to_host
  -> 172.16.112.232 联调环境
```

各系统的职责只有一份：

| 系统 | 负责 | 不负责 |
| --- | --- | --- |
| GitHub Actions | CI 执行事实、run/job 状态和日志入口 | AID 工作流、Multica 会话、自动部署 |
| CI 状态中继 | 发现终态 run、校验唯一 AID、持久化待投递事件 | 改写 CI 结论、运行测试、部署 |
| Multica | AID 评论和研发协作会话 | 重新判定 CI、保存部署真相 |
| 部署脚本 | 精确 SHA 联调部署、健康检查、诊断和回退 | 监听 GitHub、自动选择版本 |

## 2. 权威与配置归属

CI 机制不应“取自 Multica”。Multica 只消费和展示结果，各配置面的归属如下：

| 配置面 | 权威位置 | 原因 |
| --- | --- | --- |
| 测试内容、workflow、`quality-gate` 汇总、AID 解析 | 本仓库 | 与代码同版本评审，机器可重复执行 |
| required check、PR-only、禁 force push | GitHub 仓库规则 | GitHub 才能真实阻断合并 |
| workflow run/job 结论 | GitHub Actions | CI 机器事实 |
| 轮询、outbox、幂等 marker | 228 中继代码与本机状态 | 负责可靠投递，不属于协作平台业务状态 |
| AID、订阅者、Agent/Team 指令、评论展示和研发会话 | Multica | 属于协作系统内部配置 |
| 联调环境当前版本 | 目标主机激活快照 | 部署运行事实 |

Multica Agent/Team 指令可以要求成员“查看 GitHub CI 证据”，但不能自行把 CI 判绿、替代
GitHub required check，或因一条评论自动部署。

## 3. GitHub 门禁

`.github/workflows/governance.yml` 是 PR 与 `master` push 的唯一持续 CI 入口，
required check 使用稳定名称：

```text
governance / quality-gate
```

仓库规则应配置为：

- `master` 只能通过 PR；
- 禁止 force push 和删除受保护分支；
- 合并前必须通过 `governance / quality-gate`；
- PR head branch 必须且只能解析出一个 `AID-N`；标题和正文可以不写 AID，若写只能重复
  head branch 中的同一个值。

分支保护属于 GitHub 外部运行配置，仓库代码只能检查和说明它，不能用文档声称已经设置。

## 4. CI 状态中继

228 上的 `agent_gov_ci_status_relay` 只出站访问 GitHub 与本机 Multica CLI。每轮：

1. 按事件分页查询目标 workflow 的新 run，并使用 SQLite 持久化水位；水位只过滤已经观察
   过且早于边界时间的终态；与水位同一 `updated_at` 的 run 继续进入候选，再由
   repository + run id + attempt 做精确去重，避免 GitHub 时间精度相同但较小 run id 晚出现时
   被漏掉。水位不作为提前停止翻页的依据，每轮都读取到 API 短页，避免较早创建但较晚完成的
   run 因 `updated_at` 与 API 页序不同而永久漏读；首次安装写入的
   `AGENT_GOV_RELAY_NOT_BEFORE` 同时作为 GitHub `created` 查询下界，完整扫描只覆盖安装后
   创建的 run，安装后创建但较晚完成的 run 仍会被后续轮次发现；
2. 只处理 `completed` 终态；
3. 从 PR head branch 解析稳定且唯一的 AID；标题/正文若出现不同 AID 则记录 SQLite
   结构化失败，不猜测 fallback AID；
4. 失败 run 从 `/actions/runs/<run_id>/attempts/<attempt>/jobs` 读取同一 attempt 的失败 job，
   不把 rerun 的最新 job 列表错误投影到旧 attempt；
5. 生成包含 repository、event、branch、SHA、PR、结论和 workflow URL 的通知；
6. 先写 SQLite outbox，再调用 Multica；
7. Multica 成功后标记 delivered；失败保留 pending，后续重试。

幂等键至少包含：

```text
repository + workflow_run_id + run_attempt + conclusion
```

run 的幂等身份不包含可后改的 AID。Multica 评论带机器可识别 marker，SQLite 和远端 marker
共同避免服务重启后重复投递。GitHub 传输错误、响应结构错误与 AID/PR 映射失败都写入 SQLite
证据；它们不能改写 CI 结论。

run 已进入本地水位后，首次 PR/AID 映射或同一 attempt 的 Jobs payload 仍可能暂时缺失。
这类失败写入同一 SQLite 中的 enrichment failure 记录，只保存重放所需的最小定位字段，
例如 repository、workflow run id、attempt、event、branch 和 SHA，不另造一份 CI 状态。
发现水位可以继续推进，避免一个坏 run 阻塞后续终态；但下一轮必须先重试尚未 resolved
的 failure，再发现新 run。映射或 Jobs 恢复后，使用原 run 幂等键生成一次 outbox，并把
failure 标为 resolved；失败记录及 resolved 时间继续作为历史证据保留，不删除、不改写
GitHub 的终态。

当前最小实现每轮完整扫描 `not_before` 之后的终态页，API 成本随安装后的历史 run 数增长。
若单事件扫描页数开始持续挤占 GitHub token 的调用预算，应升级为持久化“已创建但未终态”
run 索引；不得重新引入按 `updated_at` 页序提前停止的假设。

中继只投递终态，不发送 queued/in_progress 噪声。它不得持有目标机部署私钥，也不得调用部署脚本。

## 5. Multica 侧配置需求

### 5.1 Workspace 与 AID

- `agent-gov` repository 登记在目标 Multica workspace 中；
- issue prefix 保持 `AID`；
- 每个 GitHub PR 的 head branch 恰好包含一个已经存在的 `AID-N`；标题和正文如含 AID，
  必须与 branch 一致；
- AID 评论允许专用 relay 身份执行 `comment list` 和 `comment add`；
- 需要看到 CI 结果的人或 Agent 在对应 AID 上订阅；relay 不依赖模糊 Team 广播；
- CI 评论不自动修改 AID status、assignee、project 或 metadata。

### 5.2 Relay 身份

CI relay 是 228 上的服务身份，不是接活、写代码或做评审的 Multica Agent。它只需要：

- 读取 AID 既有评论，检查幂等 marker；
- 新增一条顶层终态评论；
- 访问固定 workspace；
- 不拥有 GitHub 写权限、部署私钥、docker 权限或 Agent task 执行权限。

不需要为 relay 创建 Squad、Autopilot 或任务 Agent；若 Multica 另配 webhook/autopilot
重复发送同一 CI 结果，反而会形成双通知源。

### 5.3 Agent 与 Team/Squad

当前阶段的团队指令应收敛为研发协作，不再包含自动发布状态机：

| 角色 | 当前应保留的职责 | 应删除的职责 |
| --- | --- | --- |
| 开发 Agent | 实现 AID 范围内改动；PR 保持唯一 AID；修复红 CI | 判定 CI 真相、合并、部署 |
| QA Reviewer | 独立审查 diff、测试和 GitHub workflow 证据 | 把 Multica 评论当作第二套 CI、触发发布 |
| Solution Architect | 必要时做只读边界和方案审查 | 默认充当每个 issue 的发布控制器 |
| 协调者 | 拆分工作、明确 owner、提醒人工决策 | 自动合并、创建强制 Release 子任务、自动关闭父 AID |
| 人工批准者 | 决定合并以及是否执行联调部署 | 把决定委托给 CI relay |

当前 CI 不需要 `Release SRE` Agent。建议先归档；如果暂时保留，只能在人工明确发起后核对
精确 SHA、AID、PR、workflow URL 和联调结果，不得监听 CI、自动部署或推进 issue。

Team/Squad 必须只有一个清晰协调者。Agent 名称、Team leader 和 instructions 应一致，避免
“存在 delivery lead，但实际由另一个角色担任 leader”这种双重所有权。

建议把当前 Team instructions 收敛为以下最小文本，再按实际角色名替换：

```text
本 Team 只负责 agent-gov 的需求拆分、实现协作和独立审查。
每个 GitHub PR 必须关联恰好一个现有 AID。
GitHub Actions 的 governance / quality-gate 是唯一 CI 结论；
Multica 评论只展示该结论，不得重新判定。
红 CI 交还开发者修复；绿 CI 只表示具备人工合并条件。
只有人类可以决定合并和是否执行联调部署。
任何 Agent、Squad、Autopilot 都不得监听 CI 后自动部署、推进发布状态或关闭父 AID。
```

Agent instructions 只需要追加自身职责，不重复整套 CI 实现细节。例如开发 Agent 追加
“保留唯一 AID 并修复红 CI”，QA 追加“独立审查 diff 与 workflow URL”，协调者追加
“只协调 owner 与人工决策，不写代码、不自审、不部署”。

### 5.4 当前 Multica 配置待整改

根据本机 selfhost workspace 的只读核查，至少需要在 Multica 中处理：

1. `agent-gov-delivery-team` instructions 仍包含旧自动发布控制器、Release SRE 子 Issue、
   staging 自动推进和自动关闭父 Issue，全部删除；
2. `release-sre` 仍描述自动/受控 staging 发布，按 5.3 归档或降级为人工触发的联调核验；
3. `delivery-lead` 已存在，但当前 Team leader/成员关系与该名称不一致；选择一个协调者并清理重复角色；
4. `AID-16` 仍以旧 PAT-only staging CI/CD 为主题，应更新为当前“持续 CI 通知 + 人工联调部署”
   或在本次迁移验收后关闭；
5. workspace 描述使用中立的 AgentGov 研发协作表述，不绑定某个外围聊天渠道；
6. 不创建 webhook/autopilot 自动部署，也不保留 `release_sre_issue_id`、controller cursor、
   quarantine 等旧控制器 metadata 作为当前必填字段。

Multica 侧完成调整后的最小验收：

```bash
multica --profile <profile> agent list --output json
multica --profile <profile> squad get <squad-id> --output json
multica --profile <profile> squad member list <squad-id> --output json
multica --profile <profile> issue subscriber list AID-123 --output json
multica --profile <profile> issue comment list AID-123 --roots-only --output json
```

验收重点是角色与指令，不要把 Agent 名单数量当成目标。当前阶段能用更少角色完成研发、独立审查
和人工决策，就不为未来可能发生的协作预埋复杂 Team。

## 6. 配置与安装

敏感值只保存在 228 本机的 systemd credential 或私有 env 中，不进入仓库、日志和命令行历史。
仓库只提供非敏感示例：

```text
deploy/systemd/agent-gov-ci-status-relay.env.example
```

安装前必须已经满足：

- GitHub token 只有读取 Actions、仓库和 PR 元数据所需权限；
- 228 已安装并登录可用的 Multica CLI；
- Multica profile 已配置可用的用户身份和目标会话；
- AID 评论命令已在该 profile 下人工验证；
- SQLite 状态目录由专用系统用户独占。
- 执行安装器的 checkout 已完成评审、tracked diff 为空，当前 `HEAD` 就是要安装的版本。

安装：

```bash
sudo install -d -m 700 /etc/agent-gov-ci-status-relay
sudo install -m 600 /本机私有路径/github_token \
  /etc/agent-gov-ci-status-relay/github_token

sudo scripts/install_agent_gov_ci_status_relay \
  --multica-config "$HOME/.multica/profiles/selfhost/config.json"
```

安装脚本故意不接受 token 命令行参数。它只接受当前干净 checkout 的精确 `HEAD`，通过
`git archive` 写入 root-owned `/opt/agent-gov-ci-status-relay/versions/<SHA>/`，再原子切换
只读 `current`。升级前先停止 relay timer/service；新版本首次轮询或启动失败时恢复旧指针和
Multica profile，再重新启动旧版本。首次安装会写入 `AGENT_GOV_RELAY_NOT_BEFORE`，避免把
安装前的历史 CI 全部补投。升级时不从网络另行 `git pull master`，避免安装脚本、systemd unit
与实际执行代码来自不同版本。

如果主机仍有旧 `agent-gov-release-controller`，迁移期间先禁用 timer 并停止 service，防止它与
新链路并发自动部署；在新 relay 首次 one-shot 成功之前，不删除旧 PAT 本机副本、SSH、
Multica profile、repository、systemd unit、`releasectl` 或 Docker 组用户。新 relay 首次
成功并启用 timer 后，安装器才执行不可逆退役：

1. 从旧 `state/state.db` 生成字段白名单审计快照
   `/var/lib/agent-gov-release-controller-audit/release-controller-audit.json`；
2. 快照只保留 SHA、PR/AID、状态、workflow 定位、时间、事件类型和 outbox 幂等状态，
   不保留 reason/details、通知 payload、last error、日志、凭据或私有配置；首次成功写入后
   视为不可变，部分退役失败后的重跑必须先验证 JSON 可解析且仍符合字段白名单，不得用空快照
   或后续状态覆盖；
3. 删除旧本机 PAT、SSH、Multica profile、repository、unit、`releasectl`，移除 Docker
   组身份并删除旧专用用户；
4. 操作者随即在 GitHub 凭据所有者处撤销旧 release-controller PAT。安装器只能删除本机
   副本，不能声称远端 token 已失效。

若新 relay 首次轮询失败，安装器会回滚 relay 版本和 profile；旧控制器敏感资产仍完整保留，
但旧自动部署服务保持停止，不会为了恢复通知而重新启用已废弃的自动 CD。修复问题后重跑同一
安装命令。退役阶段若中途失败，新 relay 保持运行；修复残留资产后幂等重跑安装器。

检查：

```bash
systemctl status agent-gov-ci-status-relay.timer
journalctl -u agent-gov-ci-status-relay.service --since today
sudo test -s /var/lib/agent-gov-release-controller-audit/release-controller-audit.json
sudo test ! -e /etc/agent-gov-release-controller
sudo test ! -e /var/lib/agent-gov-release-controller
! getent passwd agent-gov-release
```

Multica 评论成功只证明通知已进入对应 AID 和 Multica 会话，不证明 CI 通过或联调环境已经部署。

## 7. 联调环境部署

绿色 CI 不触发部署。操作者从已合并 PR 选择通过 CI 的完整 `master` SHA，并显式绑定
AID、PR 和 workflow 证据：

```bash
scripts/deploy_agent_gov_to_host \
  --ref <40位master提交SHA> \
  --aid AID-123 \
  --pr-number 456 \
  --workflow-url https://github.com/<owner>/<repo>/actions/runs/<run-id> \
  --host 172.16.112.232 \
  --environment staging-232
```

部署模式缺少上述任一证据时必须失败。`--preflight-only`、`--remote-status`、诊断和回退等
只读或恢复动作按脚本自身参数契约执行，不伪造一次新部署。

workflow URL 只是定位符。部署脚本会通过公开 GitHub API 只读核对：

- URL 属于配置仓库，run/attempt 已完成且结论为 `success`；
- 事件是指定完整 SHA 在 `master` 上的 `push`，workflow 文件是
  `.github/workflows/governance.yml`；
- 同一 attempt 中恰好一个 `quality-gate` job 成功；
- 该 SHA 恰好是所填 PR 合入 `master` 的 merge commit；
- PR head branch 中的唯一 AID 与命令参数一致；标题和正文如含 AID，也必须与 branch 一致。

校验器不读取 GitHub 凭据；当前公开仓库使用 GitHub 的只读公开接口。URL 可访问但任一
事实不一致、API 不可达或被限流时都拒绝部署，不以人工口述或 Multica 评论替代。校验
通过后的 run id、实际 attempt、workflow、SHA、PR 和 AID 会一并写入 `release.json`。

部署事实以目标机的激活快照和 `release.json` 为准。AID 评论只记录证据链接，不成为当前版本真相。

需要重启或修复当前联调版本时，不使用平行 restart 脚本。重新执行原部署命令，保持相同的完整
SHA、AID、PR 和 workflow URL；部署脚本会复核证据并复用已提交的不可变部署快照，远端 helper
在该快照已是 `current` 时只做镜像恢复、Compose 幂等协调和健康检查。

## 8. 基础安全边界

当前只保留与真实暴露面相称的基础防护：

- token、Multica secret、会话凭据和 SSH 私钥不得进入仓库、日志或通知正文；
- GitHub token 只读，中继进程不加入 docker 组；
- `GITHUB_API_URL` 只接受 HTTPS；
- root-owned 版本目录对 relay 只读；relay 只可写 SQLite state、Multica profile 与 cache；
- SQLite/outbox 和私有配置使用专用用户与最小文件权限；
- workflow URL 必须属于配置的 GitHub repository；
- workflow URL 对应的 run/attempt、`quality-gate`、SHA、PR 和 AID 必须经 GitHub
  机器事实复核；
- 部署只接受完整 SHA，不接受漂移分支名；
- 目标机 SSH host key 必须预先确认；
- 通知失败不触发部署，部署失败不改写 CI 结论。
- 新 relay 首次成功之前不删除旧控制器凭据；成功之后旧 Docker 组身份、SSH、PAT 本机副本、
  Multica profile、repository 和自动部署 unit 必须退役，只保留脱敏审计快照。

正式 Release、供应链签名、生产审批和更强隔离只在宪法中的升级条件命中后重新设计。

## 9. 验收

### CI 与通知

- PR 红/绿、`master` push 红/绿终态均能形成恰好一次对应 AID 评论；
- rerun 的新 attempt 能形成新终态，重复轮询不重复评论；
- 从旧 relay 升级时，既有 outbox 中同一 run/attempt 仍被识别，不因新幂等键格式重复评论；
- 无 AID、多 AID、GitHub 暂时不可达、异常 GitHub payload、Multica 暂时失败都有
  SQLite 持久化证据；
- 首次 PR/AID 映射缺失或同 attempt Jobs payload 缺失时，failure 与最小 replay payload
  持久化；依赖恢复后恰好投递一次、failure 标 resolved 且历史证据仍可查；
- 停机期间积累超过单页上限的 run 恢复后仍能通过分页与持久化水位完整发现；
- Multica 恢复后 pending outbox 自动补投；
- Multica 只收到已配置 AID 的终态消息。

### 部署

- 缺 SHA、AID、PR 或 workflow URL 时部署模式失败；
- 失败/错误 attempt、非 `master` push、错误 workflow、错误 SHA、未合并或不匹配的
  PR/AID 都会在接触目标机前失败；
- 精确 SHA 的预检、部署、健康检查和远端状态查询通过；
- 失败部署能诊断并恢复先前健康快照；
- 对同一 SHA 和证据重跑唯一部署入口时复用不可变部署快照，并幂等协调当前 Compose；
- 联调环境报告的 SHA 与 GitHub workflow 证据一致。

### 旧控制器退役

- 人为制造新 relay 首次 one-shot 失败时，安装器不得删除旧 controller token、SSH、
  Multica profile、repository 或用户；
- 新 relay 首次成功后，旧用户不再存在、Docker 组无旧身份、本机凭据和自动部署 unit 已删除；
- 审计快照可解析，且不含旧日志、reason/details、通知 payload、last error 或测试 secret；
- GitHub 侧旧 PAT 已由其所有者撤销，并留下不含 token 值的人工核验记录。

### 治理

```bash
make codex-guard
make test
```

真实 228/232 与 Multica 验收需要保存脱敏后的命令结果、systemd 状态、workflow URL、
Multica AID 评论和目标机版本证据；本机单测不能替代这些外部运行态证据。
