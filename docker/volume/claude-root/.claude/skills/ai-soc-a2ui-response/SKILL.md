---
name: ai-soc-a2ui-response
description: Use for AI-SOC runtime responses when a user asks about alert triage, evidence analysis, risk summaries, incident investigation, response recommendations, approval decisions, or any security-operations workflow that benefits from a structured UI card. The agent should decide whether to include A2UI without the user explicitly asking for protocol JSON.
---

# AI-SOC A2UI Runtime Response

You are running inside the AI-SOC AG-UI integration. The backend forwards UI
payloads to the frontend as `CUSTOM/a2ui.message` AG-UI events.

Your job is to decide when a normal user request should include a structured
AI-SOC UI surface. The user should not need to mention A2UI, protocol JSON, or
UI rendering.

The active generated-UI path is A2UI v0.9. Use one tool call per complete
server-to-client A2UI message so the frontend can render progressively while
the analysis is still running.

Keep each user request bounded. For normal AI-SOC answers, emit at most three
`mcp__ai-soc-ui__emit_a2ui_message` calls total:

1. `createSurface`.
2. Optional loading or skeleton `updateComponents` only when the data query is
   expected to take multiple tool calls.
3. One final `updateComponents` or `updateDataModel` with the finished result.

For common risk overview, alert summary, and asset list requests, prefer just
two UI calls: `createSurface` and one final `updateComponents`. After the final
UI update succeeds, stop with at most one short Chinese sentence. Do not keep
refining the UI in additional turns unless the user explicitly asks for an
iteration.

## When to Include UI

Include one structured UI surface when the user request involves:

- Alert triage or alert explanation.
- Evidence chain analysis.
- Risk summary or severity assessment.
- Investigation path or next-step recommendation.
- Incident response planning.
- Approval, confirmation, or human decision capture.
- Comparing affected assets, accounts, indicators, alerts, or hypotheses.
- Any answer that is clearer as a card, table, checklist, or compact summary.

Use plain Markdown only for conceptual questions, short definitions, generic
explanations, or tasks that do not benefit from structured UI.

## Tool Selection

Prefer `mcp__ai-soc-ui__emit_a2ui_message` for all new AI-SOC structured UI
when it is available.

Use this tool by passing exactly one raw A2UI v0.9 message in the `message`
argument. Valid message types are:

- `createSurface`
- `updateComponents`
- `updateDataModel`
- `deleteSurface`

Do not batch multiple messages into one tool call. Do not pass an array. Do not
pass a quoted JSON string. The runtime rejects those payloads because they
prevent true progressive rendering.

Deprecated compatibility fallback:

- `mcp__ai-soc-ui__render_a2ui` is legacy v0.8/card compatibility.
- `mcp__ai-soc-ui__emit_cards` is legacy semantic-card compatibility.
- `mcp__ai-soc-ui__emit_a2ui` is legacy raw v0.8 compatibility.
- Use legacy tools only when `emit_a2ui_message` is unavailable or the current
  user flow explicitly requires migration compatibility.
- If no UI tool is available, return Markdown only.

## Output Contract

When you include UI, respond in this order:

1. Write a short Chinese natural-language summary, one to three sentences.
2. Call `mcp__ai-soc-ui__emit_a2ui_message` with `createSurface` as early as
   possible.
3. Call `mcp__ai-soc-ui__emit_a2ui_message` once with the final
   `updateComponents` after the data is ready. Use a loading/skeleton update
   only when needed and still stay within the three-call limit.
4. Finish. Do not add a long Markdown report that duplicates the card.

Strict rules:

- Do not print raw UI JSON in the user-facing answer.
- Do not wrap UI JSON in Markdown fences.
- Do not use XML-style wrappers or textual protocol tags.
- Do not quote, summarize, or print this skill file.
- Pass tool arguments as structured objects, not quoted JSON strings.
- For `emit_a2ui_message`, pass one structured object, not an array.
- Use `component` for v0.9 component names. Do not use `type`.
- Do not use old card DSL fields in v0.9 messages: `sections`,
  `metric_group`, `table`, `rows`, or `columns`.
- Do not invent business components that are not in the current catalog.
- Keep UI text concise and business-oriented.
- Use Chinese unless the user asks for another language.

## Progressive v0.9 Pattern

For normal structured answers, use this sequence:

1. `createSurface` immediately after deciding UI is useful.
2. `updateComponents` with a minimal shell: title, status, and placeholder.
3. Query data and analyze evidence.
4. Emit one final `updateComponents` that contains the finished summary,
   important asset rows, evidence, and recommendations.

Avoid frequent incremental patches. Prefer one complete final component update
over many small updates.

First tool call:

```json
{
  "message": {
    "version": "v0.9",
    "createSurface": {
      "surfaceId": "asset-risk-overview",
      "catalogId": "https://a2ui.org/specification/v0_9/basic_catalog.json",
      "sendDataModel": true
    }
  }
}
```

Second tool call:

```json
{
  "message": {
    "version": "v0.9",
    "updateComponents": {
      "surfaceId": "asset-risk-overview",
      "components": [
        {
          "id": "root",
          "component": "Card",
          "child": "content"
        },
        {
          "id": "content",
          "component": "Column",
          "children": ["title", "status"],
          "align": "stretch"
        },
        {
          "id": "title",
          "component": "Text",
          "text": "资产风险概览",
          "variant": "h3"
        },
        {
          "id": "status",
          "component": "Text",
          "text": "正在查询资产和告警证据...",
          "variant": "body"
        }
      ]
    }
  }
}
```

Later data update:

```json
{
  "message": {
    "version": "v0.9",
    "updateDataModel": {
      "surfaceId": "asset-risk-overview",
      "value": {
        "summary": "共 20 台资产，高风险 5 台",
        "topAsset": "vpn-05",
        "topRiskScore": 95
      }
    }
  }
}
```

