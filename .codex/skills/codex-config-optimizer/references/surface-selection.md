# 配置面选择

目标是选择最小、最稳定、最容易验证的配置面。

| 配置面 | 适合放 | 避免放 |
| --- | --- | --- |
| Prompt / 当前线程 | 一次性约束、临时偏好、当前任务验收口径 | 长期团队约定 |
| 根 `AGENTS.md` | 当前仓库必须始终生效的工作流、产品不变量、治理入口和 base-ref 策略 | 按需流程、长篇检查表、历史复盘 |
| `.codex/config.toml` | sandbox、approval、reasoning、MCP、模型等运行配置 | 工作流 SOP、长清单、个人模型偏好 |
| `.codex/guidance/*.md` | 根 `AGENTS.md` 显式引用的模型治理说明 | 假设目录内容会被 Codex 自动注入 |
| `.codex/rules/*.rules` | Codex `prefix_rule(...)` 命令执行策略 | Markdown、注释型模型指引、工作流 SOP |
| Skill | 可复用任务流程、按需加载的场景 SOP、引用和脚本 | 每次任务都必须知道的硬约束 |
| Hook | 生命周期硬门、可机械判定的阻断检查 | 需要人类判断的原则 |
| Script | 静态扫描、生成物校验、确定性报告 | 主观评价和复杂语义裁决 |
| Plugin | 多 skill、hooks、MCP、assets 的可安装分发包 | 尚未验证的本地实验 |
| MCP / connector | 外部系统实时数据或动作 | 静态团队流程说明 |

## 决策顺序

1. 是否只影响当前任务？是则留在 prompt。
2. 是否每次进入仓库都必须知道？是则考虑唯一根 `AGENTS.md`；不要在同目录并列创建会遮蔽它的 override。
3. 是否是按需执行的流程？是则放 skill。
4. 是否能确定性检查？是则写脚本；需要阻断时接 hook 或本地验证入口。
5. 是否需要跨仓库分发？验证稳定后再做 plugin 或全局 skill。

## 本仓库注意点

- `agent-gov` 的治理命令、自动化门禁、Docker 卷路径和反馈闭环产品不变量属于项目覆盖层。
- 团队通用层不得硬编码本仓库脚本名和路径。
- `.codex/guidance` 承载模型治理入口并由根 `AGENTS.md` 显式引用；`.codex/rules` 只承载可由 `codex execpolicy check` 验证的 Starlark 执行策略。
