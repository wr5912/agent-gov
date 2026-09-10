from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest
from scripts.initialize_langfuse_env import DEFAULTS, SECRET_KEYS, main, parse_env, render_env, update_env


def _private_env(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(f"HOST_RUNTIME_VOLUME_ROOT={tmp_path / 'volume'}\n", encoding="utf-8")
    return path


def test_initialization_generates_once_with_private_backup(tmp_path: Path) -> None:
    path = _private_env(tmp_path)
    original = path.read_bytes()

    assert set(update_env(path)) == set(SECRET_KEYS)
    _, values = parse_env(path.read_text())
    assert all(len(values[key]) >= 32 for key in SECRET_KEYS)
    assert values["LANGFUSE_PUBLIC_KEY"].startswith("pk-lf-")
    assert values["LANGFUSE_SECRET_KEY"].startswith("sk-lf-")
    assert len(bytes.fromhex(values["LANGFUSE_ENCRYPTION_KEY"])) == 32
    backup = next(tmp_path.glob(".env.bak-*"))
    assert backup.read_bytes() == original
    assert backup.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_mode & 0o777 == 0o600
    initialized = path.read_bytes()
    assert update_env(path) == []
    assert path.read_bytes() == initialized
    assert len(list(tmp_path.glob(".env.bak-*"))) == 1


def test_initialization_preserves_existing_values_and_unrelated_bytes(tmp_path: Path) -> None:
    path = _private_env(tmp_path)
    unrelated = "# 私有注释\nOTHER='unchanged value'\n"
    existing = "LANGFUSE_SALT='existing-salt' # keep exactly\n"
    with path.open("a") as stream:
        stream.write(unrelated + existing + "LANGFUSE_SECRET_KEY=replace-with-secret\n")

    changed = update_env(path)

    assert "LANGFUSE_SALT" not in changed
    assert "LANGFUSE_SECRET_KEY" in changed
    assert unrelated + existing in path.read_text()


@pytest.mark.parametrize("custom_mount", [False, True])
def test_existing_data_with_missing_credentials_fails_without_write(tmp_path: Path, custom_mount: bool) -> None:
    path = _private_env(tmp_path)
    data = tmp_path / ("custom-postgres" if custom_mount else "volume/langfuse/postgres")
    data.mkdir(parents=True)
    (data / "PG_VERSION").write_text("17")
    if custom_mount:
        with path.open("a") as stream:
            stream.write(f"LANGFUSE_POSTGRES_DATA_MOUNT={data}\n")
    original = path.read_bytes()

    with pytest.raises(ValueError, match="恢复原有凭据"):
        update_env(path)

    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".env.bak-*"))


def test_existing_data_and_complete_credentials_do_not_trigger_rotation(tmp_path: Path) -> None:
    path = _private_env(tmp_path)
    update_env(path)
    data = tmp_path / "volume/langfuse/postgres"
    data.mkdir(parents=True)
    (data / "PG_VERSION").write_text("17")

    assert update_env(path) == []


def test_storage_environment_override_blocks_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _private_env(tmp_path)
    monkeypatch.setenv("LANGFUSE_POSTGRES_DATA_MOUNT", str(tmp_path / "different-volume"))

    with pytest.raises(ValueError, match="宿主环境覆盖"):
        update_env(path)

    assert not list(tmp_path.glob(".env.bak-*"))


def test_indirect_storage_environment_override_blocks_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _private_env(tmp_path)
    with path.open("a") as stream:
        stream.write(f"DATA_ROOT={tmp_path / 'empty'}\nLANGFUSE_POSTGRES_DATA_MOUNT=${{DATA_ROOT}}\n")
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "existing-data"))

    with pytest.raises(ValueError, match="覆盖了存储路径引用"):
        update_env(path)

    assert not list(tmp_path.glob(".env.bak-*"))


