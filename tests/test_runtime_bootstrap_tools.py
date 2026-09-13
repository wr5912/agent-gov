from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from scripts import run_container_acceptance as acceptance
from scripts.agentscope_atomic_cutover_images import selected_env_child_env
from scripts.container_acceptance_inputs import (
    ACCEPTANCE_ACTIVE_ENV,
    ACCEPTANCE_CONTEXT_ENV,
    ACCEPTANCE_PROFILE_ENV,
    ACCEPTANCE_RUN_ID_ENV,
)
from scripts.container_acceptance_runtime_root import (
    BOOTSTRAP_AUTH_SECRET_ENV,
    authorized_runtime_bootstrap_env,
)
from scripts.container_acceptance_toolchain import TOOL_PATH_ENV_KEYS

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import bootstrap_runtime_volume as bootstrap_module  # noqa: E402
from bootstrap_runtime_volume import (  # noqa: E402
    CONTAINER_RUNTIME_VOLUME_ROOT,
    LOCAL_DEBUG_RUNTIME_VOLUME_ROOT,
    bootstrap_runtime_volume,
    require_authorized_runtime_root,
    resolve_bootstrap_dir,
    resolve_runtime_root,
)
from runtime_bootstrap_safety import sanitize_path, scan_path  # noqa: E402
from runtime_cleanup import cleanup_runtime_artifacts  # noqa: E402

BUILTIN_AGENT_ID = "security-operations-expert"


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _bootstrap_source(tmp_path: Path) -> Path:
    source = tmp_path / "runtime-bootstrap"
    governor = source / "governor-workspace"
    _write(governor / "AGENT.md", "# Governor\n")
    _write(
        governor / "agent.yaml",
        "schema_version: 1\nagent: {id: governor, runtime: agentscope, runtime_contract: agentscope-app/2.0.8}\n"
        "workspace_policy: {fail_closed: true, immutable_harness: true, allow_for_run: false}\n",
    )
    workspace = source / "business-agents" / BUILTIN_AGENT_ID / "workspace"
    _write(workspace / "AGENT.md", "# Security Operations Expert\n")
    _write(
        workspace / "agent.yaml",
        "schema_version: 1\nagent: {id: security-operations-expert, runtime: agentscope, runtime_contract: agentscope-app/2.0.8}\n"
        "workspace_policy: {fail_closed: true, immutable_harness: true, allow_for_run: false}\n",
    )
    _write(
        workspace / "mcp" / "support.json",
        json.dumps(
            {
                "schema_version": 1,
                "name": "support",
                "credential_refs": [],
                "mcp_config": {"type": "http_mcp", "url": "${MCP_SERVER_URL}"},
            }
        ),
    )
    _write(workspace / "payload.bin", b"\x00\xffworkspace-bytes")
    return source


def test_bootstrap_source_uses_selected_env_and_cli_takes_precedence(tmp_path: Path) -> None:
    env_root = tmp_path / "deployment"
    env_root.mkdir()
    configured = env_root / "converted-bootstrap"
    explicit = tmp_path / "explicit-bootstrap"
    env_file = env_root / "runtime.env"
    env_file.write_text("RUNTIME_BOOTSTRAP_HOST_DIR=./converted-bootstrap\n", encoding="utf-8")

    assert resolve_bootstrap_dir(None, env_file) == configured.resolve()
    assert resolve_bootstrap_dir(explicit, env_file) == explicit.resolve()


def test_runtime_bootstrap_safety_scan_is_read_only(tmp_path: Path) -> None:
    source = _bootstrap_source(tmp_path)
    mcp_path = source / "business-agents" / BUILTIN_AGENT_ID / "workspace" / "mcp" / "support.json"
    original = json.dumps(
        {
            "schema_version": 1,
            "name": "support",
            "credential_refs": [],
            "mcp_config": {"type": "http_mcp", "url": "https://user:secret@support.example/mcp"},
        }
    ).encode()
    mcp_path.write_bytes(original)

    findings = scan_path(source)

    assert any(finding.severity == "high" for finding in findings)
    assert mcp_path.read_bytes() == original


