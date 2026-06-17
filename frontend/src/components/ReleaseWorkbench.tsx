import type { AgentChangeSet, AgentRelease } from "../types/runtime";
import "../improvement-workbench.css";

// 发布工作台（v2.7 §12）：回答「能不能发 / 为什么 / 发了包含什么」。
// 消费真实 /api/agent-change-sets + /api/agent-releases；按当前业务 Agent 防御式过滤
//（响应暂未暴露 agent_id 时不过滤，避免误隐藏；暴露后即自动按 Agent scoping）。
type WithAgent = { agent_id?: string };

const CHANGESET_TERMINAL = new Set(["published", "abandoned"]);
const CHANGESET_BLOCKED = new Set(["regression_failed", "rejected", "failed"]);
const CHANGESET_READY = new Set(["regression_passed", "approved", "candidate_committed"]);

function scopedBy<T extends WithAgent>(items: T[], agentId: string): T[] {
  if (!agentId) return items;
  return items.filter((item) => item.agent_id == null || item.agent_id === agentId);
}

function gateState(changeSets: AgentChangeSet[]): { label: string; tone: "primary" | "success" | "danger" | "muted"; reason: string } {
  const pending = changeSets.filter((cs) => !CHANGESET_TERMINAL.has(String(cs.status)));
  if (changeSets.length === 0) return { label: "无待发布变更", tone: "muted", reason: "当前范围还没有产生候选变更。先在「改进」里推进事项到执行/回归。" };
  if (pending.some((cs) => CHANGESET_BLOCKED.has(String(cs.status)))) {
    return { label: "不可发布", tone: "danger", reason: "存在回归失败 / 被拒的变更，需先修复或重跑回归。" };
  }
  if (pending.some((cs) => CHANGESET_READY.has(String(cs.status)))) {
    return { label: "可发布候选", tone: "success", reason: "有通过回归 / 已批准的候选变更，可发布。" };
  }
  if (pending.length > 0) return { label: "进行中", tone: "primary", reason: "候选变更仍在执行 / 回归中。" };
  return { label: "已全部发布", tone: "success", reason: "范围内变更均已发布。" };
}

export function ReleaseWorkbench({
  scopeAgentId,
  releases,
  changeSets,
  onRefresh,
}: {
  scopeAgentId: string;
  releases: AgentRelease[];
  changeSets: AgentChangeSet[];
  onRefresh: () => void;
}) {
  const scopedChangeSets = scopedBy(changeSets as (AgentChangeSet & WithAgent)[], scopeAgentId);
  const scopedReleases = scopedBy(releases as (AgentRelease & WithAgent)[], scopeAgentId);
  const pendingChangeSets = scopedChangeSets.filter((cs) => !CHANGESET_TERMINAL.has(String(cs.status)));
  const gate = gateState(scopedChangeSets);

  return (
    <div className="improvement-workbench" data-testid="release-workbench" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
      <section className="iw-detail-panel">
        <div className="iw-panel-head">
          <h3>发布{scopeAgentId ? ` · ${scopeAgentId}` : "（全部业务 Agent）"}</h3>
          <button className="iw-secondary-button" type="button" onClick={onRefresh}>刷新</button>
        </div>
        <div className="iw-panel-body">
          <div className="iw-detail-section">
            <h4>能不能发</h4>
            <span
              className={`iw-stage-pill ${gate.tone === "success" ? "is-done" : ""}`}
              data-testid="release-gate"
              data-state={gate.tone}
            >
              {gate.label}
            </span>
            <div className="iw-next-step" style={{ marginTop: 8 }}>{gate.reason}</div>
          </div>

          <div className="iw-detail-section">
            <h4>待发布变更（{pendingChangeSets.length}）</h4>
            {pendingChangeSets.length === 0 ? (
              <div className="iw-empty">当前范围没有待发布的候选变更。</div>
            ) : (
              pendingChangeSets.map((cs) => (
                <div className="iw-list-item" data-testid="release-changeset-item" data-status={cs.status} key={cs.change_set_id}>
                  <span className="iw-list-item-title">{cs.title || cs.change_set_id}</span>
                  <span className="iw-list-item-meta">{cs.status} · {cs.change_set_id}{cs.publication_blocker ? ` · 阻塞：${cs.publication_blocker}` : ""}</span>
                </div>
              ))
            )}
          </div>

          <div className="iw-detail-section">
            <h4>已发布（{scopedReleases.length}）</h4>
            {scopedReleases.length === 0 ? (
              <div className="iw-empty">尚无发布记录。</div>
            ) : (
              scopedReleases.map((rel) => (
                <div className="iw-list-item" data-testid="release-item" data-status={rel.status} key={rel.release_id}>
                  <span className="iw-list-item-title">{rel.tag_name || rel.release_id}</span>
                  <span className="iw-list-item-meta">{rel.status} · {rel.commit_sha?.slice(0, 12) || "-"} · {rel.created_at}</span>
                </div>
              ))
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
