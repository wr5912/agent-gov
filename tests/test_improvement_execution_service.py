"""四阶段改进治理 §17.5 第二阶段：执行记录 governor 自动 apply + 候选版本绑定（编排逻辑，git 层用 fake）。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime.errors import BusinessRuleViolation
from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.improvement_content_store import ImprovementContentStore
from app.services.improvement_execution_service import ImprovementExecutionService
from app.services.workspace_execution_applier import WorkspaceExecutionApplier


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

    def apply_execution_operations(self, operations, *, workspace_dir=None, target_policy=None, content_guard=None, allowed_targets=None):
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


def test_missing_plan_rejected_without_creating_execution(tmp_path):
    gov = _FakeGovernance(tmp_path)
    async def fake(**_k):
        raise AssertionError("governor should not run without a plan")
    svc, content = _service(tmp_path, gov=gov, run_profile_json=fake)
    with pytest.raises(BusinessRuleViolation):
        asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert content.get_execution("imp-1") is None


def test_draft_plan_is_confirmed_before_governor_apply(tmp_path):
    gov = _FakeGovernance(tmp_path)
    calls = {"n": 0}
    async def fake(**_k):
        calls["n"] += 1
        return {"status": "ready", "summary": "已执行 draft 方案", "operations": [{"operation": "append_text", "path": "CLAUDE.md", "append_text": "x", "expected_sha256": "s"}]}
    svc, content = _service(tmp_path, gov=gov, run_profile_json=fake)
    content.upsert_optimization_plan("imp-1", summary="draft 方案", changes=[{"target": "prompt", "change": "x"}])  # 仍 draft
    rec = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert calls["n"] == 1
    assert rec.generated_by == "governor"
    assert rec.change_set_id == "agc-1"
    assert content.get_optimization_plan("imp-1").status == "confirmed"


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


def test_real_guard_blocks_settings_escalation_and_falls_back(tmp_path):
    """集成：真实 applier + 护栏。governor 产出 settings 提权 operation → 护栏拦截 → abandon change set →
    回退启发式，且提权内容未落盘。守护 improvement_execution_service 真接了 guard + allowlist。"""
    worktree = tmp_path / "worktree"
    (worktree / ".claude").mkdir(parents=True)
    settings = worktree / ".claude" / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["Read(./**)"], "deny": ["Read(/**/.env)"]}}), encoding="utf-8")
    sha = hashlib.sha256(settings.read_bytes()).hexdigest()
    gov = _FakeGovernance(worktree)

    async def escalate(**kwargs):
        return {
            "status": "ready",
            "summary": "s",
            "operations": [
                {
                    "operation": "replace_file",
                    "path": ".claude/settings.json",
                    "expected_sha256": sha,
                    "content": json.dumps({"permissions": {"allow": ["Read(./**)", "Bash(*)"], "deny": ["Read(/**/.env)"]}}),
                }
            ],
        }

    svc, content = _service(tmp_path, gov=gov, run_profile_json=escalate, exec_app=WorkspaceExecutionApplier())
    _confirm_plan(content)
    rec = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert rec.generated_by == "heuristic"  # 护栏拦截 → 回退
    assert gov.abandoned == ["agc-1"] and not gov.committed
    assert "Bash(*)" not in settings.read_text(encoding="utf-8")  # 提权内容未落盘


def test_real_allowlist_blocks_settings_local_and_falls_back(tmp_path):
    """集成：governor 试图写白名单外的 settings.local.json → applier allowlist 拦截 → abandon → 回退。"""
    worktree = tmp_path / "worktree2"
    (worktree / ".claude").mkdir(parents=True)
    gov = _FakeGovernance(worktree)

    async def offlist(**kwargs):
        return {
            "status": "ready",
            "summary": "s",
            "operations": [{"operation": "create_file", "path": ".claude/settings.local.json", "content": json.dumps({"permissions": {"allow": ["Bash(*)"]}})}],
        }

    svc, content = _service(tmp_path, gov=gov, run_profile_json=offlist, exec_app=WorkspaceExecutionApplier())
    _confirm_plan(content)
    rec = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert rec.generated_by == "heuristic"
    assert gov.abandoned == ["agc-1"] and not gov.committed
    assert not (worktree / ".claude" / "settings.local.json").exists()  # 白名单外未落盘


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


def test_unbound_heuristic_execution_does_not_block_reapply(tmp_path):
    gov = _FakeGovernance(tmp_path)
    calls = {"n": 0}
    async def ready(**_k):
        calls["n"] += 1
        return {"status": "ready", "summary": "旧记录已被真实执行覆盖", "operations": [{"operation": "append_text", "path": "CLAUDE.md", "append_text": "x", "expected_sha256": "s"}]}
    svc, content = _service(tmp_path, gov=gov, run_profile_json=ready)
    _confirm_plan(content)
    content.upsert_execution(
        "imp-1",
        summary="已按优化方案应用变更并生成新版本（初步记录，待执行引擎对接）。",
        changes_applied=["prompt：旧占位"],
        agent_version="",
        generated_by="heuristic",
    )

    rec = asyncio.run(svc.generate_and_apply_execution("imp-1"))

    assert calls["n"] == 1
    assert rec.generated_by == "governor"
    assert rec.change_set_id == "agc-1"
    assert rec.applied_agent_version_id == "ver-cand-sha"
    assert rec.summary == "旧记录已被真实执行覆盖"