def test_runtime_bootstrap_safety_sanitizes_embedded_secret(tmp_path: Path) -> None:
    source = _bootstrap_source(tmp_path)
    mcp_path = source / "business-agents" / BUILTIN_AGENT_ID / "workspace" / "mcp" / "support.json"
    mcp_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "support",
                "credential_refs": [],
                "mcp_config": {
                    "type": "http_mcp",
                    "url": "http://10.0.0.2:58001/mcp",
                    "headers": {"Authorization": "Bearer private-token"},
                },
            }
        ),
        encoding="utf-8",
    )

    sanitize_path(source)

    sanitized = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert sanitized["mcp_config"]["url"] == "${MCP_SERVER_URL}"
    assert sanitized["mcp_config"]["headers"]["Authorization"] == "Bearer ${AUTH_TOKEN}"
    assert scan_path(source) == []


def test_runtime_bootstrap_safety_rejects_symlink(tmp_path: Path) -> None:
    source = _bootstrap_source(tmp_path)
    workspace = source / "business-agents" / BUILTIN_AGENT_ID / "workspace"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (workspace / "linked.txt").symlink_to(outside)

    findings = scan_path(source)

    assert any(finding.kind == "unsafe_file_type" and finding.severity == "high" for finding in findings)
    with pytest.raises(ValueError, match="regular file or directory"):
        bootstrap_runtime_volume(runtime_root=tmp_path / "runtime", bootstrap_dir=source)


