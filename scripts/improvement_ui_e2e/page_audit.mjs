import { mkdir } from "node:fs/promises";
import path from "node:path";

const OPTIONAL_ARTIFACT_404 = [
  /\/api\/improvements\/[^/]+\/(attribution|optimization-plan|execution|regression-assessment)$/,
];

function responseRecord(response) {
  const request = response.request();
  const url = new URL(response.url());
  return { method: request.method(), path: url.pathname, status: response.status() };
}

function isOptionalArtifact404(item) {
  return item.method === "GET"
    && item.status === 404
    && OPTIONAL_ARTIFACT_404.some((pattern) => pattern.test(item.path));
}

function isExpectedHttpError(item, expectedHttpErrors) {
  return expectedHttpErrors.some((expected) => (
    item.method === expected.method
    && item.path === expected.path
    && item.status === expected.status
  ));
}

export function attachDiagnostics(page, apiBase) {
  const state = { consoleErrors: [], pageErrors: [], requestFailures: [], httpErrors: [], requests: [] };
  const apiOrigin = new URL(apiBase).origin;
  page.on("console", (message) => {
    if (message.type() === "error") {
      state.consoleErrors.push({ text: message.text(), url: message.location().url || "" });
    }
  });
  page.on("pageerror", (error) => state.pageErrors.push(error.message));
  page.on("requestfailed", (request) => {
    state.requestFailures.push({ method: request.method(), url: request.url(), error: request.failure()?.errorText || "unknown" });
  });
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.origin === apiOrigin) {
      state.requests.push({ method: request.method(), path: url.pathname, postData: request.postData() || "" });
    }
  });
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (url.origin === apiOrigin && response.status() >= 400) state.httpErrors.push(responseRecord(response));
  });
  return state;
}

export function unexpectedDiagnostics(state, expectedHttpErrors = []) {
  const optionalHttpPaths = new Set(state.httpErrors.filter(isOptionalArtifact404).map((item) => item.path));
  const expectedHttpPaths = new Set(
    state.httpErrors.filter((item) => isExpectedHttpError(item, expectedHttpErrors)).map((item) => item.path),
  );
  const ignoredHttpPaths = new Set([...optionalHttpPaths, ...expectedHttpPaths]);
  const unexpectedHttp = state.httpErrors.filter((item) => (
    !isOptionalArtifact404(item) && !isExpectedHttpError(item, expectedHttpErrors)
  ));
  const unexpectedConsole = state.consoleErrors.filter((message) => {
    if (!/Failed to load resource: the server responded with a status of \d+/.test(message.text)) return true;
    if (!message.url) return true;
    try {
      return !ignoredHttpPaths.has(new URL(message.url).pathname);
    } catch {
      return true;
    }
  });
  return {
    consoleErrors: unexpectedConsole,
    pageErrors: state.pageErrors,
    requestFailures: state.requestFailures,
    httpErrors: unexpectedHttp,
  };
}

export async function auditLayout(page) {
  return page.evaluate(() => {
    const root = document.scrollingElement || document.documentElement;
    const horizontalOverflow = Math.max(0, root.scrollWidth - root.clientWidth);
    const visibleRect = (element) => {
      const style = getComputedStyle(element);
      const raw = element.getBoundingClientRect();
      if (style.visibility === "hidden" || style.display === "none" || raw.width <= 1 || raw.height <= 1) return null;
      let left = Math.max(0, raw.left);
      let right = Math.min(innerWidth, raw.right);
      let top = Math.max(0, raw.top);
      let bottom = Math.min(innerHeight, raw.bottom);
      for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
        const ancestorStyle = getComputedStyle(ancestor);
        const ancestorRect = ancestor.getBoundingClientRect();
        const clipLeft = ancestorRect.left + ancestor.clientLeft;
        const clipTop = ancestorRect.top + ancestor.clientTop;
        if (ancestorStyle.overflowX !== "visible") {
          left = Math.max(left, clipLeft);
          right = Math.min(right, clipLeft + ancestor.clientWidth);
        }
        if (ancestorStyle.overflowY !== "visible") {
          top = Math.max(top, clipTop);
          bottom = Math.min(bottom, clipTop + ancestor.clientHeight);
        }
      }
      const width = right - left;
      const height = bottom - top;
      return width > 1 && height > 1 ? { left, right, top, bottom, width, height } : null;
    };
    const candidates = Array.from(document.querySelectorAll("button, input, select, textarea, a[href]"))
      .map((element) => {
        const rect = visibleRect(element);
        return rect ? {
          element,
          rect,
          label: element.getAttribute("data-testid") || element.getAttribute("aria-label") || element.textContent?.trim().slice(0, 40) || element.tagName,
        } : null;
      })
      .filter(Boolean);
    const overlaps = [];
    for (let i = 0; i < candidates.length; i += 1) {
      for (let j = i + 1; j < candidates.length; j += 1) {
        const left = candidates[i];
        const right = candidates[j];
        if (left.element.contains(right.element) || right.element.contains(left.element)) continue;
        const width = Math.min(left.rect.right, right.rect.right) - Math.max(left.rect.left, right.rect.left);
        const height = Math.min(left.rect.bottom, right.rect.bottom) - Math.max(left.rect.top, right.rect.top);
        if (width <= 2 || height <= 2) continue;
        const intersection = width * height;
        const smaller = Math.min(left.rect.width * left.rect.height, right.rect.width * right.rect.height);
        if (intersection / smaller < 0.12) continue;
        overlaps.push({ left: left.label, right: right.label, width: Math.round(width), height: Math.round(height) });
      }
    }
    return { horizontalOverflow, overlaps };
  });
}

export async function screenshotAndAudit(page, screenshotDir, name) {
  await mkdir(screenshotDir, { recursive: true });
  const screenshotPath = path.join(screenshotDir, `${name}.png`);
  await page.screenshot({ path: screenshotPath, fullPage: true });
  const layout = await auditLayout(page);
  if (layout.horizontalOverflow > 1 || layout.overlaps.length) {
    throw new Error(`layout audit failed for ${name}: ${JSON.stringify(layout)}`);
  }
  return { name, screenshot: screenshotPath, ...layout };
}

export function assertNoForbiddenUiRequests(requests) {
  const forbidden = requests.filter((request) => {
    if (request.method === "PUT" && /\/execution$/.test(request.path)) return true;
    if (/\/improvements\/[^/]+\/lifecycle$/.test(request.path)) return true;
    if (request.method === "POST" && request.path === "/api/assets") {
      try { return JSON.parse(request.postData || "{}").asset_type === "test_dataset"; } catch { return false; }
    }
    return false;
  });
  if (forbidden.length) throw new Error(`forbidden stale UI requests observed: ${JSON.stringify(forbidden)}`);
}
