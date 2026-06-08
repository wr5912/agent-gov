from __future__ import annotations

from collections.abc import Callable
from typing import cast

from ..json_types import JsonObject

MAX_PROMPT_LIST_ITEMS = 8
MAX_PROMPT_TEXT_CHARS = 3_000
MAX_PROMPT_FILE_TEXT_CHARS = 20_000
MAX_PROMPT_NESTED_TEXT_CHARS = 1_000


def build_attribution_prompt_context(input_json: JsonObject) -> JsonObject:
    evidence_files = _json_dict(input_json.get("evidence_files"))
    return _json_object(
        {
            "feedback_case": _compact_json_object(input_json.get("feedback_case"), MAX_PROMPT_NESTED_TEXT_CHARS),
            "evidence_package": _evidence_package_summary(_json_dict(input_json.get("evidence_package"))),
            "evidence_files": _compact_json_object(evidence_files, MAX_PROMPT_FILE_TEXT_CHARS),
            "langfuse_trace_details": _compact_json_object(input_json.get("langfuse_trace_details"), MAX_PROMPT_FILE_TEXT_CHARS),
            "main_agent_version_id": _text(input_json.get("main_agent_version_id"), 300),
            "task": _text(input_json.get("task"), 200),
        }
    )


def build_proposal_prompt_context(input_json: JsonObject) -> JsonObject:
    """Return the business context needed by proposal-generator.

    The persisted job input remains the audit source of truth. This projection is
    intentionally narrower because it is embedded into Claude Code's user prompt.
    """

    attributions = _json_list(input_json.get("attribution_outputs"))
    eval_cases = _json_list(input_json.get("eval_cases"))
    return _json_object(
        {
            "feedback_case_count": len(_json_list(input_json.get("feedback_case_ids"))),
            "eval_case_count": len(_json_list(input_json.get("eval_case_ids"))),
            "regeneration_instruction": _text(input_json.get("regeneration_instruction"), MAX_PROMPT_TEXT_CHARS),
            "target_policy": _target_policy_summary(_json_dict(input_json.get("target_policy"))),
            "allowed_target_paths": _limited_text_list(input_json.get("allowed_target_paths"), MAX_PROMPT_LIST_ITEMS),
            "attribution_summaries": _limited_objects(attributions, _attribution_summary),
            "eval_case_summaries": _limited_objects(eval_cases, _eval_case_summary),
        }
    )


def build_execution_prompt_context(input_json: JsonObject) -> JsonObject:
    target_file_contexts = _json_list(input_json.get("target_file_contexts"))
    return _json_object(
        {
            "proposal": _plan_task_summary(_json_dict(input_json.get("proposal"))),
            "target_paths": _limited_text_list(input_json.get("target_paths"), MAX_PROMPT_LIST_ITEMS),
            "target_policy": _target_policy_summary(_json_dict(input_json.get("target_policy"))),
            "target_file_contexts": _limited_objects(target_file_contexts, _target_file_context_summary),
        }
    )


def build_eval_case_generation_prompt_context(input_json: JsonObject) -> JsonObject:
    feedback_cases = _json_list(input_json.get("feedback_cases"))
    existing_eval_cases = _json_list(input_json.get("existing_eval_cases"))
    return _json_object(
        {
            "feedback_case_count": len(feedback_cases),
            "source_refs": _limited_objects(_json_list(input_json.get("source_refs")), _source_ref_summary),
            "feedback_cases": _limited_objects(feedback_cases, _eval_case_generation_case_summary),
            "existing_eval_case_summaries": _limited_objects(existing_eval_cases, _eval_case_summary),
        }
    )


def build_regression_impact_prompt_context(input_json: JsonObject) -> JsonObject:
    eval_run = _json_dict(input_json.get("eval_run"))
    items = _json_list(eval_run.get("items"))
    return _json_object(
        {
            "result_status": _text(eval_run.get("result_status"), 200),
            "summary": _compact_json_object(eval_run.get("summary"), MAX_PROMPT_NESTED_TEXT_CHARS),
            "gate_result": _gate_result_summary(_json_dict(eval_run.get("gate_result"))),
            "item_summaries": _limited_objects(items, _eval_run_item_summary),
        }
    )


