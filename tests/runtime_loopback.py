from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from uvicorn import Config, Server


@dataclass(frozen=True)
class ForwardedHttpResponse:
    """真实 TCP 转发的元数据，不记录请求正文或凭据。"""

    method: str
    path: str
    status_code: int
    dropped: bool


@contextmanager
def serve_loopback(
    app: object,
    *,
    lifespan: str = "off",
    port: int = 0,
) -> Iterator[str]:
    """在预绑定随机端口上运行真实 Uvicorn，并保证退出时回收线程。"""

    listening = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listening.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listening.bind(("127.0.0.1", port))
    listening.listen()
    port = int(listening.getsockname()[1])
    server = Server(
        Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan=lifespan,
        ),
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listening]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        listening.close()
        raise RuntimeError("loopback ASGI server did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listening.close()
        if thread.is_alive():
            raise RuntimeError("loopback ASGI server did not stop")


def unused_loopback_port() -> int:
    """预留并释放一个回环端口，用于先拒绝连接再启动服务的测试。"""

    reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])
    finally:
        reservation.close()


def _read_http_request(connection: socket.socket) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(65_536)
        if not chunk:
            return bytes(data)
        data.extend(chunk)
    header_end = data.index(b"\r\n\r\n")
    headers = bytes(data[:header_end]).split(b"\r\n")
    content_length = 0
    for header in headers[1:]:
        name, _, value = header.partition(b":")
        if name.lower() == b"content-length":
            content_length = int(value.strip())
            break
    expected = header_end + 4 + content_length
    while len(data) < expected:
        chunk = connection.recv(expected - len(data))
        if not chunk:
            break
        data.extend(chunk)
    forwarded_headers = [header for header in headers if not header.lower().startswith(b"connection:")]
    forwarded_headers.append(b"Connection: close")
    return b"\r\n".join(forwarded_headers) + b"\r\n\r\n" + bytes(data[header_end + 4 : expected])


@contextmanager
def serve_single_response_loss_proxy(
    upstream_url: str,
    *,
    drop_request: tuple[str, str] | None = None,
    responses: list[ForwardedHttpResponse] | None = None,
) -> Iterator[tuple[str, threading.Event]]:
    """经真实 TCP 转发生产 API；指定请求首个成功响应在客户端边界丢失。"""

    parsed = urlsplit(upstream_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise ValueError("response-loss proxy only accepts an explicit loopback HTTP upstream")
    listening = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listening.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listening.bind(("127.0.0.1", 0))
    listening.listen()
    listening.settimeout(0.1)
    port = int(listening.getsockname()[1])
    stopped = threading.Event()
    response_dropped = threading.Event()
    failures: list[str] = []

    def forward() -> None:
        while not stopped.is_set():
            try:
                client, _ = listening.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with client:
                client.settimeout(5)
                try:
                    request = _read_http_request(client)
                    if not request:
                        continue
                    with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as upstream:
                        upstream.sendall(request)
                        response = bytearray()
                        while chunk := upstream.recv(65_536):
                            response.extend(chunk)
                    method, target, _protocol = request.split(b"\r\n", 1)[0].decode("ascii").split(" ")
                    path = urlsplit(target).path
                    status_code = int(response.split(b"\r\n", 1)[0].split(b" ")[1])
                    matches = drop_request is None or drop_request == (method, path)
                    dropped = matches and 200 <= status_code < 300 and not response_dropped.is_set()
                    if responses is not None:
                        responses.append(ForwardedHttpResponse(method, path, status_code, dropped))
                    if dropped:
                        response_dropped.set()
                        continue
                    client.sendall(response)
                except (OSError, ValueError) as error:
                    failures.append(type(error).__name__)

    thread = threading.Thread(target=forward, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", response_dropped
    finally:
        stopped.set()
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
        except OSError:
            pass
        thread.join(timeout=5)
        listening.close()
        if thread.is_alive():
            raise RuntimeError("response-loss proxy did not stop")
        if failures:
            raise RuntimeError(f"response-loss proxy failed: {failures}")
