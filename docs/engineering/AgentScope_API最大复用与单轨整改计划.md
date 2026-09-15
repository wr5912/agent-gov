# AgentScope API 最大复用与单轨整改实施与验收记录

> 状态：截至 2026-09-12，P0–P4 已实施，P5 真实容器与浏览器验收正在进行。
> 目标：AgentScope 已提供的运行能力使用原生公共 API，删除 AgentGov 的重复协议、运行态目录和配置写入路径。
> 本文不替代当前实现基线，也不因代码、静态测试或文档完成而表示 P5、正式部署或真实业务验收通过。

本文最初用于规划，现按当前代码转为实施与验收记录。外部 Runtime 替换与 Langfuse 需求草案只按
当前架构、公开契约和实际实现选择性吸收；未全盘复制草案中的版本、存储、观测正文或未实现设计。

## 1. 决策与范围

采用“原生运行 API + Git 版本事实 + AgentGov 薄治理编排”。复用不是在旧实现外再套一层代理，
而是迁移消费者后删除原实现；也不是把原生全部写接口直接暴露给浏览器。

单轨有五个可检验含义：

1. 一个运行实现：Agent、Session、Message、AgentState、工具执行和恢复由 AgentScope 承担。
2. 一个配置写入链路：资产变更进入 Git 候选，测试审批后发布；活动 Harness 不允许原地修改。
3. 一个原生协议来源：公开 OpenAPI 与上游公开模型派生类型，不维护手写的同义协议。
4. 一个激活入口：发布编排创建原生 Agent 并提交版本绑定，取消用户另行“启用 Runtime”的必经步骤。
5. 一个在线路径：切换后旧 API、DTO、目录扫描及兼容分支退出；底层失败显式报错，不读本地镜像兜底。

整改覆盖 Agent 创建配置、发布激活、会话管理、运行资源展示及共用执行链路。
知识库、渠道、定时任务等当前未启用能力纳入复用清单，但不以本次整改名义无条件启用。
未来启用时仍只能使用原生能力，不能另建替代系统。

### 1.1 治理对象与事实所有权

| 对象 | 唯一事实来源／写入者 | AgentGov 保留内容 | 生命周期与限制 |
| --- | --- | --- | --- |
| 业务 Agent | AgentGov 稳定治理身份；行为内容来自 Git | 身份、治理状态、Git 根及版本关系 | 创建、治理、退役；不等同于单个原生 Agent |
| Harness、prompt、MCP 声明、skill、subagent、测试 | Git 候选及不可变提交 | 编辑、测试、审批、发布编排 | 候选到发布；禁止活动版本自修改 |
| 原生 Agent | AgentScope API | `业务 Agent + commit -> native agent_id` 绑定及摘要 | 每个发布版本不可变；部署可由 Git 重建，不能独立编辑 |
| Session、Message、AgentState | AgentScope | 授权范围、版本归属、必要关联引用 | 创建、交互、恢复、删除；不复制正文或会话标题 |
| 运行 Workspace 及实际资源状态 | AgentScope Workspace/API | 路径准入、版本约束、治理关联 | 区分只读 Harness 和可写运行数据；文件存在不等于已加载 |
| `run_id`、反馈、审批、改进事项 | AgentGov | 本域完整契约及持久化 | 与原生 Session/reply 生命周期不同，不冒用其 ID |
| 语义轨迹 | Langfuse | `trace_id` 引用、受控摘要 | 不存原始 prompt、输出、工具参数或凭据 |
| 治理 Agent、`main` 样板、初始化源、离线工具 | 各自所属治理／Git 边界 | 明确执行者、样板和工具用途 | 不成为特殊运行轨道或第二套模板管理平台 |

反馈按业务 Agent、精确版本、场景和 run 归属。执行资产、方法论资产、数据／证据资产分别管理；
版本、来源、审计和访问范围是横切维度，不通过新增正文副本“沉淀资产”。

闭环固定为：业务 Agent → 原生运行 → 反馈 → 归因 → Git 候选 → 测试与审批 →
原生发布绑定 → 线上效果与资产引用。治理 Agent 只是执行者，不与被治理业务 Agent 混同。

### 1.2 依据、替代方案与修订条件

