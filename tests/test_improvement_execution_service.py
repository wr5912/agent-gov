"""四阶段改进治理 §17.5 第二阶段：执行记录 governor 自动 apply + 候选版本绑定（编排逻辑，git 层用 fake）。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.improvement_content_store import ImprovementContentStore
from app.services.improvement_execution_service import ImprovementExecutionService


def _content(tmp_path: Path) -> ImprovementContentStore:
    return ImprovementContentStore(make_session_factory(tmp_path / "runtime.sqlite3"))


class _FakeImprovements:
    def __init__(self) -> None:
        self.links: list[tuple[str, str, str]] = []

    def get_improvement(self, improvement_id: str) -> object:
        return SimpleNamespace(improvement_id=improvement_id, agent_id="soc-ops", title="告警误报治理")

    def add_link(self, improvement_id: str, *, kind: str, ref_id: str) -> object:
        self.links.append((improvement_id, kind, ref_id))
        return SimpleNamespace(improvement_id=improvement_id, kind=kind, ref_id=ref_id)


class _FakeStore:
    def version_summary(self, sha, *, reason, note=None):
        return {"agent_version_id": f"ver-{sha}"}

    def commit_worktree(self, worktree, *, message):
        return "cand-sha"

    def diff_versions(self, a, b):
        return {"changed_files": ["CLAUDE.md"], "from": a, "to": b}


class _FakeGovernance:
    def __init__(self, worktree: Path) -> None:
        self._worktree = worktree
        self.abandoned: list[str] = []
        self.committed: list[str] = []

    def create_change_set(self, *, agent_id, title, note):
        return {"change_set_id": "agc-1", "agent_id": agent_id, "base_commit_sha": "base-sha"}

    def change_set_worktree_path(self, change_set):
        return self._worktree

    def _store_for(self, agent_id):
        return _FakeStore()

    def mark_candidate_committed(self, change_set_id, *, candidate_commit_sha, execution_job_id=None, note=None):
        self.committed.append(change_set_id)
        return {"change_set_id": change_set_id, "candidate_commit_sha": candidate_commit_sha}

    def abandon_change_set(self, change_set_id, *, operator="runtime", note=None):
        self.abandoned.append(change_set_id)
        return {"change_set_id": change_set_id, "status": "abandoned"}


class _FakeExecApp:
    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises
        self.applied: list[list] = []

    def apply_execution_operations(self, operations, *, workspace_dir=None, target_policy=None, content_guard=None):
        if self.raises:
            raise RuntimeError("apply blew up")
        self.applied.append(operations)


def _service(tmp_path, *, gov, run_profile_json, exec_app=None):
    content = _content(tmp_path)
    svc = ImprovementExecutionService(
        improvement_store=_FakeImprovements(),
        content_store=content,
        agent_governance=gov,
        execution_app=exec_app or _FakeExecApp(),
        run_profile_json=run_profile_json,
    )
    return svc, content


def _confirm_plan(content, improvement_id="imp-1"):
    content.upsert_optimization_plan(improvement_id, summary="收紧时间校验", changes=[{"target": "prompt", "change": "加时间校验"}])
    content.set_optimization_plan_status(improvement_id, status="confirmed")


def test_heuristic_when_no_runner(tmp_path):
    content = _content(tmp_path)
    _confirm_plan(content)
    svc = ImprovementExecutionService(improvement_store=_FakeImprovements(), content_store=content,
                                      agent_governance=_FakeGovernance(tmp_path), execution_app=_FakeExecApp(), run_profile_json=None)
    rec = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert rec.generated_by == "heuristic" and not rec.applied_agent_version_id and rec.changes_applied


def test_heuristic_when_plan_not_confirmed(tmp_path):
    gov = _FakeGovernance(tmp_path)
    async def fake(**_k):
        raise AssertionError("governor should not run without a confirmed plan")
    svc, content = _service(tmp_path, gov=gov, run_profile_json=fake)
    content.upsert_optimization_plan("imp-1", summary="draft 方案", changes=[{"target": "prompt", "change": "x"}])  # 仍 draft
    rec = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert rec.generated_by == "heuristic" and not gov.abandoned


def test_governor_decline_abandons_and_falls_back(tmp_path):
    gov = _FakeGovernance(tmp_path)
    async def declines(**_k):
        return {"status": "needs_human_review", "summary": "", "operations": [], "no_action_reason": "目标文件不存在，需人工"}
    svc, content = _service(tmp_path, gov=gov, run_profile_json=declines)
    _confirm_plan(content)
    rec = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert rec.generated_by == "heuristic"
    assert "目标文件不存在" in rec.summary
    assert gov.abandoned == ["agc-1"]  # 拒绝时放弃隔离 change set，不留 worktree


def test_governor_success_applies_and_binds_version(tmp_path):
    gov = _FakeGovernance(tmp_path)
    exec_app = _FakeExecApp()
    async def ready(**kwargs):
        kwargs["trace_callback"]({"trace_id": "tr-exec", "trace_url": "http://lf/tr-exec"})
        return {"status": "ready", "summary": "已在 CLAUDE.md 补充时间校验指令",
                "operations": [{"operation": "append_text", "path": "CLAUDE.md", "append_text": "x", "expected_sha256": "s"}]}
    svc, content = _service(tmp_path, gov=gov, run_profile_json=ready, exec_app=exec_app)
    _confirm_plan(content)
    rec = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert rec.generated_by == "governor"
    assert rec.applied_agent_version_id == "ver-cand-sha"
    assert rec.change_set_id == "agc-1"
    assert rec.applied_diff.get("changed_files") == ["CLAUDE.md"]
    assert rec.generation_trace_id == "tr-exec"
    assert rec.generation_trace_url == "http://lf/tr-exec"
    assert exec_app.applied and gov.committed == ["agc-1"] and not gov.abandoned


def test_apply_failure_abandons_change_set_and_falls_back(tmp_path):
    gov = _FakeGovernance(tmp_path)
    exec_app = _FakeExecApp(raises=True)
    async def ready(**_k):
        return {"status": "ready", "summary": "s", "operations": [{"operation": "append_text", "path": "CLAUDE.md", "append_text": "x", "expected_sha256": "s"}]}
    svc, content = _service(tmp_path, gov=gov, run_profile_json=ready, exec_app=exec_app)
    _confirm_plan(content)
    rec = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert rec.generated_by == "heuristic"  # apply 失败回退，不留半成品版本
    assert gov.abandoned == ["agc-1"] and not gov.committed


def test_idempotent_when_already_applied(tmp_path):
    gov = _FakeGovernance(tmp_path)
    calls = {"n": 0}
    async def ready(**_k):
        calls["n"] += 1
        return {"status": "ready", "summary": "s", "operations": [{"operation": "append_text", "path": "CLAUDE.md", "append_text": "x", "expected_sha256": "s"}]}
    svc, content = _service(tmp_path, gov=gov, run_profile_json=ready)
    _confirm_plan(content)
    first = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert first.applied_agent_version_id
    again = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert again.applied_agent_version_id == first.applied_agent_version_id
    assert calls["n"] == 1  # 第二次幂等返回，不再调 governor
