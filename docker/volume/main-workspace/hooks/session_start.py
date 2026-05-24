#!/usr/bin/env python3
"""Claude Code SessionStart hook: provide a short reminder."""
import json

print(json.dumps({
    "additionalContext": "当前项目是 AI 智能化网络安全运营智能体。默认证据优先；生产处置和策略变更必须先 dry-run，并需要审批、回滚和验证。"
}, ensure_ascii=False))
