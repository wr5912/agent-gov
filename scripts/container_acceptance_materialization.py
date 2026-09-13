"""把正式验收工具、依赖与源码闭包物化到单轮私有目录。"""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import stat
from pathlib import Path
from typing import NoReturn, TypeVar

from scripts.agentscope_atomic_cutover_bootstrap import source_artifact_sha256
from scripts.container_acceptance_toolchain import (
    AcceptanceToolchain,
    BoundRuntimeIdentity,
    capture_file_identity,
    capture_tree_identity,
    materialized_tool_path,
    toolchain_environment,
    verify_acceptance_toolchain,
)

_E = TypeVar("_E", bound=Exception)
_LOCK_ARTIFACTS = frozenset(
    {
        "python-lock-pyproject",
        "python-lock-requirements",
        "python-lock-requirements-api",
        "python-lock-requirements-runtime",
        "python-lock-uv",
    }
)
_BROWSER_TREES = frozenset(
    {
        "playwright-package",
        "playwright-core-package",
        "playwright-chromium-tree",
        "playwright-chromium-headless-shell-tree",
        "playwright-firefox-tree",
    }
)
_BROWSER_EXECUTABLE_TREES = {
    "playwright-chromium": "playwright-chromium-tree",
    "playwright-chromium-headless-shell": "playwright-chromium-headless-shell-tree",
    "playwright-firefox": "playwright-firefox-tree",
}
_INOTIFY_EVENT_MASK = 0x00000002 | 0x00000004 | 0x00000008 | 0x00000080 | 0x00000100 | 0x00000200 | 0x00000400 | 0x00000800
_IN_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_IN_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class ExecutionMutationGuard:
    """Keep an append-only kernel event record for every formal input directory."""

    def __init__(self, roots: tuple[Path, ...], *, error_type: type[_E]) -> None:
        self._error_type = error_type
        self._fd = -1
        self._watched: set[Path] = set()
        libc = ctypes.CDLL(None, use_errno=True)
        init = libc.inotify_init1
        init.argtypes = [ctypes.c_int]
        init.restype = ctypes.c_int
        descriptor = init(_IN_CLOEXEC | _IN_NONBLOCK)
        if descriptor < 0:
            _fail(error_type, "内核不支持正式验收输入变更监视", OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno())))
        self._fd = descriptor
        try:
            self.add_roots(roots)
        except BaseException:
            self.close()
            raise

    def add_roots(self, roots: tuple[Path, ...]) -> None:
        """在子进程消费前把新生成的回执目录纳入同一事件边界。"""

        if self._fd < 0:
            _fail(self._error_type, "正式验收输入变更监视已提前关闭")
        add_watch = ctypes.CDLL(None, use_errno=True).inotify_add_watch
        add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add_watch.restype = ctypes.c_int
        directories = {path.resolve() for root in roots for path in (root, *root.rglob("*")) if path.is_dir() and not path.is_symlink()}
        for directory in sorted(directories - self._watched):
            if add_watch(self._fd, os.fsencode(directory), _INOTIFY_EVENT_MASK) < 0:
                value = ctypes.get_errno()
                _fail(
                    self._error_type,
                    "无法覆盖正式验收输入目录变更监视",
                    OSError(value, os.strerror(value)),
                )
            self._watched.add(directory)

    def make_inheritable(self) -> int:
        """Retain the kernel event queue across the frozen-runner exec boundary."""

        if self._fd < 0:
            _fail(self._error_type, "正式验收输入变更监视已提前关闭")
        os.set_inheritable(self._fd, True)
        return self._fd

    @classmethod
    def from_inherited(cls, descriptor: int, *, error_type: type[_E]) -> ExecutionMutationGuard:
        if descriptor < 0:
            _fail(error_type, "冻结 runner 缺少输入变更监视")
        try:
            if not os.get_inheritable(descriptor):
                _fail(error_type, "冻结 runner 的输入变更监视未跨 exec 保留")
            os.set_inheritable(descriptor, False)
        except OSError as exc:
            _fail(error_type, "冻结 runner 的输入变更监视不可用", exc)
        guard = cls.__new__(cls)
        guard._error_type = error_type
        guard._fd = descriptor
        guard._watched = set()
        return guard

    def check(self) -> None:
        if self._fd < 0:
            _fail(self._error_type, "正式验收输入变更监视已提前关闭")
        try:
            event = os.read(self._fd, 1024 * 1024)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno == errno.EAGAIN:
                return
            _fail(self._error_type, "无法读取正式验收输入变更记录", exc)
        if event:
            _fail(self._error_type, "正式验收执行期间工具、依赖或源码发生变化")

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


