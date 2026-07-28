from __future__ import annotations

import asyncio
from pathlib import Path

from app.runtime.claude_cli_raw_capture import ClaudeCliRawCapture


def _fake_cli(path: Path, payload: bytes) -> Path:
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import os",
                "import sys",
                "if sys.argv[1:] in (['-v'], ['--version']):",
                "    os.write(1, b'9.8.7 fake-cli\\n')",
                "else:",
                f"    os.write(1, bytes.fromhex('{payload.hex()}'))",
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def test_cli_tee_preserves_stdout_bytes_and_bypasses_version_probe(tmp_path: Path) -> None:
    payload = b'  {"type":"assistant","dup":1,"dup":2}\r\nnot-json\x00\xff\n{"type":"unknown","content":[{"type":"future_block"}]}'
    target = _fake_cli(tmp_path / "fake-claude", payload)

    async def exercise() -> None:
        capture = await ClaudeCliRawCapture.open(target)
        directory = capture.directory
        version = await asyncio.create_subprocess_exec(
            str(capture.wrapper_path),
            "-v",
            stdout=asyncio.subprocess.PIPE,
        )
        version_stdout, _ = await version.communicate()
        assert version.returncode == 0
        assert version_stdout == b"9.8.7 fake-cli\n"
        assert capture.connected is False

        process = await asyncio.create_subprocess_exec(
            str(capture.wrapper_path),
            "--output-format",
            "stream-json",
            stdout=asyncio.subprocess.PIPE,
        )
        tapped = bytearray()
        while chunk := await capture.read():
            tapped.extend(chunk)
        stdout, _ = await process.communicate()

        assert process.returncode == 0
        assert stdout == payload
        assert bytes(tapped) == payload
        await capture.aclose()
        assert not directory.exists()

    asyncio.run(exercise())
