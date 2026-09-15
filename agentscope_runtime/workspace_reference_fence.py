"""Coordinate native Session reference writes with Workspace reclamation."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from agentgov_agentscope_contract import version_workspace_id
from agentscope.app.storage import StorageBase

ReferenceHint = tuple[str, str, str, str]


@dataclass(frozen=True)
class _ReferenceJournal:
    """Conservative deletion eligibility plus public Session lookup hints."""

    reclaimable_workspace_ids: frozenset[str]
    hints: frozenset[ReferenceHint]


class WorkspaceQuarantineStateError(RuntimeError):
    """Report that quarantine moved the target before durable bookkeeping failed."""

    def __init__(self) -> None:
        super().__init__("Workspace quarantine failed after moving the exact target")
        self.target_moved = True


class SessionWorkspaceReferenceFence:
    """Serialize Session upserts and retire quarantined Workspace identities."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._retired: set[str] = set()
        self._all_blocked = False

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        """Hold the outer reference lock; manager locks are always inner."""

        async with self._lock:
            yield

    def require_writable(self, workspace_id: str) -> None:
        """Reject a Session write once its exact Workspace was quarantined."""

        if self._all_blocked or workspace_id in self._retired:
            raise RuntimeError("The Runtime workspace identity has been retired")

    def retire(self, workspace_id: str) -> None:
        self._retired.add(workspace_id)

    def activate(self, workspace_id: str) -> None:
        self._retired.discard(workspace_id)

    def block(self, workspace_id: str | None) -> None:
        if workspace_id is None:
            self._all_blocked = True
        else:
            self.retire(workspace_id)


