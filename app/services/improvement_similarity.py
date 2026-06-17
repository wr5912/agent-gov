from __future__ import annotations

import re

from app.runtime.stores.improvement_store import ImprovementItemRecord, ImprovementStore

# 确定性相似度（无 ML）：latin 词 + 单个 CJK 字符作为 token，Jaccard + 共享来源反馈加权。
_TOKEN_RE = re.compile(r"[a-z0-9]+|[一-鿿]")
SIMILARITY_THRESHOLD = 0.4


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def similarity_score(a_text: str, a_refs: list[str], b_text: str, b_refs: list[str]) -> float:
    ta, tb = _tokens(a_text), _tokens(b_text)
    union = ta | tb
    jaccard = (len(ta & tb) / len(union)) if union else 0.0
    shared_refs = set(a_refs) & set(b_refs)
    return round(jaccard + (0.5 if shared_refs else 0.0), 4)


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
