"""业务 Agent durable deletion 的公开 API 契约。"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.runtime.agent_deletion_db import AgentDeletionOperationModel
from app.runtime.agent_deletion_fs import AgentDeletionFilesystemResult
from app.runtime.agent_governance_schemas import AgentDeleteResponse
from app.runtime.protected_business_agents import SECURITY_OPERATIONS_EXPERT_AGENT_ID
from fastapi.testclient import TestClient

from app_test_utils import load_test_app as _load_app
from workspace_package_test_utils import import_new_agent as _import_new_agent


@pytest.fixture()
def app_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return _load_app(monkeypatch, tmp_path)


def _create(client: TestClient, agent_id: str, name: str = "受测 Agent") -> tuple[Path, str]:
    created = _import_new_agent(client, agent_id=agent_id, name=name)
    assert created.status_code == 200, created.text
    agent = created.json()["agent"]
    return Path(agent["workspace_dir"]), str(agent["instance_etag"])


def _delete(
    client: TestClient,
    agent_id: str,
    instance_etag: str,
    *,
    key: str | None = None,
):
    return client.request(
        "DELETE",
        f"/api/agent-registry/{agent_id}",
        headers={
            "If-Match": f'"{instance_etag}"',
            "Idempotency-Key": key or f"agent-delete:{instance_etag}",
        },
    )


def test_delete_purges_whole_layout_and_permanently_reserves_public_id(app_module) -> None:
    with TestClient(app_module.app) as client:
        workspace, instance_etag = _create(client, "probe-agent")
        (workspace / "CLAUDE.md").write_text("前一个 Agent 的私有内容\n", encoding="utf-8")

        deleted = _delete(client, "probe-agent", instance_etag)

        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["state"] == "completed"
        assert not workspace.parent.exists()
        replacement = _import_new_agent(client, agent_id="probe-agent", name="同 ID 重建")
        assert replacement.status_code == 409
        assert replacement.json()["error_code"] == "WORKSPACE_AGENT_ID_RESERVED"
        different = _import_new_agent(client, agent_id="different-agent", name="不同 ID")
        assert different.status_code == 200, different.text


def test_delete_response_is_safe_backend_owned_snapshot(app_module) -> None:
    with TestClient(app_module.app) as client:
        _, instance_etag = _create(client, "outcome-agent")
        response = _delete(client, "outcome-agent", instance_etag)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "operation_id",
        "state",
        "deleted",
        "impact",
        "workspace_removed",
        "cleanup_complete",
        "last_error_code",
        "attempt_count",
        "updated_at",
    }
    assert body["deleted"]["agent_id"] == "outcome-agent"
    assert "workspace_dir" not in body["deleted"]
    assert "instance_etag" not in body["deleted"]
    assert instance_etag not in response.text
    assert body["workspace_removed"] is True
    assert body["cleanup_complete"] is True
    assert body["last_error_code"] is None
    assert body["attempt_count"] >= 1
    assert body["updated_at"]


def test_delete_response_model_rejects_missing_or_inconsistent_cleanup_claims() -> None:
    payload = {
        "operation_id": "adop-contract",
        "state": "cleanup_pending",
        "deleted": {
            "agent_id": "contract-agent",
            "name": "Contract Agent",
            "category": "business",
            "created_at": "2026-08-09T00:00:00+00:00",
            "status": "active",
            "builtin": False,
            "default": False,
            "protected": False,
            "requires_web_hitl": False,
        },
        "impact": {
            "runs": 0,
            "feedback_signals": 0,
            "improvements": 0,
            "test_runs": 0,
            "change_sets": 0,
            "releases": 0,
        },
        "last_error_code": "AGENT_DELETION_FILESYSTEM_FENCE",
        "attempt_count": 1,
        "updated_at": "2026-08-09T00:00:01+00:00",
    }
    with pytest.raises(ValueError, match="workspace_removed"):
        AgentDeleteResponse.model_validate(payload)
    with pytest.raises(ValueError, match="durable operation state"):
        AgentDeleteResponse.model_validate({**payload, "workspace_removed": True, "cleanup_complete": True})
    with pytest.raises(ValueError, match="durable operation state"):
        AgentDeleteResponse.model_validate(
            {
                **payload,
                "state": "completed",
                "workspace_removed": False,
                "cleanup_complete": False,
            }
        )


def test_delete_retry_converges_on_same_operation(app_module) -> None:
    with TestClient(app_module.app) as client:
        _, instance_etag = _create(client, "retry-agent")
        first = _delete(client, "retry-agent", instance_etag)
        retry = _delete(client, "retry-agent", instance_etag)
        status_response = client.get(f"/api/agent-deletion-operations/{first.json()['operation_id']}")

    assert first.status_code == retry.status_code == 200
    assert first.json() == retry.json()
    assert status_response.status_code == 200
    assert status_response.json() == first.json()
    assert instance_etag not in status_response.text


def test_delete_reports_cleanup_pending_without_claiming_disk_removal(
    app_module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.business_agent_deletion.purge_quarantined_agent_layout",
        lambda **_: AgentDeletionFilesystemResult(
            state="cleanup_pending",
            error_code="AGENT_DELETION_FILESYSTEM_FENCE",
        ),
    )
    with TestClient(app_module.app) as client:
        _, instance_etag = _create(client, "pending-agent")
        response = _delete(client, "pending-agent", instance_etag)

        location = response.headers.get("location")
        assert location == f"/api/agent-deletion-operations/{response.json()['operation_id']}"
        status_response = client.get(location)

    assert response.status_code == 202
    assert response.json()["state"] == "cleanup_pending"
    assert response.json()["workspace_removed"] is False
    assert response.json()["cleanup_complete"] is False
    assert response.json()["last_error_code"] == "AGENT_DELETION_FILESYSTEM_FENCE"
    assert response.json()["attempt_count"] >= 1
    assert response.json()["updated_at"]
    assert status_response.status_code == 200
    assert status_response.json() == response.json()
    assert instance_etag not in status_response.text


def test_deletion_status_requires_api_key_and_unknown_operation_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_module = _load_app(monkeypatch, tmp_path, api_key="status-secret")
    with TestClient(app_module.app) as client:
        unauthorized = client.get("/api/agent-deletion-operations/adop-unknown")
        unauthorized_list = client.get("/api/agent-deletion-operations")
        missing = client.get(
            "/api/agent-deletion-operations/adop-unknown",
            headers={"Authorization": "Bearer status-secret"},
        )
        listed = client.get(
            "/api/agent-deletion-operations?state=cleanup_pending&limit=1",
            headers={"Authorization": "Bearer status-secret"},
        )
        invalid_state = client.get(
            "/api/agent-deletion-operations?state=unknown",
            headers={"Authorization": "Bearer status-secret"},
        )
        invalid_limit = client.get(
            "/api/agent-deletion-operations?limit=101",
            headers={"Authorization": "Bearer status-secret"},
        )

    assert unauthorized.status_code == 401
    assert unauthorized.json()["error_code"] == "UNAUTHORIZED"
    assert unauthorized_list.status_code == 401
    assert unauthorized_list.json()["error_code"] == "UNAUTHORIZED"
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "AGENT_DELETION_OPERATION_NOT_FOUND"
    assert set(missing.json()) == {"detail", "error_code"}
    assert listed.status_code == 200
    assert listed.json() == []
    assert invalid_state.status_code == 422
    assert invalid_limit.status_code == 422


def test_deletion_discovery_recovers_pending_receipt_without_internal_evidence(
    app_module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.business_agent_deletion.purge_quarantined_agent_layout",
        lambda **_: AgentDeletionFilesystemResult(
            state="cleanup_pending",
            error_code="AGENT_DELETION_FILESYSTEM_FENCE",
        ),
    )
    with TestClient(app_module.app) as client:
        _, instance_etag = _create(client, "discover-pending-agent")
        deletion = _delete(client, "discover-pending-agent", instance_etag)
        discovery = client.get("/api/agent-deletion-operations?state=cleanup_pending&limit=1")

    assert deletion.status_code == 202
    assert discovery.status_code == 200
    assert discovery.json() == [deletion.json()]
    assert discovery.json()[0]["last_error_code"] == "AGENT_DELETION_FILESYSTEM_FENCE"
    assert instance_etag not in discovery.text
    for forbidden in ("workspace_dir", "quarantine_path", "provision_token", "inode", "mount_id"):
        assert forbidden not in discovery.text


def test_deletion_status_and_discovery_scrub_hostile_persisted_error_evidence(
    app_module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.business_agent_deletion.purge_quarantined_agent_layout",
        lambda **_: AgentDeletionFilesystemResult(state="cleanup_pending", error_code="AGENT_DELETION_FILESYSTEM_FENCE"),
    )
    private_path = "/data/business-agents/private/workspace"
    with TestClient(app_module.app) as client:
        _, instance_etag = _create(client, "hostile-error-agent")
        deletion = _delete(client, "hostile-error-agent", instance_etag)
        operation_id = deletion.json()["operation_id"]
        with app_module.agent_deletion_store._session_factory() as db:
            row = db.get(AgentDeletionOperationModel, operation_id)
            assert row is not None
            row.error_json = {
                "error_code": private_path,
                "workspace_dir": private_path,
                "expected_inode": 12345,
            }
            db.commit()
        status_response = client.get(f"/api/agent-deletion-operations/{operation_id}")
        discovery = client.get("/api/agent-deletion-operations?state=cleanup_pending&limit=20")

    assert status_response.status_code == 200
    assert status_response.json()["last_error_code"] is None
    assert discovery.status_code == 200
    assert discovery.json()[0]["last_error_code"] is None
    assert private_path not in status_response.text
    assert private_path not in discovery.text
    for forbidden in ("workspace_dir", "quarantine_path", "expected_inode", "provision_token"):
        assert forbidden not in status_response.text
        assert forbidden not in discovery.text


@pytest.mark.parametrize(
    "if_match",
    [
        None,
        "a" * 64,
        f'W/"{"a" * 64}"',
        "*",
        f'"{"a" * 64}", "{"b" * 64}"',
        '"short"',
    ],
    ids=["missing", "bare", "weak", "wildcard", "multiple", "wrong-length"],
)
def test_delete_requires_one_strong_quoted_entity_tag(app_module, if_match: str | None) -> None:
    with TestClient(app_module.app) as client:
        _, _ = _create(client, "etag-agent")
        headers = {"Idempotency-Key": "delete-etag-agent"}
        if if_match is not None:
            headers["If-Match"] = if_match
        response = client.request("DELETE", "/api/agent-registry/etag-agent", headers=headers)

    assert response.status_code == 409
    assert response.json()["error_code"] == "AGENT_DELETION_PRECONDITION"


def test_delete_requires_idempotency_key(app_module) -> None:
    with TestClient(app_module.app) as client:
        _, instance_etag = _create(client, "missing-key-agent")
        response = client.request(
            "DELETE",
            "/api/agent-registry/missing-key-agent",
            headers={"If-Match": f'"{instance_etag}"'},
        )

    assert response.status_code == 409
    assert response.json()["error_code"] == "AGENT_DELETION_PRECONDITION"


def test_delete_rejects_wrong_instance_and_cross_agent_idempotency_reuse(app_module) -> None:
    with TestClient(app_module.app) as client:
        _, first_etag = _create(client, "first-agent")
        _, second_etag = _create(client, "second-agent")
        wrong = _delete(client, "first-agent", "f" * 64, key="wrong-instance")
        first = _delete(client, "first-agent", first_etag, key="shared-delete-key")
        reused = _delete(client, "second-agent", second_etag, key="shared-delete-key")

    assert wrong.status_code == 409
    assert wrong.json()["error_code"] == "AGENT_DELETION_INSTANCE_MISMATCH"
    assert first.status_code == 200
    assert reused.status_code == 409
    assert reused.json()["error_code"] == "AGENT_DELETION_IDEMPOTENCY_CONFLICT"


def test_protected_agent_delete_is_rejected(app_module) -> None:
    with TestClient(app_module.app) as client:
        builtin = next(item for item in client.get("/api/agent-registry").json() if item["agent_id"] == SECURITY_OPERATIONS_EXPERT_AGENT_ID)
        response = _delete(client, SECURITY_OPERATIONS_EXPERT_AGENT_ID, builtin["instance_etag"])

    assert response.status_code == 409
    assert response.json()["error_code"] == "AGENT_DELETION_PROTECTED"


def test_deleted_agent_is_not_runnable(app_module) -> None:
    with TestClient(app_module.app) as client:
        _, instance_etag = _create(client, "gone-agent")
        assert _delete(client, "gone-agent", instance_etag).status_code == 200
        response = client.post("/api/chat", json={"message": "hi", "agent_id": "gone-agent"})

    assert response.status_code == 404
