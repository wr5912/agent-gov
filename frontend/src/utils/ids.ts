function randomId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : Math.random().toString(36).slice(2);
}

export function newId(prefix: string): string {
  return `${prefix}_${randomId()}`;
}

// 会话 ID 使用裸 UUID，便于首次 SDK 调用直接对齐 session_id 与 sdk_session_id。
export function newSessionId(): string {
  return randomId();
}
