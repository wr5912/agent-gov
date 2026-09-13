import { describe, expect, it } from "vitest";

import type { AgentChangeSet, AgentGitDiff, AgentGitFileDiff, AgentTestRun } from "../types/runtime";
import {
  candidateDiffReviewError,
  deriveReleaseActions,
  scopedReleaseChangeSets,
} from "./ReleaseWorkbench";
import { evidenceBoundTestRun, publicationRequestEvidence } from "./releasePublicationEvidence";

function candidate(overrides: Partial<AgentChangeSet> = {}): AgentChangeSet {
  return {
    change_set_id: "agc-general",
    agent_id: "agent-alpha",
    created_at: "2026-09-12T00:00:00Z",
    updated_at: "2026-09-12T00:00:00Z",
    status: "pending_approval",
    base_commit_sha: "a".repeat(40),
    candidate_commit_sha: "b".repeat(40),
    branch_name: "candidate/agc-general",
    worktree_path: "/candidate/agc-general",
    publication_blocker: null,
    ...overrides,
  } as AgentChangeSet;
}

function passedRun(): AgentTestRun {
  return {
    test_run_id: "agtr-pass",
    agent_id: "agent-alpha",
    change_set_id: "agc-general",
    commit_sha: "b".repeat(40),
    status: "passed",
    suite_digest: "c".repeat(64),
  } as AgentTestRun;
}

function diff(): AgentGitDiff {
  return {
    from_version_id: "a".repeat(40),
    to_version_id: "b".repeat(40),
    added: [],
    modified: [{ path: "AGENT.md" }],
    deleted: [],
    unchanged_count: 0,
  } as AgentGitDiff;
}

function fileDiff(overrides: Partial<AgentGitFileDiff> = {}): AgentGitFileDiff {
  return {
    from_version_id: "a".repeat(40),
    to_version_id: "b".repeat(40),
    path: "AGENT.md",
    archive_path: "AGENT.md",
    status: "modified",
    unified_diff: "--- a/AGENT.md\n+++ b/AGENT.md\n@@ -1 +1 @@\n-old\n+new\n",
    is_text: true,
    truncated: false,
    ...overrides,
  } as AgentGitFileDiff;
}

