#!/usr/bin/env python3
"""版本一致性硬门：仓库根 VERSION 是唯一真相源，断言所有制品对齐到它。

检查项：
- app/version.py 的 APP_VERSION（运行时 / OpenAPI info.version / health runtime_version）== VERSION；
- frontend/package.json version == VERSION；
- docker-compose 的 agent-gov-* 镜像 tag 派生自 ${APP_VERSION}，不得硬编码版本字面量；
- HEAD 若打了 v* release tag，必须 == "v" + VERSION（堵"打 tag 不 bump 版本"的漂移）。

任一不一致即退出非零，供 codex-guard / make test 调用。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SEMVER = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?")


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def artifact_consistency_errors(root: Path, *, app_version: str) -> list[str]:
    errors: list[str] = []
    version_file = root / "VERSION"
    if not version_file.exists():
        return ["根 VERSION 文件缺失（版本唯一真相源）"]
    version = version_file.read_text(encoding="utf-8").strip()
    if not _SEMVER.fullmatch(version):
        return [f"VERSION 格式非法: {version!r}（应为 semver，如 2.7.15）"]
    if app_version != version:
        errors.append(f"app/version.py APP_VERSION={app_version!r} != VERSION={version!r}（version.py 应读取 VERSION，不得硬编码）")
    pkg = json.loads((root / "frontend" / "package.json").read_text(encoding="utf-8"))
    if pkg.get("version") != version:
        errors.append(f"frontend/package.json version={pkg.get('version')!r} != VERSION={version!r}（运行 make sync-version 同步）")
    compose = (root / "docker" / "docker-compose.yml").read_text(encoding="utf-8")
    hardcoded = re.findall(r"image:\s*(agent-gov-[a-z-]+:(?!\$\{APP_VERSION)\S+)", compose)
    if hardcoded:
        errors.append(f"docker-compose 镜像 tag 硬编码了版本（应用 ${{APP_VERSION:-dev}} 派生）: {hardcoded}")
    return errors


def main() -> None:
    sys.path.insert(0, str(ROOT))
    from app.version import APP_VERSION

    errors = artifact_consistency_errors(ROOT, app_version=APP_VERSION)
    if errors:
        _fail(errors[0])
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

    # 仅在工作区干净时校验 tag：bump 提交前 HEAD 仍指向上一个 release（tag 与已 bump 的 VERSION 必然短暂不符），
    # 那是正常瞬态而非漂移；提交后 HEAD 是未打 tag 的新 commit，打 tag 时再校验即可。干净工作树下此门生效。
    try:
        # 只看已跟踪文件的未提交改动；未跟踪的私有文件（如 docker/.env.bak）不应使 tag 校验失效。
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=str(ROOT), capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except Exception:
        dirty = ""
    release_tags: list[str] = []
    if not dirty:
        try:
            out = subprocess.run(["git", "tag", "--points-at", "HEAD"], cwd=str(ROOT), capture_output=True, text=True, timeout=10)
            release_tags = [t for t in out.stdout.split() if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
        except Exception:
            release_tags = []
        for tag in release_tags:
            if tag != f"v{version}":
                _fail(f"HEAD 的 release tag {tag} != v{version}（打 release tag 必须与 VERSION 一致）")

    # 软告警（不 fail）：VERSION 领先本地最新 release tag = "bump 了版本但还没打 tag" 的漂移。
    # 单向硬门有意允许"bump 后延迟 tag"的开发窗口，这里只主动提醒，发布点用 `make tag` 补齐。
    try:
        all_tags = subprocess.run(["git", "tag", "--list", "v*"], cwd=str(ROOT), capture_output=True, text=True, timeout=10).stdout.split()
    except Exception:
        all_tags = []
    semver_tags = [t for t in all_tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    if semver_tags and m:

        def _ver_key(t: str) -> tuple[int, int, int]:
            a, b, c = t.lstrip("v").split(".")
            return (int(a), int(b), int(c))

        latest = max(semver_tags, key=_ver_key)
        if (int(m.group(1)), int(m.group(2)), int(m.group(3))) > _ver_key(latest):
            print(f"WARN: VERSION={version} 已领先最新 release tag {latest}；发布点请运行 `make tag` 补齐（不阻断）")

    suffix = f", HEAD release tag={release_tags}" if release_tags else ""
    print(f"OK: version consistency — VERSION={version}; app/frontend/compose 对齐{suffix}")


if __name__ == "__main__":
    main()
