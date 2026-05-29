from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from .feedback_store import FeedbackStore
from .schemas import ChatRequest

RunChat = Callable[[ChatRequest], Awaitable[dict[str, Any]]]


class FeedbackEvalRunner:
    """Runs feedback eval cases against the main runtime chat path."""

    def __init__(
        self,
        *,
        feedback_store: FeedbackStore,
        run_chat: RunChat,
        current_agent_version_id: Callable[[], Optional[str]],
    ) -> None:
        self.feedback_store = feedback_store
        self.run_chat = run_chat
        self.current_agent_version_id = current_agent_version_id

    async def run_feedback_eval(
        self,
        *,
        eval_case_ids: Optional[list[str]] = None,
        optimization_task_id: Optional[str] = None,
        source: str = "manual_feedback_dataset",
    ) -> dict[str, Any] | None:
        eval_cases = self._selected_eval_cases(eval_case_ids)
        if not eval_cases:
            return None
        eval_run = self.feedback_store.create_eval_run(
            eval_case_ids=[str(item["eval_case_id"]) for item in eval_cases],
            agent_version_id=self.current_agent_version_id(),
            optimization_task_id=optimization_task_id,
            source=source,
        )
        if optimization_task_id:
            self.feedback_store.update_task_status(
                optimization_task_id,
                status="regression_running",
                fields={"latest_regression_run_id": eval_run["eval_run_id"]},
            )
        try:
            for eval_case in eval_cases:
                result: dict[str, Any] | None = None
                try:
                    result = await self.run_chat(
                        ChatRequest(
                            message=str(eval_case.get("prompt") or ""),
                            session_id=f"eval-{eval_run['eval_run_id']}-{eval_case['eval_case_id']}",
                            case_id=str(eval_case.get("source_feedback_case_id") or "") or None,
                            metadata={
                                "source": "regression_eval",
                                "eval_run_id": eval_run["eval_run_id"],
                                "eval_case_id": eval_case["eval_case_id"],
                                "optimization_task_id": optimization_task_id,
                            },
                        )
                    )
                    status, score, check_results = self._evaluate_eval_case(eval_case, result)
                    self.feedback_store.append_eval_run_item(
                        eval_run["eval_run_id"],
                        eval_case=eval_case,
                        agent_result=result,
                        status=status,
                        score=score,
                        check_results=check_results,
                    )
                except Exception as exc:
                    self.feedback_store.append_eval_run_item(
                        eval_run["eval_run_id"],
                        eval_case=eval_case,
                        agent_result=result,
                        status="failed",
                        score=0.0,
                        check_results=[],
                        error_json={"error_code": "EVAL_CASE_RUNTIME_ERROR", "message": f"{exc.__class__.__name__}: {exc}"},
                    )
            return self.feedback_store.finish_eval_run(eval_run["eval_run_id"])
        except Exception as exc:
            return self.feedback_store.fail_eval_run(
                eval_run["eval_run_id"],
                error_code="EVAL_RUN_RUNTIME_ERROR",
                message=f"{exc.__class__.__name__}: {exc}",
            )

    def _selected_eval_cases(self, eval_case_ids: Optional[list[str]]) -> list[dict[str, Any]]:
        if eval_case_ids:
            selected = [self.feedback_store.find_eval_case(eval_case_id) for eval_case_id in eval_case_ids]
            return [item for item in selected if item and item.get("status") == "active"]
        return self.feedback_store.list_eval_cases(status="active", limit=100)

    def _evaluate_eval_case(self, eval_case: dict[str, Any], result: dict[str, Any]) -> tuple[str, float, list[dict[str, Any]]]:
        checks = eval_case.get("checks_json") if isinstance(eval_case.get("checks_json"), dict) else {}
        errors = result.get("errors") if isinstance(result.get("errors"), list) else []
        answer = str(result.get("answer") or "").strip()
        activity = result.get("agent_activity") if isinstance(result.get("agent_activity"), dict) else {}
        tool_names = self._eval_tool_names(activity)
        check_results: list[dict[str, Any]] = []

        def append_check(name: str, passed: bool, required: bool, detail: str) -> None:
            check_results.append({"name": name, "passed": passed, "required": required, "detail": detail})

        if checks.get("requires_non_empty_answer", True):
            append_check("non_empty_answer", bool(answer), True, "回答不应为空。")
        if checks.get("requires_no_runtime_errors", True):
            append_check("no_runtime_errors", not errors, True, "; ".join(map(str, errors)) if errors else "运行无错误。")
        if checks.get("requires_tool_use"):
            preferred = [str(item) for item in checks.get("preferred_tools") or [] if item]
            if preferred:
                tool_passed = any(any(tool == expected or expected in tool for tool in tool_names) for expected in preferred)
                detail = f"期望工具：{', '.join(preferred)}；实际工具：{', '.join(tool_names) or '-'}。"
            else:
                tool_passed = bool(tool_names)
                detail = f"实际工具：{', '.join(tool_names) or '-'}。"
            append_check("required_tool_use", tool_passed, True, detail)

        required_checks = [item for item in check_results if item["required"]]
        passed_required = sum(1 for item in required_checks if item["passed"])
        score = passed_required / len(required_checks) if required_checks else 0.0
        if any(not item["passed"] for item in required_checks):
            return "failed", score, check_results
        return "passed", score, check_results

    @staticmethod
    def _eval_tool_names(activity: dict[str, Any]) -> list[str]:
        names: list[str] = []
        for item in activity.get("tool_names") or []:
            if item:
                names.append(str(item))
        for call in activity.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("name"):
                names.append(str(call["name"]))
        return sorted(set(names))
