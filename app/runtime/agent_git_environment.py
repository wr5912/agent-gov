from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


class GovernedGitEnvironment(dict[str, str]):
    """Environment owned by AgentGov Git authority calls."""


class GovernedGitEnvironmentError(RuntimeError):
    pass


@dataclass(frozen=True)
class GovernedGitScope:
    work_tree: Path
    git_dir: Path
    common_git_dir: Path


_GIT_EXECUTABLE = shutil.which("git", path=os.defpath)
_SAFE_FSMONITOR_VIEWER = shutil.which("echo", path=os.defpath)
_FALSE_EXECUTABLE = shutil.which("false", path=os.defpath) or os.devnull
_CAT_EXECUTABLE = shutil.which("cat", path=os.defpath) or os.devnull
_GOVERNED_CONFIG = (
    ("core.fsmonitor", "false"),
    ("core.hooksPath", os.devnull),
    ("core.attributesFile", os.devnull),
    ("diff.external", ""),
    ("interactive.diffFilter", ""),
    ("commit.gpgSign", "false"),
    ("tag.gpgSign", "false"),
    ("tag.forceSignAnnotated", "false"),
    ("credential.helper", ""),
    ("protocol.allow", "never"),
    ("protocol.file.allow", "never"),
    ("protocol.ext.allow", "never"),
    ("submodule.recurse", "false"),
    ("fetch.recurseSubmodules", "false"),
)
_PROHIBITED_CONFIG_KEY = re.compile(
    r"(?:"
    r"alias\..+|"
    r"include(?:if\..+)?\.path|"
    r"filter\..+\.(?:clean|smudge|process)|"
    r"diff\..+\.(?:command|textconv)|"
    r"merge\..+\.driver|"
    r"(?:commit|tag)\.gpgsign|"
    r"tag\.forcesignannotated|"
    r"gpg(?:\..+)?\.program|"
    r"gpg\.format|"
    r"credential(?:\..+)?\.helper|"
    r"core\.(?:worktree|sshcommand|gitproxy)|"
    r"submodule\..+\.update|"
    r"tar\..+\.command|"
    r"extensions\.partialclone|"
    r"remote\..+\.(?:promisor|partialclonefilter)"
    r")\Z"
)
_PROHIBITED_GIT_PATHS = (
    ("objects", "info", "alternates"),
    ("objects", "info", "http-alternates"),
    ("info", "grafts"),
    ("shallow",),
)
_POLICY_ERROR = "Workspace Git repository violates the governed command policy"
_POLICY_TIMEOUT_SECONDS = 5.0
_OBJECT_ID_TEXT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def governed_git_environment(
    *,
    repository: Path | None = None,
    index_file: Path | None = None,
    optional_locks: bool | None = None,
) -> GovernedGitEnvironment:
    environment = GovernedGitEnvironment({key: value for key, value in os.environ.items() if not key.startswith("GIT_")})
    governed_config = list(_GOVERNED_CONFIG)
    if repository is not None:
        work_tree = repository.absolute()
        governed_config.extend(
            (
                ("safe.directory", str(work_tree)),
                ("core.bare", "false"),
                ("core.worktree", str(work_tree)),
            )
        )
    environment.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_ASKPASS": _FALSE_EXECUTABLE,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_EXTERNAL_DIFF": "",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_PAGER": _CAT_EXECUTABLE,
            "GIT_PROTOCOL_FROM_USER": "0",
            "GIT_SSH_COMMAND": _FALSE_EXECUTABLE,
            "GIT_TERMINAL_PROMPT": "0",
            "SSH_ASKPASS": _FALSE_EXECUTABLE,
            "GIT_CONFIG_COUNT": str(len(governed_config)),
        }
    )
    for index, (key, value) in enumerate(governed_config):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    if index_file is not None:
        environment["GIT_INDEX_FILE"] = str(index_file)
    if optional_locks is not None:
        environment["GIT_OPTIONAL_LOCKS"] = "1" if optional_locks else "0"
    return environment


def governed_index_query(arguments: Sequence[str]) -> list[str]:
    if _SAFE_FSMONITOR_VIEWER is None:
        raise GovernedGitEnvironmentError("Safe Git index flag viewer is unavailable")
    return ["-c", f"core.fsmonitor={_SAFE_FSMONITOR_VIEWER}", *arguments]


