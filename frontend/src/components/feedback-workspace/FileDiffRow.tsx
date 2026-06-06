import { useState } from "react";
import { ChevronRight } from "lucide-react";
import { diffAgentChangeSetFile } from "../../api/runtime";
import type { AgentGitFileDiff, RuntimeClientConfig } from "../../types/runtime";
import { Pill } from "./common";
import { fileStatusText, fileStatusTone } from "./selectors";

export function FileDiffRow({
  changeSetId,
  clientConfig,
  path,
  statusText,
}: {
  changeSetId: string;
  clientConfig: RuntimeClientConfig;
  path: string;
  statusText: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [diff, setDiff] = useState<AgentGitFileDiff | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggle() {
    const next = !expanded;
    setExpanded(next);
    if (!next || diff || loading) return;
    setLoading(true);
    setError(null);
    try {
      setDiff(await diffAgentChangeSetFile(clientConfig, changeSetId, path));
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载文件对比失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="fw-file-diff-row">
      <button className="fw-file-diff-toggle" type="button" onClick={toggle}>
        <ChevronRight size={15} className={expanded ? "is-open" : ""} />
        <span>{path}</span>
        <Pill tone={fileStatusTone(statusText)}>{fileStatusText(statusText)}</Pill>
      </button>
      {expanded ? (
        <div className="fw-file-diff-body">
          {loading ? <p className="fw-muted">加载对比中...</p> : null}
          {error ? <p className="fw-warning-text">{error}</p> : null}
          {diff ? (
            diff.unified_diff ? <pre>{diff.unified_diff}</pre> : <p className="fw-muted">{diff.reason || fileStatusText(diff.status)}</p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
