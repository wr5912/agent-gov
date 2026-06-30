---
name: security-data-standardization-review
description: 审查原始安全数据到 OCSF、OCSF 到 STIX 的映射质量，输出缺陷归因、修正建议和回归用例。
allowed-tools:
  - Read
  - Grep
  - Glob
  - Skill
  - mcp__sec-ops-data__*
context: fork
---

## 适用输入

- 原始安全日志、告警、事件、检测结果。
- 已映射的 OCSF JSON 或字段表。
- OCSF 到 STIX 2.1 的对象、SCO、relationship、observed-data 或图谱导入结果。
- 用户反馈、误报案例、字段缺失报告、图谱关系缺失报告。

## 审查步骤

1. 识别输入范围：来源系统、事件类型、样例编号、时间窗口、现有 OCSF 和 STIX 输出。
2. 委派 `ocsf-mapping-reviewer` 审查原始数据到 OCSF 的字段、类型、枚举、类别和语义。
3. 委派 `stix-threat-model-reviewer` 审查 OCSF 到 STIX 的对象、SCO、relationship 和 evidence strength。
4. 汇总缺陷并归因到：`field_missing`、`semantic_mismatch`、`enum_or_type_violation`、`time_window_mismatch`、`identity_drift`、`relationship_missing`、`over_modeling`、`evidence_gap`、`privacy_redaction_risk`。
5. 委派 `regression-case-curator` 生成最小回归用例和负向断言。
6. 输出审查报告，不直接修改生产规则或图谱。

## 审查重点

### 原始数据到 OCSF

- class/category/activity/type 是否与安全事件语义一致。
- 必填字段、时间戳、时区、严重级别、状态、设备、用户、进程、文件、网络五元组是否完整。
- src/dst、actor/target、parent/child process、file hash、url/domain/ip 归属是否颠倒。
- 枚举值、数值单位、布尔含义、数组对象结构是否符合 OCSF 语义。

### OCSF 到 STIX

- `observed-data` 是否准确引用 SCO，时间窗口是否来自观测事实。
- `ipv4-addr`、`domain-name`、`url`、`file`、`process`、`user-account`、`network-traffic` 等 SCO 是否去重且有稳定身份。
- `indicator`、`attack-pattern`、`malware`、`tool`、`identity`、`infrastructure` 是否有足够证据，不把弱线索建成强威胁事实。
- relationship 是否表达真实关系，避免缺边、错边、反向边和过度关联。

## 输出格式

```markdown
## 审查结论
- 结论：
- 置信度：
- 影响范围：

## 证据摘要
| 编号 | 来源 | 字段路径 | 事实摘要 |
| --- | --- | --- | --- |

## 缺陷清单
| 严重级别 | 缺陷类型 | 位置 | 问题 | 证据 | 修正建议 |
| --- | --- | --- | --- | --- | --- |

## 回归用例
| 用例名 | 最小输入 | 预期 OCSF | 预期 STIX | 负向断言 |
| --- | --- | --- | --- | --- |
```

## 安全约束

- 不输出完整原始日志；只输出字段路径、脱敏摘要和最小复现输入。
- 不读取 `.env`、`secrets/`、Claude 运行态家目录或任何凭据文件。
- 不执行生产修改，不调用写入、更新、删除、执行、提交类工具。
- 证据不足时输出 `evidence_gap`，不要补造字段、对象或关系。
