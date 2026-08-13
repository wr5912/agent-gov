const AGENT_ID_RE = /^[A-Za-z0-9._-]+$/;
export const AGENT_ID_MAX_LENGTH = 128;

export function validateAgentId(id: string): string | undefined {
  if (!id) return undefined;
  if (id.length > AGENT_ID_MAX_LENGTH) {
    return `Agent ID 最多 ${AGENT_ID_MAX_LENGTH} 个字符。`;
  }
  if (id === "." || id === ".." || !AGENT_ID_RE.test(id)) {
    return "仅允许字母、数字、点、下划线和连字符。";
  }
  return undefined;
}
