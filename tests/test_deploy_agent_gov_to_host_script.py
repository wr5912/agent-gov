from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "deploy_agent_gov_to_host"
CUTOVER_SCRIPT = REPO_ROOT / "scripts" / "agentscope_atomic_cutover.py"
CUTOVER_SUPPORT = REPO_ROOT / "scripts" / "agentscope_atomic_cutover_support.py"
CUTOVER_TYPES = REPO_ROOT / "scripts" / "agentscope_atomic_cutover_types.py"
CUTOVER_EVIDENCE = REPO_ROOT / "scripts" / "agentscope_atomic_cutover_evidence.py"
CUTOVER_RECOVERY = REPO_ROOT / "scripts" / "agentscope_atomic_cutover_recovery.py"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _load_cutover() -> ModuleType:
    module_name = "_agentgov_atomic_cutover_test"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, CUTOVER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _write_bound_machine_evidence(cutover: ModuleType, root: Path, manifest: dict[str, object]) -> Path:
    evidence_module = importlib.import_module("scripts.agentscope_atomic_cutover_evidence")
    acceptance = manifest["acceptance_artifacts"]
    assert isinstance(acceptance, dict)
    image_ids = acceptance["image_ids"]
    assert isinstance(image_ids, dict)
    binding = cutover._evidence_binding_sha256(acceptance)
    results = {
        "static_gates": {"checks": list(evidence_module.STATIC_CHECKS), "failure_count": 0},
        "contract_tests": {
            "contracts": list(evidence_module.CONTRACTS),
            "passed": len(evidence_module.CONTRACTS),
            "failed": 0,
            "skipped": 0,
        },
        "container_acceptance": {
            "profile": "core",
            "services": list(evidence_module.CORE_SERVICES),
            "fresh_build": True,
            "force_recreate": True,
            "image_ids": image_ids,
            "failure_count": 0,
        },
        "browser_acceptance": {
            "consecutive_passes": 3,
            "mock_sse": False,
            "workflows": list(evidence_module.BROWSER_WORKFLOWS),
            "failure_count": 0,
            "artifact_set_sha256": "c" * 64,
        },
        "live_runtime": {
            "total_runs": 50,
            "distinct_inputs": 50,
            "max_concurrency": 10,
            "identity_link_percent": 100.0,
            "feedback_matches": 10,
            "cross_scope_authorizations": 0,
            "soak_seconds": 7200,
            "unexpected_restarts": 0,
            "unhandled_errors": 0,
            "residual_runs": 0,
            "terminal_loss": 0,
            "trace_count": 50,
            "unique_trace_count": 50,
            "missing_required_spans": 0,
            "trace_query_p95_seconds": 30.0,
            "trace_query_max_seconds": 60.0,
            "secret_plaintext_hits": 0,
            "p95_latency_ratio": 1.2,
            "p99_latency_ratio": 1.5,
            "error_rate_delta_percentage_points": 0.5,
            "otel_p95_overhead_ratio": 0.1,
            "run_set_sha256": "d" * 64,
            "scenario_set_sha256": "e" * 64,
            "trace_set_sha256": "f" * 64,
            "soak_artifact_sha256": "0" * 64,
        },
    }
    receipt_dir = root / "evidence"
    receipt_dir.mkdir()
    gates: dict[str, object] = {}
    for gate in cutover.FINAL_EVIDENCE_KEYS:
        receipt = {
            "schema_version": 1,
            "producer": evidence_module.RECEIPT_PRODUCER,
            "gate_id": gate,
            "command_id": evidence_module.COMMAND_IDS[gate],
            "receipt_id": "",
            "cutover_id": manifest["cutover_id"],
            "source_artifact_sha256": manifest["source_artifact_sha256"],
            "acceptance_artifacts_sha256": binding,
            "acceptance_identity": acceptance["acceptance_identity"],
            "image_ids": image_ids,
            "status": "passed",
            "started_at": "2026-09-10T00:00:01+00:00",
            "completed_at": "2026-09-10T00:00:02+00:00",
            "exit_code": 0,
            "result": results[gate],
        }
        receipt["receipt_id"] = evidence_module.build_machine_receipt_id(receipt)
        receipt_path = receipt_dir / f"{gate}.receipt.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        gates[gate] = {
            "status": "passed",
            "receipt_path": f"evidence/{gate}.receipt.json",
            "receipt_sha256": cutover._sha256_file(receipt_path),
        }
    evidence = root / "final-evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "cutover_id": manifest["cutover_id"],
                "source_artifact_sha256": manifest["source_artifact_sha256"],
                "acceptance_artifacts_sha256": binding,
                "status": "passed",
                **gates,
            }
        ),
        encoding="utf-8",
    )
    return evidence


def test_deploy_script_is_executable_and_has_valid_bash_syntax() -> None:
    assert os.access(SCRIPT, os.X_OK)

    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_deploy_script_defaults_and_preserves_private_remote_env() -> None:
    text = _script_text()

    assert 'DEFAULT_HOST="172.16.112.232"' in text
    assert 'DEFAULT_REMOTE_DIR="~/work/agent-gov"' in text
    assert 'DEPLOY_USER="${DEPLOY_USER:-root}"' in text
    assert 'REMOTE_DIR="${REMOTE_DIR:-$DEFAULT_REMOTE_DIR}"' in text
    assert "cp -n docker/.env.example docker/.env" in text

    for excluded in (
        "--exclude='/images/'",
        "--exclude='/docker/.env'",
        "--exclude='/docker/.env.bak-*'",
        "--exclude='/docker/.env.local-debug'",
        "--exclude='/frontend/.env.local'",
    ):
        assert excluded in text


