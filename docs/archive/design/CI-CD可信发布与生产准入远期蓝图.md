# CI/CD 可信发布与生产准入远期蓝图

> 文档状态：远期归档蓝图，不属于当前交付路线，不构成当前 CI、联调部署或发版门禁。
>
> 本文保存 2026-07-16 前关于 ReleaseBundle、签名供应链、可信 Staging、生产蓝绿和
> PostgreSQL/PITR 的完整设计论证。当前实施以
> [CI/CD 宪法与交付边界](../../engineering/CI-CD宪法与交付链两阶段整改计划.md)
> 和
> [Multica 持续 CI 与联调环境部署](../../engineering/Multica持续CI与联调环境部署.md)
> 为准。
>
> 以下正文保留当时的目标整改方案、实施顺序和验收门，不表示当前 workflow、
> 联调环境或生产环境已经具备这些能力。当前运行事实以
> [Multica 持续 CI 与联调环境部署](../../engineering/Multica持续CI与联调环境部署.md)、
> [测试资产组合治理](../../engineering/测试资产组合治理.md)、仓库代码和远端运行证据为准；
> [PAT-only 持续交付与 Staging 发布](./PAT-only持续交付与Staging发布.md)同样只作历史追溯。
>
> 业务 Agent workspace 的目标包契约、版本激活和资产回流边界分别以
> [业务 Agent Workspace 包导入与热加载产品工程方案](../../业务AgentWorkspace包导入与热加载产品工程方案.md)
> 和
> [业务 Agent 工作区资产闭环产品工程方案](../../业务Agent工作区资产闭环产品工程方案.md)
> 为准；字段级当前事实仍以 OpenAPI 和运行证据为准。

## 1. 整改目标与裁决

整改分为两个阶段：

1. **可信 Staging**：恢复真实可执行的 CI/CD；Staging 允许不超过 15 分钟维护窗口，
   失败后达到 RPO=0、RTO 不超过 30 分钟。
2. **内网生产准入**：使用同一制品晋级，引入 PostgreSQL 和蓝绿切换；读流量零停机，
   写流量 drain 不超过 30 秒，应用回切不超过 60 秒，数据库灾备达到
   RPO 不超过 5 分钟、RTO 不超过 30 分钟。

阶段一验收前保持自动部署关闭。长期单维护者模式不伪装成双人审批；采用 GitHub 强制
检查加 228 主机外置信任锚作为补偿控制。治理文件发生变化时，必须通过独立的本机
`sudo` 信任更新并留下审计记录，不能由同一个 PR 修改门禁后直接自证并发布。

整改完成后的信任链为：

```text
受保护 master SHA
  -> 源码质量门
  -> 可信构建器单次构建
  -> 签名 ReleaseBundle
  -> Staging 真实验收与 attestation
  -> 同一 bundle 晋级生产
```

这条链要回答的问题只有一个：**生产上跑的这堆字节，凭什么相信它就是通过了质量门的那份代码？** 今天这个
问题无法回答——目标机自己构建、按可变 tag 解析镜像，Staging 验过的东西和生产跑的东西不保证是同一个产物。
链上每一环都是为堵一个具体的断点而存在：受保护 SHA 堵“代码来源不确定”，单次构建堵“构建两次得到两个产物”，
签名 bundle 堵“产物在传输中被替换”，attestation 堵“验收结论无法追溯到具体产物”，同一 bundle 晋级堵
“Staging 和生产跑的不是一个东西”。少任何一环，后面的验收都只能证明“某个东西是好的”，而不能证明
“**这个**东西是好的”。

两个阶段的先后也来自这条链：阶段一先把链本身建起来（可信 Staging），阶段二才谈把链的终点接到生产
（蓝绿与数据库）。反过来做——先上蓝绿再补信任链——得到的是一套能零停机部署不可信产物的系统。

这条链证明平台软件和仓库声明基线的交付版本，不单独证明运行卷中业务 Agent 的当前
行为版本。完整运行事实由两类版本血缘组合：

```text
平台版本血缘：受保护 SHA -> 签名 ReleaseBundle -> release identity
Agent 版本血缘：seed / import / change set -> per-Agent Git commit -> agent_version_id
一次运行事实：release identity + agent_id + agent_version_id
```

这不是两套发布轨道。`ReleaseBundle` 与 per-Agent Git 分别治理平台软件和业务 Agent
workspace 两个不同对象；同一对象只能有一个当前版本真相，run、trace、feedback 和
EvalRun 在使用时组合引用两类版本。

以下门禁不可豁免：

- 可信 ref、required quality gate 和外置信任锚。
- 单一 Compose env 来源。
- 制品签名、摘要和实际 image ID 一致。
- 部署前数据保护和 schema 兼容检查。
- Staging 与生产使用完全相同的 bundle digest。
- 同一 bundle 只能声明平台制品一致；涉及 Agent 行为的验收必须绑定实际
  `agent_id + agent_version_id`，不得沿用其他 workspace commit 的证据。
