import { describe, expect, it } from "vitest";

import {
  deriveNativeAgentForm,
  nativeAgentFormValueAt,
  updateNativeAgentFormValue,
} from "./nativeAgentSchemaForm";

const nativeSchema = {
  type: "object",
  required: ["name", "context_config", "react_config"],
  properties: {
    name: { type: "string", title: "Agent name" },
    system_prompt: { type: "string", title: "System prompt", format: "textarea", default: "Help safely." },
    context_config: { $ref: "#/$defs/ContextConfig" },
    react_config: { $ref: "#/$defs/ReActConfig" },
    invite_config: { $ref: "#/$defs/InviteConfig" },
  },
  $defs: {
    ContextConfig: {
      type: "object",
      title: "Context",
      properties: {
        trigger_ratio: { type: "number", title: "Trigger ratio", default: 0.8, maximum: 0.9 },
        compression_tool_enabled: { type: "boolean", title: "Compression tool", default: false },
      },
    },
    ReActConfig: {
      type: "object",
      title: "ReAct",
      properties: {
        max_iters: { type: "integer", title: "Max iterations", default: 50, minimum: 1 },
      },
    },
    InviteConfig: {
      type: "object",
      title: "Invite",
      properties: {
        invitable: { type: "boolean", title: "Invitable", default: false },
        invite_description: {
          anyOf: [{ type: "string" }, { type: "null" }],
          title: "Invite description",
          format: "textarea",
        },
      },
    },
  },
};

describe("AgentScope schema-driven candidate form", () => {
  it("derives editable sections and defaults from the native schema", () => {
    const definition = deriveNativeAgentForm(nativeSchema);

    expect(definition.fields.map((field) => field.key)).toEqual(["name", "system_prompt"]);
    expect(definition.sections.map((section) => section.key)).toEqual([
      "context_config",
      "react_config",
      "invite_config",
    ]);
    expect(definition.initialValue).toEqual({
      name: "",
      system_prompt: "Help safely.",
      context_config: { trigger_ratio: 0.8, compression_tool_enabled: false },
      react_config: { max_iters: 50 },
      invite_config: { invitable: false, invite_description: "" },
    });
  });

  it("updates one nested field without mutating sibling native fields", () => {
    const initial = deriveNativeAgentForm(nativeSchema).initialValue;
    const updated = updateNativeAgentFormValue(initial, ["react_config", "max_iters"], 12);

    expect(nativeAgentFormValueAt(updated, ["react_config", "max_iters"])).toBe(12);
    expect(nativeAgentFormValueAt(updated, ["context_config", "trigger_ratio"])).toBe(0.8);
    expect(nativeAgentFormValueAt(initial, ["react_config", "max_iters"])).toBe(50);
  });

  it("fails closed when AgentScope adds an unmapped top-level field", () => {
    expect(() => deriveNativeAgentForm({
      ...nativeSchema,
      properties: { ...nativeSchema.properties, credential_id: { type: "string" } },
    })).toThrow("可编辑字段已变化");
  });
});