def test_deploy_fails_before_sync_when_remote_python_is_older_than_311() -> None:
    text = _script_text()

    assert "sys.version_info < (3, 11)" in text
    assert "remote Python >=3.11 is required" in text
    assert text.index("sys.version_info < (3, 11)") < text.index("Syncing origin/master tracked code")


def test_deploy_rejects_legacy_epoch_before_overwriting_remote_source() -> None:
    text = _script_text()

    preflight = "Preflighting remote Runtime epoch before source sync"
    sync = "Syncing origin/master tracked code"
    assert preflight in text
    assert ".agentscope_atomic_cutover.preflight.py" in text
    assert "--require-current-or-empty" in text
    assert "trap 'rm -f \"$preflight_script\"' EXIT" in text
    assert text.index(preflight) < text.index(sync)


def test_deploy_script_packages_project_and_langfuse_dependency_images() -> None:
    text = _script_text()

    for image in (
        "agent-gov-agentscope-runtime:${VERSION}",
        "agent-gov-api:${VERSION}",
        "agent-gov-ui:${VERSION}",
    ):
        assert image in text

    for env_key in (
        "LANGFUSE_WORKER_IMAGE",
        "LANGFUSE_WEB_IMAGE",
        "LANGFUSE_POSTGRES_IMAGE",
        "LANGFUSE_CLICKHOUSE_IMAGE",
        "LANGFUSE_REDIS_IMAGE",
        "LANGFUSE_MINIO_IMAGE",
    ):
        assert env_key in text

    assert "docker save" in text
    assert "docker load" in text
    assert "sha256sum" in text
    assert "agent-gov-${VERSION}-images.tar.gz" in text
    assert "agent-gov-${VERSION}-langfuse-deps-images.tar.gz" in text


def test_deploy_script_uses_loaded_images_for_full_compose_stack() -> None:
    text = _script_text()

    assert "git fetch origin master" in text
    assert "origin/master" in text
    assert "git show origin/master:VERSION" in text
    assert "git archive origin/master" in text
    assert "working tree must be clean" not in text
    assert "--profile langfuse down --remove-orphans" in text
    assert "COMPOSE_ENV_FILE=docker/.env" in text
    assert 'COMPOSE_UP_FLAGS="--no-build --pull never"' in text
    assert "make --no-print-directory all-up" in text
    assert 'docker ps -aq --filter "name=agent-gov"' not in text
    assert "--profile langfuse up -d --no-build --pull never" not in text
    assert "runtime_root=$(expand_remote_value" not in text
    assert "chmod a+rwx" not in text
    assert "rm -rf '${HOME}'" not in text


def test_normal_deploy_checks_fresh_epoch_before_stopping_existing_services() -> None:
    text = _script_text()
    inspect = "python3 scripts/agentscope_atomic_cutover.py inspect"
    down = "--profile langfuse down --remove-orphans"

    assert inspect in text
    assert "--require-current-or-empty" in text
    assert text.index(inspect) < text.index(down)
    assert "普通部署" in text
    assert "destructive cutover" in text
    assert "agentscope_atomic_cutover.py execute" not in text


def test_deploy_script_uses_python_health_checks_without_remote_curl_dependency() -> None:
    text = _script_text()

    assert "from urllib.request import Request, urlopen" in text
    assert '("API and AgentScope Runtime readiness", "http://127.0.0.1:${host_port}/health/ready", 60, True)' in text
    assert '("UI", "http://127.0.0.1:${frontend_port}", 60, False)' in text
    assert '("Langfuse", "http://127.0.0.1:${langfuse_port}", 90, False)' in text
    assert 'last_error = RuntimeError(f"HTTP {exc.code}' in text
    assert 'print(f"{name} OK: {url} status={exc.code}")' not in text
    assert "curl " not in text


def test_deploy_script_preflights_agentscope_secrets_and_service_set_before_cutover() -> None:
    text = _script_text()

    for key in ("API_KEY", "AGENTGOV_RUNTIME_SHARED_SECRET", "MODEL_PROVIDER_API_KEY"):
        assert key in text
    assert "placeholder private env value is forbidden" in text
    assert "change-me|replace-with-*" in text
    for key in (
        "LANGFUSE_SALT",
        "LANGFUSE_ENCRYPTION_KEY",
        "LANGFUSE_NEXTAUTH_SECRET",
        "LANGFUSE_POSTGRES_PASSWORD",
        "LANGFUSE_CLICKHOUSE_PASSWORD",
        "LANGFUSE_REDIS_AUTH",
        "LANGFUSE_MINIO_ROOT_PASSWORD",
    ):
        assert key in text
    assert "64 nonzero lowercase hex characters" in text
    assert "LANGFUSE_ALLOW_PUBLIC_BIND=1" in text
    assert 'require_public_bind_opt_in "AgentGov API" API_BIND_IP API_ALLOW_PUBLIC_BIND' in text
    assert 'require_public_bind_opt_in "AgentGov UI" FRONTEND_BIND_IP FRONTEND_ALLOW_PUBLIC_BIND' in text
    assert "single-tenant operator control plane without cross-user isolation" in text
    assert "--profile langfuse config >/dev/null" in text
    assert "agent-gov-api agent-gov-ui agentscope-runtime" in text


def test_cutover_epoch_inspection_refuses_legacy_database_without_mutation(tmp_path) -> None:
    cutover = _load_cutover()
    db_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE sdk_sessions (id TEXT PRIMARY KEY)")
    before = db_path.read_bytes()

    result = cutover.classify_runtime_epoch(db_path)

    assert result["classification"] == "legacy-or-unknown"
    assert result["legacy_tables"] == ["sdk_sessions"]
    assert db_path.read_bytes() == before


