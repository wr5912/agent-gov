import type { AgentChangeSet, AgentTestRun } from "../types/runtime";

function latestExactRun(runs: AgentTestRun[], commitSha: string | null | undefined): AgentTestRun | null {
  if (!commitSha) return null;
  return runs.find((run) => run.commit_sha === commitSha) || null;
}

function projectedTestRun(changeSet: AgentChangeSet | null, testRunId: string): AgentTestRun | null {
  const run = changeSet?.latest_test_run;
  if (!run || typeof run !== "object" || Array.isArray(run)) return null;
  return run.test_run_id === testRunId ? run as unknown as AgentTestRun : null;
}

export function evidenceBoundTestRun(changeSet: AgentChangeSet | null, runs: AgentTestRun[]): AgentTestRun | null {
  const boundTestRunId = changeSet?.status === "approved"
    ? changeSet.approval_evidence?.test_run_id
    : changeSet?.status === "publishing"
      ? changeSet.publication_evidence?.test_run_id
      : undefined;
  if (boundTestRunId) {
    return runs.find((run) => run.test_run_id === boundTestRunId)
      || projectedTestRun(changeSet, boundTestRunId);
  }
  return latestExactRun(runs, changeSet?.candidate_commit_sha);
}

type PublicationRequestEvidence = {
  candidateCommitSha: string;
  diffDigest: string;
  testRunId?: string;
  suiteDigest?: string;
  tagName?: string;
  force: boolean;
};

export function publicationRequestEvidence(
  changeSet: AgentChangeSet | null,
  testRun: AgentTestRun | null,
): PublicationRequestEvidence | null {
  if (!changeSet) return null;
  if (changeSet.status === "publishing") {
    const evidence = changeSet.publication_evidence;
    if (!evidence) return null;
    if (!evidence.force && (!evidence.test_run_id || !evidence.suite_digest)) return null;
    return {
      candidateCommitSha: evidence.candidate_commit_sha,
      diffDigest: evidence.diff_digest,
      testRunId: evidence.test_run_id || undefined,
      suiteDigest: evidence.suite_digest || undefined,
      tagName: evidence.tag_name,
      force: evidence.force,
    };
  }
  if (changeSet.status === "approved" && changeSet.approval_evidence) {
    return {
      candidateCommitSha: changeSet.approval_evidence.candidate_commit_sha,
      diffDigest: changeSet.approval_evidence.diff_digest,
      testRunId: changeSet.approval_evidence.test_run_id,
      suiteDigest: changeSet.approval_evidence.suite_digest,
      force: false,
    };
  }
  const candidateCommitSha = changeSet.candidate_commit_sha;
  const diffDigest = changeSet.diff_summary?.digest;
  if (
    typeof candidateCommitSha !== "string"
    || typeof diffDigest !== "string"
    || typeof testRun?.test_run_id !== "string"
    || typeof testRun.suite_digest !== "string"
  ) return null;
  return {
    candidateCommitSha,
    diffDigest,
    testRunId: testRun.test_run_id,
    suiteDigest: testRun.suite_digest,
    force: false,
  };
}
