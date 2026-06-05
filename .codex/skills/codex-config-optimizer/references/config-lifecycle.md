# 配置生命周期

借鉴 skill-miner、skill-personalizer、skill-generalizer 的分工，把配置优化拆成三段。

## Mine：从历史中挖

候选配置必须满足：

- 至少出现两次，或一次失败造成高风险。
- 不是通用常识，而是项目特有流程、环境、工具链或踩坑。
- 有证据说明现有配置没有覆盖，或覆盖了但没有被执行。

输出候选项时记录：证据、频次、影响、建议动作和验证方式。

## Personalize：适配本仓库

适配时检查：

- 用户自然语言触发词是否写进 description 或入口说明。
- 命令是否使用本仓库约定，例如 `.venv/bin/python`、`pnpm`、项目治理硬门。
- 私有路径、账号、token 和本机偏好是否被误写进团队文件。
- 规则是否和 `AGENTS.override.md`、`.codex/rules`、hooks 冲突。

## Generalize：泛化前清理

准备做全局 skill 或 plugin 时，必须移除：

- 本仓库路径、脚本名、CI workflow、Docker 卷路径和产品不变量。
- 只适用于一个用户的别名、目录和模型偏好。
- 未验证的 claim、benchmark 和安装说明。

## 动作定义

- `keep`：保留在原面，理由是它是硬约束或必须常驻。
- `delete`：删除重复、过期、错误或低价值配置。
- `merge`：合并多个等价条目，只保留权威入口。
- `move-to-skill`：迁入按需触发的 workflow。
- `move-to-script`：变成可确定性检查或报告。
- `move-to-hook`：反复失败且可机械阻断时接入生命周期。
- `no-op`：不改配置，只记录事实或通过当前任务 prompt 解决。