def test_compact_drops_only_defaults_and_matching_derived_values(tmp_path: Path) -> None:
    path = _private_env(tmp_path)
    update_env(path)
    _, original_values = parse_env(path.read_text())
    auth = base64.b64encode(f"{original_values['LANGFUSE_PUBLIC_KEY']}:{original_values['LANGFUSE_SECRET_KEY']}".encode()).decode()
    extras = dict(DEFAULTS) | {
        "LANGFUSE_HOST_PORT": "50499",
        "LANGFUSE_NEXTAUTH_URL": "http://localhost:50499",
        "FRONTEND_LANGFUSE_URL": "http://localhost:50499",
        "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": original_values["LANGFUSE_PUBLIC_KEY"],
        "LANGFUSE_INIT_PROJECT_SECRET_KEY": original_values["LANGFUSE_SECRET_KEY"],
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://langfuse-web:3000/api/public/otel",
        "OTEL_EXPORTER_OTLP_HEADERS": f"Authorization=Basic {auth},x-langfuse-ingestion-version=4",
    }
    with path.open("a") as stream:
        stream.writelines(f"{key}={value}\n" for key, value in extras.items())

    removed = update_env(path, compact=True)
    _, compact_values = parse_env(path.read_text())

    assert set(removed) == set(extras) - {"LANGFUSE_HOST_PORT"}
    assert compact_values == original_values | {"LANGFUSE_HOST_PORT": "50499"}
    assert update_env(path, compact=True) == []


def test_compact_retains_custom_settings_and_does_not_generate(tmp_path: Path) -> None:
    original = (
        "LANGFUSE_INIT_PROJECT_ID=existing-project\nLANGFUSE_INIT_ORG_NAME=existing-org\n"
        "LANGFUSE_MINIO_ROOT_USER=existing-user\nLANGFUSE_BASE_URL=https://trace.example.test\n"
        "FRONTEND_LANGFUSE_URL=https://browser.example.test\n"
        "OTEL_EXPORTER_OTLP_ENDPOINT=http://collector.test\nOTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer custom\n"
    )

    assert render_env(original, compact=True) == (original, [])


def test_compact_rejects_ambiguous_project_identity_without_write(tmp_path: Path) -> None:
    path = _private_env(tmp_path)
    with path.open("a") as stream:
        stream.write("LANGFUSE_PUBLIC_KEY=one-project\nLANGFUSE_INIT_PROJECT_PUBLIC_KEY=another-project\n")
    original = path.read_bytes()

    with pytest.raises(ValueError, match="与主凭据不一致"):
        update_env(path, compact=True)

    assert path.read_bytes() == original


@pytest.mark.parametrize("content", ["LANGFUSE_SALT=a\nLANGFUSE_SALT=b\n", "LANGFUSE_SALT='unterminated\n"])
def test_ambiguous_env_is_rejected(content: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        render_env(content, compact=True)


def test_dry_run_leaves_private_file_and_backup_directory_unchanged(tmp_path: Path) -> None:
    path = _private_env(tmp_path)
    original = path.read_bytes()

    assert set(update_env(path, dry_run=True)) == set(SECRET_KEYS)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".env.bak-*"))


def test_cli_never_prints_values_or_private_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    path = _private_env(tmp_path)
    with path.open("a") as stream:
        stream.write("LANGFUSE_PUBLIC_KEY=private-value\nLANGFUSE_INIT_PROJECT_PUBLIC_KEY=different-private-value\n")
    monkeypatch.setattr(sys, "argv", ["initialize_langfuse_env.py", "--env-file", str(path), "--compact"])

    with pytest.raises(SystemExit, match="1"):
        main()
    output = capsys.readouterr()
    assert "private-value" not in output.err + output.out
    assert str(tmp_path) not in output.err + output.out


@pytest.mark.parametrize("link", [False, True])
def test_examples_and_symlinks_are_not_modified(tmp_path: Path, link: bool) -> None:
    path = tmp_path / ".env.example"
    path.write_text("LANGFUSE_ENABLED=true\n")
    if link:
        linked = tmp_path / ".env"
        linked.symlink_to(path)
        path = linked

    with pytest.raises(ValueError, match="普通私有 env"):
        update_env(path)


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "frontend.Dockerfile", "agentscope-runtime.Dockerfile"])
def test_private_env_and_backups_are_excluded_from_each_build_context(dockerfile: str) -> None:
    ignore_file = Path(__file__).resolve().parents[1] / "docker" / f"{dockerfile}.dockerignore"
    patterns = {line.strip() for line in ignore_file.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")}

    assert {"**/.env", "**/.env.local", "**/.env.local-debug", "**/.env.bak*"}.issubset(patterns)
    assert not any(pattern.startswith("!") for pattern in patterns), "不得重新将私有配置纳入构建上下文"
