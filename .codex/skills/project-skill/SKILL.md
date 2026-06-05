---
name: "project-skill"
description: "团队开发通用技能：在编写、评审、调试或重构代码时，强制应用项目工作流、环境规范、安全规则和简洁精准的工程行为。"
license: "MIT-compatible adaptation with attribution"
---

# 项目开发技能

当任务涉及代码编写、代码评审、调试、重构、配置调整、测试补充或构建修复时，使用本技能。

## 来源与适配

本技能结合团队通用环境、工作流和开发标准，并改写保留 `forrestchang/andrej-karpathy-skills` 的通用工程行为规则；项目专属补充应放在 `AGENTS.override.md`。

来源仓库: https://github.com/forrestchang/andrej-karpathy-skills
许可: MIT

## 快速检查

开始前：

- 是否已经读过 `AGENTS.md` 和 `AGENTS.override.md`？
- 是否已经读过 `.codex/rules/project.rules`、`architecture.rules` 和 `verify.rules`？
- 是否已经确认相关代码、配置、测试和文档？
- 是否已经检查待修改文件的行数、类/函数/路由规模、复制风险、生命周期状态和 schema 表示？
- 是否已经识别治理硬门暂未覆盖的结构风险？检查未报警不能替代架构判断。
- 是否通过用户意图和旧债信号判断需要进入“替换旧设计模式”？
- 如果任务涉及 Agent、DSPy、LLM 输出格式化或提示词输出要求，是否已经区分 backend-owned、agent-owned 和 boundary-owned 字段？
- 如果任务涉及 formatter、runner、worker、orchestrator 或 store completion，是否已经区分 `raw_agent_text`、`formatter_output`、`projected_output` 和边界 JSON payload，且没有把 formatter 输出擦成 `BaseModel`？
- 是否存在多个合理解释？如果存在，先列出并确认。
- 是否存在更简单的实现路径？如果存在，优先提出。

写文件前：

- 输出计划。
- 列出涉及文件。
- 说明逻辑修改点。
- 说明架构阈值命中情况；命中时给出先拆分、抽象或统一的处理方式。
- 如涉及 Agent、DSPy、LLM 输出契约，列出字段所有权矩阵、允许覆盖方、序列化边界和负向测试。
- 如涉及 typed output 主链路，列出 `FormatterOutputModel`、`ProjectedOutputModel`、具体 OutputModel、store completion 参数类型和允许保留 `JsonObject` 的兼容边界。
- 如触发替换旧设计模式，列出旧设计的删除、迁移、保留清单，以及公开契约、配置、数据、文档、测试和内部兼容层处理方式。
- 说明 Docker 卷挂载路径；不涉及写“不涉及”。
- 说明验证方式和成功标准。

完成后：

- 运行相关测试、构建、类型检查或人工验证。
- 运行项目覆盖层声明的治理硬门；`warn` / dry-run 类模式只允许用于 Analyze 阶段观察。
- 对照原始目标说明是否达成。
- 说明未验证项、原因和残余风险。

## 四项工程行为

本技能的默认优先级为：代码质量 > 新设计/框架/架构 > 旧模式兼容或保留。用户要求重构、去除旧设计、优化架构、提高代码质量、引入更优秀设计方案/框架/架构，或分析阶段发现旧 facade、兼容 shim、历史路径、重复实现、schema 双轨、状态分散、过期 API、不可达分支等旧债信号时，进入“替换旧设计模式”。

替换旧设计模式下，旧实现只有被明确列入保留清单，或属于已确认公开契约、数据迁移边界、安全边界时才保留；不得以“精准改动”“匹配现有风格”“外部行为不变”为理由默认延续旧设计。

### 1. 三思而后行

不要静默选择一种解释并直接实现。先管理不确定性：

- 明确说明假设。
- 明确说明歧义。
- 明确说明技术取舍。
- 必要时提出澄清问题。
- 如果用户要求的方案更复杂或风险更高，给出更简单方案及原因。

### 2. 简洁优先

实现只服务于当前目标：

