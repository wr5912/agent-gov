"""仅验证真实宿主机 API/Vite 生命周期；不代替 Runtime 或业务对话验收。"""

from __future__ import annotations

import json
import os
import secrets
import selectors
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import pytest
from scripts.run_local_debug import bind_api_socket

ROOT = Path(__file__).resolve().parents[1]


def isolated_environment(root: Path) -> dict[str, str]:
    # 不继承调用方配置，临时 cwd 没有 docker/.env*；只提供真实应用必需的隔离参数。
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "RUNTIME_CONTAINER": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOST_RUNTIME_VOLUME_ROOT": str(root),
        "DATA_DIR": str(root / "data"),
        "GOVERNOR_WORKSPACE_DIR": str(root / "governor-workspace"),
        "RUNTIME_CANDIDATES_DIR": str(root / "candidates"),
        "API_KEY": secrets.token_hex(32),
        "AGENTGOV_RUNTIME_SHARED_SECRET": secrets.token_hex(32),
        "AGENTSCOPE_RUNTIME_URL": "http://127.0.0.1:1",
        "LANGFUSE_ENABLED": "false",
        "LOG_LEVEL": "error",
        "VITE_RUNTIME_API_BASE": "http://127.0.0.1:2",
        "VITE_DEV_PROXY_TARGET": "http://127.0.0.1:3",
    }


@contextmanager
def launcher(root: Path, *arguments: str) -> Iterator[subprocess.Popen[str]]:
    root.mkdir()
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts/run_local_debug.py"), *arguments],
        cwd=root,
        env=isolated_environment(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            pytest.fail("本机入口未能在期限内关闭自身子进程")
        if process.stdout is not None:
            process.stdout.close()


def ready_receipt(process: subprocess.Popen[str]) -> dict[str, object]:
    assert process.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(60), "本机 API/Vite 未在期限内报告实际地址"
        line = process.stdout.readline()
    assert line, "本机入口未完成启动，未报告伪成功地址"
    value = json.loads(line)
    assert value["event"] == "local_debug_ready"
    assert value["mode"] == "local-debug" and value["runtime"] == "external"
    assert process.poll() is None
    return value


def read_url(url: str) -> tuple[int, str]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=10) as response:
        return response.status, response.read().decode("utf-8")


def assert_listener_closed(base: str) -> None:
    parsed = urlparse(base)
    with socket.socket() as client:
        client.settimeout(1)
        assert client.connect_ex(("127.0.0.1", parsed.port)) != 0


def test_auto_ports_reach_real_api_and_vite_with_one_api_address(tmp_path: Path) -> None:
    with launcher(tmp_path / "automatic") as process:
        receipt = ready_receipt(process)
        api_base, ui_base = str(receipt["api_base"]), str(receipt["ui_base"])
        assert api_base != ui_base
        assert urlparse(api_base).hostname == urlparse(ui_base).hostname == "127.0.0.1"
        direct_status, direct_body = read_url(f"{api_base}/health/live")
        proxy_status, proxy_body = read_url(f"{ui_base}/health/live")
        assert direct_status == proxy_status == 200
        assert json.loads(direct_body) == json.loads(proxy_body)
        _, health_body = read_url(f"{api_base}/health")
        assert json.loads(health_body)["api_port"] == urlparse(api_base).port
        assert read_url(ui_base)[0] == 200
        _, request_module = read_url(f"{ui_base}/src/api/request.ts")
        assert f'"VITE_RUNTIME_API_BASE": "{api_base}"' in request_module
        assert f'"VITE_DEV_PROXY_TARGET": "{api_base}"' in request_module
        assert set(receipt) == {"event", "api_base", "ui_base", "api_pid", "ui_pid", "mode", "runtime"}
        with launcher(tmp_path / "concurrent") as concurrent:
            other = ready_receipt(concurrent)
            assert other["api_base"] != api_base and other["ui_base"] != ui_base
            assert read_url(f"{other['ui_base']}/health/live")[0] == 200
        assert read_url(f"{ui_base}/health/live")[0] == 200
    assert_listener_closed(api_base)
    assert_listener_closed(ui_base)


@pytest.mark.parametrize("option", ["--api-port", "--ui-port"])
def test_explicit_occupied_port_fails_without_creating_api_data(tmp_path: Path, option: str) -> None:
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        root = tmp_path / "occupied"
        with launcher(root, option, str(occupied.getsockname()[1])) as process:
            assert process.wait(timeout=20) == 1
            assert process.stdout is not None and process.stdout.read() == ""
            assert not (root / "data").exists()
        assert occupied.getsockname()[1] > 0


def test_api_fd_reservation_cannot_be_stolen() -> None:
    with bind_api_socket(0) as reserved:
        with socket.socket() as competitor:
            with pytest.raises(OSError):
                competitor.bind(reserved.getsockname())


def test_explicit_free_ports_are_used_exactly(tmp_path: Path) -> None:
    with socket.socket() as api, socket.socket() as ui:
        api.bind(("127.0.0.1", 0))
        ui.bind(("127.0.0.1", 0))
        api_port, ui_port = api.getsockname()[1], ui.getsockname()[1]
    with launcher(tmp_path / "explicit", "--api-port", str(api_port), "--ui-port", str(ui_port)) as process:
        receipt = ready_receipt(process)
        assert receipt["api_base"] == f"http://127.0.0.1:{api_port}"
        assert receipt["ui_base"] == f"http://127.0.0.1:{ui_port}"


def test_stopping_owned_vite_stops_api_and_fails_launcher(tmp_path: Path) -> None:
    with launcher(tmp_path / "child-exit") as process:
        receipt = ready_receipt(process)
        os.kill(int(receipt["ui_pid"]), signal.SIGTERM)
        assert process.wait(timeout=15) == 1
        assert_listener_closed(str(receipt["api_base"]))
        assert_listener_closed(str(receipt["ui_base"]))


def test_interrupt_during_startup_leaves_reserved_api_port_free(tmp_path: Path) -> None:
    with socket.socket() as address:
        address.bind(("127.0.0.1", 0))
        port = address.getsockname()[1]
    with launcher(tmp_path / "interrupt", "--api-port", str(port)) as process:
        # 等待入口确实持有预绑定 socket，再在 Vite/API 启动阶段发真实信号。
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with socket.socket() as competitor:
                try:
                    competitor.bind(("127.0.0.1", port))
                except OSError:
                    break
            assert process.poll() is None
            time.sleep(0.02)
        else:
            pytest.fail("入口没有取得 API socket")
        process.terminate()
        assert process.wait(timeout=15) == 130
    with socket.socket() as reclaimed:
        reclaimed.bind(("127.0.0.1", port))
