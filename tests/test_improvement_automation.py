"""四阶段改进治理 W2-a：自动化策略编排（auto_advance）单元测试——确定性沿真实状态机推进。"""

from __future__ import annotations

from pathlib import Path

from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.improvement_store import ImprovementStore
from app.services.improvement_automation import auto_advance


def _store(tmp_path: Path) -> ImprovementStore:
    return ImprovementStore(make_session_factory(tmp_path / "runtime.sqlite3"))


def test_off_does_not_advance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.create_improvement(agent_id="a", title="t")
    result = auto_advance(store, mode="off", item=item)
    assert result.applied_stages == []
    assert result.stopped_reason == "policy_off"
    assert store.get_improvement(item.improvement_id).improvement_stage == "feedback_intake"


def test_semi_advances_to_attribution_gate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.create_improvement(agent_id="a", title="t")
    result = auto_advance(store, mode="semi", item=item)
    # 自动经 triage 到 attribution，遇「确认归因」门停下。
    assert result.applied_stages == ["triage", "attribution"]
    assert result.stopped_reason == "gate_confirmation"
    assert result.item.improvement_stage == "attribution"


def test_full_advances_to_regression_before_release_gate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.create_improvement(agent_id="a", title="t")
    result = auto_advance(store, mode="full", item=item)
    assert result.applied_stages == ["triage", "attribution", "optimization", "execution", "regression"]
    assert result.stopped_reason == "release_gate"
    assert result.item.improvement_stage == "regression"


def test_semi_at_gate_stage_stops_immediately(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.create_improvement(agent_id="a", title="t")
    # 手动推进到 optimization（一个 GATE 出边的起点）。
    for stage in ["triage", "attribution", "optimization"]:
        item = store.transition_stage(item.improvement_id, stage=stage)
    result = auto_advance(store, mode="semi", item=item)
    assert result.applied_stages == []
    assert result.stopped_reason == "gate_confirmation"
    assert result.item.improvement_stage == "optimization"


def test_archived_does_not_advance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.create_improvement(agent_id="a", title="t")
    archived = store.archive_improvement(item.improvement_id)
    result = auto_advance(store, mode="full", item=archived)
    assert result.applied_stages == []
    assert result.stopped_reason == "archived"
