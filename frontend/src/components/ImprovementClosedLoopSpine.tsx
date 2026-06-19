const CLOSED_LOOP_STEPS = ["反馈", "理解", "归因", "方案", "执行", "回归", "发布", "资产"];

export function ImprovementClosedLoopSpine({
  currentIndex,
  hasAssets,
}: {
  currentIndex: number;
  hasAssets: boolean;
}) {
  return (
    <div className="iw-closed-loop-spine" data-testid="closed-loop-spine" aria-label="治理闭环">
      {CLOSED_LOOP_STEPS.map((label, index) => {
        const state = index === CLOSED_LOOP_STEPS.length - 1 && hasAssets
          ? "done"
          : index < currentIndex
            ? "done"
            : index === currentIndex
              ? "current"
              : "todo";
        return (
          <span className={`iw-loop-step is-${state}`} data-testid="closed-loop-step" data-state={state} key={label}>
            {label}
          </span>
        );
      })}
    </div>
  );
}
