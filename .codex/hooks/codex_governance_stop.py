#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GOVERNANCE_COMMAND = [
    str(ROOT / ".venv/bin/python"),
    str(ROOT / "scripts/check_codex_governance.py"),
    "--mode",
    "fail",
]
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
    try:
        result = subprocess.run(
            GOVERNANCE_COMMAND,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        _emit_block(f"Codex governance Stop hook wrapper failed:\n{exc}")
        return 0

    if result.returncode == 0:
        return 0

    output = _combined_output(result)
    if not output:
        output = f"Governance command exited with status {result.returncode}."
    reason = (
        "Codex governance check failed. Fix the reported issues, then rerun "
        "`.venv/bin/python scripts/check_codex_governance.py --mode fail`.\n\n"
        f"{output}"
    )
    _emit_block(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
