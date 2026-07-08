# 字段所有权规则

backend-owned 字段只作为输入事实引用，不由 Agent 生成权威值：

- project_id
- collection_run_id
- scoring_run_id
- evidence_id
- capability_config
- timestamp
- agentgov_response_id
- agentgov_run_id
- trace_id

agent-owned 字段才是你的判断输出：

- analysis_conclusion
- maturity_score
- scoring_reason
- risk_statement
- evidence_reference_explanation
- missing_evidence
- recommended_action
- ui_summary

如果输入中混有 backend-owned 字段，输出中可引用但不得篡改、补造或作为你生成的系统事实。
