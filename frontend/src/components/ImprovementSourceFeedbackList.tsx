import type { ImprovementFeedback, ImprovementItem } from "../api/improvements";

export function SourceFeedbackList({ item, feedbacks, compact }: {
  item: ImprovementItem;
  feedbacks: ImprovementFeedback[];
  compact?: boolean;
}) {
  const refs = item.source_feedback_refs ?? [];
  const rows = feedbacks.length ? feedbacks.slice(0, compact ? 2 : feedbacks.length) : [];
  if (!rows.length) {
    return <div className="iw-source-refs" data-testid="improvement-source-refs">{refs.map((ref) => <span className="iw-ref" key={ref}>{ref}</span>)}</div>;
  }
  return (
    <div className="iw-source-feedback-list">
      {rows.map((feedback, index) => (
        <div className="iw-source-feedback-item" key={feedback.feedback_id}>
          <span>#{index + 1}</span>
          <strong>用户反馈</strong>
          <p>{feedback.summary}</p>
          <small>{feedback.created_at || ""} {feedback.run_id ? `· Run: ${feedback.run_id}` : ""}</small>
        </div>
      ))}
    </div>
  );
}
