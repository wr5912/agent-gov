from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def is_execution_enabled() -> bool:
    return os.getenv("RESPONSE_EXECUTION_ENABLED", "false").lower() in {"1", "true", "yes", "on"}


def compact(obj: Any, max_len: int = 2000) -> str:
    s = str(obj)
    return s if len(s) <= max_len else s[:max_len] + "...<truncated>"
