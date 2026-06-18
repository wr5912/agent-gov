import { useState } from "react";
import type { AgentChangeSet, AgentRelease } from "../types/runtime";
import "../improvement-workbench.css";

// 发布工作台（v2.7 §12）：回答「能不能发 / 为什么 / 发了包含什么」，并呈现三门门禁与动作。
// 按当前业务 Agent 防御式过滤（响应含 agent_id 时按 Agent scoping，否则不过滤避免误隐藏）。
type WithAgent = { agent_id?: string };

const CHANGESET_TERMINAL = new Set(["published", "abandoned"]);
const CHANGESET_BLOCKED = new Set(["regression_failed", "rejected", "failed"]);
const CHANGESET_READY = new Set(["regression_passed", "approved", "candidate_committed"]);
const EXECUTED = new Set(["candidate_committed", "regression_passed", "regression_failed", "approved", "published"]);
const REGRESSION_PASS = new Set(["regression_passed", "approved", "published"]);

type GateState = "pass" | "fail" | "pending";

function scopedBy<T extends WithAgent>(items: T[], agentId: string): T[] {
  if (!agentId) return items;
  return items.filter((item) => item.agent_id == null || item.agent_id === agentId);
}

function deriveGates(changeSets: AgentChangeSet[]): { id: string; label: string; state: GateState }[] {
  const has = changeSets.length > 0;
  const executed = changeSets.some((cs) => EXECUTED.has(String(cs.status)));
  const regPass = changeSets.some((cs) => REGRESSION_PASS.has(String(cs.status)));
  const regFail = changeSets.some((cs) => CHANGESET_BLOCKED.has(String(cs.status)));
  return [
    { id: "attribution", label: "归因已确认", state: has ? "pass" : "pending" },
    { id: "optimization", label: "优化已执行", state: executed ? "pass" : "pending" },
    { id: "regression", label: "回归测试", state: regFail ? "fail" : regPass ? "pass" : "pending" },
  ];
}

const GATE_TEXT: Record<GateState, string> = { pass: "通过", fail: "未通过", pending: "未完成" };

function overallGate(gates: { state: GateState }[], total: number): { label: string; tone: "success" | "danger" | "primary" | "muted"; reason: string } {
  if (total === 0) return { label: "无待发布变更", tone: "muted", reason: "当前范围还没有候选变更。先在「改进」里把事项推进到执行/回归。" };
  if (gates.some((g) => g.state === "fail")) return { label: "不可发布", tone: "danger", reason: "存在未通过的门禁，需先修复或重跑回归。" };
  if (gates.every((g) => g.state === "pass")) return { label: "可发布", tone: "success", reason: "三门门禁均通过，可发布。" };
  return { label: "进行中", tone: "primary", reason: "门禁尚未全部通过。" };
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
  const [showChanges, setShowChanges] = useState(false);
  const scopedChangeSets = scopedBy(changeSets as (AgentChangeSet & WithAgent)[], scopeAgentId);
  const scopedReleases = scopedBy(releases as (AgentRelease & WithAgent)[], scopeAgentId);
  const pendingChangeSets = scopedChangeSets.filter((cs) => !CHANGESET_TERMINAL.has(String(cs.status)));
  const gates = deriveGates(scopedChangeSets);
  const gate = overallGate(gates, scopedChangeSets.length);
  const regressionPending = gates.find((g) => g.id === "regression")?.state !== "pass" && pendingChangeSets.length > 0;
  const canForce = pendingChangeSets.length > 0;

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
            <span className={`iw-stage-pill ${gate.tone === "success" ? "is-done" : ""}`} data-testid="release-gate" data-state={gate.tone}>{gate.label}</span>
            <div className="iw-next-step" style={{ marginTop: 8 }}>{gate.reason}</div>
          </div>

          <div className="iw-detail-section">
            <h4>发布门禁</h4>
            <div className="iw-source-refs">
              {gates.map((g) => (
                <span
                  key={g.id}
                  className="iw-stage-pill"
                  data-testid={`release-gate-${g.id}`}
                  data-state={g.state}
                  style={{
                    background: g.state === "pass" ? "var(--color-success-soft)" : g.state === "fail" ? "#fef2f2" : "var(--bg-surface-muted)",
                    color: g.state === "pass" ? "var(--color-success)" : g.state === "fail" ? "#dc2626" : "var(--text-secondary)",
                    borderColor: g.state === "pass" ? "#bbf7d0" : g.state === "fail" ? "#fecaca" : "var(--border-subtle)",
                  }}
                >
                  {g.label}：{GATE_TEXT[g.state]}
                </span>
              ))}
            </div>
            <div className="iw-action-row">
              <button
                className="iw-secondary-button"
                type="button"
                data-testid="release-action-run-regression"
                disabled={!regressionPending}
                title={regressionPending ? "在变更详情运行回归（旧 workspace 能力 P3 迁移后直接接入）" : "无待回归变更"}
                onClick={() => onRefresh()}
              >
                去运行回归
              </button>
              <button
                className="iw-secondary-button"
                type="button"
                data-testid="release-action-view-changes"
                onClick={() => setShowChanges((v) => !v)}
              >
                查看变更
              </button>
              <button
                className="iw-secondary-button"
                type="button"
                data-testid="release-action-force"
                disabled={!canForce}
                title={canForce ? "强制发布需在变更详情二次确认（危险操作）" : "无可发布变更"}
                style={{ color: canForce ? "#dc2626" : undefined }}
                onClick={() => setShowChanges(true)}
              >
                强制发布…
              </button>
            </div>
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
                  {showChanges && cs.diff_summary ? <span className="iw-list-item-meta">{String(cs.diff_summary)}</span> : null}
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