def _attribution_summary(source: JsonObject) -> JsonObject:
    return _json_object(
        {
            "status": _text(source.get("status"), 100),
            "problem_type": _text(source.get("problem_type"), 100),
            "optimization_object_type": _text(source.get("optimization_object_type"), 100),
            "actionability": _text(source.get("actionability"), 100),
            "confidence": _text(source.get("confidence"), 100),
            "human_review_required": source.get("human_review_required") if isinstance(source.get("human_review_required"), bool) else None,
            "responsibility_boundary": _responsibility_boundary_summary(_json_dict(source.get("responsibility_boundary"))),
            "rationale": _text(source.get("rationale"), MAX_PROMPT_TEXT_CHARS),
            "recommended_next_step": _text(source.get("recommended_next_step"), 100),
            "evidence_refs": _limited_objects(_json_list(source.get("evidence_refs")), _evidence_ref_summary),
        }
    )


def _responsibility_boundary_summary(source: JsonObject) -> JsonObject:
    return _json_object({"owner": _text(source.get("owner"), 200), "reason": _text(source.get("reason"), MAX_PROMPT_NESTED_TEXT_CHARS)})


def _evidence_ref_summary(source: JsonObject) -> JsonObject:
    return _json_object(
        {
            "type": _text(source.get("type"), 100),
            "id": _text(source.get("id"), 300),
            "reason": _text(source.get("reason"), MAX_PROMPT_NESTED_TEXT_CHARS),
        }
    )


def _evidence_package_summary(source: JsonObject) -> JsonObject:
    completeness = _json_dict(source.get("completeness"))
    source_refs = _json_dict(source.get("source_refs"))
    return _json_object(
        {
            "evidence_package_id": _text(source.get("evidence_package_id"), 300),
            "main_agent_version_id": _text(source.get("main_agent_version_id"), 300),
            "source_refs": _compact_json_object(source_refs, MAX_PROMPT_NESTED_TEXT_CHARS),
            "included_files": _limited_objects(_json_list(source.get("included_files")), _included_file_summary, limit=20),
            "completeness": _compact_json_object(completeness, MAX_PROMPT_NESTED_TEXT_CHARS),
        }
    )


def _included_file_summary(source: JsonObject) -> JsonObject:
    return _json_object(
        {
            "path": _text(source.get("path"), 300),
            "type": _text(source.get("type"), 100),
            "sha256": _text(source.get("sha256"), 100),
        }
    )


def _eval_case_summary(source: JsonObject) -> JsonObject:
    return _json_object(
        {
            "title": _text(source.get("title"), 300),
            "prompt": _text(source.get("prompt"), MAX_PROMPT_TEXT_CHARS),
            "expected_behavior": _text(source.get("expected_behavior"), MAX_PROMPT_TEXT_CHARS),
            "checks_json": _compact_json_object(source.get("checks_json"), MAX_PROMPT_NESTED_TEXT_CHARS),
            "labels": _limited_text_list(source.get("labels"), MAX_PROMPT_LIST_ITEMS),
            "source_summary": _compact_json_object(source.get("source_summary"), MAX_PROMPT_NESTED_TEXT_CHARS),
            "attribution_summary": _compact_json_object(source.get("attribution_summary"), MAX_PROMPT_NESTED_TEXT_CHARS),
            "proposal_summary": _compact_json_object(source.get("proposal_summary"), MAX_PROMPT_NESTED_TEXT_CHARS),
        }
    )


def _plan_task_summary(source: JsonObject) -> JsonObject:
    return _json_object(
        {
            "title": _text(source.get("title"), 300),
            "description": _text(source.get("description"), MAX_PROMPT_TEXT_CHARS),
            "objective": _text(source.get("objective"), MAX_PROMPT_TEXT_CHARS),
            "target_summary": _text(source.get("target_summary"), 500),
            "target_type": _text(source.get("target_type"), 200),
            "target_path": _text(source.get("target_path"), 500),
            "owner": _text(source.get("owner"), 200),
            "actionability": _text(source.get("actionability"), 100),
            "recommendation": _text(source.get("recommendation"), MAX_PROMPT_TEXT_CHARS),
            "recommended_actions": _limited_text_list(source.get("recommended_actions"), MAX_PROMPT_LIST_ITEMS),
            "acceptance_criteria": _limited_text_list(source.get("acceptance_criteria"), MAX_PROMPT_LIST_ITEMS),
            "expected_effect": _text(source.get("expected_effect"), MAX_PROMPT_TEXT_CHARS),
            "validation": _text(source.get("validation"), MAX_PROMPT_TEXT_CHARS),
            "risk": _text(source.get("risk"), MAX_PROMPT_TEXT_CHARS),
            "analysis_summary": _text(source.get("analysis_summary"), MAX_PROMPT_TEXT_CHARS),
            "evidence_summary": _text(source.get("evidence_summary"), MAX_PROMPT_TEXT_CHARS),
            "evidence_refs": _limited_objects(_json_list(source.get("evidence_refs")), _evidence_ref_summary),
            "task_context": _compact_json_object(source.get("task_context"), MAX_PROMPT_NESTED_TEXT_CHARS),
        }
    )


