#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping

PRIVATE_MAKE_TARGETS = (
    "_container-core-smoke",
    "_container-openapi-check",
    "_container-live-test",
    "_container-speech-summary-test",
    "_container-health-e2e",
    "_smoke",
    "_ui-smoke",
    "_ui-feedback-smoke",
    "_ui-openai-responses-smoke",
    "_langfuse-smoke",
)
PRIVATE_FRONTEND_SCRIPTS = (
    "verify:real-container:impl",
    "verify:openai-responses-container:impl",
    "verify:provider-health-container:impl",
)
DIRECT_ACCEPTANCE_SCRIPTS = (
    "scripts/run_healthcheck_container_e2e.sh",
    "scripts/verify_improvement_ui_real_container.mjs",
    "scripts/verify_openai_responses_container.mjs",
    "scripts/verify_provider_health_container.mjs",
    "scripts/verify_speech_summary_container.py",
)
SHELL_BOUNDARY = r"(?:^|(?:&&|\|\||;|\|)\s*)"
PREFIX = r"(?:[A-Za-z_][A-Za-z0-9_]*=[^\s;&|]+\s+)*(?:command\s+)?"


def _read_payload() -> Mapping[str, object]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _command_from_payload(payload: Mapping[str, object]) -> str:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "cmd"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return ""


def _matches_private_make(command: str) -> bool:
    targets = "|".join(re.escape(target) for target in PRIVATE_MAKE_TARGETS)
    pattern = rf"{SHELL_BOUNDARY}{PREFIX}(?:\S*/)?(?:g?make)\b[^;&|]*(?:^|\s)(?:{targets})(?=\s|$)"
    return re.search(pattern, command) is not None


def _matches_private_frontend(command: str) -> bool:
    scripts = "|".join(re.escape(script) for script in PRIVATE_FRONTEND_SCRIPTS)
    pattern = rf"{SHELL_BOUNDARY}{PREFIX}(?:pnpm|corepack\s+pnpm)\b[^;&|]*\brun\s+(?:{scripts})(?=\s|$)"
    return re.search(pattern, command) is not None


def _matches_direct_script(command: str) -> bool:
    scripts = "|".join(re.escape(script) for script in DIRECT_ACCEPTANCE_SCRIPTS)
    interpreter = r"(?:python(?:3(?:\.\d+)?)?|node|bash|sh)"
    interpreted = rf"{SHELL_BOUNDARY}{PREFIX}(?:\S*/)?{interpreter}\b[^;&|]*\b(?:{scripts})(?=\s|$)"
    executable = rf"{SHELL_BOUNDARY}{PREFIX}(?:\./|(?:\S*/))?(?:{scripts})(?=\s|$)"
    return re.search(interpreted, command) is not None or re.search(executable, command) is not None


def _matches_live_pytest(command: str) -> bool:
    runner = r"(?:pytest|python(?:3(?:\.\d+)?)?\s+-m\s+pytest)"
    pattern = rf"{SHELL_BOUNDARY}{PREFIX}(?:\S*/)?{runner}\b[^;&|]*\btests/test_live_runtime_acceptance\.py(?=\s|$|:)"
    return re.search(pattern, command) is not None


def bypass_reason(command: str) -> str | None:
    if _matches_private_make(command):
        return "私有容器验收 Make 目标不能直接调用"
    if _matches_private_frontend(command):
        return "真实容器前端 :impl 脚本不能直接调用"
    if _matches_direct_script(command):
        return "真实容器验收脚本不能绕过公共 Make 入口"
    if _matches_live_pytest(command):
        return "live pytest 必须通过 make container-live-test"
    return None


def main() -> int:
    payload = _read_payload()
    tool_name = payload.get("tool_name")
    if tool_name not in {None, "Bash"}:
        return 0
    reason = bypass_reason(_command_from_payload(payload))
    if reason is None:
        return 0
    print(f"{reason}；公共入口会先重建镜像、force-recreate 服务并验证最新配置。", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
