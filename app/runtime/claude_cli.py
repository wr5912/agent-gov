from __future__ import annotations

import re
import shutil
import subprocess
from importlib.util import find_spec
from pathlib import Path


def bundled_claude_cli_path() -> Path | None:
    """Return the Claude Code binary bundled with claude-agent-sdk."""

    spec = find_spec("claude_agent_sdk")
    if spec is None or spec.origin is None:
        return None
    bundled = Path(spec.origin).resolve().parent / "_bundled" / "claude"
    return bundled if bundled.is_file() else None


def resolve_claude_cli_path(configured: Path | None) -> Path:
    """Resolve the exact real CLI that a managed raw-capture run must execute."""

    candidate = configured or bundled_claude_cli_path()
    if candidate is None:
        discovered = shutil.which("claude")
        if discovered:
            candidate = Path(discovered)
    if candidate is None:
        raise FileNotFoundError("Claude Code CLI was not found")

    resolved = candidate.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Claude Code CLI is not a file: {resolved}")
    return resolved


def command_version(command: str | Path | None) -> str | None:
    if command is None:
        return None
    try:
        output = subprocess.check_output(
            [str(command), "--version"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    return output.strip() or None


def claude_cli_semver(command: str | Path) -> str:
    output = command_version(command) or ""
    match = re.search(r"\b\d+\.\d+\.\d+\b", output)
    return match.group(0) if match else "unknown"
