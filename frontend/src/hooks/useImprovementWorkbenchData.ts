import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { listAssets, type Asset } from "../api/assets";
import {
  findSimilarImprovements,
  getAttribution,
  getExecution,
  getNormalizedFeedback,
  getOptimizationPlan,
  getRegressionTestDesign,
  listImprovementFeedbacks,
  listImprovementLinks,
  type Attribution,
  type ExecutionRecord,
  type ImprovementArtifactPresence,
  type ImprovementFeedback,
  type ImprovementItem,
  type ImprovementLink,
  type ImprovementSimilarItem,
  type NormalizedFeedback,
  type OptimizationPlan,
  type RegressionTestDesign,
} from "../api/improvements";
import { ApiRequestError } from "../api/request";
import type { RuntimeClientConfig } from "../types/runtime";

export type ImprovementArtifactKey = keyof ImprovementArtifactPresence;

const ARTIFACT_KEYS: ImprovementArtifactKey[] = [
  "normalized_feedback",
  "attribution",
  "optimization_plan",
  "execution",
  "regression_test_design",
];

export interface ImprovementWorkbenchData {
  normalizedFeedback: NormalizedFeedback | null;
  attribution: Attribution | null;
  feedbacks: ImprovementFeedback[];
  optimizationPlan: OptimizationPlan | null;
  execution: ExecutionRecord | null;
  regressionTestDesign: RegressionTestDesign | null;
  assets: Asset[];
  similar: ImprovementSimilarItem[];
  links: ImprovementLink[];
}

export interface ImprovementAuxiliaryLoadError {
  resource: "assets" | "similar" | "links";
  message: string;
}

type DetailState =
  | { status: "idle"; key: ""; data: ImprovementWorkbenchData; error?: undefined; auxiliaryErrors: [] }
  | { status: "loading"; key: string; data: ImprovementWorkbenchData; error?: undefined; auxiliaryErrors: [] }
  | {
      status: "ready";
      key: string;
      data: ImprovementWorkbenchData;
      error?: undefined;
      auxiliaryErrors: ImprovementAuxiliaryLoadError[];
    }
  | { status: "error"; key: string; data: ImprovementWorkbenchData; error: string; auxiliaryErrors: [] };

function emptyData(): ImprovementWorkbenchData {
  return {
    normalizedFeedback: null,
    attribution: null,
    feedbacks: [],
    optimizationPlan: null,
    execution: null,
    regressionTestDesign: null,
    assets: [],
    similar: [],
    links: [],
  };
}

export function artifactRequestKeys(presence: unknown): ImprovementArtifactKey[] {
  const validated = validateArtifactPresence(presence);
  return ARTIFACT_KEYS.filter((key) => validated[key]);
}

function validateArtifactPresence(presence: unknown): ImprovementArtifactPresence {
  if (!presence || typeof presence !== "object") {
    throw new Error("API 契约错误：改进事项缺少 artifact_presence。");
  }
  const candidate = presence as Record<string, unknown>;
  for (const key of ARTIFACT_KEYS) {
    if (typeof candidate[key] !== "boolean") {
      throw new Error(`API 契约错误：artifact_presence.${key} 必须是 boolean。`);
    }
  }
  return presence as ImprovementArtifactPresence;
}

function presenceKey(presence: ImprovementArtifactPresence): string {
  return ARTIFACT_KEYS.map((key) => (presence[key] ? "1" : "0")).join("");
}

class DetailResourceLoadError extends Error {
  readonly resource: string;
  readonly requestError: unknown;

  constructor(resource: string, requestError: unknown) {
    super(requestError instanceof Error ? requestError.message : String(requestError));
    this.resource = resource;
    this.requestError = requestError;
  }
}

async function requiredResource<T>(resource: string, promise: Promise<T>): Promise<T> {
  try {
    return await promise;
  } catch (error) {
    throw new DetailResourceLoadError(resource, error);
  }
}

async function optionalResource<T>(
  resource: ImprovementAuxiliaryLoadError["resource"],
  promise: Promise<T>,
  fallback: T,
): Promise<{ value: T; error?: ImprovementAuxiliaryLoadError }> {
  try {
    return { value: await promise };
  } catch (error) {
    return {
      value: fallback,
      error: {
        resource,
        message: error instanceof Error ? error.message : String(error),
      },
    };
  }
}

