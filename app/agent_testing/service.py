from __future__ import annotations

import logging
import re
import shutil
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_paths import InvalidAgentId, validate_agent_id
from app.runtime.errors import FeedbackStoreError
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db_base import utc_now
from app.runtime.schemas import ChatRequest, ChatResponse

from .runner import FIXED_PYTEST_COMMAND, AgentTestRunner
from .schemas import AgentTestSuiteSummary
from .store import AgentTestingStore, AgentTestRunAlreadyActive
from .suite import inspect_agent_test_suite

_FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
logger = logging.getLogger(__name__)


class AgentTestingError(FeedbackStoreError):
    def __init__(self, status_code: int, error_code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.error_code = error_code
        self.detail = detail


@dataclass(frozen=True)
class _TestSession:
    test_session_id: str
    agent_id: str
    commit_sha: str
    change_set_id: str | None
    checkout: Path
    created_at: str


class AgentTestingService:
    def __init__(
        self,
        *,
        store: AgentTestingStore,
        store_for: Callable[[str], GitAgentVersionStore],
        agent_exists: Callable[[str], bool],
        get_change_set: Callable[[str], JsonObject | None],
        run_candidate: Callable[..., Awaitable[ChatResponse]],
        artifacts_dir: Path,
        api_base_url: str,
        api_key: str | None,
        run_timeout_seconds: int,
    ) -> None:
        self.store = store
        self._store_for = store_for
        self._agent_exists = agent_exists
        self._get_change_set = get_change_set
        self._run_candidate = run_candidate
        self._sessions_dir = artifacts_dir / "sessions"
        self._sessions: dict[str, _TestSession] = {}
        self._sessions_lock = threading.RLock()
        self.runner = AgentTestRunner(
            store=store,
            store_for=store_for,
            artifacts_dir=artifacts_dir / "runs",
            api_base_url=api_base_url,
            api_key=api_key,
            timeout_seconds=run_timeout_seconds,
        )

    def recover(self) -> JsonObject:
        if self._sessions_dir.exists():
            shutil.rmtree(self._sessions_dir, ignore_errors=True)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        return self.runner.recover()

    def close(self) -> None:
        self.runner.close()
        with self._sessions_lock:
            session_ids = list(self._sessions)
        for session_id in session_ids:
            self.delete_session(session_id)

    def inspect_suite(self, agent_id: str, *, commit_sha: str | None = None) -> AgentTestSuiteSummary:
        safe_agent_id = self._require_agent(agent_id)
        store = self._store_for(safe_agent_id)
        resolved = self._resolve_commit(store, commit_sha)
        current = str(store.current_commit_sha() or "")
        if resolved == current:
            return inspect_agent_test_suite(
                store.repository_dir,
                agent_id=safe_agent_id,
                commit_sha=resolved,
            )
        session_id = f"inspect-{uuid.uuid4()}"
        checkout = self._sessions_dir / session_id / "workspace"
        try:
            self.runner.checkout(store=store, commit_sha=resolved, destination=checkout)
            return inspect_agent_test_suite(
                checkout,
                agent_id=safe_agent_id,
                commit_sha=resolved,
            )
        finally:
            self.runner.remove_checkout(store=store, destination=checkout)

    def create_run(
        self,
        *,
        agent_id: str,
        commit_sha: str | None,
        change_set_id: str | None,
        source: str,
    ) -> JsonObject:
        safe_agent_id = self._require_agent(agent_id)
        store = self._store_for(safe_agent_id)
        resolved_commit = self._resolve_commit(store, commit_sha)
        self._validate_change_set_binding(safe_agent_id, resolved_commit, change_set_id)
        suite = self.inspect_suite(safe_agent_id, commit_sha=resolved_commit)
        if not suite.runnable:
            raise AgentTestingError(
                422,
                "AGENT_TEST_SUITE_NOT_RUNNABLE",
                "Workspace tests/ must contain parseable, flat test_*.py files before a platform test run can start.",
            )
        try:
            run = self.store.create_run(
                agent_id=safe_agent_id,
                commit_sha=resolved_commit,
                change_set_id=change_set_id,
                source=source,
                command=FIXED_PYTEST_COMMAND,
                suite=suite.model_dump(mode="json"),
                suite_digest=suite.suite_digest,
            )
        except AgentTestRunAlreadyActive as exc:
            raise AgentTestingError(
                409,
                "AGENT_TEST_RUN_ALREADY_ACTIVE",
                f"An active platform test run already exists for this exact target: {exc.test_run_id}",
            ) from exc
        self.runner.enqueue(str(run["test_run_id"]))
        return run

    def create_change_set_run(self, change_set_id: str) -> JsonObject:
        change_set = self._get_change_set(change_set_id)
        if change_set is None:
            raise AgentTestingError(404, "CHANGE_SET_NOT_FOUND", f"待发布变更不存在：{change_set_id}")
        agent_id = str(change_set.get("agent_id") or "")
        commit_sha = str(change_set.get("candidate_commit_sha") or "")
        if not agent_id or not commit_sha:
            raise AgentTestingError(
                409,
                "CHANGE_SET_TEST_TARGET_UNAVAILABLE",
                "待发布变更尚未形成可测试的待发布 commit。",
            )
        return self.create_run(
            agent_id=agent_id,
            commit_sha=commit_sha,
            change_set_id=change_set_id,
            source="release_check",
        )

    def create_session(self, *, agent_id: str, commit_sha: str | None, change_set_id: str | None) -> JsonObject:
        safe_agent_id = self._require_agent(agent_id)
        store = self._store_for(safe_agent_id)
        resolved = self._resolve_commit(store, commit_sha)
        self._validate_change_set_binding(safe_agent_id, resolved, change_set_id)
        session_id = f"ats-{uuid.uuid4()}"
        checkout = self._sessions_dir / session_id / "workspace"
        self.runner.checkout(store=store, commit_sha=resolved, destination=checkout)
        session = _TestSession(session_id, safe_agent_id, resolved, change_set_id, checkout, utc_now())
        with self._sessions_lock:
            self._sessions[session_id] = session
        return self._session_payload(session)

    async def invoke(self, test_session_id: str, *, message: str, metadata: JsonObject) -> ChatResponse:
        session = self._get_session(test_session_id)
        request = ChatRequest(
            message=message,
            session_id=f"agent-test-{test_session_id}",
            agent_id=session.agent_id,
            metadata={
                **metadata,
                "source": "agent_workspace_pytest",
                "test_session_id": test_session_id,
                "tested_commit_sha": session.commit_sha,
            },
        )
        return await self._run_candidate(
            request,
            worktree_path=session.checkout,
            candidate_commit_sha=session.commit_sha,
            change_set_id=session.change_set_id or test_session_id,
            agent_id=session.agent_id,
        )

    def delete_session(self, test_session_id: str) -> None:
        with self._sessions_lock:
            session = self._sessions.pop(test_session_id, None)
        if session is None:
            return
        self.runner.remove_checkout(store=self._store_for(session.agent_id), destination=session.checkout)

    def record_import(self, *, agent_id: str, action: str, package_sha256: str, tree_sha256: str, commit_sha: str) -> tuple[str, AgentTestSuiteSummary]:
        suite = self.inspect_suite(agent_id, commit_sha=commit_sha)
        import_id = self.store.record_import(
            agent_id=agent_id,
            action=action,
            package_sha256=package_sha256,
            tree_sha256=tree_sha256,
            commit_sha=commit_sha,
            suite=suite.model_dump(mode="json"),
        )
        return import_id, suite

    def record_import_failure(
        self,
        *,
        agent_id: str,
        action: str,
        package_sha256: str | None,
        tree_sha256: str | None,
        error_code: str,
        detail: str,
    ) -> str:
        return self.store.record_import_failure(
            agent_id=agent_id,
            action=action,
            package_sha256=package_sha256,
            tree_sha256=tree_sha256,
            error={"error_code": error_code, "detail": detail},
        )

    def latest_passed_for_commit(self, *, agent_id: str, commit_sha: str) -> JsonObject | None:
        passed = self.store.latest_passed_for_commit(agent_id=agent_id, commit_sha=commit_sha)
        if passed is None:
            return None
        try:
            suite = self.inspect_suite(agent_id, commit_sha=commit_sha)
        except Exception:
            logger.warning(
                "Failed to inspect current Agent test suite while evaluating publication gate",
                extra={"agent_id": agent_id, "commit_sha": commit_sha},
                exc_info=True,
            )
            return None
        if not suite.runnable or not suite.suite_digest or suite.suite_digest != passed.get("suite_digest"):
            return None
        return passed

    def _require_agent(self, agent_id: str) -> str:
        try:
            safe_agent_id = validate_agent_id(agent_id)
        except InvalidAgentId as exc:
            raise AgentTestingError(422, "AGENT_ID_INVALID", str(exc)) from exc
        if not self._agent_exists(safe_agent_id):
            raise AgentTestingError(404, "AGENT_NOT_FOUND", f"Business Agent not found: {safe_agent_id}")
        return safe_agent_id

    @staticmethod
    def _resolve_commit(store: GitAgentVersionStore, requested: str | None) -> str:
        store.ensure_bootstrap()
        if requested is None:
            current = str(store.current_commit_sha() or "")
            if not current:
                raise AgentTestingError(409, "AGENT_COMMIT_UNAVAILABLE", "Business Agent has no current commit")
            return current
        normalized = requested.strip().lower()
        if not _FULL_COMMIT_RE.fullmatch(normalized):
            raise AgentTestingError(422, "AGENT_COMMIT_INVALID", "commit_sha must be a full 40-character Git commit SHA")
        try:
            resolved = store.resolve_commit_sha(normalized)
        except Exception as exc:
            raise AgentTestingError(409, "AGENT_COMMIT_NOT_FOUND", f"Commit is not available in this Agent repository: {normalized}") from exc
        if resolved != normalized:
            raise AgentTestingError(409, "AGENT_COMMIT_NOT_FOUND", f"Commit is not available in this Agent repository: {normalized}")
        return resolved

    def _validate_change_set_binding(self, agent_id: str, commit_sha: str, change_set_id: str | None) -> None:
        if not change_set_id:
            return
        change_set = self._get_change_set(change_set_id)
        if change_set is None:
            raise AgentTestingError(404, "CHANGE_SET_NOT_FOUND", f"待发布变更不存在：{change_set_id}")
        if str(change_set.get("agent_id") or "") != agent_id or str(change_set.get("candidate_commit_sha") or "") != commit_sha:
            raise AgentTestingError(409, "CHANGE_SET_COMMIT_MISMATCH", "待发布变更、业务 Agent 与测试 commit 不匹配。")

    def _get_session(self, test_session_id: str) -> _TestSession:
        with self._sessions_lock:
            session = self._sessions.get(test_session_id)
        if session is None:
            raise AgentTestingError(404, "AGENT_TEST_SESSION_NOT_FOUND", "Agent test session does not exist or was interrupted by a service restart.")
        return session

    @staticmethod
    def _session_payload(session: _TestSession) -> JsonObject:
        return {
            "test_session_id": session.test_session_id,
            "agent_id": session.agent_id,
            "commit_sha": session.commit_sha,
            "change_set_id": session.change_set_id,
            "created_at": session.created_at,
        }
