from __future__ import annotations

import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from threading import Event

import pytest
from app.runtime.advisory_lock import advisory_lock
from app.runtime.agent_paths import business_agent_repository_lock_path
from app.runtime.agent_profile_resolver import resolve_business_profile
from app.runtime.agent_registry_db import AgentRegistryModel
from app.runtime.business_agent_workspace import (
    WorkspaceProvisionEntry,
    WorkspaceProvisionPlan,
)
from app.runtime.errors import ConflictError, DataIntegrityError, NotFoundError
from app.runtime.runtime_db import make_session_factory, utc_now
from app.runtime.settings import AppSettings
from app.runtime.state_machines import StateTransitionError, validate_transition
from app.runtime.stores.agent_registry_store import (
    AgentProvisionOutcome,
    AgentProvisionReservation,
    AgentRegistryStore,
)
from app.services import business_agent_provisioning
from app.services.business_agent_provisioning import provision_business_agent


def _store(tmp_path: Path) -> tuple[AgentRegistryStore, object]:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    return AgentRegistryStore(factory, data_dir=tmp_path / "data"), factory


def _plan(*entries: tuple[str, bytes]) -> WorkspaceProvisionPlan:
    if not entries:
        entries = (
            ("CLAUDE.md", b"# SOC\n"),
            (".mcp.json", b'{"mcpServers": {}}\n'),
            (".claude/settings.json", b'{"permissions":{"ask":["Bash(*)"]}}\n'),
        )
    return WorkspaceProvisionPlan(
        entries=tuple(
            WorkspaceProvisionEntry(
                relative_path=PurePosixPath(relative_path),
                content=content,
                mode=0o644,
            )
            for relative_path, content in entries
        )
    )


def _provision(store: AgentRegistryStore, workspace: Path, *, agent_id: str = "soc-ops", name: str = "SOC"):
    return provision_business_agent(
        store=store,
        agent_id=agent_id,
        name=name,
        workspace_dir=workspace,
        plan=_plan(),
    )


def _mark_published_deleted(factory, agent_id: str) -> None:  # type: ignore[no-untyped-def]
    """Build a legacy published tombstone fixture without reviving the removed delete facade."""

    with factory.begin() as db:
        row = db.get(AgentRegistryModel, agent_id)
        assert row is not None and row.provision_completed_token
        row.deleted_at = utc_now()


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


def test_creator_wins_stable_lock_and_recovery_exact_reread_skips_completed_claim(
    tmp_path: Path,
) -> None:
    store, _ = _store(tmp_path)
    workspace = tmp_path / "data" / "business-agents" / "soc-ops" / "workspace"
    reservation = store.reserve_business_agent(
        name="SOC",
        agent_id="soc-ops",
        workspace_dir=str(workspace),
    )
    store.renew_business_agent_provision(reservation, now="2000-01-01T00:00:00+00:00")
    lock_path = business_agent_repository_lock_path(tmp_path / "data", "soc-ops")
    recovery_started = Event()

    def recover_after_discovery() -> int:
        recovery_started.set()
        return store.recover_incomplete_provisions(now="2999-01-01T00:00:00+00:00")

    with ThreadPoolExecutor(max_workers=1) as executor:
        with advisory_lock(lock_path, mode="exclusive"):
            pending = executor.submit(recover_after_discovery)
            assert recovery_started.wait(timeout=5)
            assert not pending.done()
            completed = store.finalize_business_agent(reservation)
        assert pending.result(timeout=10) == 0

    assert completed.agent_id == "soc-ops"
    assert store.get_agent("soc-ops") is not None


def test_recovery_wins_stable_lock_before_retry_create_without_deadlock(
    tmp_path: Path,
) -> None:
    store, _ = _store(tmp_path)
    workspace = tmp_path / "data" / "business-agents" / "soc-ops" / "workspace"
    reservation = store.reserve_business_agent(
        name="Interrupted",
        agent_id="soc-ops",
        workspace_dir=str(workspace),
    )
    store.renew_business_agent_provision(reservation, now="2000-01-01T00:00:00+00:00")
    lock_path = business_agent_repository_lock_path(tmp_path / "data", "soc-ops")
    create_started = Event()

    def retry_create():
        create_started.set()
        return _provision(store, workspace, name="Recovered")

    with ThreadPoolExecutor(max_workers=1) as executor:
        with advisory_lock(lock_path, mode="exclusive"):
            pending = executor.submit(retry_create)
            assert create_started.wait(timeout=5)
            assert not pending.done()
            assert store.recover_incomplete_provisions(now="2999-01-01T00:00:00+00:00") == 1
        recovered = pending.result(timeout=10)

    assert recovered.name == "Recovered"
    assert workspace.joinpath("CLAUDE.md").read_bytes() == b"# SOC\n"


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


