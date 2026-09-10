from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import TypeAlias, cast

OpenApiSchema = Mapping[str, object]
EnvValues: TypeAlias = dict[str, str]


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the AgentGov FastAPI OpenAPI schema.")
    parser.add_argument("--output", default="/tmp/agent-gov-openapi.json")
    args = parser.parse_args()
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(build_openapi_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(str(output_path))


@lru_cache(maxsize=1)
def build_openapi_schema() -> OpenApiSchema:
    """在独立进程与临时运行目录中导出，不读取调用方运行卷或私有配置。"""

    project_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="agent-gov-openapi-", dir="/tmp") as temporary_root:
        volume_root = Path(temporary_root)
        schema_path = volume_root / "openapi.json"
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import json; from pathlib import Path; from app.main import app; Path('openapi.json').write_text(json.dumps(app.openapi()), encoding='utf-8')",
            ],
            cwd=volume_root,
            env=_export_environment(project_root, volume_root),
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return cast(OpenApiSchema, json.loads(schema_path.read_text(encoding="utf-8")))


def _export_environment(project_root: Path, volume_root: Path) -> EnvValues:
    # 子进程 cwd 中不存在 docker/.env*；不继承 env，避免 AppSettings 读取部署秘密、
    # 非法参数、外部服务地址或独立配置的 Git/worktree/gate 路径。
    values = {
        "PATH": os.defpath,
        "PYTHONPATH": project_root,
        "PYTHONDONTWRITEBYTECODE": "1",
        "RUNTIME_CONTAINER": "0",
        "RUNTIME_VOLUME_MODE": "local-debug",
        "HOST_RUNTIME_VOLUME_ROOT": volume_root,
        "HOST_DATA_MOUNT": volume_root / "data",
        "HOST_GOVERNOR_WORKSPACE_MOUNT": volume_root / "governor-workspace",
        "DATA_DIR": volume_root / "data",
        "GOVERNOR_WORKSPACE_DIR": volume_root / "governor-workspace",
        "RUNTIME_CANDIDATES_DIR": volume_root / "agentscope-runtime" / "candidates",
        "AGENTSCOPE_RUNTIME_URL": "http://127.0.0.1:18090",
        "AGENTGOV_RUNTIME_SHARED_SECRET": "openapi-export-only-secret",
    }
    return {key: str(value) for key, value in values.items()}


if __name__ == "__main__":
    main()
