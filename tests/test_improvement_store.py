"""四阶段改进治理 跨代重建：改进事项 ImprovementItem 事项级单一领域实体存储单元测试。

覆盖：阶段状态机（合法/非法/全前向链/返工回退）、agent scoping、创建校验、
status 派生、非法转移与未知 id 的领域错误。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.runtime.errors import BusinessRuleViolation, ConflictError, NotFoundError
from app.runtime.runtime_db import AgentChangeSetModel, make_session_factory, utc_now
from app.runtime.state_machines import StateTransitionError
from app.runtime.stores.improvement_content_store import ImprovementContentStore
from app.runtime.stores.improvement_store import ImprovementStore


def _store(tmp_path: Path) -> ImprovementStore:
    return ImprovementStore(make_session_factory(tmp_path / "runtime.sqlite3"))


def test_create_assigns_backend_owned_identity_and_initial_stage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_improvement(agent_id="soc-ops", title="告警误报治理", summary="数据时间不一致")
    assert record.improvement_id.startswith("imp-")
    assert record.agent_id == "soc-ops"
    assert record.improvement_stage == "feedback_intake"
    assert record.improvement_status == "active"
    assert record.created_at and record.updated_at


def test_create_requires_agent_and_title(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(BusinessRuleViolation):
        store.create_improvement(agent_id="  ", title="x")
    with pytest.raises(BusinessRuleViolation):
        store.create_improvement(agent_id="soc-ops", title="   ")


def test_create_cleans_source_feedback_refs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_improvement(agent_id="soc-ops", title="t", source_feedback_refs=[" fbs-1 ", "", "fbs-2", "   "])
    assert record.source_feedback_refs == ["fbs-1", "fbs-2"]


def test_list_is_scoped_by_agent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = store.create_improvement(agent_id="agent-a", title="a-item")
    b = store.create_improvement(agent_id="agent-b", title="b-item")
    assert {r.improvement_id for r in store.list_improvements(agent_id="agent-a")} == {a.improvement_id}
    assert {r.improvement_id for r in store.list_improvements(agent_id="agent-b")} == {b.improvement_id}
    assert {r.improvement_id for r in store.list_improvements()} == {a.improvement_id, b.improvement_id}


def test_get_returns_none_for_unknown(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get_improvement("imp-nope") is None


def test_rework_backward_transition_is_allowed(tmp_path: Path) -> None:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    store = ImprovementStore(factory)
    content = ImprovementContentStore(factory)
    record = store.create_improvement(agent_id="soc-ops", title="t")
    content.upsert_normalized_feedback(record.improvement_id, problem="p", advance_to_stage="triage")
    content.upsert_attribution(record.improvement_id, summary="a", advance_to_stage="attribution")
    content.upsert_optimization_plan(
        record.improvement_id,
        summary="o",
        changes=[{"target": "prompt", "change": "x"}],
        advance_to_stage="optimization",
    )
    # 返工：optimization -> attribution 合法（回退边）。
    back = store.refine_stage(record.improvement_id, stage="attribution")
    assert back.improvement_stage == "attribution"
    assert back.improvement_status == "active"


def test_public_refinement_rejects_forward_and_unknown_transition(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_improvement(agent_id="soc-ops", title="t")
    # 即使状态机边合法，公开返工命令也不能用于前推。
    with pytest.raises(StateTransitionError):
        store.refine_stage(record.improvement_id, stage="triage")
    # 未知目标阶段也被拒绝。
    with pytest.raises(StateTransitionError):
        store.refine_stage(record.improvement_id, stage="bogus_stage")


def test_stage_commands_unknown_improvement_raise_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(NotFoundError):
        store.refine_stage("imp-nope", stage="triage")


def test_archive_sets_terminal_status_and_blocks_transition(tmp_path: Path) -> None:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    store = ImprovementStore(factory)
    content = ImprovementContentStore(factory)
    record = store.create_improvement(agent_id="soc-ops", title="t")
    content.upsert_normalized_feedback(record.improvement_id, problem="p", advance_to_stage="triage")
    archived = store.archive_improvement(record.improvement_id)
    assert archived.improvement_status == "archived"
    # 归档后阶段转移被拒（终态状态）。
    with pytest.raises(ConflictError):
        store.refine_stage(record.improvement_id, stage="feedback_intake")
    # 归档项仍可查询（审计），仍出现在列表中。
    assert store.get_improvement(record.improvement_id).improvement_status == "archived"
    assert any(item.improvement_id == record.improvement_id for item in store.list_improvements(agent_id="soc-ops"))


def test_archive_unknown_improvement_raises_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(NotFoundError):
        store.archive_improvement("imp-nope")


def test_add_link_is_idempotent_by_business_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    improvement = store.create_improvement(agent_id="soc-ops", title="t")
    first = store.add_link(improvement.improvement_id, kind="change_set", ref_id="agc-11111111")
    repeated = store.add_link(improvement.improvement_id, kind="change_set", ref_id="agc-11111111")
    assert repeated.link_id == first.link_id
    assert [(link.kind, link.ref_id) for link in store.list_links(improvement.improvement_id)] == [("change_set", "agc-11111111")]


@pytest.mark.parametrize("change_set_status", ["rejected", "failed"])
def test_refine_archive_and_delete_require_explicit_abandonment(tmp_path: Path, change_set_status: str) -> None:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    store = ImprovementStore(factory)
    content = ImprovementContentStore(factory)
    improvement = store.create_improvement(agent_id="soc-ops", title="t")
    content.upsert_normalized_feedback(improvement.improvement_id, problem="p", advance_to_stage="triage")
    content.upsert_attribution(improvement.improvement_id, summary="a", advance_to_stage="attribution")
    content.upsert_optimization_plan(
        improvement.improvement_id,
        summary="o",
        changes=[{"target": "prompt", "change": "x"}],
        advance_to_stage="optimization",
    )
    change_set_id = f"agc-{change_set_status}"
    now = utc_now()
    with factory.begin() as db:
        db.add(
            AgentChangeSetModel(
                change_set_id=change_set_id,
                agent_id="soc-ops",
                created_at=now,
                updated_at=now,
                status=change_set_status,
                base_commit_sha="base-sha",
                branch_name=f"change-set/{change_set_id}",
                worktree_path=str(tmp_path / "worktrees" / change_set_id),
                payload_json={"status": change_set_status},
            )
        )
    content.upsert_execution(
        improvement.improvement_id,
        summary="applied",
        changes_applied=["prompt"],
        agent_version="candidate",
        change_set_id=change_set_id,
        advance_to_stage="execution",
    )
    store.add_link(improvement.improvement_id, kind="change_set", ref_id=change_set_id)

    with pytest.raises(ConflictError, match="Abandon change set"):
        store.refine_stage(improvement.improvement_id, stage="optimization")
    with pytest.raises(ConflictError, match="Abandon change set"):
        store.archive_improvement(improvement.improvement_id)
    with pytest.raises(ConflictError, match="Abandon change set"):
        store.delete_improvement(improvement.improvement_id)
    assert store.list_links(improvement.improvement_id)

    with factory.begin() as db:
        row = db.get(AgentChangeSetModel, change_set_id)
        row.status = "abandoned"
    refined = store.refine_stage(improvement.improvement_id, stage="optimization")
    assert refined.improvement_stage == "optimization"
    assert store.list_links(improvement.improvement_id) == []
