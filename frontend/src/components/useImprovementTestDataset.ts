import { useEffect, useState } from "react";

import {
  listTestDatasetRevisions,
  type TestDataset,
  type TestDatasetRevision,
} from "../api/assets";
import type {
  Attribution,
  ExecutionRecord,
  NormalizedFeedback,
  OptimizationPlan,
  RegressionAssessment,
} from "../api/improvements";
import type { RuntimeClientConfig } from "../types/runtime";

export type ImprovementTestDatasetChain = {
  improvementId: string;
  agentId: string;
  normalizedFeedback: NormalizedFeedback | null;
  attribution: Attribution | null;
  optimizationPlan: OptimizationPlan | null;
  execution: ExecutionRecord | null;
  regressionAssessment: RegressionAssessment | null;
};

export function isCurrentTestDataset(
  dataset: TestDataset,
  chain: ImprovementTestDatasetChain,
): boolean {
  const { normalizedFeedback, attribution, optimizationPlan, execution, regressionAssessment } = chain;
  if (!normalizedFeedback || !attribution || !optimizationPlan || !execution || !regressionAssessment) return false;
  const candidateVersion = execution.applied_agent_version_id;
  if (!candidateVersion) return false;
  const provenance = dataset.provenance;
  return dataset.source_improvement_id === chain.improvementId
    && dataset.agent_id === chain.agentId
    && provenance.normalized_feedback_id === normalizedFeedback.normalized_feedback_id
    && provenance.normalized_feedback_updated_at === normalizedFeedback.updated_at
    && provenance.attribution_id === attribution.attribution_id
    && provenance.attribution_updated_at === attribution.updated_at
    && provenance.optimization_plan_id === optimizationPlan.optimization_plan_id
    && provenance.optimization_plan_updated_at === optimizationPlan.updated_at
    && provenance.execution_id === execution.execution_id
    && provenance.execution_updated_at === execution.updated_at
    && provenance.regression_assessment_id === regressionAssessment.regression_assessment_id
    && provenance.regression_assessment_updated_at === regressionAssessment.updated_at
    && provenance.candidate_agent_version_id === candidateVersion;
}

export function useTestDatasetRevisions(
  clientConfig: RuntimeClientConfig,
  testDataset: TestDataset | null,
  reloadToken: number,
): {
  revisions: TestDatasetRevision[];
  error: string | undefined;
} {
  const [revisions, setRevisions] = useState<TestDatasetRevision[]>([]);
  const [error, setError] = useState<string | undefined>();

  useEffect(() => {
    if (!testDataset) {
      setRevisions([]);
      setError(undefined);
      return;
    }
    let cancelled = false;
    setRevisions([]);
    setError(undefined);
    void listTestDatasetRevisions(clientConfig, testDataset.dataset_id, testDataset.agent_id)
      .then((items) => { if (!cancelled) setRevisions(items); })
      .catch((loadError) => {
        if (!cancelled) {
          setError(`修订记录加载失败：${loadError instanceof Error ? loadError.message : String(loadError)}`);
        }
      });
    return () => { cancelled = true; };
  }, [clientConfig, reloadToken, testDataset?.agent_id, testDataset?.dataset_id, testDataset?.revision]);

  return { revisions, error };
}
