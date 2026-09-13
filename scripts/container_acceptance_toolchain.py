"""隔离容器验收宿主工具与浏览器依赖的稳定身份。"""

from __future__ import annotations

import json
import os
import platform
import pwd
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Final, TypedDict, TypeVar

from scripts.container_acceptance_identity import (
    BoundRuntimeIdentity,
)
from scripts.container_acceptance_identity import (
    capture_file_identity as _capture_file,
)
from scripts.container_acceptance_identity import (
    capture_tree_identity as _capture_tree,
)
from scripts.container_acceptance_identity import (
    fail as _raise,
)
from scripts.container_acceptance_identity import (
    resolved_path as _resolved_path,
)
from scripts.container_acceptance_identity import (
    stable_file_payload as _stable_file_payload,
)
from scripts.container_acceptance_python_runtime import (
    capture_python_artifacts,
    materialized_python_paths,
    verify_materialized_python_runtime,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
TRUSTED_SYSTEM_PATH: Final = "/usr/bin:/bin"
TRUSTED_USER_HOME: Final = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
TOOLCHAIN_ENV: Final = "AGENT_GOV_ACCEPTANCE_TOOLCHAIN_JSON"
TOOLCHAIN_ROOT_ENV: Final = "AGENTGOV_ACCEPTANCE_TOOLCHAIN_ROOT"
FORMAL_SOURCE_ROOT_ENV: Final = "AGENTGOV_ACCEPTANCE_FORMAL_SOURCE_ROOT"
ACCEPTANCE_TARGET_ENV: Final = "AGENT_GOV_CONTAINER_ACCEPTANCE_TARGET"
MINIMUM_NODE_MAJOR: Final = 22
BROWSER_ACCEPTANCE_TARGETS: Final = frozenset(
    {
        "container-release-candidate",
        "main-flow-live-test",
        "ui-agent-candidate-technical-smoke",
        "ui-feedback-smoke",
        "ui-playground-cancel-smoke",
        "ui-playground-technical-smoke",
    }
)
TOOL_PATH_ENV_KEYS: Final = {
    "awk": "AGENTGOV_ACCEPTANCE_AWK",
    "bash": "AGENTGOV_ACCEPTANCE_BASH",
    "curl": "AGENTGOV_ACCEPTANCE_CURL",
    "docker": "AGENTGOV_ACCEPTANCE_DOCKER",
    "docker-buildx": "AGENTGOV_ACCEPTANCE_DOCKER_BUILDX",
    "docker-compose": "AGENTGOV_ACCEPTANCE_DOCKER_COMPOSE",
    "git": "AGENTGOV_ACCEPTANCE_GIT",
    "make": "AGENTGOV_ACCEPTANCE_MAKE",
    "node": "AGENTGOV_ACCEPTANCE_NODE",
    "python": "AGENTGOV_ACCEPTANCE_PYTHON",
}
SYSTEM_TOOL_PATHS: Final = {
    "awk": Path("/usr/bin/awk"),
    "bash": Path("/usr/bin/bash"),
    "curl": Path("/usr/bin/curl"),
    "docker": Path("/usr/bin/docker"),
    "docker-buildx": Path("/usr/libexec/docker/cli-plugins/docker-buildx"),
    "docker-compose": Path("/usr/libexec/docker/cli-plugins/docker-compose"),
    "git": Path("/usr/bin/git"),
    "make": Path("/usr/bin/make"),
}
_IDENTITY_KEYS: Final = frozenset(
    {
        "name",
        "kind",
        "path",
        "real_path",
        "version",
        "entrypoint",
        "file_count",
        "path_device",
        "path_inode",
        "path_mode",
        "path_uid",
        "path_gid",
        "path_size",
        "path_mtime_ns",
        "path_ctime_ns",
        "real_device",
        "real_inode",
        "real_mode",
        "real_uid",
        "real_gid",
        "real_size",
        "real_mtime_ns",
        "real_ctime_ns",
        "sha256",
    }
)
_TOOLCHAIN_KEYS: Final = frozenset({"schema_version", "stage", "acceptance_target", "execution_root", "tools", "artifacts"})
_T = TypeVar("_T", bound=Exception)


class AcceptanceToolchain(TypedDict):
    schema_version: int
    stage: str
    acceptance_target: str
    execution_root: str
    tools: list[BoundRuntimeIdentity]
    artifacts: list[BoundRuntimeIdentity]


AcceptanceEnvironment = dict[str, str]
IdentityMap = dict[str, BoundRuntimeIdentity]


def _require_system_tool(path: Path, *, error_type: type[_T], name: str) -> None:
    resolved, launcher = _resolved_path(path, error_type=error_type, label=name)
    try:
        real = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        _raise(error_type, f"{name} 系统工具身份不可用", exc)
    launcher_writable = not stat.S_ISLNK(launcher.st_mode) and bool(launcher.st_mode & 0o022)
    if launcher.st_uid != 0 or real.st_uid != 0 or launcher_writable or real.st_mode & 0o022:
        _raise(error_type, f"{name} 必须是 root 持有且不可由普通用户写入的系统工具")
    if resolved.parent not in {Path("/usr/bin"), Path("/usr/libexec/docker/cli-plugins")}:
        _raise(error_type, f"{name} 系统工具解析到非可信目录")


def _trusted_node_path(path: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat(follow_symlinks=False)
    except OSError:
        return False
    if not stat.S_ISREG(metadata.st_mode) or not metadata.st_mode & 0o111 or metadata.st_mode & stat.S_IWOTH:
        return False
    if resolved == Path("/usr/bin/node"):
        return metadata.st_uid == 0 and not metadata.st_mode & stat.S_IWGRP
    allowed_roots = (
        TRUSTED_USER_HOME / ".config/nvm/versions/node",
        TRUSTED_USER_HOME / ".nvm/versions/node",
    )
    for root in allowed_roots:
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        return (
            len(relative.parts) == 3
            and re.fullmatch(r"v\d+\.\d+\.\d+", relative.parts[0]) is not None
            and relative.parts[1:] == ("bin", "node")
            and metadata.st_uid == os.getuid()
        )
    return False


def _trusted_python_path(path: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat(follow_symlinks=False)
        path.absolute().relative_to(TRUSTED_USER_HOME)
    except (OSError, ValueError):
        return False
    return (
        path.parts[-2:] == ("bin", "python")
        and path.parent.parent.name == ".venv"
        and stat.S_ISREG(metadata.st_mode)
        and bool(metadata.st_mode & 0o111)
        and metadata.st_uid in {0, os.getuid()}
        and not metadata.st_mode & stat.S_IWOTH
    )


def _node_version(path: Path, *, error_type: type[_T]) -> str:
    try:
        result = subprocess.run(
            [str(path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": TRUSTED_SYSTEM_PATH},
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _raise(error_type, "可信 Node 无法执行版本核验", exc)
    version = result.stdout.strip()
    match = re.fullmatch(r"v(\d+)\.\d+\.\d+", version)
    if result.returncode or match is None or int(match.group(1)) < MINIMUM_NODE_MAJOR:
        _raise(error_type, f"浏览器验收要求 Node >= {MINIMUM_NODE_MAJOR}")
    return version


def _resolve_node(environ: dict[str, str], *, error_type: type[_T]) -> tuple[Path, str]:
    ambient = shutil.which("node", path=environ.get("PATH", ""))
    if ambient is not None and not _trusted_node_path(Path(ambient)):
        _raise(error_type, "PATH 中优先命中的 Node 位于非可信路径")
    candidates: list[Path] = []
    if ambient is not None:
        candidates.append(Path(ambient))
    for root in (
        TRUSTED_USER_HOME / ".config/nvm/versions/node",
        TRUSTED_USER_HOME / ".nvm/versions/node",
    ):
        candidates.extend(sorted(root.glob("v*/bin/node"), reverse=True))
    candidates.append(Path("/usr/bin/node"))
    for candidate in dict.fromkeys(candidates):
        if not _trusted_node_path(candidate):
            continue
        try:
            return candidate.absolute(), _node_version(candidate.absolute(), error_type=error_type)
        except Exception as exc:
            if not isinstance(exc, error_type):
                raise
    _raise(error_type, f"未找到可信且版本不低于 {MINIMUM_NODE_MAJOR} 的 Node")


def _package_payload(path: Path, *, error_type: type[_T], label: str) -> dict[str, object]:
    payload, _metadata = _stable_file_payload(path, error_type=error_type, label=label)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _raise(error_type, f"{label} 不是有效 JSON", exc)
    if not isinstance(value, dict):
        _raise(error_type, f"{label} JSON schema 无效")
    return value


def _package_entrypoint(payload: dict[str, object]) -> str | None:
    main = payload.get("main")
    if isinstance(main, str):
        return main.removeprefix("./")
    exports = payload.get("exports")
    if not isinstance(exports, dict) or not isinstance(exports.get("."), dict):
        return None
    root_export = exports["."]
    for key in ("require", "default", "import"):
        value = root_export.get(key)
        if isinstance(value, str):
            return value.removeprefix("./")
    return None


def _playwright_lock_version(lock_path: Path, *, error_type: type[_T]) -> str:
    payload, _metadata = _stable_file_payload(lock_path, error_type=error_type, label="Playwright lock")
    try:
        source = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        _raise(error_type, "Playwright lock 不是 UTF-8", exc)
    matches = re.findall(
        r"(?m)^      playwright:\n        specifier: [^\n]+\n        version: ([^\s]+)\s*$",
        source,
    )
    if len(matches) != 1 or re.fullmatch(r"\d+\.\d+\.\d+", matches[0]) is None:
        _raise(error_type, "Playwright lock 无法解析唯一安装版本")
    return matches[0]


def _browser_cache_root(environ: dict[str, str], playwright_root: Path, *, error_type: type[_T]) -> Path:
    configured = environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if configured == "0":
        return playwright_root / ".local-browsers"
    if configured:
        candidate = Path(configured)
        if not candidate.is_absolute():
            _raise(error_type, "PLAYWRIGHT_BROWSERS_PATH 必须是绝对可信路径或 0")
        resolved = candidate.resolve(strict=False)
    else:
        resolved = TRUSTED_USER_HOME / ".cache/ms-playwright"
    try:
        resolved.resolve(strict=False).relative_to(TRUSTED_USER_HOME)
    except ValueError:
        _raise(error_type, "Playwright 浏览器目录必须位于当前用户 home 内")
    return resolved


def _capture_browser_binaries(
    core_root: Path,
    environ: dict[str, str],
    *,
    error_type: type[_T],
) -> list[BoundRuntimeIdentity]:
    browsers_file = core_root / "browsers.json"
    browsers = _package_payload(browsers_file, error_type=error_type, label="Playwright browsers.json").get("browsers")
    if not isinstance(browsers, list) or platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        _raise(error_type, "当前平台无法静态绑定 Playwright 浏览器可执行文件")
    revisions = {
        item.get("name"): (item.get("revision"), item.get("browserVersion"))
        for item in browsers
        if isinstance(item, dict) and item.get("name") in {"chromium", "chromium-headless-shell", "firefox"}
    }
    cache_root = _browser_cache_root(environ, core_root, error_type=error_type)
    executable_parts = {
        "chromium": ("chromium", "chrome-linux64", "chrome"),
        "chromium-headless-shell": (
            "chromium_headless_shell",
            "chrome-headless-shell-linux64",
            "chrome-headless-shell",
        ),
        "firefox": ("firefox", "firefox", "firefox"),
    }
    artifacts: list[BoundRuntimeIdentity] = []
    for browser_name in ("chromium", "chromium-headless-shell", "firefox"):
        revision, browser_version = revisions.get(browser_name, (None, None))
        if not isinstance(revision, str) or not isinstance(browser_version, str):
            _raise(error_type, f"Playwright {browser_name} 版本元数据缺失")
        cache_name, *relative = executable_parts[browser_name]
        browser_root = cache_root / f"{cache_name}-{revision}"
        executable = browser_root / Path(*relative)
        artifacts.append(
            _capture_tree(
                f"playwright-{browser_name}-tree",
                browser_root,
                version=browser_version,
                entrypoint=Path(*relative).as_posix(),
                error_type=error_type,
            )
        )
        artifacts.append(
            _capture_file(
                f"playwright-{browser_name}",
                executable,
                kind="executable",
                error_type=error_type,
                executable=True,
                version=browser_version,
            )
        )
    return artifacts


def _capture_browser_artifacts(environ: dict[str, str], *, error_type: type[_T]) -> list[BoundRuntimeIdentity]:
    package_file = REPO_ROOT / "frontend/package.json"
    lock_file = REPO_ROOT / "frontend/pnpm-lock.yaml"
    package_link = REPO_ROOT / "frontend/node_modules/playwright"
    package = _package_payload(package_file, error_type=error_type, label="frontend package.json")
    playwright_root = package_link.resolve(strict=False)
    installed = _package_payload(playwright_root / "package.json", error_type=error_type, label="Playwright package.json")
    version = installed.get("version")
    main = _package_entrypoint(installed)
    dependencies = installed.get("dependencies")
    if (
        not isinstance(version, str)
        or not isinstance(main, str)
        or not isinstance(dependencies, dict)
        or dependencies.get("playwright-core") != version
        or _playwright_lock_version(lock_file, error_type=error_type) != version
    ):
        _raise(error_type, "Playwright package、core 与 lock 版本不一致")
    declared = package.get("devDependencies")
    if not isinstance(declared, dict) or "playwright" not in declared:
        _raise(error_type, "frontend package.json 未声明 Playwright")
    core_link = playwright_root.parent / "playwright-core"
    core_root = core_link.resolve(strict=False)
    package_store = (REPO_ROOT / "frontend/node_modules/.pnpm").resolve(strict=False)
    for resolved, label in ((playwright_root, "Playwright"), (core_root, "playwright-core")):
        try:
            resolved.relative_to(package_store)
        except ValueError:
            _raise(error_type, f"{label} 安装目录逃离 frontend/node_modules/.pnpm")
    core = _package_payload(core_root / "package.json", error_type=error_type, label="playwright-core package.json")
    core_main = _package_entrypoint(core)
    if core.get("version") != version or not isinstance(core_main, str):
        _raise(error_type, "playwright-core 安装版本或入口无效")
    artifacts = [
        _capture_file("frontend-package", package_file, kind="file", error_type=error_type, version=version),
        _capture_file("playwright-lock", lock_file, kind="file", error_type=error_type, version=version),
        _capture_tree("playwright-package", package_link, version=version, entrypoint=main, error_type=error_type),
        _capture_tree("playwright-core-package", core_link, version=version, entrypoint=core_main, error_type=error_type),
    ]
    artifacts.extend(_capture_browser_binaries(core_root, environ, error_type=error_type))
    return artifacts


def capture_acceptance_toolchain(
    environ: dict[str, str],
    acceptance_target: str,
    *,
    error_type: type[_T] = ValueError,
) -> AcceptanceToolchain:
    tools: list[BoundRuntimeIdentity] = []
    for name, path in SYSTEM_TOOL_PATHS.items():
        _require_system_tool(path, error_type=error_type, name=name)
        tools.append(_capture_file(name, path, kind="executable", error_type=error_type, executable=True))
    node_path, node_version = _resolve_node(environ, error_type=error_type)
    tools.append(
        _capture_file(
            "node",
            node_path,
            kind="executable",
            error_type=error_type,
            executable=True,
            version=node_version,
        )
    )
    tools.append(
        _capture_file(
            "python",
            REPO_ROOT / ".venv/bin/python",
            kind="executable",
            error_type=error_type,
            executable=True,
        )
    )
    tools.sort(key=lambda item: item["name"])
    artifacts = capture_python_artifacts(REPO_ROOT, error_type=error_type) if acceptance_target else []
    if acceptance_target in BROWSER_ACCEPTANCE_TARGETS:
        artifacts.extend(_capture_browser_artifacts(environ, error_type=error_type))
    artifacts.sort(key=lambda item: item["name"])
    return AcceptanceToolchain(
        schema_version=2,
        stage="source",
        acceptance_target=acceptance_target,
        execution_root="",
        tools=tools,
        artifacts=artifacts,
    )


def capture_file_identity(
    name: str,
    path: Path,
    *,
    kind: str,
    error_type: type[_T],
    executable: bool = False,
    version: str = "",
    entrypoint: str = "",
) -> BoundRuntimeIdentity:
    """Capture one materialized file without searching PATH."""

    return _capture_file(
        name,
        path,
        kind=kind,
        error_type=error_type,
        executable=executable,
        version=version,
        entrypoint=entrypoint,
    )


def capture_tree_identity(
    name: str,
    path: Path,
    *,
    version: str,
    entrypoint: str,
    error_type: type[_T],
) -> BoundRuntimeIdentity:
    """Capture one materialized dependency tree without resolving a package manager."""

    return _capture_tree(
        name,
        path,
        version=version,
        entrypoint=entrypoint,
        error_type=error_type,
    )


def materialized_tool_path(root: Path, name: str) -> Path:
    if name in {"docker-buildx", "docker-compose"}:
        return root / "docker-config/cli-plugins" / name
    return root / "bin" / name


def toolchain_environment(toolchain: AcceptanceToolchain) -> AcceptanceEnvironment:
    tools = {item["name"]: item for item in toolchain["tools"]}
    if set(tools) != set(TOOL_PATH_ENV_KEYS):
        raise ValueError("验收工具集合不精确")
    result = {TOOL_PATH_ENV_KEYS[name]: tools[name]["path"] for name in sorted(TOOL_PATH_ENV_KEYS)}
    result[TOOLCHAIN_ENV] = json.dumps(toolchain, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    result[TOOLCHAIN_ROOT_ENV] = toolchain["execution_root"]
    result[ACCEPTANCE_TARGET_ENV] = toolchain["acceptance_target"]
    root = Path(toolchain["execution_root"]) if toolchain["execution_root"] else None
    result["PATH"] = f"{root / 'bin'}:{TRUSTED_SYSTEM_PATH}" if root is not None else TRUSTED_SYSTEM_PATH
    python_path = Path(tools["python"]["path"])
    result.update(
        {
            "PYTHON": str(python_path),
            "PYTHON_RUN": str(python_path),
            "VENV": str(root if root is not None else python_path.parent.parent),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
        }
    )
    artifacts = {item["name"]: item for item in toolchain["artifacts"]}
    formal_source = artifacts.get("formal-source")
    if formal_source is not None:
        result[FORMAL_SOURCE_ROOT_ENV] = formal_source["path"]
        if root is None:
            result["PYTHONPATH"] = os.pathsep.join((formal_source["path"], str(Path(formal_source["path"]) / "packages/agentgov-testkit/src")))
        else:
            python_home, python_paths = materialized_python_paths(root, Path(formal_source["path"]))
            result["PYTHONHOME"] = str(python_home)
            result["PYTHONPATH"] = os.pathsep.join(str(path) for path in python_paths)
    if root is not None:
        result["DOCKER_CONFIG"] = str(root / "docker-config")
    if "playwright-package" in artifacts:
        result["NODE_PATH"] = str(Path(artifacts["playwright-package"]["path"]).parent)
        browser_tree = artifacts.get("playwright-chromium-tree")
        if browser_tree is not None:
            result["PLAYWRIGHT_BROWSERS_PATH"] = str(Path(browser_tree["path"]).parent)
    return result


def toolchain_from_environment(
    environ: dict[str, str],
    *,
    error_type: type[_T] = ValueError,
) -> AcceptanceToolchain:
    raw = environ.get(TOOLCHAIN_ENV, "")
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        _raise(error_type, "验收工具链身份缺失或不是有效 JSON", exc)
    if not isinstance(payload, dict) or set(payload) != _TOOLCHAIN_KEYS or payload.get("schema_version") != 2:
        _raise(error_type, "验收工具链 schema 不精确")
    stage = payload.get("stage")
    target = payload.get("acceptance_target")
    execution_root = payload.get("execution_root")
    if stage not in {"source", "materialized"} or not isinstance(target, str) or not isinstance(execution_root, str):
        _raise(error_type, "验收工具链阶段或目标无效")
    if (stage == "source" and execution_root) or (stage == "materialized" and not execution_root):
        _raise(error_type, "验收工具链执行根与阶段不一致")
    tools = payload.get("tools")
    artifacts = payload.get("artifacts")
    if not isinstance(tools, list) or not isinstance(artifacts, list):
        _raise(error_type, "验收工具链身份集合无效")
    for item in (*tools, *artifacts):
        string_fields = {
            "name",
            "kind",
            "path",
            "real_path",
            "version",
            "entrypoint",
            "sha256",
        }
        integer_fields = _IDENTITY_KEYS - string_fields
        if (
            not isinstance(item, dict)
            or set(item) != _IDENTITY_KEYS
            or any(not isinstance(item[field], str) for field in string_fields)
            or any(type(item[field]) is not int for field in integer_fields)
            or item["kind"] not in {"executable", "file", "tree"}
            or not Path(item["path"]).is_absolute()
            or not Path(item["real_path"]).is_absolute()
            or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
            or item["file_count"] < 1
        ):
            _raise(error_type, "验收工具链单项身份 schema 不精确")
    typed = AcceptanceToolchain(
        schema_version=2,
        stage=stage,
        acceptance_target=target,
        execution_root=execution_root,
        tools=tools,
        artifacts=artifacts,
    )  # type: ignore[typeddict-item]
    tool_map = {item["name"]: item for item in typed["tools"]}
    if len(tool_map) != len(typed["tools"]) or set(tool_map) != set(TOOL_PATH_ENV_KEYS):
        _raise(error_type, "验收工具链工具集合不精确")
    expected_path = f"{Path(execution_root) / 'bin'}:{TRUSTED_SYSTEM_PATH}" if stage == "materialized" else TRUSTED_SYSTEM_PATH
    if environ.get("PATH") != expected_path:
        _raise(error_type, "验收子进程 PATH 不是可信确定值")
    if environ.get(TOOLCHAIN_ROOT_ENV, "") != execution_root or environ.get(ACCEPTANCE_TARGET_ENV, "") != target:
        _raise(error_type, "验收工具链执行根或目标环境不一致")
    for name, env_key in TOOL_PATH_ENV_KEYS.items():
        if environ.get(env_key) != tool_map[name]["path"]:
            _raise(error_type, f"{name} 工具路径与绑定身份不一致")
    return typed


def _required_artifacts(target: str, stage: str) -> set[str]:
    required = (
        {
            "formal-quality-policy",
            "python-lock-pyproject",
            "python-lock-requirements",
            "python-lock-requirements-api",
            "python-lock-requirements-runtime",
            "python-lock-uv",
            "python-base-stdlib",
            "python-site-packages",
            "python-venv-config",
        }
        if target
        else set()
    )
    if stage == "materialized":
        required.add("formal-source")
    if target in BROWSER_ACCEPTANCE_TARGETS:
        required.update(
            {
                "frontend-package",
                "playwright-lock",
                "playwright-package",
                "playwright-core-package",
                "playwright-chromium",
                "playwright-chromium-tree",
                "playwright-chromium-headless-shell",
                "playwright-chromium-headless-shell-tree",
                "playwright-firefox",
                "playwright-firefox-tree",
            }
        )
    return required


def _require_private_root(root: Path, *, label: str, error_type: type[_T]) -> None:
    try:
        metadata = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError as exc:
        _raise(error_type, f"{label}不可用", exc)
    if (
        not root.is_absolute()
        or resolved != root
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        _raise(error_type, f"{label}必须是当前用户持有的 0700 真实目录")


def _validate_materialized_root(bound: AcceptanceToolchain, *, error_type: type[_T]) -> Path:
    root = Path(bound["execution_root"])
    _require_private_root(root, label="验收私有工具链根", error_type=error_type)
    for item in (*bound["tools"], *bound["artifacts"]):
        try:
            Path(item["path"]).relative_to(root)
            Path(item["real_path"]).relative_to(root)
        except ValueError:
            _raise(error_type, f"{item['name']} 逃离验收私有工具链根")
    return root


def _verify_tool_locations(
    bound: AcceptanceToolchain,
    tools: IdentityMap,
    *,
    error_type: type[_T],
) -> Path | None:
    if bound["stage"] == "source":
        for name, system_path in SYSTEM_TOOL_PATHS.items():
            if tools[name]["path"] != str(system_path):
                _raise(error_type, f"{name} 工具未绑定固定系统路径")
            _require_system_tool(system_path, error_type=error_type, name=name)
        if not _trusted_node_path(Path(tools["node"]["path"])):
            _raise(error_type, "Node 工具路径不可信")
        if not _trusted_python_path(Path(tools["python"]["path"])):
            _raise(error_type, "Python 工具路径不可信")
        return None
    root = _validate_materialized_root(bound, error_type=error_type)
    for name, identity in tools.items():
        if Path(identity["path"]) != materialized_tool_path(root, name):
            _raise(error_type, f"{name} 未绑定到验收私有工具副本")
    return root


def _verify_materialized_environment(
    environ: dict[str, str],
    bound: AcceptanceToolchain,
    tools: IdentityMap,
    root: Path,
    *,
    error_type: type[_T],
) -> None:
    formal_source = next(item for item in bound["artifacts"] if item["name"] == "formal-source")
    formal_path = Path(formal_source["path"])
    if environ.get(FORMAL_SOURCE_ROOT_ENV) != str(formal_path):
        _raise(error_type, "正式验收源码根未绑定到私有副本")
    if environ.get("DOCKER_CONFIG") != str(root / "docker-config"):
        _raise(error_type, "DOCKER_CONFIG 未绑定到私有 plugin 目录")
    for name in ("PYTHON", "PYTHON_RUN"):
        if environ.get(name) != tools["python"]["path"]:
            _raise(error_type, f"{name} 未绑定到私有 Python")
    if environ.get("VENV") != str(root):
        _raise(error_type, "VENV 未绑定到私有 Python 环境")
    verify_materialized_python_runtime(
        environ,
        execution_root=root,
        formal_root=formal_path,
        python_path=Path(tools["python"]["path"]),
        error_type=error_type,
    )


def _verify_browser_environment(
    environ: dict[str, str],
    bound: AcceptanceToolchain,
    *,
    error_type: type[_T],
) -> None:
    if bound["acceptance_target"] not in BROWSER_ACCEPTANCE_TARGETS:
        if environ.get("NODE_PATH"):
            _raise(error_type, "非浏览器验收不得注入 NODE_PATH")
        return
    artifacts = {item["name"]: item for item in bound["artifacts"]}
    package_node_path = str(Path(artifacts["playwright-package"]["path"]).parent)
    if environ.get("NODE_PATH") != package_node_path:
        _raise(error_type, "NODE_PATH 未绑定到已核验的 Playwright package root")
    chromium_root = artifacts["playwright-chromium-tree"]["path"]
    if environ.get("PLAYWRIGHT_BROWSERS_PATH") != str(Path(chromium_root).parent):
        _raise(error_type, "PLAYWRIGHT_BROWSERS_PATH 未绑定到私有浏览器副本")


def _verify_buildx_environment(environ: dict[str, str], *, error_type: type[_T]) -> None:
    if environ.get("AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE") != "1":
        return
    runtime_root = Path(environ.get("HOST_RUNTIME_VOLUME_ROOT", ""))
    buildx_state = runtime_root / "buildx-state"
    if environ.get("BUILDX_CONFIG") != str(buildx_state) or environ.get("BUILDX_BUILDER") != "default":
        _raise(error_type, "Docker Buildx state/builder 未绑定到私有本机 default")
    for path in (runtime_root, buildx_state):
        _require_private_root(path, label="Docker Buildx 私有目录", error_type=error_type)


def _recapture_toolchain(bound: AcceptanceToolchain, *, error_type: type[_T]) -> AcceptanceToolchain:
    current_tools = [
        _capture_file(
            item["name"],
            Path(item["path"]),
            kind=item["kind"],
            error_type=error_type,
            executable=True,
            version=item["version"],
            entrypoint=item["entrypoint"],
        )
        for item in bound["tools"]
    ]
    current_artifacts = [
        (
            _capture_tree(
                item["name"],
                Path(item["path"]),
                version=item["version"],
                entrypoint=item["entrypoint"],
                error_type=error_type,
            )
            if item["kind"] == "tree"
            else _capture_file(
                item["name"],
                Path(item["path"]),
                kind=item["kind"],
                error_type=error_type,
                executable=item["kind"] == "executable",
                version=item["version"],
                entrypoint=item["entrypoint"],
            )
        )
        for item in bound["artifacts"]
    ]
    return AcceptanceToolchain(
        schema_version=2,
        stage=bound["stage"],
        acceptance_target=bound["acceptance_target"],
        execution_root=bound["execution_root"],
        tools=current_tools,
        artifacts=current_artifacts,
    )


def verify_acceptance_toolchain(
    environ: dict[str, str],
    expected: AcceptanceToolchain | None = None,
    *,
    error_type: type[_T] = ValueError,
) -> AcceptanceToolchain:
    bound = toolchain_from_environment(environ, error_type=error_type)
    target = bound["acceptance_target"]
    tools = {item["name"]: item for item in bound["tools"]}
    root = _verify_tool_locations(bound, tools, error_type=error_type)
    artifact_names = {item["name"] for item in bound["artifacts"]}
    if artifact_names != _required_artifacts(target, bound["stage"]) or len(artifact_names) != len(bound["artifacts"]):
        _raise(error_type, "验收依赖集合与目标、阶段不一致")
    if root is not None:
        _verify_materialized_environment(environ, bound, tools, root, error_type=error_type)
    _verify_browser_environment(environ, bound, error_type=error_type)
    _verify_buildx_environment(environ, error_type=error_type)
    current = _recapture_toolchain(bound, error_type=error_type)
    if bound != current or (expected is not None and bound != expected):
        _raise(error_type, "验收工具或浏览器依赖身份已变化")
    return bound
