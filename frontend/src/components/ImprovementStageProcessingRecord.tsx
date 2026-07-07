import type {
  Attribution,
  ExecutionRecord,
  OptimizationPlan,
  RegressionAssessment,
} from "../api/improvements";
import type { ImprovementPendingOperation } from "../improvementOperationState";
import type { ImprovementStageView } from "../improvementStage";

type LocalRecordState = "done" | "current" | "pending";

interface LocalRecord {
  label: string;
  state: LocalRecordState;
  status: string;
}

export function ImprovementStageProcessingRecord({
  stageView,
  attribution,
  optimizationPlan,
  execution,
  regressionAssessment,
  pendingOperation,
}: {
  stageView: ImprovementStageView;
  attribution: Attribution | null;
  optimizationPlan: OptimizationPlan | null;
  execution: ExecutionRecord | null;
  regressionAssessment: RegressionAssessment | null;
  pendingOperation?: ImprovementPendingOperation | null;
}) {
  const records = localRecords(stageView.visibleKey, {
    attribution,
    optimizationPlan,
    execution,
    regressionAssessment,
    pendingOperation,
  });
  return (
    <section className="iw-stage-card iw-processing-record" data-testid="stage-local-record">
      <div className="iw-stage-card-head">
        <h4>处理记录</h4>
        <details className="iw-full-chain-inline" data-testid="full-chain">
          <summary>查看完整链路</summary>
          <ol className="iw-chain">
            {stageView.stages.map((stage, index) => {
              const word = index < stageView.stageIndex ? "已完成" : index === stageView.stageIndex ? "当前阶段" : "待开始";
              return (
                <li key={stage.key} data-testid="full-chain-step" className={index === stageView.stageIndex ? "is-current" : index < stageView.stageIndex ? "is-done" : ""}>
                  <strong>{stage.label}</strong> - {word}
                </li>
              );
            })}
          </ol>
        </details>
      </div>
      <div className="iw-record-track">
        {records.map((record) => (
          <div className={`iw-record-node ${record.state}`} data-testid="stage-local-record-node" data-state={record.state} key={record.label}>
            <span>{record.state === "done" ? "✓" : record.state === "current" ? "●" : "○"}</span>
            <strong>{record.label}</strong>
            <small>{record.status}</small>
          </div>
        ))}
      </div>
    </section>
  );
}

function rec(label: string, state: LocalRecordState, status?: string): LocalRecord {
  return { label, state, status: status ?? (state === "done" ? "已完成" : state === "current" ? "生成中" : "待生成") };
}

function localRecords(
  stage: ImprovementStageView["visibleKey"],
  {
    attribution,
    optimizationPlan,
    execution,
    regressionAssessment,
    pendingOperation,
  }: {
    attribution: Attribution | null;
    optimizationPlan: OptimizationPlan | null;
    execution: ExecutionRecord | null;
    regressionAssessment: RegressionAssessment | null;
    pendingOperation?: ImprovementPendingOperation | null;
  },
) {
  switch (stage) {
    case "feedback_sorting":
      return [
        rec("收到反馈", "done"),
        rec("相似归并", "done"),
        rec("系统整理", "done"),
        rec("证据确认", "done"),
        rec("生成归因分析", pendingOperation?.kind === "generate_attribution" ? "current" : attribution ? "done" : "pending"),
      ];
    case "attribution_analysis":
      return [
        rec("生成归因分析", pendingOperation?.kind === "generate_attribution" ? "current" : attribution ? "done" : "pending"),
        rec("收集证据链", attribution ? "done" : "pending"),
        rec("Trace 定位", attribution ? "done" : "pending"),
        rec("归因结论", attribution ? "done" : "pending"),
        rec("生成优化方案", pendingOperation?.kind === "generate_optimization_plan" ? "current" : optimizationPlan ? "done" : "pending"),
      ];
    case "optimization_execution":
      return [
        rec("进入优化执行", "done"),
        rec("生成优化方案", pendingOperation?.kind === "generate_optimization_plan" ? "current" : optimizationPlan ? "done" : "pending"),
        rec("风险评估", optimizationPlan ? "done" : "pending"),
        rec("执行优化", pendingOperation?.kind === "apply_execution" ? "current" : execution ? "done" : "pending"),
        rec("生成回归测试", pendingOperation?.kind === "generate_regression" ? "current" : regressionAssessment ? "done" : "pending"),
      ];
    case "test_release":
      return [
        rec("进入测试发布", "done"),
        rec("执行回归测试", pendingOperation?.kind === "generate_regression" ? "current" : regressionAssessment ? "done" : "pending"),
        rec("确认测试集", "pending", regressionAssessment ? "待确认" : "待生成"),
        rec("发布门禁预览", regressionAssessment ? "done" : "pending"),
      ];
  }
}
