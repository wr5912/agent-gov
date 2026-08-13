"""改进执行测试共享 fake 与 fixture；文件名避免被 pytest 独立收集。"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict

from app.runtime.agent_git_store import AgentGitError
from app.runtime.improvement_db import ExecutionRecordModel, ImprovementItemModel
from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.improvement_content_store import ImprovementContentStore
from app.services.generated_agent_tests import build_generated_agent_test
from app.services.improvement_execution_service import ImprovementExecutionService

from feedback_store_test_utils import _seed_execution_record


class _ClaimSource(TypedDict):
    source_optimization_plan_id: str
    source_optimization_plan_updated_at: str
    source_attribution_id: str
    source_attribution_updated_at: str


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
    def __init__(self, worktrees_dir: Path) -> None:
        self.head = "base-sha"
        self.worktrees_dir = worktrees_dir
        self.removed: list[str] = []
        self.cleanup_modes: list[tuple[str, bool]] = []
        self.guard_depth = 0
        self.guard_entries = 0
        self.guard_entry_error: Exception | None = None
        self.guard_entry_hook: Callable[[], None] | None = None
        self._stable_lock = threading.RLock()

    def current_commit_sha(self):
        return "base-sha"

    @contextmanager
    def mutation_guard(self):
        with self._stable_lock:
            self.guard_entries += 1
            if self.guard_entry_hook is not None:
                self.guard_entry_hook()
            if self.guard_entry_error is not None:
                raise self.guard_entry_error
            self.guard_depth += 1
            try:
                yield
            finally:
                self.guard_depth -= 1

    def version_summary(self, sha, *, reason, note=None):
        return {"agent_version_id": f"ver-{sha}"}

    def commit_worktree(self, worktree, *, message):
        self.head = "cand-sha"
        return "cand-sha"

    def commit_squashed_worktree(self, worktree, *, base_ref, message):
        self.head = "cand-tests-sha"
        return "cand-tests-sha"

    def diff_versions(self, a, b):
        return {"changed_files": ["CLAUDE.md"] if a != b else [], "from": a, "to": b}

    def worktree_commit_sha(self, worktree):
        return self.head

    def _require_existing_worktree_authority(self, change_set_id, worktree, *, expected_head):
        expected_path = self.worktrees_dir / change_set_id
        marker = Path(worktree) / ".git"
        if (
            self.guard_depth <= 0
            or Path(worktree) != expected_path
            or not Path(worktree).is_dir()
            or Path(worktree).is_symlink()
            or not marker.exists()
            or marker.is_symlink()
            or self.head != expected_head
        ):
            raise AgentGitError("Candidate worktree authority rejected")

    def reset_worktree(self, worktree, *, base_ref):
        shutil.rmtree(Path(worktree) / "tests", ignore_errors=True)
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
        self.change_set_status = "draft"
        self.change_sets: dict[str, dict] = {}
        self.store = _FakeStore(worktree.parent)

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
        self.allowed_targets: list[set[str] | None] = []

    def apply_execution_operations(
        self,
        operations,
        *,
        workspace_dir=None,
        target_policy=None,
        content_guard=None,
        workspace_guard=None,
        allowed_targets=None,
    ):
        if self.raises:
            raise RuntimeError("apply blew up")
        self.applied.append(operations)
        self.allowed_targets.append(allowed_targets)


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


def _claim_source(content: ImprovementContentStore, improvement_id: str = "imp-1") -> _ClaimSource:
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
        item = db.get(ImprovementItemModel, "imp-1")
        assert item is not None
        return str(item.improvement_stage)


def _materialization_service(
    tmp_path: Path,
) -> tuple[Path, _FakeGovernance, ImprovementContentStore, ImprovementExecutionService]:
    worktree = tmp_path / "worktrees" / "agc-tests"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: fake\n", encoding="utf-8")
    gov = _FakeGovernance(worktree)
    gov.store.head = "cand-sha"
    gov.change_sets["agc-tests"] = {
        "change_set_id": "agc-tests",
        "agent_id": "soc-ops",
        "base_commit_sha": "base-sha",
        "candidate_commit_sha": "cand-sha",
        "execution_job_id": "exec-tests",
        "status": "candidate_committed",
        "worktree_path": str(worktree),
    }
    content = _content(tmp_path)
    execution = _seed_execution_record(
        content,
        "imp-1",
        summary="已生成待发布版本",
        changes_applied=["CLAUDE.md"],
        agent_version="cand-sha",
        change_set_id="agc-tests",
        applied_agent_version_id="cand-sha",
        applied_diff={"changed_files": ["CLAUDE.md"]},
    )
    with content._session_factory.begin() as db:
        row = db.get(ExecutionRecordModel, execution.execution_id)
        assert row is not None
        row.status = "confirmed"
        row.base_commit_sha = "base-sha"
    candidate = build_generated_agent_test(
        improvement_id="imp-1",
        index=1,
        test_code=(
            "def test_evidence_boundary(agent):\n"
            "    result = agent.run('分析告警')\n"
            "    assert not result.errors\n"
            "    normalized_text = ''.join(result.text.split())\n"
            "    assert '证据' in normalized_text\n"
            "    assert '核验' in normalized_text\n"
        ),
        test_intent="解释证据边界",
        assertion_rationale="回答必须指出证据与核验动作",
    )
    content.upsert_regression_test_design(
        "imp-1",
        summary="覆盖误报反馈",
        tests=[candidate.to_payload()],
    )
    service = ImprovementExecutionService(
        improvement_store=_FakeImprovements(),
        content_store=content,
        agent_governance=gov,
        execution_app=_FakeExecApp(),
        run_profile_json=None,
    )
    return worktree, gov, content, service
