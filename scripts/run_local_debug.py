"""前台启动本机 API 与 Vite；不启动 Runtime，不更改所选配置文件。"""

from __future__ import annotations

import argparse
import json
import os
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path
from threading import Event

ROOT = Path(__file__).resolve().parents[1]
LOOPBACK = "127.0.0.1"
STARTUP_TIMEOUT = 45.0


class LocalDebugError(Exception):
    """只携带可公开的启动阶段，不转发私有配置或子进程原始错误。"""


def port_number(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("端口必须为 0–65535 的整数，0 表示自动选择") from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须为 0–65535 的整数，0 表示自动选择")
    return port


def bind_api_socket(port: int) -> socket.socket:
    """保留真实 socket 到 Uvicorn 接管，避免先探测再释放的端口竞争。"""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind((LOOPBACK, port))
    except OSError as exc:
        listener.close()
        raise LocalDebugError("api_port_unavailable") from exc
    return listener


def start_vite(api_base: str, port: int) -> subprocess.Popen[str]:
    node = shutil.which("node")
    if node is None:
        raise LocalDebugError("node_unavailable")
    environment = dict(os.environ)
    environment.update(VITE_RUNTIME_API_BASE=api_base, VITE_DEV_PROXY_TARGET=api_base)
    return subprocess.Popen(
        [node, str(ROOT / "frontend/scripts/run_local_debug_vite.mjs"), str(port)],
        cwd=ROOT / "frontend",
        env=environment,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def check_interrupted(stop: Event) -> None:
    if stop.is_set():
        raise KeyboardInterrupt


def vite_address(process: subprocess.Popen[str], api_base: str, stop: Event) -> str:
    assert process.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while not selector.select(0.1):
            check_interrupted(stop)
            if time.monotonic() >= deadline:
                raise LocalDebugError("vite_startup_timeout")
        check_interrupted(stop)
        try:
            receipt = json.loads(process.stdout.readline(8192))
        except (ValueError, OSError) as exc:
            raise LocalDebugError("vite_startup_failed") from exc
    if not isinstance(receipt, dict) or receipt.get("event") != "local_debug_vite_ready" or receipt.get("api_base") != api_base:
        raise LocalDebugError("vite_receipt_invalid")
    ui_base = receipt.get("ui_base")
    if not isinstance(ui_base, str) or not ui_base.startswith(f"http://{LOOPBACK}:"):
        raise LocalDebugError("vite_receipt_invalid")
    return ui_base


def start_api(listener: socket.socket) -> subprocess.Popen[str]:
    port = listener.getsockname()[1]
    environment = dict(os.environ)
    environment.update(API_HOST=LOOPBACK, API_PORT=str(port))
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", str(ROOT), "--fd", str(listener.fileno()), "--no-access-log"],
        env=environment,
        stdout=sys.stderr,
        text=True,
        pass_fds=(listener.fileno(),),
        start_new_session=True,
    )


def wait_api_liveness(api: subprocess.Popen[str], ui: subprocess.Popen[str], api_base: str, stop: Event) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        check_interrupted(stop)
        if api.poll() is not None or ui.poll() is not None:
            raise LocalDebugError("local_debug_child_exited")
        try:
            with opener.open(f"{api_base}/health/live", timeout=0.5) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.1)
    raise LocalDebugError("api_liveness_timeout")


def stop_children(children: list[subprocess.Popen[str]]) -> None:
    for child in reversed(children):
        if child.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGTERM)
    for child in reversed(children):
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()


def run_local_debug(api_port: int, ui_port: int, stop: Event) -> int:
    children: list[subprocess.Popen[str]] = []
    try:
        with bind_api_socket(api_port) as listener:
            api_base = f"http://{LOOPBACK}:{listener.getsockname()[1]}"
            ui = start_vite(api_base, ui_port)
            children.append(ui)
            ui_base = vite_address(ui, api_base, stop)
            api = start_api(listener)
            children.append(api)
        wait_api_liveness(api, ui, api_base, stop)
        check_interrupted(stop)
        print(
            json.dumps(
                {
                    "event": "local_debug_ready",
                    "api_base": api_base,
                    "ui_base": ui_base,
                    "api_pid": api.pid,
                    "ui_pid": ui.pid,
                    "mode": "local-debug",
                    "runtime": "external",
                }
            ),
            flush=True,
        )
        while all(child.poll() is None for child in children):
            check_interrupted(stop)
            stop.wait(0.2)
        raise LocalDebugError("local_debug_child_exited")
    finally:
        stop_children(children)


def main() -> int:
    parser = argparse.ArgumentParser(description="前台启动本机 API/Vite；AgentScope Runtime 必须单独准备。Ctrl+C 停止本轮子进程。")
    parser.add_argument("--api-port", type=port_number, default=0, help="API 端口；默认 0 由操作系统分配，显式端口占用即失败")
    parser.add_argument("--ui-port", type=port_number, default=0, help="Vite 端口；默认 0 自动选择可用端口，显式端口占用即失败")
    args = parser.parse_args()
    # 此入口只选择本机模式，不把容器部署误当作本机调试。
    sys.path.insert(0, str(ROOT))
    from app.runtime.settings import running_in_container

    if running_in_container():
        print("local_debug_requires_host_environment", file=sys.stderr)
        return 1

    stop = Event()

    def interrupt(_signum: int, _frame: object) -> None:
        # 信号只标记退出；不能打断 Popen 返回到登记 child 之间的所有权交接。
        stop.set()

    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    try:
        return run_local_debug(args.api_port, args.ui_port, stop)
    except KeyboardInterrupt:
        return 130
    except (LocalDebugError, OSError) as exc:
        code = str(exc) if isinstance(exc, LocalDebugError) else "local_debug_process_start_failed"
        print(f"local_debug_failed: {code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
