#!/usr/bin/env python3
"""Validate Docker image archive metadata before a remote daemon is mutated."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import tarfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

SOURCE_LABEL = "io.agentgov.source-artifact-sha256"
MAX_METADATA_BYTES = 8 * 1024 * 1024


class ImageArchiveError(RuntimeError):
    pass


def _safe_members(stream: tarfile.TarFile) -> Mapping[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    for member in stream.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
            raise ImageArchiveError("Docker image archive 含路径逃逸/link/special entry")
        normalized = path.as_posix()
        if normalized in members:
            raise ImageArchiveError("Docker image archive 含重复 member")
        members[normalized] = member
    return members


def _read_metadata(stream: tarfile.TarFile, members: Mapping[str, tarfile.TarInfo], name: str) -> bytes:
    member = members.get(PurePosixPath(name).as_posix())
    if member is None or not member.isfile() or member.size > MAX_METADATA_BYTES:
        raise ImageArchiveError(f"Docker image archive metadata 缺失或过大: {name}")
    source = stream.extractfile(member)
    if source is None:
        raise ImageArchiveError(f"Docker image archive metadata 无法读取: {name}")
    payload = source.read(MAX_METADATA_BYTES + 1)
    if len(payload) != member.size or len(payload) > MAX_METADATA_BYTES:
        raise ImageArchiveError(f"Docker image archive metadata 长度无效: {name}")
    return payload


def _config_digest(config_name: str, payload: bytes) -> None:
    basename = PurePosixPath(config_name).name.removesuffix(".json")
    if len(basename) != 64 or any(character not in "0123456789abcdef" for character in basename):
        raise ImageArchiveError("Docker image config 文件名不是 SHA-256")
    if hashlib.sha256(payload).hexdigest() != basename:
        raise ImageArchiveError("Docker image config digest 与 archive 内容不一致")


def validate_image_archive(
    archive: Path,
    *,
    architecture: str,
    expected_images: frozenset[str] = frozenset(),
    source_digest: str | None = None,
    forbidden_prefix: str | None = None,
) -> None:
    metadata = archive.lstat()
    if archive.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ImageArchiveError("Docker image archive 必须是普通非 symlink 文件")
    with tarfile.open(archive, mode="r:gz") as stream:
        members = _safe_members(stream)
        try:
            manifest = json.loads(_read_metadata(stream, members, "manifest.json"))
        except json.JSONDecodeError as exc:
            raise ImageArchiveError("Docker image archive manifest 不是 JSON") from exc
        if not isinstance(manifest, list) or not manifest or len(manifest) > 64:
            raise ImageArchiveError("Docker image archive manifest 结构无效")
        observed_images: set[str] = set()
        for item in manifest:
            if not isinstance(item, dict) or not isinstance(item.get("Config"), str):
                raise ImageArchiveError("Docker image archive manifest entry 无效")
            tags = item.get("RepoTags")
            if not isinstance(tags, list) or not tags or not all(isinstance(tag, str) and tag for tag in tags):
                raise ImageArchiveError("Docker image archive 缺少明确 RepoTags")
            observed_images.update(tags)
            config_payload = _read_metadata(stream, members, item["Config"])
            _config_digest(item["Config"], config_payload)
            try:
                config = json.loads(config_payload)
            except json.JSONDecodeError as exc:
                raise ImageArchiveError("Docker image config 不是 JSON") from exc
            if not isinstance(config, dict) or config.get("os") != "linux" or config.get("architecture") != architecture:
                raise ImageArchiveError("Docker image archive platform 与远端 daemon 不一致")
            labels = config.get("config", {}).get("Labels") if isinstance(config.get("config"), dict) else None
            if source_digest is not None and (not isinstance(labels, dict) or labels.get(SOURCE_LABEL) != source_digest):
                raise ImageArchiveError("Docker project image 未绑定目标 source artifact")
    if expected_images and observed_images != set(expected_images):
        raise ImageArchiveError("Docker project archive image set 与目标版本不一致")
    if forbidden_prefix and any(image.startswith(forbidden_prefix) for image in observed_images):
        raise ImageArchiveError("Docker dependency archive 冒充 project image")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--architecture", required=True)
    parser.add_argument("--expected-image", action="append", default=[])
    parser.add_argument("--source-digest")
    parser.add_argument("--forbidden-prefix")
    args = parser.parse_args()
    try:
        validate_image_archive(
            args.archive,
            architecture=args.architecture,
            expected_images=frozenset(args.expected_image),
            source_digest=args.source_digest,
            forbidden_prefix=args.forbidden_prefix,
        )
    except (OSError, tarfile.TarError, ImageArchiveError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