async function loadWorkbenchData(
  clientConfig: RuntimeClientConfig,
  itemId: string,
  presence: ImprovementArtifactPresence,
  signal: AbortSignal,
): Promise<{ data: ImprovementWorkbenchData; auxiliaryErrors: ImprovementAuxiliaryLoadError[] }> {
  const readOptions = { signal };
  const corePromise = Promise.all([
    requiredResource("来源反馈", listImprovementFeedbacks(clientConfig, itemId, readOptions)),
    presence.normalized_feedback
      ? requiredResource("系统整理", getNormalizedFeedback(clientConfig, itemId, readOptions))
      : Promise.resolve(null),
    presence.attribution
      ? requiredResource("归因分析", getAttribution(clientConfig, itemId, readOptions))
      : Promise.resolve(null),
    presence.optimization_plan
      ? requiredResource("优化方案", getOptimizationPlan(clientConfig, itemId, readOptions))
      : Promise.resolve(null),
    presence.execution
      ? requiredResource("执行记录", getExecution(clientConfig, itemId, readOptions))
      : Promise.resolve(null),
    presence.regression_test_design
      ? requiredResource("回归测试设计", getRegressionTestDesign(clientConfig, itemId, readOptions))
      : Promise.resolve(null),
  ]);
  const [core, assets, similar, links] = await Promise.all([
    corePromise,
    optionalResource("assets", listAssets(clientConfig, { sourceImprovementId: itemId }, readOptions), []),
    optionalResource("similar", findSimilarImprovements(clientConfig, itemId, readOptions), []),
    optionalResource("links", listImprovementLinks(clientConfig, itemId, readOptions), []),
  ]);
  const [feedbacks, normalizedFeedback, attribution, optimizationPlan, execution, regressionTestDesign] = core;
  return {
    data: {
      feedbacks,
      normalizedFeedback,
      attribution,
      optimizationPlan,
      execution,
      regressionTestDesign,
      assets: assets.value,
      similar: similar.value,
      links: links.value,
    },
    auxiliaryErrors: [assets.error, similar.error, links.error].filter(
      (error): error is ImprovementAuxiliaryLoadError => Boolean(error),
    ),
  };
}

function detailErrorMessage(error: unknown): string {
  if (error instanceof DetailResourceLoadError) {
    if (error.requestError instanceof ApiRequestError && error.requestError.status === 404) {
      return `${error.resource}已发生变化，请刷新后重试。`;
    }
    return `${error.resource}加载失败：${error.message}`;
  }
  return error instanceof Error ? error.message : String(error);
}

export function useImprovementWorkbenchData(
  clientConfig: RuntimeClientConfig,
  item: ImprovementItem | null,
  refreshRevision: number,
) {
  const [manualRevision, setManualRevision] = useState(0);
  const generationRef = useRef(0);
  const presenceResult = useMemo(() => {
    try {
      const presence = validateArtifactPresence(item?.artifact_presence);
      return { presence, key: presenceKey(presence), error: undefined };
    } catch (error) {
      return {
        presence: undefined,
        key: "invalid",
        error: error instanceof Error ? error.message : String(error),
      };
    }
  }, [item?.artifact_presence]);
  const desiredKey = item
    ? `${item.improvement_id}:${presenceResult.key}:${refreshRevision}:${manualRevision}`
    : "";
  const [state, setState] = useState<DetailState>({
    status: "idle",
    key: "",
    data: emptyData(),
    auxiliaryErrors: [],
  });

  useEffect(() => {
    if (!item) {
      setState({ status: "idle", key: "", data: emptyData(), auxiliaryErrors: [] });
      return;
    }
    if (!presenceResult.presence) {
      setState({
        status: "error",
        key: desiredKey,
        data: emptyData(),
        error: presenceResult.error || "API 契约错误：产物存在性不可用。",
        auxiliaryErrors: [],
      });
      return;
    }
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    const controller = new AbortController();
    const itemId = item.improvement_id;
    setState({ status: "loading", key: desiredKey, data: emptyData(), auxiliaryErrors: [] });
    void Promise.resolve()
      .then(() => {
        if (controller.signal.aborted) return null;
        return loadWorkbenchData(clientConfig, itemId, presenceResult.presence, controller.signal);
      })
      .then((result) => {
        if (!result) return;
        if (controller.signal.aborted || generationRef.current !== generation) return;
        setState({
          status: "ready",
          key: desiredKey,
          data: result.data,
          auxiliaryErrors: result.auxiliaryErrors,
        });
      })
      .catch((error) => {
        if (controller.signal.aborted || generationRef.current !== generation) return;
        controller.abort();
        setState({
          status: "error",
          key: desiredKey,
          data: emptyData(),
          error: detailErrorMessage(error),
          auxiliaryErrors: [],
        });
      });
    return () => controller.abort();
  }, [clientConfig, desiredKey, item?.improvement_id, presenceResult.error, presenceResult.presence]);

  const reload = useCallback(() => setManualRevision((revision) => revision + 1), []);
  const patchData = useCallback((patch: Partial<ImprovementWorkbenchData>) => {
    setState((current) => {
      if (current.status !== "ready" || current.key !== desiredKey) return current;
      return { ...current, data: { ...current.data, ...patch } };
    });
  }, [desiredKey]);

  if (!item) return { ...state, reload, patchData };
  if (state.key !== desiredKey) {
    if (presenceResult.error) {
      return {
        status: "error" as const,
        key: desiredKey,
        data: emptyData(),
        error: presenceResult.error,
        auxiliaryErrors: [] as ImprovementAuxiliaryLoadError[],
        reload,
        patchData,
      };
    }
    return {
      status: "loading" as const,
      key: desiredKey,
      data: emptyData(),
      error: undefined,
      auxiliaryErrors: [] as ImprovementAuxiliaryLoadError[],
      reload,
      patchData,
    };
  }
  return { ...state, reload, patchData };
}
