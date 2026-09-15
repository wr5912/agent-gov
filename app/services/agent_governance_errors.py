from __future__ import annotations

from app.runtime.errors import FeedbackStoreError


class AgentGovernanceError(FeedbackStoreError):
    """可安全映射到 Agent 治理 HTTP 边界的业务错误。"""

    def __init__(self, status_code: int, detail: str, *, error_code: str | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        if error_code is not None:
            self.error_code = error_code
        elif status_code == 404:
            self.error_code = "NOT_FOUND"
        elif status_code == 409:
            self.error_code = "CONFLICT"
        else:
            self.error_code = "AGENT_GOVERNANCE_ERROR"
