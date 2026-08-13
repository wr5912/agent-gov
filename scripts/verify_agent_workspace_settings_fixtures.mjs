export const timestamp = "2026-07-16T00:00:00Z";
export const previousCommit = "a".repeat(40);
export const rollbackCommit = "b".repeat(40);
export const importedCommit = "c".repeat(40);
export const restoredCommit = "d".repeat(40);
export const packageDigest = "e".repeat(64);
export const treeDigest = "f".repeat(64);
export const pendingDeletionOperationId = "adop-00000000-0000-4000-8000-000000000099";

export const workspaceAgent = {
  agent_id: "workspace-agent",
  name: "Workspace Agent",
  category: "",
  workspace_dir: "/runtime/workspace-agent",
  created_at: timestamp,
  status: "active",
  builtin: false,
  default: false,
  protected: false,
  requires_web_hitl: false,
};

export const secondWorkspaceAgent = {
  ...workspaceAgent,
  agent_id: "workspace-agent-2",
  name: "Workspace Agent 2",
  workspace_dir: "/runtime/workspace-agent-2",
  protected: true,
};

export const importedWorkspaceAgent = {
  ...workspaceAgent,
  agent_id: "imported-new",
  name: "Imported Package Agent",
  workspace_dir: "/runtime/imported-new",
};

export const exportFilename = `workspace-agent-workspace-${previousCommit.slice(0, 12)}.tar.gz`;

export const pendingDeletionReceipt = {
  operation_id: pendingDeletionOperationId,
  state: "cleanup_pending",
  deleted: {
    ...workspaceAgent,
    agent_id: "cleanup-pending-agent",
    name: "Cleanup Pending Agent",
  },
  impact: {
    runs: 1,
    feedback_signals: 2,
    improvements: 3,
    test_runs: 4,
    change_sets: 5,
    releases: 6,
  },
  workspace_removed: false,
  cleanup_complete: false,
  last_error_code: "AGENT_DELETION_FILESYSTEM_FENCE",
  attempt_count: 2,
  updated_at: timestamp,
};