- 不添加请求之外的功能。
- 不为单次使用代码创建抽象。
- 不添加未要求的灵活性或配置项。
- 不为理论上不可能的场景写复杂处理。
- 命中架构阈值、代码明显臃肿或触发替换旧设计模式时，先压缩、拆分、替换或统一方案。
- 简洁优先不得被解释为保留低质量旧设计；删除旧兼容层、收口单一来源或引入成熟框架能降低复杂度时，应优先纳入方案。

### 3. 精准改动

控制 diff，保护团队协作效率：

- 不修改无关文件。
- 不顺手重排格式、重写注释或调整命名。
- 普通小改不重构没有坏的代码；替换旧设计模式下，删除、迁移和重命名旧实现属于目标范围。
- 匹配健康的项目现有风格。
- 但“匹配现有风格”不得用于继续扩大上帝模块、复制大段代码、分散状态机、保留旧 facade 或延续兼容 shim。
- 普通小改只清理本次改动造成的无用代码；替换旧设计模式清理已确认清单内的旧实现和兼容层。

### 4. 目标驱动

把任务转成可验证目标：

- “修复 bug”要落实为“复现 bug -> 修复 -> 验证不再复现”。
- “添加校验”要落实为“无效输入失败、有效输入通过、错误信息符合预期”。
- 普通“重构”要落实为“重构前后测试通过，公开行为不变”。
- “替换旧设计”要落实为“保留契约和允许变化已确认，旧设计已删除或迁移，新契约通过验证”。
- 多步骤任务每一步都要有独立验证点。

## 架构阈值

以下情况视为“明显臃肿”或“债务继续增长”，必须在计划中先处理边界：

- 单个手写代码文件超过 800 行；Markdown 文档不适用该阈值。
- 单类超过 30 个公开方法。
- 单函数超过 80 行，或圈复杂度超过 15。
- 路由文件超过 20 个路由。
- 即将复制超过 40 行已有代码。
- 同一职责在 3 个及以上文件中通过字符串字面量耦合。
- 同一实体存在多套手写 schema、DTO、ORM 或序列化表示且缺少单一真相来源；Python 项目中包括 Pydantic 模型与手写 dict 双轨表示。
- 表达持久化生命周期的状态字段（如 `status` / `state` / `phase` / `stage`）的合法转移散落在多个分支中，或集中状态机只登记状态集合但缺完整转移表。
- 跨表、跨文件、跨服务或 DB + 文件系统更新拆成多个不可恢复的事务步骤，或事务块内执行不可回滚副作用。
- 旧 facade、兼容 shim、历史路径、旧数据格式、过期 API、不可达 UI 分支或调试脚本仍参与主流程，且没有明确迁移/删除计划。
- 修改路由、settings、Docker env、公开 schema 或默认值但不同步 README / docs。

命中阈值不等于允许大范围重写。普通小改默认只抽出完成当前目标所需的最小稳定边界；替换旧设计模式下，以已确认的新架构边界和旧设计清理清单为准删除、迁移或统一旧实现。
治理硬门以 git base 对比判定旧债；既有超限可暂存但本次不得增长，新增超限直接失败。
普通文档或 issue 可用于人工还债排期，但不得作为治理放行豁免。

## Agent 输出字段所有权

涉及 Agent、DSPy formatter、LLM 输出格式化或提示词输出要求时，先做字段所有权矩阵：

- backend-owned：job input、DB row、route 参数、store context、scheduler context 或确定性 projection 已知的字段，例如 job_id、batch_id、case ids、时间戳、scope、provenance、eval run、gate result、task execution id。不得要求 LLM 输出；如脏输出包含，后端必须覆盖或忽略。
- agent-owned：只有 Agent 结果才能提供的业务语义，例如分析结论、优化动作、业务假设、风险说明、证据摘要和建议。这类字段才进入 Agent 输出契约。
- boundary-owned：DB、HTTP、文件、日志和观测等序列化边界；`model_dump(mode="json")` 只应出现在这些边界。

prompt 和 Signature 只能要求 agent-owned 输出字段。backend-owned 字段可以作为输入上下文帮助 grounding，但不能成为 Agent 输出结构的一部分。

## Typed Output 类型边界

