#!/usr/bin/env python3
"""Claude Code SessionStart hook: provide a short reminder."""

import json

print(
    json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "当前项目是只读防御性安全运营智能体。仅处理已授权材料，区分事实、推断和建议；Agent 不执行任何 SOC 或系统变更。",
            }
        },
        ensure_ascii=False,
    )
)
