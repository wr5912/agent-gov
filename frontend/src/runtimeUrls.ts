export function makeApiDocsUrl(apiBase: string): string {
  const base = apiBase.trim().replace(/\/$/, "");
  if (!base) return "/docs";
  return `${base}/docs`;
}

function isLoopbackHost(hostname: string): boolean {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "0.0.0.0" || hostname === "::1";
}

export function defaultLangfuseUrl(): string {
  const configured = (import.meta.env.VITE_LANGFUSE_URL || "http://localhost:53000").trim();
  let parsed: URL | null = null;
  try {
    parsed = new URL(configured);
  } catch {
    parsed = null;
  }
  if (parsed && !isLoopbackHost(parsed.hostname)) return configured;
  if (typeof window !== "undefined" && window.location?.hostname) {
    const protocol = window.location.protocol === "https:" ? "https" : "http";
    const port = parsed?.port || "53000";
    return `${protocol}://${window.location.hostname}${port ? `:${port}` : ""}`;
  }
  return configured;
}
