from __future__ import annotations

import hashlib


def business_agent_instance_etag(provision_completed_token: str) -> str:
    """派生可公开的实例 CAS，不暴露供给事务 token。"""

    return hashlib.sha256(provision_completed_token.encode("utf-8")).hexdigest()
