const AGENT_ID_RE = /^[A-Za-z0-9._-]+$/;

export function validateAgentId(id: string): string | undefined {
  if (!id) return undefined;
  if (id === "." || id === ".." || !AGENT_ID_RE.test(id)) {
    return "仅允许字母、数字、点、下划线、连字符（留空将自动生成）。";
  }
  return undefined;
}
