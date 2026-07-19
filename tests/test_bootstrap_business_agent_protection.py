"""运行卷初始化只创建内置 Agent，且绝不覆盖已有运行态 Workspace。"""

from __future__ import annotations

from pathlib import Path

from scripts.bootstrap_runtime_volume import bootstrap_runtime_volume

BUILTIN_AGENT_ID = "security-operations-expert"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _bootstrap_source(tmp_path: Path) -> Path:
    root = tmp_path / "runtime-bootstrap"
    _write(root / "governor-workspace" / "CLAUDE.md", "Governor\n")
    _write(
        root / "business-agents" / BUILTIN_AGENT_ID / "workspace" / "CLAUDE.md",
        "Built-in birth configuration\n",
    )
    return root


def test_bootstrap_preserves_existing_workspace_and_initializes_missing_builtin(tmp_path: Path) -> None:
    source = _bootstrap_source(tmp_path)
    runtime_root = tmp_path / "runtime"
    existing = _write(
        runtime_root / "data" / "business-agents" / BUILTIN_AGENT_ID / "workspace" / "CLAUDE.md",
        "Optimized runtime configuration\n",
    )

    result = bootstrap_runtime_volume(runtime_root=runtime_root, bootstrap_dir=source)

    assert existing.read_text(encoding="utf-8") == "Optimized runtime configuration\n"
    assert existing.parent.as_posix() in result["skipped_existing"]

    other_runtime = tmp_path / "other-runtime"
    bootstrap_runtime_volume(runtime_root=other_runtime, bootstrap_dir=source)
    initialized = other_runtime / "data" / "business-agents" / BUILTIN_AGENT_ID / "workspace" / "CLAUDE.md"
    assert initialized.read_text(encoding="utf-8") == "Built-in birth configuration\n"
