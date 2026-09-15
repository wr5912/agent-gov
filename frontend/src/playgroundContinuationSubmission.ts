import {
  connectedConfirmationTurn,
  type DetachedRunController,
  type PlaygroundActiveTurn,
} from "./playgroundDetachedRun";
import { reconcilePendingRequestCards } from "./playgroundPendingProjection";
import type {
  AssistantUpdater,
  PlaygroundRunOptions,
  RunRefs,
} from "./playgroundRunContract";
import {
  postPreparedContinuation,
  prepareExternalExecutionContinuation,
  prepareUserConfirmContinuation,
  recoverContinuationSubmission,
  type PreparedRuntimeContinuation,
} from "./playgroundRunHelpers";
import type {
  AgentScopeToolResultState,
  RuntimeExternalExecutionRequest,
  RuntimeUserConfirmAction,
  RuntimeUserConfirmRequest,
} from "./types/runtime";
import { runtimePendingRequestIdentity } from "./runtimePendingRequestIdentity";

export interface PlaygroundContinuationContext {
  options: PlaygroundRunOptions;
  refs: RunRefs;
  detached: DetachedRunController;
  bindRunHandle: (turn: PlaygroundActiveTurn, runId: string) => void;
  updateAssistant: (turn: PlaygroundActiveTurn, updater: AssistantUpdater) => void;
}

export async function submitPlaygroundUserConfirm(
  context: PlaygroundContinuationContext,
  request: RuntimeUserConfirmRequest,
  action: RuntimeUserConfirmAction,
) {
  await submitContinuation(context, "human", request, (turn) => (
    prepareUserConfirmContinuation(turn, request, action)
  ), (resolvedAt) => {
    context.options.updateUserConfirmRequest(request.requestId, {
      status: "resolved",
      decision: action,
      resolvedAt,
    });
  });
}

export async function submitPlaygroundExternalExecution(
  context: PlaygroundContinuationContext,
  request: RuntimeExternalExecutionRequest,
  state: AgentScopeToolResultState,
  outputs: Record<string, string>,
) {
  await submitContinuation(context, "external", request, (turn) => (
    prepareExternalExecutionContinuation(turn, request, state, outputs)
  ), (resolvedAt) => {
    context.options.updateExternalExecutionRequest(request.requestId, {
      status: "resolved",
      resultState: state,
      resolvedAt,
    });
  });
}

async function submitContinuation(
  context: PlaygroundContinuationContext,
  kind: "human" | "external",
  request: RuntimeUserConfirmRequest | RuntimeExternalExecutionRequest,
  prepare: (turn: PlaygroundActiveTurn) => PreparedRuntimeContinuation,
  resolveRequest: (resolvedAt: string) => void,
) {
  if (request.status !== "waiting") return;
  const fenceKey = claimContinuationSubmission(
    context.refs.continuationSubmissions.current,
    kind,
    runtimePendingRequestIdentity(request),
  );
  if (!fenceKey) return;
  clearRequestError(context.options, request.requestId);
  context.options.setSubmittingUserInputRequests((current) => new Set(current).add(request.requestId));

  let turn: PlaygroundActiveTurn | undefined;
  let postAttempted = false;
  let releaseFence = false;
  try {
    turn = await connectedConfirmationTurn(context.detached);
    const submission = prepare(turn);
    let acceptedRunId: string | undefined;
    try {
      postAttempted = true;
      const receipt = await postPreparedContinuation(context.options, turn, submission);
      if (receipt.runId !== turn.runtimeRunId) {
        throw new Error("Runtime 续跑返回了不同的 run_id，正在按原生 Event.id 核对。");
      }
      acceptedRunId = receipt.runId;
    } catch (error) {
      setUncertainError(context.options, request.requestId, kind);
      const recovered = await recoverContinuationSubmission(
        context.options, turn, submission, request,
      );
      if (recovered.kind === "not_submitted") {
        releaseFence = true;
        context.options.setUserInputErrors((current) => ({
          ...current,
          [request.requestId]: `${errorMessage(error)}；已按原生 Event.id 确认本次未提交，可以重试。`,
        }));
        return;
      }
      if (recovered.kind === "settled_elsewhere") {
        releaseFence = true;
        const activeTurn = turn;
        reconcilePendingRequestCards(
          activeTurn,
          recovered.pendingActions,
          (updater) => context.updateAssistant(activeTurn, updater),
        );
        context.options.setLastError(undefined);
        clearRequestError(context.options, request.requestId);
        return;
      }
      acceptedRunId = recovered.runId;
    }

    context.bindRunHandle(turn, acceptedRunId);
    resolveRequest(new Date().toISOString());
    context.options.dispatchRun({ type: "input_resolved", operationId: turn.operationId });
    context.options.setLastError(undefined);
    clearRequestError(context.options, request.requestId);
    releaseFence = true;
  } catch (error) {
    if (!turn?.controller.signal.aborted) {
      context.options.setUserInputErrors((current) => ({
        ...current,
        [request.requestId]: postAttempted
          ? `${errorMessage(error)}；提交状态仍不确定，卡片保持只读，可中断当前 run。`
          : errorMessage(error),
      }));
    }
    if (!postAttempted) releaseFence = true;
  } finally {
    if (releaseFence) releaseSubmissionFence(context, fenceKey, request.requestId);
  }
}

function setUncertainError(
  options: PlaygroundRunOptions,
  requestId: string,
  kind: "human" | "external",
) {
  const label = kind === "human" ? "确认结果" : "外部执行结果";
  options.setUserInputErrors((current) => ({
    ...current,
    [requestId]: `${label}回执状态不确定，正在按原生 Event.id 核对；核清前卡片保持只读。`,
  }));
}

function releaseSubmissionFence(
  context: PlaygroundContinuationContext,
  fenceKey: string,
  requestId: string,
) {
  releaseContinuationSubmission(context.refs.continuationSubmissions.current, fenceKey);
  context.options.setSubmittingUserInputRequests((current) => {
    const next = new Set(current);
    next.delete(requestId);
    return next;
  });
}

/** 同步占用必须发生在第一个 await 前，避免双击提交两个不同决定。 */
export function claimContinuationSubmission(
  fences: Set<string>,
  kind: "human" | "external",
  requestIdentity: string,
): string | undefined {
  const fenceKey = `${kind}:${requestIdentity}`;
  if (fences.has(fenceKey)) return undefined;
  fences.add(fenceKey);
  return fenceKey;
}

export function releaseContinuationSubmission(fences: Set<string>, fenceKey: string) {
  fences.delete(fenceKey);
}

function clearRequestError(options: PlaygroundRunOptions, requestId: string) {
  options.setUserInputErrors((current) => {
    const next = { ...current };
    delete next[requestId];
    return next;
  });
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}
