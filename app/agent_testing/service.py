from __future__ import annotations

import ast
import logging
import re
import shutil
import threading
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session

from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_paths import InvalidAgentId, validate_agent_id
from app.runtime.business_agent_lifecycle import BusinessAgentLifecycleFenceError
from app.runtime.errors import FeedbackStoreError
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db_base import utc_now
from app.runtime.schemas import ChatRequest, ChatResponse

from .execution_contracts import (
    FIXED_PYTEST_COMMAND,
    P0_EXACT_COMMIT_LANE,
    SUPPORTED_RECEIPT_CONTRACTS,
    AgentTestExecutionReceipt,
    canonical_json_digest,
    verify_receipt_integrity,
)
from .materializer import MaterializationError, SourceFingerprint, materialize_git_commit
from .schemas import AgentTestDiagnostic, AgentTestSuiteSummary
from .store import AgentTestingStore, AgentTestRunAlreadyActive
from .suite import inspect_agent_test_suite

_FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_MAX_TEST_SOURCE_BYTES = 512 * 1024
_TEST_SUITE_INSPECTION_UNAVAILABLE = "AGENT_TEST_SUITE_INSPECTION_UNAVAILABLE"
logger = logging.getLogger(__name__)


def _test_file_symbols(tree: ast.Module) -> list[JsonObject]:
    symbols: list[JsonObject] = []
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef):
            symbols.append({"kind": "async_function", "name": node.name, "qualified_name": node.name, "line": node.lineno})
        elif isinstance(node, ast.FunctionDef):
            symbols.append({"kind": "function", "name": node.name, "qualified_name": node.name, "line": node.lineno})
        elif isinstance(node, ast.ClassDef):
            symbols.append({"kind": "class", "name": node.name, "qualified_name": node.name, "line": node.lineno})
            if not node.name.startswith("Test"):
                continue
            for member in node.body:
                if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) or not member.name.startswith("test_"):
                    continue
                kind = "async_function" if isinstance(member, ast.AsyncFunctionDef) else "function"
                symbols.append(
                    {
                        "kind": kind,
                        "name": member.name,
                        "qualified_name": f"{node.name}.{member.name}",
                        "line": member.lineno,
                    }
                )
    return symbols