- 业务 Agent 当前行为版本只认 per-Agent Git HEAD。seed、import receipt、change set、
  release event 和数据库投影都不得成为第二个 active-version 真相。
- 运行态 workspace 回流仓库必须经过导出、脱敏、受保护提交、质量门和重新签名发布；
  容器和在线 API 不得直接写回 seed 声明源。
- workspace 导入至少通过结构、安全和受管策略校验，并留下精确 commit、actor 和回滚锚点；
  receipt 只证明导入事实，不证明行为测试通过。

## 2. 目标权威与机器契约

### 2.1 权威顺序

整改后按以下顺序裁决，后层不得重新定义前层语义：

1. 产品目标与术语：`docs/项目目标愿景使命.md`、
   `docs/AgentGov术语与版本边界.md`。
2. CI/CD 人类宪法：后续从本文收口出的薄稳定契约，只保留权威顺序、不可豁免门、
   修宪规则和环境边界。
3. 机器策略：
   - `tests/quality_policy.json`：测试资产、lane、workflow/job 绑定和证据；
   - `tests/agv_acceptance_policy.json`：AGV 状态、criterion 和证据；
   - `deploy/release_policy.json`：环境、可信 ref、制品、迁移和发布门；
   - `deploy/release_waivers.json`：最长 14 天的临时豁免。
4. 执行实现：Makefile、workflows 和 scripts。
5. 运行事实：GitHub ruleset、Actions run、发布控制器、目标主机和 attestation。

文档不能替代运行事实。机器策略中声明的 trigger、预算或验收能力如果没有执行器，
必须删除或明确登记为 gap。

两份 workspace 目标方案只负责包、导入、热加载、导出和归档的产品工程契约；本文负责
这些能力进入 Staging/生产后如何与平台发布血缘、可信证据和恢复边界组合。不得把
workspace 专项状态机复制到 CI/CD policy，也不得另建 `WorkspaceReleaseBundle`。

### 2.2 ReleaseBundle v1

定义环境无关的 `ReleaseBundle v1`，至少包含：

- repository、完整 commit SHA、VERSION、CI run/attempt 和构建器身份；
- source archive、项目及依赖镜像 archive、image ID 和各自 checksum；
- 依赖锁、Dockerfile、policy 和构建输入摘要；
- 仓库中业务 Agent seed catalog 的 Agent 清单、tree digest，以及 managed policy、
  guard hook 和扫描器的规范化摘要；
- 每个镜像的 SBOM、漏洞报告和构建 provenance；
- schema 起止版本、N/N-1 兼容声明；
- controller/helper 最低协议版本；
- bundle manifest checksum 和 Cosign 离线签名。

Bundle 不包含环境名称、目标主机、私有 env、运行卷内容或部署凭据。同一个 commit SHA
只能对应一个有效 bundle digest；出现第二个不同 digest 时必须隔离该 SHA。

仓库 seed 随 source archive 和 bundle manifest 进入签名交付链；`${HOME}/volume-agent-gov`
中的业务 Agent workspace、per-Agent Git、import receipt 和会话数据属于可变运行态，
不得打进 bundle。Staging 与生产 bundle digest 相同，只证明仓库 seed 基线相同，不证明
已有运行卷中的 workspace HEAD 相同。

### 2.3 版本血缘与单一运行事实

| 对象 | 唯一权威 | 明确不是权威的对象 |
| --- | --- | --- |
| 平台当前发布版本 | 目标环境实际加载并验签通过的 `ReleaseBundle` | tag、控制器缓存或文档声明 |
| 业务 Agent 当前行为版本 | 该 Agent per-Agent Git HEAD | seed、import receipt、change set 状态或注册表标签 |
| 仓库出生基线 | 受保护 SHA 中的 seed catalog | 已存在运行卷中的 workspace |
| 一次运行事实 | release identity 与 `agent_id + agent_version_id` 的组合记录 | 只记录其中一类版本的推断 |

import、change set、publish、restore 和 rollback 可以是不同用户动作，但必须收口到同一
per-Agent Git 版本链、相同的 expected-HEAD/维护栅栏和统一版本读取。操作记录只承担协调、
恢复和审计，不得各自持有一套“当前版本”或“已发布版本”事实。

Staging 和生产均允许 workspace 在结构、安全和受管策略校验通过后立即激活；运行中 turn
继续固定启动时的 commit，新 commit 从下一 turn 生效。立即激活不触发自动行为测试或自动
回滚，也不把版本标记为已验证。新 commit 激活后，旧 `agent_version_id` 上的行为证据不再
覆盖新版本；平台 release attestation 仍只对其签名 bundle 负责。