def test_standalone_remote_preflight_inspect_does_not_require_destructive_helper(tmp_path) -> None:
    remote_root = tmp_path / "remote"
    images = remote_root / "images"
    images.mkdir(parents=True)
    standalone = images / ".agentscope_atomic_cutover.preflight.py"
    standalone.write_bytes(CUTOVER_SCRIPT.read_bytes())
    runtime_root = remote_root / "runtime-volume"
    runtime_root.mkdir()
    env_file = remote_root / "docker.env"
    env_file.write_text(f"HOST_RUNTIME_VOLUME_ROOT={runtime_root}\n", encoding="utf-8")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(standalone), "inspect", "--env-file", str(env_file), "--require-current-or-empty"],
        cwd=remote_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["classification"] == "empty"


@pytest.mark.parametrize("command", ["execute", "restore"])
def test_standalone_mutating_commands_fail_before_touching_runtime_without_helper(tmp_path, command: str) -> None:
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    standalone = isolated / "agentscope_atomic_cutover.py"
    standalone.write_bytes(CUTOVER_SCRIPT.read_bytes())
    runtime_root = isolated / "runtime-volume"
    runtime_root.mkdir()
    sentinel = runtime_root / "must-survive"
    sentinel.write_text("safe", encoding="utf-8")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [
            sys.executable,
            str(standalone),
            command,
            "--manifest",
            str(isolated / "missing-manifest.json"),
            "--runtime-root",
            str(runtime_root),
            "--confirmation-token",
            "invalid",
        ],
        cwd=isolated,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "helper 缺失" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "safe"


@pytest.mark.parametrize(
    ("include_types", "expected_error"),
    [
        (False, "helper 无法完整加载；未执行任何切换动作"),
        (True, "cutover manifest 无法读取"),
    ],
)
def test_restore_helper_bundle_is_complete_or_fails_before_mutation(
    tmp_path,
    include_types: bool,
    expected_error: str,
) -> None:
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    standalone = isolated / "agentscope_atomic_cutover.py"
    standalone.write_bytes(CUTOVER_SCRIPT.read_bytes())
    (isolated / CUTOVER_SUPPORT.name).write_bytes(CUTOVER_SUPPORT.read_bytes())
    if include_types:
        (isolated / CUTOVER_TYPES.name).write_bytes(CUTOVER_TYPES.read_bytes())
    runtime_root = isolated / "runtime-volume"
    runtime_root.mkdir()
    sentinel = runtime_root / "must-survive"
    sentinel.write_text("safe", encoding="utf-8")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [
            sys.executable,
            str(standalone),
            "restore",
            "--manifest",
            str(isolated / "missing-manifest.json"),
            "--runtime-root",
            str(runtime_root),
            "--confirmation-token",
            "invalid",
        ],
        cwd=isolated,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert expected_error in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "safe"


def test_cutover_epoch_inspection_accepts_only_agentscope_marker(tmp_path) -> None:
    cutover = _load_cutover()
    db_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT)")
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (cutover.SCHEMA_EPOCH, "2026-09-09T00:00:00Z"),
        )

    result = cutover.classify_runtime_epoch(db_path)

    assert result["classification"] == "agentscope"


