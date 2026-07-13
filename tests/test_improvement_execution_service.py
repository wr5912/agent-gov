"""四阶段改进治理 §17.5 第二阶段：执行记录 governor 自动 apply + 候选版本绑定（编排逻辑，git 层用 fake）。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.runtime.agent_git_store import AgentGitError
from app.runtime.errors import BusinessRuleViolation, ConflictError, RuntimeUnavailableError
from app.runtime.improvement_db import ImprovementItemModel, OptimizationPlanModel
from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.improvement_content_store import ImprovementContentStore
from app.runtime.stores.improvement_store import ImprovementStore
from app.services.improvement_execution_service import ImprovementExecutionService
from app.services.workspace_execution_applier import WorkspaceExecutionApplier


def _content(tmp_path: Path) -> ImprovementContentStore:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    with factory.begin() as db:
        if db.get(ImprovementItemModel, "imp-1") is None:
            db.add(
                ImprovementItemModel(
                    improvement_id="imp-1",
                    agent_id="soc-ops",
                    title="告警误报治理",
                    improvement_stage="optimization",
                    improvement_status="active",
                )
            )
    return ImprovementContentStore(factory)


class _FakeImprovements:
    def __init__(self) -> None:
        self.links: list[tuple[str, str, str]] = []
        self.fail_link_once = False

    def get_improvement(self, improvement_id: str) -> object:
        return SimpleNamespace(improvement_id=improvement_id, agent_id="soc-ops", title="告警误报治理")

    def add_link(self, improvement_id: str, *, kind: str, ref_id: str) -> object:
        if self.fail_link_once:
            self.fail_link_once = False
            raise RuntimeError("link insert failed")
        if (improvement_id, kind, ref_id) not in self.links:
            self.links.append((improvement_id, kind, ref_id))
        return SimpleNamespace(improvement_id=improvement_id, kind=kind, ref_id=ref_id)

    def list_links(self, improvement_id: str) -> list[object]:
        return [SimpleNamespace(improvement_id=i, kind=kind, ref_id=ref) for i, kind, ref in self.links if i == improvement_id]


class _FakeStore:
    def __init__(self) -> None:
        self.head = "base-sha"
        self.removed: list[str] = []
        self.cleanup_modes: list[tuple[str, bool]] = []

    def current_commit_sha(self):
        return "base-sha"

    def version_summary(self, sha, *, reason, note=None):
        return {"agent_version_id": f"ver-{sha}"}

    def commit_worktree(self, worktree, *, message):
        self.head = "cand-sha"
        return "cand-sha"

    def diff_versions(self, a, b):
        return {"changed_files": ["CLAUDE.md"] if a != b else [], "from": a, "to": b}

    def worktree_commit_sha(self, worktree):
        return self.head

    def reset_worktree(self, worktree, *, base_ref):
        self.head = base_ref

    def remove_worktree(self, change_set_id, *, delete_branch=True):
        self.removed.append(change_set_id)
        self.cleanup_modes.append((change_set_id, delete_branch))


class _FakeGovernance:
    def __init__(self, worktree: Path) -> None:
        self._worktree = worktree
        self.abandoned: list[str] = []
        self.committed: list[str] = []
        self.created: list[str] = []
        self.change_set_status = "draft"  # get_change_set 返回态；测试可改为 abandoned/rejected 模拟失效
        self.change_sets: dict[str, dict] = {}
        self.store = _FakeStore()

    def create_change_set(self, *, agent_id, title, note, execution_job_id, base_commit_sha, change_set_id, source=None):
        self.change_set_status = "draft"
        if change_set_id not in self.created:
            self.created.append(change_set_id)
        self.change_sets.setdefault(
            change_set_id,
            {
                "change_set_id": change_set_id,
                "agent_id": agent_id,
                "base_commit_sha": base_commit_sha,
                "candidate_commit_sha": None,
                "execution_job_id": execution_job_id,
                "status": "draft",
                "worktree_path": str(self._worktree),
                "source_improvement_id": source.improvement_id if source else None,
                "source_attribution_id": source.attribution_id if source else None,
                "source_attribution_status": source.attribution_status if source else None,
            },
        )
        return dict(self.change_sets[change_set_id])

    def get_change_set(self, change_set_id):
        existing = self.change_sets.get(change_set_id)
        if existing is not None:
            return dict(existing)
        return {
            "change_set_id": change_set_id,
            "agent_id": "soc-ops",
            "base_commit_sha": "base-sha",
            "candidate_commit_sha": None,
            "status": self.change_set_status,
            "worktree_path": str(self._worktree),
        }

    def change_set_worktree_path(self, change_set):
        return self._worktree

    def _store_for(self, agent_id):
        return self.store

    def mark_candidate_committed(self, change_set_id, *, candidate_commit_sha, execution_job_id=None, note=None, operator="runtime"):
        self.committed.append(change_set_id)
        row = self.change_sets[change_set_id]
        row.update(candidate_commit_sha=candidate_commit_sha, execution_job_id=execution_job_id, status="candidate_committed")
        return dict(row)

    def abandon_change_set(self, change_set_id, *, operator="runtime", note=None):
        self.abandoned.append(change_set_id)
        row = self.change_sets.setdefault(
            change_set_id,
            {"change_set_id": change_set_id, "base_commit_sha": "base-sha", "worktree_path": str(self._worktree)},
        )
        row["status"] = "abandoned"
        return dict(row)


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


def _claim_source(content: ImprovementContentStore, improvement_id: str = "imp-1") -> dict[str, str]:
    plan = content.get_optimization_plan(improvement_id)
    attribution = content.get_attribution(improvement_id)
    assert plan is not None
    return {
        "source_optimization_plan_id": plan.optimization_plan_id,
        "source_optimization_plan_updated_at": plan.updated_at,
        "source_attribution_id": attribution.attribution_id if attribution else "",
        "source_attribution_updated_at": attribution.updated_at if attribution else "",
    }


def _stage(tmp_path: Path) -> str:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    with factory() as db:
        return str(db.get(ImprovementItemModel, "imp-1").improvement_stage)


def test_heuristic_when_no_runner(tmp_path):
    content = _content(tmp_path)
    _confirm_plan(content)
    svc = ImprovementExecutionService(
        improvement_store=_FakeImprovements(),
        content_store=content,
        agent_governance=_FakeGovernance(tmp_path),
        execution_app=_FakeExecApp(),
        run_profile_json=None,
    )
    rec = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert rec.generated_by == "heuristic" and not rec.applied_agent_version_id and not rec.changes_applied  # C1：heuristic 不填 changes_applied


def test_missing_plan_rejected_without_creating_execution(tmp_path):
    gov = _FakeGovernance(tmp_path)

    async def fake(**_k):
        raise AssertionError("governor should not run without a plan")

    svc, content = _service(tmp_path, gov=gov, run_profile_json=fake)
    with pytest.raises(BusinessRuleViolation):
        asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert content.get_execution("imp-1") is None


def test_draft_plan_blocks_execution_until_separately_confirmed(tmp_path):
    gov = _FakeGovernance(tmp_path)
    calls = {"n": 0}

    async def fake(**_k):
        calls["n"] += 1
        return {
            "status": "ready",
            "summary": "已执行 draft 方案",
            "operations": [{"operation": "append_text", "path": "CLAUDE.md", "append_text": "x", "expected_sha256": "s"}],
        }

    svc, content = _service(tmp_path, gov=gov, run_profile_json=fake)
    content.upsert_optimization_plan("imp-1", summary="draft 方案", changes=[{"target": "prompt", "change": "x"}])  # 仍 draft
    with pytest.raises(ConflictError, match="confirmed optimization plan"):
        asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert calls["n"] == 0 and gov.created == []
    assert content.get_optimization_plan("imp-1").status == "draft"


def test_draft_attribution_blocks_execution_before_change_set_creation(tmp_path):
    gov = _FakeGovernance(tmp_path)

    async def should_not_run(**_kwargs):
        raise AssertionError("governor must not run with draft attribution")

    svc, content = _service(tmp_path, gov=gov, run_profile_json=should_not_run)
    _confirm_plan(content)
    content.upsert_attribution("imp-1", summary="尚未确认的归因")

    with pytest.raises(ConflictError, match="confirmed attribution"):
        asyncio.run(svc.generate_and_apply_execution("imp-1"))

    assert gov.created == [] and content.get_execution("imp-1") is None


def test_governor_decline_abandons_and_falls_back(tmp_path):
    gov = _FakeGovernance(tmp_path)

    async def declines(**_k):
        return {"status": "needs_human_review", "summary": "", "operations": [], "no_action_reason": "目标文件不存在，需人工"}

    svc, content = _service(tmp_path, gov=gov, run_profile_json=declines)
    _confirm_plan(content)
    rec = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert rec.generated_by == "heuristic"
    assert "目标文件不存在" in rec.summary
    assert gov.abandoned == gov.created
    assert gov.store.removed == gov.created
    assert _stage(tmp_path) == "optimization"


def test_governor_success_applies_and_binds_version(tmp_path):
    gov = _FakeGovernance(tmp_path)
    exec_app = _FakeExecApp()

    async def ready(**kwargs):
        kwargs["trace_callback"]({"trace_id": "tr-exec", "trace_url": "http://lf/tr-exec"})
        return {
            "status": "ready",
            "summary": "已在 CLAUDE.md 补充时间校验指令",
            "operations": [{"operation": "append_text", "path": "CLAUDE.md", "append_text": "x", "expected_sha256": "s"}],
        }

    svc, content = _service(tmp_path, gov=gov, run_profile_json=ready, exec_app=exec_app)
    _confirm_plan(content)
    attribution = content.upsert_attribution("imp-1", summary="外部数据时间不一致")
    content.set_attribution_status("imp-1", status="confirmed")
    rec = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert rec.generated_by == "governor"
    assert rec.applied_agent_version_id == "ver-cand-sha"
    assert rec.change_set_id == gov.created[0]
    assert rec.applied_diff.get("changed_files") == ["CLAUDE.md"]
    assert rec.generation_trace_id == "tr-exec"
    assert rec.generation_trace_url == "http://lf/tr-exec"
    assert exec_app.applied and gov.committed == gov.created and not gov.abandoned
    change_set = gov.change_sets[rec.change_set_id]
    assert change_set["source_improvement_id"] == "imp-1"
    assert change_set["source_attribution_id"] == attribution.attribution_id
    assert change_set["source_attribution_status"] == "confirmed"


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
    assert gov.abandoned == gov.created and gov.store.removed == gov.created and not gov.committed
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
    assert gov.abandoned == gov.created and gov.store.removed == gov.created and not gov.committed
    assert not (worktree / ".claude" / "settings.local.json").exists()  # 白名单外未落盘


def test_apply_failure_abandons_change_set_and_falls_back(tmp_path):
    gov = _FakeGovernance(tmp_path)
    exec_app = _FakeExecApp(raises=True)

    async def ready(**_k):
        return {
            "status": "ready",
            "summary": "s",
            "operations": [{"operation": "append_text", "path": "CLAUDE.md", "append_text": "x", "expected_sha256": "s"}],
        }

    svc, content = _service(tmp_path, gov=gov, run_profile_json=ready, exec_app=exec_app)
    _confirm_plan(content)
    rec = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert rec.generated_by == "heuristic"  # apply 失败回退，不留半成品版本
    assert gov.abandoned == gov.created and gov.store.removed == gov.created and not gov.committed


def test_idempotent_when_already_applied(tmp_path):
    gov = _FakeGovernance(tmp_path)
    calls = {"n": 0}

    async def ready(**_k):
        calls["n"] += 1
        return {
            "status": "ready",
            "summary": "s",
            "operations": [{"operation": "append_text", "path": "CLAUDE.md", "append_text": "x", "expected_sha256": "s"}],
        }

    svc, content = _service(tmp_path, gov=gov, run_profile_json=ready)
    _confirm_plan(content)
    first = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert first.applied_agent_version_id
    again = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert again.applied_agent_version_id == first.applied_agent_version_id
    assert calls["n"] == 1  # 第二次幂等返回，不再调 governor


def test_applied_execution_is_not_reused_for_a_new_plan_revision(tmp_path):
    gov = _FakeGovernance(tmp_path)
    calls = {"n": 0}

    async def ready(**_kwargs):
        calls["n"] += 1
        return {
            "status": "ready",
            "summary": "bound to one plan revision",
            "operations": [{"operation": "append_text", "path": "CLAUDE.md", "append_text": "x", "expected_sha256": "s"}],
        }

    svc, content = _service(tmp_path, gov=gov, run_profile_json=ready)
    _confirm_plan(content)
    first = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    content.upsert_optimization_plan("imp-1", summary="new plan revision", changes=[{"target": "prompt", "change": "new"}])
    content.set_optimization_plan_status("imp-1", status="confirmed")

    with pytest.raises(ConflictError, match="different plan or attribution revision"):
        asyncio.run(svc.generate_and_apply_execution("imp-1"))

    assert first.applied_agent_version_id and calls["n"] == 1


def test_unbound_heuristic_execution_does_not_block_reapply(tmp_path):
    gov = _FakeGovernance(tmp_path)
    calls = {"n": 0}

    async def ready(**_k):
        calls["n"] += 1
        return {
            "status": "ready",
            "summary": "旧记录已被真实执行覆盖",
            "operations": [{"operation": "append_text", "path": "CLAUDE.md", "append_text": "x", "expected_sha256": "s"}],
        }

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
    assert rec.change_set_id == gov.created[0]
    assert rec.applied_agent_version_id == "ver-cand-sha"
    assert rec.summary == "旧记录已被真实执行覆盖"


def test_existing_unapplied_change_set_resumes_instead_of_false_idempotence(tmp_path):
    gov = _FakeGovernance(tmp_path)
    calls = {"n": 0}

    async def ready(**_k):
        calls["n"] += 1
        return {
            "status": "ready",
            "summary": "s",
            "operations": [{"operation": "append_text", "path": "CLAUDE.md", "append_text": "x", "expected_sha256": "s"}],
        }

    svc, content = _service(tmp_path, gov=gov, run_profile_json=ready)
    _confirm_plan(content)
    content.upsert_execution("imp-1", summary="已绑定候选变更集", changes_applied=[], agent_version="", generated_by="governor", change_set_id="agc-1")
    rec = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert calls["n"] == 1 and rec.change_set_id == "agc-1" and rec.applied_agent_version_id


def test_reapply_when_change_set_invalidated(tmp_path):
    # C3：绑定的 change_set 已被 abandon/reject -> 不再幂等短路，允许重跑
    gov = _FakeGovernance(tmp_path)
    gov.change_sets["agc-old"] = {
        "change_set_id": "agc-old",
        "agent_id": "soc-ops",
        "base_commit_sha": "base-sha",
        "candidate_commit_sha": "audit-candidate-sha",
        "status": "abandoned",
        "worktree_path": str(tmp_path),
    }
    calls = {"n": 0}

    async def ready(**_k):
        calls["n"] += 1
        return {
            "status": "ready",
            "summary": "重跑生成新候选",
            "operations": [{"operation": "append_text", "path": "CLAUDE.md", "append_text": "x", "expected_sha256": "s"}],
        }

    svc, content = _service(tmp_path, gov=gov, run_profile_json=ready)
    _confirm_plan(content)
    content.upsert_execution("imp-1", summary="旧绑定已作废", changes_applied=[], agent_version="", generated_by="governor", change_set_id="agc-old")
    rec = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert calls["n"] == 1 and rec.generated_by == "governor"
    assert ("agc-old", False) in gov.store.cleanup_modes


def test_runtime_unavailable_surfaces_not_heuristic(tmp_path):
    # C2：governor 基础设施不可用（RuntimeUnavailableError/503）上抛，不掩成 heuristic 200
    gov = _FakeGovernance(tmp_path)

    async def unavailable(**_k):
        raise RuntimeUnavailableError("model down")

    svc, content = _service(tmp_path, gov=gov, run_profile_json=unavailable)
    _confirm_plan(content)
    with pytest.raises(RuntimeUnavailableError):
        asyncio.run(svc.generate_and_apply_execution("imp-1"))
    record = content.get_execution("imp-1")
    assert record is not None and not record.applied_agent_version_id and not record.changes_applied
    assert gov.abandoned == gov.created
    assert gov.store.removed == gov.created


def test_git_infrastructure_failure_surfaces_after_safe_compensation(tmp_path, monkeypatch):
    gov = _FakeGovernance(tmp_path)

    async def should_not_run(**_kwargs):
        raise AssertionError("governor must not run when worktree reset fails")

    def fail_reset(*_args, **_kwargs):
        raise AgentGitError("worktree unavailable")

    monkeypatch.setattr(gov.store, "reset_worktree", fail_reset)
    svc, content = _service(tmp_path, gov=gov, run_profile_json=should_not_run)
    _confirm_plan(content)

    with pytest.raises(AgentGitError, match="worktree unavailable"):
        asyncio.run(svc.generate_and_apply_execution("imp-1"))

    record = content.get_execution("imp-1")
    assert record is not None and not record.changes_applied and not record.applied_agent_version_id
    assert gov.abandoned == gov.created


def test_execution_claim_rejects_parallel_request_and_fences_stale_owner(tmp_path):
    content = _content(tmp_path)
    _confirm_plan(content)
    claims = content.execution_claims
    first = claims.claim_execution(
        "imp-1",
        change_set_id="agc-11111111",
        base_commit_sha="base-sha",
        **_claim_source(content),
        claim_token="claim-one",
        now="2026-07-10T00:00:00+00:00",
        claim_expires_at="2026-07-10T00:01:00+00:00",
    )
    with pytest.raises(ConflictError):
        claims.claim_execution(
            "imp-1",
            change_set_id=first.change_set_id,
            base_commit_sha=first.base_commit_sha,
            **_claim_source(content),
            claim_token="parallel",
            now="2026-07-10T00:00:30+00:00",
            claim_expires_at="2026-07-10T00:01:30+00:00",
        )
    takeover = claims.claim_execution(
        "imp-1",
        change_set_id=first.change_set_id,
        base_commit_sha=first.base_commit_sha,
        **_claim_source(content),
        claim_token="claim-two",
        now="2026-07-10T00:02:00+00:00",
        claim_expires_at="2026-07-10T00:03:00+00:00",
    )
    with pytest.raises(ConflictError):
        claims.finish_without_application(
            "imp-1",
            claim_token=first.claim_token,
            claim_generation=first.claim_generation,
            summary="stale owner must not win",
        )
    claims.finish_without_application(
        "imp-1",
        claim_token=takeover.claim_token,
        claim_generation=takeover.claim_generation,
        summary="new owner finished without apply",
    )
    record = content.get_execution("imp-1")
    assert record is not None and record.summary == "new owner finished without apply"
    assert record.claim_generation == 2 and not record.claim_token


def test_archive_and_delete_reject_active_execution_claim(tmp_path):
    content = _content(tmp_path)
    _confirm_plan(content)
    claim = content.execution_claims.claim_execution(
        "imp-1",
        change_set_id="agc-22222222",
        base_commit_sha="base-sha",
        **_claim_source(content),
        claim_token="claim-archived",
        now="2026-07-10T00:00:00+00:00",
        claim_expires_at="2026-07-10T00:01:00+00:00",
    )
    improvements = ImprovementStore(make_session_factory(tmp_path / "runtime.sqlite3"))
    with pytest.raises(ConflictError, match="execution is applying"):
        improvements.archive_improvement("imp-1")
    with pytest.raises(ConflictError, match="execution is applying"):
        improvements.delete_improvement("imp-1")
    record = content.get_execution("imp-1")
    assert record is not None and not record.applied_agent_version_id and record.status == "applying"
    content.execution_claims.finish_without_application(
        "imp-1",
        claim_token=claim.claim_token,
        claim_generation=claim.claim_generation,
        summary="cancelled before archive",
        retain_change_set=False,
    )
    assert improvements.archive_improvement("imp-1").improvement_status == "archived"


def test_parallel_apply_creates_only_one_change_set(tmp_path):
    gov = _FakeGovernance(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def blocking_runner(**_kwargs):
        calls["n"] += 1
        entered.set()
        await release.wait()
        return {
            "status": "ready",
            "summary": "single winner",
            "operations": [{"operation": "append_text", "path": "CLAUDE.md", "append_text": "x", "expected_sha256": "s"}],
        }

    svc, content = _service(tmp_path, gov=gov, run_profile_json=blocking_runner)
    _confirm_plan(content)

    async def scenario():
        winner = asyncio.create_task(svc.generate_and_apply_execution("imp-1"))
        await entered.wait()
        with pytest.raises(ConflictError):
            await svc.generate_and_apply_execution("imp-1")
        release.set()
        return await winner

    record = asyncio.run(scenario())
    assert record.applied_agent_version_id and calls["n"] == 1
    assert len(gov.created) == 1 and gov.committed == gov.created


def test_plan_and_attribution_cannot_change_while_execution_is_applying(tmp_path):
    gov = _FakeGovernance(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_runner(**_kwargs):
        entered.set()
        await release.wait()
        return {
            "status": "ready",
            "summary": "source revision remained stable",
            "operations": [{"operation": "append_text", "path": "CLAUDE.md", "append_text": "x", "expected_sha256": "s"}],
        }

    svc, content = _service(tmp_path, gov=gov, run_profile_json=blocking_runner)
    _confirm_plan(content)
    content.upsert_attribution("imp-1", summary="confirmed source")
    content.set_attribution_status("imp-1", status="confirmed")
    expected_source = _claim_source(content)

    async def scenario():
        execution = asyncio.create_task(svc.generate_and_apply_execution("imp-1"))
        await entered.wait()
        with pytest.raises(ConflictError, match="optimization plan while execution is applying"):
            content.upsert_optimization_plan(
                "imp-1",
                summary="concurrent plan",
                changes=[{"target": "prompt", "change": "concurrent"}],
            )
        with pytest.raises(ConflictError, match="attribution while execution is applying"):
            content.upsert_attribution("imp-1", summary="concurrent attribution")
        release.set()
        return await execution

    record = asyncio.run(scenario())
    assert record.applied_agent_version_id
    assert record.source_optimization_plan_id == expected_source["source_optimization_plan_id"]
    assert record.source_optimization_plan_updated_at == expected_source["source_optimization_plan_updated_at"]
    assert record.source_attribution_id == expected_source["source_attribution_id"]
    assert record.source_attribution_updated_at == expected_source["source_attribution_updated_at"]


def test_source_revision_fences_finalize_and_same_change_set_takeover(tmp_path):
    content = _content(tmp_path)
    _confirm_plan(content)
    source = _claim_source(content)
    claim = content.execution_claims.claim_execution(
        "imp-1",
        change_set_id="agc-33333333",
        base_commit_sha="base-sha",
        **source,
        claim_token="claim-old-source",
        now="2026-07-10T00:00:00+00:00",
        claim_expires_at="2026-07-10T00:01:00+00:00",
    )
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    with factory.begin() as db:
        plan = db.get(OptimizationPlanModel, source["source_optimization_plan_id"])
        assert plan is not None
        plan.updated_at = "2026-07-10T00:01:30+00:00"

    with pytest.raises(ConflictError, match="revision changed"):
        content.execution_claims.finalize_execution_claim(
            "imp-1",
            claim_token=claim.claim_token,
            claim_generation=claim.claim_generation,
            summary="stale candidate",
            changes_applied=["edit: CLAUDE.md"],
            agent_version="ver-stale",
            risk_level="low",
            rollback_strategy="reset",
            rollback_instructions=["reset"],
            applied_diff={"changed_files": ["CLAUDE.md"]},
        )
    with pytest.raises(ConflictError, match="different source revision"):
        content.execution_claims.claim_execution(
            "imp-1",
            change_set_id=claim.change_set_id,
            base_commit_sha=claim.base_commit_sha,
            **_claim_source(content),
            claim_token="claim-new-source",
            now="2026-07-10T00:02:00+00:00",
            claim_expires_at="2026-07-10T00:03:00+00:00",
        )
    record = content.get_execution("imp-1")
    assert record is not None and record.status == "applying" and not record.applied_agent_version_id
    assert _stage(tmp_path) == "optimization"


def test_candidate_reconciles_after_execution_finalize_failure(tmp_path, monkeypatch):
    gov = _FakeGovernance(tmp_path)
    calls = {"n": 0}

    async def ready(**_kwargs):
        calls["n"] += 1
        return {
            "status": "ready",
            "summary": "candidate persisted before DB finalize",
            "operations": [{"operation": "append_text", "path": "CLAUDE.md", "append_text": "x", "expected_sha256": "s"}],
        }

    svc, content = _service(tmp_path, gov=gov, run_profile_json=ready)
    _confirm_plan(content)
    original_finalize = svc._claims.finalize_execution_claim
    failed = {"once": False}

    def fail_once(*args, **kwargs):
        if not failed["once"]:
            failed["once"] = True
            raise RuntimeError("execution finalize failed")
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(svc._claims, "finalize_execution_claim", fail_once)
    with pytest.raises(RuntimeError, match="finalize failed"):
        asyncio.run(svc.generate_and_apply_execution("imp-1"))
    interrupted = content.get_execution("imp-1")
    assert interrupted is not None and interrupted.status == "applying" and not interrupted.applied_agent_version_id
    assert _stage(tmp_path) == "optimization"

    recovered = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert recovered.applied_agent_version_id == "ver-cand-sha"
    assert calls["n"] == 1 and len(gov.created) == 1 and len(gov.committed) == 1
    assert _stage(tmp_path) == "execution"
    assert len(svc._improvements.links) == 1


def test_unmarked_worktree_commit_is_reconciled_without_second_candidate(tmp_path, monkeypatch):
    gov = _FakeGovernance(tmp_path)
    calls = {"n": 0}

    async def ready(**_kwargs):
        calls["n"] += 1
        return {
            "status": "ready",
            "summary": "commit survives mark failure",
            "operations": [{"operation": "append_text", "path": "CLAUDE.md", "append_text": "x", "expected_sha256": "s"}],
        }

    svc, content = _service(tmp_path, gov=gov, run_profile_json=ready)
    _confirm_plan(content)
    original_mark = gov.mark_candidate_committed
    failed = {"once": False}

    def fail_once(*args, **kwargs):
        if not failed["once"]:
            failed["once"] = True
            raise RuntimeError("candidate mark failed")
        return original_mark(*args, **kwargs)

    monkeypatch.setattr(gov, "mark_candidate_committed", fail_once)
    with pytest.raises(RuntimeError, match="mark failed"):
        asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert gov.store.head == "cand-sha" and not gov.committed

    recovered = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert recovered.applied_agent_version_id == "ver-cand-sha"
    assert calls["n"] == 1 and len(gov.created) == 1 and gov.committed == gov.created


def test_missing_link_is_reconciled_after_finalize(tmp_path):
    gov = _FakeGovernance(tmp_path)
    improvements = _FakeImprovements()
    improvements.fail_link_once = True
    calls = {"n": 0}

    async def ready(**_kwargs):
        calls["n"] += 1
        return {
            "status": "ready",
            "summary": "execution finalizes before link",
            "operations": [{"operation": "append_text", "path": "CLAUDE.md", "append_text": "x", "expected_sha256": "s"}],
        }

    content = _content(tmp_path)
    svc = ImprovementExecutionService(
        improvement_store=improvements,
        content_store=content,
        agent_governance=gov,
        execution_app=_FakeExecApp(),
        run_profile_json=ready,
    )
    _confirm_plan(content)
    with pytest.raises(RuntimeError, match="link insert failed"):
        asyncio.run(svc.generate_and_apply_execution("imp-1"))
    finalized = content.get_execution("imp-1")
    assert finalized is not None and finalized.applied_agent_version_id and _stage(tmp_path) == "execution"
    assert not improvements.links

    recovered = asyncio.run(svc.generate_and_apply_execution("imp-1"))
    assert recovered.execution_id == finalized.execution_id
    assert calls["n"] == 1 and len(gov.created) == 1 and len(improvements.links) == 1
