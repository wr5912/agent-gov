---
name: ocsf-mapping
description: 当用户需要把原始安全日志、进程行为、网络连接、文件行为映射到 OCSF 或 STIX 时使用。
allowed-tools:
  - Read
  - Grep
  - Glob
---

# OCSF / STIX Mapping Skill

## 工作流程

1. 识别原始日志类型：进程、网络、文件、注册表、认证、告警等。
2. 判断最贴近的 OCSF event class。
3. 保留原始字段，不要强行丢弃无法映射的信息。
4. 对无法直接映射的字段，放入 `metadata`、`unmapped` 或扩展字段。
5. 如需映射到 STIX，优先构造 `observed-data + SCO` 图。

## 输出要求

- 先解释映射思路。
- 再输出 JSON。
- 最后说明哪些字段无法标准映射。

## 注意事项

- OCSF 和 STIX 都有表达边界，不要假装所有字段都能无损映射。
- 进程创建关系、父子关系、命令行、哈希、签名、用户、主机上下文要尽量保留。
