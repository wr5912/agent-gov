# Hooks 说明

- `pre_tool_guard.py`：在 Bash / MCP 工具执行前识别高风险动作，必要时要求确认或拒绝。
- `post_tool_audit.py`：把工具调用摘要写入 `/data/transcripts/claude-hook-audit.jsonl`。
- `session_start.py`：会话开始时注入安全运营提醒。

这些 hook 是安全辅助层，不替代企业级审批、审计、SOAR 权限控制和变更流程。
