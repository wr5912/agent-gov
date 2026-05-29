from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.runtime.errors import FeedbackStoreError


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(FeedbackStoreError)
    async def feedback_store_error_handler(_: Request, exc: FeedbackStoreError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": str(exc),
                "error_code": exc.error_code,
            },
        )
