from __future__ import annotations

import io
import tarfile

import yaml


def package_with_agent_id(package: bytes, agent_id: str) -> bytes:
    """重打测试包并显式声明目标 ID，模拟包所有者修改而非平台改写。"""
    files: dict[str, tuple[bytes, int]] = {}
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as source_archive:
        for member in source_archive:
            if not member.isfile() or not member.name.startswith("workspace/"):
                continue
            relative = member.name.removeprefix("workspace/")
            source = source_archive.extractfile(member)
            assert source is not None
            files[relative] = (source.read(), member.mode)
    raw_manifest = files.get("agent.yaml", (b"", 0o644))[0]
    loaded = yaml.safe_load(raw_manifest.decode("utf-8")) if raw_manifest else {}
    manifest = loaded if isinstance(loaded, dict) else {}
    agent = manifest.get("agent")
    if not isinstance(agent, dict):
        agent = {}
        manifest["agent"] = agent
    agent["id"] = agent_id
    files["agent.yaml"] = (
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True).encode("utf-8"),
        0o644,
    )

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as target_archive:
        root = tarfile.TarInfo("workspace/")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        target_archive.addfile(root)
        for relative, (content, mode) in sorted(files.items()):
            member = tarfile.TarInfo(f"workspace/{relative}")
            member.size = len(content)
            member.mode = mode
            target_archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()