describe("候选治理发布状态", () => {
  it("通用模式接收该 Agent 全部候选，Improvement 模式仍精确隔离来源", () => {
    const general = candidate();
    const improvement = candidate({
      change_set_id: "agc-improvement",
      source_improvement_id: "imp-1",
    });
    const unrelated = candidate({ change_set_id: "agc-other", agent_id: "agent-beta" });

    expect(scopedReleaseChangeSets([general, improvement, unrelated], "agent-alpha").map((item) => item.change_set_id))
      .toEqual(["agc-general", "agc-improvement"]);
    expect(scopedReleaseChangeSets([general, improvement, unrelated], "agent-alpha", "imp-1").map((item) => item.change_set_id))
      .toEqual(["agc-improvement"]);
  });

  it("pending_approval 在精确测试通过后只允许审批，审批完成后才允许发布", () => {
    const hiddenDiff = deriveReleaseActions(candidate(), passedRun());
    expect(hiddenDiff.canApprove).toBe(false);

    const pending = deriveReleaseActions(candidate(), passedRun(), true);
    expect(pending.canApprove).toBe(true);
    expect(pending.canPublish).toBe(false);
    expect(pending.canReject).toBe(true);

    const approved = deriveReleaseActions(candidate({ status: "approved" }), passedRun());
    expect(approved.canApprove).toBe(false);
    expect(approved.canPublish).toBe(true);
    expect(approved.canReject).toBe(true);
  });

  it("测试记录未通过时不能审批或发布候选", () => {
    const failed = deriveReleaseActions(candidate(), {
      ...passedRun(),
      status: "failed",
    } as AgentTestRun);

    expect(failed.canApprove).toBe(false);
    expect(failed.canPublish).toBe(false);
  });

  it("别的 Agent、候选或 commit 的 passed 测试不得解锁审批", () => {
    for (const patch of [
      { agent_id: "agent-beta" },
      { change_set_id: "agc-other" },
      { commit_sha: "d".repeat(40) },
      { suite_digest: null },
    ]) {
      const stale = deriveReleaseActions(candidate(), { ...passedRun(), ...patch } as AgentTestRun, true);
      expect(stale.canApprove).toBe(false);
      expect(stale.canPublish).toBe(false);
    }
  });

  it("存在发布阻断项时，即使候选已审批且精确测试通过也不能发布", () => {
    const blocked = deriveReleaseActions(candidate({
      status: "approved",
      publication_blocker: "sensitive review is incomplete",
    }), passedRun());

    expect(blocked.canApprove).toBe(false);
    expect(blocked.canPublish).toBe(false);
  });

  it("publishing 恢复始终使用不可变 intent 证据，不受后来测试或列表截断影响", () => {
    const boundRun = passedRun();
    const publishing = candidate({
      status: "publishing",
      latest_test_run: boundRun as unknown as AgentChangeSet["latest_test_run"],
      publication_evidence: {
        candidate_commit_sha: "b".repeat(40),
        diff_digest: "d".repeat(64),
        test_run_id: boundRun.test_run_id,
        suite_digest: boundRun.suite_digest,
        tag_name: "agent-release-agc-general",
        force: false,
      },
    });
    const newerRuns = Array.from({ length: 21 }, (_, index) => ({
      ...boundRun,
      test_run_id: `agtr-newer-${index}`,
      suite_digest: "e".repeat(64),
    } as AgentTestRun));

    expect(evidenceBoundTestRun(publishing, newerRuns)?.test_run_id).toBe(boundRun.test_run_id);
    expect(publicationRequestEvidence(publishing, newerRuns[0])).toEqual({
      candidateCommitSha: "b".repeat(40),
      diffDigest: "d".repeat(64),
      testRunId: boundRun.test_run_id,
      suiteDigest: boundRun.suite_digest,
      tagName: "agent-release-agc-general",
      force: false,
    });
  });

  it("approved 发布使用审批时冻结的测试与 diff，而不是后来运行记录", () => {
    const approved = candidate({
      status: "approved",
      approval_evidence: {
        candidate_commit_sha: "b".repeat(40),
        diff_digest: "d".repeat(64),
        test_run_id: "agtr-approved",
        suite_digest: "c".repeat(64),
        review_digest: "f".repeat(64),
        reviewed_file_count: 1,
      },
    });
    const newer = { ...passedRun(), test_run_id: "agtr-newer", suite_digest: "e".repeat(64) } as AgentTestRun;

    expect(publicationRequestEvidence(approved, newer)).toMatchObject({
      testRunId: "agtr-approved",
      suiteDigest: "c".repeat(64),
      diffDigest: "d".repeat(64),
    });
  });

  it("只有逐文件 Diff 身份一致、未截断且含真实变更行时才通过审阅", () => {
    expect(candidateDiffReviewError(candidate(), diff(), [fileDiff()])).toBeUndefined();
    expect(candidateDiffReviewError(candidate(), { ...diff(), to_version_id: "d".repeat(40) }, [fileDiff()]))
      .toContain("版本身份");
    expect(candidateDiffReviewError(candidate(), diff(), [fileDiff({ to_version_id: "d".repeat(40) })]))
      .toContain("身份不完整");
    expect(candidateDiffReviewError(candidate(), diff(), [fileDiff({ status: "added" })]))
      .toContain("无法完整审阅");
    expect(candidateDiffReviewError(candidate(), diff(), [fileDiff({ truncated: true })]))
      .toContain("无法完整审阅");
    const withoutTruncated = fileDiff() as Partial<AgentGitFileDiff>;
    delete withoutTruncated.truncated;
    expect(candidateDiffReviewError(candidate(), diff(), [withoutTruncated as AgentGitFileDiff]))
      .toContain("无法完整审阅");
    expect(candidateDiffReviewError(candidate(), diff(), [fileDiff({ is_text: false })]))
      .toContain("无法完整审阅");
    expect(candidateDiffReviewError(candidate(), diff(), [fileDiff({ unified_diff: "--- a/AGENT.md\n+++ b/AGENT.md\n" })]))
      .toContain("缺少实际变更行");
    expect(candidateDiffReviewError(candidate(), diff(), []))
      .toContain("尚未完整加载");
    const duplicate = { ...diff(), added: [{ path: "AGENT.md" }] } as AgentGitDiff;
    expect(candidateDiffReviewError(candidate(), duplicate, [fileDiff(), fileDiff({ status: "added" })]))
      .toContain("重复文件路径");
    const twoFiles = { ...diff(), added: [{ path: "agent.yaml" }] } as AgentGitDiff;
    expect(candidateDiffReviewError(candidate(), twoFiles, [fileDiff(), fileDiff()]))
      .toContain("重复文件路径");
  });
});
