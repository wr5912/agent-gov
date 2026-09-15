"""四阶段改进治理 跨代重建：改进事项 ImprovementItem 的 /api/improvements API 验收。

覆盖：事项级单一领域实体端到端（创建→列表 scoping→详情→阶段转移）、非法转移 409、
未知 404、空字段 400、以及 backend-owned 字段所有权（hostile 输入不得越权覆盖）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from app.runtime.improvement_db import (
    ImprovementIdempotencyOperationModel,
    ImprovementItemModel,
)
from app.runtime.improvement_idempotency_migration import (
    IDEMPOTENCY_TABLE,
    IMPROVEMENT_IDEMPOTENCY_SCHEMA_MIGRATION,
    PRE_IDEMPOTENCY_V4_SCHEMA_CONTRACT_SHA256,
)
from app.runtime.runtime_db import ensure_schema, make_engine, make_session_factory
from app.runtime.sqlite_schema_contract import CURRENT_SCHEMA_EPOCH, physical_schema_contract_sha256
from app.runtime.stores.improvement_store import ImprovementStore
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app_test_utils import load_test_app as _load_app

EMPTY_ARTIFACT_PRESENCE = {
    "normalized_feedback": False,
    "attribution": False,
    "optimization_plan": False,
    "execution": False,
    "regression_test_design": False,
}


def test_improvement_item_single_source_lifecycle(process_environment, tmp_path: Path) -> None:
    """业务产物负责前推阶段，公开 lifecycle 只允许返工。"""
    module = _load_app(process_environment, tmp_path)
    with TestClient(module.app) as client:
        created = client.post(
            "/api/improvements",
            json={"agent_id": "soc-ops", "title": "告警误报治理", "summary": "事件时间不一致", "source_feedback_refs": ["fbs-1"]},
        )
        assert created.status_code == 201
        body = created.json()
        improvement_id = body["improvement_id"]
        assert improvement_id.startswith("imp-")
        assert body["agent_id"] == "soc-ops"
        assert body["improvement_stage"] == "feedback_intake"
        assert body["improvement_status"] == "active"
        assert body["source_feedback_refs"] == ["fbs-1"]
        assert body["artifact_presence"] == EMPTY_ARTIFACT_PRESENCE

        # 列表按业务 Agent scoping。
        scoped = client.get("/api/improvements", params={"agent_id": "soc-ops"})
        assert scoped.status_code == 200
        assert improvement_id in {item["improvement_id"] for item in scoped.json()}
        assert scoped.json()[0]["artifact_presence"] == EMPTY_ARTIFACT_PRESENCE

        # 详情可读。
        detail = client.get(f"/api/improvements/{improvement_id}")
        assert detail.status_code == 200 and detail.json()["improvement_id"] == improvement_id
        assert detail.json()["artifact_presence"] == EMPTY_ARTIFACT_PRESENCE

        # 通用 lifecycle 即使目标是相邻状态也不得前推。
        forward = client.post(f"/api/improvements/{improvement_id}/lifecycle", json={"stage": "triage"})
        assert forward.status_code == 409
        assert client.get(f"/api/improvements/{improvement_id}").json()["improvement_stage"] == "feedback_intake"

        # 业务产物成功后由后端推进：系统理解 -> triage，归因 -> attribution。
        assert (
            client.put(
                f"/api/improvements/{improvement_id}/normalized-feedback",
                json={"problem": "告警误报"},
            ).status_code
            == 200
        )
        after_normalized = client.get(f"/api/improvements/{improvement_id}").json()
        assert after_normalized["improvement_stage"] == "triage"
        assert after_normalized["artifact_presence"] == {
            **EMPTY_ARTIFACT_PRESENCE,
            "normalized_feedback": True,
        }
        assert client.post(f"/api/improvements/{improvement_id}/normalized-feedback/confirm").status_code == 200
        assert (
            client.put(
                f"/api/improvements/{improvement_id}/attribution",
                json={"summary": "数据时间不一致", "responsibility_boundary": [], "evidence": []},
            ).status_code
            == 200
        )
        after_attribution = client.get(f"/api/improvements/{improvement_id}").json()
        assert after_attribution["improvement_stage"] == "attribution"
        assert after_attribution["artifact_presence"] == {
            **EMPTY_ARTIFACT_PRESENCE,
            "normalized_feedback": True,
            "attribution": True,
        }

        # lifecycle 保留合法返工 attribution -> triage。
        refined = client.post(f"/api/improvements/{improvement_id}/lifecycle", json={"stage": "triage"})
        assert refined.status_code == 200 and refined.json()["improvement_stage"] == "triage"
        assert refined.json()["artifact_presence"] == {
            **EMPTY_ARTIFACT_PRESENCE,
            "normalized_feedback": True,
        }

        # 非法跨段转移被状态机拒绝（409）。
        rejected = client.post(f"/api/improvements/{improvement_id}/lifecycle", json={"stage": "release"})
        assert rejected.status_code == 409
        assert "transition" in rejected.json()["detail"].lower()


def test_list_scoped_by_agent_and_global(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    with TestClient(module.app) as client:
        a = client.post("/api/improvements", json={"agent_id": "agent-a", "title": "a"}).json()["improvement_id"]
        b = client.post("/api/improvements", json={"agent_id": "agent-b", "title": "b"}).json()["improvement_id"]
        only_a = {i["improvement_id"] for i in client.get("/api/improvements", params={"agent_id": "agent-a"}).json()}
        allitems = {i["improvement_id"] for i in client.get("/api/improvements").json()}
    assert only_a == {a}
    assert {a, b}.issubset(allitems)


def test_create_retry_key_replays_one_improvement_and_rejects_changed_input(process_environment, tmp_path: Path) -> None:
    """服务端提交后响应丢失时，同一公开重试键不得创建第二个事项。"""
    module = _load_app(process_environment, tmp_path)
    headers = {"Idempotency-Key": "feedback-drawer-create-1"}
    payload = {
        "agent_id": "soc-ops",
        "title": "响应丢失重试",
        "summary": "同一请求",
        "source_feedback_refs": ["run-1"],
    }
    with TestClient(module.app) as client:
        first = client.post("/api/improvements", headers=headers, json=payload)
        replay = client.post("/api/improvements", headers=headers, json=payload)
        conflict = client.post("/api/improvements", headers=headers, json={**payload, "title": "不同请求"})
        listed = client.get("/api/improvements", params={"agent_id": "soc-ops"}).json()

    assert first.status_code == replay.status_code == 201
    assert replay.json()["improvement_id"] == first.json()["improvement_id"]
    assert [item["improvement_id"] for item in listed] == [first.json()["improvement_id"]]
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_retry_key_replays_mutated_resource_and_tombstone_blocks_recreation(process_environment, tmp_path: Path) -> None:
    """账本绑定原始请求与资源引用；资源正文变化不误判，硬删后不复活。"""
    module = _load_app(process_environment, tmp_path)
    headers = {"Idempotency-Key": "feedback-drawer-durable-result"}
    payload = {"agent_id": "soc-ops", "title": "初始标题", "summary": "原始请求"}
    with TestClient(module.app) as client:
        first = client.post("/api/improvements", headers=headers, json=payload)
        improvement_id = first.json()["improvement_id"]
        module.improvement_store.update_title(improvement_id, title="后续编辑标题")
        replay = client.post("/api/improvements", headers=headers, json=payload)
        deleted = client.delete(f"/api/improvements/{improvement_id}")
        stale_retry = client.post("/api/improvements", headers=headers, json=payload)
        listed = client.get("/api/improvements", params={"agent_id": "soc-ops"}).json()

    assert replay.status_code == 201
    assert replay.json()["improvement_id"] == improvement_id
    assert replay.json()["title"] == "后续编辑标题"
    assert deleted.status_code == 204
    assert stale_retry.status_code == 409
    assert listed == []
    with module.runtime_db_session_factory() as db:
        ledger = db.query(ImprovementIdempotencyOperationModel).one()
        assert ledger.result_resource_id == improvement_id
        assert ledger.request_fingerprint
        assert ledger.tombstoned is True


def test_same_retry_key_is_atomic_under_concurrent_store_calls(tmp_path: Path) -> None:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    store = ImprovementStore(factory)
    barrier = Barrier(8)

    def create_once(_: int) -> str:
        barrier.wait(timeout=5)
        return store.create_improvement(
            agent_id="soc-ops",
            title="并发响应丢失",
            summary="完全相同的请求",
            idempotency_key="concurrent-create-key",
        ).improvement_id

    with ThreadPoolExecutor(max_workers=8) as pool:
        result_ids = list(pool.map(create_once, range(8)))

    assert len(set(result_ids)) == 1
    with factory() as db:
        assert db.query(ImprovementItemModel).count() == 1
        assert db.query(ImprovementIdempotencyOperationModel).count() == 1


def _pre_idempotency_v4_engine(tmp_path: Path) -> Engine:
    db_path = tmp_path / "runtime.sqlite3"
    make_session_factory(db_path)
    engine = make_engine(db_path)
    with engine.begin() as connection:
        connection.exec_driver_sql(f'DROP TABLE "{IDEMPOTENCY_TABLE}"')
        connection.execute(
            text("DELETE FROM schema_migrations WHERE version = :version"),
            {"version": IMPROVEMENT_IDEMPOTENCY_SCHEMA_MIGRATION},
        )
    with engine.connect() as connection:
        assert physical_schema_contract_sha256(connection.connection.driver_connection) == PRE_IDEMPOTENCY_V4_SCHEMA_CONTRACT_SHA256
    return engine


def test_exact_pre_idempotency_v4_schema_is_migrated_in_place(tmp_path: Path) -> None:
    engine = _pre_idempotency_v4_engine(tmp_path)

    ensure_schema(engine)

    assert IDEMPOTENCY_TABLE in set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        versions = set(connection.execute(text("SELECT version FROM schema_migrations")).scalars())
        assert IMPROVEMENT_IDEMPOTENCY_SCHEMA_MIGRATION in versions


@pytest.mark.parametrize("retain_known_marker", [False, True])
def test_pre_idempotency_v4_migration_rejects_every_unknown_marker_without_mutation(
    tmp_path: Path,
    retain_known_marker: bool,
) -> None:
    engine = _pre_idempotency_v4_engine(tmp_path)
    with engine.begin() as connection:
        if not retain_known_marker:
            connection.execute(
                text("DELETE FROM schema_migrations WHERE version != :epoch"),
                {"epoch": CURRENT_SCHEMA_EPOCH},
            )
        connection.execute(
            text("INSERT INTO schema_migrations(version, applied_at) VALUES (:version, :applied_at)"),
            {"version": "unknown-v4-migration", "applied_at": "now"},
        )
    db_path = tmp_path / "runtime.sqlite3"
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        tables_before = set(inspect(connection).get_table_names())
        versions_before = set(connection.execute(text("SELECT version FROM schema_migrations")).scalars())
    bytes_before = db_path.read_bytes()

    with pytest.raises(RuntimeError):
        ensure_schema(engine)

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        tables_after = set(inspect(connection).get_table_names())
        versions_after = set(connection.execute(text("SELECT version FROM schema_migrations")).scalars())
    assert db_path.read_bytes() == bytes_before
    assert tables_after == tables_before
    assert versions_after == versions_before
    assert IDEMPOTENCY_TABLE not in tables_after
    assert IMPROVEMENT_IDEMPOTENCY_SCHEMA_MIGRATION not in versions_after


def test_pre_idempotency_v4_migration_rejects_partial_schema_without_mutation(tmp_path: Path) -> None:
    engine = _pre_idempotency_v4_engine(tmp_path)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE partial_unknown (id TEXT PRIMARY KEY)")

    with pytest.raises(RuntimeError):
        ensure_schema(engine)

    assert IDEMPOTENCY_TABLE not in set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        versions = set(connection.execute(text("SELECT version FROM schema_migrations")).scalars())
        assert IMPROVEMENT_IDEMPOTENCY_SCHEMA_MIGRATION not in versions


def test_create_rejects_empty_and_unknown_is_404(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    with TestClient(module.app) as client:
        assert client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "  "}).status_code == 400
        assert client.post("/api/improvements", json={"agent_id": "  ", "title": "x"}).status_code == 400
        assert (
            client.post(
                "/api/improvements",
                json={"agent_id": "soc-ops", "title": "伪造归属", "source_feedback_refs": ["fbc-forged"]},
            ).status_code
            == 400
        )
        assert client.get("/api/improvements/imp-unknown").status_code == 404
        assert client.post("/api/improvements/imp-unknown/lifecycle", json={"stage": "triage"}).status_code == 404
        assert client.post("/api/improvements/imp-unknown/lifecycle", json={"stage": "unknown"}).status_code == 422


def test_archive_is_terminal_status_and_blocks_lifecycle(process_environment, tmp_path: Path) -> None:
    """归档为终态：事项关系与内容都不可再写，且失败写入不留下部分副作用。"""
    module = _load_app(process_environment, tmp_path)
    with TestClient(module.app) as client:
        created = client.post(
            "/api/improvements",
            json={"agent_id": "soc-ops", "title": "待归档事项", "source_feedback_refs": ["feedback-1"]},
        )
        improvement_id = created.json()["improvement_id"]
        assert (
            client.put(
                f"/api/improvements/{improvement_id}/normalized-feedback",
                json={"problem": "归档前问题"},
            ).status_code
            == 200
        )
        archived = client.post(f"/api/improvements/{improvement_id}/archive")
        assert archived.status_code == 200 and archived.json()["improvement_status"] == "archived"
        assert archived.json()["artifact_presence"]["normalized_feedback"] is True
        assert (
            client.post(
                f"/api/improvements/{improvement_id}/lifecycle",
                json={"stage": "feedback_intake"},
            ).status_code
            == 409
        )
        assert (
            client.put(
                f"/api/improvements/{improvement_id}/normalized-feedback",
                json={"problem": "归档后污染"},
            ).status_code
            == 409
        )
        assert client.post(f"/api/improvements/{improvement_id}/normalized-feedback/confirm").status_code == 409
        assert (
            client.post(
                f"/api/improvements/{improvement_id}/feedbacks",
                json={"summary": "归档后反馈"},
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"/api/improvements/{improvement_id}/split",
                json={"feedback_ref": "feedback-1"},
            ).status_code
            == 409
        )
        normalized = client.get(f"/api/improvements/{improvement_id}/normalized-feedback").json()
        unchanged = client.get(f"/api/improvements/{improvement_id}").json()
        assert normalized["problem"] == "归档前问题" and normalized["status"] == "draft"
        assert unchanged["source_feedback_refs"] == ["feedback-1"]
        # 归档项仍可列出（审计）。
        assert improvement_id in {i["improvement_id"] for i in client.get("/api/improvements").json()}
        # 未知 id 归档 404。
        assert client.post("/api/improvements/imp-unknown/archive").status_code == 404


def test_merge_split_and_similar_api(process_environment, tmp_path: Path) -> None:
    """W2-b：相似 → 归并(同 Agent)→ 拆分；跨 Agent 归并 400、未知 404。"""
    module = _load_app(process_environment, tmp_path)
    with TestClient(module.app) as client:
        a = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "告警时间窗口不一致误报", "source_feedback_refs": ["f1"]}).json()
        b = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "告警时间窗口不一致重复反馈", "source_feedback_refs": ["f2"]}).json()
        # 相似列表（同 Agent，含 b）。
        similar = client.get(f"/api/improvements/{a['improvement_id']}/similar").json()
        assert any(s["improvement"]["improvement_id"] == b["improvement_id"] for s in similar)
        assert all(s["improvement"]["artifact_presence"] == EMPTY_ARTIFACT_PRESENCE for s in similar)
        # 归并 b 进 a：a 拿到 f1+f2，b 归档。
        merged = client.post(f"/api/improvements/{a['improvement_id']}/merge", json={"source_improvement_id": b["improvement_id"]})
        assert merged.status_code == 200 and set(merged.json()["source_feedback_refs"]) == {"f1", "f2"}
        assert merged.json()["artifact_presence"] == EMPTY_ARTIFACT_PRESENCE
        assert client.get(f"/api/improvements/{b['improvement_id']}").json()["improvement_status"] == "archived"
        # 拆分 f2 出来为新事项。
        split = client.post(f"/api/improvements/{a['improvement_id']}/split", json={"feedback_ref": "f2"})
        assert split.status_code == 201 and split.json()["source_feedback_refs"] == ["f2"]
        assert split.json()["artifact_presence"] == EMPTY_ARTIFACT_PRESENCE
        # 跨 Agent 归并被拒（400）。
        other = client.post("/api/improvements", json={"agent_id": "shop-bot", "title": "无关"}).json()
        assert client.post(f"/api/improvements/{a['improvement_id']}/merge", json={"source_improvement_id": other["improvement_id"]}).status_code == 400
        # 未知 merge 源 404。
        assert client.post(f"/api/improvements/{a['improvement_id']}/merge", json={"source_improvement_id": "imp-nope"}).status_code == 404


def test_auto_merge_on_create(process_environment, tmp_path: Path) -> None:
    """auto_merge 首次绑定结果；候选漂移时重放原结果，异请求无副作用。"""
    module = _load_app(process_environment, tmp_path)
    headers = {"Idempotency-Key": "auto-merge-retry-key"}
    payload = {
        "agent_id": "soc-ops",
        "title": "数据时间窗口不可靠导致误判",
        "source_feedback_refs": ["fb"],
        "auto_merge": True,
    }
    with TestClient(module.app) as client:
        base = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "数据时间窗口不可靠导致误判", "source_feedback_refs": ["fa"]}).json()
        merged = client.post("/api/improvements", headers=headers, json=payload)
        # 首次完成后增加一个更新、同分的候选，使重试时相似度选择发生漂移。
        newer = client.post(
            "/api/improvements",
            json={"agent_id": "soc-ops", "title": payload["title"], "source_feedback_refs": ["fc"]},
        ).json()
        replay = client.post("/api/improvements", headers=headers, json=payload)
        conflict = client.post(
            "/api/improvements",
            headers=headers,
            json={**payload, "source_feedback_refs": ["fd"]},
        )
        newer_after = client.get(f"/api/improvements/{newer['improvement_id']}").json()
        base_after = client.get(f"/api/improvements/{base['improvement_id']}").json()

    assert merged.status_code == replay.status_code == 201
    assert merged.json()["improvement_id"] == base["improvement_id"]
    assert replay.json()["improvement_id"] == base["improvement_id"]
    assert set(base_after["source_feedback_refs"]) == {"fa", "fb"}
    assert newer_after["source_feedback_refs"] == ["fc"]
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_closed_loop_links_api_is_read_only(process_environment, tmp_path: Path) -> None:
    """闭环链接由权威业务动作写入；公开 API 只读，不能注入任意或跨 Agent 引用。"""
    module = _load_app(process_environment, tmp_path)
    with TestClient(module.app) as client:
        item = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "关联闭环"}).json()
        iid = item["improvement_id"]
        module.improvement_store.add_link(iid, kind="attribution", ref_id="attr-authoritative")
        injected = client.post(
            f"/api/improvements/{iid}/links",
            json={"kind": "change_set", "ref_id": "foreign-or-missing-change-set"},
        )
        links = client.get(f"/api/improvements/{iid}/links").json()
        assert injected.status_code == 405
        assert {(link["kind"], link["ref_id"]) for link in links} == {("attribution", "attr-authoritative")}
        assert client.get("/api/improvements/imp-nope/links").status_code == 404


def test_create_ignores_hostile_backend_owned_fields(process_environment, tmp_path: Path) -> None:
    """字段所有权：请求体里夹带 backend-owned 字段不得越权——后端权威生成 id/stage/status。"""
    module = _load_app(process_environment, tmp_path)
    with TestClient(module.app) as client:
        created = client.post(
            "/api/improvements",
            json={
                "agent_id": "soc-ops",
                "title": "正常标题",
                "improvement_id": "hacked-id",
                "improvement_stage": "release",
                "improvement_status": "done",
                "artifact_presence": {
                    "normalized_feedback": True,
                    "attribution": True,
                    "optimization_plan": True,
                    "execution": True,
                    "regression_test_design": True,
                },
                "created_at": "1999-01-01T00:00:00Z",
            },
        )
    assert created.status_code == 201
    body = created.json()
    # 后端权威字段未被污染。
    assert body["improvement_id"] != "hacked-id" and body["improvement_id"].startswith("imp-")
    assert body["improvement_stage"] == "feedback_intake"
    assert body["improvement_status"] == "active"
    assert body["artifact_presence"] == EMPTY_ARTIFACT_PRESENCE
    assert body["created_at"] != "1999-01-01T00:00:00Z"


def test_artifact_presence_false_keeps_strict_subresource_404(process_environment, tmp_path: Path) -> None:
    module = _load_app(process_environment, tmp_path)
    with TestClient(module.app) as client:
        item = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "空产物事项"}).json()
        improvement_id = item["improvement_id"]
        assert item["artifact_presence"] == EMPTY_ARTIFACT_PRESENCE
        for suffix in (
            "normalized-feedback",
            "attribution",
            "optimization-plan",
            "execution",
            "regression-test-design",
        ):
            assert client.get(f"/api/improvements/{improvement_id}/{suffix}").status_code == 404
