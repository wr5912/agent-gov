import type { ImprovementStageView } from "../improvementStage";

export function ImprovementClosedLoopSpine({
  stageView,
}: {
  stageView: ImprovementStageView;
}) {
  return (
    <div className="iw-four-stage-spine" data-testid="closed-loop-spine" aria-label="四阶段改进治理主链路">
      {stageView.stages.map((stage, index) => {
        const state = index < stageView.stageIndex
            ? "done"
            : index === stageView.stageIndex
              ? "current"
              : "todo";
        return (
          <div className={`iw-four-stage-step is-${state}`} data-testid="closed-loop-step" data-state={state} data-stage-key={stage.key} key={stage.key}>
            <span className="iw-four-stage-index">{state === "done" ? "✓" : index + 1}</span>
            <span>
              <strong>{stage.label}</strong>
              <small>{index === stageView.stageIndex ? stageView.description : index < stageView.stageIndex ? "完成" : "待开始"}</small>
            </span>
          </div>
        );
      })}
    </div>
  );
}