- Formatter 和 runner 主链路返回 `FormatterOutputModel` 或具体 formatter 输出模型，不得返回裸 `BaseModel`。
- Store completion 接收具体 formatter 输出模型；只有历史 fixture、外部脏输入或确定性投影路径允许额外接收 `JsonObject` 或具体 projected 输出模型。
- 后端补齐上下文字段后的模型属于 `ProjectedOutputModel`；DB、HTTP、文件、日志和观测边界才 dump 成 JSON payload。
- 变量命名必须体现阶段：Agent 原文用 `raw_agent_text` / `raw_text`，DSPy 结果用 `formatter_output`，投影结果用 `projected_output`，持久化字段才使用 `raw_output_json`。
- 新增 job 类型时，必须同步集中注册表、`FormatterOutputModel`、`ProjectedOutputModel`、DSPy Signature、OutputModel、projection 测试和 hostile backend-owned 字段污染测试。

## 生命周期状态硬约束

- 当字段表达持久化生命周期且存在受约束的合法转移时，必须进入集中状态机。
- `status` / `state` / `phase` / `stage` 只是常见信号；只读外部字段、展示标签、临时 UI 步骤或无受限转移的简单属性，不强制状态机。
- 集中状态机必须有状态集合和完整合法转移表；缺表视为未实现，不得称为已治理。
- 生命周期状态写入必须经统一 helper 调用转移校验；不得新增绕过 helper 的直写路径。
- 涉及生命周期状态字段时，必须补充非法转移或非法状态输入的负向测试。

## 测试纵深

- 生命周期状态字段变更：至少补充 1 个非法状态转移或非法状态输入测试。
- 并发资源变更：至少补充 1 个重复执行、竞争或部分失败场景测试。
- 外部输入变更：至少补充 1 个异常、恶意或越权输入测试。
- Agent/DSPy/LLM 输出契约变更：至少补充 1 个 hostile backend-owned 字段污染测试，证明错误 job_id、batch_id、case ids、时间戳、scope 或 provenance 不会覆盖后端权威值。
- happy path 只作为基线，不单独代表测试充分。

## 环境规范

Python：

- 使用仓库本地 `.venv`。
- 使用 Python 3.10+。
- 使用 `uv` 管理依赖。
- 禁止直接使用 `pip`。

Node.js：

- 使用 Node.js 20+。
- 必须使用 `pnpm`，不得新增 npm/yarn lockfile 或以 npm/yarn 作为项目默认入口。

Docker：

- Docker 文件放在 `docker/`。
- 环境变量放在 `docker/.env`。
- 卷挂载放在 `docker/volume/`。
- 端口映射遵循 `50000 + 容器端口`。
- 下载源必须优先使用国内源，例外必须说明。

## 安全规则

- 不输出或写入 API key、MCP header、数据库凭据和敏感工具输入。
- 不把个人路径、个人 token 或私有账号配置写入团队文件。
- 如项目声明离线或内网运行不变量，必需工作流不得依赖远程服务。
- 配置值不得硬编码到业务逻辑。

## 反模式

- 未确认需求就实现数据导出、权限、批量操作或破坏性操作。
- 为简单函数引入策略模式、插件系统或复杂配置。
- 修一个 bug 时顺手重写相邻模块。
- 改动格式导致 diff 难以评审。
- 没有复现问题就直接修 bug。
- 用“应该可以”代替测试、构建或明确的人工验证。
- 在已超 800 行的文件中继续追加新职责，理由是“匹配现有风格”。
- 通过复制超过 40 行已有函数实现新变体，理由是“精准改动”。
- 在替换旧设计模式下继续保留旧 facade、兼容 shim、历史路径、重复实现或不可达分支，理由是“外部行为不变”或“少改一点”。
- 把生命周期状态字段的合法转移写成散落的 if 分支。
- 只把状态值放进集中集合，却没有合法转移表和非法转移测试。
- 同一实体在多套 schema、DTO、ORM 或手写 dict 中重复定义。
- 让 Agent 或 LLM 复述后端已经知道的 job_id、batch_id、case ids、时间戳、scope、provenance 等系统字段，并把它们作为权威输出。
- 修改路由、settings 或 Docker env 但不同步 README / docs 对应章节。
- 修改数据库表结构或字段语义但不写项目约定的 migration。
- 给跨资源更新分多次不可恢复的事务提交。
