"""后台治理任务和候选测试的 AgentScope 非流式执行桥接。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_job_types import FormatterOutputModel, agent_job_spec
from app.runtime.json_types import JsonObject
from app.runtime.schemas import ChatRequest, ChatResponse
from app.runtime.settings import AppSettings

from ._execution_support import (
    _awaiting_restart_cutoff,
    _candidate_source_id,
    _canonical_assistant_result,
    _ephemeral_runtime_name,
    _ExecutionResourcePlan,
    _governed_evidence_root,
    _iter_sse_events,
    _json_digest,
    _messages_from_body,
    _ObservedExecution,
    _parse_structured_output,
    _reject_symlinks,
    _requires_interactive_continuation,
    _requires_runtime_restart,
    _stable_session_uuid,
    _user_message,
    _with_json_contract,
)
from .client import AgentScopeRuntimeClient, RuntimeUpstreamError
from .contracts import (
    GOVERNED_EVIDENCE_ROOT_METADATA_KEY,
    TERMINAL_RUN_STATUSES,
    AgentRunResponse,
    RunStatus,
)
from .harness_snapshots import PublishedHarnessSnapshotStore
from .provisioning import agent_payload_from_workspace, session_settings_from_workspace
from .store import RuntimeRunStore, RuntimeStateConflict, harness_digest


@dataclass(frozen=True)
class _ExecutionResource:
    cache_key: str
    business_agent_id: str
    version_id: str
    harness_digest: str
    source_id: str
    source_root: Path
    version_owner_id: str
    source_kind: str
    runtime_agent_id: str
    session_id: str
    workspace_id: str


class AgentScopeExecutionService:
    """用 AgentScope 原生 Session/Chat/SSE 实现非流式后台调用。"""

    def __init__(
        self,
        *,
        settings: AppSettings,
        client: AgentScopeRuntimeClient,
        store: RuntimeRunStore,
        version_store_for: Callable[[str], GitAgentVersionStore],
        snapshot_store: PublishedHarnessSnapshotStore,
    ) -> None:
        self.settings = settings
        self.client = client
        self.store = store
        self.version_store_for = version_store_for
        self.snapshot_store = snapshot_store
        self._resources: dict[str, _ExecutionResource] = {}
        self._lock = asyncio.Lock()

    async def run_candidate(
        self,
        req: ChatRequest,
        *,
        worktree_path: Path,
        candidate_commit_sha: str,
        change_set_id: str,
        agent_id: str,
    ) -> ChatResponse:
        """在独立候选 Agent/Session 中运行，并按测试 Session 保留多轮上下文。"""

        external_session_id = req.session_id or f"candidate-{uuid.uuid4()}"
        cache_key = f"candidate:{external_session_id}"
        resource = await self._ensure_resource(
            cache_key=cache_key,
            business_agent_id=agent_id,
            version_id=candidate_commit_sha,
            workspace=worktree_path,
            display_name=f"{agent_id} candidate {change_set_id}",
            candidate_version_store=self.version_store_for(agent_id),
        )
        return await self._run_resource(resource, req)

    async def release_candidate(self, external_session_id: str) -> None:
        """显式销毁一个候选测试 Session 及其临时 Agent/Workspace。"""

        await self._release_key(f"candidate:{external_session_id}")

    async def run_profile_json(
        self,
        *,
        profile_name: str,
        prompt: str,
        job_type: str,
        job_input: JsonObject,
        governor: JsonObject | None = None,
        trace_callback: Any | None = None,
    ) -> FormatterOutputModel:
        """运行治理 Agent，并对它在同一 AgentScope run 中返回的 JSON 做严格校验。"""

        del profile_name
        cache_key = f"governor:{uuid.uuid4()}"
        digest = harness_digest(self.settings.governor_workspace_dir)
        version_id = f"governor-{digest}-{uuid.uuid4().hex}"
        resource = await self._ensure_resource(
            cache_key=cache_key,
            business_agent_id="governor",
            version_id=version_id,
            workspace=self.settings.governor_workspace_dir,
            display_name="AgentGov Governor",
        )
        request = ChatRequest(
            message=_with_json_contract(prompt, job_type),
            session_id=cache_key,
            agent_id="governor",
            metadata={
                **(governor or {}),
                "job_type": job_type,
                "job_input_digest": _json_digest(job_input),
                "source": "agentgov_governance",
            },
        )
        try:
            response = await self._run_resource(
                resource,
                request,
                governed_evidence_root=_governed_evidence_root(job_input),
            )
            if trace_callback is not None:
                trace_callback(
                    {
                        "run_id": response.run_id,
                        "trace_id": response.trace_id or "",
                        "trace_url": response.trace_url or "",
                    },
                )
            return _parse_structured_output(job_type, response.answer)
        finally:
            await self._release_key(cache_key)

    async def format_agent_text(
        self,
        *,
        job_type: str,
        raw_text: str,
        job_input: JsonObject,
    ) -> FormatterOutputModel:
        """把轻量反馈整理也统一交给 AgentScope governor，API 不直连模型。"""

        spec = agent_job_spec(job_type)
        normalized_input = dict(job_input)
        normalized_input.setdefault("raw_feedback", raw_text)
        return await self.run_profile_json(
            profile_name=spec.profile_name,
            prompt=spec.prompt_builder(normalized_input),
            job_type=spec.job_type.value,
            job_input=normalized_input,
        )

    async def close(self) -> None:
        """在 API 退出时尽力回收所有候选与治理临时资源。"""

        async with self._lock:
            keys = list(self._resources)
        for key in keys:
            try:
                await self._release_key(key)
            except Exception:
                # 退出阶段不掩盖原始 shutdown；durable ledger 会在下次启动续清。
                continue

    async def reconcile_ephemeral_resources(self, *, include_ready: bool) -> int:
        """重启/周期恢复无进程内对象的临时 Runtime 资源。"""

        cleaned = 0
        for row in self.store.recoverable_ephemeral_resources(
            include_ready=include_ready,
            awaiting_restart_before=_awaiting_restart_cutoff(
                include_ready,
                self.settings.agent_test_run_timeout_seconds,
            ),
        ):
            try:
                await self._release_key(row.cache_key)
            except Exception:
                continue
            cleaned += 1
        return cleaned

    async def _ensure_resource(
        self,
        *,
        cache_key: str,
        business_agent_id: str,
        version_id: str,
        workspace: Path,
        display_name: str,
        candidate_version_store: GitAgentVersionStore | None = None,
    ) -> _ExecutionResource:
        async with self._lock:
            existing = self._resources.get(cache_key)
            if existing is not None:
                if existing.version_id != version_id or existing.business_agent_id != business_agent_id:
                    raise RuntimeStateConflict("Candidate Session cannot be rebound to another Agent version")
                return existing
            plan = self._resource_plan(
                cache_key=cache_key,
                business_agent_id=business_agent_id,
                version_id=version_id,
                workspace=workspace,
                display_name=display_name,
                candidate_version_store=candidate_version_store,
            )
            ledger = self.store.start_ephemeral_resource(
                cache_key=plan.cache_key,
                business_agent_id=plan.business_agent_id,
                version_owner_id=plan.version_owner_id,
                agent_version_id=plan.version_id,
                digest=plan.harness_digest,
                source_id=plan.source_id,
                source_kind=plan.source_kind,
                workspace_id=plan.workspace_id,
            )
            source_root = plan.source_root
            stage = "materialize_source"
            try:
                source_root = await self._materialize_source(plan, workspace, candidate_version_store)
                permission_mode, cwd, model_profile = session_settings_from_workspace(source_root / "workspace")
                if model_profile != "default":
                    raise RuntimeStateConflict("Candidate requested an unavailable model profile")
                stage = "create_runtime_agent"
                runtime_agent_id = await self._ensure_runtime_agent(plan, source_root, ledger.runtime_agent_id)
                stage = "bind_runtime_agent"
                self.store.bind_agent_version(
                    agent_id=plan.version_owner_id,
                    agent_version_id=plan.version_id,
                    digest=plan.harness_digest,
                    runtime_agent_id=runtime_agent_id,
                    governance_agent_id=plan.business_agent_id,
                    source_kind=plan.source_kind,
                    source_id=plan.source_id,
                )
                stage = "create_runtime_session"
                session_id = await self._ensure_runtime_session(plan, runtime_agent_id, ledger.session_id)
                stage = "configure_runtime_session"
                resource = await self._configure_execution_resource(
                    plan,
                    source_root=source_root,
                    runtime_agent_id=runtime_agent_id,
                    session_id=session_id,
                    permission_mode=permission_mode,
                    cwd=cwd,
                )
                self._resources[cache_key] = resource
                return resource
            except Exception as exc:
                restart_required = await self._record_resource_failure(plan, source_root, stage, exc)
                if restart_required:
                    raise RuntimeStateConflict(
                        "Candidate subagent templates are prepared; restart AgentScope Runtime and retry the same Session",
                    ) from exc
                raise

    def _resource_plan(
        self,
        *,
        cache_key: str,
        business_agent_id: str,
        version_id: str,
        workspace: Path,
        display_name: str,
        candidate_version_store: GitAgentVersionStore | None,
    ) -> _ExecutionResourcePlan:
        digest = harness_digest(workspace)
        if not digest:
            raise RuntimeStateConflict("Candidate Harness digest is unavailable")
        if candidate_version_store is not None:
            identity = self.snapshot_store.candidate_identity(
                agent_id=business_agent_id,
                agent_version_id=version_id,
                expected_digest=digest,
                isolation_key=cache_key,
            )
            source_id = identity.source_id
            source_kind = "candidate_snapshot"
        else:
            source_id = _candidate_source_id(cache_key, business_agent_id, version_id, digest)
            source_kind = "staged"
        return _ExecutionResourcePlan(
            cache_key=cache_key,
            business_agent_id=business_agent_id,
            version_id=version_id,
            harness_digest=digest,
            source_id=source_id,
            source_root=self.settings.runtime_candidates_dir.resolve() / source_id,
            version_owner_id=source_id,
            source_kind=source_kind,
            workspace_id=f"{source_id}--v-{digest}--s-session-intent-{_stable_session_uuid(cache_key, source_id)}",
            display_name=display_name,
        )

    async def _materialize_source(
        self,
        plan: _ExecutionResourcePlan,
        workspace: Path,
        candidate_version_store: GitAgentVersionStore | None,
    ) -> Path:
        if candidate_version_store is None:
            return await asyncio.to_thread(self._stage_workspace, plan.source_id, workspace, plan.harness_digest)
        snapshot = await asyncio.to_thread(
            self.snapshot_store.materialize_candidate,
            version_store=candidate_version_store,
            agent_id=plan.business_agent_id,
            agent_version_id=plan.version_id,
            expected_digest=plan.harness_digest,
            isolation_key=plan.cache_key,
        )
        return snapshot.workspace.parent

    async def _ensure_runtime_agent(
        self,
        plan: _ExecutionResourcePlan,
        source_root: Path,
        runtime_agent_id: str | None,
    ) -> str:
        if runtime_agent_id is None:
            matches = await self.client.list_agent_ids_by_name(_ephemeral_runtime_name(plan.source_id))
            if len(matches) > 1:
                raise RuntimeStateConflict("Ephemeral Runtime Agent identity is ambiguous")
            if matches:
                runtime_agent_id = matches[0]
            else:
                request_data = agent_payload_from_workspace(
                    source_root / "workspace",
                    display_name=_ephemeral_runtime_name(plan.source_id),
                )
                runtime_agent_id = await self.client.create_agent(request_data)
            self.store.record_ephemeral_agent(plan.cache_key, runtime_agent_id)
        return runtime_agent_id

    async def _ensure_runtime_session(
        self,
        plan: _ExecutionResourcePlan,
        runtime_agent_id: str,
        session_id: str | None,
    ) -> str:
        if session_id is not None:
            return session_id
        matches = await self.client.list_session_ids_for_workspace(runtime_agent_id, plan.workspace_id)
        if len(matches) > 1:
            raise RuntimeStateConflict("Ephemeral Runtime Session identity is ambiguous")
        if matches:
            session_id = matches[0]
        else:
            upstream = await self.client.request_json(
                "POST",
                "/sessions/",
                json={
                    "agent_id": runtime_agent_id,
                    "workspace_id": plan.workspace_id,
                    "chat_model_config": self._model_config(),
                    "name": plan.display_name,
                },
            )
            candidate = upstream.body.get("session_id") if isinstance(upstream.body, dict) else None
            if not isinstance(candidate, str) or not candidate:
                raise RuntimeStateConflict("AgentScope did not return a Session id")
            session_id = candidate
        self.store.record_ephemeral_session(plan.cache_key, session_id)
        return session_id

    async def _configure_execution_resource(
        self,
        plan: _ExecutionResourcePlan,
        *,
        source_root: Path,
        runtime_agent_id: str,
        session_id: str,
        permission_mode: str,
        cwd: str,
    ) -> _ExecutionResource:
        await self.client.request_json(
            "PATCH",
            f"/sessions/{session_id}",
            params={"agent_id": runtime_agent_id},
            json={"permission_mode": permission_mode, "cwd": cwd},
        )
        self.store.bind_session(
            session_id=session_id,
            agent_id=plan.business_agent_id,
            agent_version_id=plan.version_id,
            runtime_agent_id=runtime_agent_id,
            digest=plan.harness_digest,
            idempotency_key=None,
        )
        self.store.mark_ephemeral_ready(plan.cache_key)
        return _ExecutionResource(
            cache_key=plan.cache_key,
            business_agent_id=plan.business_agent_id,
            version_id=plan.version_id,
            harness_digest=plan.harness_digest,
            source_id=plan.source_id,
            source_root=source_root,
            version_owner_id=plan.version_owner_id,
            source_kind=plan.source_kind,
            runtime_agent_id=runtime_agent_id,
            session_id=session_id,
            workspace_id=plan.workspace_id,
        )

    async def _record_resource_failure(
        self,
        plan: _ExecutionResourcePlan,
        source_root: Path,
        stage: str,
        error: BaseException,
    ) -> bool:
        if plan.source_kind == "candidate_snapshot" and _requires_runtime_restart(error, source_root):
            self.store.mark_ephemeral_awaiting_restart(
                plan.cache_key,
                stage=stage,
                error_type=type(error).__name__,
            )
            return True
        self.store.mark_ephemeral_cleanup_pending(
            plan.cache_key,
            stage=stage,
            error_type=type(error).__name__,
        )
        with suppress(Exception):
            await self._cleanup_ephemeral(plan.cache_key)
        return False

    async def _run_resource(
        self,
        resource: _ExecutionResource,
        req: ChatRequest,
        *,
        governed_evidence_root: str | None = None,
    ) -> ChatResponse:
        if governed_evidence_root is not None and resource.business_agent_id != "governor":
            raise RuntimeStateConflict("Only the Governor may receive a governed evidence root")
        input_message = _user_message(
            req.message,
            governed_evidence_root=governed_evidence_root,
        )
        run_metadata = dict(req.metadata)
        run_metadata.pop(GOVERNED_EVIDENCE_ROOT_METADATA_KEY, None)
        if governed_evidence_root is not None:
            run_metadata[GOVERNED_EVIDENCE_ROOT_METADATA_KEY] = governed_evidence_root
        run = self.store.begin_run(
            session_id=resource.session_id,
            runtime_agent_id=resource.runtime_agent_id,
            input_value=input_message,
            alert_id=req.alert_id,
            case_id=req.case_id,
            metadata=run_metadata,
        )
        try:
            async with asyncio.timeout(self._timeout_for(resource)):
                observed = await self._trigger_and_observe(
                    resource,
                    run.run_id,
                    input_message,
                )
        except Exception:
            await self._interrupt_if_active(resource.session_id, resource.runtime_agent_id)
            raise

        messages_body = await self.client.request_json(
            "GET",
            f"/sessions/{resource.session_id}/messages",
            params={"agent_id": resource.runtime_agent_id, "limit": 200},
        )
        messages = _messages_from_body(messages_body.body)
        canonical = _canonical_assistant_result(
            messages,
            observed.terminal.reply_ids,
        )
        if observed.terminal.status is RunStatus.SUCCEEDED and canonical is None:
            raise RuntimeStateConflict("AgentScope canonical final reply is unavailable")
        errors: list[str] = []
        if observed.terminal.error:
            errors.append(json.dumps(observed.terminal.error, ensure_ascii=False, sort_keys=True))
        if observed.terminal.status != RunStatus.SUCCEEDED:
            errors.append(f"AgentScope run ended as {observed.terminal.status.value}")
        return ChatResponse(
            run_id=observed.terminal.run_id,
            session_id=observed.terminal.session_id,
            agent_version_id=observed.terminal.agent_version_id,
            trace_id=observed.terminal.trace_id,
            trace_url=observed.terminal.trace_url,
            answer=canonical.text if canonical is not None else "",
            messages=messages,
            agent_activity={
                "event_types": [str(item.get("type")) for item in observed.events],
                "reply_ids": observed.terminal.reply_ids,
            },
            usage=canonical.usage if canonical is not None else None,
            stop_reason=(canonical.finished_reason if canonical is not None else None) or observed.terminal.terminal_reason or "",
            errors=errors,
        )

    async def _trigger_and_observe(
        self,
        resource: _ExecutionResource,
        run_id: str,
        input_message: JsonObject,
    ) -> _ObservedExecution:
        async with self.client.stream(
            f"/sessions/{resource.session_id}/stream",
            params={"agent_id": resource.runtime_agent_id},
        ) as response:
            upstream = await self.client.request_json(
                "POST",
                "/chat/",
                json={
                    "agent_id": resource.runtime_agent_id,
                    "session_id": resource.session_id,
                    "input": input_message,
                },
            )
            if not isinstance(upstream.body, dict) or upstream.body.get("status") != "started":
                raise RuntimeStateConflict("AgentScope did not accept the run")
            self.store.mark_trigger_started(run_id)
            return await self._observe_until_terminal(response, run_id)

    async def _observe_until_terminal(self, response: Any, run_id: str) -> _ObservedExecution:
        events: list[JsonObject] = []
        stream_task = asyncio.create_task(self._collect_stream_events(response, events))
        terminal_task = asyncio.create_task(self._wait_for_terminal(run_id))
        try:
            done, _pending = await asyncio.wait(
                (stream_task, terminal_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stream_task in done:
                await stream_task
                terminal = await terminal_task
            else:
                terminal = terminal_task.result()
            return _ObservedExecution(events=events, terminal=terminal)
        finally:
            for task in (stream_task, terminal_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stream_task, terminal_task, return_exceptions=True)

    @staticmethod
    async def _collect_stream_events(response: Any, events: list[JsonObject]) -> None:
        async for event in _iter_sse_events(response):
            events.append(event)
            if _requires_interactive_continuation(event):
                raise RuntimeStateConflict(
                    "Non-interactive governance/test execution requires an explicit HITL continuation",
                )

    async def _wait_for_terminal(self, run_id: str) -> AgentRunResponse:
        while True:
            run = self.store.get_run(run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                return run
            await asyncio.sleep(0.05)

    async def _interrupt_if_active(self, session_id: str, runtime_agent_id: str) -> None:
        del runtime_agent_id
        active = self.store.active_run_for_session(session_id)
        if active is None:
            return
        self.store.mark_cancel_requested(active.run_id)
        bindings = self.store.active_session_bindings(active.run_id)
        results = await asyncio.gather(
            *(
                self.client.request_json(
                    "POST",
                    f"/sessions/{binding.session_id}/interrupt",
                    params={"agent_id": binding.runtime_agent_id},
                )
                for binding in bindings
            ),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            self.store.mark_cancellation_uncertain(
                active.run_id,
                error={"type": type(errors[0]).__name__},
            )

    async def _release_key(self, cache_key: str) -> None:
        async with self._lock:
            resource = self._resources.pop(cache_key, None)
        ledger = self.store.get_ephemeral_resource(cache_key)
        if ledger is None or ledger.status == "cleanup_complete":
            return
        if resource is not None:
            await self._interrupt_if_active(resource.session_id, resource.runtime_agent_id)
            for _ in range(100):
                if self.store.active_run_for_session(resource.session_id) is None:
                    break
                await asyncio.sleep(0.05)
            if self.store.active_run_for_session(resource.session_id) is not None:
                async with self._lock:
                    self._resources[cache_key] = resource
                raise RuntimeStateConflict("Candidate resource still owns an active run")
        self.store.mark_ephemeral_cleanup_pending(
            cache_key,
            stage="release",
            error_type="CleanupRequested",
        )
        try:
            await self._cleanup_ephemeral(cache_key)
        except Exception as exc:
            self.store.mark_ephemeral_cleanup_pending(
                cache_key,
                stage="cleanup",
                error_type=type(exc).__name__,
            )
            if resource is not None:
                async with self._lock:
                    self._resources.setdefault(cache_key, resource)
            raise

    async def _cleanup_ephemeral(self, cache_key: str) -> None:
        """按 durable locator 顺序清 Session→Agent→binding/version→source。"""

        ledger = self.store.get_ephemeral_resource(cache_key)
        if ledger is None or ledger.status == "cleanup_complete":
            return
        runtime_agent_id = ledger.runtime_agent_id
        if runtime_agent_id is None:
            matches = await self.client.list_agent_ids_by_name(
                _ephemeral_runtime_name(ledger.source_id),
            )
            if len(matches) > 1:
                raise RuntimeStateConflict("Ephemeral Runtime Agent identity is ambiguous")
            if matches:
                runtime_agent_id = matches[0]
                ledger = self.store.record_ephemeral_agent(cache_key, runtime_agent_id)

        if runtime_agent_id is not None:
            local_sessions = self.store.sessions_for_runtime_agent(runtime_agent_id)
            if any(binding.active_run_id for binding in local_sessions):
                raise RuntimeStateConflict("Ephemeral Runtime Agent still owns an active run")
            try:
                remote_session_ids = await self.client.list_session_ids(runtime_agent_id)
            except RuntimeUpstreamError as exc:
                if exc.status_code != 404:
                    raise
                remote_session_ids = []
            session_ids = sorted(
                {binding.session_id for binding in local_sessions} | set(remote_session_ids) | ({ledger.session_id} if ledger.session_id else set()),
            )
            for session_id in session_ids:
                await self._delete_session_allow_missing(session_id, runtime_agent_id)
            await self._delete_agent_allow_missing(runtime_agent_id)
            # 远端资源确认不存在后，才丢可重建的本地定位证据。
            for binding in local_sessions:
                self.store.delete_session_binding(binding.session_id)
            self.store.delete_agent_version(
                agent_id=ledger.version_owner_id,
                agent_version_id=ledger.agent_version_id,
                digest=ledger.harness_digest,
            )

        source_root = self.settings.runtime_candidates_dir.resolve() / ledger.source_id
        if ledger.source_kind == "candidate_snapshot":
            removed = await asyncio.to_thread(
                self.snapshot_store.remove_exact_source,
                source_id=ledger.source_id,
                agent_id=ledger.business_agent_id,
                agent_version_id=ledger.agent_version_id,
                expected_digest=ledger.harness_digest,
            )
            if not removed:
                raise RuntimeStateConflict("Candidate snapshot removal was not confirmed")
        elif ledger.source_kind == "staged":
            await asyncio.to_thread(self._remove_staged_workspace, source_root)
        else:
            raise RuntimeStateConflict("Ephemeral source kind is not removable")
        self.store.complete_ephemeral_resource(cache_key)

    async def _delete_session_allow_missing(
        self,
        session_id: str,
        runtime_agent_id: str,
    ) -> None:
        try:
            await self.client.request_json(
                "DELETE",
                f"/sessions/{session_id}",
                params={"agent_id": runtime_agent_id},
            )
        except RuntimeUpstreamError as exc:
            if exc.status_code != 404:
                raise

    async def _delete_agent_allow_missing(self, runtime_agent_id: str) -> None:
        try:
            await self.client.delete_agent(runtime_agent_id)
        except RuntimeUpstreamError as exc:
            if exc.status_code != 404:
                raise

    def _stage_workspace(self, source_id: str, source: Path, digest: str) -> Path:
        candidates = self.settings.runtime_candidates_dir.resolve()
        candidates.mkdir(parents=True, exist_ok=True)
        target = candidates / source_id
        if target.exists():
            if harness_digest(target / "workspace") != digest:
                raise RuntimeStateConflict("Existing candidate workspace digest does not match")
            return target
        _reject_symlinks(source)
        staging = Path(tempfile.mkdtemp(prefix=f".{source_id}.", dir=candidates))
        try:
            copied = staging / "workspace"
            shutil.copytree(
                source,
                copied,
                ignore=shutil.ignore_patterns(
                    ".git",
                    ".agentgov-runtime-workspace.json",
                    "conversion-report.json",
                    "__pycache__",
                    "*.pyc",
                ),
            )
            _reject_symlinks(copied)
            (copied / "conversion-report.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "purpose": "runtime_candidate_materialization",
                        "harness_digest": digest,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            os.rename(staging, target)
            return target
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def _remove_staged_workspace(self, target: Path) -> None:
        root = self.settings.runtime_candidates_dir.resolve()
        resolved = target.resolve()
        if resolved.parent != root or not resolved.name.startswith("candidate-"):
            raise RuntimeStateConflict("Refusing to remove a path outside the candidate Runtime root")
        if resolved.exists():
            shutil.rmtree(resolved)

    def _model_config(self) -> JsonObject:
        return {
            "type": self.settings.agentscope_model_type,
            "credential_id": self.settings.agentscope_credential_id,
            "model": self.settings.agentscope_model_name,
            "parameters": self.settings.agentscope_model_parameters,
        }

    def _timeout_for(self, resource: _ExecutionResource) -> int:
        return self.settings.governance_agent_timeout_seconds if resource.business_agent_id == "governor" else self.settings.agent_test_run_timeout_seconds
