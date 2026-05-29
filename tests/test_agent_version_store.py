import json
import tarfile

import pytest

from app.runtime.errors import AgentVersionIntegrityError
from app.runtime.agent_version_store import AgentVersionStore


def _store(tmp_path):
    workspace = tmp_path / "workspace"
    claude_root = tmp_path / "claude-root"
    versions = tmp_path / "data" / "agent-versions"
    workspace.mkdir(parents=True)
    claude_root.mkdir(parents=True)
    return AgentVersionStore(versions_dir=versions, workspace_dir=workspace, claude_root=claude_root)


def test_agent_version_snapshot_excludes_runtime_state(tmp_path):
    store = _store(tmp_path)
    workspace = store.workspace_dir
    claude_root = store.claude_root

    (workspace / "agent.yaml").write_text("agent:\n  version: 0.1.0\n", encoding="utf-8")
    (workspace / ".claude" / "skills" / "alert-triage").mkdir(parents=True)
    (workspace / ".claude" / "skills" / "alert-triage" / "SKILL.md").write_text("triage", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("ignore", encoding="utf-8")
    (workspace / "__pycache__").mkdir()
    (workspace / "__pycache__" / "x.pyc").write_bytes(b"ignore")
    (store.versions_dir.parent / "feedback").mkdir(parents=True)
    (store.versions_dir.parent / "feedback" / "runs.jsonl").write_text("ignore\n", encoding="utf-8")

    (claude_root / ".agents" / "skills" / "a2ui-adk").mkdir(parents=True)
    (claude_root / ".agents" / "skills" / "a2ui-adk" / "SKILL.md").write_text("a2ui", encoding="utf-8")
    (claude_root / ".claude").mkdir()
    (claude_root / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    (claude_root / ".claude.json").write_text('{"token":"secret"}', encoding="utf-8")
    (claude_root / ".npm").mkdir()
    (claude_root / ".npm" / "log").write_text("ignore", encoding="utf-8")
    (claude_root / ".claude" / "session-env").mkdir()
    (claude_root / ".claude" / "session-env" / "s").write_text("ignore", encoding="utf-8")
    (claude_root / ".claude" / "telemetry").mkdir()
    (claude_root / ".claude" / "telemetry" / "t.json").write_text("ignore", encoding="utf-8")

    version = store.create_snapshot(reason="manual_snapshot")
    manifest = store.get_manifest(version["agent_version_id"])
    paths = {item["path"] for item in manifest["files"]}

    assert "workspace/agent.yaml" in paths
    assert "workspace/.claude/skills/alert-triage/SKILL.md" in paths
    assert not any(path.startswith("workspace/.git") for path in paths)
    assert not any(path.startswith("workspace/__pycache__") for path in paths)
    assert not any(path.startswith("data/") or path.startswith("/data") for path in paths)
    assert not any(path.startswith("claude-root/") for path in paths)

    with tarfile.open(version["bundle_path"], "r:gz") as tar:
        names = set(tar.getnames())
    assert "workspace/agent.yaml" in names
    assert not any(path.startswith("claude-root/") for path in names)


def test_agent_version_restore_preserves_unmanaged_claude_state(tmp_path):
    store = _store(tmp_path)
    workspace = store.workspace_dir
    claude_root = store.claude_root
    workspace.joinpath("agent.yaml").write_text("agent:\n  version: 0.1.0\n", encoding="utf-8")
    workspace.joinpath("rules.md").write_text("one", encoding="utf-8")
    claude_root.joinpath(".claude").mkdir(parents=True)
    claude_root.joinpath(".claude", "settings.json").write_text('{"mode":"one"}', encoding="utf-8")
    claude_root.joinpath(".claude.json").write_text("keep-original", encoding="utf-8")

    v1 = store.create_snapshot(reason="manual_snapshot")
    workspace.joinpath("rules.md").write_text("two", encoding="utf-8")
    workspace.joinpath("new.md").write_text("new", encoding="utf-8")
    claude_root.joinpath(".claude", "settings.json").write_text('{"mode":"two"}', encoding="utf-8")
    claude_root.joinpath(".claude.json").write_text("keep-current", encoding="utf-8")
    v2 = store.create_snapshot(reason="manual_snapshot")

    diff = store.diff_versions(v1["agent_version_id"], v2["agent_version_id"])
    assert any(item["path"] == "workspace/new.md" for item in diff["added"])
    assert any(item["path"] == "workspace/rules.md" for item in diff["modified"])

    restored = store.restore_version(v1["agent_version_id"], note="回滚测试")

    assert restored["requires_runtime_restart"] is True
    assert restored["pre_restore_version"]["reason"] == "pre_restore"
    assert restored["current_version"]["reason"] == "rollback"
    assert workspace.joinpath("rules.md").read_text(encoding="utf-8") == "one"
    assert not workspace.joinpath("new.md").exists()
    assert claude_root.joinpath(".claude", "settings.json").read_text(encoding="utf-8") == '{"mode":"two"}'
    assert claude_root.joinpath(".claude.json").read_text(encoding="utf-8") == "keep-current"


def test_agent_version_file_diff_returns_unified_diff(tmp_path):
    store = _store(tmp_path)
    workspace = store.workspace_dir
    workspace.joinpath("CLAUDE.md").write_text("one\n", encoding="utf-8")
    v1 = store.create_snapshot(reason="manual_snapshot")
    workspace.joinpath("CLAUDE.md").write_text("one\ntwo\n", encoding="utf-8")
    v2 = store.create_snapshot(reason="manual_snapshot")

    diff = store.diff_version_file(v1["agent_version_id"], v2["agent_version_id"], "CLAUDE.md")

    assert diff["status"] == "modified"
    assert diff["is_text"] is True
    assert "+two" in diff["unified_diff"]


def test_agent_version_restore_rejects_bundle_hash_mismatch(tmp_path):
    store = _store(tmp_path)
    store.workspace_dir.joinpath("agent.yaml").write_text("agent:\n  version: 0.1.0\n", encoding="utf-8")
    version = store.create_snapshot(reason="manual_snapshot")
    bundle_path = store.bundles_dir / f"{version['agent_version_id']}.tar.gz"
    bundle_path.write_text("corrupted", encoding="utf-8")

    with pytest.raises(AgentVersionIntegrityError, match="bundle hash mismatch"):
        store.restore_version(version["agent_version_id"])


def test_agent_version_restore_rejects_unsafe_archive_path(tmp_path):
    store = _store(tmp_path)
    store.workspace_dir.joinpath("agent.yaml").write_text("agent:\n  version: 0.1.0\n", encoding="utf-8")
    version = store.create_snapshot(reason="manual_snapshot")
    bundle_path = store.bundles_dir / f"{version['agent_version_id']}.tar.gz"
    payload = tmp_path / "escape.txt"
    payload.write_text("escape", encoding="utf-8")
    with tarfile.open(bundle_path, "w:gz") as tar:
        tar.add(payload, arcname="../escape.txt", recursive=False)
    manifest_path = store.manifests_dir / f"{version['agent_version_id']}.json"
    manifest = store.get_manifest(version["agent_version_id"])
    manifest["bundle_sha256"] = store._sha256_file(bundle_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(AgentVersionIntegrityError, match="Unsafe archive path"):
        store.restore_version(version["agent_version_id"])