def test_cutover_quiescence_gate_counts_old_and_new_runtime_work(tmp_path) -> None:
    cutover = _load_cutover()
    db_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE sessions (active_run_id TEXT);
            INSERT INTO sessions VALUES ('run-old');
            CREATE TABLE session_turn_intents (status TEXT);
            INSERT INTO session_turn_intents VALUES ('running');
            CREATE TABLE claude_user_input_requests (status TEXT);
            INSERT INTO claude_user_input_requests VALUES ('waiting');
            CREATE TABLE agent_test_runs (status TEXT);
            INSERT INTO agent_test_runs VALUES ('queued');
            CREATE TABLE agent_jobs (status TEXT);
            INSERT INTO agent_jobs VALUES ('running');
            CREATE TABLE agent_change_sets (status TEXT);
            INSERT INTO agent_change_sets VALUES ('publishing');
            """
        )

    counts = cutover.active_work_counts(db_path)

    assert counts == {
        "active_sessions": 1,
        "active_runs": 1,
        "hitl_waits": 1,
        "active_tests": 1,
        "active_agent_jobs": 1,
        "active_publications": 1,
    }


def test_cutover_snapshot_is_external_verified_and_exact_clear_keeps_siblings(tmp_path) -> None:
    cutover = _load_cutover()
    runtime_root = tmp_path / "volume-agent-gov"
    runtime_root.mkdir()
    (runtime_root / "data").mkdir()
    (runtime_root / "data/runtime.sqlite3").write_bytes(b"legacy-db")
    (runtime_root / ".hidden").write_text("legacy", encoding="utf-8")
    env_file = tmp_path / "docker.env"
    env_file.write_text(f"HOST_RUNTIME_VOLUME_ROOT={runtime_root}\n", encoding="utf-8")
    backup = tmp_path / "backups" / "cutover-one"
    sibling = tmp_path / "must-survive"
    sibling.write_text("safe", encoding="utf-8")

    archive, digest, tree = cutover.create_snapshot_with_restore_drill(runtime_root, env_file, backup)

    assert archive.parent == backup
    assert archive.is_file()
    assert digest == cutover._sha256_file(archive)
    assert {item["path"] for item in tree} == {".hidden", "data", "data/runtime.sqlite3"}
    cutover._clear_runtime_root(runtime_root)
    assert list(runtime_root.iterdir()) == []
    assert sibling.read_text(encoding="utf-8") == "safe"

    cutover._safe_extract_regular_archive(archive, runtime_root)
    assert (runtime_root / "data/runtime.sqlite3").read_bytes() == b"legacy-db"
    assert (runtime_root / ".hidden").read_text(encoding="utf-8") == "legacy"


def test_cutover_target_must_match_env_and_backup_must_be_external(tmp_path) -> None:
    cutover = _load_cutover()
    runtime_root = tmp_path / "volume-agent-gov"
    runtime_root.mkdir()
    env_file = tmp_path / "docker.env"
    env_file.write_text(f"HOST_RUNTIME_VOLUME_ROOT={runtime_root}\n", encoding="utf-8")

    assert cutover.resolve_runtime_root(env_file, runtime_root, require_exists=True) == runtime_root.resolve()
    with pytest.raises(cutover.CutoverError, match="精确一致"):
        cutover.resolve_runtime_root(env_file, tmp_path / "other", require_exists=True)
    with pytest.raises(cutover.CutoverError, match="之外"):
        cutover.create_snapshot_with_restore_drill(runtime_root, env_file, runtime_root / "backup")


def test_cutover_finalize_requires_machine_receipt_for_every_gate(tmp_path) -> None:
    cutover = _load_cutover()
    evidence = tmp_path / "final-evidence.json"
    acceptance_artifacts = {
        "acceptance_identity": "cutover-one",
        "source_artifact_sha256": "b" * 64,
        "openapi_sha256": "a" * 64,
        "image_ids": {
            "agentscope-runtime": "sha256:" + "1" * 64,
            "agent-gov-api": "sha256:" + "2" * 64,
            "agent-gov-ui": "sha256:" + "3" * 64,
        },
    }
    manifest = {
        "cutover_id": "cutover-one",
        "source_artifact_sha256": "b" * 64,
        "acceptance_artifacts": acceptance_artifacts,
        "executed_at": "2026-09-10T00:00:00+00:00",
        "snapshot_archive": str(tmp_path / "runtime-root.tar"),
    }
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "cutover_id": "cutover-one",
                "source_artifact_sha256": "b" * 64,
                "acceptance_artifacts_sha256": cutover._evidence_binding_sha256(acceptance_artifacts),
                "status": "passed",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(cutover.CutoverError, match="schema"):
        cutover._validate_final_evidence(evidence, manifest)


def test_cutover_finalize_recomputes_bound_machine_receipt_hashes(tmp_path) -> None:
    cutover = _load_cutover()
    acceptance_artifacts = {
        "acceptance_identity": "cutover-one",
        "source_artifact_sha256": "b" * 64,
        "openapi_sha256": "a" * 64,
        "image_ids": {
            "agentscope-runtime": "sha256:" + "1" * 64,
            "agent-gov-api": "sha256:" + "2" * 64,
            "agent-gov-ui": "sha256:" + "3" * 64,
        },
    }
    manifest = {
        "cutover_id": "cutover-one",
        "source_artifact_sha256": "b" * 64,
        "acceptance_artifacts": acceptance_artifacts,
        "executed_at": "2026-09-10T00:00:00+00:00",
        "snapshot_archive": str(tmp_path / "runtime-root.tar"),
    }
    evidence = _write_bound_machine_evidence(cutover, tmp_path, manifest)

    payload, digest = cutover._validate_final_evidence(evidence, manifest)
    assert payload["status"] == "passed"
    assert digest == cutover._sha256_file(evidence)

    (tmp_path / "evidence/static_gates.receipt.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(cutover.CutoverError, match="receipt digest 不匹配"):
        cutover._validate_final_evidence(evidence, manifest)


def test_cutover_finalize_rejects_free_text_command_and_self_declared_receipt(tmp_path) -> None:
    cutover = _load_cutover()
    acceptance = {
        "acceptance_identity": "cutover-one",
        "source_artifact_sha256": "b" * 64,
        "openapi_sha256": "a" * 64,
        "image_ids": {
            "agentscope-runtime": "sha256:" + "1" * 64,
            "agent-gov-api": "sha256:" + "2" * 64,
            "agent-gov-ui": "sha256:" + "3" * 64,
        },
    }
    manifest = {
        "cutover_id": "cutover-one",
        "source_artifact_sha256": "b" * 64,
        "acceptance_artifacts": acceptance,
        "executed_at": "2026-09-10T00:00:00+00:00",
        "snapshot_archive": str(tmp_path / "runtime-root.tar"),
    }
    evidence = _write_bound_machine_evidence(cutover, tmp_path, manifest)
    receipt_path = tmp_path / "evidence/static_gates.receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["command"] = "make whatever-passes"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    outer = json.loads(evidence.read_text(encoding="utf-8"))
    outer["static_gates"]["receipt_sha256"] = cutover._sha256_file(receipt_path)
    evidence.write_text(json.dumps(outer), encoding="utf-8")

    with pytest.raises(cutover.CutoverError, match="receipt schema 不精确"):
        cutover._validate_final_evidence(evidence, manifest)


def test_cutover_finalize_rejects_non_allowlisted_machine_command(tmp_path) -> None:
    cutover = _load_cutover()
    evidence_module = importlib.import_module("scripts.agentscope_atomic_cutover_evidence")
    acceptance = {
        "acceptance_identity": "cutover-one",
        "source_artifact_sha256": "b" * 64,
        "openapi_sha256": "a" * 64,
        "image_ids": {
            "agentscope-runtime": "sha256:" + "1" * 64,
            "agent-gov-api": "sha256:" + "2" * 64,
            "agent-gov-ui": "sha256:" + "3" * 64,
        },
    }
    manifest = {
        "cutover_id": "cutover-one",
        "source_artifact_sha256": "b" * 64,
        "acceptance_artifacts": acceptance,
        "executed_at": "2026-09-10T00:00:00+00:00",
        "snapshot_archive": str(tmp_path / "runtime-root.tar"),
    }
    evidence = _write_bound_machine_evidence(cutover, tmp_path, manifest)
    receipt_path = tmp_path / "evidence/static_gates.receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["command_id"] = "operator-claimed-pass"
    receipt["receipt_id"] = evidence_module.build_machine_receipt_id(receipt)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    outer = json.loads(evidence.read_text(encoding="utf-8"))
    outer["static_gates"]["receipt_sha256"] = cutover._sha256_file(receipt_path)
    evidence.write_text(json.dumps(outer), encoding="utf-8")

    with pytest.raises(cutover.CutoverError, match="固定 allowlist"):
        cutover._validate_final_evidence(evidence, manifest)


def test_cutover_exports_exact_rollback_compose_env_and_images(tmp_path, monkeypatch) -> None:
    cutover = _load_cutover()
    env_file = tmp_path / "compose.env"
    env_file.write_text("API_KEY=private\n", encoding="utf-8")
    backup = tmp_path / "external-backup"
    backup.mkdir()
    legacy_compose = tmp_path / "legacy-compose.yml"
    legacy_compose.write_text("services: {}\n", encoding="utf-8")

    def fake_run(command, label, *, capture=False):
        del label
        if command[-2:] == ["config", "--images"]:
            return "old-api:one\nold-runtime:one\n"
        if command[-1:] == ["config"]:
            return "name: old-agent-gov\nservices: {}\n"
        if command[:3] == ["docker", "image", "inspect"]:
            reference = command[-1]
            digit = "1" if reference.startswith("old-api") else "2"
            return json.dumps([{"Id": "sha256:" + digit * 64}])
        if command[:3] == ["docker", "image", "save"]:
            output = Path(command[command.index("--output") + 1])
            payload = tmp_path / "manifest.json"
            payload.write_text("{}", encoding="utf-8")
            with tarfile.open(output, "w") as archive:
                archive.add(payload, arcname="manifest.json")
            return ""
        raise AssertionError(command)

    monkeypatch.setattr(cutover, "_run", fake_run)

    bundle = cutover._capture_rollback_bundle(env_file, backup, legacy_compose)

    assert bundle["rollback_image_restore_drill"] == "passed"
    assert len(bundle["rollback_images"]) == 2
    assert Path(bundle["rollback_compose"]).read_text(encoding="utf-8").startswith("name: old-agent-gov")
    assert bundle["rollback_compose_source"] == str(legacy_compose)
    assert bundle["rollback_compose_source_sha256"] == cutover._sha256_file(legacy_compose)
    assert Path(bundle["rollback_image_archive"]).is_file()
    cutover._verify_rollback_bundle({**bundle, "snapshot_archive": str(backup / "runtime-root.tar")})


def test_cutover_agentscope_version_comes_from_runtime_lockfile() -> None:
    cutover = _load_cutover()

    assert cutover._agent_scope_version() == "2.0.8"


def test_cutover_source_hash_covers_frontend_lock_and_docker_build_inputs(tmp_path, monkeypatch) -> None:
    cutover = _load_cutover()
    for directory in ("app", "agentscope_runtime", "scripts", "frontend/src", "docker/api-gate"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    files = {
        "frontend/package.json": "{}\n",
        "frontend/pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
        "frontend/tsconfig.json": "{}\n",
        "frontend/tsconfig.node.json": "{}\n",
        "frontend/vite.config.ts": "export default {}\n",
        "frontend/index.html": "<main></main>\n",
        "docker/Dockerfile": "FROM scratch\n",
        "docker/Dockerfile.dockerignore": ".git\n",
        "docker/frontend.Dockerfile": "FROM scratch\n",
        "docker/frontend.Dockerfile.dockerignore": "node_modules\n",
        "docker/agentscope-runtime.Dockerfile": "FROM scratch\n",
        "docker/agentscope-runtime.Dockerfile.dockerignore": ".git\n",
        "docker/docker-compose.yml": "services: {}\n",
        "docker/docker-compose.langfuse.yml": "services: {}\n",
        "docker/api-gate/api-gate-state.json": "{}\n",
        "Makefile": "all:\n\ttrue\n",
        "VERSION": "0.0.0\n",
        "pyproject.toml": "[project]\nname='test'\n",
        "requirements.txt": "httpx==1\n",
        "requirements-api.txt": "fastapi==1\n",
        "agentgov_harness_digest.py": "VALUE = 1\n",
    }
    for relative, content in files.items():
        (tmp_path / relative).write_text(content, encoding="utf-8")
    monkeypatch.setattr(cutover, "REPO_ROOT", tmp_path)

    initial = cutover._source_artifact_sha256()
    (tmp_path / "frontend/pnpm-lock.yaml").write_text("lockfileVersion: '9.1'\n", encoding="utf-8")
    lock_changed = cutover._source_artifact_sha256()
    (tmp_path / "docker/Dockerfile.dockerignore").write_text(".git\n.env\n", encoding="utf-8")
    docker_changed = cutover._source_artifact_sha256()
    (tmp_path / "docker/agentscope-runtime.Dockerfile.dockerignore").write_text(".git\n**/.env*\n", encoding="utf-8")
    runtime_docker_changed = cutover._source_artifact_sha256()

    assert initial != lock_changed
    assert lock_changed != docker_changed
    assert docker_changed != runtime_docker_changed


def _cutover_recovery_fixture(cutover: ModuleType, tmp_path: Path, monkeypatch) -> dict[str, object]:
    operations = cutover._destructive_support()
    recovery_module = importlib.import_module("scripts.agentscope_atomic_cutover_recovery")
    types_module = importlib.import_module("scripts.agentscope_atomic_cutover_types")
    backup = tmp_path / "external-backup"
    backup.mkdir()
    target_names = (
        "runtime-root.tar",
        "rollback-images.tar",
        "compose.env.snapshot",
        "rollback-compose.resolved.yml",
        "rollback-images.json",
        "acceptance-only.env",
        "production-drain.env",
    )
    for name in target_names:
        (backup / name).write_text(f"artifact:{name}\n", encoding="utf-8")
    gate = backup / "api-gate/api-gate-state.json"
    operations.atomic_write_gate_state(gate, state="drain", cutover_id="cutover-one")
    database = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE runtime_cutover_ledger (cutover_id TEXT PRIMARY KEY, phase TEXT, status TEXT, detail TEXT, artifacts_json TEXT, created_at TEXT)"
        )
    monkeypatch.setattr(operations, "_classify_runtime_epoch", lambda _path: {"classification": "agentscope"})
    clock = ["2026-09-10T00:00:00+00:00"]
    manifest = {
        "cutover_id": "cutover-one",
        "state": "production_drain_ready",
        "irreversible": False,
        "snapshot_archive": str(backup / "runtime-root.tar"),
        "rollback_image_archive": str(backup / "rollback-images.tar"),
        "env_snapshot": str(backup / "compose.env.snapshot"),
        "rollback_compose": str(backup / "rollback-compose.resolved.yml"),
        "rollback_image_inventory": str(backup / "rollback-images.json"),
        "acceptance_env": str(backup / "acceptance-only.env"),
        "production_drain_env": str(backup / "production-drain.env"),
        "api_gate_state_file": str(gate),
        "acceptance_artifacts": {
            "source_artifact_sha256": "c" * 64,
            "openapi_sha256": "b" * 64,
            "image_ids": {
                "agentscope-runtime": "sha256:" + "1" * 64,
                "agent-gov-api": "sha256:" + "2" * 64,
                "agent-gov-ui": "sha256:" + "3" * 64,
            },
        },
        "production_drain_artifacts": {
            "evidence_sha256": "a" * 64,
            "evidence": {"schema_version": 2, "status": "passed"},
            "openapi_sha256": "b" * 64,
            "image_ids": {
                "agentscope-runtime": "sha256:" + "1" * 64,
                "agent-gov-api": "sha256:" + "2" * 64,
                "agent-gov-ui": "sha256:" + "3" * 64,
            },
            "api_mode": "drain",
        },
    }
    manifest_path = backup / "cutover-manifest.json"
    operations.write_json(manifest_path, manifest)
    recovery = recovery_module.CutoverRecoverySupport(
        error_type=cutover.CutoverError,
        utc_now=lambda: clock[0],
        sha256_file=cutover._sha256_file,
        write_json=operations.write_json,
        read_gate_state=operations.read_gate_state,
        atomic_write_gate_state=operations.atomic_write_gate_state,
        record_ledger=operations.record_ledger,
        database_path=lambda _root, _env: database,
    )
    artifacts = manifest["production_drain_artifacts"]
    drain = types_module.ProductionDrain(database, gate, "a" * 64, artifacts)
    runtime_root = tmp_path / "runtime-root"
    runtime_root.mkdir()
    env_file = tmp_path / "source.env"
    env_file.write_text("HOST_RUNTIME_VOLUME_ROOT=/unused\n", encoding="utf-8")
    return {
        "operations": operations,
        "recovery": recovery,
        "manifest": manifest,
        "manifest_path": manifest_path,
        "gate": gate,
        "database": database,
        "backup": backup,
        "target_names": target_names,
        "drain": drain,
        "clock": clock,
        "runtime_root": runtime_root,
        "env_file": env_file,
    }


@pytest.mark.parametrize("after_gate_write", [False, True])
def test_cutover_finalize_recovers_crash_around_atomic_gate_write(tmp_path, monkeypatch, after_gate_write) -> None:
    cutover = _load_cutover()
    context = _cutover_recovery_fixture(cutover, tmp_path, monkeypatch)
    recovery = context["recovery"]
    operations = context["operations"]
    original_gate_write = recovery._atomic_write_gate_state

    def crash_gate_write(*args, **kwargs):
        if after_gate_write:
            original_gate_write(*args, **kwargs)
        raise cutover.CutoverError("simulated gate-write crash")

    monkeypatch.setattr(recovery, "_atomic_write_gate_state", crash_gate_write)
    with pytest.raises(cutover.CutoverError, match="simulated"):
        recovery.open_production_gate(
            manifest_path=context["manifest_path"],
            manifest=context["manifest"],
            drain=context["drain"],
        )

    manifest = context["manifest"]
    assert manifest["state"] == "deletion_intent_ready"
    assert manifest["legacy_deletion_deadline"] == "2026-09-10T00:15:00+00:00"
    assert (context["backup"] / "irreversible-deletion-intent.json").is_file()
    if after_gate_write:
        with pytest.raises(cutover.CutoverError, match="永久禁止"):
            operations.require_restore_allowed(manifest)
    else:
        operations.require_restore_allowed(manifest)

    monkeypatch.setattr(recovery, "_atomic_write_gate_state", original_gate_write)
    recovery.resume_irreversible_transition(
        manifest_path=context["manifest_path"],
        manifest=manifest,
        runtime_root=context["runtime_root"],
        env_file=context["env_file"],
        expected_evidence_sha256="a" * 64,
    )
    recovery.resume_irreversible_transition(
        manifest_path=context["manifest_path"],
        manifest=manifest,
        runtime_root=context["runtime_root"],
        env_file=context["env_file"],
        expected_evidence_sha256="a" * 64,
    )

    assert manifest["state"] == "irreversible"
    assert all(not (context["backup"] / name).exists() for name in context["target_names"])
    assert (context["backup"] / "irreversible-deletion-completion.json").is_file()
    with sqlite3.connect(context["database"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runtime_cutover_ledger WHERE phase = 'opened'").fetchone()[0] == 1


def test_cutover_finalize_recovers_partial_delete_before_opened_ledger(tmp_path, monkeypatch) -> None:
    cutover = _load_cutover()
    context = _cutover_recovery_fixture(cutover, tmp_path, monkeypatch)
    recovery = context["recovery"]
    original_delete = recovery._delete_legacy_rollback_artifacts

    def crash_after_one_delete(manifest_path, manifest, intent, completed_at):
        del manifest, completed_at
        (manifest_path.parent / intent["targets"][0]["path"]).unlink()
        raise cutover.CutoverError("simulated partial-delete crash")

    monkeypatch.setattr(recovery, "_delete_legacy_rollback_artifacts", crash_after_one_delete)
    with pytest.raises(cutover.CutoverError, match="partial-delete"):
        recovery.open_production_gate(
            manifest_path=context["manifest_path"],
            manifest=context["manifest"],
            drain=context["drain"],
        )
    with sqlite3.connect(context["database"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runtime_cutover_ledger WHERE phase = 'opened'").fetchone()[0] == 0

    monkeypatch.setattr(recovery, "_delete_legacy_rollback_artifacts", original_delete)
    recovery.resume_irreversible_transition(
        manifest_path=context["manifest_path"],
        manifest=context["manifest"],
        runtime_root=context["runtime_root"],
        env_file=context["env_file"],
        expected_evidence_sha256="a" * 64,
    )

    assert all(not (context["backup"] / name).exists() for name in context["target_names"])
    with sqlite3.connect(context["database"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runtime_cutover_ledger WHERE phase = 'opened'").fetchone()[0] == 1


def test_cutover_finalize_recovers_ledger_write_after_artifacts_are_deleted(tmp_path, monkeypatch) -> None:
    cutover = _load_cutover()
    context = _cutover_recovery_fixture(cutover, tmp_path, monkeypatch)
    recovery = context["recovery"]
    original_record = recovery._record_opened_ledger

    def crash_before_opened_ledger(*_args, **_kwargs):
        raise cutover.CutoverError("simulated opened-ledger crash")

    monkeypatch.setattr(recovery, "_record_opened_ledger", crash_before_opened_ledger)
    with pytest.raises(cutover.CutoverError, match="opened-ledger"):
        recovery.open_production_gate(
            manifest_path=context["manifest_path"],
            manifest=context["manifest"],
            drain=context["drain"],
        )
    assert all(not (context["backup"] / name).exists() for name in context["target_names"])
    assert context["manifest"]["state"] == "irreversible"

    monkeypatch.setattr(recovery, "_record_opened_ledger", original_record)
    recovery.resume_irreversible_transition(
        manifest_path=context["manifest_path"],
        manifest=context["manifest"],
        runtime_root=context["runtime_root"],
        env_file=context["env_file"],
        expected_evidence_sha256="a" * 64,
    )
    with sqlite3.connect(context["database"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runtime_cutover_ledger WHERE phase = 'opened'").fetchone()[0] == 1


def test_cutover_finalize_does_not_alert_when_only_ledger_replay_crosses_deadline(tmp_path, monkeypatch) -> None:
    cutover = _load_cutover()
    context = _cutover_recovery_fixture(cutover, tmp_path, monkeypatch)
    recovery = context["recovery"]
    original_record = recovery._record_opened_ledger

    monkeypatch.setattr(
        recovery,
        "_record_opened_ledger",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(cutover.CutoverError("simulated ledger crash")),
    )
    with pytest.raises(cutover.CutoverError, match="ledger crash"):
        recovery.open_production_gate(
            manifest_path=context["manifest_path"],
            manifest=context["manifest"],
            drain=context["drain"],
        )
    context["clock"][0] = "2026-09-10T00:16:00+00:00"
    monkeypatch.setattr(recovery, "_record_opened_ledger", original_record)

    recovery.resume_irreversible_transition(
        manifest_path=context["manifest_path"],
        manifest=context["manifest"],
        runtime_root=context["runtime_root"],
        env_file=context["env_file"],
    )

    assert "legacy_deletion_timeout_alert_at" not in context["manifest"]
    with sqlite3.connect(context["database"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runtime_cutover_ledger WHERE status = 'alert'").fetchone()[0] == 0


def test_cutover_finalize_records_timeout_alert_and_still_deletes(tmp_path, monkeypatch) -> None:
    cutover = _load_cutover()
    context = _cutover_recovery_fixture(cutover, tmp_path, monkeypatch)
    recovery = context["recovery"]
    original_reconcile = recovery._reconcile_irreversible_open

    def crash_after_open(*_args, **_kwargs):
        raise cutover.CutoverError("simulated post-open crash")

    monkeypatch.setattr(recovery, "_reconcile_irreversible_open", crash_after_open)
    with pytest.raises(cutover.CutoverError, match="post-open"):
        recovery.open_production_gate(
            manifest_path=context["manifest_path"],
            manifest=context["manifest"],
            drain=context["drain"],
        )
    context["clock"][0] = "2026-09-10T00:16:00+00:00"
    monkeypatch.setattr(recovery, "_reconcile_irreversible_open", original_reconcile)

    recovery.resume_irreversible_transition(
        manifest_path=context["manifest_path"],
        manifest=context["manifest"],
        runtime_root=context["runtime_root"],
        env_file=context["env_file"],
        expected_evidence_sha256="a" * 64,
    )

    assert context["manifest"]["legacy_deletion_timeout_alert_at"] == "2026-09-10T00:16:00+00:00"
    context["clock"][0] = "2026-09-10T00:20:00+00:00"
    recovery.resume_irreversible_transition(
        manifest_path=context["manifest_path"],
        manifest=context["manifest"],
        runtime_root=context["runtime_root"],
        env_file=context["env_file"],
        expected_evidence_sha256="a" * 64,
    )
    with sqlite3.connect(context["database"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runtime_cutover_ledger WHERE status = 'alert'").fetchone()[0] == 1


def test_gate_flip_is_single_irreversible_marker_and_restore_decision(tmp_path) -> None:
    cutover = _load_cutover()
    backup = tmp_path / "external-backup"
    gate = backup / "api-gate/api-gate-state.json"
    manifest = {
        "cutover_id": "cutover-one",
        "state": "production_drain_failed",
        "irreversible": False,
        "snapshot_archive": str(backup / "runtime-root.tar"),
        "api_gate_state_file": str(gate),
    }

    cutover._atomic_write_gate_state(gate, state="drain", cutover_id="cutover-one")
    cutover._require_restore_allowed(manifest)
    drain_inode = gate.stat().st_ino

    cutover._atomic_write_gate_state(
        gate,
        state="open",
        cutover_id="cutover-one",
        irreversible_at="2026-09-09T00:00:00Z",
    )

    assert gate.stat().st_ino != drain_inode
    assert not [path for path in gate.parent.iterdir() if path.suffix == ".tmp"]
    with pytest.raises(cutover.CutoverError, match="永久禁止"):
        cutover._require_restore_allowed(manifest)


def test_cutover_script_has_acceptance_gate_drain_ledger_and_atomic_open() -> None:
    entrypoint_source = CUTOVER_SCRIPT.read_text(encoding="utf-8")
    support_source = CUTOVER_SUPPORT.read_text(encoding="utf-8")
    recovery_source = CUTOVER_RECOVERY.read_text(encoding="utf-8")
    evidence_source = CUTOVER_EVIDENCE.read_text(encoding="utf-8")
    source = entrypoint_source + support_source + recovery_source + evidence_source

    for required in (
        "PREPARE-AGENTSCOPE-FRESH-EPOCH",
        "active run/HITL/test/publish",
        "restore_drill",
        "snapshot_sha256",
        "source_artifact_sha256",
        "--force-recreate --no-build --pull never",
        "AGENTGOV_CUTOVER_ACCEPTANCE_ONLY",
        "AGENTGOV_API_MODE",
        "AGENTGOV_ACCEPTANCE_IDENTITY",
        "production_drain",
        "rollback_image_archive",
        'docker", "image", "load',
        "127.0.0.1",
        '"API_BIND_IP": "127.0.0.1"',
        '"HOST_PORT": host_port',
        "runtime_cutover_ledger",
        "api-gate-state.json",
        "irreversible-deletion-intent.json",
        "legacy_deletion_deadline",
        "machine receipt",
        "os.replace",
    ):
        assert required in source
    assert "shutil.rmtree(runtime_root)" not in source
    assert "rm -rf" not in source
    assert "DO_NOT_RESTORE_AFTER_OPEN" not in source
    finalize_source = entrypoint_source.split("def command_finalize", 1)[1].split("def command_restore", 1)[0]
    assert finalize_source.index("start_production_drain") < finalize_source.index("open_production_gate")
    drain_source = support_source.split("def start_production_drain", 1)[1].split("def open_production_gate", 1)[0]
    open_source = recovery_source.split("def open_production_gate", 1)[1].split("def resume_irreversible_transition", 1)[0]
    assert drain_source.count("bootstrap_and_force_recreate") == 1
    ordered_implementation = drain_source + open_source
    assert ordered_implementation.index("wait_ready(production_drain_env)") < ordered_implementation.index('state="open"')


def test_cutover_prepare_requires_explicit_legacy_compose_source() -> None:
    cutover = _load_cutover()

    with pytest.raises(SystemExit):
        cutover.build_parser().parse_args(
            [
                "prepare",
                "--env-file",
                "docker/.env",
                "--backup-dir",
                "/tmp/agentgov-backup",
                "--confirmation-token",
                cutover.PREPARE_CONFIRMATION,
            ]
        )

    parsed = cutover.build_parser().parse_args(
        [
            "prepare",
            "--env-file",
            "docker/.env",
            "--backup-dir",
            "/tmp/agentgov-backup",
            "--rollback-compose-file",
            "/srv/agent-gov-legacy/docker/docker-compose.yml",
            "--confirmation-token",
            cutover.PREPARE_CONFIRMATION,
        ]
    )
    assert parsed.rollback_compose_file == Path("/srv/agent-gov-legacy/docker/docker-compose.yml")

    recovery = cutover.build_parser().parse_args(
        [
            "recover-finalize",
            "--manifest",
            "/srv/cutover/cutover-manifest.json",
            "--confirmation-token",
            "FINALIZE-cutover-token",
        ]
    )
    assert recovery.evidence_file is None