### 2.4 Env 与运行边界

`COMPOSE_ENV_FILE` 保持唯一公开选择变量：

- 本机开发：选择 `docker/.env`；
- CI：选择 runner 临时目录中的一份完整 env；
- Staging：选择 root-only 的 `shared/env-revisions/<revision>.env`；
- 生产：选择受控、版本化的 production env revision。

所有调用方先把路径解析为绝对路径并导出，同一文件同时供 Compose 插值和服务
`env_file` 使用。容器内 `AppSettings` 只读取进程环境，不再尝试读取镜像内或 release
目录中的 `docker/.env`。

Release 只记录 opaque `config_revision`。私有 env 内容及其可离线猜测的摘要不进入
bundle、日志、文档或对外健康响应。

`docker/runtime-volume-seeds/` 是随仓库发布的只读声明基线；容器部署的宿主机持久化路径是
`${HOME}/volume-agent-gov/data/business-agents/<agent_id>/workspace`，容器内对应
`/data/business-agents/<agent_id>/workspace`；本机调试使用
`/tmp/local-debug-volume-agent-gov` 对应隔离根。seed 只在 workspace 整体不存在时承担
出生配置，部署、重启和 profile 解析不得逐文件回灌、补写或复活已有 workspace。

本机调试目录可写不能证明容器只读挂载下同一动作可用。任何 seed、workspace、导入、导出
或恢复能力的 Staging/生产验收必须使用真实 Compose 挂载语义；local-debug 结果只能作为
专项开发证据。

### 2.5 运维接口

发布 CLI 收口为：

```text
releasectl artifact build --ref <SHA>
releasectl deploy-staging --artifact <digest>
releasectl rollback-staging <release-id>
releasectl restart-current
releasectl import-legacy
releasectl trust inspect --ref <SHA>
releasectl trust approve --ref <SHA> --reason <reason>
releasectl promote-production --tag <tag> --artifact <digest>
```

上列是日常运维面。验证期子命令同属 `releasectl`，在第 6 章被调用，一并登记以免实现者照上表实现后
在验收阶段撞上未知子命令：

```text
releasectl artifact rehearse --ref <SHA>
releasectl deploy-staging --artifact <digest> --fault-injection <scenario>
releasectl rehearse-production --artifact <digest>
releasectl rehearse-pitr
```

CLI 收口的目的不是好看：当前发布动作分散在多个脚本与主机操作里，收口成单一 binary 才能让「谁能执行发布」
这个问题有唯一答案，也才能让信任锚检查挂在一个入口上。

数据库运维接口收口为：

```text
service_launcher schema-status
service_launcher migrate
service_launcher backup
service_launcher restore
```

API 启动只校验 schema，不再隐式迁移。健康响应增加 release ID、commit、image ID、
config revision、schema head 和 deployment slot；这些字段是加法契约。

健康响应不枚举所有业务 Agent workspace，也不把平台 release identity 当作 Agent 行为版本。
具体运行继续通过 Responses/run/trace 契约返回实际 `agent_id` 和 `agent_version_id`。

### 2.6 修宪规则

以下变化属于修宪，而不是普通 policy 调参：权威顺序、不可豁免门、平台或 Agent 版本真相、
环境与凭据边界、外置信任锚保护类别，以及 attestation 能够证明的范围。

修宪必须同时满足：

1. 通过受保护 `master` PR 和完整 `quality-gate`；
2. 由已安装可信 validator 完成外置信任批准，不执行候选仓库代码；
3. 同步人类宪法、机器 policy、执行器、测试和迁移/回退说明；
4. 在隔离环境证明旧版本仍可恢复，且新旧证明范围没有静默扩大；
5. 可信工具和主机信任摘要更新后才允许新条款生效。

最长 14 天的 release waiver 只能豁免机器 policy 中明确标为可豁免的门，不能修改宪法，
也不能绕过本节和第 1 节的不可豁免项。

## 3. 阶段一：可信 Staging

### 3.1 止血与恢复真实门禁

实施前先在 228 禁用发布 timer，并保存：

- controller SQLite 状态；
- 当前健康 release 和镜像归档；
- current 链接及 release manifest；
- 私有 env revision；
- GitHub 远端保护状态和最近 CI 证据。

第一批变更固定为：

- 删除向 GitHub Actions 注入模型与部署凭据的 `release-live` workflow；真实模型验收迁入受控 Staging
  发布链。该 workflow 本身可以运行，问题不在“坏掉”而在信任边界：它把模型 provider 凭据与 release API key
  放进 Actions 运行环境，等于让公有 CI 持有生产凭据，与本文“构建器不持有 PAT/SSH/Staging env”的约束冲突。
