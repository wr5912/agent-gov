from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path

from .claude_cli import claude_cli_semver, resolve_claude_cli_path
from .runtime_raw_events import RuntimeRawCaptureUnavailableError

_SOCKET_NAME = "stdout.sock"
_WRAPPER_NAME = "claude"
_CONFIG_NAME = "claude.json"
_UNIX_SOCKET_PATH_LIMIT = 103


class ClaudeCliRawCapture:
    """A private, bounded Unix-socket sink for one transparent CLI tee."""

    def __init__(
        self,
        *,
        directory: Path,
        wrapper_path: Path,
        target_path: Path,
        socket_path: Path,
        runtime_version: str,
    ) -> None:
        self.directory = directory
        self.wrapper_path = wrapper_path
        self.target_path = target_path
        self.socket_path = socket_path
        self.runtime_version = runtime_version
        self._server: asyncio.AbstractServer | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connection: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._closed = False

    @classmethod
    async def open(cls, configured_cli_path: Path | None) -> ClaudeCliRawCapture:
        try:
            target_path = await asyncio.to_thread(resolve_claude_cli_path, configured_cli_path)
            wrapper_source = Path(__file__).with_name("claude_cli_tee.py").resolve()
            if target_path == wrapper_source:
                raise ValueError("CLAUDE_CLI_PATH cannot point to the AgentGov tee wrapper")

            temp_root = Path("/tmp") if Path("/tmp").is_dir() else Path(tempfile.gettempdir())
            directory = Path(tempfile.mkdtemp(prefix="agentgov-raw-", dir=temp_root))
            directory.chmod(0o700)
            wrapper_path = directory / _WRAPPER_NAME
            socket_path = directory / _SOCKET_NAME
            if len(os.fsencode(socket_path)) > _UNIX_SOCKET_PATH_LIMIT:
                raise OSError("Unix socket path is too long")

            await asyncio.to_thread(shutil.copyfile, wrapper_source, wrapper_path)
            wrapper_path.chmod(0o700)
            _write_private_config(
                directory / _CONFIG_NAME,
                {
                    "target_cli": str(target_path),
                    "socket_path": str(socket_path),
                },
            )
            runtime_version = await asyncio.to_thread(claude_cli_semver, target_path)
            capture = cls(
                directory=directory,
                wrapper_path=wrapper_path,
                target_path=target_path,
                socket_path=socket_path,
                runtime_version=runtime_version,
            )
            capture._server = await asyncio.start_unix_server(
                capture._accept_client,
                path=str(socket_path),
                limit=64 * 1024,
            )
            socket_path.chmod(0o600)
            return capture
        except Exception as exc:
            if "directory" in locals():
                await asyncio.to_thread(shutil.rmtree, directory, True)
            raise RuntimeRawCaptureUnavailableError(f"Raw Runtime capture could not be initialized ({exc.__class__.__name__})") from exc

    @property
    def connected(self) -> bool:
        return self._connection.done() and not self._connection.cancelled() and self._connection.exception() is None

    def _accept_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closed or self._connection.done():
            writer.close()
            return
        self._reader = reader
        self._writer = writer
        self._connection.set_result(None)
        if self._server is not None:
            self._server.close()

    async def wait_connected(self) -> None:
        await self._connection

    async def read(self, size: int = 64 * 1024) -> bytes:
        await self.wait_connected()
        if self._reader is None:
            raise RuntimeError("Raw capture connection has no reader")
        return await self._reader.read(size)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        server = self._server
        writer = self._writer
        self._server = None
        self._writer = None
        if server is not None:
            server.close()
        if not self._connection.done():
            self._connection.cancel()
        # Python 3.12 的 Server.wait_closed() 会等待活跃连接；必须先关闭
        # 已接受的 writer，否则 listener 与连接互相等待形成确定性死锁。
        if writer is not None:
            writer.close()
        if server is not None:
            await server.wait_closed()
        if writer is not None:
            with suppress(BrokenPipeError, ConnectionError):
                await writer.wait_closed()
        await asyncio.to_thread(shutil.rmtree, self.directory, True)


def _write_private_config(path: Path, config: dict[str, str]) -> None:
    payload = json.dumps(config, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)
