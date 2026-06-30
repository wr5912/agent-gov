#!/usr/bin/env python3
"""Claude Code SessionStart hook: provide a short reminder."""

import json

print(
    json.dumps(
        {"additionalContext": "当前业务 Agent 是安全数据标准化审查智能体。先定位字段路径和证据，再审查 OCSF/STIX 映射；不直接修改生产规则或图谱。"},
        ensure_ascii=False,
    )
)
