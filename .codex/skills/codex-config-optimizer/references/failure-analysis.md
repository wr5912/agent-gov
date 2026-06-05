# 失败归因

配置优化必须从失败证据开始，不能从“再加一条规则”开始。

## 证据链

记录以下事实：

- 用户原始说法和期望结果。
- Codex 实际做了什么，包括是否误触发、未触发、问了多余问题或跳过验证。
- 涉及的配置面：prompt、AGENTS、override、config、rules、skill、hook、script、MCP。
- 相关输出、trace、Langfuse observation、session 摘要或本地日志。
- 失败是否可复现，是否已有测试或治理脚本覆盖。

## Failure taxonomy

| 类型 | 判断标准 | 优先动作 |
| --- | --- | --- |
| trigger-miss | 有合适 skill 但未触发 | 改 description 或入口指针 |
| overtrigger | 不相关任务触发了 skill | 收窄 description，必要时禁止隐式触发 |
| context-bloat | 常驻配置重复或过长 | `delete`、`merge`、`move-to-skill` |
| surface-mismatch | 内容放错配置面 | 迁移到 skill/script/hook/override |
| rule-conflict | 多处规则互相冲突 | 选更具体配置面，删除弱规则 |
| validation-gap | 只能自报，没有机器门 | `move-to-script` 或测试化 |
| execution-gap | 规则写了但执行时没有前置 | 增加模板、脚本或 hook 检查 |
| env-drift | 本地环境、容器或依赖不同步 | 写入项目覆盖说明或诊断脚本 |

## 输出要求

先给 failure type，再给配置动作。不能只说“优化提示词”。

示例：

| 失败 | 类型 | 动作 |
| --- | --- | --- |
| Agent 输出缺少后端可确定字段 | surface-mismatch + execution-gap | backend-owned 不进 LLM 输出；配置中要求字段所有权矩阵 |
| `.codex/rules` 和 skill 重复写同一长清单 | context-bloat | 常驻层保留入口，细节迁入 skill reference |
