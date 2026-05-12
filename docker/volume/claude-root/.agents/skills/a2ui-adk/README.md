# a2ui-adk

An agent skill for building [Google ADK](https://google.github.io/adk-docs/) agents with [A2UI](https://github.com/google/A2UI) — the declarative JSON format that lets agents generate rich, interactive UIs. Includes a **Tailwind CSS + shadcn/ui** design system for creating beautiful, modern components.

Works with **Claude Code**, **Cursor**, **Windsurf**, **Cline**, and any AI coding agent that supports the skills ecosystem.

## Install

```bash
npx skills add coolxeo/a2ui-adk
```

That's it. The skill is now available in your agent sessions.

## What it does

When you're building an ADK agent that needs to render dynamic UI (cards, forms, lists, dashboards, interactive elements), this skill gives your AI coding agent the full context on:

- **A2UI + ADK patterns** — `A2uiSchemaManager`, `LlmAgent`, streaming with validation/retry, A2A server hosting
- **Tailwind + shadcn/ui design system** — theme tokens, typography hierarchy, layout rules, component styling guidelines
- **Custom catalog creation** — define your own shadcn-inspired A2UI component catalog with Badge, Avatar, Alert, Progress, and more
- **Beautiful example templates** — contact cards, dashboard metrics, booking forms — all following shadcn/ui aesthetics
- **Orchestrator pattern** — multi-agent UI routing, surface-to-subagent mapping, part converters
- **A2A transport** — agent cards with A2UI extensions, `DataPart` format, user action handling
- **Response parsing** — `<a2ui-json>` tag extraction, JSON validation against catalog schemas

## Usage

Ask your AI coding agent to build an A2UI agent:

```
> Build me an ADK agent that shows a product catalog with cards and a booking form
> Add A2UI support to my existing ADK agent
> Create a shadcn-styled dashboard agent with metrics cards
> Create an orchestrator that routes to multiple A2UI sub-agents
```

The skill triggers automatically when you're working with A2UI + ADK.

## What's inside

| Section | Description |
|---------|-------------|
| Architecture | A2UI flow, transport, rendering pipeline |
| Pattern 1: Single Agent | Full agent setup with schema manager, executor, server |
| Pattern 2: Orchestrator | Multi-agent routing with surface-to-subagent mapping |
| Design System | Tailwind + shadcn/ui tokens, layout rules, typography |
| Custom Catalog | Full shadcn-inspired component catalog JSON schema |
| Examples | Contact card, dashboard metrics, booking form templates |

## Keywords

a2ui, adk, google-adk, agent-to-user-interface, declarative-ui, server-driven-ui, a2a-protocol, tailwind, shadcn, shadcn-ui, genui, agent-ui, google-agent-development-kit, llm-ui, agentic-ui

## Uninstall

Remove the skill directory:

```bash
rm -rf ~/.claude/skills/a2ui-adk
```