def test_cleanup_runtime_artifacts_uses_bootstrap_names_and_protects_runtime_data(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    backup_dir = runtime_root / ".runtime-bootstrap-backups" / "20260717T000000Z"
    _write(backup_dir / "agent.yaml", "backup")
    removable = runtime_root / "governor-workspace" / "AGENT.md.bak-20260717T000000Z"
    protected = runtime_root / "data" / "runtime.sqlite3.bak-20260717T000000Z"
    _write(removable, "backup")
    _write(protected, "database backup")

    result = cleanup_runtime_artifacts(runtime_root=runtime_root)

    assert (runtime_root / ".runtime-bootstrap-backups").as_posix() in result["removed"]
    assert removable.as_posix() in result["removed"]
    assert protected.as_posix() in result["skipped_protected"]
    assert not removable.exists()
    assert protected.exists()


def test_cleanup_runtime_bootstrap_transient_artifacts(tmp_path: Path) -> None:
    bootstrap_dir = tmp_path / "docker" / "runtime-bootstrap"
    _write(bootstrap_dir / "README.md", "current\n")
    artifacts = [
        bootstrap_dir.parent / ".runtime-bootstrap-backups",
        bootstrap_dir.parent / ".runtime-bootstrap-staging",
        bootstrap_dir.parent / ".runtime-bootstrap.restore",
        bootstrap_dir.parent / ".runtime-bootstrap.before-restore",
        bootstrap_dir.parent / ".runtime-bootstrap.old-20260717T000000Z",
    ]
    for path in artifacts:
        path.mkdir()

    result = cleanup_runtime_artifacts(bootstrap_dir=bootstrap_dir)

    assert set(result["removed"]) == {path.as_posix() for path in artifacts}
    assert all(not path.exists() for path in artifacts)
    assert (bootstrap_dir / "README.md").read_text(encoding="utf-8") == "current\n"


def test_bootstrap_initializes_only_governor_and_declared_builtin_agent(tmp_path: Path) -> None:
    source = _bootstrap_source(tmp_path)
    runtime_root = tmp_path / "runtime"

    result = bootstrap_runtime_volume(runtime_root=runtime_root, bootstrap_dir=source)

    assert (runtime_root / "governor-workspace" / "AGENT.md").is_file()
    workspace = runtime_root / "data" / "business-agents" / BUILTIN_AGENT_ID / "workspace"
    assert (workspace / "AGENT.md").is_file()
    assert (workspace / "agent.yaml").is_file()
    assert (workspace / "payload.bin").read_bytes() == b"\x00\xffworkspace-bytes"
    assert not (runtime_root / "data" / "seed-catalog").exists()
    assert not (runtime_root / "templates").exists()
    assert not (runtime_root / "data" / "business-agents" / "main-agent").exists()
    assert (runtime_root / "agentscope-runtime" / "data").is_dir()
    assert (runtime_root / "agentscope-runtime" / "workspaces").is_dir()
    assert (runtime_root / "agentscope-runtime" / "candidates").is_dir()
    assert not (runtime_root / "data" / "sessions").exists()
    assert not (runtime_root / "data" / "transcripts").exists()
    assert result["copied"]


def test_bootstrap_never_reconciles_an_existing_business_agent_workspace(tmp_path: Path) -> None:
    source = _bootstrap_source(tmp_path)
    runtime_root = tmp_path / "runtime"
    bootstrap_runtime_volume(runtime_root=runtime_root, bootstrap_dir=source)
    workspace = runtime_root / "data" / "business-agents" / BUILTIN_AGENT_ID / "workspace"
    (workspace / "AGENT.md").write_text("operator-owned\n", encoding="utf-8")
    (workspace / "mcp" / "support.json").unlink()

    result = bootstrap_runtime_volume(runtime_root=runtime_root, bootstrap_dir=source)

    assert (workspace / "AGENT.md").read_text(encoding="utf-8") == "operator-owned\n"
    assert not (workspace / "mcp" / "support.json").exists()
    assert workspace.as_posix() in result["skipped_existing"]


def test_bootstrap_atomically_refreshes_readonly_governor_without_reconciling_business_workspace(tmp_path: Path) -> None:
    source = _bootstrap_source(tmp_path)
    runtime_root = tmp_path / "runtime"
    bootstrap_runtime_volume(runtime_root=runtime_root, bootstrap_dir=source)
    target = runtime_root / "governor-workspace" / "AGENT.md"
    target.chmod(0o444)
    original_owner = (target.stat().st_uid, target.stat().st_gid)
    source_file = source / "governor-workspace" / "AGENT.md"
    source_file.write_text("# Governor v2\n", encoding="utf-8")
    source_file.chmod(0o664)
    business_file = runtime_root / "data" / "business-agents" / BUILTIN_AGENT_ID / "workspace" / "AGENT.md"
    business_file.write_text("operator-owned\n", encoding="utf-8")

    result = bootstrap_runtime_volume(runtime_root=runtime_root, bootstrap_dir=source)

    assert target.read_text(encoding="utf-8") == "# Governor v2\n"
    assert target.stat().st_mode & 0o777 == 0o444
    assert (target.stat().st_uid, target.stat().st_gid) == original_owner
    assert business_file.read_text(encoding="utf-8") == "operator-owned\n"
    assert target.as_posix() in result["copied"]
    inode = target.stat().st_ino

    repeated = bootstrap_runtime_volume(runtime_root=runtime_root, bootstrap_dir=source)

    assert target.stat().st_ino == inode
    assert target.as_posix() in repeated["skipped_existing"]
    assert target.as_posix() not in repeated["copied"]
    assert not list(target.parent.glob(".AGENT.md.bootstrap-*"))


@pytest.mark.parametrize("target_type", ["symlink", "directory"])
def test_bootstrap_rejects_unsafe_governor_file_target(tmp_path: Path, target_type: str) -> None:
    source = _bootstrap_source(tmp_path)
    runtime_root = tmp_path / "runtime"
    bootstrap_runtime_volume(runtime_root=runtime_root, bootstrap_dir=source)
    target = runtime_root / "governor-workspace" / "AGENT.md"
    target.unlink()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    if target_type == "symlink":
        target.symlink_to(outside)
    else:
        target.mkdir()

    with pytest.raises(ValueError, match="governor target must be a regular file"):
        bootstrap_runtime_volume(runtime_root=runtime_root, bootstrap_dir=source)

    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert target.is_symlink() if target_type == "symlink" else target.is_dir()
    assert not list(target.parent.glob(".AGENT.md.bootstrap-*"))


def test_bootstrap_governor_replace_failure_keeps_old_file_and_removes_temporary(tmp_path: Path, monkeypatch) -> None:
    source = _bootstrap_source(tmp_path)
    runtime_root = tmp_path / "runtime"
    bootstrap_runtime_volume(runtime_root=runtime_root, bootstrap_dir=source)
    target = runtime_root / "governor-workspace" / "AGENT.md"
    target.chmod(0o444)
    source_file = source / "governor-workspace" / "AGENT.md"
    source_file.write_text("new content\n", encoding="utf-8")
    source_file.chmod(0o664)
    old_stat = target.stat()

    def fail_replace(_source, _target):
        raise OSError("simulated atomic replacement failure")

    monkeypatch.setattr(bootstrap_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated atomic replacement failure"):
        bootstrap_runtime_volume(runtime_root=runtime_root, bootstrap_dir=source)

    assert target.read_text(encoding="utf-8") == "# Governor\n"
    assert target.stat().st_ino == old_stat.st_ino
    assert target.stat().st_mode & 0o777 == 0o444
    assert not list(target.parent.glob(".AGENT.md.bootstrap-*"))


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_bootstrap_requires_exact_declared_builtin_set(tmp_path: Path, mutation: str) -> None:
    source = _bootstrap_source(tmp_path)
    builtins = source / "business-agents"
    if mutation == "missing":
        (builtins / BUILTIN_AGENT_ID).rename(source / "removed-agent")
    else:
        _write(builtins / "unexpected-agent" / "workspace" / "AGENT.md", "unexpected\n")

    with pytest.raises(ValueError, match="do not match the declared set"):
        bootstrap_runtime_volume(runtime_root=tmp_path / "runtime", bootstrap_dir=source)


def test_bootstrap_does_not_migrate_pre_cutover_runtime_dirs(tmp_path: Path) -> None:
    source = _bootstrap_source(tmp_path)
    runtime_root = tmp_path / "runtime"
    legacy = runtime_root / "data" / "agent-governance" / "worktrees" / "cs-old"
    legacy.mkdir(parents=True)

    result = bootstrap_runtime_volume(runtime_root=runtime_root, bootstrap_dir=source)

    target = runtime_root / "data" / "business-agents" / "main-agent" / "version" / "worktrees" / "cs-old"
    assert legacy.is_dir()
    assert not target.exists()
    assert "migrated" not in result


def test_resolve_runtime_root_uses_local_debug_mode_default(tmp_path: Path, process_environment) -> None:
    env_file = tmp_path / ".env.local-debug"
    env_file.write_text("", encoding="utf-8")
    process_environment.remove("HOST_RUNTIME_VOLUME_ROOT")
    process_environment.remove("RUNTIME_VOLUME_MODE")

    assert resolve_runtime_root(None, env_file) == LOCAL_DEBUG_RUNTIME_VOLUME_ROOT


@pytest.fixture
def isolated_acceptance_context():
    parent = Path(tempfile.mkdtemp(prefix=f"agentgov-acceptance-{os.geteuid()}-"))
    parent.chmod(0o700)
    runtime_root = parent / "runtime-root"
    runtime_root.mkdir(mode=0o700)
    frozen_inputs = parent / "acceptance-inputs"
    frozen_inputs.mkdir(mode=0o700)
    context_root = parent / "acceptance-context"
    context_root.mkdir(mode=0o700)
    run_id = "1777777777-acde1234abcd"
    project = f"agv-acceptance-{os.geteuid()}-acde1234abcd"
    effective_env = frozen_inputs / "compose.acceptance.env"
    effective_env.write_text(
        f"COMPOSE_PROJECT_NAME={project}\n"
        f"CONTAINER_NAME_PREFIX={project}\n"
        f"HOST_RUNTIME_VOLUME_ROOT={runtime_root}\n"
        f"RUNTIME_BOOTSTRAP_HOST_DIR={acceptance.REPO_ROOT / 'docker/runtime-bootstrap'}\n",
        encoding="utf-8",
    )
    effective_env.chmod(0o400)
    frozen_inputs.chmod(0o500)
    environment = {
        **os.environ,
        ACCEPTANCE_ACTIVE_ENV: "1",
        ACCEPTANCE_RUN_ID_ENV: run_id,
        ACCEPTANCE_PROFILE_ENV: "core",
        ACCEPTANCE_CONTEXT_ENV: str(context_root / "acceptance-context.json"),
        "AGENT_GOV_COMPOSE_ENV_FILE": str(effective_env),
        "COMPOSE_ENV_FILE": str(effective_env),
        "COMPOSE_PROJECT_NAME": project,
        "CONTAINER_NAME_PREFIX": project,
        "HOST_RUNTIME_VOLUME_ROOT": str(runtime_root),
        TOOL_PATH_ENV_KEYS["python"]: sys.executable,
    }
    isolation = acceptance.IsolatedEnvironment(
        effective_env,
        runtime_root,
        project,
        project,
        {},
        acceptance.REPO_ROOT,
    )
    try:
        yield isolation, environment
    finally:
        for private_inputs in (frozen_inputs, parent / "acceptance-inputs-redirected"):
            if private_inputs.is_dir() and not private_inputs.is_symlink():
                private_inputs.chmod(0o700)
        shutil.rmtree(parent)


def test_public_runner_bootstraps_exact_isolated_root_and_consumes_authorization(isolated_acceptance_context) -> None:
    isolation, environment = isolated_acceptance_context
    context_file = Path(environment[ACCEPTANCE_CONTEXT_ENV])

    acceptance._bootstrap_isolated_runtime(isolation, environment)

    assert (isolation.runtime_root / "governor-workspace/AGENT.md").is_file()
    assert not context_file.exists()
    assert BOOTSTRAP_AUTH_SECRET_ENV not in environment


def test_active_flag_and_run_id_alone_cannot_authorize_arbitrary_root(isolated_acceptance_context) -> None:
    isolation, environment = isolated_acceptance_context

    with pytest.raises(ValueError, match="隔离验收授权无效"):
        require_authorized_runtime_root(isolation.runtime_root, "container", environment)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (ACCEPTANCE_CONTEXT_ENV, "acceptance-context.json"),
        ("COMPOSE_ENV_FILE", "compose.acceptance.env"),
        ("COMPOSE_PROJECT_NAME", "agv-acceptance-hostile"),
        ("CONTAINER_NAME_PREFIX", "agv-acceptance-hostile"),
    ],
)
def test_bootstrap_rejects_imprecise_isolation_binding(isolated_acceptance_context, key: str, value: str) -> None:
    isolation, environment = isolated_acceptance_context
    environment[key] = str(isolation.runtime_root.parent / value) if value.endswith(".json") or value.endswith(".env") else value

    with pytest.raises(acceptance.AcceptanceError, match="隔离验收路径或 project 绑定不精确"):
        with authorized_runtime_bootstrap_env(isolation.runtime_root, environment):
            pytest.fail("非精确隔离绑定不得取得授权")


