from __future__ import annotations

import errno
import socket
from pathlib import Path
from unittest.mock import Mock

import pytest
from scripts import run_container_acceptance as acceptance


def _simulate_sockets(
    monkeypatch: pytest.MonkeyPatch,
    occupied: set[int],
    errors: dict[int, int] | None = None,
) -> tuple[list[tuple[str, int]], list[Mock]]:
    attempts: list[tuple[str, int]] = []
    listeners: list[Mock] = []

    def create_listener(family: int, kind: int) -> Mock:
        assert (family, kind) == (socket.AF_INET, socket.SOCK_STREAM)
        listener = Mock()

        def bind(address: tuple[str, int]) -> None:
            attempts.append(address)
            if address[1] in occupied:
                raise OSError(errno.EADDRINUSE, "address in use")
            if errors and address[1] in errors:
                raise OSError(errors[address[1]], "bind failed")

        listener.bind.side_effect = bind
        listeners.append(listener)
        return listener

    monkeypatch.setattr(acceptance.socket, "socket", create_listener)
    return attempts, listeners


def test_acceptance_port_range_includes_both_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts, listeners = _simulate_sockets(monkeypatch, set())

    ports = acceptance._allocate_loopback_ports(100)

    assert ports == tuple(range(50400, 50500))
    assert attempts == [("127.0.0.1", port) for port in ports]
    for listener in listeners:
        listener.close.assert_called_once()


def test_acceptance_ports_skip_occupied_ports_without_duplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    _, listeners = _simulate_sockets(monkeypatch, {50400, 50402, 50404})

    ports = acceptance._allocate_loopback_ports(5)

    assert ports == (50401, 50403, 50405, 50406, 50407)
    assert len(set(ports)) == 5
    for listener in listeners:
        listener.close.assert_called_once()


def test_acceptance_port_shortage_exits_without_starting_containers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    attempts, listeners = _simulate_sockets(monkeypatch, set(range(50400, 50496)))
    source_env = tmp_path / "source.env"
    source_env.write_text("", encoding="utf-8")
    monkeypatch.setattr(acceptance, "LOCK_FILE", tmp_path / "acceptance.lock")
    monkeypatch.setattr(acceptance, "source_fingerprint", lambda _path: "stable")
    bootstrap = Mock()
    refresh = Mock()
    monkeypatch.setattr(acceptance, "_bootstrap_isolated_runtime", bootstrap)
    monkeypatch.setattr(acceptance, "refresh_profile", refresh)

    result = acceptance.main(["--profile", "core", "--env-file", str(source_env), "--", "true"])

    assert result == 1
    assert "50400–50499" in capsys.readouterr().err
    assert attempts == [("127.0.0.1", port) for port in range(50400, 50500)]
    bootstrap.assert_not_called()
    refresh.assert_not_called()
    for listener in listeners:
        listener.close.assert_called_once()


def test_acceptance_bind_error_is_not_treated_as_an_occupied_port(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts, listeners = _simulate_sockets(monkeypatch, set(), {50402: errno.EACCES})

    with pytest.raises(acceptance.AcceptanceError, match="无法检查"):
        acceptance._allocate_loopback_ports(5)

    assert attempts == [("127.0.0.1", port) for port in range(50400, 50403)]
    for listener in listeners:
        listener.close.assert_called_once()


def test_isolated_publish_ports_and_urls_share_the_selected_range(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _simulate_sockets(monkeypatch, set(range(50400, 50495)))
    source_env = tmp_path / "source.env"
    source_env.write_text("HOST_PORT=58000\nFRONTEND_HOST_PORT=5173\nLANGFUSE_HOST_PORT=53000\n", encoding="utf-8")

    isolation = acceptance.prepare_isolated_environment(source_env, "1234-portcheck", tmp_path)
    child_env = acceptance.build_acceptance_env(acceptance.PROFILES["langfuse"], isolation, "1234-portcheck", {"HOST_PORT": "58000"})

    expected_ports = {
        "HOST_PORT": 50495,
        "FRONTEND_HOST_PORT": 50496,
        "LANGFUSE_HOST_PORT": 50497,
        "LANGFUSE_MINIO_HOST_PORT": 50498,
        "LANGFUSE_MINIO_CONSOLE_HOST_PORT": 50499,
    }
    generated_env = dict(line.split("=", 1) for line in isolation.env_file.read_text(encoding="utf-8").splitlines() if "=" in line)
    for key, port in expected_ports.items():
        assert child_env[key] == generated_env[key] == str(port)
    expected_urls = {
        "FRONTEND_RUNTIME_API_BASE": 50495,
        "API_BASE": 50495,
        "FRONTEND_URL": 50496,
        "LANGFUSE_NEXTAUTH_URL": 50497,
        "FRONTEND_LANGFUSE_URL": 50497,
        "LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT": 50498,
        "LANGFUSE_S3_BATCH_EXPORT_EXTERNAL_ENDPOINT": 50498,
    }
    for key, port in expected_urls.items():
        assert child_env[key] == generated_env[key] == f"http://127.0.0.1:{port}"
