from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.runtime.json_types import JsonObject
from app.services.agent_publication import PublicationIntent


@dataclass(frozen=True, slots=True)
class PublicationTestEvidence:
    test_run_id: str
    receipt_digest: str
    suite_digest: str
    source_digest: str

    @classmethod
    def from_release_eligible_run(cls, run: object) -> PublicationTestEvidence:
        receipt = run.get("receipt") if isinstance(run, dict) else None
        values = (
            run.get("test_run_id") if isinstance(run, dict) else None,
            receipt.get("receipt_digest") if isinstance(receipt, dict) else None,
            run.get("suite_digest") if isinstance(run, dict) else None,
            run.get("source_digest") if isinstance(run, dict) else None,
        )
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError("待发布版本缺少可固化的平台测试回执门证。")
        return cls(*(str(value) for value in values))

    @classmethod
    def from_change_set(cls, change_set: JsonObject) -> PublicationTestEvidence:
        return cls.from_release_eligible_run(change_set.get("latest_test_run"))


def publication_intent_evidence_error(intent: PublicationIntent, current_run: JsonObject | None) -> str | None:
    if intent.force_publication_blocker:
        return "旧策略 force intent 带有被绕过的阻断项"
    expected = (
        intent.test_run_id,
        intent.test_receipt_digest,
        intent.test_suite_digest,
        intent.test_source_digest,
    )
    if not all(expected):
        return "intent 未固化 test run/receipt/suite/source"
    if current_run is None:
        return "当前候选提交已无 release-eligible 测试运行"
    try:
        current = PublicationTestEvidence.from_release_eligible_run(current_run)
    except ValueError:
        return "当前 release-eligible 测试运行缺少完整门证"
    actual = (current.test_run_id, current.receipt_digest, current.suite_digest, current.source_digest)
    if actual != expected:
        return "当前 release-eligible 测试门证与 intent 不一致"
    return None


def matching_passed_test_run(
    resolver: Callable[[str, str], JsonObject | None] | None,
    *,
    agent_id: str,
    commit_sha: str,
) -> JsonObject | None:
    if not commit_sha or resolver is None:
        return None
    candidate = resolver(agent_id, commit_sha)
    if not isinstance(candidate, dict):
        return None
    if (
        str(candidate.get("agent_id") or "") != agent_id
        or str(candidate.get("commit_sha") or "") != commit_sha
        or str(candidate.get("status") or "") != "passed"
    ):
        return None
    return candidate


def publication_blocker(change_set: JsonObject) -> str | None:
    blocker = change_set.get("publication_blocker")
    return str(blocker) if blocker else None
