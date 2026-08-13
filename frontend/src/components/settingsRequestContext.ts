export interface SettingsRequestGeneration {
  active: boolean;
  context: number;
  lanes: Record<string, number>;
}

export interface SettingsRequestToken {
  context: number;
  lanes: Record<string, number>;
}

export interface SettingsRequestAuthority {
  token: SettingsRequestToken;
  isCurrent: (lanes?: readonly string[]) => boolean;
}

type SettingsRequestHandler<T> = (value: T) => void | Promise<void>;

interface SettingsRequestHandlers<T> {
  onSuccess: SettingsRequestHandler<T>;
  onError: SettingsRequestHandler<unknown>;
  onFinally?: () => void | Promise<void>;
  successLanes?: readonly string[];
  errorLanes?: readonly string[];
  finallyLanes?: readonly string[];
}

export function activateSettingsRequestContext(
  generation: SettingsRequestGeneration,
): number {
  generation.active = true;
  generation.context += 1;
  generation.lanes = {};
  return generation.context;
}

export function deactivateSettingsRequestContext(
  generation: SettingsRequestGeneration,
  context?: number,
): void {
  if (context !== undefined && generation.context !== context) return;
  generation.active = false;
  generation.context += 1;
  generation.lanes = {};
}

export function beginSettingsRequest(
  generation: SettingsRequestGeneration,
  claimedLanes: readonly string[],
  observedLanes: readonly string[] = [],
): SettingsRequestToken {
  const laneGenerations: Record<string, number> = {};
  for (const lane of claimedLanes) {
    const nextGeneration = (generation.lanes[lane] ?? 0) + 1;
    generation.lanes[lane] = nextGeneration;
    laneGenerations[lane] = nextGeneration;
  }
  for (const lane of observedLanes) {
    laneGenerations[lane] = generation.lanes[lane] ?? 0;
  }
  return { context: generation.context, lanes: laneGenerations };
}

export function isCurrentSettingsRequest(
  generation: SettingsRequestGeneration,
  token: SettingsRequestToken,
  lanes?: readonly string[],
): boolean {
  if (!generation.active || generation.context !== token.context) return false;
  return (lanes ?? Object.keys(token.lanes)).every(
    (lane) => (generation.lanes[lane] ?? 0) === token.lanes[lane],
  );
}

export function beginSettingsRequestAuthority(
  generation: SettingsRequestGeneration,
  claimedLanes: readonly string[],
  observedLanes: readonly string[] = [],
): SettingsRequestAuthority {
  const token = beginSettingsRequest(generation, claimedLanes, observedLanes);
  return {
    token,
    isCurrent: (lanes) => isCurrentSettingsRequest(generation, token, lanes),
  };
}

export async function settleSettingsRequest<T>(
  generation: SettingsRequestGeneration,
  token: SettingsRequestToken,
  request: Promise<T>,
  handlers: SettingsRequestHandlers<T>,
): Promise<void> {
  try {
    const value = await request;
    if (isCurrentSettingsRequest(generation, token, handlers.successLanes)) {
      await handlers.onSuccess(value);
    }
  } catch (cause) {
    if (isCurrentSettingsRequest(generation, token, handlers.errorLanes)) {
      await handlers.onError(cause);
    }
  } finally {
    if (isCurrentSettingsRequest(generation, token, handlers.finallyLanes)) {
      await handlers.onFinally?.();
    }
  }
}