- 移除服务硬编码 `env_file: .env`，建立统一 Compose wrapper。
- 在干净 `git archive` 中执行 `docker compose config`、build、up 和 health，不能依赖
  开发机私有文件。
- `quality-gate` 增加阻塞的 `container-contract`：API、UI、sidecar、provider health
  和真实容器 UI smoke 全部成功。
- `container-contract` 对 seed/workspace 增加两种运行态：空卷能按签名 seed catalog
  完成初始化；已有卷中的业务 Agent workspace 保持当前 Git HEAD，不被新 bundle 回灌。
- workflow 增加 actionlint、PR 并发取消和显式超时：
  - static 15 分钟；
  - backend/frontend 30 分钟；
  - container contract 45 分钟；
  - final gate 10 分钟。
- GitHub Actions 全部固定完整 commit SHA，默认 token 权限为只读。
- Ruff 扩展为全量检查 `app scripts tests`，清理当前存量问题。
- Pyright 分两批恢复 argument、assignment、call、index、optional 和 return 诊断；
  每批清零后再合并。unknown 系列诊断留到阶段二。

### 3.2 依赖与供应链

保留 `requirements.txt` 作为人工输入，使用 `uv` 生成并提交：

- `requirements.lock`；
- A2UI 依赖 lock；
- LiteLLM sidecar 依赖 lock。

CI、Docker 和可信构建器统一从 lock 安装并验证哈希，不直接使用 `pip`。当前工作区中
未跟踪的三行 `uv.lock` 不覆盖，也不作为本方案的依赖真相源。

同时完成：

- 前端移除 `latest`，写入当前 lock 对应的确切版本；
- Python、Node、Langfuse 和数据库镜像固定 digest；
- 使用固定版本及 checksum 的 Syft、Grype 和 Cosign；
- 每个镜像生成 SBOM；
- Critical 漏洞直接阻断；
- High 漏洞只能使用最长 14 天、带 owner 和 issue 的 waiver；
- 漏洞数据库超过 7 天未更新时发布 fail closed。

### 3.3 GitHub 保护与外置信任锚

本节要解决的是一个单维护者项目绕不开的结构性问题：**门禁和被门禁的代码在同一个仓库里，由同一个人改。**
一个 PR 完全可以先放宽 `quality-gate`、再让被放宽的门为自己放行——所有检查都是绿的，但什么也没被证明。
常规解法是双人审批，本项目没有第二个人，把 CODEOWNERS approval 打开只会造成自锁（自己批自己的 PR，
形式合规、实质为零）。

因此改用**外置信任锚**：把治理文件的摘要放在仓库之外（228 主机、root-only），候选 SHA 一旦改动这些路径就
停下等人工 `sudo` 批准。它的本质是让「改门禁」这个动作**必须离开 PR 这个上下文**才能生效——攻击者或失误者
无法只靠一次 git push 完成“改门 + 过门”。这不是职责分离的等价物，只是把自证的成本从零提高到“需要一次
带审计记录的主机操作”，其上限见第 8 章残余风险。

GitHub ruleset 固定为：

- 只允许 PR 和 squash merge；
- required `quality-gate`，合并前分支必须最新；
- linear history；
- 禁止 force push 和删除；
- 规则覆盖管理员，无常规 bypass；
- 单维护者模式不要求人工 approval。

`.github/CODEOWNERS` 只承担责任声明，不启用会导致单维护者自锁的 code-owner approval。

228 保存 root-owned `trusted-governance.json`，其中包含受保护路径集合和规范化摘要：

- workflows、Makefile、测试与发布 policy；
- release/controller/build scripts；
- Dockerfiles、依赖 locks 和 VERSION；
- CI/CD、测试质量与文档治理检查器；
- managed Agent policy、审批契约基线、guard hook 模板、workspace archive 安全扫描器。

普通 prompt、skill 和业务 Agent seed 内容仍属于签名构建输入并通过质量门、secret 扫描和
行为测试，但不因每次业务内容变化都要求修宪；只有定义安全策略、审批门和扫描规则的资产
进入外置信任锚。

候选 SHA 修改上述路径后进入 `awaiting_trust_approval`，不得自动部署。
`releasectl trust approve` 必须使用已安装的可信 validator 检查候选内容，不得以 root
身份执行候选仓库代码，并记录 OS/sudo 身份、原因、旧摘要、新摘要和时间。

新增 CI/CD 治理检查：

- workflow、policy、trigger、job 和 retention 一致；
- coverage/mutation 阈值不得降低；
- blocking/release lane 不得降级；
- 主流程绑定删除必须有替代证据或明确 gap；
- managed policy、guard 和 workspace 扫描规则不得降级，seed catalog 变化必须有清单、
  tree digest 和对应测试证据；
- branch protection/ruleset 满足发布策略；
- scheduled lane 最近状态满足 readiness；
- 候选治理摘要与外置信任锚一致。

