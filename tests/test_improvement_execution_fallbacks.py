"""改进执行未生成候选版本时的 fallback 与前置失败契约。"""

from __future__ import annotations

import asyncio

import pytest
from app.runtime.errors import BusinessRuleViolation

from feedback_store_test_utils import _seed_execution_record
from improvement_execution_test_support import _confirm_plan, _FakeGovernance, _service, _stage


def test_heuristic_when_no_runner(tmp_path):
    svc, content = _service(
        tmp_path,
        gov=_FakeGovernance(tmp_path),
        run_profile_json=None,
    )
    _confirm_plan(content)

    rec = asyncio.run(svc.generate_and_apply_execution("imp-1"))

    assert rec.generated_by == "heuristic"
    assert not rec.applied_agent_version_id
    assert not rec.changes_applied


def test_missing_plan_rejected_without_creating_execution(tmp_path):
    gov = _FakeGovernance(tmp_path)

    async def fake(**_kwargs):
        raise AssertionError("governor should not run without a plan")

    svc, content = _service(tmp_path, gov=gov, run_profile_json=fake)
    with pytest.raises(BusinessRuleViolation):
        asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert content.get_execution("imp-1") is None


def test_governor_decline_abandons_and_falls_back(tmp_path):
    gov = _FakeGovernance(tmp_path)

    async def declines(**_kwargs):
        return {
            "status": "needs_human_review",
            "summary": "",
            "operations": [],
            "no_action_reason": "目标文件不存在，需人工",
        }

    svc, content = _service(tmp_path, gov=gov, run_profile_json=declines)
    _confirm_plan(content)
    rec = asyncio.run(svc.generate_and_apply_execution("imp-1"))

    assert rec.generated_by == "heuristic"
    assert "目标文件不存在" in rec.summary
    assert gov.abandoned == gov.created
    assert gov.store.removed == gov.created
    assert _stage(tmp_path) == "optimization"


def test_unbound_heuristic_execution_does_not_block_reapply(tmp_path):
    gov = _FakeGovernance(tmp_path)
    calls = {"n": 0}

    async def ready(**_kwargs):
        calls["n"] += 1
        return {
            "status": "ready",
            "summary": "旧记录已被真实执行覆盖",
            "operations": [
                {
                    "operation": "append_text",
                    "path": "CLAUDE.md",
                    "append_text": "x",
                    "expected_sha256": "s",
                }
            ],
        }

    svc, content = _service(tmp_path, gov=gov, run_profile_json=ready)
    _confirm_plan(content)
    _seed_execution_record(
        content,
        "imp-1",
        summary="已按优化方案应用变更并生成新版本（初步记录，待执行引擎对接）。",
        changes_applied=["prompt：旧占位"],
        agent_version="",
        generated_by="heuristic",
    )

    rec = asyncio.run(svc.generate_and_apply_execution("imp-1"))

    assert calls["n"] == 1
    assert rec.generated_by == "governor"
    assert rec.change_set_id == gov.created[0]
    assert rec.applied_agent_version_id == "ver-cand-sha"
    assert rec.summary == "旧记录已被真实执行覆盖"
