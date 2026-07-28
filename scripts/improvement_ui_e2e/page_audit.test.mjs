import assert from "node:assert/strict";
import test from "node:test";

import { unexpectedDiagnostics } from "./page_audit.mjs";

function diagnostics(httpErrors = []) {
  return {
    consoleErrors: [],
    pageErrors: [],
    requestFailures: [],
    httpErrors,
    requests: [],
  };
}

const controlledFailure = {
  method: "POST",
  origin: "http://runtime.test",
  path: "/api/improvements",
  status: 422,
};

test("expected HTTP errors are consumed by exact count", () => {
  const result = unexpectedDiagnostics(
    diagnostics([controlledFailure]),
    [{ method: "POST", path: "/api/improvements", status: 422, count: 1 }],
  );

  assert.deepEqual(result.httpErrors, []);
  assert.deepEqual(result.missingExpectedHttpErrors, []);
});

test("duplicate HTTP errors are not hidden by a one-shot expectation", () => {
  const result = unexpectedDiagnostics(
    diagnostics([controlledFailure, controlledFailure]),
    [{ method: "POST", path: "/api/improvements", status: 422, count: 1 }],
  );

  assert.deepEqual(result.httpErrors, [controlledFailure]);
  assert.deepEqual(result.missingExpectedHttpErrors, []);
});

test("missing controlled failures and optional artifact 404s both fail the audit", () => {
  const optionalArtifact404 = {
    method: "GET",
    origin: "http://runtime.test",
    path: "/api/improvements/imp-1/execution",
    status: 404,
  };
  const result = unexpectedDiagnostics(
    diagnostics([optionalArtifact404]),
    [{ method: "POST", path: "/api/improvements", status: 422, count: 1 }],
  );

  assert.deepEqual(result.httpErrors, [optionalArtifact404]);
  assert.deepEqual(result.missingExpectedHttpErrors, [{
    method: "POST",
    path: "/api/improvements",
    status: 422,
    count: 1,
    missing: 1,
  }]);
});
