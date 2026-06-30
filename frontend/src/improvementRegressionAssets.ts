import { createAsset, listAssets, type Asset } from "./api/assets";
import type {
  Attribution,
  ExecutionRecord,
  ImprovementFeedback,
  ImprovementItem,
  NormalizedFeedback,
  OptimizationPlan,
  RegressionAssessment,
} from "./api/improvements";
import type { RuntimeClientConfig } from "./types/runtime";

export async function adoptRegressionArtifacts({
  clientConfig,
  item,
  regressionAssessment,
  feedbacks,
  execution,
  normalizedFeedback,
  attribution,
  optimizationPlan,
}: {
  clientConfig: RuntimeClientConfig;
  item: ImprovementItem;
  regressionAssessment: RegressionAssessment | null;
  feedbacks: ImprovementFeedback[];
  execution: ExecutionRecord | null;
  normalizedFeedback: NormalizedFeedback | null;
  attribution: Attribution | null;
  optimizationPlan: OptimizationPlan | null;
}): Promise<Asset[]> {
  const cases = regressionAssessment?.cases ?? [];
  const sourceFeedbackIds = feedbacks.map((f) => f.feedback_id || "").filter(Boolean);
  const baselineVersion = feedbacks.find((f) => f.agent_version_id)?.agent_version_id || "";
  const candidateVersion = execution?.applied_agent_version_id || execution?.agent_version || "";
  const testDatasetId = `tds-${item.improvement_id}`;
  const datasetBody = JSON.stringify({
    test_dataset_id: testDatasetId,
    agent_id: item.agent_id,
    improvement_id: item.improvement_id,
    lifecycle: "candidate",
    source_feedback_refs: sourceFeedbackIds.length ? sourceFeedbackIds : item.source_feedback_refs ?? [],
    test_cases: cases.map((c, i) => ({
      case_id: `${testDatasetId}-case-${i + 1}`,
      prompt: c.prompt,
      expected_behavior: c.expected_behavior,
      checkpoints: c.checkpoints,
    })),
    selection_strategy: "current-improvement-regression",
    scope: "改进事项测试发布阶段",
    baseline_version: baselineVersion || "missing",
    candidate_version: candidateVersion || "missing",
    provenance: {
      normalized_feedback_id: normalizedFeedback?.normalized_feedback_id,
      attribution_id: attribution?.attribution_id,
      optimization_plan_id: optimizationPlan?.optimization_plan_id,
      execution_id: execution?.execution_id,
    },
  }, null, 2);
  await createAsset(clientConfig, {
    agent_id: item.agent_id,
    asset_type: "test_dataset",
    title: `测试数据集：${item.title}`,
    body: datasetBody,
    source_improvement_id: item.improvement_id,
  });
  const body = cases.length
    ? cases.map((c, i) => `用例${i + 1}：${c.prompt}\n期望：${c.expected_behavior}\n检查点：\n${(c.checkpoints || []).map((x) => `- ${x}`).join("\n")}`).join("\n\n")
    : `用例：当出现「${item.title}」类问题时，Agent 应正确处理，不得直接误判。\n检查点：\n${["是否识别问题条件", "是否提示需核验数据源", "是否避免直接升级处置"].map((c) => `- ${c}`).join("\n")}`;
  await createAsset(clientConfig, {
    agent_id: item.agent_id,
    asset_type: "regression",
    title: `回归保障：${item.title}`,
    body,
    source_improvement_id: item.improvement_id,
  });
  return listAssets(clientConfig, { sourceImprovementId: item.improvement_id });
}
