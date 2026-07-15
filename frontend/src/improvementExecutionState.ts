import type { ExecutionRecord } from "./api/improvements";

export function hasAppliedExecution(execution: ExecutionRecord | null | undefined): boolean {
  const boundCandidate = Boolean(
    execution?.change_set_id
    && execution.applied_agent_version_id
    && execution.applied_diff
    && Object.keys(execution.applied_diff).length,
  );
  const manualEvidence = Boolean(execution?.changes_applied?.length && execution.agent_version?.trim());
  return boundCandidate || manualEvidence;
}

export function hasFileDiffBinding(execution: ExecutionRecord | null | undefined): boolean {
  return Boolean(execution?.change_set_id && execution.applied_diff && Object.keys(execution.applied_diff).length);
}

export function hasUnboundExecutionRecord(execution: ExecutionRecord | null | undefined): boolean {
  return Boolean(execution && !hasAppliedExecution(execution));
}
