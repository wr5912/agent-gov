import { describe, expect, it } from "vitest";

import { artifactRequestKeys } from "./useImprovementWorkbenchData";

describe("artifactRequestKeys", () => {
  it("returns no requests for an empty presence projection", () => {
    expect(artifactRequestKeys({
      normalized_feedback: false,
      attribution: false,
      optimization_plan: false,
      execution: false,
      regression_test_design: false,
    })).toEqual([]);
  });

  it("uses presence independently for sparse historical artifacts", () => {
    expect(artifactRequestKeys({
      normalized_feedback: false,
      attribution: false,
      optimization_plan: false,
      execution: true,
      regression_test_design: true,
    })).toEqual(["execution", "regression_test_design"]);
  });

  it("returns every present artifact in the stable registry order", () => {
    expect(artifactRequestKeys({
      normalized_feedback: true,
      attribution: true,
      optimization_plan: true,
      execution: true,
      regression_test_design: true,
    })).toEqual([
      "normalized_feedback",
      "attribution",
      "optimization_plan",
      "execution",
      "regression_test_design",
    ]);
  });

  it("rejects a missing or malformed backend-owned projection", () => {
    expect(() => artifactRequestKeys(undefined)).toThrow("缺少 artifact_presence");
    expect(() => artifactRequestKeys({
      normalized_feedback: false,
      attribution: false,
      optimization_plan: false,
      execution: "false",
      regression_test_design: false,
    })).toThrow("artifact_presence.execution 必须是 boolean");
  });
});
