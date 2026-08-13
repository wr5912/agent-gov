import { afterEach, describe, expect, it, vi } from "vitest";

import type { RuntimeClientConfig } from "../types/runtime";
import {
  deleteBusinessAgent,
  getBusinessAgentDeletionOperation,
  listBusinessAgentDeletionOperations,
} from "./runtime";

const config: RuntimeClientConfig = { apiBase: "http://runtime.test", apiKey: "" };

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("deleteBusinessAgent", () => {
  it("uses a strong quoted instance ETag and stable idempotency key", verifyDeleteHeaders);
  it("reads a durable deletion operation by its opaque operation id", verifyDeletionOperationLookup);
  it("discovers bounded pending deletion receipts for UI recovery", verifyPendingDeletionDiscovery);
});

async function verifyDeleteHeaders() {
  const instanceEtag = "a".repeat(64);
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    operation_id: "adop-test",
    state: "completed",
    deleted: {
      agent_id: "delete-agent",
      name: "Delete agent",
      category: "business",
      created_at: "2026-08-09T00:00:00Z",
      status: "active",
      builtin: false,
      default: false,
      protected: false,
      requires_web_hitl: false,
    },
    impact: {
      runs: 0,
      feedback_signals: 0,
      improvements: 0,
      test_runs: 0,
      change_sets: 0,
      releases: 0,
    },
    workspace_removed: true,
    cleanup_complete: true,
    last_error_code: null,
    attempt_count: 1,
    updated_at: "2026-08-09T00:00:01Z",
  }), { status: 200, headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);

  await deleteBusinessAgent(config, "delete-agent", instanceEtag);

  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toBe("http://runtime.test/api/agent-registry/delete-agent");
  expect(init.method).toBe("DELETE");
  expect(init.headers).toMatchObject({
    "If-Match": `"${instanceEtag}"`,
    "Idempotency-Key": `agent-delete:${instanceEtag}`,
  });
}

async function verifyDeletionOperationLookup() {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    operation_id: "adop-status",
    state: "cleanup_pending",
    deleted: {
      agent_id: "delete-agent",
      name: "Delete agent",
      category: "business",
      created_at: "2026-08-09T00:00:00Z",
      status: "active",
      builtin: false,
      default: false,
      protected: false,
      requires_web_hitl: false,
    },
    impact: { runs: 0, feedback_signals: 0, improvements: 0, test_runs: 0, change_sets: 0, releases: 0 },
    workspace_removed: false,
    cleanup_complete: false,
    last_error_code: "AGENT_DELETION_FILESYSTEM_FENCE",
    attempt_count: 2,
    updated_at: "2026-08-09T00:00:02Z",
  }), { status: 200, headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);

  await getBusinessAgentDeletionOperation(config, "adop/status");

  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(fetchMock.mock.calls[0]?.[0]).toBe(
    "http://runtime.test/api/agent-deletion-operations/adop%2Fstatus",
  );
}

async function verifyPendingDeletionDiscovery() {
  const fetchMock = vi.fn().mockResolvedValue(new Response("[]", {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }));
  vi.stubGlobal("fetch", fetchMock);

  await listBusinessAgentDeletionOperations(config, "cleanup_pending", 7);

  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(fetchMock.mock.calls[0]?.[0]).toBe(
    "http://runtime.test/api/agent-deletion-operations?state=cleanup_pending&limit=7",
  );
}