@pytest.mark.parametrize(
    ("target", "mode"),
    [("acceptance-inputs", 0o700), ("acceptance-context", 0o500), ("effective-env", 0o600)],
)
def test_bootstrap_rejects_unsealed_isolation_inputs(isolated_acceptance_context, target: str, mode: int) -> None:
    isolation, environment = isolated_acceptance_context
    parent = isolation.runtime_root.parent
    path = isolation.env_file if target == "effective-env" else parent / target
    path.chmod(mode)

    with pytest.raises(acceptance.AcceptanceError, match="必须由当前用户持有|真实目录"):
        with authorized_runtime_bootstrap_env(isolation.runtime_root, environment):
            pytest.fail("未封存的验收输入不得取得授权")


@pytest.mark.parametrize("target", ["acceptance-inputs", "acceptance-context"])
def test_bootstrap_rejects_symlinked_isolation_directory(isolated_acceptance_context, target: str) -> None:
    isolation, environment = isolated_acceptance_context
    parent = isolation.runtime_root.parent
    directory = parent / target
    if target == "acceptance-inputs":
        directory.chmod(0o700)
    redirected = parent / f"{target}-redirected"
    directory.rename(redirected)
    directory.symlink_to(redirected, target_is_directory=True)

    with pytest.raises(acceptance.AcceptanceError, match="真实目录"):
        with authorized_runtime_bootstrap_env(isolation.runtime_root, environment):
            pytest.fail("链接验收目录不得取得授权")


