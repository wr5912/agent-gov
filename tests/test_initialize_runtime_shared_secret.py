from __future__ import annotations

import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from scripts.agentscope_atomic_cutover_env import parse_selected_env_bindings, read_stable_env_file
from scripts.container_acceptance_environment import prepare_isolated_environment
from scripts.container_acceptance_inputs import AcceptanceError
from scripts.initialize_runtime_shared_secret import (
    SECRET_KEY,
    _replace_atomically,
    initialize_before_operation,
    initialize_shared_secret,
)
from scripts.selected_env_operation_contract import DEPLOYED_BROWSER_OPERATIONS

ROOT = Path(__file__).resolve().parents[1]


def _env(tmp_path: Path, key: str | None = None) -> Path:
    path = tmp_path / "selected.env"
    content = f"HOST_RUNTIME_VOLUME_ROOT={tmp_path / 'runtime'}\n# 保留私有配置\nUNRELATED='unchanged=value'\n"
    if key is not None:
        content += f"{SECRET_KEY}={key}\n"
    path.write_text(content, encoding="utf-8")
    return path


def _key(env_file: Path) -> str:
    return next(binding.value or "" for binding in parse_selected_env_bindings(env_file) if binding.key == SECRET_KEY)


@pytest.mark.parametrize("initial", [None, "", "replace-with-at-least-32-random-characters", "local-dev-insecure-change-me-32"])
def test_initialization_writes_one_private_random_key_and_preserves_other_values(tmp_path, initial) -> None:
    env_file = _env(tmp_path, initial)
    original = env_file.read_text(encoding="utf-8").split(f"{SECRET_KEY}=", 1)[0]

    assert initialize_shared_secret(env_file)

    assert re.fullmatch(r"[a-f0-9]{64}", _key(env_file))
    assert env_file.read_text(encoding="utf-8").startswith(original)
    assert env_file.stat().st_mode & 0o777 == 0o600
    before = (env_file.read_bytes(), env_file.stat().st_ino, env_file.stat().st_mtime_ns)
    assert not initialize_shared_secret(env_file)
    assert before == (env_file.read_bytes(), env_file.stat().st_ino, env_file.stat().st_mtime_ns)


def test_existing_valid_secret_is_preserved_byte_for_byte_even_with_runtime_data(tmp_path) -> None:
    env_file = _env(tmp_path, "'existing-private-runtime-secret' # keep formatting")
    database = tmp_path / "runtime/data/runtime.sqlite3"
    database.parent.mkdir(parents=True)
    database.touch()
    original = env_file.read_bytes()

    assert not initialize_shared_secret(env_file)
    assert env_file.read_bytes() == original


@pytest.mark.parametrize("relative", ["data/runtime.sqlite3", "agentscope-runtime/data/agentscope.db", "agentscope-runtime/workspaces/session/state.json"])
def test_lost_secret_with_actual_runtime_state_is_not_silently_regenerated(tmp_path, relative) -> None:
    env_file = _env(tmp_path)
    artifact = tmp_path / "runtime" / relative
    artifact.parent.mkdir(parents=True)
    artifact.write_text("existing runtime state\n", encoding="utf-8")
    original = env_file.read_bytes()

    with pytest.raises(ValueError, match="恢复原共享密钥"):
        initialize_shared_secret(env_file)

    assert env_file.read_bytes() == original
    assert artifact.read_text(encoding="utf-8") == "existing runtime state\n"


def test_empty_bootstrap_directories_do_not_prevent_first_initialization(tmp_path) -> None:
    env_file = _env(tmp_path)
    (tmp_path / "runtime/agentscope-runtime/data/empty").mkdir(parents=True)
    (tmp_path / "runtime/agentscope-runtime/workspaces").mkdir(parents=True)
    assert initialize_shared_secret(env_file)


def test_explicit_native_data_mount_is_checked_before_initialization(tmp_path) -> None:
    env_file = _env(tmp_path)
    native = tmp_path / "native-data"
    native.mkdir()
    (native / "agentscope.db").touch()
    env_file.write_text(env_file.read_text() + f"HOST_AGENTSCOPE_RUNTIME_DATA_MOUNT={native}\n")
    with pytest.raises(ValueError, match="恢复原共享密钥"):
        initialize_shared_secret(env_file)


@pytest.mark.parametrize("suffix", [".example", ".symlink"])
def test_initializer_rejects_examples_and_symbolic_links_without_mutation(tmp_path, suffix) -> None:
    env_file = _env(tmp_path)
    target = tmp_path / ("private.env" + suffix)
    if suffix == ".symlink":
        target.symlink_to(env_file)
    else:
        target.write_bytes(env_file.read_bytes())
    original = env_file.read_bytes()
    with pytest.raises(ValueError):
        initialize_shared_secret(target)
    assert env_file.read_bytes() == original


