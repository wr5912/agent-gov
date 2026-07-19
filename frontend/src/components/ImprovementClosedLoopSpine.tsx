import type { ImprovementStageView, VisibleImprovementStageKey } from "../improvementStage";

export function ImprovementClosedLoopSpine({
  stageView,
  reviewStageKey,
  onReviewStage,
}: {
  stageView: ImprovementStageView;
  reviewStageKey: VisibleImprovementStageKey | null;
  onReviewStage: (stageKey: VisibleImprovementStageKey) => void;
}) {
  return (
    <div className="iw-four-stage-spine" data-testid="closed-loop-spine" aria-label="四阶段改进治理主链路">
      {stageView.stages.map((stage, index) => {
        const state = stageView.isCompleted && index === stageView.stageIndex
          ? "done"
          : index < stageView.stageIndex
            ? "done"
            : index === stageView.stageIndex
              ? "current"
              : "todo";
        const stageKey = stage.key as VisibleImprovementStageKey;
        const isReviewing = reviewStageKey === stageKey;
        const canReview = index <= stageView.stageIndex;
        return (
          <button
            className={`iw-four-stage-step is-${state} ${isReviewing ? "is-reviewing" : ""}`}
            type="button"
            data-testid="closed-loop-step"
            data-state={state}
            data-reviewing={isReviewing ? "true" : "false"}
            data-stage-key={stage.key}
            disabled={!canReview}
            aria-pressed={isReviewing}
            onClick={() => onReviewStage(stageKey)}
            key={stage.key}
          >
            <span className="iw-four-stage-index">{state === "done" ? "✓" : index + 1}</span>
            <span>
              <strong>{stage.label}</strong>
              <small>{isReviewing ? "正在回看" : state === "done" ? "完成" : index === stageView.stageIndex ? stageView.description : "待开始"}</small>
            </span>
          </button>
        );
      })}
    </div>
  );
}
