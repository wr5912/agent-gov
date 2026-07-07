"""反馈整理 NormalizedFeedback：一次 DSPy formatter 把原始反馈归纳成 title+problem（无 governor）；
formatter 不可用/校验失败回退启发式；title 仅在自动截断态时回填（不覆盖用户手改）；原因分析不在整理阶段产出。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.runtime.feedback_schemas import NormalizedFeedbackFormatterOutput
from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.improvement_content_store import ImprovementContentStore
from app.services.improvement_governor_service import ImprovementGovernorService

RAW = "转换后的数据中有API，不符合OCSF官方标准定义https://schema.ocsf.io/1.8.0/classes/process_activity"
AUTO_TITLE = RAW[:40]  # 前端 firstSentence 的截断自动态


class _FakeImprovements:
    def __init__(self, item: SimpleNamespace) -> None:
        self._item = item
        self.title_updates: list[str] = []

    def get_improvement(self, improvement_id: str) -> SimpleNamespace:
        return self._item

    def update_title(self, improvement_id: str, *, title: str) -> SimpleNamespace:
        self.title_updates.append(title)
        return self._item


def _svc(tmp_path, *, title=AUTO_TITLE, fmt=None):
    content = ImprovementContentStore(make_session_factory(tmp_path / "runtime.sqlite3"))
    item = SimpleNamespace(improvement_id="imp-1", title=title, agent_id="a", summary="")
    svc = ImprovementGovernorService(
        improvement_store=_FakeImprovements(item),
        content_store=content,
        run_profile_json=None,
        data_dir=tmp_path / "data",
        format_normalized_feedback=fmt,
    )
    content.create_feedback("imp-1", agent_id="a", summary="转换数据不符合 OCSF 规范", raw_text=RAW)
    return svc, content


def test_llm_organizes_title_problem_and_backfills(tmp_path):
    async def fmt(raw: str) -> NormalizedFeedbackFormatterOutput:
        assert "OCSF" in raw and "http" in raw  # 直接喂原始反馈（不经 governor）
        return NormalizedFeedbackFormatterOutput(title="OCSF 标准转换不合规", problem="转换输出不符合 OCSF process_activity 定义")

    svc, _ = _svc(tmp_path, fmt=fmt)
    rec = asyncio.run(svc.generate_normalized_feedback("imp-1"))
    assert rec.problem == "转换输出不符合 OCSF process_activity 定义"
    assert rec.generated_by == "llm"
    assert rec.possible_reason == "" and rec.possible_object == ""  # 原因/对象不在整理阶段产出
    assert svc._improvements.title_updates == ["OCSF 标准转换不合规"]  # 回填自动截断态标题


def test_heuristic_when_formatter_absent(tmp_path):
    svc, _ = _svc(tmp_path, fmt=None)
    rec = asyncio.run(svc.generate_normalized_feedback("imp-1"))
    assert rec.generated_by == "heuristic"
    assert rec.problem  # 兜底 problem 非空


def test_formatter_error_falls_back(tmp_path):
    async def boom(raw: str) -> NormalizedFeedbackFormatterOutput:
        raise RuntimeError("dspy sidecar down")

    svc, _ = _svc(tmp_path, fmt=boom)
    rec = asyncio.run(svc.generate_normalized_feedback("imp-1"))
    assert rec.generated_by == "heuristic"


def test_hostile_formatter_missing_problem_falls_back(tmp_path):
    async def bad(raw: str) -> NormalizedFeedbackFormatterOutput:
        return NormalizedFeedbackFormatterOutput(title="t", problem="")  # 空 problem → 校验抛

    svc, _ = _svc(tmp_path, fmt=bad)
    rec = asyncio.run(svc.generate_normalized_feedback("imp-1"))
    assert rec.generated_by == "heuristic"  # 不崩，回退


def test_backfill_does_not_overwrite_user_edited_title(tmp_path):
    async def fmt(raw: str) -> NormalizedFeedbackFormatterOutput:
        return NormalizedFeedbackFormatterOutput(title="LLM 标题", problem="p")

    svc, _ = _svc(tmp_path, title="用户自己改过的标题", fmt=fmt)  # 与原文无前缀关系
    asyncio.run(svc.generate_normalized_feedback("imp-1"))
    assert svc._improvements.title_updates == []  # 不覆盖用户编辑