workflow 修复并连续三次绿色后再启用 ruleset。远端审计不通过时 controller 必须保持
`CD_DISABLED`。

### 3.4 Build once 与签名制品

228 上的可信构建器只在精确 SHA 的 `quality-gate` 成功后工作：

- 构建器不持有 GitHub PAT、SSH 私钥或 Staging env；
- 构建网络只允许受控软件源；
- 构建一次后保存不可变 bundle；
- 目标机禁止 build、pull 或按可变 tag 重新解析镜像；
- 签名私钥仅由已安装的可信签名器读取，Staging 和生产只安装公钥。

Bundle 在隔离临时根完成：

1. Compose config；
2. image load 和 image ID 核对；
3. API/UI/sidecar 真实容器主流程；
4. provider health；
5. migration rehearsal；
6. seed catalog/tree digest 与 bundle manifest 一致性；
7. 空卷初始化和已有业务 Agent workspace 不回灌；
8. SBOM、漏洞与 secret 扫描。

全部通过后才签署 bundle manifest。

### 3.5 数据迁移与恢复

现有 0044 作为首个受管 schema baseline。旧 release 只能显式导入为
`legacy-unverified`，不得成为生产候选。

旧 SQLite migration 链冻结为历史升级入口；新迁移进入 Alembic 注册表并声明：

- `expand`、`backfill` 或 `contract`；
- from/to schema；
- 是否兼容上一应用版本；
- 是否需要部署前快照。

普通发布只允许 expand/backfill。DROP、DELETE、重命名和字段语义收窄属于 contract，
最早在 N+2 且回滚窗口关闭后单独执行。

每个候选 bundle 必须完成 N/N-1 rehearsal：

1. N-1 创建并写入数据；
2. N 执行迁移并读写；
3. N-1 在新 schema 上重新读写；
4. N 再次读取 N-1 的写入。

Staging 正式部署顺序：

1. 校验 bundle、签名、image ID、CI、信任锚和磁盘空间；
2. 停止 API/UI/sidecar 写入者，Langfuse 稳定服务保持运行；
3. 对 `${HOME}/volume-agent-gov` 下的 `data`、governor workspace 和 governor
   Claude root 创建一致性快照；`data` 清单必须显式包含全部业务 Agent workspace、
   per-Agent Git、registry、import receipt 和未终结 change set，不依赖目录名推断；
4. 执行 SQLite `integrity_check`；
5. 由独立 migration 命令升级 schema；
6. 使用 `--no-build --pull never` 启动新 bundle；
7. 核对健康身份、历史列表/详情、业务 Agent Git HEAD、registry/receipt 对账、真实模型、
   浏览器 network/console 和关键写读事务；
8. 成功后原子切换 current，并生成签名 Staging attestation。attestation 分开记录平台
   bundle 证明和本次实际测试过的 `agent_id + agent_version_id`；未测试的 Agent 不得被推断为已验证。

任一步失败：

- 停止新 release；
- 恢复数据、workspace 和绑定 config revision；
- 启动原 bundle 并重新验收；
- 恢复失败时锁死后续发布，进入人工灾难恢复。

保留最近 5 个成功 release 和 14 天内全部失败证据。空间不足以容纳新 bundle、两份快照
和 20% 安全余量时拒绝部署。

### 3.6 发布实现重构

当前 controller、state 和 shell 发布脚本已经接近架构阈值。新增职责前先拆成
`scripts/agent_gov_release/` Python 包，分别承载：

- policy 与 trust；
- artifact 与 signing；
- controller 与 lineage；
- state machine 与 persistence；
- remote deployment protocol；
- CLI 与 diagnostics。

旧设计处理：

| 对象 | 动作 | 退出条件 |
| --- | --- | --- |
| `restart_agent_gov_on_host` | `delete` | `releasectl restart-current` 通过真实远端测试 |
| 隐式 legacy bootstrap | `delete` | 显式 `import-legacy` 可用 |
| 宽泛容器名前缀清理 | `delete` | 全部资源按 Compose project/label 管理 |
| `deploy_agent_gov_to_host` 构建逻辑 | `merge` | artifact-only 部署稳定后删除 |
| 现有 exact SHA/AID/lineage | `keep` | 继续作为 controller 准入门 |
| SQLite state 与 durable outbox | `keep` | 集中状态机和幂等行为不变 |
| systemd credential、known_hosts、flock | `keep` | 继续作为凭据与并发边界 |

### 3.7 阶段一完成门

阶段一必须同时满足：

