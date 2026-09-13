"""复用隔离验收的可信工具物化，为 selected-env 固定 Node 与浏览器。"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict, cast

from scripts.container_acceptance_identity import BoundRuntimeIdentity, capture_file_identity, capture_tree_identity
from scripts.container_acceptance_materialization import (
    _BROWSER_EXECUTABLE_TREES,
    _BROWSER_TREES,
    ExecutionMutationGuard,
    _copy_file_exact,
    _materialize_browser_artifacts,
    seal_materialized_input_tree,
)
from scripts.container_acceptance_toolchain import _capture_browser_artifacts, _resolve_node
from scripts.selected_env_operation_contract import OperationEnvironment, SelectedEnvError

BROWSER_TOOLCHAIN_ENV = "AGENTGOV_DEPLOYED_BROWSER_TOOLCHAIN"


class BrowserToolchain(TypedDict):
    root: str
    source_root: str
    node: BoundRuntimeIdentity
    artifacts: dict[str, BoundRuntimeIdentity]


def prepare_browser_toolchain(root: Path, source_root: Path, environ: dict[str, str]) -> OperationEnvironment:
    """仅在 live 入口解析安装位置；冻结进程不再解析 PATH/node_modules。"""
    browser_root = root / "browser-toolchain"
    browser_root.mkdir(mode=0o700)
    node_path, version = _resolve_node(environ, error_type=SelectedEnvError)
    node = capture_file_identity("node", node_path, kind="executable", version=version, executable=True, error_type=SelectedEnvError)
    copied_node = _copy_file_exact(node, browser_root / "bin/node", executable=True, error_type=SelectedEnvError)
    source = {item["name"]: item for item in _capture_browser_artifacts(environ, error_type=SelectedEnvError)}
    artifacts: dict[str, BoundRuntimeIdentity] = {}
    _materialize_browser_artifacts(source, browser_root, source_root, artifacts, error_type=SelectedEnvError)
    seal_materialized_input_tree(browser_root, error_type=SelectedEnvError)
    toolchain = BrowserToolchain(
        root=str(browser_root),
        source_root=str(source_root),
        node=_capture_identity(copied_node),
        artifacts={name: _capture_identity(item) for name, item in artifacts.items()},
    )
    environment = {
        BROWSER_TOOLCHAIN_ENV: json.dumps(toolchain, sort_keys=True),
        "PLAYWRIGHT_BROWSERS_PATH": str(browser_root / "browsers"),
        "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
    }
    verify_browser_toolchain(environment)
    return environment


def _capture_identity(expected: BoundRuntimeIdentity) -> BoundRuntimeIdentity:
    if expected["kind"] == "tree":
        return capture_tree_identity(
            expected["name"],
            Path(expected["path"]),
            entrypoint=expected["entrypoint"],
            version=expected["version"],
            error_type=SelectedEnvError,
        )
    return capture_file_identity(
        expected["name"],
        Path(expected["path"]),
        kind=expected["kind"],
        entrypoint=expected["entrypoint"],
        executable=expected["kind"] == "executable",
        version=expected["version"],
        error_type=SelectedEnvError,
    )


def _verify_identity(expected: BoundRuntimeIdentity) -> None:
    if _capture_identity(expected) != expected:
        raise SelectedEnvError("冻结 Node/Playwright/browser 依赖身份已漂移")


def verify_browser_toolchain(environ: Mapping[str, str]) -> BrowserToolchain:
    try:
        toolchain = cast(BrowserToolchain, json.loads(environ[BROWSER_TOOLCHAIN_ENV]))
        root = Path(toolchain["root"])
        if not root.is_absolute() or root.is_symlink() or root.stat().st_mode & 0o222:
            raise SelectedEnvError("冻结浏览器工具根无效")
        if environ.get("PLAYWRIGHT_BROWSERS_PATH") != str(root / "browsers"):
            raise SelectedEnvError("浏览器缓存位置脱离冻结边界")
        required = _BROWSER_TREES | _BROWSER_EXECUTABLE_TREES.keys() | {"frontend-package", "playwright-lock"}
        if set(toolchain["artifacts"]) != required:
            raise SelectedEnvError("冻结浏览器依赖集合不精确")
        _verify_identity(toolchain["node"])
        source_root = Path(toolchain["source_root"])
        lock_paths = {
            "frontend-package": source_root / "frontend/package.json",
            "playwright-lock": source_root / "frontend/pnpm-lock.yaml",
        }
        for name, identity in toolchain["artifacts"].items():
            path = Path(identity["path"])
            if identity["name"] != name or path.is_symlink() or path != Path(identity["real_path"]):
                raise SelectedEnvError("浏览器依赖含间接入口或身份错配")
            if name in lock_paths:
                if path != lock_paths[name]:
                    raise SelectedEnvError("浏览器 lock 脱离冻结源码")
            elif not path.is_relative_to(root):
                raise SelectedEnvError("浏览器依赖路径逃离冻结工具根")
            _verify_identity(identity)
        if Path(toolchain["node"]["path"]) != root / "bin/node":
            raise SelectedEnvError("Node 入口脱离冻结边界")
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise SelectedEnvError("冻结浏览器工具链无效") from exc
    return toolchain


@contextmanager
def browser_mutation_monitor(environ: Mapping[str, str]) -> Iterator[BrowserToolchain]:
    toolchain = verify_browser_toolchain(environ)
    guard = ExecutionMutationGuard((Path(toolchain["root"]),), error_type=SelectedEnvError)
    try:
        yield toolchain
    finally:
        try:
            guard.check()
            verify_browser_toolchain(environ)
        finally:
            guard.close()
