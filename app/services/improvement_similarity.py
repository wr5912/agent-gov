from __future__ import annotations

import re

from app.runtime.stores.improvement_store import ImprovementItemRecord, ImprovementStore

# 确定性相似度（无 ML）：latin 词 + CJK uni/bi/tri-gram token，Jaccard + 共享来源反馈加权。
_LATIN_RE = re.compile(r"[a-z0-9]+")
_CJK_SPAN_RE = re.compile(r"[\u4e00-\u9fff]+")
SIMILARITY_THRESHOLD = 0.4


def _tokens(text: str) -> set[str]:
    lowered = (text or "").lower()
    tokens = set(_LATIN_RE.findall(lowered))
    for span in _CJK_SPAN_RE.findall(lowered):
        tokens.update(span)
        for n in (2, 3):
            tokens.update(span[i : i + n] for i in range(max(0, len(span) - n + 1)))
    return tokens


def similarity_score(a_text: str, a_refs: list[str], b_text: str, b_refs: list[str]) -> float:
    ta, tb = _tokens(a_text), _tokens(b_text)
    union = ta | tb
    jaccard = (len(ta & tb) / len(union)) if union else 0.0
    containment = (len(ta & tb) / min(len(ta), len(tb))) if ta and tb else 0.0
    shared_refs = set(a_refs) & set(b_refs)
    return round(max(jaccard, containment * 0.75) + (0.5 if shared_refs else 0.0), 4)


def find_similar_improvements(
    store: ImprovementStore,
    *,
    agent_id: str,
    text: str,
    refs: list[str],
    exclude_id: str | None = None,
    threshold: float = SIMILARITY_THRESHOLD,
) -> list[tuple[ImprovementItemRecord, float]]:
    """同 Agent 的开放（非归档）改进事项中，与给定文本/反馈相似度 >= 阈值者，按分数降序。"""
    scored: list[tuple[ImprovementItemRecord, float]] = []
    for item in store.list_improvements(agent_id=agent_id):
        if item.improvement_id == exclude_id or item.improvement_status == "archived":
            continue
        score = similarity_score(text, refs, f"{item.title} {item.summary}", item.source_feedback_refs)
        if score >= threshold:
            scored.append((item, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored
