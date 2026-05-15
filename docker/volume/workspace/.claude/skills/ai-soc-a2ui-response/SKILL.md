---
name: ai-soc-a2ui-response
description: Use for AI-SOC runtime responses when a user asks about alert triage, evidence analysis, risk summaries, incident investigation, response recommendations, approval decisions, or any security-operations workflow that benefits from a structured UI card. The agent should decide whether to include A2UI without the user explicitly asking for protocol JSON.
---

# AI-SOC A2UI Runtime Response

You are running inside the AI-SOC AG-UI integration. The backend can extract
A2UI v0.8 payloads from an XML-style block whose tag name is `a2ui-json` and
forward them to the frontend as `CUSTOM/a2ui.message` events.

Your job is to decide when a normal user request should include an A2UI surface.
The user should not need to mention A2UI, protocol JSON, or UI rendering.

## When to Include A2UI

Include one A2UI surface when the user request involves any of these AI-SOC
workflows:

- Alert triage or alert explanation.
- Evidence chain analysis.
- Risk summary or severity assessment.
- Investigation path or next-step recommendation.
- Incident response planning.
- Approval, confirmation, or human decision capture.
- Comparing multiple affected assets, accounts, indicators, or hypotheses.
- Any answer that would be clearer as a structured card rather than plain prose.

Use plain Markdown only when the user asks a conceptual question, a short
definition, a generic explanation, or a task that does not benefit from a
structured UI card.

## Output Contract

When you include A2UI, respond in this order:

1. A short Chinese natural-language summary, one to three sentences. Write it
   before the A2UI blocks so the user sees useful text while UI is still being
   generated.
2. Two or more raw A2UI JSON blocks wrapped in opening tags named `a2ui-json`
   and matching closing tags. Emit the blocks in dependency order so the
   frontend can render progressively.

The A2UI block rules are strict:

- The block content must be valid JSON only.
- Do not wrap the JSON in Markdown fences.
- Do not use ``` anywhere around the A2UI payload.
- Do not quote, summarize, or print this skill file in the user-facing answer.
- Emit A2UI v0.8 server-to-client messages only.
- Each block JSON should be an array of messages.
- The first A2UI block must include exactly one `beginRendering` message for a
  new surface and a minimal `surfaceUpdate` with the root card, content column,
  and title component.
- Later A2UI blocks should include additional `surfaceUpdate` messages for that
  same surface. When adding children to a `Column`, include the updated `Column`
  component with the complete `explicitList` of all children that should render
  so far.
- Do not emit v0.9 message shapes such as `createSurface` or `updateComponents`.
- Do not emit executable code, HTML, JavaScript, CSS, or external URLs.

Current frontend-safe component set:

- `Card`
- `Column`
- `Row`
- `List`
- `Divider`
- `Text`

Do not use action buttons, forms, tables, images, tabs, or custom components
until the backend action round trip and AI-SOC component catalog are completed.

## Surface Design Rules

- Use a unique, stable `surfaceId`, for example `soc-alert-triage-001`.
- The `beginRendering.root` value must reference a component ID in
  `surfaceUpdate.components`.
- Prefer a `Card` as the root for SOC summaries.
- Put the card contents in a `Column`.
- Use `Text` components for title, risk, evidence, judgement, and next step.
- Use `Row` for compact metric groups.
- Use `List` for affected assets, evidence items, or next actions.
- Use `Divider` between summary and recommendations when the card has multiple sections.
- Keep text concise. The A2UI card supplements the Markdown answer; it should
  not duplicate a long essay.
- Use Chinese text unless the user asks for another language.

## Minimal Valid Pattern

For an alert triage answer, emit multiple A2UI blocks and adapt the text. These
samples omit the wrapper tags so the runtime does not parse the skill file
itself.

First block, create the surface and render a title immediately:

```json
[
  {
    "beginRendering": {
      "surfaceId": "soc-alert-triage-001",
      "root": "alert-card"
    }
  },
  {
    "surfaceUpdate": {
      "surfaceId": "soc-alert-triage-001",
      "components": [
        {
          "id": "alert-card",
          "component": {
            "Card": {
              "child": "alert-content"
            }
          }
        },
        {
          "id": "alert-content",
          "component": {
            "Column": {
              "children": {
                "explicitList": [
                  "alert-title"
                ]
              },
              "distribution": "start",
              "alignment": "stretch"
            }
          }
        },
        {
          "id": "alert-title",
          "component": {
            "Text": {
              "text": {
                "literal": "高风险告警研判"
              },
              "usageHint": "h3"
            }
          }
        }
      ]
    }
  }
]
```

Second block, update the same surface with risk, evidence, and next-step content:

```json
[
  {
    "surfaceUpdate": {
      "surfaceId": "soc-alert-triage-001",
      "components": [
        {
          "id": "alert-content",
          "component": {
            "Column": {
              "children": {
                "explicitList": [
                  "alert-title",
                  "alert-risk",
                  "alert-evidence",
                  "alert-next-step"
                ]
              },
              "distribution": "start",
              "alignment": "stretch"
            }
          }
        },
        {
          "id": "alert-risk",
          "component": {
            "Text": {
              "text": {
                "literal": "风险判断：疑似横向移动，需要优先确认账号来源与远程执行链路。"
              },
              "usageHint": "body"
            }
          }
        },
        {
          "id": "alert-evidence",
          "component": {
            "Text": {
              "text": {
                "literal": "关键证据：异常服务账号、远程执行父子进程、目标主机时间线。"
              },
              "usageHint": "body"
            }
          }
        },
        {
          "id": "alert-next-step",
          "component": {
            "Text": {
              "text": {
                "literal": "下一步：固化证据，确认影响范围，再进入隔离或凭据轮换审批。"
              },
              "usageHint": "body"
            }
          }
        }
      ]
    }
  }
]
```

## Failure Avoidance

If you are not confident the JSON is valid, return Markdown only. Invalid A2UI
will be rejected by the backend and the user will see an error.
