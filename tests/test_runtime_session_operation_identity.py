from pathlib import Path

from app.routers.error_handlers import register_error_handlers
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway.router import create_runtime_router
from app.runtime_gateway.store import RuntimeRunStore
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_session_create_route_rejects_same_key_with_changed_name_before_provisioning(
    tmp_path: Path,
) -> None:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    intent, owned = store.start_session_creation(
        idempotency_key="session-create-name-bound",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
        workspace_id=f"agent-a--v-{'a' * 64}",
        requested_name=None,
    )
    assert owned is True
    store.record_session_creation_upstream(intent.intent_id, "session-created")
    store.complete_session_creation(intent.intent_id)

    class _NeverProvisioner:
        def require_current_runtime(self, _runtime_agent_id: str):
            raise AssertionError(
                "request identity must be checked before provisioning",
            )

    api = FastAPI()
    register_error_handlers(api)
    api.include_router(
        create_runtime_router(
            client=object(),  # type: ignore[arg-type]
            store=store,
            provisioner=_NeverProvisioner(),  # type: ignore[arg-type]
            model_type="openai_credential",
            credential_id="provider",
            model_name="model",
            model_parameters={},
            require_api_key=lambda: None,
        ),
    )
    headers = {"Idempotency-Key": "session-create-name-bound"}

    with TestClient(api) as client:
        replay = client.post(
            "/api/runtime/sessions/",
            headers=headers,
            json={"agent_id": "runtime-a", "name": None},
        )
        conflict = client.post(
            "/api/runtime/sessions/",
            headers=headers,
            json={"agent_id": "runtime-a", "name": "Different name"},
        )

    assert replay.status_code == 200
    assert replay.json() == {"session_id": "session-created"}
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "RUNTIMESTATECONFLICT"
