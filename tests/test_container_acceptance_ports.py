from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import run_container_acceptance as acceptance


def _available_acceptance_ports() -> tuple[int, ...]:
    available: list[int] = []
    for port in range(50400, 50500):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                continue
            available.append(port)
    return tuple(available)


def test_acceptance_port_range_matches_all_currently_available_ports() -> None:
    available = _available_acceptance_ports()
    if not available:
        pytest.skip("50400–50499 当前没有可用端口")

    ports = acceptance._allocate_loopback_ports(len(available))

    assert ports == available
    assert all(50400 <= port <= 50499 for port in ports)
    with pytest.raises(acceptance.AcceptanceError, match="50400–50499"):
        acceptance._allocate_loopback_ports(len(available) + 1)


def test_acceptance_ports_skip_a_real_occupied_port_without_duplicates() -> None:
    available = _available_acceptance_ports()
    if len(available) < 6:
        pytest.skip("50400–50499 当前不足 6 个可用端口")
    occupied_port = available[0]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", occupied_port))
        occupied.listen(1)

        ports = acceptance._allocate_loopback_ports(5)

    assert ports == available[1:6]
    assert occupied_port not in ports
    assert len(set(ports)) == 5


def test_acceptance_port_shortage_exits_without_starting_containers(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    listeners: list[socket.socket] = []
    try:
        for port in _available_acceptance_ports():
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", port))
            listener.listen(1)
            listeners.append(listener)
        source_env = tmp_path / "source.env"
        source_env.write_text("", encoding="utf-8")

        result = acceptance.main(
            [
                "--profile",
                "core",
                "--env-file",
                str(source_env),
                "--",
                "/usr/bin/make",
                "--no-print-directory",
                "_container-core-smoke",
            ]
        )
    finally:
        for listener in listeners:
            listener.close()

    assert result == 1
    assert "50400–50499" in capsys.readouterr().err


def test_non_address_in_use_socket_error_fails_closed_in_real_subprocess() -> None:
    code = """
import resource
from scripts import run_container_acceptance as acceptance

_, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (3, hard))
try:
    acceptance._allocate_loopback_ports(1)
except acceptance.AcceptanceError as exc:
    assert '无法创建' in str(exc)
    assert '50400–50499' in str(exc)
    raise SystemExit(0)
raise SystemExit(1)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=acceptance.REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_isolated_publish_ports_and_urls_share_real_selected_ports(tmp_path: Path) -> None:
    source_env = tmp_path / "source.env"
    source_env.write_text("HOST_PORT=58000\nFRONTEND_HOST_PORT=5173\nLANGFUSE_HOST_PORT=53000\n", encoding="utf-8")

    isolation = acceptance.prepare_isolated_environment(source_env, "1234-portcheck", tmp_path)
    child_env = acceptance.build_acceptance_env(
        acceptance.PROFILES["langfuse"],
        isolation,
        "1234-portcheck",
        {"HOST_PORT": "58000"},
    )

    selected_ports = {
        "HOST_PORT": int(isolation.overrides["HOST_PORT"]),
        "FRONTEND_HOST_PORT": int(isolation.overrides["FRONTEND_HOST_PORT"]),
        "LANGFUSE_HOST_PORT": int(isolation.overrides["LANGFUSE_HOST_PORT"]),
        "LANGFUSE_MINIO_HOST_PORT": int(isolation.overrides["LANGFUSE_MINIO_HOST_PORT"]),
        "LANGFUSE_MINIO_CONSOLE_HOST_PORT": int(isolation.overrides["LANGFUSE_MINIO_CONSOLE_HOST_PORT"]),
    }
    assert len(set(selected_ports.values())) == 5
    assert all(50400 <= port <= 50499 for port in selected_ports.values())
    generated_env = dict(line.split("=", 1) for line in isolation.env_file.read_text(encoding="utf-8").splitlines() if "=" in line)
    for key, port in selected_ports.items():
        assert child_env[key] == generated_env[key] == str(port)
    expected_urls = {
        "FRONTEND_RUNTIME_API_BASE": selected_ports["HOST_PORT"],
        "API_BASE": selected_ports["HOST_PORT"],
        "FRONTEND_URL": selected_ports["FRONTEND_HOST_PORT"],
        "LANGFUSE_NEXTAUTH_URL": selected_ports["LANGFUSE_HOST_PORT"],
        "FRONTEND_LANGFUSE_URL": selected_ports["LANGFUSE_HOST_PORT"],
        "LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT": selected_ports["LANGFUSE_MINIO_HOST_PORT"],
        "LANGFUSE_S3_BATCH_EXPORT_EXTERNAL_ENDPOINT": selected_ports["LANGFUSE_MINIO_HOST_PORT"],
    }
    for key, port in expected_urls.items():
        assert child_env[key] == generated_env[key] == f"http://127.0.0.1:{port}"
