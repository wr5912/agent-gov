"""候选快照 Git 的固定 argv 与最小无配置环境。"""

from __future__ import annotations

import os
from pathlib import Path

from scripts import container_acceptance_tool_authority as tool_authority


class GitAuthorityError(RuntimeError):
    """候选 Git 调用偏离固定 repository 或 private state。"""


class GitEnvironment(dict[str, str]):
    """仅承载候选快照 Git 的固定无配置环境。"""


def git_argv(
    repository: Path,
    arguments: tuple[str, ...],
    *,
    git_executable: str,
    source_root: Path,
) -> tuple[str, ...]:
    if Path(os.path.abspath(repository)) != source_root or not arguments or any(not item or "\x00" in item for item in arguments):
        raise GitAuthorityError("candidate Git invocation is invalid")
    return (
        git_executable,
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "core.excludesFile=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "filter.lfs.required=false",
        "-c",
        "filter.lfs.process=",
        "-c",
        "filter.lfs.smudge=",
        "-c",
        "filter.lfs.clean=",
        *arguments,
    )


def git_environment(*, index_file: Path | None = None) -> GitEnvironment:
    try:
        git_home = tool_authority.private_state_paths().git_home
    except tool_authority.ToolFileAuthorityError as exc:
        raise GitAuthorityError("candidate Git private state is unavailable") from exc
    environment = GitEnvironment(
        {
            "HOME": str(git_home),
            "XDG_CONFIG_HOME": str(git_home),
            "PATH": "/usr/bin",
            "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    if index_file is not None:
        absolute = Path(os.path.abspath(index_file))
        if not absolute.is_absolute() or "\x00" in str(absolute):
            raise GitAuthorityError("candidate Git index authority is invalid")
        environment["GIT_INDEX_FILE"] = str(absolute)
    return environment