def test_tampered_runner_receipt_is_rejected_by_real_bootstrap_process(isolated_acceptance_context) -> None:
    isolation, environment = isolated_acceptance_context
    context_file = Path(environment[ACCEPTANCE_CONTEXT_ENV])
    with authorized_runtime_bootstrap_env(isolation.runtime_root, environment) as authorized:
        receipt = json.loads(context_file.read_text(encoding="utf-8"))
        receipt["signature"] = "0" * 64
        context_file.write_text(json.dumps(receipt), encoding="utf-8")
        context_file.chmod(0o600)
        result = acceptance.subprocess.run(
            [
                acceptance.sys.executable,
                str(acceptance.REPO_ROOT / "scripts/bootstrap_runtime_volume.py"),
                "--env-file",
                str(isolation.env_file),
                "--runtime-root",
                str(isolation.runtime_root),
                "--dry-run",
                "--quiet",
            ],
            cwd=acceptance.REPO_ROOT,
            env=authorized,
            check=False,
            capture_output=True,
            text=True,
        )

    assert result.returncode != 0
    assert "隔离验收授权无效" in result.stderr
    assert not context_file.exists()


def test_bootstrap_failure_still_removes_one_time_receipt(isolated_acceptance_context, monkeypatch) -> None:
    isolation, environment = isolated_acceptance_context
    context_file = Path(environment[ACCEPTANCE_CONTEXT_ENV])
    captured_environment: dict[str, str] | None = None

    def fail(*_args, **kwargs):
        nonlocal captured_environment
        captured_environment = kwargs["env"]
        assert context_file.is_file()
        raise acceptance.AcceptanceError("bootstrap failed")

    monkeypatch.setattr(acceptance, "_run_checked", fail)
    with pytest.raises(acceptance.AcceptanceError, match="bootstrap failed"):
        acceptance._bootstrap_isolated_runtime(isolation, environment)

    assert not context_file.exists()
    assert BOOTSTRAP_AUTH_SECRET_ENV not in environment
    assert captured_environment is not None and BOOTSTRAP_AUTH_SECRET_ENV not in captured_environment


def test_normal_deployment_root_remains_the_only_unsigned_container_root() -> None:
    require_authorized_runtime_root(CONTAINER_RUNTIME_VOLUME_ROOT.resolve(), "container", {})


def test_selected_env_runner_never_forwards_ambient_bootstrap_capability(tmp_path: Path, process_environment) -> None:
    selected = tmp_path / "selected.env"
    selected.write_text("AGENTGOV_API_MODE=open\n", encoding="utf-8")
    for key in (
        ACCEPTANCE_ACTIVE_ENV,
        ACCEPTANCE_CONTEXT_ENV,
        ACCEPTANCE_PROFILE_ENV,
        ACCEPTANCE_RUN_ID_ENV,
        BOOTSTRAP_AUTH_SECRET_ENV,
    ):
        process_environment.set(key, "hostile-ambient-value")

    child = selected_env_child_env(selected)

    assert not set(child).intersection(
        {
            ACCEPTANCE_ACTIVE_ENV,
            ACCEPTANCE_CONTEXT_ENV,
            ACCEPTANCE_PROFILE_ENV,
            ACCEPTANCE_RUN_ID_ENV,
            BOOTSTRAP_AUTH_SECRET_ENV,
        }
    )
