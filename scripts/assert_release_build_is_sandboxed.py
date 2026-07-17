#!/usr/bin/env python3
"""校验被发布提交自带的 compose，其 build 段没有把构建面伸出 release 归档之外。

**为什么需要这道门**：`docker compose build` 跑在人工部署执行机上，构建用的
compose 文件来自**待部署提交自己的归档**。如果 `build.context` 指向执行机 HOME，
Dockerfile 就能把本机私有配置意外打进镜像。

这道基础检查只把构建输入限定在精确 commit 归档内，避免部署结果依赖执行机上的
额外文件；它不承担生产级供应链安全职责。

范围：只管**构建面伸出归档之外**。归档内的 Dockerfile 想干什么不归这里管
（那本就是「合并 = 发布」授予的）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

# 这些 build 键会把宿主机的凭据挂进构建过程，等于绕开「构建面限定在归档内」。
_HOST_SECRET_KEYS = ("secrets", "ssh")

# compose 的 context / additional_contexts 允许远程来源（git 仓库、URL、镜像、OCI layout）。
# 它们不读控制器磁盘，但同样把构建面伸出被发布的代码，且破坏不可变发布——同一个 SHA
# 两次构建可以拉到不同内容。当成相对路径去 resolve 会「解析」进归档里从而误判为合法，
# 故必须先按前缀识别出来。
_REMOTE_PREFIXES = (
    "http://",
    "https://",
    "git://",
    "git@",
    "ssh://",
    "docker-image://",
    "image:",
    "oci-layout:",
    "target:",
)


def _is_remote(value: str) -> bool:
    return value.startswith(_REMOTE_PREFIXES) or value.startswith("github.com/")


class BuildSandboxViolation(RuntimeError):
    """被发布提交试图把构建面伸出 release 归档。"""


def _resolve(base: Path, value: str) -> Path | None:
    """按 compose 的相对路径语义解析；软链接一并解析。"""
    candidate = Path(value)
    try:
        return candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    except OSError:
        return None


def _within(root: Path, resolved: Path | None) -> bool:
    if resolved is None:
        return False
    root = root.resolve()
    return resolved == root or root in resolved.parents


def _service_violations(name: str, build: Any, archive_root: Path, compose_dir: Path) -> list[str]:
    if isinstance(build, str):
        build = {"context": build}
    if not isinstance(build, dict):
        return [f"service {name}: build 段格式无法识别: {build!r}"]

    violations: list[str] = []
    for key in _HOST_SECRET_KEYS:
        if build.get(key):
            violations.append(f"service {name}: build.{key} 会把宿主机凭据挂进构建过程，而构建输入必须限定在待部署 commit 归档内")

    # compose 语义：context 相对 compose 文件所在目录；dockerfile 相对 context。
    # 按错的基准目录判定，会把 `context: ..`（= 归档根，真实 compose 就这么写）
    # 误判成逃逸，从而把这道门变成「挡死所有正常发布」。
    raw_context = build.get("context")
    context = compose_dir
    if raw_context is not None:
        if not isinstance(raw_context, str):
            violations.append(f"service {name}: build.context 必须是字符串，实为 {raw_context!r}")
        elif _is_remote(raw_context):
            violations.append(f"service {name}: build.context={raw_context!r} 是远程来源；构建面必须限定在被发布的代码内，且远程来源会破坏不可变发布")
        else:
            resolved_context = _resolve(compose_dir, raw_context)
            if not _within(archive_root, resolved_context):
                violations.append(f"service {name}: build.context={raw_context!r} 解析后落在 release 归档之外；构建面必须限定在被发布的代码内")
            elif resolved_context is not None:
                context = resolved_context

    dockerfile = build.get("dockerfile")
    if dockerfile is not None:
        if not isinstance(dockerfile, str):
            violations.append(f"service {name}: build.dockerfile 必须是字符串，实为 {dockerfile!r}")
        elif not _within(archive_root, _resolve(context, dockerfile)):
            violations.append(f"service {name}: build.dockerfile={dockerfile!r} 解析后落在 release 归档之外；构建面必须限定在被发布的代码内")

    extra = build.get("additional_contexts")
    entries: list[str] = []
    if isinstance(extra, dict):
        entries = [str(item) for item in extra.values()]
    elif isinstance(extra, list):
        entries = [str(item).split("=", 1)[-1] for item in extra]
    for entry in entries:
        if _is_remote(entry) or not _within(archive_root, _resolve(compose_dir, entry)):
            violations.append(f"service {name}: build.additional_contexts 指向 {entry!r}，落在 release 归档之外")
    return violations


def assert_build_is_sandboxed(
    compose_path: Path,
    archive_root: Path,
    services: list[str],
) -> None:
    """services 中任一服务的 build 段伸出归档时抛 BuildSandboxViolation。"""
    document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise BuildSandboxViolation(f"compose 文件格式无法识别: {compose_path}")
    defined = document.get("services")
    if not isinstance(defined, dict):
        raise BuildSandboxViolation(f"compose 文件没有 services 段: {compose_path}")

    violations: list[str] = []
    for name in services:
        service = defined.get(name)
        if not isinstance(service, dict):
            raise BuildSandboxViolation(f"compose 中缺少要构建的 service: {name}")
        build = service.get("build")
        if build is None:
            continue
        violations.extend(_service_violations(name, build, archive_root, compose_path.resolve().parent))

    if violations:
        raise BuildSandboxViolation("\n".join(violations))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--compose", required=True, type=Path, help="被发布提交的 compose 文件")
    parser.add_argument("--archive-root", required=True, type=Path, help="release 归档根目录")
    parser.add_argument("--service", action="append", default=[], help="要构建的 service，可重复")
    args = parser.parse_args(argv)

    try:
        assert_build_is_sandboxed(args.compose, args.archive_root, args.service)
    except (BuildSandboxViolation, OSError, yaml.YAMLError) as exc:
        print(f"[build-sandbox] 拒绝构建：{exc}", file=sys.stderr)
        return 1
    print(f"[build-sandbox] OK: {len(args.service)} 个 service 的构建面均限定在 release 归档内")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
