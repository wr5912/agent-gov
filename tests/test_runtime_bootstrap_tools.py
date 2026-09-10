from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from bootstrap_runtime_volume import (  # noqa: E402
    LOCAL_DEBUG_RUNTIME_VOLUME_ROOT,
    bootstrap_runtime_volume,
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


def test_resolve_runtime_root_uses_local_debug_mode_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env.local-debug"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.delenv("HOST_RUNTIME_VOLUME_ROOT", raising=False)
    monkeypatch.delenv("RUNTIME_VOLUME_MODE", raising=False)

    assert resolve_runtime_root(None, env_file) == LOCAL_DEBUG_RUNTIME_VOLUME_ROOT
