#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = str(ROOT / ".venv/bin/python")
GOVERNANCE_COMMANDS = (
    ("codex governance", [PYTHON, str(ROOT / "scripts/check_codex_governance.py"), "--mode", "fail"]),
    ("stage language", [PYTHON, str(ROOT / "scripts/check_stage_language.py")]),
)
MAX_REASON_CHARS = 20000


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    parts = []
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(stderr)
    return "\n".join(parts).strip()


def _truncate(text: str) -> str:
    if len(text) <= MAX_REASON_CHARS:
        return text
    omitted = len(text) - MAX_REASON_CHARS
    return f"{text[:MAX_REASON_CHARS]}\n\n... truncated {omitted} characters ..."


def _emit_block(reason: str) -> None:
    sys.stdout.write(json.dumps({"decision": "block", "reason": _truncate(reason)}))
    sys.stdout.write("\n")


def main() -> int:
    failures: list[str] = []
    for label, command in GOVERNANCE_COMMANDS:
        try:
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
        except Exception as exc:
            _emit_block(f"Codex governance Stop hook wrapper failed while running {label}:\n{exc}")
            return 0

        if result.returncode == 0:
            continue
        output = _combined_output(result) or f"{label} exited with status {result.returncode}."
        failures.append(f"[{label}]\n{output}")

    if not failures:
        return 0

    reason = f"Codex governance checks failed. Fix the reported issues, then rerun `make codex-guard`.\n\n{chr(10).join(failures)}"
    _emit_block(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
