from __future__ import annotations

import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from app.runtime.agent_paths import business_agent_layout
from app.runtime.agent_profile_resolver import resolve_business_profile
from app.runtime.agent_registry_db import AgentRegistryModel
from app.runtime.business_agent_seed_catalog import declared_business_agent_ids
from app.runtime.business_agent_workspace import (
    WorkspaceSafetyError,
    prepare_business_agent_workspace,
    prepare_declared_business_agent_workspace,
)
from app.runtime.errors import ConflictError, NotFoundError
from app.runtime.runtime_db import make_session_factory
from app.runtime.settings import AppSettings
from app.runtime.state_machines import StateTransitionError, validate_transition
from app.runtime.stores.agent_registry_store import AgentProvisionReservation, AgentRegistryStore
from app.services import business_agent_provisioning
from app.services.business_agent_provisioning import provision_business_agent
from fastapi.testclient import TestClient

from test_api_execution_optimizer import _load_app


def _store(tmp_path: Path) -> tuple[AgentRegistryStore, object]:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    return AgentRegistryStore(factory), factory


def _provision(store: AgentRegistryStore, workspace: Path, *, agent_id: str = "soc-ops", name: str = "SOC"):
    return provision_business_agent(
        store=store,
        agent_id=agent_id,
        name=name,
        workspace_dir=workspace,
        template_id="general",
    )


