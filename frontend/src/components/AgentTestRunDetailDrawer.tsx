import type { AgentTestRun } from "../types/runtime";
import { DrawerShell } from "./DrawerShell";

export function AgentTestRunDetailDrawer({
  runDetail,
  statusLabels,
  onClose,
}: {
  runDetail: AgentTestRun | undefined;
  statusLabels: Readonly<Record<string, string>>;
  onClose: () => void;
}) {
  if (!runDetail) return null;

  return (
    <DrawerShell
      title={`测试运行 · ${statusLabels[runDetail.status]}`}
      description={`${runDetail.agent_id} · ${runDetail.commit_sha}`}
      size="wide"
      testId="test-run-detail-drawer"
      bodyClassName="feedback-drawer-body"
      onClose={onClose}
    >
      <div className="test-run-detail-meta">
        <span>来源：{runDetail.source}</span>
        <span>创建：{formatDateTime(runDetail.created_at)}</span>
        <span>退出码：{runDetail.exit_code ?? "—"}</span>
      </div>
      {(runDetail.items?.length ?? 0) > 0 ? (
        <div className="test-run-items">
          {(runDetail.items ?? []).map((item) => (
            <div key={`${item.nodeid}-${item.phase}`}>
              <strong>{item.outcome}</strong><code>{item.nodeid}</code><span>{item.detail}</span>
            </div>
          ))}
        </div>
      ) : null}
      {(runDetail.invocations?.length ?? 0) > 0 ? (
        <>
          <h4>Agent 调用</h4>
          <pre className="test-run-output">{JSON.stringify(runDetail.invocations, null, 2)}</pre>
        </>
      ) : null}
      {Object.keys(runDetail.error ?? {}).length ? (
        <pre className="test-run-output is-error">{JSON.stringify(runDetail.error, null, 2)}</pre>
      ) : null}
      <h4>stdout</h4>
      <pre className="test-run-output">{runDetail.stdout || "（空）"}</pre>
      <h4>stderr</h4>
      <pre className="test-run-output">{runDetail.stderr || "（空）"}</pre>
    </DrawerShell>
  );
}

function formatDateTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}