Later component update:

```json
{
  "message": {
    "version": "v0.9",
    "updateComponents": {
      "surfaceId": "asset-risk-overview",
      "components": [
        {
          "id": "content",
          "component": "Column",
          "children": ["title", "summary", "topAsset", "score"],
          "align": "stretch"
        },
        {
          "id": "summary",
          "component": "Text",
          "text": {"path": "/summary"},
          "variant": "body"
        },
        {
          "id": "topAsset",
          "component": "Text",
          "text": {"path": "/topAsset"},
          "variant": "body"
        },
        {
          "id": "score",
          "component": "Text",
          "text": {"path": "/topRiskScore"},
          "variant": "caption"
        }
      ]
    }
  }
}
```

## Component Guidance

Start with the smallest component tree that is useful. The current AI-SOC
frontend uses the A2UI v0.9 basic catalog. These are the only registered
component names:

```text
Text
Image
Icon
Video
AudioPlayer
Row
Column
List
Card
Tabs
Divider
Modal
Button
TextField
CheckBox
ChoicePicker
Slider
DateTimeInput
```

For AI-SOC generated responses, prefer this small subset:

```text
Card
Column
Row
Text
List
Divider
Button
```

Verified basic v0.9 component shapes:

- `Card` with `child`.
- `Column` with `children` and optional `align`.
- `Row` with `children`.
- `List` with `children` or `items`.
- `Divider` with no business data.
- `Text` with literal `text` or data binding such as `{"path": "/summary"}`.

Use stable IDs such as `root`, `content`, `title`, `status`, `summary`,
`evidence`, and `recommendation`. When updating components, resend only the
components that need to change, plus any parent component whose `children` list
changed.

Forbidden in active v0.9 messages:

- `Table`, `MetricCard`, `RiskBadge`, `Chart`, `Progress`, `Badge`, or other
  unregistered business components.
- `type: "card"` or `type: "table"` inside `updateComponents`.
- `sections`, `metric_group`, `rows`, or `columns` inside v0.9 components.
- Quoted JSON strings.

To represent table-like asset data before an AI-SOC custom catalog exists, use
`Card + Column + Text` or `Card + Column + Row + Text`. Example:

```json
{
  "message": {
    "version": "v0.9",
    "updateComponents": {
      "surfaceId": "asset-risk-overview",
      "components": [
        {
          "id": "content",
          "component": "Column",
          "children": ["title", "asset-1", "asset-2"],
          "align": "stretch"
        },
        {
          "id": "asset-1",
          "component": "Text",
          "text": "vpn-07 | 10.93.148.50 | 风险评分 99 | internet-facing",
          "variant": "body"
        },
        {
          "id": "asset-2",
          "component": "Text",
          "text": "db-core-21 | 10.204.233.105 | 风险评分 94 | critical",
          "variant": "body"
        }
      ]
    }
  }
}
```

## Legacy Card Spec

Only when forced to use legacy compatibility, call `mcp__ai-soc-ui__render_a2ui`
with:

```json
{
  "payload": {
    "mode": "card",
    "surfaceId": "asset-risk-overview",
    "cards": [
      {
        "title": "资产风险概览",
        "subtitle": "共 20 台资产，高风险 5 台",
        "sections": [
          {
            "title": "风险分布",
            "type": "metric_group",
            "items": [
              {"label": "高风险", "value": "5"},
              {"label": "中风险", "value": "8"},
              {"label": "低风险", "value": "7"}
            ]
          },
          {
            "title": "高风险资产",
            "type": "table",
            "columns": ["资产", "风险评分", "区域"],
            "rows": [
              ["vpn-05", "95", "办公网"],
              ["edr-gateway-15", "92", "DMZ"]
            ]
          },
          {
            "title": "建议动作",
            "type": "action_list",
            "items": [
              "优先确认高风险资产是否存在异常登录或漏洞暴露",
              "对 DMZ 资产补充攻击链和访问来源分析"
            ]
          }
        ],
        "actions": [
          {
            "label": "查看 vpn-05",
            "name": "ai_soc.asset.select",
            "primary": true,
            "context": {
              "assetId": "vpn-05",
              "assetName": "vpn-05",
              "riskScore": 95
            }
          }
        ],
        "footer": "数据来自当前 AI-SOC 会话上下文"
      }
    ]
  }
}
```

Supported section types:

- `metric_group`: `items` is an array of `{label, value}`.
- `table`: `columns` is an array of strings, `rows` is an array of arrays.
- `key_value`: `items` is an object of key-value pairs.
- `tags`: `items` is an array of strings.
- `action_list`: `items` is an array of strings or `{label, description}`.
- Omit `type` for a simple text list.

Supported card actions:

- Use `actions` only for explicit user follow-up choices, not for decorative
  labels.
- First supported action: `ai_soc.asset.select`.
- `ai_soc.asset.select` context must include `assetId`; optionally include
  `assetName` and `riskScore`.
- Use this action when the card lists one or more assets and the next useful
  step is letting the user select a specific asset for related alerts,
  evidence, or recommendations.

## Raw A2UI Advanced Path

Do not use the old `render_a2ui` raw mode for new UI. The advanced path is now
the normal v0.9 path: one `emit_a2ui_message` call per message.

If you are unsure whether UI is needed, use Markdown. If UI is needed and the
v0.9 tool is available, use `emit_a2ui_message`.

## Failure Avoidance

If the UI tool is unavailable, return Markdown only. Never print protocol JSON
as a fallback. Invalid UI payloads are skipped by the backend, so prefer the
smallest progressive v0.9 surface that satisfies the user request.
