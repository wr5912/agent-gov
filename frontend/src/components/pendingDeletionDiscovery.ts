export interface PendingDeletionRequestGeneration {
  context: number;
  request: number;
}

export interface PendingDeletionRequestToken {
  context: number;
  request: number;
}

interface PendingDeletionRequestHandlers<T> {
  onSuccess: (value: T) => void;
  onError: (cause: unknown) => void;
  onFinally?: () => void;
}

export function activatePendingDeletionContext(
  generation: PendingDeletionRequestGeneration,
): number {
  generation.context += 1;
  generation.request = 0;
  return generation.context;
}

export function deactivatePendingDeletionContext(
  generation: PendingDeletionRequestGeneration,
  context: number,
): void {
  if (generation.context !== context) return;
  generation.context += 1;
  generation.request = 0;
}

export function beginPendingDeletionRequest(
  generation: PendingDeletionRequestGeneration,
): PendingDeletionRequestToken {
  generation.request += 1;
  return { context: generation.context, request: generation.request };
}

function isCurrentPendingDeletionRequest(
  generation: PendingDeletionRequestGeneration,
  token: PendingDeletionRequestToken,
): boolean {
  return generation.context === token.context && generation.request === token.request;
}

export async function settlePendingDeletionRequest<T>(
  generation: PendingDeletionRequestGeneration,
  token: PendingDeletionRequestToken,
  request: Promise<T>,
  handlers: PendingDeletionRequestHandlers<T>,
): Promise<void> {
  try {
    const value = await request;
    if (isCurrentPendingDeletionRequest(generation, token)) handlers.onSuccess(value);
  } catch (cause) {
    if (isCurrentPendingDeletionRequest(generation, token)) handlers.onError(cause);
  } finally {
    if (isCurrentPendingDeletionRequest(generation, token)) handlers.onFinally?.();
  }
}
