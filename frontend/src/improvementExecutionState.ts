import type { ExecutionRecord } from "./api/improvements";

export function hasAppliedExecution(execution: ExecutionRecord | null | undefined): boolean {
  return Boolean(execution?.applied_agent_version_id || execution?.change_set_id);
}

export function hasFileDiffBinding(execution: ExecutionRecord | null | undefined): boolean {
  return Boolean(execution?.change_set_id && execution.applied_diff && Object.keys(execution.applied_diff).length);
}

export function hasUnboundExecutionRecord(execution: ExecutionRecord | null | undefined): boolean {
  return Boolean(execution && !hasAppliedExecution(execution));
}
