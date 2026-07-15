from __future__ import annotations

FEEDBACK_CASE_SOURCE = "feedback_inbox"
FEEDBACK_CASE_ID_PREFIX = "fbc-"
FEEDBACK_CASE_ATTACH_ONLY_MESSAGE = "FeedbackCase feedback must be assigned through attach-feedback-case"


def has_feedback_case_semantics(*, source: object, case_id: object) -> bool:
    """判断通用反馈输入是否在表达只能由正式挂接入口处理的 FeedbackCase。"""
    clean_source = str(source or "").strip().casefold()
    clean_case_id = str(case_id or "").strip().casefold()
    return clean_source == FEEDBACK_CASE_SOURCE or clean_case_id.startswith(FEEDBACK_CASE_ID_PREFIX)
