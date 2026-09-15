from __future__ import annotations

FEEDBACK_CASE_SOURCE = "feedback_inbox"
FEEDBACK_CASE_ATTACH_ONLY_MESSAGE = "FeedbackCase feedback must be assigned through attach-feedback-case"


def is_feedback_case_source(source: object) -> bool:
    """治理来源由 source 明示；业务实体 ID 的字面形状不构成挂接证据。"""
    return isinstance(source, str) and source.strip().casefold() == FEEDBACK_CASE_SOURCE
