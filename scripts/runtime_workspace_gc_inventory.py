#!/usr/bin/env python3
"""离线维护专用：通过固定 SDK 的公共 Storage 读取引用，不迁移或写入 native DB。"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import re
from pathlib import Path
from typing import Literal, TypedDict

from agentscope.app.storage import AsyncSQLAlchemyStorage

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class NativeSessionReference(TypedDict):
    session_id: str
    workspace_id: str | None


class NativeAgentReference(TypedDict):
    agent_id: str
    present: bool
    sessions: list[NativeSessionReference]


class NativeInventory(TypedDict):
    scope: Literal["reachable_and_explicit_agents"]
    global_complete: Literal[False]
    agents: list[NativeAgentReference]


async def collect_native_inventory(database: Path, extra_agent_ids: set[str]) -> NativeInventory:
    if importlib.metadata.version("agentscope") != "2.0.8":
        raise ValueError("Maintenance inventory requires the pinned AgentScope SDK")
    if not database.is_absolute() or database.is_symlink() or not database.is_file():
        raise ValueError("Native database must be an existing regular file")
    if any(_IDENTIFIER.fullmatch(value) is None for value in extra_agent_ids):
        raise ValueError("Invalid Runtime Agent locator")
    url = f"sqlite+aiosqlite:///{database.as_uri()}?mode=ro&uri=true"
    user_id = "agentgov-runtime"
    async with AsyncSQLAlchemyStorage(url, create_tables=False, auto_migrate=False) as storage:
        agents = await storage.list_agents(user_id)
        agent_ids = {agent.id for agent in agents} | extra_agent_ids
        teams = await storage.list_teams(user_id)
        for team in teams:
            if team.leader_agent_id:
                agent_ids.add(team.leader_agent_id)
            agent_ids.update(team.data.member_ids)
            agent_ids.update(member.agent_id for member in team.data.members)
        rows: list[NativeAgentReference] = []
        for agent_id in sorted(agent_ids):
            agent = await storage.get_agent(user_id, agent_id)
            sessions = await storage.list_sessions(user_id, agent_id)
            rows.append(
                {
                    "agent_id": agent_id,
                    "present": agent is not None,
                    "sessions": [{"session_id": session.id, "workspace_id": session.config.workspace_id} for session in sessions],
                },
            )
    # SDK 无全局 Session 列表；不能把 reachable inventory 冒充 orphan 全量清单。
    return {"scope": "reachable_and_explicit_agents", "global_complete": False, "agents": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--agent-ids", nargs="*", default=[])
    args = parser.parse_args()
    try:
        result = asyncio.run(collect_native_inventory(args.database, set(args.agent_ids)))
    except Exception:
        parser.exit(1, "无法通过固定 SDK 只读枚举 Runtime 引用；未批准回收。\n")
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