def _fail(error_type: type[_E], message: str, cause: BaseException | None = None) -> NoReturn:
    error = error_type(message)
    if cause is None:
        raise error
    raise error from cause


def _ensure_private_root(path: Path, *, error_type: type[_E]) -> None:
    try:
        path.mkdir(mode=0o700)
        metadata = path.lstat()
    except OSError as exc:
        _fail(error_type, "无法创建验收私有工具链根", exc)
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or any(path.iterdir())
    ):
        _fail(error_type, "验收私有工具链根必须是当前用户持有的空 0700 真实目录")


def _assert_no_symlink(root: Path, *, error_type: type[_E], label: str) -> None:
    for path in (root, *root.rglob("*")):
        try:
            if stat.S_ISLNK(path.lstat().st_mode):
                _fail(error_type, f"{label} 含符号链接")
        except OSError as exc:
            _fail(error_type, f"{label} 无法稳定枚举", exc)


def _copy_file_exact(
    source: BoundRuntimeIdentity,
    destination: Path,
    *,
    error_type: type[_E],
    executable: bool = False,
) -> BoundRuntimeIdentity:
    current = capture_file_identity(
        source["name"],
        Path(source["path"]),
        kind=source["kind"],
        error_type=error_type,
        executable=source["kind"] == "executable",
        version=source["version"],
        entrypoint=source["entrypoint"],
    )
    if current != source:
        _fail(error_type, f"{source['name']} 在物化前发生变化")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with Path(source["real_path"]).open("rb") as reader, destination.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        destination.chmod(0o500 if executable else 0o400)
    except OSError as exc:
        _fail(error_type, f"无法物化 {source['name']}", exc)
    copied = capture_file_identity(
        source["name"],
        destination,
        kind=source["kind"],
        error_type=error_type,
        executable=executable,
        version=source["version"],
        entrypoint=source["entrypoint"],
    )
    if copied["sha256"] != source["sha256"] or copied["real_size"] != source["real_size"]:
        _fail(error_type, f"{source['name']} 私有副本与绑定源不一致")
    return copied


def _make_tree_read_only(root: Path, *, error_type: type[_E], label: str) -> None:
    _assert_no_symlink(root, error_type=error_type, label=label)
    paths = sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True)
    try:
        for path in paths:
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                path.chmod(0o500)
            elif stat.S_ISREG(metadata.st_mode):
                path.chmod(0o500 if metadata.st_mode & 0o111 else 0o400)
            else:
                _fail(error_type, f"{label} 含不支持的文件类型")
        root.chmod(0o500)
    except OSError as exc:
        _fail(error_type, f"无法收紧 {label} 权限", exc)


def _copy_tree_exact(
    source: BoundRuntimeIdentity,
    destination: Path,
    *,
    error_type: type[_E],
    read_only: bool = True,
) -> BoundRuntimeIdentity:
    current = capture_tree_identity(
        source["name"],
        Path(source["path"]),
        version=source["version"],
        entrypoint=source["entrypoint"],
        error_type=error_type,
    )
    if current != source:
        _fail(error_type, f"{source['name']} 在物化前发生变化")
    source_root = Path(source["real_path"])
    _assert_no_symlink(source_root, error_type=error_type, label=source["name"])
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        shutil.copytree(source_root, destination, copy_function=shutil.copy2)
    except OSError as exc:
        _fail(error_type, f"无法物化 {source['name']}", exc)
    copied = capture_tree_identity(
        source["name"],
        destination,
        version=source["version"],
        entrypoint=source["entrypoint"],
        error_type=error_type,
    )
    if copied["sha256"] != source["sha256"] or copied["file_count"] != source["file_count"]:
        _fail(error_type, f"{source['name']} 私有副本与绑定源不一致")
    if read_only:
        _make_tree_read_only(destination, error_type=error_type, label=source["name"])
        copied = capture_tree_identity(
            source["name"],
            destination,
            version=source["version"],
            entrypoint=source["entrypoint"],
            error_type=error_type,
        )
    return copied


def _freeze_formal_source(
    deployable_root: Path,
    destination: Path,
    policy: BoundRuntimeIdentity,
    *,
    error_type: type[_E],
) -> BoundRuntimeIdentity:
    source_digest = source_artifact_sha256(deployable_root)
    try:
        shutil.copytree(deployable_root, destination, copy_function=shutil.copy2)
    except OSError as exc:
        _fail(error_type, "无法物化正式验收源码闭包", exc)
    if source_artifact_sha256(destination) != source_digest:
        _fail(error_type, "正式验收源码副本与 deployable source 不一致")
    policy_copy = _copy_file_exact(
        policy,
        destination / "tests/quality_policy.json",
        error_type=error_type,
    )
    if policy_copy["sha256"] != policy["sha256"] or source_artifact_sha256(destination) != source_digest:
        _fail(error_type, "正式验收 policy 未精确附加到源码副本")
    return capture_tree_identity(
        "formal-source",
        destination,
        version=source_digest,
        entrypoint="Makefile",
        error_type=error_type,
    )


