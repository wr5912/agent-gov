from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from app.openapi_contract import REMOVED_RUNTIME_PATHS
from app.sse_contracts import RUNTIME_STREAM_PATH, runtime_sse_contract
from scripts.audit_openapi_contract import REQUIRED_RUNTIME_OPERATIONS, audit_live_matches_local, audit_schema
from scripts.export_openapi import build_openapi_schema


def test_build_schema_preserves_parent_environment_and_working_directory(monkeypatch) -> None:
    monkeypatch.setenv("RUNTIME_CONTAINER", "1")
    monkeypatch.setenv("HOST_RUNTIME_VOLUME_ROOT", "/unusable-deployment-volume")
    before_env, before_cwd = dict(os.environ), Path.cwd()

    schema = build_openapi_schema()

    assert schema["openapi"].startswith("3.1.")
    assert dict(os.environ) == before_env
    assert Path.cwd() == before_cwd


def test_export_script_writes_current_schema(tmp_path: Path) -> None:
    output_path = tmp_path / "openapi.json"
    env = os.environ.copy()
    env["HOST_RUNTIME_VOLUME_ROOT"] = str(tmp_path / "runtime")

    subprocess.run(
        [sys.executable, "scripts/export_openapi.py", "--output", str(output_path)],
        check=True,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
    )

    schema = json.loads(output_path.read_text(encoding="utf-8"))
    assert not (tmp_path / "runtime").exists()
    assert schema["openapi"].startswith("3.1.")
    operations = {(path, method) for path, path_item in schema["paths"].items() for method in path_item if method in {"get", "post", "put", "patch", "delete"}}
    assert operations >= REQUIRED_RUNTIME_OPERATIONS
    assert set(schema["paths"]).isdisjoint(REMOVED_RUNTIME_PATHS)


@pytest.mark.parametrize("container_marker", ["0", "1"])
def test_fresh_export_ignores_deployment_paths_private_env_and_preloaded_app(tmp_path: Path, container_marker: str) -> None:
    project_root = Path(__file__).resolve().parents[1]
    selected_volume = tmp_path / "existing-volume"
    selected_volume.mkdir()
    sentinel = selected_volume / "runtime.sqlite3"
    sentinel.write_bytes(b"existing-runtime-must-not-be-read-or-written")
    before = (sentinel.stat().st_mtime_ns, sentinel.read_bytes())
    polluted = _polluted_export_environment(selected_volume, container_marker)
    (tmp_path / "docker").mkdir()
    private_env = "\n".join(f"{key}={value}" for key, value in polluted.items()) + "\n"
    for name in (".env", ".env.local-debug"):
        (tmp_path / "docker" / name).write_text(private_env, encoding="utf-8")
    env = {**os.environ, **polluted, "PYTHONPATH": str(project_root)}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, os, sys, types; from pathlib import Path; "
            "from scripts.export_openapi import build_openapi_schema; "
            "sys.modules['app.main'] = types.ModuleType('app.main'); "
            "before = dict(os.environ); cwd = Path.cwd(); schema = build_openapi_schema(); "
            "assert dict(os.environ) == before and Path.cwd() == cwd; "
            "print(json.dumps({'openapi': schema['openapi'], 'paths': sorted(schema['paths'])}))",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )

    schema = json.loads(result.stdout)
    assert schema["openapi"].startswith("3.1.")
    assert {path for path, _method in REQUIRED_RUNTIME_OPERATIONS} <= set(schema["paths"])
    assert sorted(selected_volume.iterdir()) == [sentinel]
    assert (sentinel.stat().st_mtime_ns, sentinel.read_bytes()) == before


def _polluted_export_environment(selected_volume: Path, container_marker: str) -> dict[str, str]:
    path_keys = (
        "HOST_RUNTIME_VOLUME_ROOT",
        "HOST_DATA_MOUNT",
        "HOST_GOVERNOR_WORKSPACE_MOUNT",
        "DATA_DIR",
        "GOVERNOR_WORKSPACE_DIR",
        "RUNTIME_CANDIDATES_DIR",
        "AGENT_GIT_REPOSITORY_DIR",
        "AGENT_GIT_WORKTREES_DIR",
        "AGENT_RELEASE_ARCHIVES_DIR",
        "AGENTGOV_API_GATE_STATE_FILE",
    )
    return {
        **{key: str(selected_volume) for key in path_keys},
        "RUNTIME_CONTAINER": container_marker,
        "RUNTIME_VOLUME_MODE": "container",
        "COMPOSE_ENV_FILE": str(selected_volume / "private.env"),
        "TMPDIR": str(selected_volume),
        "AGENTSCOPE_MODEL_PARAMETERS_JSON": "not-valid-json",
        "AGENTGOV_API_MODE": "acceptance",
        "AGENTGOV_ACCEPTANCE_IDENTITY": "",
        "AGENTGOV_ACCEPTANCE_API_KEY": "",
        "AGENTGOV_RUNTIME_SHARED_SECRET": "invalid-short",
        "LANGFUSE_ENABLED": "true",
    }


def test_current_schema_passes_minimal_agent_scope_audit() -> None:
    schema = dict(build_openapi_schema())
    expected_version = Path("VERSION").read_text(encoding="utf-8").strip()
    assert audit_schema(schema, expected_version=expected_version) == []


@pytest.mark.parametrize(("path", "method"), sorted(REQUIRED_RUNTIME_OPERATIONS))
def test_audit_rejects_each_missing_required_runtime_operation(path: str, method: str) -> None:
    schema = copy.deepcopy(dict(build_openapi_schema()))
    del schema["paths"][path][method]

    issues = audit_schema(schema)

    assert f"missing required AgentScope gateway operation {method.upper()} {path}" in issues


def test_runtime_stream_documents_native_byte_passthrough() -> None:
    schema = build_openapi_schema()
    operation = schema["paths"][RUNTIME_STREAM_PATH]["get"]
    content = operation["responses"]["200"]["content"]

    assert set(content) == {"text/event-stream"}
    assert operation["x-agentgov-sse-contract"] == runtime_sse_contract()
    assert operation["x-agentgov-sse-contract"]["unknown_events"] == "pass-through"
    assert operation["x-agentgov-sse-contract"]["terminal_event"] == "REPLY_END"


def test_audit_rejects_removed_compatibility_path() -> None:
    schema = dict(build_openapi_schema())
    schema = copy.deepcopy(schema)
    schema["paths"]["/api/chat"] = {
        "post": {
            "security": [{"HTTPBearer": []}],
            "responses": {"200": {"description": "stale"}},
        }
    }

    issues = audit_schema(schema)
    assert any("removed compatibility path is still exposed: /api/chat" in issue for issue in issues)


def test_live_local_comparison_ignores_deployment_metadata_only() -> None:
    local = {"openapi": "3.1.0", "paths": {}, "servers": [{"url": "http://local"}]}
    live = {"openapi": "3.1.0", "paths": {}, "servers": [{"url": "http://live"}], "x-deployment-name": "prod"}
    assert audit_live_matches_local(live, local) == []

    changed = copy.deepcopy(live)
    changed["paths"]["/health"] = {"get": {}}
    assert audit_live_matches_local(changed, local)
