import { useEffect, useState } from "react";
import { createImprovement, upsertNormalizedFeedback, addImprovementFeedback, type ImprovementItem } from "../api/improvements";
import type { RuntimeClientConfig } from "../types/runtime";
import { DrawerShell } from "./DrawerShell";

// 四阶段改进治理 §4 创建反馈 Drawer（两阶段）：自然语言反馈 → 整理为系统理解 → 确认保存 → 生成改进事项。
// 注：P1 阶段「系统理解」为客户端初步整理（占位），真正的 NormalizedFeedback 后端实体在 P3 接入；
// 这里明确标注「初步」，不冒充已有后端归一化能力。

export interface FeedbackContext {
  runId?: string;
  sessionId?: string;
  agentVersionId?: string;
  scenario?: string;
  taskId?: string;
  alertId?: string;
  caseId?: string;
  agentId: string;
  agentName: string;
}

type Phase = "input" | "understanding" | "saved";

function firstSentence(text: string): string {
  const t = text.trim().replace(/\s+/g, " ");
  const m = t.match(/^.{0,40}?[。.!！?？]/);
  return (m ? m[0] : t.slice(0, 40)).replace(/[。.!！?？]$/, "") || "改进事项";
}

export function FeedbackDrawer({
  open,
  context,
  clientConfig,
  onClose,
  onCreated,
}: {
  open: boolean;
  context: FeedbackContext | null;
  clientConfig: RuntimeClientConfig;
  onClose: () => void;
  onCreated: (item: ImprovementItem) => void;
}) {
  const [phase, setPhase] = useState<Phase>("input");
  const [wrong, setWrong] = useState("");
  const [expected, setExpected] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [created, setCreated] = useState<ImprovementItem | null>(null);

  useEffect(() => {
    if (open) { setPhase("input"); setWrong(""); setExpected(""); setError(undefined); setCreated(null); }
  }, [open]);

  if (!open || !context) return null;

  const problem = firstSentence(wrong);
  const summary = [
    `问题：${problem}`,
    `用户反馈：${wrong.trim()}`,
    expected.trim() ? `期望处理：${expected.trim()}` : "",
    `归属业务 Agent：${context.agentName}（${context.agentId}）`,
    context.agentVersionId ? `Agent 版本：${context.agentVersionId}` : "",
    context.scenario ? `业务场景：${context.scenario}` : "",
    context.taskId ? `任务：${context.taskId}` : "",
    context.runId ? `来源 Run：${context.runId}` : "",
    context.sessionId ? `来源 Session：${context.sessionId}` : "",
    context.alertId ? `关联 Alert：${context.alertId}` : "",
    context.caseId ? `关联 Case：${context.caseId}` : "",
  ].filter(Boolean).join("\n");

  const organize = () => { if (!wrong.trim()) return; setPhase("understanding"); };

  const save = () => {
    setBusy(true); setError(undefined);
    void createImprovement(clientConfig, {
      agent_id: context.agentId,
      title: problem,
      summary,
      source_feedback_refs: context.runId ? [context.runId] : [],
      auto_merge: false,
    }).then(async (item) => {
      setCreated(item);
      setPhase("saved");
      // P3：把「系统理解（初步）」持久化为 NormalizedFeedback 子资源（不再只存客户端）。
      try {
        await upsertNormalizedFeedback(clientConfig, item.improvement_id, {
          problem,
          possible_reason: "待系统归因",
          possible_object: "当前 Agent 运行 / MCP 数据",
          impact: "待评估",
          suggestion: "进入改进处理",
          user_quote: wrong.trim(),
        });
        await addImprovementFeedback(clientConfig, item.improvement_id, {
          summary: problem,
          source: "playground_run",
          raw_text: wrong.trim(),
          run_id: context.runId || "",
          session_id: context.sessionId || "",
          agent_version_id: context.agentVersionId || "",
          scenario: context.scenario || "",
          task_id: context.taskId || "",
          alert_id: context.alertId || "",
          case_id: context.caseId || "",
        });
      } catch { /* 非致命：改进事项已创建 */ }
    }).catch((e) => setError(e instanceof Error ? e.message : String(e))).finally(() => setBusy(false));
  };

  return (
    <DrawerShell
      title={phase === "saved" ? "反馈已保存" : phase === "understanding" ? "确认系统理解" : "创建反馈"}
      description="保留当前对话上下文，把反馈整理为可推进的改进事项。"
      size="narrow"
      testId="feedback-drawer"
      dataState={phase}
      className="feedback-drawer"
      bodyClassName="feedback-drawer-body"
      onClose={onClose}
    >
      {error ? <div className="error-box" data-testid="feedback-drawer-error">{error}</div> : null}
      {phase === "input" ? (
        <>
          <label className="feedback-field">
            <span>这个结果哪里不对？</span>
            <textarea data-testid="feedback-input-wrong" value={wrong} onChange={(e) => setWrong(e.target.value)} placeholder="例如：这个告警其实是误报，AI 没注意到事件时间和告警时间不一致。" />
          </label>
          <label className="feedback-field">
            <span>希望以后怎么处理？（可选）</span>
            <textarea data-testid="feedback-input-expected" value={expected} onChange={(e) => setExpected(e.target.value)} placeholder="例如：应先提示时间窗口不一致，要求核验真实数据源。" />
          </label>
          <div className="feedback-autoinclude">
            <span>系统会自动带入：</span>
            <span>✓ 当前 Run / Trace</span>
            <span>✓ 业务 Agent（{context.agentName}）</span>
            {context.agentVersionId ? <span>✓ Agent 版本（{context.agentVersionId}）</span> : null}
            {context.scenario || context.alertId || context.caseId ? <span>✓ 场景上下文</span> : null}
          </div>
          <div className="feedback-drawer-actions">
            <button className="secondary-button" onClick={onClose}>取消</button>
            <button className="primary-button" data-testid="feedback-organize" disabled={!wrong.trim()} onClick={organize}>整理反馈</button>
          </div>
        </>
      ) : phase === "understanding" ? (
        <>
          <div className="feedback-understanding-card" data-testid="feedback-understanding-card">
            <div className="feedback-understanding-note">系统整理（初步）：</div>
            <ul>
              <li>问题：{problem}</li>
              <li>原因：待系统归因（确认保存后进入归因）</li>
              <li>可能对象：当前 Agent 运行 / MCP 数据</li>
              <li>归属：{context.agentName}{context.agentVersionId ? ` / ${context.agentVersionId}` : ""}</li>
              {context.scenario ? <li>场景：{context.scenario}</li> : null}
              <li>影响：待评估</li>
              <li>建议：进入改进处理</li>
            </ul>
            <div className="feedback-understanding-quote">用户原话：“{wrong.trim()}”</div>
          </div>
          <div className="feedback-drawer-actions">
            <button className="secondary-button" data-testid="feedback-edit" onClick={() => setPhase("input")}>修改</button>
            <button className="primary-button" data-testid="feedback-confirm-save" disabled={busy} onClick={save}>确认保存</button>
          </div>
        </>
      ) : (
        <div data-testid="feedback-saved">
          <p>已保存为一条反馈，并生成改进事项：</p>
          <div className="feedback-saved-item">
            <strong>{created?.title}</strong>
            <span className="muted">归属：{context.agentName} · 当前阶段：{created?.improvement_stage}</span>
          </div>
          <p className="muted">系统将按自动化策略判断是否归因 / 生成方案 / 生成回归保障。</p>
          <div className="feedback-drawer-actions">
            <button className="secondary-button" onClick={onClose}>关闭</button>
            <button className="primary-button" data-testid="feedback-view-improvement" onClick={() => { if (created) onCreated(created); onClose(); }}>查看改进事项</button>
          </div>
        </div>
      )}
    </DrawerShell>
  );
}