def _sanitize_python_site_packages(root: Path, *, error_type: type[_E]) -> None:
    for forbidden in (root / "sitecustomize.py", root / "usercustomize.py"):
        if forbidden.exists() or forbidden.is_symlink():
            _fail(error_type, f"Python 验收环境禁止 {forbidden.name}")
    try:
        for pth_file in root.glob("*.pth"):
            pth_file.unlink()
        for cache in root.rglob("__pycache__"):
            shutil.rmtree(cache)
        for bytecode in root.rglob("*.py[co]"):
            bytecode.unlink()
    except OSError as exc:
        _fail(error_type, "无法移除 Python 启动注入或预编译字节码", exc)


def _materialize_python_artifacts(
    source: dict[str, BoundRuntimeIdentity],
    root: Path,
    artifacts: dict[str, BoundRuntimeIdentity],
    *,
    error_type: type[_E],
) -> None:
    base_tree_source = source["python-base-stdlib"]
    base_tree_root = root / "python-base/lib/python3.11"
    _copy_tree_exact(base_tree_source, base_tree_root, error_type=error_type, read_only=False)
    base_site_packages = base_tree_root / "site-packages"
    try:
        if base_site_packages.exists():
            shutil.rmtree(base_site_packages)
        base_site_packages.mkdir(mode=0o500)
    except OSError as exc:
        _fail(error_type, "无法清除 base Python site-packages", exc)
    _sanitize_python_site_packages(base_tree_root, error_type=error_type)
    _make_tree_read_only(base_tree_root, error_type=error_type, label="python-base-stdlib")
    artifacts["python-base-stdlib"] = capture_tree_identity(
        "python-base-stdlib",
        base_tree_root,
        version=base_tree_source["version"],
        entrypoint=base_tree_source["entrypoint"],
        error_type=error_type,
    )
    python_tree_source = source["python-site-packages"]
    python_tree_root = root / "lib/python3.11/site-packages"
    _copy_tree_exact(python_tree_source, python_tree_root, error_type=error_type, read_only=False)
    _sanitize_python_site_packages(python_tree_root, error_type=error_type)
    _make_tree_read_only(python_tree_root, error_type=error_type, label="python-site-packages")
    artifacts["python-site-packages"] = capture_tree_identity(
        "python-site-packages",
        python_tree_root,
        version=python_tree_source["version"],
        entrypoint=python_tree_source["entrypoint"],
        error_type=error_type,
    )


def _materialize_browser_artifacts(
    source: dict[str, BoundRuntimeIdentity],
    root: Path,
    formal_root: Path,
    artifacts: dict[str, BoundRuntimeIdentity],
    *,
    error_type: type[_E],
) -> None:
    for name, relative in (
        ("frontend-package", "frontend/package.json"),
        ("playwright-lock", "frontend/pnpm-lock.yaml"),
    ):
        original = source[name]
        captured = capture_file_identity(
            name,
            formal_root / relative,
            kind="file",
            version=original["version"],
            error_type=error_type,
        )
        if captured["sha256"] != original["sha256"]:
            _fail(error_type, f"{name} 正式源码副本与绑定源不一致")
        artifacts[name] = captured
    tree_destinations = {
        "playwright-package": root / "node_modules/playwright",
        "playwright-core-package": root / "node_modules/playwright-core",
    }
    for name in sorted(_BROWSER_TREES):
        original = source[name]
        destination = tree_destinations.get(name, root / "browsers" / Path(original["real_path"]).name)
        artifacts[name] = _copy_tree_exact(original, destination, error_type=error_type)
    for name, tree_name in _BROWSER_EXECUTABLE_TREES.items():
        original = source[name]
        tree_source = source[tree_name]
        relative = Path(original["real_path"]).relative_to(Path(tree_source["real_path"]))
        artifacts[name] = capture_file_identity(
            name,
            Path(artifacts[tree_name]["path"]) / relative,
            kind="executable",
            executable=True,
            version=original["version"],
            error_type=error_type,
        )
        if artifacts[name]["sha256"] != original["sha256"]:
            _fail(error_type, f"{name} 浏览器可执行副本与绑定源不一致")


