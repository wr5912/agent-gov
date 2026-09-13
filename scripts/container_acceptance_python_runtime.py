"""正式容器验收 Python base runtime 的捕获与实际加载边界。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import NoReturn, TypeVar

from scripts.container_acceptance_identity import (
    BoundRuntimeIdentity,
    capture_file_identity,
    capture_tree_identity,
)

_E = TypeVar("_E", bound=Exception)
_PYTHON_VERSION = "3.11"


def _fail(error_type: type[_E], message: str, cause: BaseException | None = None) -> NoReturn:
    error = error_type(message)
    if cause is None:
        raise error
    raise error from cause


def python_base_stdlib(repo_root: Path, *, error_type: type[_E]) -> Path:
    try:
        real_python = (repo_root / ".venv/bin/python").resolve(strict=True)
        base_root = real_python.parent.parent
        stdlib = (base_root / f"lib/python{_PYTHON_VERSION}").resolve(strict=True)
        stdlib.relative_to(base_root)
    except (OSError, ValueError) as exc:
        _fail(error_type, "无法从项目 Python 解析固定 base stdlib", exc)
    if real_python.parent != base_root / "bin" or not (stdlib / "os.py").is_file():
        _fail(error_type, "项目 Python base runtime 布局不受支持")
    return stdlib


def capture_python_artifacts(
    repo_root: Path,
    *,
    error_type: type[_E],
) -> list[BoundRuntimeIdentity]:
    artifacts = [
        capture_file_identity(
            f"python-lock-{name}",
            repo_root / relative,
            kind="file",
            error_type=error_type,
        )
        for name, relative in (
            ("requirements", "requirements.txt"),
            ("requirements-api", "requirements-api.txt"),
            ("requirements-runtime", "agentscope_runtime/requirements.txt"),
            ("uv", "uv.lock"),
            ("pyproject", "pyproject.toml"),
        )
    ]
    artifacts.extend(
        (
            capture_file_identity(
                "python-venv-config",
                repo_root / ".venv/pyvenv.cfg",
                kind="file",
                error_type=error_type,
            ),
            capture_tree_identity(
                "python-base-stdlib",
                python_base_stdlib(repo_root, error_type=error_type),
                version=_PYTHON_VERSION,
                entrypoint="os.py",
                error_type=error_type,
            ),
            capture_tree_identity(
                "python-site-packages",
                repo_root / f".venv/lib/python{_PYTHON_VERSION}/site-packages",
                version=_PYTHON_VERSION,
                entrypoint="_virtualenv.py",
                error_type=error_type,
            ),
            capture_file_identity(
                "formal-quality-policy",
                repo_root / "tests/quality_policy.json",
                kind="file",
                error_type=error_type,
            ),
        )
    )
    return artifacts


def materialized_python_paths(execution_root: Path, formal_root: Path) -> tuple[Path, tuple[Path, ...]]:
    python_home = execution_root / "python-base"
    python_path = (
        formal_root,
        formal_root / "packages/agentgov-testkit/src",
        execution_root / f"lib/python{_PYTHON_VERSION}/site-packages",
    )
    return python_home, python_path


def verify_materialized_python_runtime(
    environ: dict[str, str],
    *,
    execution_root: Path,
    formal_root: Path,
    python_path: Path,
    error_type: type[_E],
) -> None:
    expected_home, expected_python_path = materialized_python_paths(execution_root, formal_root)
    if environ.get("PYTHONHOME") != str(expected_home):
        _fail(error_type, "PYTHONHOME 未绑定到私有 base runtime")
    if environ.get("PYTHONPATH") != os.pathsep.join(str(path) for path in expected_python_path):
        _fail(error_type, "PYTHONPATH 未绑定到正式源码与私有 site-packages")
    probe = (
        "import json,site,sys;"
        "print(json.dumps({'base_prefix':sys.base_prefix,'prefix':sys.prefix,'executable':sys.executable,"
        "'path':sys.path,'no_user_site':sys.flags.no_user_site,'safe_path':sys.flags.safe_path,"
        "'dont_write_bytecode':sys.flags.dont_write_bytecode,'enable_user_site':site.ENABLE_USER_SITE,"
        "'sitecustomize':'sitecustomize' in sys.modules,'usercustomize':'usercustomize' in sys.modules}))"
    )
    try:
        result = subprocess.run(
            [str(python_path), "-c", probe],
            env=environ,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        _fail(error_type, "私有 Python runtime 探针不可用", exc)
    if result.returncode or not isinstance(payload, dict):
        _fail(error_type, "私有 Python runtime 探针失败")
    if (
        payload.get("base_prefix") != str(expected_home)
        or payload.get("prefix") != str(expected_home)
        or Path(str(payload.get("executable"))).resolve() != python_path.resolve()
        or payload.get("no_user_site") != 1
        or payload.get("safe_path") is not True
        or payload.get("dont_write_bytecode") != 1
        or payload.get("enable_user_site") is not False
        or payload.get("sitecustomize") is not False
        or payload.get("usercustomize") is not False
    ):
        _fail(error_type, "私有 Python runtime 身份或安全 flags 不一致")
    runtime_paths = payload.get("path")
    if not isinstance(runtime_paths, list) or not runtime_paths:
        _fail(error_type, "私有 Python sys.path 无效")
    allowed_roots = (execution_root.resolve(), formal_root.resolve())
    for raw_path in runtime_paths:
        if not isinstance(raw_path, str) or not raw_path:
            _fail(error_type, "私有 Python sys.path 含动态工作目录")
        resolved = Path(raw_path).resolve(strict=False)
        if not any(resolved == root or resolved.is_relative_to(root) for root in allowed_roots):
            _fail(error_type, "私有 Python sys.path 逃离物化执行根")