class AgentTestingError(FeedbackStoreError):
    def __init__(self, status_code: int, error_code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.error_code = error_code
        self.detail = detail


class AgentImportAuditPersistenceError(FeedbackStoreError):
    """Stable internal boundary for an accepted-import audit write failure."""


ImportSuiteStatus = Literal["ready", "warning", "invalid"]
AcceptedImportAction = Literal["created", "overwritten", "unchanged"]


@dataclass(frozen=True)
class PreparedWorkspaceImportAudit:
    import_id: str
    agent_id: str
    action: AcceptedImportAction
    package_sha256: str
    tree_sha256: str
    commit_sha: str
    suite_status: ImportSuiteStatus
    suite: AgentTestSuiteSummary


def _inspection_unavailable_suite(*, agent_id: str, commit_sha: str, cause_code: str) -> AgentTestSuiteSummary:
    return AgentTestSuiteSummary(
        agent_id=agent_id,
        commit_sha=commit_sha,
        tests_directory_present=False,
        readme_present=False,
        test_file_count=0,
        diagnostics=[
            AgentTestDiagnostic(
                level="warning",
                code=_TEST_SUITE_INSPECTION_UNAVAILABLE,
                message=(f"平台无法检查该提交的测试套件；原因码为 {cause_code}，修复后需重新检查并运行平台测试。"),
            ),
            AgentTestDiagnostic(
                level="error",
                code=cause_code,
                message="该提交当前不能形成平台测试证据；未启动任何测试执行。",
            ),
        ],
    )


def _stable_error_code(value: object) -> str:
    candidate = str(value or "").strip()
    return candidate if _ERROR_CODE_RE.fullmatch(candidate) else "AGENT_TEST_SUITE_INSPECTION_FAILED"


def _import_suite_status(suite: AgentTestSuiteSummary) -> ImportSuiteStatus:
    if any(item.level == "error" for item in suite.diagnostics):
        return "invalid"
    if any(item.level == "warning" for item in suite.diagnostics):
        return "warning"
    return "ready"


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
        list_agents: Callable[[], Iterable[object]] | None = None,
        schedule_reader: Callable[[str], JsonObject | None] | None = None,
        schedule_list_reader: Callable[[list[str]], list[JsonObject]] | None = None,
    ) -> None:
        self.store = store
        self._store_for = store_for
        self._agent_exists = agent_exists
        self._get_change_set = get_change_set
        self._run_candidate = run_candidate
        self._list_agents = list_agents or (lambda: ())
        self._schedule_reader = schedule_reader
        self._schedule_list_reader = schedule_list_reader
        self._sessions_dir = artifacts_dir / "sessions"
        self._materializations_dir = artifacts_dir / "materializations"
        self._sessions: dict[str, _TestSession] = {}
        self._sessions_lock = threading.RLock()

    def recover(self) -> JsonObject:
        removed: list[str] = []
        for path in (self._sessions_dir, self._materializations_dir):
            if path.exists():
                shutil.rmtree(path)
                removed.append(path.name)
            path.mkdir(parents=True, exist_ok=True)
        return {"temporary_roots_removed": removed}

    def close(self) -> None:
        with self._sessions_lock:
            session_ids = list(self._sessions)
        for session_id in session_ids:
            self.delete_session(session_id)

    def inspect_suite(
        self,
        agent_id: str,
        *,
        commit_sha: str | None = None,
        require_registered: bool = True,
        candidate_store: GitAgentVersionStore | None = None,
    ) -> AgentTestSuiteSummary:
        safe_agent_id = self._require_agent(agent_id) if require_registered else self._safe_agent_id(agent_id)
        store = candidate_store or self._store_for(safe_agent_id)
        resolved = self._resolve_commit(store, commit_sha)
        materialization_root, workspace = self._new_materialization("inspect")
        try:
            self._materialize(store, resolved, workspace)
            return inspect_agent_test_suite(
                workspace,
                agent_id=safe_agent_id,
                commit_sha=resolved,
            )
        finally:
            shutil.rmtree(materialization_root, ignore_errors=True)

    def create_run(
        self,
        *,
        agent_id: str,
        commit_sha: str | None,
        change_set_id: str | None,
        source: str,
        schedule_id: str | None = None,
        scheduled_for: str | None = None,
    ) -> JsonObject:
        if source == "scheduled" and (not schedule_id or not scheduled_for):
            raise AgentTestingError(
                409,
                "AGENT_TEST_SCHEDULE_BINDING_INVALID",
                "Scheduled test runs require schedule_id and scheduled_for.",
            )
        if source != "scheduled" and (schedule_id is not None or scheduled_for is not None):
            raise AgentTestingError(
                409,
                "AGENT_TEST_SCHEDULE_BINDING_INVALID",
                "Only scheduled test runs may carry schedule provenance.",
            )
        if commit_sha is None:
            raise AgentTestingError(
                422,
                "AGENT_TEST_COMMIT_REQUIRED",
                "Platform test runs require an exact full commit_sha.",
            )
        safe_agent_id = self._require_agent(agent_id)
        store = self._store_for(safe_agent_id)
        with store.mutation_guard():
            return self._create_run_under_guard(
                store=store,
                agent_id=safe_agent_id,
                commit_sha=commit_sha,
                change_set_id=change_set_id,
                source=source,
                schedule_id=schedule_id,
                scheduled_for=scheduled_for,
            )

    def _create_run_under_guard(
        self,
        *,
        store: GitAgentVersionStore,
        agent_id: str,
        commit_sha: str,
        change_set_id: str | None,
        source: str,
        schedule_id: str | None,
        scheduled_for: str | None,
    ) -> JsonObject:
        resolved_commit = self._resolve_commit(store, commit_sha)
        self._validate_change_set_binding(agent_id, resolved_commit, change_set_id)
        materialization_root, workspace = self._new_materialization("enqueue")
        try:
            fingerprint = self._materialize(store, resolved_commit, workspace)
            suite = inspect_agent_test_suite(workspace, agent_id=agent_id, commit_sha=resolved_commit)
            if not suite.runnable:
                raise AgentTestingError(
                    422,
                    "AGENT_TEST_SUITE_NOT_RUNNABLE",
                    "Workspace tests/ must contain parseable, flat test_*.py files before a platform test run can start.",
                )
            if suite.requires_live_agent:
                raise AgentTestingError(
                    422,
                    "AGENT_TEST_LIVE_FIXTURE_REQUIRES_LIVE_LANE",
                    "The exact-commit P0 lane cannot run suites that use the live agent fixture.",
                )
        finally:
            shutil.rmtree(materialization_root, ignore_errors=True)
        try:
            run = self.store.create_run(
                agent_id=agent_id,
                commit_sha=resolved_commit,
                change_set_id=change_set_id,
                source=source,
                command=list(FIXED_PYTEST_COMMAND),
                suite=suite.model_dump(mode="json"),
                suite_digest=suite.suite_digest,
                source_digest=fingerprint.source_digest,
                source_tree_sha=fingerprint.tree_sha,
                schedule_id=schedule_id,
                scheduled_for=scheduled_for,
            )
        except AgentTestRunAlreadyActive as exc:
            raise AgentTestingError(
                409,
                "AGENT_TEST_RUN_ALREADY_ACTIVE",
                f"An active platform test run already exists for this exact target: {exc.test_run_id}",
            ) from exc
        except BusinessAgentLifecycleFenceError as exc:
            raise AgentTestingError(
                409,
                "AGENT_TEST_AGENT_UNAVAILABLE",
                "The business Agent was deleted or fenced before the platform test run could be queued.",
            ) from exc
        return run

    def resolve_current_commit(self, agent_id: str) -> str:
        safe_agent_id = self._require_agent(agent_id)
        return self._resolve_commit(self._store_for(safe_agent_id), None)

    def get_suite_file(self, agent_id: str, *, path: str, commit_sha: str | None = None) -> JsonObject:
        safe_path = path.strip()
        if not safe_path.startswith("tests/") or ".." in Path(safe_path).parts or Path(safe_path).is_absolute():
            raise AgentTestingError(422, "AGENT_TEST_FILE_PATH_INVALID", "path must identify a test file inside Workspace tests/")
        suite = self.inspect_suite(agent_id, commit_sha=commit_sha)
        if safe_path not in suite.test_files:
            raise AgentTestingError(404, "AGENT_TEST_FILE_NOT_FOUND", f"Test file is not part of this suite: {safe_path}")
        materialization_root, workspace = self._new_materialization("source")
        try:
            self._materialize(self._store_for(suite.agent_id), suite.commit_sha, workspace)
            source_path = workspace / safe_path
            try:
                source_bytes = source_path.read_bytes()
            except OSError as exc:
                raise AgentTestingError(
                    404,
                    "AGENT_TEST_FILE_NOT_FOUND",
                    f"Test file not found at commit {suite.commit_sha}: {safe_path}",
                ) from exc
            if len(source_bytes) > _MAX_TEST_SOURCE_BYTES:
                raise AgentTestingError(413, "AGENT_TEST_FILE_TOO_LARGE", f"Test file exceeds {_MAX_TEST_SOURCE_BYTES} bytes")
            try:
                source = source_bytes.decode("utf-8")
                tree = ast.parse(source, filename=safe_path)
            except (UnicodeDecodeError, SyntaxError) as exc:
                raise AgentTestingError(
                    409,
                    "AGENT_TEST_FILE_INVALID",
                    f"Test file is not parseable UTF-8 Python at the selected commit: {exc}",
                ) from exc
        finally:
            shutil.rmtree(materialization_root, ignore_errors=True)
        return {
            "agent_id": suite.agent_id,
            "commit_sha": suite.commit_sha,
            "path": safe_path,
            "content": source,
            "line_count": len(source.splitlines()),
            "symbols": _test_file_symbols(tree),
        }

    def list_run_history(
        self,
        *,
        agent_id: str | None,
        status: str | None,
        source: str | None,
        commit_sha: str | None,
        cursor: str | None,
        limit: int,
    ) -> JsonObject:
        if agent_id:
            self._require_agent(agent_id)
        if commit_sha and not _FULL_COMMIT_RE.fullmatch(commit_sha.strip().lower()):
            raise AgentTestingError(422, "AGENT_COMMIT_INVALID", "commit_sha must be a full 40-character Git commit SHA")
        try:
            items, next_cursor = self.store.list_run_history(
                agent_id=agent_id,
                status=status,
                source=source,
                commit_sha=commit_sha.strip().lower() if commit_sha else None,
                cursor=cursor,
                limit=limit,
            )
        except ValueError as exc:
            raise AgentTestingError(422, "AGENT_TEST_RUN_CURSOR_INVALID", str(exc)) from exc
        return {"items": items, "next_cursor": next_cursor}

    def list_test_assets(self) -> list[JsonObject]:
        records = list(self._list_agents())
        agent_ids = [str(getattr(record, "agent_id", "")) for record in records]
        agent_ids = [agent_id for agent_id in agent_ids if agent_id]
        latest_runs = {str(item["agent_id"]): item for item in self.store.latest_run_summaries(agent_ids)}
        schedule_items = self._schedule_list_reader(agent_ids) if self._schedule_list_reader is not None else []
        schedules = {str(item["agent_id"]): item for item in schedule_items}
        assets: list[JsonObject] = []
        for record in records:
            agent_id = str(getattr(record, "agent_id", ""))
            if not agent_id:
                continue
            suite = self._inspect_asset_suite(agent_id)
            latest = latest_runs.get(agent_id)
            schedule = schedules.get(agent_id)
            if schedule is None and self._schedule_list_reader is None and self._schedule_reader is not None:
                schedule = self._schedule_reader(agent_id)
            assets.append(
                {
                    "agent_id": agent_id,
                    "agent_name": str(getattr(record, "name", agent_id)),
                    "agent_status": str(getattr(record, "status", "active")),
                    "suite": suite.model_dump(mode="json"),
                    "latest_run": latest,
                    "schedule": schedule
                    or {
                        "schedule_id": None,
                        "agent_id": agent_id,
                        "enabled": False,
                        "cron_expression": "0 2 * * *",
                        "timezone": "UTC",
                        "next_run_at": None,
                        "created_at": None,
                        "updated_at": None,
                    },
                }
            )
        return assets

    def _inspect_asset_suite(self, agent_id: str) -> AgentTestSuiteSummary:
        try:
            return self.inspect_suite(agent_id)
        except Exception as exc:
            cause_code = _stable_error_code(exc.error_code) if isinstance(exc, AgentTestingError) else "AGENT_TEST_SUITE_INSPECTION_FAILED"
            try:
                commit_sha = self.resolve_current_commit(agent_id)
            except Exception:
                commit_sha = ""
            logger.warning(
                "Agent test asset inspection is unavailable: agent_id=%s commit_sha=%s error_code=%s",
                agent_id,
                commit_sha,
                cause_code,
            )
            return _inspection_unavailable_suite(
                agent_id=agent_id,
                commit_sha=commit_sha,
                cause_code=cause_code,
            )

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
        checkout.parent.mkdir(parents=True, exist_ok=False)
        self._materialize(store, resolved, checkout)
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
        shutil.rmtree(session.checkout.parent, ignore_errors=True)

    def prepare_import(
        self,
        *,
        agent_id: str,
        action: AcceptedImportAction,
        package_sha256: str,
        tree_sha256: str,
        commit_sha: str,
        candidate_store: GitAgentVersionStore | None = None,
    ) -> PreparedWorkspaceImportAudit:
        try:
            suite = self.inspect_suite(
                agent_id,
                commit_sha=commit_sha,
                require_registered=False,
                candidate_store=candidate_store,
            )
        except Exception as exc:
            cause_code = _stable_error_code(exc.error_code) if isinstance(exc, AgentTestingError) else "AGENT_TEST_SUITE_INSPECTION_FAILED"
            suite = _inspection_unavailable_suite(
                agent_id=agent_id,
                commit_sha=commit_sha,
                cause_code=cause_code,
            )
            logger.warning(
                "Workspace import candidate test suite inspection is unavailable: agent_id=%s action=%s commit_sha=%s error_code=%s",
                agent_id,
                action,
                commit_sha,
                cause_code,
            )
        return PreparedWorkspaceImportAudit(
            import_id=f"awi-{uuid.uuid4()}",
            agent_id=agent_id,
            action=action,
            package_sha256=package_sha256,
            tree_sha256=tree_sha256,
            commit_sha=commit_sha,
            suite_status=_import_suite_status(suite),
            suite=suite,
        )

    def persist_prepared_import(
        self,
        prepared: PreparedWorkspaceImportAudit,
        *,
        db: Session | None = None,
    ) -> None:
        suite_payload = prepared.suite.model_dump(mode="json")
        diagnostics = [item.model_dump(mode="json") for item in prepared.suite.diagnostics]
        try:
            if db is None:
                self.store.record_import(
                    import_id=prepared.import_id,
                    agent_id=prepared.agent_id,
                    action=prepared.action,
                    package_sha256=prepared.package_sha256,
                    tree_sha256=prepared.tree_sha256,
                    commit_sha=prepared.commit_sha,
                    suite=suite_payload,
                    suite_status=prepared.suite_status,
                    diagnostics=diagnostics,
                )
            else:
                self.store.record_import_in_transaction(
                    db,
                    import_id=prepared.import_id,
                    agent_id=prepared.agent_id,
                    action=prepared.action,
                    package_sha256=prepared.package_sha256,
                    tree_sha256=prepared.tree_sha256,
                    commit_sha=prepared.commit_sha,
                    suite=suite_payload,
                    suite_status=prepared.suite_status,
                    diagnostics=diagnostics,
                )
        except Exception as exc:
            raise AgentImportAuditPersistenceError("Accepted Workspace import audit could not be persisted") from exc

    def record_import(
        self,
        *,
        agent_id: str,
        action: AcceptedImportAction,
        package_sha256: str,
        tree_sha256: str,
        commit_sha: str,
    ) -> tuple[str, AgentTestSuiteSummary]:
        prepared = self.prepare_import(
            agent_id=agent_id,
            action=action,
            package_sha256=package_sha256,
            tree_sha256=tree_sha256,
            commit_sha=commit_sha,
        )
        self.persist_prepared_import(prepared)
        return prepared.import_id, prepared.suite

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

    def cancel_run(self, test_run_id: str) -> JsonObject:
        return self.store.request_cancel(test_run_id)

    def latest_passed_for_commit(self, *, agent_id: str, commit_sha: str) -> JsonObject | None:
        passed = self.store.latest_passed_for_commit(agent_id=agent_id, commit_sha=commit_sha)
        if passed is None:
            return None
        try:
            receipt = AgentTestExecutionReceipt.model_validate(passed.get("receipt"))
            if (
                receipt.contract not in SUPPORTED_RECEIPT_CONTRACTS
                or receipt.lane != P0_EXACT_COMMIT_LANE
                or not verify_receipt_integrity(receipt)
                or receipt.test_run_id != passed.get("test_run_id")
                or receipt.worker_id != passed.get("_worker_id")
                or receipt.container_id is None
                or receipt.container_id != passed.get("_container_id")
                or receipt.result.status != "passed"
                or receipt.invocation is None
                or receipt.isolation is None
                or not receipt.cleanup.complete
                or receipt.target.agent_id != agent_id
                or receipt.target.commit_sha != commit_sha
                or receipt.target.suite_digest != passed.get("suite_digest")
                or receipt.target.source_digest != passed.get("source_digest")
                or receipt.target.source_observation != "stable"
                or receipt.target.pre_source_digest != receipt.target.source_digest
                or receipt.target.post_source_digest != receipt.target.source_digest
                or receipt.target.tree_sha != passed.get("source_tree_sha")
                or list(receipt.invocation.argv) != list(passed.get("command") or [])
                or receipt.assurance_level != "execution_provenance"
                or receipt.result.workspace_report_authority != "agent_owned_unverified"
                or receipt.result.workspace_report_digest != canonical_json_digest(passed.get("report") or {})
                or receipt.result.stdout_digest != canonical_json_digest(str(passed.get("stdout") or ""))
                or receipt.result.stderr_digest != canonical_json_digest(str(passed.get("stderr") or ""))
            ):
                return None
            materialization_root, workspace = self._new_materialization("gate")
            try:
                fingerprint = self._materialize(self._store_for(agent_id), commit_sha, workspace)
                suite = inspect_agent_test_suite(workspace, agent_id=agent_id, commit_sha=commit_sha)
            finally:
                shutil.rmtree(materialization_root, ignore_errors=True)
        except Exception:
            logger.warning(
                "Failed to validate Agent test receipt while evaluating publication gate",
                extra={"agent_id": agent_id, "commit_sha": commit_sha},
                exc_info=True,
            )
            return None
        if (
            not suite.runnable
            or suite.requires_live_agent
            or not suite.suite_digest
            or suite.suite_digest != passed.get("suite_digest")
            or fingerprint.source_digest != passed.get("source_digest")
            or fingerprint.tree_sha != passed.get("source_tree_sha")
            or receipt.target.pre_source_digest != fingerprint.source_digest
            or receipt.target.post_source_digest != fingerprint.source_digest
        ):
            return None
        return passed

    def _require_agent(self, agent_id: str) -> str:
        safe_agent_id = self._safe_agent_id(agent_id)
        if not self._agent_exists(safe_agent_id):
            raise AgentTestingError(404, "AGENT_NOT_FOUND", f"Business Agent not found: {safe_agent_id}")
        return safe_agent_id

    @staticmethod
    def _safe_agent_id(agent_id: str) -> str:
        try:
            safe_agent_id = validate_agent_id(agent_id)
        except InvalidAgentId as exc:
            raise AgentTestingError(422, "AGENT_ID_INVALID", str(exc)) from exc
        return safe_agent_id

    @staticmethod
    def _resolve_commit(store: GitAgentVersionStore, requested: str | None) -> str:
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

    def _new_materialization(self, purpose: str) -> tuple[Path, Path]:
        self._materializations_dir.mkdir(parents=True, exist_ok=True)
        root = self._materializations_dir / f"{purpose}-{uuid.uuid4()}"
        root.mkdir()
        return root, root / "workspace"

    @staticmethod
    def _materialize(store: GitAgentVersionStore, commit_sha: str, destination: Path) -> SourceFingerprint:
        try:
            return materialize_git_commit(store.repository_dir, commit_sha, destination)
        except MaterializationError as exc:
            raise AgentTestingError(422, exc.code, str(exc)) from exc

    @staticmethod
    def _session_payload(session: _TestSession) -> JsonObject:
        return {
            "test_session_id": session.test_session_id,
            "agent_id": session.agent_id,
            "commit_sha": session.commit_sha,
            "change_set_id": session.change_set_id,
            "created_at": session.created_at,
        }