def _materialize_artifacts(
    source: dict[str, BoundRuntimeIdentity],
    root: Path,
    formal_source: BoundRuntimeIdentity,
    *,
    error_type: type[_E],
) -> list[BoundRuntimeIdentity]:
    artifacts: dict[str, BoundRuntimeIdentity] = {"formal-source": formal_source}
    for name in sorted(_LOCK_ARTIFACTS):
        artifacts[name] = _copy_file_exact(
            source[name],
            root / "locks" / name,
            error_type=error_type,
        )
    artifacts["python-venv-config"] = _copy_file_exact(
        source["python-venv-config"],
        root / "locks/python-venv-config",
        error_type=error_type,
    )
    _materialize_python_artifacts(source, root, artifacts, error_type=error_type)
    formal_root = Path(formal_source["path"])
    artifacts["formal-quality-policy"] = capture_file_identity(
        "formal-quality-policy",
        formal_root / "tests/quality_policy.json",
        kind="file",
        error_type=error_type,
    )
    if "frontend-package" not in source:
        return list(artifacts.values())
    _materialize_browser_artifacts(source, root, formal_root, artifacts, error_type=error_type)
    return list(artifacts.values())


def materialize_acceptance_toolchain(
    source: AcceptanceToolchain,
    execution_root: Path,
    deployable_root: Path,
    *,
    error_type: type[_E] = ValueError,
) -> tuple[AcceptanceToolchain, Path]:
    """Copy every executable/loadable input before formal acceptance starts."""

    verify_acceptance_toolchain(toolchain_environment(source), source, error_type=error_type)
    _ensure_private_root(execution_root, error_type=error_type)
    source_tools = {item["name"]: item for item in source["tools"]}
    source_artifacts = {item["name"]: item for item in source["artifacts"]}
    formal_source = _freeze_formal_source(
        deployable_root,
        execution_root / "formal-source",
        source_artifacts["formal-quality-policy"],
        error_type=error_type,
    )
    tools = [
        _copy_file_exact(
            item,
            materialized_tool_path(execution_root, name),
            error_type=error_type,
            executable=True,
        )
        for name, item in sorted(source_tools.items())
    ]
    artifacts = _materialize_artifacts(
        source_artifacts,
        execution_root,
        formal_source,
        error_type=error_type,
    )
    materialized = AcceptanceToolchain(
        schema_version=2,
        stage="materialized",
        acceptance_target=source["acceptance_target"],
        execution_root=str(execution_root),
        tools=sorted(tools, key=lambda item: item["name"]),
        artifacts=sorted(artifacts, key=lambda item: item["name"]),
    )
    verify_acceptance_toolchain(toolchain_environment(source), source, error_type=error_type)
    verify_acceptance_toolchain(
        toolchain_environment(materialized),
        materialized,
        error_type=error_type,
    )
    return materialized, Path(formal_source["path"])


def make_materialized_tree_disposable(root: Path, *, error_type: type[_E] = ValueError) -> None:
    """Restore owner write bits only after execution monitoring has stopped."""

    if not root.exists():
        return
    try:
        metadata = root.lstat()
    except OSError as exc:
        _fail(error_type, "无法核验待回收的验收工具链根", exc)
    if root.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        _fail(error_type, "拒绝回收非本轮真实目录的验收工具链")
    _assert_no_symlink(root, error_type=error_type, label="待回收验收工具链")
    try:
        for directory, _names, files in os.walk(root):
            current = Path(directory)
            current.chmod(0o700)
            for name in files:
                (current / name).chmod(0o600)
    except OSError as exc:
        _fail(error_type, "无法恢复验收工具链回收权限", exc)


def scrub_private_acceptance_inputs(temp_root: Path, *, error_type: type[_E] = ValueError) -> None:
    """Remove sensitive formal inputs while preserving diagnostic-safe metadata."""

    for name in ("execution-toolchain", "acceptance-inputs", "acceptance-context"):
        make_materialized_tree_disposable(temp_root / name, error_type=error_type)
    for name in (
        "acceptance-inputs",
        "acceptance-context",
        "compose.acceptance.env",
        "source-snapshot",
        "execution-toolchain",
    ):
        target = temp_root / name
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        except OSError as exc:
            _fail(error_type, "无法清除正式验收私有输入", exc)


def seal_materialized_input_tree(root: Path, *, error_type: type[_E] = ValueError) -> None:
    """在首个消费子进程前将固定 env、场景或回执目录收紧为只读。"""

    _make_tree_read_only(root, error_type=error_type, label="正式验收固定输入")