def test_generic_provision_returns_success_when_finalize_commit_ack_is_lost(monkeypatch, tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    workspace = tmp_path / "data" / "business-agents" / "soc-ops" / "workspace"
    session_class = factory.class_
    original_commit = session_class.commit
    committed_token: str | None = None

    def commit_then_lose_ack(db_session) -> None:
        nonlocal committed_token
        row = next(
            (
                item
                for item in db_session.identity_map.values()
                if isinstance(item, AgentRegistryModel) and item.agent_id == "soc-ops" and item.provision_state == "ready" and item.provision_completed_token
            ),
            None,
        )
        original_commit(db_session)
        if row is not None and committed_token is None:
            committed_token = str(row.provision_completed_token)
            raise RuntimeError("injected generic finalize commit acknowledgement loss")

    monkeypatch.setattr(session_class, "commit", commit_then_lose_ack)
    created = _provision(store, workspace)

    assert created.agent_id == "soc-ops" and committed_token is not None
    assert workspace.joinpath("CLAUDE.md").read_bytes() == b"# SOC\n"
    assert store.get_agent("soc-ops") is not None
    with factory() as db:
        row = db.get(AgentRegistryModel, "soc-ops")
        assert row is not None
        assert row.provision_state == "ready" and row.provision_token is None
        assert row.provision_completed_token == committed_token


@pytest.mark.parametrize("resolver_failure", ["read_error", "indeterminate"])
def test_indeterminate_finalize_outcome_preserves_workspace_and_reservation(
    monkeypatch,
    tmp_path: Path,
    resolver_failure: str,
) -> None:
    store, factory = _store(tmp_path)
    workspace = tmp_path / "data" / "business-agents" / "soc-ops" / "workspace"

    def fail_finalize(_reservation):
        raise RuntimeError("injected finalize failure")

    def unresolved(_reservation):
        if resolver_failure == "read_error":
            raise RuntimeError("injected outcome read failure")
        return AgentProvisionOutcome("indeterminate")

    monkeypatch.setattr(store, "finalize_business_agent", fail_finalize)
    monkeypatch.setattr(store, "resolve_business_agent_provision", unresolved)
    with pytest.raises(DataIntegrityError, match="workspace preserved"):
        _provision(store, workspace)

    assert workspace.joinpath("CLAUDE.md").read_bytes() == b"# SOC\n"
    with factory() as db:
        row = db.get(AgentRegistryModel, "soc-ops")
        assert row is not None
        assert row.provision_state == "provisioning" and row.provision_token


def test_finalize_failure_rolls_back_new_workspace_and_deletes_new_row(monkeypatch, tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    workspace = tmp_path / "data" / "business-agents" / "soc-ops" / "workspace"

    def fail_finalize(_reservation):
        raise RuntimeError("forced finalize failure")

    monkeypatch.setattr(store, "finalize_business_agent", fail_finalize)
    with pytest.raises(business_agent_provisioning.BusinessAgentProvisioningFailure, match="provisioning failed"):
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
    with pytest.raises(business_agent_provisioning.BusinessAgentProvisioningFailure, match="provisioning failed"):
        _provision(store, workspace)

    assert (workspace / "CLAUDE.md").read_text(encoding="utf-8") == "external-owner"
    assert store.get_agent("soc-ops") is None
    with factory.begin() as db:
        row = db.get(AgentRegistryModel, "soc-ops")
        assert row is not None and row.deleted_at and row.provision_state == "ready"


def test_preexisting_workspace_is_rejected_before_apply_without_registry_state(monkeypatch, tmp_path: Path) -> None:
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
        assert row is None


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
        assert row is None


def test_workspace_intermediate_symlink_cannot_escape_package_publish(tmp_path: Path) -> None:
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
        provision_business_agent(
            store=store,
            agent_id="soc-ops",
            name="SOC",
            workspace_dir=workspace,
            plan=_plan(("linked/nested/escaped.txt", b"package-owned")),
        )

    assert sentinel.read_text(encoding="utf-8") == "external-owner"
    assert not (outside_nested / "escaped.txt").exists()
    assert store.get_agent("soc-ops") is None
    with factory.begin() as db:
        row = db.get(AgentRegistryModel, "soc-ops")
        assert row is None


def test_new_agent_rejects_foreign_layout_residue_without_claiming_it(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    workspace = tmp_path / "data" / "business-agents" / "soc-ops" / "workspace"
    private = workspace.parent / "claude-root" / "preexisting-private-state"
    private.parent.mkdir(parents=True)
    private.write_text("foreign-owner", encoding="utf-8")

    with pytest.raises(ConflictError, match="unowned state"):
        _provision(store, workspace)

    assert private.read_text(encoding="utf-8") == "foreign-owner"
    assert not workspace.exists()
    with factory.begin() as db:
        assert db.get(AgentRegistryModel, "soc-ops") is None


def test_published_tombstone_permanently_reserves_id_and_preserves_unowned_workspace(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    workspace = tmp_path / "data" / "business-agents" / "soc-ops" / "workspace"
    workspace.mkdir(parents=True)
    sentinel = workspace / "KEEP.txt"
    sentinel.write_text("old", encoding="utf-8")
    store.create_business_agent(name="Old", agent_id="soc-ops", workspace_dir=str(workspace))
    _mark_published_deleted(factory, "soc-ops")
    with factory.begin() as db:
        old = db.get(AgentRegistryModel, "soc-ops")
        old_deleted_at = old.deleted_at
        old_created_at = old.created_at

    with pytest.raises(ConflictError, match="already reserved"):
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


def test_startup_recovery_keeps_published_tombstone_and_quarantines_new_orphan(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    old_workspace = tmp_path / "old"
    replacement_workspace = tmp_path / "new"
    store.create_business_agent(name="Old", agent_id="old", workspace_dir=str(old_workspace))
    _mark_published_deleted(factory, "old")
    with pytest.raises(ConflictError, match="already reserved"):
        store.reserve_business_agent(name="Replacement", agent_id="old", workspace_dir=str(replacement_workspace))
    orphan_workspace = tmp_path / "data" / "business-agents" / "orphan" / "workspace"
    store.reserve_business_agent(name="Orphan", agent_id="orphan", workspace_dir=str(orphan_workspace))
    orphan_workspace.mkdir(parents=True)
    partial = orphan_workspace / "CLAUDE.md"
    partial.write_text("partial replacement", encoding="utf-8")

    assert store.recover_incomplete_provisions(now="2999-01-01T00:00:00+00:00") == 1
    assert store.list_agents() == []
    with pytest.raises(ConflictError, match="already reserved"):
        _provision(store, replacement_workspace, agent_id="old", name="Retry")
    assert partial.read_text(encoding="utf-8") == "partial replacement"
    with factory.begin() as db:
        restored = db.get(AgentRegistryModel, "old")
        orphan = db.get(AgentRegistryModel, "orphan")
        assert restored.name == "Old" and restored.deleted_at
        assert restored.provision_completed_token
        assert restored.provision_previous_json is None
        assert orphan.provision_previous_json == {
            "kind": "workspace_must_be_absent",
            "workspace_dir": str(orphan_workspace),
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
    with pytest.raises(ConflictError, match="already reserved"):
        store.create_business_agent(name="Unsafe", agent_id="soc-ops", workspace_dir=str(workspace))
    with factory.begin() as db:
        blocked = db.get(AgentRegistryModel, "soc-ops")
        assert blocked is not None and blocked.deleted_at
        assert blocked.provision_state == "ready"
        assert blocked.provision_previous_json is not None

    shutil.rmtree(workspace.parent)
    recovered = _provision(store, workspace, name="Recovered")

    assert recovered.name == "Recovered"
    assert store.get_agent("soc-ops") is not None
    with factory.begin() as db:
        active = db.get(AgentRegistryModel, "soc-ops")
        assert active is not None and active.deleted_at is None
        assert active.provision_previous_json is None
