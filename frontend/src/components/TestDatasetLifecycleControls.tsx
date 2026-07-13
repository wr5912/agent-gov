import { useEffect, useState } from "react";
import type { TestDataset, TestDatasetRevision } from "../api/assets";

const ALLOWED_TARGETS: Record<TestDataset["lifecycle_state"], TestDataset["lifecycle_state"][]> = {
  draft: ["active", "archived"],
  active: ["deprecated", "archived"],
  evaluating: ["active", "deprecated", "archived"],
  deprecated: ["active", "archived"],
  archived: [],
};

export function TestDatasetLifecycleControls({
  dataset,
  revisions,
  revisionError,
  busy,
  readOnly,
  onTransition,
}: {
  dataset: TestDataset;
  revisions: TestDatasetRevision[];
  revisionError?: string;
  busy: boolean;
  readOnly: boolean;
  onTransition: (targetState: TestDataset["lifecycle_state"], reason: string) => void;
}) {
  const targets = ALLOWED_TARGETS[dataset.lifecycle_state];
  const [targetState, setTargetState] = useState<TestDataset["lifecycle_state"] | "">(targets[0] || "");
  const [reason, setReason] = useState("");

  useEffect(() => {
    setTargetState(ALLOWED_TARGETS[dataset.lifecycle_state][0] || "");
    setReason("");
  }, [dataset.dataset_id, dataset.lifecycle_state, dataset.revision]);

  return (
    <div className="iw-test-dataset-lifecycle" data-testid="test-dataset-lifecycle-management">
      <span className="iw-list-item-meta">修订记录 {revisions.length}</span>
      {revisionError ? <div className="iw-error" data-testid="test-dataset-revision-error">{revisionError}</div> : null}
      {!readOnly && targets.length ? (
        <div className="iw-action-row">
          <select
            className="iw-select select-inline"
            data-testid="test-dataset-lifecycle-target"
            value={targetState}
            disabled={busy}
            onChange={(event) => setTargetState(event.target.value as TestDataset["lifecycle_state"])}
          >
            {targets.map((target) => <option key={target} value={target}>{target}</option>)}
          </select>
          <input
            className="iw-input"
            data-testid="test-dataset-lifecycle-reason"
            aria-label="状态变更原因"
            value={reason}
            disabled={busy}
            onChange={(event) => setReason(event.target.value)}
          />
          <button
            className="iw-secondary-button"
            type="button"
            data-testid="test-dataset-lifecycle-submit"
            disabled={busy || !targetState || !reason.trim()}
            onClick={() => targetState && onTransition(targetState, reason.trim())}
          >
            应用状态
          </button>
        </div>
      ) : null}
    </div>
  );
}
