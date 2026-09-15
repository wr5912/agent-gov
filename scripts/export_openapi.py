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
    return {**{key: str(value) for key, value in values.items()}, **_frozen_python_environment(project_root)}


def _frozen_python_environment(project_root: Path) -> EnvValues:
    if "AGENT_GOV_ACCEPTANCE_TOOLCHAIN_JSON" not in os.environ:
        return {}

    # 冻结解释器的 stdlib 与 site-packages 不在默认布局中；只取既有工具链
    # 核验后派生的 Python 加载参数，不把部署配置传入 schema 导出子进程。
    from scripts.container_acceptance_toolchain import (
        FORMAL_SOURCE_ROOT_ENV,
        TOOL_PATH_ENV_KEYS,
        toolchain_environment,
        verify_acceptance_toolchain,
    )

    bound = verify_acceptance_toolchain(dict(os.environ))
    runtime = toolchain_environment(bound)
    if (
        bound["stage"] != "materialized"
        or Path(runtime[TOOL_PATH_ENV_KEYS["python"]]).resolve() != Path(sys.executable).resolve()
        or Path(runtime[FORMAL_SOURCE_ROOT_ENV]).resolve() != project_root
    ):
        raise ValueError("OpenAPI 导出必须继续使用已物化的 Python 与正式源码")
    return {key: runtime[key] for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONNOUSERSITE", "PYTHONSAFEPATH", "PYTHONDONTWRITEBYTECODE")}


if __name__ == "__main__":
    main()
