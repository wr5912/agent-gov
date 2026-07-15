from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Optional, cast

from app.runtime.json_types import JsonObject
from app.runtime.schemas import ChatRequest, ChatResponse, EvalRunResponse
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.test_dataset_schemas import TestCaseRecord

RunChat = Callable[[ChatRequest], Awaitable[ChatResponse]]
# #24-A：候选回归带 agent_id（被治理的真实业务 Agent），使候选 profile/归属落到该 Agent 而非 main。
RunCandidateChat = Callable[[ChatRequest, Path, str, str, str], Awaitable[ChatResponse]]


class FeedbackEvalRunner:
    """Runs an immutable TestDataset snapshot through its owning Agent runtime."""

    def __init__(
        self,
        *,
        feedback_store: FeedbackStore,
        run_chat: RunChat,
        run_candidate_chat: RunCandidateChat | None = None,
    ) -> None:
        self.feedback_store = feedback_store
        self.run_chat = run_chat
        self.run_candidate_chat = run_candidate_chat

    async def run_feedback_eval(
        self,
        *,
        dataset_id: str,
        source: str = "manual_feedback_dataset",
        change_set_id: Optional[str] = None,
        regression_attempt_id: Optional[str] = None,
        candidate_commit_sha: Optional[str] = None,
        candidate_worktree_path: Optional[str] = None,
    ) -> EvalRunResponse | None:
        eval_run = self._create_eval_run(
            dataset_id=dataset_id,
            source=source,
            change_set_id=change_set_id,
            regression_attempt_id=regression_attempt_id,
            candidate_commit_sha=candidate_commit_sha,
            candidate_worktree_path=candidate_worktree_path,
        )
        try:
            for dataset_case in eval_run.dataset_snapshot.cases:
                if not self.feedback_store.renew_eval_run_lease(eval_run.eval_run_id):
                    raise RuntimeError(f"EvalRun lease was lost before dataset case {dataset_case.case_id}")
                result: ChatResponse | None = None
                try:
                    request = self._eval_chat_request(eval_run, dataset_case)
                    result = await asyncio.wait_for(self._run_eval_chat(request, change_set_id, candidate_commit_sha, candidate_worktree_path), timeout=300)
                    status, score, check_results = self._evaluate_dataset_case(dataset_case, result)
                    self.feedback_store.append_eval_run_item(
                        eval_run.eval_run_id,
                        dataset_case=dataset_case,
                        agent_result=self._chat_result_payload(
                            result,
                            expected_agent_version_id=eval_run.agent_version_id,
                        ),
                        status=status,
                        score=score,
                        check_results=check_results,
                    )
                except Exception as exc:
                    self.feedback_store.append_eval_run_item(
                        eval_run.eval_run_id,
                        dataset_case=dataset_case,
                        agent_result=self._chat_result_payload(
                            result,
                            expected_agent_version_id=eval_run.agent_version_id,
                        ),
                        status="failed",
                        score=0.0,
                        check_results=[],
                        error_json={"error_code": "EVAL_RUN_ITEM_RUNTIME_ERROR", "message": f"{exc.__class__.__name__}: {exc}"},
                    )
            return self._eval_run_response(self.feedback_store.finish_eval_run(eval_run.eval_run_id))
        except asyncio.CancelledError:
            self.feedback_store.fail_eval_run(
                eval_run.eval_run_id,
                error_code="EVAL_RUN_CANCELLED",
                message="EvalRun was cancelled before all dataset cases completed",
            )
            raise
        except Exception as exc:
            return self._eval_run_response(
                self.feedback_store.fail_eval_run(
                    eval_run.eval_run_id,
                    error_code="EVAL_RUN_RUNTIME_ERROR",
                    message=f"{exc.__class__.__name__}: {exc}",
                )
            )

    def _create_eval_run(
        self,
        *,
        dataset_id: str,
        source: str,
        change_set_id: Optional[str],
        regression_attempt_id: Optional[str],
        candidate_commit_sha: Optional[str],
        candidate_worktree_path: Optional[str],
    ) -> EvalRunResponse:
        response = self._eval_run_response(
            self.feedback_store.create_eval_run(
                dataset_id=dataset_id,
                agent_version_id=candidate_commit_sha,
                source=source,
                change_set_id=change_set_id,
                regression_attempt_id=regression_attempt_id,
                candidate_commit_sha=candidate_commit_sha,
                candidate_worktree_path=candidate_worktree_path,
            )
        )
        if response is None:  # pragma: no cover - create_eval_run always returns a record
            raise RuntimeError("EvalRun was not persisted")
        return response

    def _eval_chat_request(
        self,
        eval_run: EvalRunResponse,
        dataset_case: TestCaseRecord,
    ) -> ChatRequest:
        return ChatRequest(
            message=dataset_case.prompt,
            session_id=f"eval-{eval_run.eval_run_id}-{dataset_case.case_id}",
            agent_id=eval_run.dataset_snapshot.agent_id,
            metadata={
                "source": "regression_eval",
                "eval_run_id": eval_run.eval_run_id,
                "dataset_id": eval_run.dataset_id,
                "dataset_revision": eval_run.dataset_snapshot.revision,
                "dataset_case_id": dataset_case.case_id,
                "change_set_id": eval_run.change_set_id,
                "candidate_commit_sha": eval_run.candidate_commit_sha,
                "candidate_worktree_path": eval_run.candidate_worktree_path,
            },
        )

    async def _run_eval_chat(
        self,
        request: ChatRequest,
        change_set_id: Optional[str],
        candidate_commit_sha: Optional[str],
        candidate_worktree_path: Optional[str],
    ) -> ChatResponse:
        if change_set_id and candidate_commit_sha and candidate_worktree_path and self.run_candidate_chat:
            agent_id = (request.agent_id or "").strip()
            if not agent_id:
                raise RuntimeError("Candidate EvalRun request is missing snapshot agent_id")
            return await self.run_candidate_chat(request, Path(candidate_worktree_path), candidate_commit_sha, change_set_id, agent_id)
        return await self.run_chat(request)

    def _evaluate_dataset_case(self, dataset_case: TestCaseRecord, result: ChatResponse) -> tuple[str, float, list[JsonObject]]:
        errors = result.errors
        answer = result.answer.strip()
        check_results: list[JsonObject] = []

        def append_check(name: str, passed: bool, required: bool, detail: str) -> None:
            check_results.append({"name": name, "passed": passed, "required": required, "detail": detail})

        append_check("non_empty_answer", bool(answer), True, "回答不应为空。")
        append_check("no_runtime_errors", not errors, True, "; ".join(map(str, errors)) if errors else "运行无错误。")
        semantic_requirements = [dataset_case.expected_behavior.strip(), *[value.strip() for value in dataset_case.checkpoints]]
        semantic_requirements = [value for value in semantic_requirements if value]
        if semantic_requirements:
            append_check(
                "semantic_requirements_require_human_review",
                False,
                False,
                f"数据集声明 {len(semantic_requirements)} 项自然语言期望；当前无可验证的结构化断言，禁止自动判定通过。",
            )

        required_checks = [item for item in check_results if item["required"]]
        passed_required = sum(1 for item in required_checks if item["passed"])
        score = passed_required / len(required_checks) if required_checks else 1.0
        if any(not item["passed"] for item in required_checks):
            return "failed", score, check_results
        if semantic_requirements:
            return "needs_human_review", score, check_results
        return "passed", score, check_results

    @staticmethod
    def _chat_result_payload(
        result: ChatResponse | None,
        *,
        expected_agent_version_id: str | None,
    ) -> JsonObject:
        payload = cast(JsonObject, result.model_dump(mode="json")) if result else {}
        if not payload.get("agent_version_id"):
            payload["agent_version_id"] = expected_agent_version_id
        return payload

    @staticmethod
    def _eval_run_response(payload: JsonObject | None) -> EvalRunResponse | None:
        return EvalRunResponse.model_validate(payload) if payload else None