def test_reservation_is_hidden_from_list_get_and_chat_resolution(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    workspace = tmp_path / "data" / "business-agents" / "soc-ops" / "workspace"
    reservation = store.reserve_business_agent(
        name="SOC",
        agent_id="soc-ops",
        workspace_dir=str(workspace),
    )

    assert store.list_agents() == []
    assert store.get_agent("soc-ops") is None
    with pytest.raises(NotFoundError):
        resolve_business_profile(AppSettings(), store, "soc-ops")

    store.compensate_business_agent(reservation, workspace_cleanup_complete=True)
    assert store.list_agents() == []


def test_reservation_token_and_state_machine_reject_stale_or_illegal_finalize(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    reservation = store.reserve_business_agent(
        name="SOC",
        agent_id="soc-ops",
        workspace_dir=str(tmp_path / "workspace"),
    )
    stale = AgentProvisionReservation(
        agent_id=reservation.agent_id,
        token="stale-token",
        created_new=True,
    )

    with pytest.raises(ConflictError):
        store.finalize_business_agent(stale)
    with pytest.raises(StateTransitionError):
        validate_transition("agent_provision", "ready", "active")
    assert store.get_agent("soc-ops") is None

    store.compensate_business_agent(reservation, workspace_cleanup_complete=True)
    assert store.list_agents() == []


def test_provision_recovery_only_reclaims_expired_heartbeat(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    reservation = store.reserve_business_agent(
        name="SOC",
        agent_id="soc-ops",
        workspace_dir=str(tmp_path / "workspace"),
    )

    assert store.recover_incomplete_provisions() == 0
    store.renew_business_agent_provision(reservation, now="2099-01-01T00:00:00+00:00")
    assert store.recover_incomplete_provisions(now="2099-01-01T00:14:59+00:00") == 0
    assert store.recover_incomplete_provisions(now="2099-01-01T00:15:00+00:00") == 1
    assert store.get_agent("soc-ops") is None


def test_success_finalizes_after_workspace_and_derives_hitl_from_settings(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    workspace = tmp_path / "data" / "business-agents" / "soc-ops" / "workspace"

    created = _provision(store, workspace)

    assert created.requires_web_hitl is True
    assert store.get_agent("soc-ops") is not None
    assert store.get_agent("soc-ops").requires_web_hitl is True
    settings_path = workspace / ".claude" / "settings.json"
    settings_path.write_text('{"permissions":{"ask":[]}}\n', encoding="utf-8")
    assert store.get_agent("soc-ops").requires_web_hitl is False


def test_post_and_list_derive_the_same_live_hitl_value(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = client.post("/api/agent-registry", json={"name": "SOC", "agent_id": "soc-ops"})
        listed = {item["agent_id"]: item for item in client.get("/api/agent-registry").json()}
        assert created.status_code == 201
        assert created.json()["requires_web_hitl"] is True
        assert listed["soc-ops"]["requires_web_hitl"] is True

        settings_path = Path(created.json()["workspace_dir"]) / ".claude" / "settings.json"
        settings_path.write_text('{"permissions":{"ask":[]}}\n', encoding="utf-8")
        refreshed = {item["agent_id"]: item for item in client.get("/api/agent-registry").json()}
        assert refreshed["soc-ops"]["requires_web_hitl"] is False


def test_post_rejects_workspace_symlink_and_agent_never_becomes_runnable(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    workspace = business_agent_layout(module.settings.data_dir, "linked-agent").workspace
    target = tmp_path / "external"
    target.mkdir()
    (target / "sentinel.txt").write_text("safe", encoding="utf-8")
    workspace.parent.mkdir(parents=True)
    workspace.symlink_to(target, target_is_directory=True)

    with TestClient(module.app) as client:
        created = client.post(
            "/api/agent-registry",
            json={"name": "Linked", "agent_id": "linked-agent"},
        )
        listed_ids = {item["agent_id"] for item in client.get("/api/agent-registry").json()}
        chat = client.post("/api/chat", json={"message": "hi", "agent_id": "linked-agent"})

    assert created.status_code == 409
    assert "linked-agent" not in listed_ids
    assert chat.status_code == 404
    assert [path.name for path in target.iterdir()] == ["sentinel.txt"]


def test_finalize_failure_rolls_back_new_workspace_and_deletes_new_row(monkeypatch, tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    workspace = tmp_path / "data" / "business-agents" / "soc-ops" / "workspace"

    def fail_finalize(_reservation):
        raise RuntimeError("forced finalize failure")

    monkeypatch.setattr(store, "finalize_business_agent", fail_finalize)
    with pytest.raises(RuntimeError, match="forced finalize failure"):
        _provision(store, workspace)

    assert not workspace.exists()
    assert store.get_agent("soc-ops") is None
    with factory.begin() as db:
        assert db.get(AgentRegistryModel, "soc-ops") is None


def test_rollback_preserves_file_replaced_by_external_writer_and_keeps_tombstone(monkeypatch, tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    workspace = tmp_path / "data" / "business-agents" / "soc-ops" / "workspace"

    def replace_owned_file_then_fail(_reservation):
        replacement = workspace / "external.tmp"
        replacement.write_text("external-owner", encoding="utf-8")
        os.replace(replacement, workspace / "CLAUDE.md")
        raise RuntimeError("forced finalize failure")

    monkeypatch.setattr(store, "finalize_business_agent", replace_owned_file_then_fail)
    with pytest.raises(RuntimeError, match="forced finalize failure"):
        _provision(store, workspace)

    assert (workspace / "CLAUDE.md").read_text(encoding="utf-8") == "external-owner"
    assert store.get_agent("soc-ops") is None
    with factory.begin() as db:
        row = db.get(AgentRegistryModel, "soc-ops")
        assert row is not None and row.deleted_at and row.provision_state == "ready"


def test_apply_failure_preserves_preexisting_workspace_and_tombstones_new_row(monkeypatch, tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    workspace = tmp_path / "data" / "business-agents" / "soc-ops" / "workspace"
    workspace.mkdir(parents=True)
    keep = workspace / "KEEP.txt"
    keep.write_text("operator-owned", encoding="utf-8")
    claude = workspace / "CLAUDE.md"
    claude.write_text("custom", encoding="utf-8")

    import app.runtime.business_agent_workspace as workspace_module

    real_publish = workspace_module._publish_entry
    calls = 0

    def fail_second_publish(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("forced write failure")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(workspace_module, "_publish_entry", fail_second_publish)
    with pytest.raises(ConflictError):
        _provision(store, workspace)

    assert keep.read_text(encoding="utf-8") == "operator-owned"
    assert claude.read_text(encoding="utf-8") == "custom"
    assert not (workspace / ".mcp.json").exists()
    assert not (workspace / ".claude" / "settings.json").exists()
    assert store.get_agent("soc-ops") is None
    with factory.begin() as db:
        row = db.get(AgentRegistryModel, "soc-ops")
        assert row is not None and row.deleted_at and row.provision_state == "ready"


def test_workspace_symlink_fails_closed_without_touching_target(monkeypatch, tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    workspace = tmp_path / "data" / "business-agents" / "soc-ops" / "workspace"
    target = tmp_path / "external"
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("safe", encoding="utf-8")
    workspace.parent.mkdir(parents=True)
    workspace.symlink_to(target, target_is_directory=True)

    with pytest.raises(ConflictError):
        _provision(store, workspace)

    assert sentinel.read_text(encoding="utf-8") == "safe"
    assert list(target.iterdir()) == [sentinel]
    with factory.begin() as db:
        row = db.get(AgentRegistryModel, "soc-ops")
        assert row is not None and row.deleted_at


def test_workspace_intermediate_symlink_cannot_escape_template_publish(monkeypatch, tmp_path: Path) -> None:
    template_root = tmp_path / "templates"
    template_entry = template_root / "general" / "linked" / "nested" / "escaped.txt"
    template_entry.parent.mkdir(parents=True)
    template_entry.write_text("template-owned", encoding="utf-8")
    monkeypatch.setenv("BUSINESS_AGENT_TEMPLATES_DIR", str(template_root))

    store, factory = _store(tmp_path)
    workspace = tmp_path / "data" / "business-agents" / "soc-ops" / "workspace"
    outside = tmp_path / "external"
    outside_nested = outside / "nested"
    outside_nested.mkdir(parents=True)
    sentinel = outside_nested / "sentinel.txt"
    sentinel.write_text("external-owner", encoding="utf-8")
    workspace.mkdir(parents=True)
    (workspace / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConflictError):
        _provision(store, workspace)

    assert sentinel.read_text(encoding="utf-8") == "external-owner"
    assert not (outside_nested / "escaped.txt").exists()
    assert store.get_agent("soc-ops") is None
    with factory.begin() as db:
        row = db.get(AgentRegistryModel, "soc-ops")
        assert row is not None and row.deleted_at


def test_failed_tombstone_reuse_restores_previous_row_and_workspace(monkeypatch, tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    workspace = tmp_path / "data" / "business-agents" / "soc-ops" / "workspace"
    workspace.mkdir(parents=True)
    sentinel = workspace / "KEEP.txt"
    sentinel.write_text("old", encoding="utf-8")
    store.create_business_agent(name="Old", agent_id="soc-ops", workspace_dir=str(workspace))
    store.delete_business_agent("soc-ops")
    with factory.begin() as db:
        old = db.get(AgentRegistryModel, "soc-ops")
        old_deleted_at = old.deleted_at
        old_created_at = old.created_at

    def fail_apply(*_args, **_kwargs):
        raise business_agent_provisioning.WorkspaceProvisioningError(
            "forced apply failure",
            cleanup_complete=True,
        )

    monkeypatch.setattr(business_agent_provisioning, "apply_business_agent_workspace_plan", fail_apply)
    with pytest.raises(ConflictError):
        _provision(store, workspace, name="New")

    assert sentinel.read_text(encoding="utf-8") == "old"
    assert store.get_agent("soc-ops") is None
    with factory.begin() as db:
        restored = db.get(AgentRegistryModel, "soc-ops")
        assert restored.name == "Old"
        assert restored.deleted_at == old_deleted_at
        assert restored.created_at == old_created_at
        assert restored.provision_state == "ready"
        assert restored.provision_previous_json is None


def test_concurrent_same_agent_id_has_exactly_one_winner(monkeypatch, tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    workspace = tmp_path / "data" / "business-agents" / "soc-ops" / "workspace"
    first_apply_started = Event()
    release_first = Event()
    real_apply = business_agent_provisioning.apply_business_agent_workspace_plan

    def blocking_apply(*args, **kwargs):
        first_apply_started.set()
        assert release_first.wait(timeout=10)
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(business_agent_provisioning, "apply_business_agent_workspace_plan", blocking_apply)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(_provision, store, workspace)
        assert first_apply_started.wait(timeout=10)
        second = executor.submit(_provision, store, workspace, name="Duplicate")
        with pytest.raises(ConflictError):
            second.result(timeout=10)
        release_first.set()
        winner = first.result(timeout=10)

    assert winner.agent_id == "soc-ops"
    assert [record.agent_id for record in store.list_agents()] == ["soc-ops"]


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        ("__pycache__/hook.pyc", "cache"),
        (".claude/settings.json", "not-json"),
    ],
)
def test_template_preflight_rejects_cache_and_invalid_json(
    monkeypatch,
    tmp_path: Path,
    relative_path: str,
    content: str,
) -> None:
    root = tmp_path / "templates"
    template = root / "general"
    target = template / relative_path
    target.parent.mkdir(parents=True)
    target.write_text(content, encoding="utf-8")
    monkeypatch.setenv("BUSINESS_AGENT_TEMPLATES_DIR", str(root))

    with pytest.raises(WorkspaceSafetyError):
        prepare_business_agent_workspace(agent_id="soc-ops", name="SOC", template_id="general")


def test_template_preflight_rejects_symlink(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "templates"
    template = root / "general"
    template.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (template / "CLAUDE.md").symlink_to(outside)
    monkeypatch.setenv("BUSINESS_AGENT_TEMPLATES_DIR", str(root))

    with pytest.raises(WorkspaceSafetyError):
        prepare_business_agent_workspace(agent_id="soc-ops", name="SOC", template_id="general")


def test_declared_seed_cross_id_plan_preserves_all_file_bytes(monkeypatch, tmp_path: Path) -> None:
    # 这里只验证 catalog -> live 的原样复制；repo 准入分级由 runtime_template_safety 专项测试覆盖。
    seed_root = tmp_path / "seeds"
    workspace = seed_root / "data" / "business-agents" / "source-agent" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "CLAUDE.md").write_bytes(b"# {{AGENT_ID}}\n")
    (workspace / ".mcp.json").write_bytes(b'{"mcpServers":{"live":{"type":"http","url":"http://live.internal/mcp"}}}\n')
    (workspace / "asset.bin").write_bytes(b"\x00\xfflive-workspace")
    (workspace / "README.md").write_bytes(b"workspace-owned readme\n")
    monkeypatch.setenv("RUNTIME_VOLUME_SEEDS_DIR", str(seed_root))

    plan = prepare_declared_business_agent_workspace(
        source_agent_id="source-agent",
    )

    assert plan is not None
    assert plan.template_id == "declared:source-agent"
    entries = {entry.relative_path.as_posix(): entry.content for entry in plan.entries}
    assert entries == {
        ".mcp.json": b'{"mcpServers":{"live":{"type":"http","url":"http://live.internal/mcp"}}}\n',
        "CLAUDE.md": b"# {{AGENT_ID}}\n",
        "README.md": b"workspace-owned readme\n",
        "asset.bin": b"\x00\xfflive-workspace",
    }


def test_declared_seed_catalog_filters_invalid_agent_directory_names(tmp_path: Path) -> None:
    seed_root = tmp_path / "seeds"
    for agent_id in ("valid-agent", "invalid agent", "bad@id"):
        (seed_root / "data" / "business-agents" / agent_id / "workspace").mkdir(parents=True)

    assert declared_business_agent_ids(seed_root=seed_root) == frozenset({"valid-agent"})


def test_startup_recovery_restores_tombstone_and_hides_new_orphan(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    old_workspace = tmp_path / "old"
    replacement_workspace = tmp_path / "new"
    store.create_business_agent(name="Old", agent_id="old", workspace_dir=str(old_workspace))
    store.delete_business_agent("old")
    store.reserve_business_agent(name="Replacement", agent_id="old", workspace_dir=str(replacement_workspace))
    store.reserve_business_agent(name="Orphan", agent_id="orphan", workspace_dir=str(tmp_path / "orphan"))
    replacement_workspace.mkdir()
    partial = replacement_workspace / "CLAUDE.md"
    partial.write_text("partial replacement", encoding="utf-8")

    assert store.recover_incomplete_provisions(now="2999-01-01T00:00:00+00:00") == 2
    assert store.list_agents() == []
    with pytest.raises(ConflictError, match="safely"):
        _provision(store, replacement_workspace, agent_id="old", name="Retry")
    assert partial.read_text(encoding="utf-8") == "partial replacement"
    with factory.begin() as db:
        restored = db.get(AgentRegistryModel, "old")
        orphan = db.get(AgentRegistryModel, "orphan")
        assert restored.name == "Old" and restored.deleted_at
        assert restored.provision_previous_json == {
            "kind": "workspace_must_be_absent",
            "workspace_dir": str(replacement_workspace),
        }
        assert orphan.deleted_at and orphan.provision_state == "ready"


def test_crash_recovery_blocks_partial_workspace_reuse_until_verified_cleanup(
    tmp_path: Path,
) -> None:
    store, factory = _store(tmp_path)
    workspace = tmp_path / "data" / "business-agents" / "soc-ops" / "workspace"
    store.reserve_business_agent(
        name="SOC",
        agent_id="soc-ops",
        workspace_dir=str(workspace),
    )
    workspace.mkdir(parents=True)
    partial_path = workspace / "CLAUDE.md"
    partial_path.write_text("crash-owned partial", encoding="utf-8")
    assert store.recover_incomplete_provisions(now="2999-01-01T00:00:00+00:00") == 1

    replacement = partial_path.with_name(f"{partial_path.name}.external")
    replacement.write_text("external-owner", encoding="utf-8")
    os.replace(replacement, partial_path)

    with pytest.raises(ConflictError, match="safely"):
        _provision(store, workspace, name="Retry")
    assert partial_path.read_text(encoding="utf-8") == "external-owner"
    with pytest.raises(ConflictError, match="safe provisioning"):
        store.create_business_agent(name="Unsafe", agent_id="soc-ops", workspace_dir=str(workspace))
    with factory.begin() as db:
        blocked = db.get(AgentRegistryModel, "soc-ops")
        assert blocked is not None and blocked.deleted_at
        assert blocked.provision_state == "ready"
        assert blocked.provision_previous_json is not None

    shutil.rmtree(workspace)
    recovered = _provision(store, workspace, name="Recovered")

    assert recovered.name == "Recovered"
    assert store.get_agent("soc-ops") is not None
    with factory.begin() as db:
        active = db.get(AgentRegistryModel, "soc-ops")
        assert active is not None and active.deleted_at is None
        assert active.provision_previous_json is None
