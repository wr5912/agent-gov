import { describe, expect, it } from "vitest";

import { AGENT_ID_MAX_LENGTH, validateAgentId } from "./agentSettingsValidation";

describe("validateAgentId", () => {
  it("accepts the central 128-character boundary", () => {
    expect(validateAgentId("a".repeat(AGENT_ID_MAX_LENGTH))).toBeUndefined();
  });

  it("rejects an Agent id above the central boundary", () => {
    expect(validateAgentId("a".repeat(AGENT_ID_MAX_LENGTH + 1))).toBe(
      `Agent ID 最多 ${AGENT_ID_MAX_LENGTH} 个字符。`,
    );
  });

  it("keeps traversal-like path segments invalid", () => {
    expect(validateAgentId(".")).toBeDefined();
    expect(validateAgentId("..")).toBeDefined();
    expect(validateAgentId("agent/escape")).toBeDefined();
  });
});