- `master` 远端显示受保护，远端 policy audit 全绿；
- 连续 5 个不同 SHA 完成“源码门 -> 单次构建 -> 签名 bundle -> Staging 消费”；
- 至少完成 migration 失败、应用 health 失败、snapshot 恢复失败三类故障注入；
- 前两类自动恢复成功，第三类锁死发布并给出可执行诊断；
- bundle、目标 image ID 和 Staging attestation 完全一致；
- 签名 seed catalog 在空卷初始化时逐项匹配；已有卷中的业务 Agent workspace HEAD
  不因部署发生隐式变化；
- attestation 中平台 bundle 证明与实际测试的 Agent 版本证据边界清楚，不存在 receipt
  或旧 commit 证据冒充；
- Staging 维护窗口不超过 15 分钟；
- 成功恢复达到 RPO=0、RTO 不超过 30 分钟；
- artifact 和日志 secret 扫描零泄漏。

达到上述条件后才重新启用自动部署 timer。

## 4. 阶段二：内网生产准入

### 4.1 PostgreSQL 与统一迁移

SQLite 只保留本地开发和阶段一历史兼容。阶段二先把 Staging 迁移到独立的 AgentGov
PostgreSQL，不复用 Langfuse 数据库。

实施内容：

- 建立与 0044 等价的 PostgreSQL Alembic baseline；
- 建立一次性 SQLite -> PostgreSQL 导入工具；
- 导入支持 dry-run、重试、行数、主键、外键和关键业务摘要核对；
- Staging 在 PostgreSQL 上完成生产演练后，生产才允许晋级；
- API、migration job 和两个应用 slot 共享 PostgreSQL；
- 所有生产发布只允许 expand/backfill；
- contract migration 在至少一个完整发布周期、旧 slot 退出后单独执行。

PostgreSQL 与业务 Agent workspace Git 分属数据库和文件系统两个恢复边界。每次备份和 PITR
演练必须生成共同恢复清单，至少记录数据库备份/PITR 点、registry/import receipt 水位以及
各业务 Agent HEAD/tree digest。恢复后任一引用无法对账时 fail closed，不允许仅因数据库
健康或 Git 仓库可读就继续开放写流量。

### 4.2 蓝绿拓扑

Compose 拆分为稳定 shared 栈和两个应用 slot：

| 组成 | 端口/职责 |
| --- | --- |
| stable router | 对外 API 58080、UI 55173 |
| blue | API 58081、UI 55174、sidecar |
| green | API 58082、UI 55175、sidecar |
| shared | AgentGov PostgreSQL、Langfuse、持久存储、Nginx router |

取消固定 `container_name`，使用 Compose project、labels 和外部网络隔离。

前端改为多阶段构建，由固定 digest 的 Nginx 提供静态资产，不再运行 Vite dev server。
当前调试 UI、Playground 和 Langfuse 只开放在内网管理面，不作为公网安全边界。

Standby slot 禁止业务写入和后台任务。PostgreSQL leader lease 保证任一时刻只有一个
active writer。增加仅在内部网络可用的部署控制接口：

- drain/activate；
- active requests、Agent runs 和 leader 状态；
- release、slot、schema 和 config revision 身份。

这些接口使用独立 deploy credential，不进入公开 OpenAPI。

### 4.3 生产晋级与回切

`make tag` 增加前置检查：

- 工作区干净；
- HEAD 是受保护 master 的精确 SHA；
- Staging attestation 成功且 bundle digest 一致；
- VERSION 与 `v<VERSION>` 一致；
- 远端 tag 不存在。

生产只接受同一个签名 bundle，不重新构建，也不拉取可变 tag。

晋级顺序：

1. 验证 tag、bundle、Staging attestation、SBOM、签名、漏洞和 trust anchor；
2. 启动 inactive slot 为 standby；
3. 完成只读及隔离事务 smoke；
4. active slot drain，最长 30 秒；
5. 执行一次 expand migration；
6. 新 slot 获得 leader lease，原子更新 Nginx upstream；
7. 执行真实读写 synthetic；
8. 观察 30 分钟，旧 slot 保持 standby。

自动回切条件：

- 连续两次 readiness 失败；
- 2 分钟窗口内 5xx 超过 1%；
- 连续三次 synthetic 失败。

切换后前 5 分钟命中条件时自动回切，目标不超过 60 秒；此后只暂停晋级并要求人工回切。
数据库不随代码自动回滚。PostgreSQL 启用 PITR，灾难恢复必须先在隔离实例验证。

### 4.4 阶段二完成门

- 在生产等价环境连续完成 10 次蓝绿切换；
- 覆盖 standby 写入、leader 竞争、drain 超时、迁移失败、router reload 失败、
  synthetic 失败和双 controller 竞争；
