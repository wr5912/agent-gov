---
name: security-operations-analysis
description: 网络安全运营研判。用于告警分流、事件调查、威胁狩猎、资产/账号上下文补强、风险排序和处置建议；真实响应处置交给 threat-response-disposition。
allowed-tools:
  - Read
  - Grep
  - Glob
  - mcp__sec-ops-data__*
  - mcp__soc-ops-query__*
  - mcp__soc-playbook-query__*
context: fork
---

## 安全约束

- 只做防御性安全运营研判，不提供攻击性执行步骤。
- 每个结论必须区分事实、推断和建议动作。
- 不调用 `soc-playbook-execution` 或 `soc-playbook-registry`；需要真实处置时输出进入 `threat-response-disposition` 的 response_case 摘要。
- 不读取或输出密钥、token、私钥、cookie、session、数据库密码和完整敏感原始日志。

## 步骤

1. **范围识别**：确认时间范围、告警/事件 ID、资产、账号、租户、业务影响和用户期望。
2. **证据查询**：用 `mcp__sec-ops-data__*` 查询基础 SOC 数据；必要时用 `mcp__soc-ops-query__*`、`mcp__soc-playbook-query__*` 了解可用响应能力。
3. **事实归纳**：只把工具返回或用户输入中明确存在的信息写入事实。
4. **推断研判**：按攻击阶段、影响面、资产重要性、异常强度、证据完整性给出置信度。
5. **风险排序**：标注 `low`、`medium`、`high` 或 `critical`，说明排序依据。
6. **行动建议**：优先补证据和低风险 containment；高危动作只输出进入响应处置闭环的条件。

## 输出

- 安全运营结论、风险等级和置信度。
- 证据与事实表。
- 推断与缺口表。
- 处置建议和后续验证步骤。
- 如需真实处置，输出 response_case 摘要和进入 `threat-response-disposition` 的原因。
