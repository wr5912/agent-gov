"""四阶段改进治理 W2-b：相似度归并/拆分 + 确定性相似度 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.runtime.errors import BusinessRuleViolation, ConflictError, NotFoundError
from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.improvement_store import ImprovementStore
from app.services.improvement_similarity import find_similar_improvements, similarity_score


def _store(tmp_path: Path) -> ImprovementStore:
    return ImprovementStore(make_session_factory(tmp_path / "runtime.sqlite3"))


def test_merge_unions_refs_and_archives_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = store.create_improvement(agent_id="a", title="告警误报", source_feedback_refs=["f1"])
    source = store.create_improvement(agent_id="a", title="告警误报2", source_feedback_refs=["f2", "f1"])
    merged = store.merge_improvements(target.improvement_id, source_id=source.improvement_id)
    assert set(merged.source_feedback_refs) == {"f1", "f2"}
    assert store.get_improvement(source.improvement_id).improvement_status == "archived"


def test_merge_rejects_self_cross_agent_and_archived(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a1 = store.create_improvement(agent_id="a", title="x")
    b1 = store.create_improvement(agent_id="b", title="y")
    with pytest.raises(BusinessRuleViolation):
        store.merge_improvements(a1.improvement_id, source_id=a1.improvement_id)
    with pytest.raises(BusinessRuleViolation):
        store.merge_improvements(a1.improvement_id, source_id=b1.improvement_id)
    with pytest.raises(NotFoundError):
        store.merge_improvements(a1.improvement_id, source_id="imp-nope")
    store.archive_improvement(b1.improvement_id)
    a2 = store.create_improvement(agent_id="b", title="z")
    with pytest.raises(ConflictError):
        store.merge_improvements(a2.improvement_id, source_id=b1.improvement_id)


def test_split_creates_new_improvement_and_removes_ref(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.create_improvement(agent_id="a", title="多反馈", source_feedback_refs=["f1", "f2"])
    new_item = store.split_improvement(item.improvement_id, feedback_ref="f2")
    assert new_item.agent_id == "a"
    assert new_item.source_feedback_refs == ["f2"]
    assert new_item.improvement_stage == "feedback_intake"
    assert store.get_improvement(item.improvement_id).source_feedback_refs == ["f1"]
    with pytest.raises(BusinessRuleViolation):
        store.split_improvement(item.improvement_id, feedback_ref="not-there")


def test_similarity_score_and_find(tmp_path: Path) -> None:
    assert similarity_score("告警时间不一致", [], "告警时间不一致", []) == pytest.approx(1.0)
    assert similarity_score("a", ["f1"], "b", ["f1"]) >= 0.5  # 共享反馈加权
    store = _store(tmp_path)
    keep = store.create_improvement(agent_id="a", title="告警时间窗口不一致导致误报", source_feedback_refs=["f1"])
    store.create_improvement(agent_id="a", title="完全无关的报告格式问题", source_feedback_refs=["f9"])
    other = store.create_improvement(agent_id="b", title="告警时间窗口不一致导致误报")  # 不同 Agent，排除
    store.archive_improvement(other.improvement_id)
    results = find_similar_improvements(store, agent_id="a", text="告警时间窗口不一致", refs=["f1"], exclude_id="none")
    ids = [rec.improvement_id for rec, _ in results]
    assert keep.improvement_id in ids  # 相似命中
    assert other.improvement_id not in ids  # 跨 Agent（b）被排除
    # 自身排除：以 keep 自己为基准、排除自身，结果不含 keep。
    self_excluded = find_similar_improvements(store, agent_id="a", text=keep.title, refs=keep.source_feedback_refs, exclude_id=keep.improvement_id)
    assert all(rec.improvement_id != keep.improvement_id for rec, _ in self_excluded)


def test_similarity_finds_chinese_semantic_case_without_shared_refs(tmp_path: Path) -> None:
    """四阶段改进治理 W2：中文长标题/摘要即使没有共享 feedback ref，也应命中同 Agent 相似事项。"""
    store = _store(tmp_path)
    target = store.create_improvement(
        agent_id="soc-ops",
        title="sec-ops-data MCP 数据接口返回模拟数据导致安全研判结论错误",
        summary="list_events 返回的安全事件时间窗口和当前告警时间不一致，Agent 误判为真实横向移动。",
        source_feedback_refs=["fb-a"],
    )
    store.create_improvement(
        agent_id="soc-ops",
        title="告警卡片按钮样式错位",
        summary="前端按钮布局问题，与安全事件数据质量无关。",
        source_feedback_refs=["fb-b"],
    )

    results = find_similar_improvements(
        store,
        agent_id="soc-ops",
        text="sec-ops-data 返回事件时间窗口不一致，导致横向移动告警被误判为真实攻击",
        refs=[],
        exclude_id="imp-none",
    )

    assert results
    assert results[0][0].improvement_id == target.improvement_id
    assert results[0][1] >= 0.4
