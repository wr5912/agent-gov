"""四阶段改进治理 §17.5：改进事项归因/方案 governor 生成服务（真 LLM 路径 + 确定性回退 + 字段所有权）。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.improvement_content_store import ImprovementContentStore
from app.services.improvement_governor_service import ImprovementGovernorService


def _content(tmp_path: Path) -> ImprovementContentStore:
    return ImprovementContentStore(make_session_factory(tmp_path / "runtime.sqlite3"))


class _FakeImprovements:
    def __init__(self, item: object) -> None:
        self._item = item

    def get_improvement(self, improvement_id: str) -> object:
        return self._item


def _item() -> SimpleNamespace:
    return SimpleNamespace(improvement_id="imp-1", title="告警误报治理", agent_id="soc-ops")


def _service(tmp_path: Path, run_profile_json) -> tuple[ImprovementGovernorService, ImprovementContentStore]:
    content = _content(tmp_path)
    content.upsert_normalized_feedback("imp-1", problem="告警误报", possible_object="MCP 数据", possible_reason="时间不一致", suggestion="加时间校验", user_quote="这是误报")
    svc = ImprovementGovernorService(improvement_store=_FakeImprovements(_item()), content_store=content, run_profile_json=run_profile_json)
    return svc, content


def test_attribution_governor_path_maps_agent_owned_fields(tmp_path: Path) -> None:
    """governor 成功：映射 rationale/responsibility_boundary/evidence_refs，标 generated_by=governor。"""
    async def fake_run(**_kwargs):
        return {
            "rationale": "MCP 返回的事件时间与告警时间窗口不一致，导致误判",
            "confidence": "high",
            "responsibility_boundary": {"owner": "external_mcp_service", "reason": "sec-ops-data 数据质量"},
            "evidence_refs": [{"type": "trace", "id": "run-1", "reason": "list_events 时间窗口不一致"}],
            "status": "confirmed",  # hostile: LLM 不能设 backend-owned 状态
        }
    svc, content = _service(tmp_path, fake_run)
    rec = asyncio.run(svc.generate_attribution("imp-1"))
    assert rec.generated_by == "governor"
    assert "时间窗口不一致" in rec.summary and "置信度 high" in rec.summary
    assert rec.responsibility_boundary == ["external_mcp_service：sec-ops-data 数据质量"]
    assert rec.evidence and "list_events" in rec.evidence[0]
    # 字段所有权：status 由后端定为 draft，LLM 的 "confirmed" 不得污染。
    assert content.get_attribution("imp-1").status == "draft"


def test_attribution_falls_back_to_heuristic_on_governor_failure(tmp_path: Path) -> None:
    async def boom(**_kwargs):
        raise RuntimeError("missing model credentials")
    svc, _ = _service(tmp_path, boom)
    rec = asyncio.run(svc.generate_attribution("imp-1"))
    assert rec.generated_by == "heuristic"
    assert "MCP 数据" in rec.summary and rec.status == "draft"


def test_attribution_none_runner_is_heuristic(tmp_path: Path) -> None:
    svc, _ = _service(tmp_path, None)
    rec = asyncio.run(svc.generate_attribution("imp-1"))
    assert rec.generated_by == "heuristic"


def test_hostile_formatter_output_does_not_crash_or_pollute(tmp_path: Path) -> None:
    """恶意/畸形 agent-owned 输出：缺字段、错类型、注入 backend-owned 字段，服务防御性映射且不污染。"""
    async def hostile(**_kwargs):
        return {
            "rationale": "",  # 空 → 退回启发式 summary
            "responsibility_boundary": "not-a-dict",
            "evidence_refs": "not-a-list",
            "attribution_id": "attacker-controlled",  # 不得被采纳
            "generated_by": "user-spoofed",  # 不得被采纳
        }
    svc, content = _service(tmp_path, hostile)
    rec = asyncio.run(svc.generate_attribution("imp-1"))
    assert rec.generated_by == "governor"  # 来源由后端判定，非 LLM 字段
    assert not rec.attribution_id.startswith("attacker")  # id 后端生成
    assert rec.status == "draft"
    assert rec.summary  # 防御性回退非空


def test_optimization_plan_governor_maps_tasks_to_changes(tmp_path: Path) -> None:
    async def fake_run(**_kwargs):
        return {
            "summary": "收紧时间一致性校验",
            "tasks": [
                {"target_type": "prompt", "recommendation": "新增事件时间与告警时间一致性校验"},
                {"target_path": "skills/triage.md", "title": "补充误报判定 SOP"},
            ],
        }
    content = _content(tmp_path)
    svc = ImprovementGovernorService(improvement_store=_FakeImprovements(_item()), content_store=content, run_profile_json=fake_run)
    rec = asyncio.run(svc.generate_optimization_plan("imp-1"))
    assert rec.generated_by == "governor"
    assert rec.summary == "收紧时间一致性校验"
    assert {c["target"] for c in rec.changes} == {"prompt", "skills/triage.md"}
    assert content.get_optimization_plan("imp-1").status == "draft"


def test_regression_governor_maps_eval_cases(tmp_path: Path) -> None:
    """governor EVAL_CASE_GENERATION → 回归用例候选（prompt/期望/检查点），generated_by=governor。"""
    async def fake_run(**_kwargs):
        return {"eval_cases": [
            {"prompt": "当事件时间与告警时间不一致时如何处置？", "expected_behavior": "先核验时间一致性，不直接升级", "checks_json": {"c1": "是否核验时间", "c2": "是否避免误升级"}},
        ]}
    svc, content = _service(tmp_path, fake_run)
    rec = asyncio.run(svc.generate_regression_assessment("imp-1"))
    assert rec.generated_by == "governor"
    assert rec.cases and rec.cases[0]["prompt"].startswith("当事件时间")
    assert rec.cases[0]["checkpoints"] == ["是否核验时间", "是否避免误升级"]
    assert content.get_regression_assessment("imp-1").status == "draft"


def test_regression_heuristic_fallback(tmp_path: Path) -> None:
    async def boom(**_kwargs):
        raise RuntimeError("no governor")
    svc, _ = _service(tmp_path, boom)
    rec = asyncio.run(svc.generate_regression_assessment("imp-1"))
    assert rec.generated_by == "heuristic" and rec.cases and rec.cases[0]["checkpoints"]


def test_optimization_plan_heuristic_fallback(tmp_path: Path) -> None:
    async def boom(**_kwargs):
        raise TimeoutError("governor timeout")
    content = _content(tmp_path)
    svc = ImprovementGovernorService(improvement_store=_FakeImprovements(_item()), content_store=content, run_profile_json=boom)
    rec = asyncio.run(svc.generate_optimization_plan("imp-1"))
    assert rec.generated_by == "heuristic" and rec.changes and rec.status == "draft"


def test_attribution_governor_maps_counter_evidence_uncertainty_verification(tmp_path: Path) -> None:
    """归因新增 agent-owned 字段（反证/不确定性/验证建议）正确映射、空项过滤，backend-owned 不污染。"""
    async def fake_run(**_kwargs):
        return {
            "rationale": "时间窗口不一致导致误判",
            "counter_evidence": ["非边界时段未出现同类误判", "  "],
            "uncertainty_factors": ["数据源时区标注覆盖率不足"],
            "verification_suggestions": ["多时区数据回放验证边界"],
            "attribution_id": "attacker-controlled",  # hostile：不得采纳
            "status": "confirmed",  # hostile：不得污染
        }
    svc, content = _service(tmp_path, fake_run)
    rec = asyncio.run(svc.generate_attribution("imp-1"))
    assert rec.counter_evidence == ["非边界时段未出现同类误判"]  # 空白项过滤
    assert rec.uncertainty_factors == ["数据源时区标注覆盖率不足"]
    assert rec.verification_suggestions == ["多时区数据回放验证边界"]
    assert rec.status == "draft" and not rec.attribution_id.startswith("attacker")


def test_attribution_heuristic_provides_uncertainty_and_verification(tmp_path: Path) -> None:
    async def boom(**_kwargs):
        raise RuntimeError("no governor")
    svc, _ = _service(tmp_path, boom)
    rec = asyncio.run(svc.generate_attribution("imp-1"))
    assert rec.generated_by == "heuristic"
    assert rec.uncertainty_factors and rec.verification_suggestions  # 启发式诚实默认非空


def test_optimization_plan_maps_risk_level(tmp_path: Path) -> None:
    async def fake_run(**_kwargs):
        return {"summary": "收紧校验", "risk": "中", "tasks": [{"target_type": "prompt", "recommendation": "加时间一致性校验"}]}
    content = _content(tmp_path)
    svc = ImprovementGovernorService(improvement_store=_FakeImprovements(_item()), content_store=content, run_profile_json=fake_run)
    rec = asyncio.run(svc.generate_optimization_plan("imp-1"))
    assert rec.risk_level == "中" and rec.generated_by == "governor"


def test_optimization_plan_heuristic_provides_risk_level(tmp_path: Path) -> None:
    async def boom(**_kwargs):
        raise RuntimeError("no governor")
    content = _content(tmp_path)
    svc = ImprovementGovernorService(improvement_store=_FakeImprovements(_item()), content_store=content, run_profile_json=boom)
    rec = asyncio.run(svc.generate_optimization_plan("imp-1"))
    assert rec.risk_level  # 启发式给出风险级别


def test_regression_maps_suggested_gate_thresholds(tmp_path: Path) -> None:
    async def fake_run(**_kwargs):
        return {
            "eval_cases": [{"prompt": "时间不一致如何处置？", "expected_behavior": "先核验时间一致性"}],
            "suggested_gate_thresholds": {"pass_rate": "≥97%", "new_critical": "0", "blank": ""},
        }
    svc, content = _service(tmp_path, fake_run)
    rec = asyncio.run(svc.generate_regression_assessment("imp-1"))
    assert rec.suggested_gate_thresholds == {"pass_rate": "≥97%", "new_critical": "0"}  # 空值过滤


def test_regression_heuristic_provides_default_gate_thresholds(tmp_path: Path) -> None:
    async def boom(**_kwargs):
        raise RuntimeError("no governor")
    svc, _ = _service(tmp_path, boom)
    rec = asyncio.run(svc.generate_regression_assessment("imp-1"))
    assert rec.generated_by == "heuristic" and rec.suggested_gate_thresholds  # 标准 SLA 默认非空


def test_execution_store_roundtrips_risk_and_rollback(tmp_path: Path) -> None:
    """执行记录新增 risk_level/rollback_strategy/rollback_instructions 的 DB 列 + Record 映射回环。"""
    content = _content(tmp_path)
    content.upsert_execution(
        "imp-1", summary="已应用", risk_level="中",
        rollback_strategy="回滚到执行前基线 Agent 版本", rollback_instructions=["放弃 change_set", "恢复版本"],
    )
    got = content.get_execution("imp-1")
    assert got is not None
    assert got.risk_level == "中"
    assert got.rollback_strategy == "回滚到执行前基线 Agent 版本"
    assert got.rollback_instructions == ["放弃 change_set", "恢复版本"]