def _target_policy_summary(source: JsonObject) -> JsonObject:
    return _json_object(
        {
            "type": _text(source.get("type"), 200),
            "excluded_names": _limited_text_list(source.get("excluded_names"), MAX_PROMPT_LIST_ITEMS),
            "excluded_patterns": _limited_text_list(source.get("excluded_patterns"), MAX_PROMPT_LIST_ITEMS),
            "max_inline_text_bytes": source.get("max_inline_text_bytes") if isinstance(source.get("max_inline_text_bytes"), int) else None,
        }
    )


def _target_file_context_summary(source: JsonObject) -> JsonObject:
    return _json_object(
        {
            "path": _text(source.get("path"), 500),
            "managed": source.get("managed") if isinstance(source.get("managed"), bool) else None,
            "exists": source.get("exists") if isinstance(source.get("exists"), bool) else None,
            "type": _text(source.get("type"), 100),
            "size_bytes": source.get("size_bytes") if isinstance(source.get("size_bytes"), int) else None,
            "sha256": _text(source.get("sha256"), 100),
            "content_encoding": _text(source.get("content_encoding"), 100),
            "skipped_reason": _text(source.get("skipped_reason"), 500),
            "content_text": _text(source.get("content_text"), MAX_PROMPT_FILE_TEXT_CHARS),
        }
    )


def _source_ref_summary(source: JsonObject) -> JsonObject:
    return _json_object({"source_kind": _text(source.get("source_kind"), 100), "source_id": _text(source.get("source_id"), 300)})


def _eval_case_generation_case_summary(source: JsonObject) -> JsonObject:
    feedback_case = _json_dict(source.get("feedback_case"))
    source_run = _json_dict(source.get("source_run"))
    return _json_object(
        {
            "source_feedback_case_id": _text(feedback_case.get("feedback_case_id"), 300),
            "title": _text(feedback_case.get("title"), 500),
            "priority": _text(feedback_case.get("priority"), 100),
            "source_refs": _limited_objects(_json_list(source.get("source_refs")), _source_ref_summary),
            "source_message": _text(source_run.get("message"), MAX_PROMPT_TEXT_CHARS),
            "source_answer_summary": _text(source_run.get("answer_summary"), MAX_PROMPT_TEXT_CHARS),
            "source_record_summaries": _limited_objects(_json_list(source.get("source_records")), _feedback_source_summary),
            "attribution_summary": _attribution_summary(_json_dict(source.get("attribution_output"))),
            "optimization_plan_summary": _optimization_plan_summary(_json_dict(source.get("optimization_plan"))),
        }
    )


def _feedback_source_summary(source: JsonObject) -> JsonObject:
    return _json_object(
        {
            "source_kind": _text(source.get("source_kind") or source.get("kind"), 100),
            "title": _text(source.get("title"), 500),
            "labels": _limited_text_list(source.get("labels"), MAX_PROMPT_LIST_ITEMS),
            "comment": _text(source.get("comment"), MAX_PROMPT_TEXT_CHARS),
            "message": _text(source.get("message"), MAX_PROMPT_TEXT_CHARS),
            "answer_summary": _text(source.get("answer_summary"), MAX_PROMPT_TEXT_CHARS),
        }
    )


def _optimization_plan_summary(source: JsonObject) -> JsonObject:
    return _json_object(
        {
            "title": _text(source.get("title"), 500),
            "summary": _text(source.get("summary"), MAX_PROMPT_TEXT_CHARS),
            "recommendation": _text(source.get("recommendation"), MAX_PROMPT_TEXT_CHARS),
            "expected_effect": _text(source.get("expected_effect"), MAX_PROMPT_TEXT_CHARS),
            "validation": _text(source.get("validation"), MAX_PROMPT_TEXT_CHARS),
            "risk": _text(source.get("risk"), MAX_PROMPT_TEXT_CHARS),
            "tasks": _limited_objects(_json_list(source.get("tasks")), _plan_task_summary),
            "blocked_items": _limited_objects(_json_list(source.get("blocked_items")), _blocked_item_summary),
        }
    )


