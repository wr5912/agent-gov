"""v2.7 跨代重建：改进事项 ImprovementItem 事项级单一领域实体存储单元测试。

覆盖：阶段状态机（合法/非法/全前向链/返工回退）、agent scoping、创建校验、
status 派生、非法转移与未知 id 的领域错误。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.runtime.errors import BusinessRuleViolation, NotFoundError
from app.runtime.runtime_db import make_session_factory
from app.runtime.state_machines import StateTransitionError
from app.runtime.stores.improvement_store import ImprovementStore, derive_improvement_status


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
    record = store.create_improvement(
        agent_id="soc-ops", title="t", source_feedback_refs=[" fbs-1 ", "", "fbs-2", "   "]
    )
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


def test_full_forward_stage_chain_and_status_derivation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_improvement(agent_id="soc-ops", title="t")
    chain = ["triage", "attribution", "optimization", "execution", "regression", "release"]
    current = record
    for stage in chain:
        current = store.transition_stage(record.improvement_id, stage=stage)
        assert current.improvement_stage == stage
    # release 为完成态，status 派生为 done。
    assert current.improvement_status == "done"
    assert derive_improvement_status("release") == "done"
    assert derive_improvement_status("optimization") == "active"


def test_rework_backward_transition_is_allowed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_improvement(agent_id="soc-ops", title="t")
    store.transition_stage(record.improvement_id, stage="triage")
    store.transition_stage(record.improvement_id, stage="attribution")
    store.transition_stage(record.improvement_id, stage="optimization")
    # 返工：optimization -> attribution 合法（回退边）。
    back = store.transition_stage(record.improvement_id, stage="attribution")
    assert back.improvement_stage == "attribution"
    assert back.improvement_status == "active"


def test_illegal_stage_transition_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_improvement(agent_id="soc-ops", title="t")
    # 跨段跳跃 feedback_intake -> release 非法。
    with pytest.raises(StateTransitionError):
        store.transition_stage(record.improvement_id, stage="release")
    # 未知目标阶段也被拒绝。
    with pytest.raises(StateTransitionError):
        store.transition_stage(record.improvement_id, stage="bogus_stage")


def test_release_is_terminal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_improvement(agent_id="soc-ops", title="t")
    for stage in ["triage", "attribution", "optimization", "execution", "regression", "release"]:
        store.transition_stage(record.improvement_id, stage=stage)
    with pytest.raises(StateTransitionError):
        store.transition_stage(record.improvement_id, stage="optimization")


def test_transition_unknown_improvement_raises_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(NotFoundError):
        store.transition_stage("imp-nope", stage="triage")