实际问题是已有原生 API 未成为所有运行视图和协议的权威，而文件编辑又未贯通不可变发布。
第 2 节给出当前代码证据，第 3 节给出底层能力边界。

- 不选“原生控制台直接编辑生产 Agent，事后同步 Git”：操作简单，但会造成发布内容和实际配置分离。
- 不选“保留当前管理后端，再增加原生管理页”：迁移成本低，但两个入口长期解释、修改同一事实。
- 选择“原生能力统一接入，Git 候选唯一写入，发布统一物化”：保留必要治理，不重写底层引擎。
- 若固定版本增加 Git 事务、版本化配置或安全受限的 Workspace 写 API，应重新评估并删除可被替代的编排；
  在能力实际可用并验收前，不使用私有导入、核心补丁或临时 fallback。

## 2. 整改前事实与迁移对象

核验来源为用户指定的 [AgentScope API 文档](http://127.0.0.1:8000/docs) 及其
[OpenAPI](http://127.0.0.1:8000/openapi.json)：AgentScope **2.0.8，67 个路径、86 个操作**。
本次只读取文档及代码，没有调用创建、删除、安装等业务操作。
该地址是契约参照，不表示 AgentGov 已连接此实例，也不授权合并两个实例的存储。

下表固定记录首次评审时的迁移对象，不是当前源码目录。`catalog.py`、`agent_loader.py` 等旧
入口已删除，运行目录现取自原生 API；当前落地状态以第 6 节及
[Runtime 实施基线](./AgentGov_AgentScope_Runtime替换实施基线与验收.md) 为准，不恢复历史入口。

| 整改前证据 | 当时结论 | 整改要求 |
| --- | --- | --- |
| `app/runtime_gateway/client.py`、`router.py` | 已调用原生 Agent、Session、chat、messages、status、stream | 保留并收口，不重建 Session 服务 |
| `frontend/src/types/runtime.ts` 的 `AgentScope*` 类型 | 手写上游 Session、消息、事件及输入协议 | 删除，改为上游派生类型 |
| `frontend/src/api/runtime.ts` 的 `sessionViewToSessionInfo` | 兼容多种字段，补当前时间和默认轮次 | 删除历史 wire 兼容及补造事实；只留展示 selector |
| `app/routers/catalog.py`、`app/runtime/agent_loader.py` | `/api/agents` 实际扫描 subagent，`/api/skills` 扫描文件 | 不再充当运行目录；候选资产回到 Git 文件视图 |
| `app/runtime/config_mapping.py` | 从路径存在推导 `runtime_loaded/runtime_materialized` | 删除假运行状态；使用原生资源结果 |
| `app/services/agent_config_files.py`、`AgentConfigFileEditor.tsx` | 写 Workspace 后未提交 Git、未创建新绑定，却提示后续运行使用最新配置 | 迁入候选编辑；取消“应用即生效”的接口语义和话术 |
| `app/services/agent_release_workflows.py`、`frontend/src/App.tsx` | 发布与显式 provision 是两个用户步骤 | 统一为可恢复的发布激活编排 |
| `app/runtime_gateway/router.py`、`execution.py` | UI 和后台分别编排资源创建、运行准入、触发和失败处理 | 共用一个编排实现；流消费方式可以不同 |
| `agentscope_runtime/access_middleware.py` | 白名单仅覆盖部分 Agent/Session API | 根据操作级策略扩充，禁止通配反向代理 |
| `agentscope_runtime/workspace_manager.py` | skill 读只读 Harness；原生写可能落到可写 Workspace，MCP `.mcp` 又被拒绝持久化 | 先建立候选构建边界，不能直接开放运行态写入口 |
| `agentscope_runtime/service.py` | KB manager 未配置，索引／渠道／scheduler worker 关闭，Hub 列表为空 | 文档里有路由不等于当前服务已启用能力 |

已有治理 Registry、版本绑定和 run 记录不因名称相似就删除：稳定业务身份、不可变部署和单次运行
是不同对象。必须删的是同一事实的第二份可编辑定义、无用途镜像和手写协议，不是必要关联。

## 3. 原生 API 复用清单

下表覆盖实测的全部 86 个操作；操作数用于核对底层清单，不是“全部开放”的目标。
实施时按 `method + path` 展开为机器可校验策略，逐项标注接入方式、资源范围、写入者和验收场景。

| 能力族／操作数 | 原生接口 | 目标处理 |
| --- | --- | --- |
| Agent／6 | `/agent/`、`/agent/{agent_id}`、`/agent/schema/v2`、旧 `/agent/schema` | 复用 list/create/delete 与 v2 schema；禁止已绑定版本 PATCH；不接 deprecated schema |
| Session／8 | `/sessions/`、`/{session_id}`、`messages/status/stream/interrupt` | 全部会话操作走原生；AgentGov 仅做鉴权、跨版本聚合、运行关联与必要命令编排 |
| Chat／1 | `POST /chat/` | 唯一触发端点；UI、后台测试、治理运行共用调用及准入规则 |
| Workspace／13 | `directories/files/status`、下载 token、MCP/skill 挂载与上传 | 运行读取复用；写入只准隔离候选构建；旧服务端路径 skill 安装不接入 |
| MCP 库／3 | `GET /mcp`、`PATCH/DELETE /mcp/{mcp_id}` | 原生持有库记录；与某版本安装声明分开；库变化不得暗改已发布 Harness |
| Skill 库／3 | `GET /skill`、`GET/DELETE /skill/{skill_id}` | 复用库目录与详情；不再维护 AgentGov 运行 skill 目录 |
| Hub／8 | `/hub/mcp`、`/hub/skill` 及 cards/detail/install | 配置原生 Hub 后接入；库安装和 Workspace 安装是不同动作；保留离线来源 |
| Credentials／5 | `/credential/schemas`、credential CRUD | 凭据真值只在独立 Runtime 管理通道；不经 AgentGov API／前端传递；只允许必要无秘密元数据 |
| 模型／3 | `/model/`、`/tts-model/`、`/embedding-model/` | 复用原生模型发现，不维护第二份模型目录；列表成功不等于模型可调用 |
| Knowledge／16 | `/knowledge_bases/` 及 documents、search、chunks、参数 schema | 当前不强制启用；需要时只接原生管理、索引和检索，不另建知识库后端 |
| Schedule／5 | `/schedule/`、`/{schedule_id}`、`/{schedule_id}/sessions` | 当前不强制启用；启用前解决发布绑定、权限与 run 关联，不新增 AgentGov 调度执行器 |
| Channels／14 | `/channels/`、types、bindings、启停、状态及 sessions/chat_ids | 当前不强制启用；启用前验收消息入口的治理准入，不允许绕过 run／审批边界 |
| Health／1 | `GET /health` | 复用原生健康；依然需要可信身份头，不能替代功能验收 |

### 3.1 不伪造底层能力

- 当前没有通用 Workspace 文件写／删、Git commit/log/diff/checkout、完整 Agent 包导入导出 API。
  安全导入导出、候选文件变更、版本比较、测试审批与发布因此仍属 AgentGov／Git 编排。
- 当前没有 subagent 管理 HTTP API。使用 Git Harness 及公共 `create_app` 扩展，不另建运行 subagent 目录。
  当前 `service.py` 在启动时注册 subagent templates；模板类型包含 Harness 摘要，WorkspaceManager
  遇到未注册的新摘要会要求重启 Runtime。因此原生 `POST /agent/` 成功不能单独证明新版本可运行。
- Workspace skill/MCP API 要求 `agent_id + session_id`，不能直接当作未物化 Git 草稿的编辑 API。
- Agent／Session 列表没有原生分页参数；消息使用 `before + limit`，不恢复 deprecated `offset`。
  Hub 和 KB 使用各自原生分页契约。UI 本地分页不得伪称底层服务端分页。

## 4. 目标契约与用户流程

### 4.1 原生契约、传输与治理附加信息

1. 从固定版本的公共 `create_app()` 导出 OpenAPI，在构建中生成原生 TypeScript 类型／客户端。
   生成过程不依赖开发者的 `127.0.0.1:8000` 常驻服务；上游升级必须重新核对操作清单与语义。
2. OpenAPI 有缺口：SSE 响应 schema 为空，历史 `messages.items` 也为空。
   对这两处使用公共 `agentscope.event.AgentEvent`、`agentscope.message.Msg/ContentBlock` 自动派生，
   或经版本匹配验证的官方 `@agentscope-ai/agentscope/event`、`/message` 类型。
   P0 固定一种来源并锁定版本，不同时保留两种实现；不能将空 schema 扩写为手写事件协议。
3. AgentGov 自有公开类型只从自己的 OpenAPI 派生。原生对象和治理附加信息分别命名：
   `SessionView` 保持原生，`active_run_id`／版本归属属于独立治理关联，不回写原生消息或状态。
4. 保留一个受控 HTTP client、一个身份／资源绑定解析入口、一个 run admission/trigger/cancel 实现。
   UI 消费原生 SSE，后台等待结果并读取原生 messages；二者不维护不同的启动、幂等和失败规则。
5. SSE 原始字节、未知事件、事件 ID 和顺序透传；不为了生成类型而重新编码流。
   原生错误只增加必要治理上下文，不把失败改成空目录、成功响应或本地缓存结果。
6. 会话状态使用原生 status；删除对 deprecated `SessionView.is_running` 的依赖，覆盖 HITL 等待。
   run 的取消意图、审批、fencing 仍属治理事实，不能用 session status 覆盖 run 生命周期。

开放 JSON 仅允许原生明确开放的 metadata、未知事件和真实边界 payload；内部 records／store
使用有所有者的类型，不新增无来源 `dict` 协议。安全脱敏投影必须明确派生关系和字段准入。

### 4.2 Agent 创建、配置与发布

目标支持原生 schema 驱动的创建／配置表单；包导入保留为另一种输入形式。
二者必须调用同一个候选创建命令，产出同一个 Git Harness，不形成两种 Agent 生命周期。
这会替换当前“普通 Agent 只能包导入”的产品限制，但不恢复旧 seed／模板 catalog。

- 表单字段以 `/agent/schema/v2` 为准，保留原生校验与错误；治理层只声明可编辑字段子集和版本规则。
  Provider 凭据、后端所属字段、活动版本标识不得由表单注入。
- 该 schema 覆盖 `name/system_prompt/context_config/react_config/invite_config`，不是整个 Harness schema。
  模型选择属于 Session；MCP、skill、subagent、权限声明和测试分别使用其原生公共契约或 Git 治理契约，
  不能以“消除 AgentData 分叉”为由删掉这些不同职责的配置。
- 原生字段到 Git 文件的映射集中在一个边界，验证无静默丢字段；扩展字段必须声明 Git 归属或显式拒绝。
  不继续维护一个与 AgentData 分叉的可编辑 Agent 配置模型。
- 名称／描述指定唯一内容来源；业务展示名称与原生部署资源名如用途不同，应明确命名，不能互相回填覆盖。
- 编辑只形成候选变化，显示“已保存候选，尚未发布”；已有 Session 始终使用原绑定。
  发布成功后新会话使用新版本，旧会话继续旧版本，不做“下一 turn 偷换配置”。
- 测试、审批通过后，发布编排以精确 commit 生成 Harness，调用原生 `POST /agent/`，验证部署绑定，
  最后原子提交活动版本指针。正常用户流程不再要求额外点击“启用 Runtime”。
- 发布前验证新版本 subagent 模板是否已注册。P0 核定公共扩展的刷新能力；若只能启动注册，必须走受控维护门：
  展示受影响 Agent／Session 并获得维护授权，暂停新运行、处理在途运行、准备模板、重启 Runtime，
  验证 readiness、原生状态恢复及旧会话继续能力后才激活。未完成不得标记发布可用，不私改内部模板 registry。
- 生产绑定不允许 `PATCH /agent/{id}`。禁止为消耗这个 API 再引入一套可编辑的原生生产配置；
  使用原生 create 创建不可变版本，本身就是复用其 Agent 管理能力。

发布不是跨 Git、HTTP、SQLite 的虚假单事务：远程创建在数据库事务外执行，采用持久化操作记录、
唯一版本约束和现有稳定资源引用查询实现重入。网络超时先查创建结果，不假定底层支持幂等键。
失败保留旧活动绑定，新版本不得显示已激活；孤立资源只按本次操作所有权补偿。
删除业务 Agent 先停新运行、处理在途运行和引用，再调用原生删除；逐步记录、可恢复，不在 DB 事务内删文件。

### 4.3 会话与运行资源管理

| 用户动作 | 业务产物／原生操作 | 治理副作用与 UI 归属 |
| --- | --- | --- |
| 新建、选择、重命名会话 | 原生 Session create/list/PATCH | 校验 Agent 与版本范围；会话侧栏展示原生标题、时间和状态 |
| 查看历史、继续对话 | 原生 messages、stream、chat | 关联 run／审批；消息正文不入治理库；恢复仍指向原 native session_id |
| 停止运行、删除会话 | 原生 interrupt／DELETE | 对当前 run 做身份与 fencing 检查；显示真实失败与重试入口 |
| 查看 Workspace 可用 skill、MCP 连接状态与工具目录 | 原生 `/workspace/mcp`、`/workspace/skill` | 展示所属 Session 和版本；连接错误明确可见；可发现不等于已在回复中加载／执行，连接成功不等于业务调用成功 |
| 查看运行文件 | 原生 directories/files/status／下载 | 强制沙箱路径、对象范围、敏感内容和 token 准入；不是宿主机文件浏览器 |
| 编辑配置、安装候选资源 | 候选变更／受控原生安装 | 归候选版本页，不推进生产状态；显示差异、测试条件和未发布状态 |
| 发布并激活 | 原生 Agent + 不可变版本绑定 | 在既有发布入口完成业务动作，状态变化是结果，不新增独立治理阶段 |

跨历史版本的会话列表可以保留 AgentGov 聚合查询，但底层唯一数据来源是原生 Session API。
Registry 列表展示业务治理对象，原生 Agent 列表展示具体部署，界面需区分，不能称为两套可编辑 Agent 配置。
浏览器重命名只放行 Session PATCH 的 `name`；模型／fallback、permission mode、cwd、知识库和凭据引用等
字段继续由受控创建／治理命令决定，不把完整原生 PATCH schema 当作可编辑表单。验证越权字段及 `null` 清空均被拒绝。

### 4.4 候选资源安装只允许一个写入者

原生 MCP／skill 安装和上传优先复用，但当前 WorkspaceManager 尚不具备安全的候选构建模式。
实施顺序是先通过公共 WorkspaceManager／Workspace 扩展建立隔离构建区，再接入这些写 API：

1. 从指定候选 commit 生成临时构建 Workspace，记录业务 Agent、基准 commit 和操作身份。
2. 创建隔离原生 Agent／Session，仅允许该构建区接收原生安装；不得指向活动 Harness 或生产 state。
3. 将安装产物校验、归一化后一次性纳入 Git 候选；检查基准未变，冲突不覆盖他人编辑。
   不整体提交原生 `.mcp` 或已解析的 MCPClient 配置；只提取受控声明与凭据引用，不回流 Runtime
   解析出的秘密。验证“原生安装产物 → Git 候选 → 发布加载”的语义一致性，不能靠脱敏把安装变成无效配置。
4. 成功返回新 commit／差异；构建区不是第二个配置源，结束后按所有权回收临时原生资源。
5. 测试发布后从 Git 物化新的只读 Harness；不得同时保留旧自定义安装器和原生安装器供线上选择。

能力准入用真实安装／失败恢复验证。若公共扩展无法满足资产导出、路径隔离或一致性要求，
该写功能保持关闭并记录明确底层缺口；不私改核心、不把运行 Workspace 当“临时候选”长期留存。
库内容变更或 Hub 卡片更新不得动态修改已发布版本；安装结果必须冻结为可审计的版本资产／摘要。

### 4.5 安全与原生能力开关

- 浏览器身份经 AgentGov 认证后映射并注入 `X-User-ID`，不信任用户提交的身份头、原生 Agent ID 或路径。
  保留现有 Gateway → Runtime 的 HMAC（method、原始 path/query/body）、时间戳和防重放验证，
  新增原生路由不得绕过签名链；身份头注入不替代服务间认证。
- 白名单按操作、对象所有权、发布／候选模式和允许字段判定；原生路由新增默认拒绝，不用 `/workspace/*` 放行。
- 原生文件 API 允许访问 Workspace 根外的后端可达路径，必须叠加沙箱与规范化路径限制，验证 `..`、绝对路径、
  符号链接和下载 token 的越权；无法建立边界时不开放对应操作。
- Credentials owner 读取可能返回完整 data，不能裸透传；模型 Provider 真值只属于 Runtime，不进入
  AgentGov API、前端、Git Harness 或日志。现有合法私有 live Workspace 资产按原安全边界保留，
  不自动脱敏改写或回流源码仓库；安装过程不得将 Runtime 解析出的秘密新写入候选资产。
  改变既有敏感配置必须有明确迁移方案；回流仓库初始化源仍需经过 `runtime-bootstrap` 准入扫描。
- `GET /workspace/mcp` 会实际连接并列举工具；按受控 endpoint／网络权限执行，不能把 HTTP GET 视为无外部副作用。
- Knowledge、Schedule、Channels、Hub 先配置原生组件，再验证权限、版本、run 关联和离线可用性。
  对未启用能力显示明确状态，不展示可点击却不可执行的管理功能。

## 5. 分阶段实施与退出门

以下阶段编号仅属于本整改，不替代已有测评／Governor P1、P2B 等计划。实施顺序为
P0 → P1 → P2/P3 → P4 → P5；P2 与 P3 在接口边界冻结后并行收口。P0–P4 已进入当前
4.0.1 工作树，P5 仍须用同一工作树重建的真实服务完成，不以已实现代码预签验收结论。

| 阶段 | 当前状态与实现边界 | 当前证据或剩余门槛 |
| --- | --- | --- |
| P0：能力与所有权冻结 | **已实施**：固定 AgentScope 2.0.8 的 67 个 path／86 个 operation，并生成操作级准入策略；原生、治理限制、暂未启用与 deprecated 能力分别登记 | 生成物漂移与操作所有权由契约测试检查；公开能力缺口不以私有导入或 fallback 填补 |
| P1：协议与共用执行收口 | **已实施**：公开模型生成类型；Session、chat、SSE、run admission／cancel 共用执行边界；SSE readiness comment 后按原始字节透传上游未知事件 | 手写同义 wire DTO 与顶层 `session_id` fallback 已退出在线路径；仍须由 P5 真实浏览器检查事件时序与恢复 |
| P2：Agent 生命周期收口 | **已实施**：原生 schema 表单和 Workspace 包导入写入同一 Git 候选；候选以 change set 与预期 commit 做 CAS；测试、审批和发布 saga 是唯一激活链路 | draft 不可激活，发布以精确 commit 创建不可变原生 Agent 并提交绑定；P5 仍需真实发布旅程复验 |
| P3：会话与资源接入 | **已实施**：会话创建、列表、重命名、历史、状态、interrupt／delete 读写 AgentScope；MCP、skill 与 Workspace 状态按 Session 独立投影 | 没有 pending Session 或资源成功态的本地伪造；真实 MCP／权限／HITL 效果留给 P5 |
| P4：删除旧轨与契约迁移 | **已实施**：删除 live Workspace discard/snapshot、release restore/rollback、独立 provision 等旁路及陈旧 DTO；控制库当前 epoch 为 v4 | 精确 v1/v2/v3 先备份再事务迁移；通用 entities/event 与原生 chat 身份单次切换，旧治理关联和证据文件保留；活动 run、漂移或不支持的非空历史拒绝迁移。详细转换和回滚边界见 Runtime 实施基线 |
| P5：真实验收与原子切换 | **正在进行／待结论**：公共 Make 容器入口、真实浏览器、历史数据、发布恢复及正式部署 | 必须绑定当前源码镜像、真实 provider/MCP、真实 API/SSE/OTLP、浏览器交互与部署数据；本记录不预先声称通过 |

架构阈值在 P1 就处理：手写文件不超过 800 行、函数不超过 80 行、圈复杂度不超过 15、类公开方法
不超过 30、路由文件不超过 20 个路由。不能继续向接近或超过阈值的 `router.py`、`execution.py`、
`workspace_manager.py` 堆逻辑；按原生 transport、治理编排、候选构建和投影责任拆分。

建议一个操作策略来源生成 Runtime 允许列表并供契约测试消费，不另建多份字符串路由表。
它只记录安全／治理差异和上游来源，不复制整份 AgentScope schema，也不发展成通用插件平台。

## 6. 删除、迁移、保留清单

| 对象 | 动作 | 退出或保留条件 |
| --- | --- | --- |
| 手写 `AgentScope*`、重复 `RuntimeCurrentVersion`、Session wire 转换、旧 ID fallback | 删除／生成替代 | 前端、后台测试、API 消费者全部切换后，不留旧字段别名 |
| `/api/agents`、`/api/skills` 作为运行目录，`runtime_loaded/runtime_materialized` 路径推断 | 删除 | 运行视图改读原生；确需候选浏览则只保留 Git 文件资产语义 |
| `/api/agent-config-file` 直接写 live Workspace，保存即运行生效 | 删除／迁移 | 配置入口只写候选；旧路由、UI 话术和陈旧契约测试同批退出 |
| 独立“启用 Runtime”正常用户步骤及重复 provision 编排 | 迁移 | 纳入发布 saga；必要恢复操作仅重入同一发布命令，不成为第二激活入口 |
| UI／后台资源创建、chat、失败记账、interrupt 的重复逻辑 | 合并 | published／ephemeral 资源生命周期策略可不同；传输与治理状态规则只有一份 |
| `ChatRequest/ChatResponse` 的泛化重投影和旧路由描述 | 缩小／改名 | 只保留明确候选测试输入结果；原生消息引用上游类型，删除无消费者字段 |
| Agent Registry、Git import/export、候选编辑、测试审批、发布记录 | 保留并瘦身 | 仅承担原生 API 未持有的治理事实，禁止重新持有可独立修改的原生配置 |
| session/version bindings、run fencing、审批及 trace 引用 | 保留 | 逐字段证明授权、审计或恢复用途；不存 Session 标题、消息正文或 AgentState 镜像 |
| Runtime 受控代理、公共 Workspace／middleware 扩展 | 保留 | 只约束安全、版本和关联；不实现 agent loop 或私有 API 适配层 |
| 过渡迁移读取器 | 限定离线使用 | 一次迁移验收后退出活跃路径；不注册在线旧格式兼容路由 |

## 7. 数据、配置与文档同步

| 配置面 | 处理 |
| --- | --- |
| 当前整改与本记录 | P0–P4 应用、契约、迁移和文档已进入 4.0.1 工作树；P5 部署、真实容器／浏览器与私有数据验收另以现场证据记账 |
| 根 AGENTS／Claude 规则、guidance、skill | 核对单轨和 Git 不可变约束；实施若改变操作流程则同步双宿主 skill，不复制 API 清单进根规则 |
| script／hook／`config/*` | 操作级准入与禁止重复协议交给机器检查；规则单源，不用 docs 豁免新增债 |
| README／docs | 已同步 P0–P4 真实契约与 v4 迁移边界；P5 未完成项继续明确标注，本文由 docs 索引发现 |
| memory | 不写入工程事实或私有数据；本次不更新 memory |

数据迁移先做只读清单和可恢复备份：控制面 SQLite、业务 Agent Git、AgentScope 存储和版本绑定
分别标识所有者。只迁移必要治理引用，不搬运 Message／AgentState 进 AgentGov，不重造
`session_id/run_id/reply_id/trace_id`。悬空或冲突绑定进入可见失败清单，不静默认领陌生原生资源。

保持 `${HOME}/volume-agent-gov` 持久化根；`docker/volume/` 只作为历史迁移来源，不恢复双目录读取。
本计划不要求改变 env 选择或共享模型凭据。增加能力开关时同步所选环境示例、settings、Compose、
安全启动日志和策略测试；不得把本机调试结果作为容器验证。

原生 API 写入与 Git／DB 更新使用恢复记录及部分失败补偿，不在事务内调用远程删除或 `rmtree`。
切换时暂停新写入并核对在途运行，备份后切单一服务版本；恢复是停新版本后恢复旧整套状态，
不是在线 fallback／双写。整套切前快照恢复仅限尚未开放真实业务写入的验证窗口；恢复业务写入即关闭该窗口。
之后必须采用保留新增 Session/message/run 的无损前向修复，或另行取得明确有损恢复授权并验证影响，
不得用切前备份覆盖新业务数据。不得绕过或自行恢复当前已禁用的原子切换执行入口；未满足现有硬门则不切生产。

文档实施同步清单：

- [当前 Runtime 基线](./AgentGov_AgentScope_Runtime替换实施基线与验收.md)：同步管理 API、发布绑定和验收证据。
- [术语与版本边界](../AgentGov术语与版本边界.md)：澄清业务 Agent／原生部署／候选／运行 Workspace，
  同步创建输入限制的目标变更，不恢复运行时选择器。
- [Workspace 包方案](../业务AgentWorkspace包导入与热加载产品工程方案.md)：消除“下一 turn 生效”与不可变 Session
  的冲突；包导入与表单只表达不同输入，发布只有一个入口。
- [集成指南](../AgentGov集成指南.md)、[核心测试用例](../AgentGov核心功能测试用例.md)、
  [文档索引](../README.md)：同步删除 API、原生契约来源、状态与错误行为，不保留平行权威说明。

## 8. 测试同步与验收

| 测试层／动作 | 同步内容 | 必须有的场景证据 |
| --- | --- | --- |
| 原生契约：保留并扩充 | 固定包公共 API 的真实 HTTP 测试、生成物漂移检查 | schema、create/list/delete、Session PATCH、历史游标；错误不被替换为成功 |
| 原生类型：重构 | 替换手写 DTO 的测试与 fixture；OpenAPI 空 schema 使用公开模型补齐 | 未知 SSE 事件保留；原生新增字段不被手写投影丢弃 |
| 配置与目录：删陈测、补行为 | 删除“文件存在即加载”“编辑即生效”“仅包可创建”等旧行为断言 | 表单／包同一候选产物；候选失败不改活动版本；实际 MCP 失败可见 |
| 生命周期与并发：保留并收口 | 发布、模板注册、资源创建、准入、取消、清理共用测试 | 重复发布、响应丢失、竞争编辑、非法状态转移、部分成功及恢复；模板未注册拒绝激活，维护重启后旧会话恢复；只补偿本次拥有资源 |
| 安全：补负向场景 | 签名与重放、身份、跨 Agent／Session、路径、安装、凭据与后端字段污染 | 伪造身份／native ID 被拒；Session PATCH 越权改 permission/cwd/credential 或 null 清空被拒；活动 Workspace 写、越权路径／token、secret 输出被拒 |
| UI：迁移并补完整操作 | Agent 创建配置、版本、会话列表、运行资源、失败重试 | 新建→编辑候选→发布→新会话；旧会话版本不变；重命名、刷新恢复、历史翻页、HITL、取消与删除 |
| 历史数据：新增迁移验收 | 脱敏清单及仓库外真实备份演练 | 多业务 Agent／多版本 Session 归属正确；悬空引用不导致越权、500 或静默丢失 |
| 删除旧轨：新增负向硬门 | 旧路由注册、旧 DTO 导入、目录 fallback、私有导入扫描 | 旧入口不可调用；底层不可用时不切本地数据；控制库不新增消息副本 |

实施阶段按风险运行：

1. 日常目标后端／前端测试，以及 OpenAPI 导出、类型生成和前端构建。
2. 共用执行、发布和可见 UI 变化运行 `make main-flow-test`，同步 `tests/quality_policy.json` 的 owner、lane 和场景绑定。
3. `make codex-guard`；最终 `make test` 及串行 `main-full` 要求，不能用 TIA／并行 shadow 替代。
4. 真实容器验证只走公共 Make 入口，基于当前工作树重建镜像并 force-recreate，加载所选配置；
   浏览器实际操作验证请求、结果、错误、刷新恢复和持久化，不使用伪 API、请求拦截或预制页面状态。
5. 发布证据继续满足[现有 Runtime 验收硬门](./AgentGov_AgentScope_Runtime替换实施基线与验收.md)：
   真实模型、MCP、浏览器、并发、恢复和 OTLP／Langfuse，不用文档存在、HTTP 200 或静态测试替代。

P0–P4 的实现状态须由当前源码及回归门共同证明；P5 结论只接受公共 Make 入口重建的真实容器、
真实浏览器和现场部署证据。文档差异检查、静态治理、HTTP 200 或测试替身均不能替代这些证据。

最终验收不是“新增多少代理接口”，而是：所需原生能力均从同一底层读取／执行；手写原生协议与旧运行目录清零；
所有资产写入可追到唯一候选 commit；发布激活与恢复只有一个入口；真实 UI、历史数据和容器证据证明旧轨已退出。