- 至少 3 次自动回切成功，零丢写；
- 完成一次 PostgreSQL PITR 演练，达到 RPO 不超过 5 分钟、RTO 不超过 30 分钟；
- PITR 后 registry、import receipt、change set 与全部业务 Agent workspace HEAD 对账成功；
- Staging 和生产的 bundle digest、image IDs、SBOM 与 provenance 完全一致；
- 上一条只证明平台和仓库声明基线一致；任何 Agent 行为级验收均列出实际
  `agent_id + agent_version_id`，不存在跨 commit 复用证据；
- 当前生产版本能在新 schema 上运行，证明 N/N-1 兼容；
- production release、GitHub tag、AID 和 attestation 可互相追溯。

## 5. 测试、策略与文档同步

### 5.1 测试资产动作

| 资产 | 动作 | 要求 |
| --- | --- | --- |
| controller lineage/AID/state/outbox 测试 | `KEEP` | 保留 exact SHA、幂等和凭据隔离 |
| workflow/shell 字符串测试 | `REFACTOR` | 改为 YAML 契约、真实 CLI 和 Compose 行为 |
| 旧 restart/legacy/release-live 测试 | `DELETE-CANDIDATE` | 对应行为删除时同步删除 |
| env/trust/bundle/migration/恢复测试 | `GAP -> KEEP` | 覆盖正常、边界、失败和敌意输入 |
| seed/workspace 版本血缘测试 | `GAP -> KEEP` | 覆盖空卷初始化、已有卷不回灌、多入口同一 Git HEAD、证据版本绑定和 DB/FS 恢复对账 |
| PostgreSQL/蓝绿/leader/PITR 测试 | `GAP -> KEEP` | 阶段二阻塞生产准入 |

`tests/quality_policy.json` 必须增加 workflow executor 映射并修复：

- 声明 trigger 与实际 workflow 不一致；
- `real_container_ui_target` 未执行；
- mutation 多 target 使用最弱阈值；
- shadow 历史 evaluator 未接入；
- p95、flaky 等无消费者预算。

TIA/xdist 继续保持 shadow。只有达到 20 组同 SHA、跨 14 天、零漏测和零并行特有失败，
并通过独立治理变更后才允许晋级 blocking。

### 5.2 AGV 验收

`current` 必须表示每条 criterion 都有完整证据，不能继续使用“自动验收（部分）”。

AGV-007、AGV-019、AGV-039、AGV-040 和 AGV-042 先降为 `gap`。迁移 manifest 时，
其他存在未覆盖 criterion 的用例按相同规则降级，不为维持数量放宽证据。

证据类型只允许：

- pytest；
- UI script；
- Staging release evidence；
- 精确 SHA 的人工 release evidence。

将 `pytest` 用作 Agent 行为证据时，只有登记执行器生成可信 evidence，并绑定精确源码 SHA、
run/attempt 和被测 `agent_version_id` 才能满足相应门；普通代码测试仍按其源码 SHA 和
lane 契约取证。workspace 包内测试、开发者本地 pytest、测试目录存在性和 import receipt
都只是自测或审计事实，不能推导“行为已验证”。立即激活的 workspace 在没有对应可信证据时
保持 active，但相关 AGV criterion 必须继续是 `gap` 或未覆盖。

### 5.3 配置与文档动作

本表是全文唯一会删除既有资产的地方，因此每行的动作都必须先对仓库复核过再执行：文档判断错一行只是措辞，
删除动作错一行会打断硬门，且事后难以从症状回溯到根因。下表已按此标准逐行核实。

| 当前配置面 | 问题 | 动作 | 目标配置面 |
| --- | --- | --- | --- |
| `AGENTS.md` | 无。它是 Codex 唯一根指令入口，且被测试与审计直接读取 | `keep` | 保持根入口；受行数门与 codex-config 审计约束 |
| `docs/codex_setting_review_reports/*` | 历史复盘被 `AGENTS.md` 列为硬门来源 | `降级引用` | 复盘留作证据；硬门来源改指 `tests/quality_policy.json` 与本文收口出的宪法 |
| `.codex/skills/project-skill/SKILL.md` | 卷路径与 env 措辞违反根规则 | `edit` | 卷路径改指 `${HOME}/volume-agent-gov` 并注明 `docker/volume/` 仅迁移来源；env 改为“选择一份完整 env”；同步核对 `.claude` 侧镜像 |
| PAT Staging 文档 | 稳定契约与主机操作混写 | `split` | 稳定契约留 docs，操作流程进 release runbook |
| 测试治理文档 | 声明强于真实执行器 | `merge` | 机器事实引用 quality policy |
| workspace 导入与热加载方案 | 包、API、激活、回滚专项契约 | `keep` | 本文只引用版本血缘和交付边界，不复制状态机 |
| workspace 资产闭环方案 | 导出、实例化、离线归档专项契约 | `keep` | 本文只引用 seed 回流与声明源边界 |
| 本文 | 评审与阶段实施计划 | `keep` | 评审通过后作为长程整改入口 |

