import { describe, expect, it } from "vitest";
import {
  claimContinuationSubmission,
  releaseContinuationSubmission,
} from "./playgroundContinuationSubmission";

describe("Playground continuation submission fence", () => {
  it("claims synchronously before async work and rejects a second decision for the same card", () => {
    const fences = new Set<string>();

    const claimed = claimContinuationSubmission(fences, "human", "pending-action-1");

    expect(claimed).toBe("human:pending-action-1");
    expect(claimContinuationSubmission(fences, "human", "pending-action-1")).toBeUndefined();
    expect(claimContinuationSubmission(fences, "external", "pending-action-1"))
      .toBe("external:pending-action-1");
  });

  it("permits retry only after the exact fence is released", () => {
    const fences = new Set<string>();
    const claimed = claimContinuationSubmission(fences, "external", "external-1");
    expect(claimed).toBeDefined();

    releaseContinuationSubmission(fences, claimed!);

    expect(claimContinuationSubmission(fences, "external", "external-1"))
      .toBe("external:external-1");
  });
});
