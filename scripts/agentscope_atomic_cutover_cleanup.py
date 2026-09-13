"""Fail-closed cleanup for isolated container-acceptance runtime roots."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

try:
    from scripts.container_acceptance_inputs import AcceptanceError
except ModuleNotFoundError:
    from container_acceptance_inputs import AcceptanceError


REPO_ROOT = Path(__file__).resolve().parents[1]
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")


def _run_checked(command: list[str], env: dict[str, str], label: str, *, capture: bool = False) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            check=False,
            capture_output=capture,
            text=capture,
        )
    except OSError as exc:
        raise AcceptanceError(f"{label}无法启动") from exc
    if result.returncode:
        raise AcceptanceError(f"{label}失败")
    return result.stdout.strip() if capture else ""


def _validate_cleanup_root(runtime_root: Path) -> None:
    parent = runtime_root.parent
    if (
        runtime_root.name != "runtime-root"
        or parent.parent != Path(tempfile.gettempdir()).resolve()
        or not parent.name.startswith(f"agentgov-acceptance-{os.getuid()}-")
        or parent.is_symlink()
        or runtime_root.is_symlink()
        or runtime_root.resolve() != runtime_root
        or parent.stat().st_uid != os.getuid()
        or stat.S_IMODE(parent.stat().st_mode) != 0o700
    ):
        raise AcceptanceError("拒绝清理未经确认的临时验收根目录")


def _repair_ownership(
    base: list[str],
    runtime_root: Path,
    env: dict[str, str],
    repair_image_id: str | None,
) -> None:
    image = repair_image_id
    if image is None:
        raw = _run_checked([*base, "config", "--format", "json"], env, "隔离清理镜像解析", capture=True)
        try:
            image = json.loads(raw)["services"]["agent-gov-api"]["image"]
        except (ValueError, KeyError, TypeError) as exc:
            raise AcceptanceError("隔离清理缺少已构建 API 镜像") from exc
        if not isinstance(image, str) or not image:
            raise AcceptanceError("隔离清理缺少已构建 API 镜像")
    elif _IMAGE_ID.fullmatch(image) is None:
        raise AcceptanceError("隔离清理 API 镜像 identity 无效")
    repair = (
        "import os,sys; uid,gid=map(int,sys.argv[1:]); "
        "root='/acceptance-runtime'; "
        "[(os.chown(path,uid,gid,follow_symlinks=False),os.chmod(path,0o700)) "
        "for path,dirs,files in os.walk(root,followlinks=False)]"
    )
    _run_checked(
        [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "DAC_OVERRIDE",
            "--cap-add",
            "FOWNER",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            f"type=bind,src={runtime_root},dst=/acceptance-runtime",
            "--entrypoint",
            "python",
            image,
            "-c",
            repair,
            str(os.getuid()),
            str(os.getgid()),
        ],
        env,
        "隔离临时卷目录权限回收",
        capture=True,
    )


def cleanup_runtime_root(
    base: list[str],
    runtime_root: Path,
    env: dict[str, str],
    *,
    before_delete: Callable[[], None] | None = None,
    repair_image_id: str | None = None,
) -> None:
    """Delete only a validated temporary root, probing immediately before each attempt."""

    _validate_cleanup_root(runtime_root)
    if not runtime_root.exists():
        return
    if before_delete is not None:
        before_delete()
    try:
        shutil.rmtree(runtime_root)
        return
    except PermissionError:
        pass
    _repair_ownership(base, runtime_root, env, repair_image_id)
    if before_delete is not None:
        before_delete()
    try:
        shutil.rmtree(runtime_root)
    except OSError as exc:
        raise AcceptanceError("隔离临时卷回收失败，已保留临时目录供检查") from exc