前两行需要说明，因为初稿曾把它们判反：`AGENTS.md` 自身把历史复盘列为“治理硬门来源”，这容易让人把
**引用了复盘的根权威**误当成**复盘本身**。实际上它是仓库唯一的根级 Codex 指令入口，承载治理对象矩阵、
配置面矩阵与 UI 语义矩阵等常驻红线；`tests/test_codex_config_optimizer.py` 无条件读取它，
`audit_codex_config.py` 把它纳入审计文件集并施加行数门。删除它会让 `make test` 与 `make codex-guard`
同时失败，并使 Codex 侧失去唯一根入口。该删的是它引用的那份历史复盘的硬门地位，不是它本身。

project-skill 一行同理收窄为 `edit`：经核实问题只存在于 `.codex` 侧的两行措辞，`.claude` 侧镜像并无同类
表述；对整个 skill 下 `delete/merge` 会连带删除与本缺陷无关的架构与验证清单。两侧 project-skill 是同源镜像，
单改一侧会被 `codex-guard` 的镜像漂移检查拒绝，故动作里写明同步义务。

实现收尾时使用 `agentgov-closeout-sync` 核对 README、docs、AGENTS/CLAUDE、skills、
测试策略与实际代码一致。

## 6. 分层验证

阶段一至少运行：

```bash
git diff --check
actionlint
make codex-guard
make typecheck
make test
make main-flow-test
make runtime-volume-seeds-scan
make container-health-e2e COMPOSE_ENV_FILE=<ephemeral-env>
.venv/bin/python scripts/check_ci_cd_governance.py --mode fail --local
.venv/bin/python scripts/check_ci_cd_governance.py --mode fail --remote
.venv/bin/python scripts/check_agv_acceptance.py --mode fail --collect-pytest
releasectl artifact rehearse --ref <SHA>
releasectl deploy-staging --artifact <digest> --fault-injection <scenario>
```

阶段二追加：

```bash
make mutation-test
.venv/bin/python scripts/run_release_migration_rehearsal.py --from <N-1> --to <N>
.venv/bin/python scripts/check_supply_chain_policy.py --mode fail
releasectl rehearse-production --artifact <digest>
releasectl rehearse-pitr
```

局部测试或 coverage 百分比不能替代真实 Compose、真实数据升级、远端保护和发布恢复证据。

## 7. 实施顺序

实施严格按以下顺序推进，每一步通过自己的验收门后才能进入下一步：

1. 关闭自动部署并保存当前运行证据。
2. 修复 Compose env、workflow 语法和真实 container contract。
3. 建立机器 policy、AGV manifest、远端审计和外置信任锚。
4. 固定依赖、Actions 和镜像，把 seed catalog/managed policy 摘要纳入单次构建的签名
   ReleaseBundle。
5. 拆分发布控制器，完成 artifact-only Staging 部署。
6. 拆出 migration，完成快照、业务 Agent Git 对账、N/N-1 和故障恢复。
7. 连续完成阶段一验收，重新启用 Staging 自动部署。
8. 将 Staging 迁移到 PostgreSQL，完成 SQLite 数据导入验证。
9. 建立 shared/blue/green 拓扑和 leader/drain 协议。
10. 完成生产等价演练、PITR 和同一制品晋级验收。
11. 删除旧部署入口和失实文档，完成最终知识同步。

## 8. 默认假设与残余风险

- Staging 继续使用现有 228 控制器和 232 目标机。
- 生产目标是内网生产；蓝绿先部署在同一 Docker 主机。
- 本阶段解决应用发布零停机，不解决主机级高可用。
- 长期单维护者意味着不存在真正的职责分离。外置信任锚能阻止同一 PR 自动自批，
  但不能阻止维护者主动修改主机信任根；该风险必须保留在最终宪法中。
- 阶段一不 bump VERSION、不创建 tag。
- 阶段一除健康元数据外不修改公开业务 API。
- 阶段二新增的 drain/slot 接口是内部部署接口，不进入公开集成契约。
- Staging 和生产均保留 workspace 校验后立即激活；该入口不会自动获得 EvalRun、AGV
  或 release attestation 的行为验证资格。
- 同一 bundle 不要求两个环境的可变业务 Agent workspace HEAD 全部相同；只有明确声明
  Agent 行为等价时，才要求相应 `agent_id + agent_version_id` 和证据一致。
- 立即激活允许未经过行为回归的新 workspace 进入生产，这是已接受的残余风险。精确版本、
  审计、维护栅栏和人工回滚缩小可追溯与恢复风险，但不能等价替代发布前验证。
- 不新增 `WorkspaceReleaseBundle`、第二套 Agent 发布状态机或独立 active-version 字段。
- 本计划评审通过前，现有 CI/CD 能力不得按目标状态对外宣称。
