import type { VisibleImprovementStageKey } from "../improvementStage";
import type { ImprovementPendingOperation } from "../improvementOperationState";
import { GenerationStatus } from "./ImprovementStagePrimitives";

const OPERATION_TARGET_STAGE: Record<
  ImprovementPendingOperation["kind"],
  { stage: VisibleImprovementStageKey; testId: string }
> = {
  generate_attribution: { stage: "attribution_analysis", testId: "attribution-generation-status" },
  generate_optimization_plan: { stage: "optimization_execution", testId: "optimization-generation-status" },
  apply_execution: { stage: "optimization_execution", testId: "execution-generation-status" },
  generate_regression: { stage: "test_release", testId: "regression-generation-status" },
};

export function ImprovementCrossStageGenerationStatus({
  operation,
  visibleStage,
}: {
  operation: ImprovementPendingOperation | null | undefined;
  visibleStage: VisibleImprovementStageKey;
}) {
  if (!operation) return null;
  const target = OPERATION_TARGET_STAGE[operation.kind];
  if (target.stage === visibleStage) return null;
  return <GenerationStatus operation={operation} testId={target.testId} />;
}
