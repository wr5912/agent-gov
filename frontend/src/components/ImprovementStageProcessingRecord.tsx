import type {
  Attribution,
  ExecutionRecord,
  OptimizationPlan,
  RegressionTestDesign,
} from "../api/improvements";
import { hasAppliedExecution } from "../improvementExecutionState";
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
  regressionTestDesign,
  pendingOperation,
}: {
  stageView: ImprovementStageView;
  attribution: Attribution | null;
  optimizationPlan: OptimizationPlan | null;
  execution: ExecutionRecord | null;
  regressionTestDesign: RegressionTestDesign | null;
  pendingOperation?: ImprovementPendingOperation | null;
}) {
  const records = localRecords(stageView.visibleKey, {
    attribution,
    optimizationPlan,
    execution,
    regressionTestDesign,
    pendingOperation,
    completed: stageView.isCompleted,
  });
  return (
    <section className="iw-stage-card iw-processing-record" data-testid="stage-local-record">
      <div className="iw-stage-card-head">
        <h4>处理记录</h4>
        <details className="iw-full-chain-inline" data-testid="full-chain">
          <summary>查看完整链路</summary>
          <ol className="iw-chain">
            {stageView.stages.map((stage, index) => {
              const done = index < stageView.stageIndex || (stageView.isCompleted && index === stageView.stageIndex);
              const word = done ? "已完成" : index === stageView.stageIndex ? "当前阶段" : "待开始";
              return (
                <li key={stage.key} data-testid="full-chain-step" className={done ? "is-done" : index === stageView.stageIndex ? "is-current" : ""}>
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
    regressionTestDesign,
    pendingOperation,
    completed,
  }: {
    attribution: Attribution | null;
    optimizationPlan: OptimizationPlan | null;
    execution: ExecutionRecord | null;
    regressionTestDesign: RegressionTestDesign | null;
    pendingOperation?: ImprovementPendingOperation | null;
    completed: boolean;
  },
) {
  const executionApplied = hasAppliedExecution(execution);
  const generatedTestFiles = regressionTestDesign?.generated_test_files ?? [];
  const testsMaterialized = generatedTestFiles.some((path) => path.startsWith("tests/") && path.endsWith(".py"));
  const testRun = regressionTestDesign?.test_run;
  const testPassed = testRun?.status === "passed";
  const testRunState: LocalRecordState = testPassed ? "done" : testRun ? "current" : "pending";
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
        rec("执行优化", pendingOperation?.kind === "apply_execution" ? "current" : executionApplied ? "done" : "pending", execution && !executionApplied ? "未绑定待发布变更" : undefined),
        rec("生成回归测试", pendingOperation?.kind === "generate_regression" ? "current" : regressionTestDesign ? "done" : "pending"),
      ];
    case "test_release":
      return [
        rec("进入测试发布", "done"),
        rec("生成回归测试", pendingOperation?.kind === "generate_regression" ? "current" : regressionTestDesign ? "done" : "pending"),
        rec("确认待发布变更", testsMaterialized ? "done" : "pending", testsMaterialized ? `${generatedTestFiles.length} 个测试文件已写入` : regressionTestDesign ? "待用户确认" : "待生成测试"),
        rec("运行测试", testRunState, testRun ? testRun.status : testsMaterialized ? "待显式运行" : "待确认变更"),
        rec(
          "发布版本",
          completed ? "done" : testPassed ? "current" : "pending",
          completed ? "已发布" : testPassed ? "平台测试已通过，待发布" : "等待当前待发布版本通过",
        ),
      ];
  }
}