class NativeSessionWorkspaceReferences:
    """Read durable public Session references without private storage access.

    AgentScope intentionally omits ``source='team'`` agents from
    ``list_agents``. The journal records public ``upsert_session`` identities
    before their database commit, then confirms every hint through public
    ``get_session``. Only Workspaces created while this journal is healthy are
    eligible for orphan deletion: a missing/corrupt/legacy journal therefore
    leaks safely instead of treating missing hints as proof of absence.
    """

    def __init__(self, workspaces_root: Path) -> None:
        self._storage: StorageBase | None = None
        self._journal = workspaces_root / ".agentgov-native-session-references.json"
        self._journal_lock = asyncio.Lock()

    def bind_storage(self, storage: StorageBase) -> None:
        self._storage = storage

    async def reserve(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str,
    ) -> None:
        """Persist a discovery hint before the authoritative Session commit."""

        hint = (user_id, agent_id, session_id, workspace_id)
        async with self._journal_lock:
            journal = self._load_journal_locked()
            if hint in journal.hints:
                return
            self._write_journal_locked(
                _ReferenceJournal(
                    reclaimable_workspace_ids=journal.reclaimable_workspace_ids,
                    hints=journal.hints | {hint},
                ),
            )

    async def confirm_reclaimable_binding(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str,
    ) -> None:
        """Atomically publish one validated lookup hint and its GC eligibility."""

        hint = (user_id, agent_id, session_id, workspace_id)
        async with self._journal_lock:
            journal = self._load_journal_locked()
            if hint in journal.hints and workspace_id in journal.reclaimable_workspace_ids:
                return
            self._write_journal_locked(
                _ReferenceJournal(
                    reclaimable_workspace_ids=journal.reclaimable_workspace_ids | {workspace_id},
                    hints=journal.hints | {hint},
                ),
            )

    async def reclaimable_workspace_ids(self) -> frozenset[str]:
        """Return the conservative allowlist; absence never authorizes delete."""

        async with self._journal_lock:
            return self._load_journal_locked().reclaimable_workspace_ids

    async def snapshot(
        self,
        user_id: str,
        observed: dict[tuple[str, str, str], str],
    ) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str, str], str]]:
        storage = self._require_storage()
        agent_ids = {agent.id for agent in await storage.list_agents(user_id)}
        for team in await storage.list_teams(user_id):
            if team.leader_agent_id:
                agent_ids.add(team.leader_agent_id)
            agent_ids.update(team.data.member_ids)
            agent_ids.update(member.agent_id for member in team.data.members)
        bindings: dict[tuple[str, str], str] = {}
        for agent_id in sorted(agent_ids):
            for session in await storage.list_sessions(user_id, agent_id):
                if session.config.workspace_id:
                    bindings[(session.agent_id, session.id)] = session.config.workspace_id
        async with self._journal_lock:
            journal = self._load_journal_locked()
            hints = set(journal.hints)
            hints.update((owner, agent_id, session_id, workspace_id) for (owner, agent_id, session_id), workspace_id in observed.items())
            normalized: set[tuple[str, str, str, str]] = {hint for hint in hints if hint[0] != user_id}
            for owner, hinted_agent_id, session_id, _ in sorted(hints):
                if owner != user_id:
                    continue
                session = await storage.get_session(owner, hinted_agent_id, session_id)
                if session is None or not session.config.workspace_id:
                    continue
                binding = (session.agent_id, session.id)
                bindings[binding] = session.config.workspace_id
                normalized.add((owner, session.agent_id, session.id, session.config.workspace_id))
            if normalized != hints:
                self._write_journal_locked(
                    _ReferenceJournal(
                        reclaimable_workspace_ids=journal.reclaimable_workspace_ids,
                        hints=frozenset(normalized),
                    ),
                )
        retained = {
            binding: workspace_id
            for binding, workspace_id in observed.items()
            if binding[0] != user_id or bindings.get((binding[1], binding[2])) == workspace_id
        }
        return bindings, retained

    def _load_journal_locked(self) -> _ReferenceJournal:
        if not self._journal.exists() and not self._journal.is_symlink():
            return _ReferenceJournal(frozenset(), frozenset())
        if self._journal.is_symlink() or not self._journal.is_file():
            raise RuntimeError("Runtime Session reference journal is unsafe")
        try:
            payload = json.loads(self._journal.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Runtime Session reference journal is invalid") from exc
        expected_root = {"schema_version", "reclaimable_workspace_ids", "entries"}
        if not isinstance(payload, dict) or set(payload) != expected_root:
            raise RuntimeError("Runtime Session reference journal schema is invalid")
        entries = payload.get("entries")
        workspace_ids = payload.get("reclaimable_workspace_ids")
        if payload.get("schema_version") != 1 or not isinstance(entries, list) or not isinstance(workspace_ids, list):
            raise RuntimeError("Runtime Session reference journal values are invalid")
        if any(not isinstance(workspace_id, str) or not workspace_id for workspace_id in workspace_ids):
            raise RuntimeError("Runtime Session reference journal Workspace IDs are invalid")
        reclaimable = frozenset(workspace_ids)
        if len(reclaimable) != len(workspace_ids):
            raise RuntimeError("Runtime Session reference journal Workspace IDs must be unique")
        hints: set[ReferenceHint] = set()
        expected = {"user_id", "agent_id", "session_id", "workspace_id"}
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != expected:
                raise RuntimeError("Runtime Session reference journal entry is invalid")
            hint = tuple(entry[name] for name in ("user_id", "agent_id", "session_id", "workspace_id"))
            if not all(isinstance(value, str) and value for value in hint):
                raise RuntimeError("Runtime Session reference journal entry values are invalid")
            hints.add(hint)
        if len(hints) != len(entries):
            raise RuntimeError("Runtime Session reference journal entries must be unique")
        return _ReferenceJournal(reclaimable, frozenset(hints))

    def _write_journal_locked(self, journal: _ReferenceJournal) -> None:
        payload = {
            "schema_version": 1,
            "reclaimable_workspace_ids": sorted(journal.reclaimable_workspace_ids),
            "entries": [
                {
                    "user_id": user_id,
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "workspace_id": workspace_id,
                }
                for user_id, agent_id, session_id, workspace_id in sorted(journal.hints)
            ],
        }
        descriptor, name = tempfile.mkstemp(
            prefix=f".{self._journal.name}.",
            suffix=".tmp",
            dir=self._journal.parent,
        )
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view) :]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self._journal)
            self._fsync_directory(self._journal.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary.exists() or temporary.is_symlink():
                if temporary.is_symlink() or not temporary.is_file():
                    raise RuntimeError("Runtime Session reference temporary is unsafe")
                temporary.unlink()

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def require_binding(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str,
    ) -> None:
        if version_workspace_id(workspace_id) == workspace_id or self._storage is None:
            return
        session = await self._storage.get_session(user_id, agent_id, session_id)
        if session is None or session.config.workspace_id != workspace_id:
            raise ValueError("Per-Session Runtime workspace has no matching native Session")

    def _require_storage(self) -> StorageBase:
        if self._storage is None:
            raise RuntimeError("AgentScope storage is unavailable for Workspace reclamation")
        return self._storage