def test_initializer_does_not_overwrite_an_invalid_existing_private_value(tmp_path) -> None:
    env_file = _env(tmp_path, "short")
    original = env_file.read_bytes()
    with pytest.raises(ValueError, match="长度无效"):
        initialize_shared_secret(env_file)
    assert env_file.read_bytes() == original


def test_atomic_replace_rejects_a_concurrent_user_edit(tmp_path) -> None:
    env_file = _env(tmp_path)
    original, identity = read_stable_env_file(env_file, error_type=ValueError)
    user_updated = original + b"USER_CHANGE=preserve\n"
    env_file.write_bytes(user_updated)
    with pytest.raises(ValueError, match="发生变化"):
        _replace_atomically(env_file, original, identity, original + b"MUST_NOT_WRITE=1\n")
    assert env_file.read_bytes() == user_updated


def test_concurrent_real_initializer_processes_keep_one_identity_without_printing_it(tmp_path) -> None:
    env_file = _env(tmp_path)

    def initialize() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts/initialize_runtime_shared_secret.py"), "--env-file", str(env_file)],
            env={"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            check=False,
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: initialize(), range(3)))

    assert all(result.returncode == 0 for result in results)
    assert sum("已首次初始化" in result.stdout for result in results) == 1
    value = _key(env_file)
    assert re.fullmatch(r"[a-f0-9]{64}", value)
    assert all(value not in result.stdout + result.stderr for result in results)


@pytest.mark.parametrize("operation", ["check", "logs", "down", "build", "runtime-clean"])
def test_read_only_or_unrelated_operations_never_initialize_env(tmp_path, operation) -> None:
    absent = tmp_path / "absent.env"
    initialize_before_operation(absent, operation)
    assert not absent.exists()


@pytest.mark.parametrize("operation", ("up", *DEPLOYED_BROWSER_OPERATIONS))
def test_start_operation_initializes_the_selected_file_before_its_snapshot_is_read(tmp_path, operation: str) -> None:
    env_file = _env(tmp_path)
    initialize_before_operation(env_file, operation)
    payload, identity = read_stable_env_file(env_file, error_type=ValueError)
    assert _key(env_file).encode() in payload
    assert identity


@pytest.mark.parametrize("initial", [None, "", "replace-with-at-least-32-random-characters", "local-dev-insecure-change-me-32"])
def test_isolated_acceptance_initializes_only_derived_env_before_freezing(tmp_path, initial) -> None:
    source = _env(tmp_path, initial)
    original = source.read_bytes()
    # 所选来源可以属于已有实例；仅派生的空隔离卷允许生成本轮身份。
    database = tmp_path / "runtime/data/runtime.sqlite3"
    database.parent.mkdir(parents=True)
    database.touch()

    first = prepare_isolated_environment(source, "1234-first", tmp_path / "first")
    second = prepare_isolated_environment(source, "1234-second", tmp_path / "second")

    assert re.fullmatch(r"[a-f0-9]{64}", _key(first.env_file))
    assert re.fullmatch(r"[a-f0-9]{64}", _key(second.env_file))
    assert _key(first.env_file) != _key(second.env_file)
    assert source.read_bytes() == original
    assert database.exists()
    for isolated in (first, second):
        assert isolated.env_file.stat().st_mode & 0o777 == 0o600
        assert sum(binding.key == SECRET_KEY for binding in parse_selected_env_bindings(isolated.env_file)) == 1
        before_freeze = read_stable_env_file(isolated.env_file, error_type=AcceptanceError)
        assert not initialize_shared_secret(isolated.env_file)
        assert read_stable_env_file(isolated.env_file, error_type=AcceptanceError) == before_freeze


def test_isolated_acceptance_preserves_existing_selected_secret_without_reformatting(tmp_path) -> None:
    source = _env(tmp_path, "'existing-private-runtime-secret' # preserve")
    original = source.read_bytes()

    isolated = prepare_isolated_environment(source, "1234-preserve", tmp_path / "isolated")

    assert source.read_bytes() == original
    assert _key(isolated.env_file) == _key(source)
    assert f"{SECRET_KEY}='existing-private-runtime-secret' # preserve\n" in isolated.env_file.read_text(encoding="utf-8")


def test_isolated_acceptance_rejects_invalid_selected_secret_with_a_safe_error(tmp_path) -> None:
    source = _env(tmp_path, "short-private")
    original = source.read_bytes()

    with pytest.raises(AcceptanceError) as failure:
        prepare_isolated_environment(source, "1234-invalid", tmp_path / "isolated")

    assert str(failure.value) == "ISOLATED_RUNTIME_SHARED_SECRET_INITIALIZATION_FAILED"
    assert source.read_bytes() == original
