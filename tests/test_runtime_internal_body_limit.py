from __future__ import annotations

from app.routers.error_handlers import register_error_handlers
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway.router import _MAX_INTERNAL_BODY_BYTES, create_internal_runtime_router
from app.runtime_gateway.store import RuntimeRunStore
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app(tmp_path) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(
        create_internal_runtime_router(
            store=RuntimeRunStore(make_session_factory(tmp_path / "runtime.db")),
            shared_secret="test-secret",
        ),
    )
    return app


def test_internal_body_limit_rejects_declared_oversize_before_signature(tmp_path) -> None:
    with TestClient(_app(tmp_path)) as client:
        response = client.post(
            "/internal/runtime-receipts",
            content=b"{}",
            headers={"Content-Length": str(_MAX_INTERNAL_BODY_BYTES + 1)},
        )

    assert response.status_code == 413
    assert response.json()["detail"] == "Internal Runtime request body is too large"


def test_internal_body_limit_counts_stream_without_content_length(tmp_path) -> None:
    chunk = b"x" * ((_MAX_INTERNAL_BODY_BYTES // 2) + 1)

    def chunks():
        yield chunk
        yield chunk

    with TestClient(_app(tmp_path)) as client:
        response = client.post(
            "/internal/runtime-receipts",
            content=chunks(),
            headers={"Transfer-Encoding": "chunked"},
        )

    assert response.status_code == 413
    assert response.json()["detail"] == "Internal Runtime request body is too large"
