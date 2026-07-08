const DEFAULT_LANGFUSE_PROJECT = "agent-gov";

function clean(value?: string | null): string {
  return typeof value === "string" ? value.trim() : "";
}

function trimTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

function tracePathFromUrl(value: string): string {
  if (!value) return "";
  try {
    const parsed = new URL(value);
    return parsed.pathname.includes("/traces/") ? `${parsed.pathname}${parsed.search}${parsed.hash}` : "";
  } catch {
    const projectIndex = value.indexOf("/project/");
    if (projectIndex >= 0) return value.slice(projectIndex);
    return value.startsWith("/") && value.includes("/traces/") ? value : "";
  }
}

function tracePathFromId(traceId: string): string {
  return traceId ? `/project/${DEFAULT_LANGFUSE_PROJECT}/traces/${encodeURIComponent(traceId)}` : "";
}

export function concreteLangfuseTraceUrl({
  langfuseBaseUrl,
  traceId,
  traceUrl,
}: {
  langfuseBaseUrl: string;
  traceId?: string | null;
  traceUrl?: string | null;
}): string {
  const base = trimTrailingSlash(clean(langfuseBaseUrl));
  const rawTraceUrl = clean(traceUrl);
  const path = tracePathFromUrl(rawTraceUrl) || tracePathFromId(clean(traceId));
  if (!path) return "";
  return base ? `${base}${path}` : rawTraceUrl || path;
}
