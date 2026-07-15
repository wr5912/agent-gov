from __future__ import annotations

from typing import Any


def pytest_collection_modifyitems(items: list[Any]) -> None:
    for item in items:
        item.user_properties.append(("agentgov_nodeid", item.nodeid))
