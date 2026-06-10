from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

CONTAINER_RUNTIME_VOLUME_ROOT = Path.home() / "volume-agent-gov"
LOCAL_DEBUG_RUNTIME_VOLUME_ROOT = Path("/tmp/local-debug-volume-agent-gov")
_CONTAINER_MARKER_ENV = "RUNTIME_CONTAINER"
_TRUTHY_CONTAINER_MARKERS = {"1", "true", "yes", "on", "container"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the FastAPI OpenAPI schema to a JSON file.")
    parser.add_argument(
        "--output",
        default="/tmp/agent-gov-openapi.json",
        help="Output path for the generated OpenAPI JSON.",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    _apply_local_defaults(project_root)

    from app.main import app

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(str(output_path))


def _local_default_volume_root() -> Path:
    if root := os.environ.get("HOST_RUNTIME_VOLUME_ROOT"):
        return Path(root)
    if os.environ.get(_CONTAINER_MARKER_ENV, "").strip().lower() in _TRUTHY_CONTAINER_MARKERS:
        return CONTAINER_RUNTIME_VOLUME_ROOT
    return LOCAL_DEBUG_RUNTIME_VOLUME_ROOT


def _apply_local_defaults(_project_root: Path) -> None:
    volume_root = _local_default_volume_root()
    os.environ.setdefault(_CONTAINER_MARKER_ENV, "0")
    os.environ.setdefault("HOST_RUNTIME_VOLUME_ROOT", str(volume_root))
    defaults = {
        "WORKSPACE_DIR": volume_root / "main-workspace",
        "MAIN_WORKSPACE_DIR": volume_root / "main-workspace",
        "ATTRIBUTION_ANALYZER_WORKSPACE_DIR": volume_root / "attribution-analyzer-workspace",
        "PROPOSAL_GENERATOR_WORKSPACE_DIR": volume_root / "proposal-generator-workspace",
        "EXECUTION_OPTIMIZER_WORKSPACE_DIR": volume_root / "execution-optimizer-workspace",
        "EVAL_CASE_GOVERNOR_WORKSPACE_DIR": volume_root / "eval-case-governor-workspace",
        "REGRESSION_IMPACT_ANALYZER_WORKSPACE_DIR": volume_root / "regression-impact-analyzer-workspace",
        "DATA_DIR": volume_root / "data",
        "CLAUDE_ROOT": volume_root / "claude-roots" / "main",
        "MAIN_CLAUDE_ROOT": volume_root / "claude-roots" / "main",
        "ATTRIBUTION_ANALYZER_CLAUDE_ROOT": volume_root / "claude-roots" / "attribution-analyzer",
        "PROPOSAL_GENERATOR_CLAUDE_ROOT": volume_root / "claude-roots" / "proposal-generator",
        "EXECUTION_OPTIMIZER_CLAUDE_ROOT": volume_root / "claude-roots" / "execution-optimizer",
        "EVAL_CASE_GOVERNOR_CLAUDE_ROOT": volume_root / "claude-roots" / "eval-case-governor",
        "REGRESSION_IMPACT_ANALYZER_CLAUDE_ROOT": volume_root / "claude-roots" / "regression-impact-analyzer",
        "CLAUDE_HOME": volume_root / "claude-roots" / "main" / ".claude",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, str(value))


if __name__ == "__main__":
    main()
