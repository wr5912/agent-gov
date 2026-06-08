---
name: "codex-config-optimizer"
description: "治理和优化 Codex 配置、AGENTS.md、.codex/rules、hooks、project-skill 或 Agent Skills。用户提到 Codex 配置优化、skill 优化、配置越改越重、触发不准、上下文噪声、规则重复、治理配置审计、把失败经验沉淀到 skill/hook/script 时使用。"
---

# Codex 配置治理

本技能用于把 Codex 配置优化从“继续追加说明”改成证据驱动的治理决策。默认先审计、删重和分层，只有证据证明需要时才新增配置。

## 工作流

1. 读取当前仓库的 `AGENTS.md`、项目覆盖说明、`.codex/README.md`、`.codex/config.toml`、`.codex/hooks.json`、`.codex/rules/*.rules` 和相关 `SKILL.md`。
2. 如果可运行，先执行只读审计脚本：

   ```bash
   .venv/bin/python .codex/skills/codex-config-optimizer/scripts/audit_codex_config.py
   ```

3. 先还原失败证据，再给优化建议。证据至少包括用户症状、触发语句、相关配置面、实际行为、期望行为和验证缺口。
4. 每条建议必须给出一个动作：`keep`、`delete`、`merge`、`move-to-skill`、`move-to-script`、`move-to-hook`、`no-op`。
5. 优先选择最小有效配置面：能用一次性 prompt 解决的不进持久配置；能按需触发的进 skill；能静态判定的进脚本或 hook；项目私有内容不放进团队通用层。

## 何时读取 References

- 配置应放在哪个面：读 [surface-selection.md](references/surface-selection.md)。
- 评估一个 skill 是否高质量：读 [skill-quality-rubric.md](references/skill-quality-rubric.md)。
- 从失败轨迹归因：读 [failure-analysis.md](references/failure-analysis.md)。
- 从历史经验沉淀、个性化、泛化：读 [config-lifecycle.md](references/config-lifecycle.md)。
- 判断优化是否真的变好：读 [eval-rubric.md](references/eval-rubric.md)。
- 反馈闭环、runtime/env 或 Agent job 问题反复出现，需要执行前短预检：读 [feedback-runtime-preflight.md](references/feedback-runtime-preflight.md)。

## 输出格式

输出配置治理方案时使用表格：

| 证据 | 当前配置面 | 问题 | 动作 | 目标配置面 | 验证 | 风险 |
| --- | --- | --- | --- | --- | --- | --- |

如果建议新增内容，必须同时说明为什么不能通过 `delete`、`merge`、`move-to-skill`、`move-to-script` 或 `no-op` 解决。

## 禁止事项

- 不把通用编程常识写进项目常驻配置。
- 不把项目私有路径、CI 命令或产品不变量写进团队通用层。
- 不用“之前出过错”作为永久新增规则的唯一理由；先确认是否能测试化或脚本化。
- 不把 judge、EvoSkill 或外部优化框架直接接入首版治理；先有稳定 failure taxonomy、样本和验证器。