def _blocked_item_summary(source: JsonObject) -> JsonObject:
    return _json_object(
        {
            "title": _text(source.get("title"), 500),
            "target_type": _text(source.get("target_type"), 200),
            "target_path": _text(source.get("target_path"), 500),
            "owner": _text(source.get("owner"), 200),
            "actionability": _text(source.get("actionability"), 100),
            "reason": _text(source.get("reason"), MAX_PROMPT_TEXT_CHARS),
            "recommendation": _text(source.get("recommendation"), MAX_PROMPT_TEXT_CHARS),
        }
    )


def _gate_result_summary(source: JsonObject) -> JsonObject:
    return _json_object(
        {
            "status": _text(source.get("status"), 100),
            "blocked_case_ids": _limited_text_list(source.get("blocked_case_ids"), MAX_PROMPT_LIST_ITEMS),
            "review_case_ids": _limited_text_list(source.get("review_case_ids"), MAX_PROMPT_LIST_ITEMS),
            "note_case_ids": _limited_text_list(source.get("note_case_ids"), MAX_PROMPT_LIST_ITEMS),
            "reason": _text(source.get("reason"), MAX_PROMPT_TEXT_CHARS),
        }
    )


def _eval_run_item_summary(source: JsonObject) -> JsonObject:
    return _json_object(
        {
            "status": _text(source.get("status"), 100),
            "score": source.get("score") if isinstance(source.get("score"), int | float) else None,
            "answer_summary": _text(source.get("answer_summary"), MAX_PROMPT_TEXT_CHARS),
            "error_json": _compact_json_object(source.get("error_json"), MAX_PROMPT_NESTED_TEXT_CHARS),
            "eval_case_snapshot": _eval_case_summary(_json_dict(source.get("eval_case_snapshot"))),
            "check_results": _limited_objects(_json_list(source.get("check_results")), _check_result_summary),
        }
    )


def _check_result_summary(source: JsonObject) -> JsonObject:
    return _json_object(
        {
            "name": _text(source.get("name"), 200),
            "passed": source.get("passed") if isinstance(source.get("passed"), bool) else None,
            "message": _text(source.get("message"), MAX_PROMPT_NESTED_TEXT_CHARS),
        }
    )


def _json_dict(value: object) -> JsonObject:
    return cast(JsonObject, value) if isinstance(value, dict) else {}


def _json_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _limited_objects(value: list[object], mapper: Callable[[JsonObject], JsonObject], *, limit: int = MAX_PROMPT_LIST_ITEMS) -> list[object]:
    mapped: list[object] = []
    for item in value[:limit]:
        if isinstance(item, dict):
            mapped.append(mapper(cast(JsonObject, item)))
    if len(value) > limit:
        mapped.append({"truncated_count": len(value) - limit})
    return mapped


def _limited_text_list(value: object, limit: int) -> list[object]:
    items = value if isinstance(value, list) else []
    result = [_text(item, 500) for item in items[:limit]]
    cleaned = [item for item in result if item]
    if len(items) > limit:
        cleaned.append(f"... truncated {len(items) - limit} items")
    return cleaned


def _compact_json_object(value: object, text_limit: int) -> object:
    if isinstance(value, dict):
        return _clean({str(key): _compact_json_object(item, text_limit) for key, item in value.items()})
    if isinstance(value, list):
        return _clean([_compact_json_object(item, text_limit) for item in value[:MAX_PROMPT_LIST_ITEMS]])
    return _text(value, text_limit) if isinstance(value, str) else value


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n...[truncated {len(text) - limit} chars]"


def _json_object(value: dict[str, object]) -> JsonObject:
    cleaned = _clean(value)
    return cast(JsonObject, cleaned if isinstance(cleaned, dict) else {})


def _clean(value: object) -> object:
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            cleaned = _clean(item)
            if _present(cleaned):
                result[str(key)] = cleaned
        return result
    if isinstance(value, list):
        result = [_clean(item) for item in value]
        return [item for item in result if _present(item)]
    return value


def _present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True
