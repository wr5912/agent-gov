"""反馈整理 NormalizedFeedback：一次 DSPy formatter 把原始反馈归纳成 title+problem（无 governor）；
formatter 不可用/校验失败回退启发式；title 仅在自动截断态时回填（不覆盖用户手改）；原因分析不在整理阶段产出。"""

from __future__ import annotations

import asyncio

from app.runtime.improvement_db import ImprovementItemModel
from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.improvement_content_store import ImprovementContentStore
from app.runtime.stores.improvement_store import ImprovementStore
from app.services.improvement_governor_service import ImprovementGovernorService

RAW = "转换后的数据中有API，不符合OCSF官方标准定义https://schema.ocsf.io/1.8.0/classes/process_activity"
AUTO_TITLE = RAW[:40]  # 前端 firstSentence 的截断自动态


def _svc(tmp_path, *, title=AUTO_TITLE):
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    content = ImprovementContentStore(factory)
    improvements = ImprovementStore(factory)
    with factory.begin() as db:
        db.add(ImprovementItemModel(improvement_id="imp-1", title=title, agent_id="a", summary=""))
    svc = ImprovementGovernorService(
        improvement_store=improvements,
        content_store=content,
        run_profile_json=None,
        data_dir=tmp_path / "data",
    )
    content.create_feedback("imp-1", agent_id="a", summary="转换数据不符合 OCSF 规范", raw_text=RAW)
    return svc, content


def test_heuristic_when_governor_absent(tmp_path):
    svc, _ = _svc(tmp_path)
    rec = asyncio.run(svc.generate_normalized_feedback("imp-1"))
    assert rec.generated_by == "heuristic"
    assert rec.problem  # 兜底 problem 非空
