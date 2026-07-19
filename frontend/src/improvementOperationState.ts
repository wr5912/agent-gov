import type { ImprovementPrimaryDecisionKind } from "./improvementDecisionActions";

export interface ImprovementPendingOperation {
  kind: ImprovementPrimaryDecisionKind;
  label: string;
}

export interface ImprovementOperationError {
  kind?: ImprovementPrimaryDecisionKind;
  label?: string;
  message: string;
}

const OPERATION_LABELS: Record<ImprovementPrimaryDecisionKind, string> = {
  generate_attribution: "生成归因分析",
  generate_optimization_plan: "生成优化方案",
  apply_execution: "执行优化",
  generate_regression: "生成回归测试",
};

export function operationLabel(kind: ImprovementPrimaryDecisionKind): string {
  return OPERATION_LABELS[kind];
}

export function operationStatusText(operation: ImprovementPendingOperation): string {
  return `正在${operation.label}，请稍候。`;
}

export function isPendingOperation(
  operation: ImprovementPendingOperation | null | undefined,
  kind: ImprovementPrimaryDecisionKind,
): boolean {
  return operation?.kind === kind;
}