def governed_git_command(
    repository: Path,
    arguments: Sequence[str],
    *,
    allow_initialization: bool = False,
) -> list[str]:
    if _GIT_EXECUTABLE is None:
        raise GovernedGitEnvironmentError("Governed Git executable is unavailable")
    if allow_initialization and arguments and arguments[0] == "init" and not _path_lexists(repository / ".git"):
        return [_GIT_EXECUTABLE, "--no-pager", *arguments]
    scope = governed_git_scope(repository)
    return [
        _GIT_EXECUTABLE,
        "--no-pager",
        f"--git-dir={scope.git_dir}",
        f"--work-tree={scope.work_tree}",
        *arguments,
    ]


def governed_git_scope(repository: Path) -> GovernedGitScope:
    work_tree = repository.absolute()
    _require_directory(work_tree)
    metadata = work_tree / ".git"
    metadata_stat = _safe_lstat(metadata)
    if stat.S_ISDIR(metadata_stat.st_mode) and not stat.S_ISLNK(metadata_stat.st_mode):
        git_dir = metadata
        if _path_lexists(git_dir / "commondir"):
            raise GovernedGitEnvironmentError(_POLICY_ERROR)
        common_git_dir = git_dir
    elif stat.S_ISREG(metadata_stat.st_mode) and not stat.S_ISLNK(metadata_stat.st_mode):
        git_dir = _read_gitdir_pointer(metadata, work_tree)
        _require_linked_worktree_backlink(git_dir, metadata)
        common_git_dir = _common_git_dir(git_dir)
        if git_dir.parent.name != "worktrees" or common_git_dir != git_dir.parent.parent:
            raise GovernedGitEnvironmentError(_POLICY_ERROR)
    else:
        raise GovernedGitEnvironmentError(_POLICY_ERROR)
    _require_directory(git_dir)
    _require_directory(common_git_dir)
    return GovernedGitScope(
        work_tree=work_tree,
        git_dir=git_dir,
        common_git_dir=common_git_dir,
    )


def require_governed_repository(repository: Path) -> GovernedGitScope:
    scope = governed_git_scope(repository)
    _require_repository_metadata(scope)
    for parts in _PROHIBITED_GIT_PATHS:
        if _path_lexists(scope.common_git_dir.joinpath(*parts)):
            raise GovernedGitEnvironmentError(_POLICY_ERROR)
    for config_path in {scope.common_git_dir / "config", scope.git_dir / "config.worktree"}:
        if _path_lexists(config_path):
            metadata = _safe_lstat(config_path)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise GovernedGitEnvironmentError(_POLICY_ERROR)
    for key, value in _repository_config(scope):
        if _PROHIBITED_CONFIG_KEY.fullmatch(key):
            raise GovernedGitEnvironmentError(_POLICY_ERROR)
        if key == "core.bare" and value not in {"false", "no", "off", "0"}:
            raise GovernedGitEnvironmentError(_POLICY_ERROR)
    return scope


def _require_repository_metadata(scope: GovernedGitScope) -> None:
    for directory in (scope.common_git_dir / "objects", scope.common_git_dir / "refs"):
        _require_directory(directory)
        _require_no_symlinks(directory)
    for path in (
        scope.common_git_dir / "config",
        scope.common_git_dir / "packed-refs",
        scope.git_dir / "HEAD",
        scope.git_dir / "index",
        scope.git_dir / "config.worktree",
    ):
        if not _path_lexists(path):
            continue
        metadata = _safe_lstat(path)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise GovernedGitEnvironmentError(_POLICY_ERROR)
    _require_head_topology(scope)


def _require_head_topology(scope: GovernedGitScope) -> None:
    head = _read_single_line(scope.git_dir / "HEAD")
    if _OBJECT_ID_TEXT.fullmatch(head):
        return
    prefix = "ref: "
    if not head.startswith(prefix):
        raise GovernedGitEnvironmentError(_POLICY_ERROR)
    target = head.removeprefix(prefix)
    if not _safe_branch_ref(target):
        raise GovernedGitEnvironmentError(_POLICY_ERROR)
    loose_ref = scope.common_git_dir.joinpath(*target.split("/"))
    if _path_lexists(loose_ref) and _read_single_line(loose_ref).startswith("ref: "):
        raise GovernedGitEnvironmentError(_POLICY_ERROR)


def _safe_branch_ref(value: str) -> bool:
    return (
        value.startswith("refs/heads/")
        and value != "refs/heads/"
        and not value.endswith(("/", ".", ".lock"))
        and not any(token in value for token in ("..", "@{", "\\", " ", "~", "^", ":", "?", "*", "["))
        and all(part not in {"", "."} for part in value.split("/"))
    )


def _require_no_symlinks(root: Path) -> None:
    try:
        entries = tuple(os.scandir(root))
    except OSError:
        raise GovernedGitEnvironmentError(_POLICY_ERROR) from None
    for entry in entries:
        try:
            if entry.is_symlink():
                raise GovernedGitEnvironmentError(_POLICY_ERROR)
            if entry.is_dir(follow_symlinks=False):
                _require_no_symlinks(Path(entry.path))
            elif not entry.is_file(follow_symlinks=False):
                raise GovernedGitEnvironmentError(_POLICY_ERROR)
        except OSError:
            raise GovernedGitEnvironmentError(_POLICY_ERROR) from None


def _repository_config(scope: GovernedGitScope) -> tuple[tuple[str, str], ...]:
    if _GIT_EXECUTABLE is None:
        raise GovernedGitEnvironmentError(_POLICY_ERROR)
    command = [
        _GIT_EXECUTABLE,
        "--no-pager",
        f"--git-dir={scope.git_dir}",
        f"--work-tree={scope.work_tree}",
        "config",
        "--no-includes",
        "--null",
        "--list",
    ]
    try:
        process = subprocess.run(
            command,
            cwd=scope.work_tree,
            env=_policy_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=_POLICY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise GovernedGitEnvironmentError(_POLICY_ERROR) from None
    if process.returncode != 0:
        raise GovernedGitEnvironmentError(_POLICY_ERROR)
    rows: list[tuple[str, str]] = []
    for raw in process.stdout.split(b"\0"):
        if not raw:
            continue
        key_raw, separator, value_raw = raw.partition(b"\n")
        if not separator:
            raise GovernedGitEnvironmentError(_POLICY_ERROR)
        key = key_raw.decode("utf-8", errors="replace").lower()
        value = value_raw.decode("utf-8", errors="replace").strip().lower()
        rows.append((key, value))
    return tuple(rows)


def _policy_environment() -> GovernedGitEnvironment:
    environment = GovernedGitEnvironment({key: value for key, value in os.environ.items() if not key.startswith("GIT_")})
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _common_git_dir(git_dir: Path) -> Path:
    pointer = git_dir / "commondir"
    if not _path_lexists(pointer):
        return git_dir
    metadata = _safe_lstat(pointer)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise GovernedGitEnvironmentError(_POLICY_ERROR)
    value = _read_single_line(pointer)
    return (git_dir / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()


def _read_gitdir_pointer(metadata: Path, work_tree: Path) -> Path:
    value = _read_single_line(metadata)
    prefix = "gitdir: "
    if not value.startswith(prefix):
        raise GovernedGitEnvironmentError(_POLICY_ERROR)
    target = Path(value.removeprefix(prefix))
    return (work_tree / target).resolve() if not target.is_absolute() else target.resolve()


def _require_linked_worktree_backlink(git_dir: Path, metadata: Path) -> None:
    backlink = git_dir / "gitdir"
    backlink_stat = _safe_lstat(backlink)
    if not stat.S_ISREG(backlink_stat.st_mode) or stat.S_ISLNK(backlink_stat.st_mode):
        raise GovernedGitEnvironmentError(_POLICY_ERROR)
    target = Path(_read_single_line(backlink))
    resolved = (git_dir / target).resolve() if not target.is_absolute() else target.resolve()
    if resolved != metadata.resolve():
        raise GovernedGitEnvironmentError(_POLICY_ERROR)


def _read_single_line(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise GovernedGitEnvironmentError(_POLICY_ERROR) from None
    lines = value.splitlines()
    if len(lines) != 1 or not lines[0]:
        raise GovernedGitEnvironmentError(_POLICY_ERROR)
    return lines[0]


def _require_directory(path: Path) -> None:
    metadata = _safe_lstat(path)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise GovernedGitEnvironmentError(_POLICY_ERROR)


def _safe_lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError:
        raise GovernedGitEnvironmentError(_POLICY_ERROR) from None


def _path_lexists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise GovernedGitEnvironmentError(_POLICY_ERROR) from None
    return True
