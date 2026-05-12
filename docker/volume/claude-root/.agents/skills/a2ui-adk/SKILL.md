---
name: a2ui-adk
description: Build agents that generate rich, interactive UIs using Google's A2UI (Agent-to-User Interface) format with Google ADK (Agent Development Kit). Use when building ADK agents that need to render dynamic UI components, forms, cards, lists, or interactive elements via the A2UI declarative JSON format. Includes a Tailwind CSS + shadcn/ui design system flavor for creating beautiful, modern A2UI catalogs and components. Covers A2UI schema management, catalog configuration, A2A transport integration, response parsing/validation, custom catalog creation, and the orchestrator pattern for multi-agent UI routing.
---

# A2UI + ADK Skill

Build Google ADK agents that generate rich, interactive UIs using the A2UI declarative JSON format, styled with Tailwind CSS and shadcn/ui design principles.

## What is A2UI?

**A2UI (Agent-to-User Interface)** is an open standard (Apache 2.0, github.com/google/A2UI) that lets agents "speak UI." Agents send declarative JSON describing UI intent; client apps render it using native components (Flutter, Angular, Lit, React, etc.).

Core principles:
- **Security first**: Declarative data format, not executable code. Clients maintain a catalog of trusted, pre-approved UI components.
- **LLM-friendly**: Flat list of components with ID references, easy for LLMs to generate incrementally.
- **Framework-agnostic**: Same JSON payload renders on any supported client framework.
- **Currently v0.8 (Public Preview)**, v0.9 also available.

## Architecture Overview

```
Agent (ADK + A2UI SDK) → A2UI JSON Response → Transport (A2A/AG UI) → Client Renderer (Lit/Flutter/etc.)
```

1. **Generation**: Agent uses LLM to generate A2UI JSON payload
2. **Transport**: Sent via A2A protocol or AG UI
3. **Resolution**: Client's A2UI Renderer parses the JSON
4. **Rendering**: Maps abstract components to native widgets

## Key Dependencies

```toml
[project]
dependencies = [
    "a2a-sdk>=0.3.0",
    "google-adk>=1.8.0",
    "google-genai>=1.27.0",
    "litellm",
    "jsonschema>=4.0.0",
    "a2ui-agent",           # The A2UI agent SDK (from agent_sdks/python in the A2UI repo)
    "python-dotenv>=1.1.0",
    "click>=8.1.8",
]
```

The `a2ui-agent` package is sourced from the A2UI repo at `agent_sdks/python` (use `uv` workspace source):
```toml
[tool.uv.sources]
a2ui-agent = { path = "../../../agent_sdks/python", editable = true }
```

## Core A2UI SDK Imports

```python
# Schema management
from a2ui.core.schema.constants import VERSION_0_8, VERSION_0_9, A2UI_OPEN_TAG, A2UI_CLOSE_TAG
from a2ui.core.schema.manager import A2uiSchemaManager
from a2ui.core.schema.common_modifiers import remove_strict_validation

# Catalog (built-in component catalog)
from a2ui.basic_catalog.provider import BasicCatalog

# Response parsing
from a2ui.core.parser.parser import parse_response, ResponsePart

# A2A integration helpers
from a2ui.a2a import (
    create_a2ui_part,        # Wrap A2UI JSON as A2A DataPart
    is_a2ui_part,            # Check if an A2A Part contains A2UI data
    get_a2ui_agent_extension,# Create AgentExtension for agent card
    parse_response_to_parts, # Parse LLM response → list of A2A Parts
    try_activate_a2ui_extension, # Activate A2UI extension from request context
    A2UI_EXTENSION_URI,
)
```

## Pattern 1: Single Agent with A2UI (e.g., Restaurant Finder)

### Step 1: Schema Manager Setup

```python
from a2ui.core.schema.manager import A2uiSchemaManager
from a2ui.core.schema.constants import VERSION_0_8
from a2ui.basic_catalog.provider import BasicCatalog
from a2ui.core.schema.common_modifiers import remove_strict_validation

schema_manager = A2uiSchemaManager(
    VERSION_0_8,
    catalogs=[
        BasicCatalog.get_config(version=VERSION_0_8, examples_path="examples")
    ],
    schema_modifiers=[remove_strict_validation],
)
```

- `examples_path` points to a directory of JSON example files used for few-shot prompting
- `remove_strict_validation` strips `additionalProperties: false` to make LLM generation easier

### Step 2: Build the ADK LlmAgent

```python
from google.adk.agents.llm_agent import LlmAgent
from google.adk.models.lite_llm import LiteLlm

ROLE_DESCRIPTION = "You are a helpful assistant. Your final output MUST be an A2UI UI JSON response."
UI_DESCRIPTION = "Describe when to use which template/layout..."

instruction = schema_manager.generate_system_prompt(
    role_description=ROLE_DESCRIPTION,
    ui_description=UI_DESCRIPTION,
    include_schema=True,       # Injects the JSON schema into the prompt
    include_examples=True,     # Injects example A2UI payloads
    validate_examples=True,    # Validates examples against schema at startup
)

agent = LlmAgent(
    model=LiteLlm(model="gemini/gemini-2.5-flash"),
    name="my_agent",
    description="Agent description",
    instruction=instruction,
    tools=[my_tool_function],
)
```

### Step 3: Agent Class with Streaming + Validation

```python
from google.adk.runners import Runner
from google.adk.artifacts import InMemoryArtifactService
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.sessions import InMemorySessionService
from google.genai import types
from a2ui.core.parser.parser import parse_response
from a2ui.a2a import parse_response_to_parts

class MyAgent:
    def __init__(self, base_url: str, use_ui: bool = False):
        self.use_ui = use_ui
        self._schema_manager = A2uiSchemaManager(...) if use_ui else None
        self._agent = self._build_agent(use_ui)
        self._runner = Runner(
            app_name=self._agent.name,
            agent=self._agent,
            artifact_service=InMemoryArtifactService(),
            session_service=InMemorySessionService(),
            memory_service=InMemoryMemoryService(),
        )

    async def stream(self, query, session_id):
        # Create/get session
        session = await self._runner.session_service.get_session(...)
        if session is None:
            session = await self._runner.session_service.create_session(...)

        # Run agent with retry on validation failure
        max_retries = 1
        for attempt in range(max_retries + 1):
            message = types.Content(role="user", parts=[types.Part.from_text(text=query)])
            final_response = None

            async for event in self._runner.run_async(
                user_id=self._user_id, session_id=session.id, new_message=message
            ):
                if event.is_final_response():
                    final_response = "\n".join(
                        [p.text for p in event.content.parts if p.text]
                    )
                    break
                else:
                    yield {"is_task_complete": False, "updates": "Processing..."}

            # Validate A2UI JSON if using UI
            if self.use_ui and final_response:
                try:
                    response_parts = parse_response(final_response)
                    for part in response_parts:
                        if part.a2ui_json:
                            selected_catalog = self._schema_manager.get_selected_catalog()
                            selected_catalog.validator.validate(part.a2ui_json)
                    # Valid! Send final response
                    yield {
                        "is_task_complete": True,
                        "parts": parse_response_to_parts(final_response, fallback_text="OK."),
                    }
                    return
                except Exception as e:
                    if attempt < max_retries:
                        query = f"Previous response invalid: {e}. Retry with valid A2UI JSON."
                        continue
                    # Exhausted retries, send error
                    yield {"is_task_complete": True, "parts": [Part(root=TextPart(text="Error generating UI."))]}
                    return
            else:
                yield {"is_task_complete": True, "parts": parse_response_to_parts(final_response)}
                return
```

### Step 4: Agent Executor (A2A Server)

The executor bridges A2A protocol to your agent. Key pattern: maintain both a UI agent and a text-only agent, selecting based on whether the A2UI extension is active.

```python
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import DataPart, Part, TaskState, TextPart
from a2a.utils import new_agent_parts_message, new_agent_text_message, new_task
from a2ui.a2a import try_activate_a2ui_extension

class MyAgentExecutor(AgentExecutor):
    def __init__(self, ui_agent, text_agent):
        self.ui_agent = ui_agent
        self.text_agent = text_agent

    async def execute(self, context: RequestContext, event_queue: EventQueue):
        use_ui = try_activate_a2ui_extension(context)
        agent = self.ui_agent if use_ui else self.text_agent

        # Extract query from message parts
        query = context.get_user_input()

        # Handle A2UI user actions (button clicks, form submissions)
        for part in (context.message.parts or []):
            if isinstance(part.root, DataPart) and "userAction" in part.root.data:
                ui_event = part.root.data["userAction"]
                action = ui_event.get("actionName")
                ctx = ui_event.get("context", {})
                query = f"User action: {action} with data: {ctx}"
                break

        task = context.current_task or new_task(context.message)
        if not context.current_task:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        async for item in agent.stream(query, task.context_id):
            if not item["is_task_complete"]:
                await updater.update_status(
                    TaskState.working,
                    new_agent_text_message(item["updates"], task.context_id, task.id),
                )
            else:
                await updater.update_status(
                    TaskState.input_required,  # or TaskState.completed
                    new_agent_parts_message(item["parts"], task.context_id, task.id),
                    final=True,
                )
                break
```

### Step 5: Server Entry Point (__main__.py)

```python
import click, os, logging, uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware

load_dotenv()

@click.command()
@click.option("--host", default="localhost")
@click.option("--port", default=10002)
def main(host, port):
    base_url = f"http://{host}:{port}"
    ui_agent = MyAgent(base_url=base_url, use_ui=True)
    text_agent = MyAgent(base_url=base_url, use_ui=False)

    executor = MyAgentExecutor(ui_agent, text_agent)
    handler = DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore())
    server = A2AStarletteApplication(agent_card=ui_agent.get_agent_card(), http_handler=handler)

    app = server.build()
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"http://localhost:\d+",
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    uvicorn.run(app, host=host, port=port)

if __name__ == "__main__":
    main()
```

### Step 6: Agent Card with A2UI Extension

```python
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2ui.a2a import get_a2ui_agent_extension

def get_agent_card(self) -> AgentCard:
    return AgentCard(
        name="My Agent",
        description="Agent description",
        url=self.base_url,
        version="1.0.0",
        default_input_modes=["text", "text/plain"],
        default_output_modes=["text", "text/plain"],
        capabilities=AgentCapabilities(
            streaming=True,
            extensions=[
                get_a2ui_agent_extension(
                    self._schema_manager.accepts_inline_catalogs,
                    self._schema_manager.supported_catalog_ids,
                )
            ],
        ),
        skills=[AgentSkill(id="my_skill", name="My Skill", description="...", tags=["tag"], examples=["example query"])],
    )
```

## Pattern 2: Orchestrator with Multi-Agent UI Routing

For orchestrating multiple sub-agents that each produce A2UI, use:

### Remote Sub-Agent Discovery

```python
from a2a.client import A2ACardResolver
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.agents.llm_agent import LlmAgent
from google.adk.planners.built_in_planner import BuiltInPlanner

# Discover sub-agents via A2A card resolution
async with httpx.AsyncClient() as client:
    resolver = A2ACardResolver(httpx_client=client, base_url=subagent_url)
    subagent_card = await resolver.get_agent_card()

# Create remote agent wrapper
remote_agent = RemoteA2aAgent(
    name, subagent_card,
    description=json.dumps({...}),
    a2a_part_converter=convert_a2a_part_to_genai_part,
    genai_part_converter=convert_genai_part_to_a2a_part,
    a2a_client_factory=A2AClientFactoryWithA2UIMetadata(...)
)

# Build orchestrator
orchestrator = LlmAgent(
    model=LiteLlm(model="gemini/gemini-2.5-flash"),
    name="orchestrator_agent",
    instruction="Route tasks to the appropriate subagent.",
    sub_agents=[remote_agent_1, remote_agent_2],
    planner=BuiltInPlanner(thinking_config=genai_types.ThinkingConfig(include_thoughts=True)),
    before_model_callback=programmatic_route_user_action,  # Route UI actions to correct sub-agent
)
```

### Part Converters (A2A <-> GenAI)

When A2UI parts pass through the orchestrator, they need conversion between A2A and GenAI formats:

```python
from a2ui.a2a import is_a2ui_part

def convert_a2a_part_to_genai_part(a2a_part):
    """Serialize A2UI DataParts to text for LLM context."""
    if is_a2ui_part(a2a_part):
        return genai_types.Part(text=a2a_part.model_dump_json())
    return part_converter.convert_a2a_part_to_genai_part(a2a_part)

def convert_genai_part_to_a2a_part(part):
    """Deserialize A2UI text back to DataParts."""
    if part.text:
        try:
            a2a_part = a2a_types.Part.model_validate_json(part.text)
            if is_a2ui_part(a2a_part):
                return a2a_part
        except pydantic.ValidationError:
            pass
    return part_converter.convert_genai_part_to_a2a_part(part)
```

### Surface-to-Subagent Routing

Route user actions to the correct sub-agent based on `surfaceId`:

```python
# When a sub-agent sends a beginRendering with a surfaceId, save the mapping
if begin_rendering := a2ui_data.get("beginRendering"):
    surface_id = begin_rendering.get("surfaceId")
    SubagentRouteManager.set_route_to_subagent_name(surface_id, agent_name, ...)

# When a userAction arrives, look up which sub-agent owns that surface
if user_action := a2ui_data.get("userAction"):
    surface_id = user_action.get("surfaceId")
    target_agent = SubagentRouteManager.get_route_to_subagent_name(surface_id, state)
    # Programmatically route via transfer_to_agent function call
```

### A2UI Metadata Interceptor

Pass A2UI extension headers and client capabilities to remote agents:

```python
from a2a.client.middleware import ClientCallInterceptor
from a2a.extensions.common import HTTP_EXTENSION_HEADER

class A2UIMetadataInterceptor(ClientCallInterceptor):
    async def intercept(self, method_name, request_payload, http_kwargs, agent_card, context):
        if context and context.state and context.state.get("use_ui"):
            http_kwargs["headers"] = {HTTP_EXTENSION_HEADER: A2UI_EXTENSION_URI}
            # Add client capabilities to message metadata
            if (params := request_payload.get("params")) and (message := params.get("message")):
                message.setdefault("metadata", {})[A2UI_CLIENT_CAPABILITIES_KEY] = context.state.get("client_capabilities")
        return request_payload, http_kwargs
```

## A2UI JSON Format Quick Reference

A2UI responses are wrapped in `<a2ui-json>` and `</a2ui-json>` tags within LLM output. The JSON follows the A2UI schema with these key message types:

- **`beginRendering`**: Start a new UI surface (with `surfaceId`)
- **`surfaceUpdate`**: Update components within a surface
- **`dataModelUpdate`**: Update data bindings
- **`endRendering`**: Signal rendering is complete

Components use the BasicCatalog types: `Text`, `Card`, `Button`, `TextField`, `Image`, `Column`, `Row`, `Grid`, etc.

## A2UI Data Part Format (A2A Transport)

A2UI data in A2A uses `DataPart` with metadata:
```python
Part(root=DataPart(
    data={"surfaceUpdate": {...}},
    metadata={"mimeType": "application/json+a2ui"}
))
```

The `mimeType: "application/json+a2ui"` is the discriminator for identifying A2UI parts.

## User Actions (Client Events)

When users interact with rendered UI (button clicks, form submissions), the client sends back:
```json
{
  "userAction": {
    "actionName": "book_restaurant",
    "surfaceId": "main-surface",
    "context": {
      "restaurantName": "Example Restaurant",
      "address": "123 Main St"
    }
  }
}
```

The executor extracts these from `DataPart.data["userAction"]` and converts them into natural language queries for the LLM.

## Design System: Tailwind CSS + shadcn/ui for A2UI

When generating A2UI components, ALWAYS follow these design principles inspired by Tailwind CSS utility patterns and shadcn/ui component aesthetics. This produces clean, modern, accessible UIs.

### Theme & Style Tokens

Use these style values in `beginRendering.styles` to establish the shadcn/ui look:

```json
{
  "beginRendering": {
    "surfaceId": "main",
    "root": "root",
    "styles": {
      "primaryColor": "#18181B",
      "primaryForeground": "#FAFAFA",
      "secondaryColor": "#F4F4F5",
      "secondaryForeground": "#18181B",
      "accentColor": "#F4F4F5",
      "accentForeground": "#18181B",
      "destructiveColor": "#EF4444",
      "mutedColor": "#F4F4F5",
      "mutedForeground": "#71717A",
      "borderColor": "#E4E4E7",
      "ringColor": "#18181B",
      "backgroundColor": "#FFFFFF",
      "foregroundColor": "#09090B",
      "cardColor": "#FFFFFF",
      "cardForeground": "#09090B",
      "radius": "0.5rem",
      "font": "Inter"
    }
  }
}
```

For dark mode surfaces:
```json
{
  "styles": {
    "primaryColor": "#FAFAFA",
    "primaryForeground": "#18181B",
    "secondaryColor": "#27272A",
    "secondaryForeground": "#FAFAFA",
    "accentColor": "#27272A",
    "accentForeground": "#FAFAFA",
    "destructiveColor": "#EF4444",
    "mutedColor": "#27272A",
    "mutedForeground": "#A1A1AA",
    "borderColor": "#27272A",
    "backgroundColor": "#09090B",
    "foregroundColor": "#FAFAFA",
    "cardColor": "#09090B",
    "cardForeground": "#FAFAFA",
    "font": "Inter"
  }
}
```

### Design Rules for LLM Prompts

Add these rules to your `UI_DESCRIPTION` or `ROLE_DESCRIPTION` to guide the LLM toward beautiful output:

```python
DESIGN_SYSTEM_RULES = """
## Design System Rules (Tailwind + shadcn/ui)

Follow these rules when generating A2UI JSON to produce clean, modern UIs:

### Layout & Spacing
- Use Column as the primary layout container, with Row for horizontal arrangements.
- Prefer single-column layouts for mobile-friendly surfaces. Use two-column Row layouts only for grid/comparison views.
- Group related content inside Card components for visual separation.
- Use Divider sparingly — whitespace (via layout nesting) is preferred over explicit dividers.

### Typography Hierarchy
- h1: Page titles only. One per surface. Bold, large.
- h2: Section headings within a page/card.
- h3: Card titles, list item names.
- h4/h5: Labels, metadata, secondary info.
- caption: Timestamps, helper text, small annotations.
- body: Default for all other text.
- NEVER use more than 2 heading levels in a single Card.

### Card Patterns (shadcn-style)
- Every Card should follow: Header → Content → Footer pattern.
- Header: h3 title + optional caption subtitle.
- Content: The main body — text, images, form fields.
- Footer: Action buttons aligned to the end of a Row.
- Keep Cards focused — one purpose per card (one form, one item, one status).

### Buttons
- Use `"primary": true` for the single main action per surface/card.
- Secondary actions: `"primary": false` or omit the field.
- Destructive actions (delete, cancel): pair with a confirmation step, not inline.
- Button text should be short action verbs: "Book Now", "Submit", "View Details", "Save".

### Forms (shadcn Input style)
- Always provide a clear `label` for every TextField.
- Group form fields in a Column inside a Card.
- Place the submit Button at the bottom of the form, inside a Row with `distribution: "end"`.
- Use appropriate `textFieldType`: "number" for quantities, "longText" for descriptions, "shortText" for names.
- Use DateTimeInput for dates/times instead of raw TextFields.
- Use MultipleChoice with `variant: "chips"` for tag-like selections, `variant: "checkbox"` for multi-select lists.

### Images
- Use `usageHint: "header"` for hero/banner images at the top of a Card or surface.
- Use `usageHint: "avatar"` for user/entity profile pictures (circular).
- Use `usageHint: "smallFeature"` or `"mediumFeature"` for thumbnails in list items.
- Always set `fit: "cover"` for feature images to prevent distortion.

### Color & Visual Tone
- Keep surfaces clean with plenty of whitespace — avoid visual clutter.
- Use the primaryColor for CTAs and interactive elements only.
- Use mutedForeground for secondary text (timestamps, descriptions, helper text).
- Avoid bright colors for large areas — reserve them for accents and status indicators.

### Data Display
- For lists of items (products, contacts, results): use List with template + Card children.
- For key-value data: use Row with two Text children (label as caption, value as body).
- For status/metrics: use a Row of Cards, each with an h4 label + h2 value.
- For charts: prefer Chart component with clear titles.

### Responsive Patterns
- 1-3 items: Single column list with Card per item.
- 4-6 items: Two-column grid (Row containing 2 Cards per row).
- 7+ items: Scrollable List with compact Card templates.
- Forms: Always single column regardless of field count.
"""
```

### Custom Catalog: shadcn/ui-Inspired Components

Create a custom catalog that mirrors shadcn/ui component patterns. This extends the BasicCatalog with design-system-aware components:

```json
{
  "catalogId": "https://your-org.com/catalogs/shadcn-a2ui/v1/catalog.json",
  "components": {
    "Text": {
      "type": "object",
      "description": "Typography component following shadcn/ui text styles. Use usageHint to set the visual hierarchy. Supports Tailwind-style sizing: h1 (text-4xl font-bold tracking-tight), h2 (text-3xl font-semibold tracking-tight), h3 (text-2xl font-semibold), h4 (text-xl font-semibold), h5 (text-lg font-medium), caption (text-sm text-muted-foreground), body (text-base).",
      "properties": {
        "text": {
          "type": "object",
          "properties": {
            "literalString": { "type": "string" },
            "path": { "type": "string" }
          }
        },
        "usageHint": {
          "type": "string",
          "enum": ["h1", "h2", "h3", "h4", "h5", "caption", "body"]
        }
      },
      "required": ["text"]
    },
    "Badge": {
      "type": "object",
      "description": "A small status indicator, similar to shadcn/ui Badge. Variants: 'default' (solid primary bg), 'secondary' (muted bg), 'outline' (border only), 'destructive' (red bg for errors/warnings).",
      "properties": {
        "text": {
          "type": "object",
          "properties": {
            "literalString": { "type": "string" },
            "path": { "type": "string" }
          }
        },
        "variant": {
          "type": "string",
          "enum": ["default", "secondary", "outline", "destructive"]
        }
      },
      "required": ["text"]
    },
    "Avatar": {
      "type": "object",
      "description": "A circular avatar component (shadcn/ui Avatar). Shows image with fallback initials. Use in user profiles, contact cards, comment threads.",
      "properties": {
        "imageUrl": {
          "type": "object",
          "properties": {
            "literalString": { "type": "string" },
            "path": { "type": "string" }
          }
        },
        "fallbackText": {
          "type": "object",
          "description": "1-2 character initials shown when image fails to load.",
          "properties": {
            "literalString": { "type": "string" },
            "path": { "type": "string" }
          }
        }
      },
      "required": ["fallbackText"]
    },
    "Separator": {
      "type": "object",
      "description": "A thin divider line (shadcn/ui Separator). Defaults to horizontal. Use to separate content sections within a card or page, but prefer spacing over separators when possible.",
      "properties": {
        "orientation": {
          "type": "string",
          "enum": ["horizontal", "vertical"]
        }
      }
    },
    "Alert": {
      "type": "object",
      "description": "A callout box for important messages (shadcn/ui Alert). Variants: 'default' for info, 'destructive' for errors. Always include a title and description.",
      "properties": {
        "variant": {
          "type": "string",
          "enum": ["default", "destructive"]
        },
        "title": {
          "type": "object",
          "properties": {
            "literalString": { "type": "string" },
            "path": { "type": "string" }
          }
        },
        "description": {
          "type": "object",
          "properties": {
            "literalString": { "type": "string" },
            "path": { "type": "string" }
          }
        },
        "icon": {
          "type": "string",
          "enum": ["info", "warning", "error", "check"]
        }
      },
      "required": ["title", "description"]
    },
    "Progress": {
      "type": "object",
      "description": "A horizontal progress bar (shadcn/ui Progress). Value is 0-100.",
      "properties": {
        "value": {
          "type": "object",
          "properties": {
            "literalNumber": { "type": "number" },
            "path": { "type": "string" }
          }
        },
        "label": {
          "type": "object",
          "properties": {
            "literalString": { "type": "string" },
            "path": { "type": "string" }
          }
        }
      },
      "required": ["value"]
    },
    "Card": {
      "type": "object",
      "description": "A container with border, rounded corners (radius from theme), and subtle shadow — following shadcn/ui Card. Use as the primary content grouping element. Structure content inside as: header (h3 + caption) → body content → footer (action buttons).",
      "properties": {
        "child": {
          "type": "string",
          "description": "The ID of the component to render inside the card."
        }
      },
      "required": ["child"]
    },
    "Button": {
      "type": "object",
      "description": "An interactive button (shadcn/ui Button). Use primary=true for the main CTA. Keep label text short and action-oriented (2-3 words). One primary button per card/surface.",
      "properties": {
        "child": { "type": "string" },
        "primary": { "type": "boolean" },
        "action": {
          "type": "object",
          "properties": {
            "name": { "type": "string" },
            "context": {
              "type": "array",
              "items": {
                "type": "object",
                "properties": {
                  "key": { "type": "string" },
                  "value": {
                    "type": "object",
                    "properties": {
                      "path": { "type": "string" },
                      "literalString": { "type": "string" },
                      "literalNumber": { "type": "number" },
                      "literalBoolean": { "type": "boolean" }
                    }
                  }
                },
                "required": ["key", "value"]
              }
            }
          },
          "required": ["name"]
        }
      },
      "required": ["child", "action"]
    },
    "Image": {
      "type": "object",
      "description": "An image component with rounded corners matching the theme radius. Use usageHint to control sizing: 'avatar' (circular, 40px), 'icon' (24px square), 'smallFeature' (80px), 'mediumFeature' (160px), 'largeFeature' (320px), 'header' (full-width banner). Always set fit='cover' for feature images.",
      "properties": {
        "url": {
          "type": "object",
          "properties": {
            "literalString": { "type": "string" },
            "path": { "type": "string" }
          }
        },
        "fit": {
          "type": "string",
          "enum": ["contain", "cover", "fill", "none", "scale-down"]
        },
        "usageHint": {
          "type": "string",
          "enum": ["icon", "avatar", "smallFeature", "mediumFeature", "largeFeature", "header"]
        }
      },
      "required": ["url"]
    },
    "Row": {
      "type": "object",
      "description": "Horizontal flex layout (Tailwind: flex flex-row). Use distribution for justify-content and alignment for align-items. Default gap between children follows the theme spacing.",
      "properties": {
        "children": {
          "type": "object",
          "properties": {
            "explicitList": { "type": "array", "items": { "type": "string" } },
            "template": {
              "type": "object",
              "properties": {
                "componentId": { "type": "string" },
                "dataBinding": { "type": "string" }
              },
              "required": ["componentId", "dataBinding"]
            }
          }
        },
        "distribution": {
          "type": "string",
          "enum": ["center", "end", "spaceAround", "spaceBetween", "spaceEvenly", "start"]
        },
        "alignment": {
          "type": "string",
          "enum": ["start", "center", "end", "stretch"]
        }
      },
      "required": ["children"]
    },
    "Column": {
      "type": "object",
      "description": "Vertical flex layout (Tailwind: flex flex-col). The primary layout container. Use for page structure and card internals.",
      "properties": {
        "children": {
          "type": "object",
          "properties": {
            "explicitList": { "type": "array", "items": { "type": "string" } },
            "template": {
              "type": "object",
              "properties": {
                "componentId": { "type": "string" },
                "dataBinding": { "type": "string" }
              },
              "required": ["componentId", "dataBinding"]
            }
          }
        },
        "distribution": {
          "type": "string",
          "enum": ["start", "center", "end", "spaceBetween", "spaceAround", "spaceEvenly"]
        },
        "alignment": {
          "type": "string",
          "enum": ["center", "end", "start", "stretch"]
        }
      },
      "required": ["children"]
    },
    "List": {
      "type": "object",
      "description": "A scrollable list of items. Use template children for dynamic data-driven lists. Prefer vertical direction for most cases.",
      "properties": {
        "children": {
          "type": "object",
          "properties": {
            "explicitList": { "type": "array", "items": { "type": "string" } },
            "template": {
              "type": "object",
              "properties": {
                "componentId": { "type": "string" },
                "dataBinding": { "type": "string" }
              },
              "required": ["componentId", "dataBinding"]
            }
          }
        },
        "direction": {
          "type": "string",
          "enum": ["vertical", "horizontal"]
        }
      },
      "required": ["children"]
    },
    "TextField": {
      "type": "object",
      "description": "A text input field (shadcn/ui Input). Renders with border, rounded corners, and focus ring matching the theme. Always provide a label.",
      "properties": {
        "label": {
          "type": "object",
          "properties": {
            "literalString": { "type": "string" },
            "path": { "type": "string" }
          }
        },
        "text": {
          "type": "object",
          "properties": {
            "literalString": { "type": "string" },
            "path": { "type": "string" }
          }
        },
        "textFieldType": {
          "type": "string",
          "enum": ["date", "longText", "number", "shortText", "obscured"]
        },
        "validationRegexp": { "type": "string" }
      },
      "required": ["label"]
    },
    "DateTimeInput": {
      "type": "object",
      "description": "Date/time picker (shadcn/ui DatePicker). Use instead of TextField for date/time values.",
      "properties": {
        "value": {
          "type": "object",
          "properties": {
            "literalString": { "type": "string" },
            "path": { "type": "string" }
          }
        },
        "label": {
          "type": "object",
          "properties": {
            "literalString": { "type": "string" },
            "path": { "type": "string" }
          }
        },
        "enableDate": { "type": "boolean" },
        "enableTime": { "type": "boolean" }
      },
      "required": ["value"]
    },
    "MultipleChoice": {
      "type": "object",
      "description": "Multi-select component. Use variant='chips' for tag-like selections (shadcn/ui style), 'checkbox' for traditional checkboxes.",
      "properties": {
        "selections": {
          "type": "object",
          "properties": {
            "literalArray": { "type": "array", "items": { "type": "string" } },
            "path": { "type": "string" }
          }
        },
        "options": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "label": {
                "type": "object",
                "properties": {
                  "literalString": { "type": "string" },
                  "path": { "type": "string" }
                }
              },
              "value": { "type": "string" }
            },
            "required": ["label", "value"]
          }
        },
        "maxAllowedSelections": { "type": "integer" },
        "variant": {
          "type": "string",
          "enum": ["checkbox", "chips"]
        },
        "filterable": { "type": "boolean" }
      },
      "required": ["selections", "options"]
    },
    "Tabs": {
      "type": "object",
      "description": "Tab navigation (shadcn/ui Tabs). Use for organizing content into switchable sections within a surface.",
      "properties": {
        "tabItems": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "title": {
                "type": "object",
                "properties": {
                  "literalString": { "type": "string" },
                  "path": { "type": "string" }
                }
              },
              "child": { "type": "string" }
            },
            "required": ["title", "child"]
          }
        }
      },
      "required": ["tabItems"]
    },
    "Divider": {
      "type": "object",
      "description": "A thin horizontal or vertical line. Use sparingly — prefer whitespace and Card boundaries for visual separation.",
      "properties": {
        "axis": {
          "type": "string",
          "enum": ["horizontal", "vertical"]
        }
      }
    },
    "Icon": {
      "type": "object",
      "description": "A material icon. Use alongside text in buttons, list items, or as status indicators.",
      "properties": {
        "name": {
          "type": "object",
          "properties": {
            "literalString": {
              "type": "string",
              "enum": ["accountCircle", "add", "arrowBack", "arrowForward", "attachFile", "calendarToday", "call", "camera", "check", "close", "delete", "download", "edit", "event", "error", "favorite", "favoriteOff", "folder", "help", "home", "info", "locationOn", "lock", "lockOpen", "mail", "menu", "moreVert", "moreHoriz", "notificationsOff", "notifications", "payment", "person", "phone", "photo", "print", "refresh", "search", "send", "settings", "share", "shoppingCart", "star", "starHalf", "starOff", "upload", "visibility", "visibilityOff", "warning"]
            },
            "path": { "type": "string" }
          }
        }
      },
      "required": ["name"]
    },
    "Slider": {
      "type": "object",
      "description": "A range slider input (shadcn/ui Slider).",
      "properties": {
        "value": {
          "type": "object",
          "properties": {
            "literalNumber": { "type": "number" },
            "path": { "type": "string" }
          }
        },
        "minValue": { "type": "number" },
        "maxValue": { "type": "number" }
      },
      "required": ["value"]
    },
    "CheckBox": {
      "type": "object",
      "description": "A checkbox input (shadcn/ui Checkbox).",
      "properties": {
        "label": {
          "type": "object",
          "properties": {
            "literalString": { "type": "string" },
            "path": { "type": "string" }
          }
        },
        "value": {
          "type": "object",
          "properties": {
            "literalBoolean": { "type": "boolean" },
            "path": { "type": "string" }
          }
        }
      },
      "required": ["label", "value"]
    },
    "Chart": {
      "type": "object",
      "description": "An interactive chart component for data visualization.",
      "properties": {
        "type": {
          "type": "string",
          "enum": ["doughnut", "pie"]
        },
        "title": {
          "type": "object",
          "properties": {
            "literalString": { "type": "string" },
            "path": { "type": "string" }
          }
        },
        "chartData": {
          "type": "object",
          "properties": {
            "literalArray": {
              "type": "array",
              "items": {
                "type": "object",
                "properties": {
                  "label": { "type": "string" },
                  "value": { "type": "number" },
                  "drillDown": {
                    "type": "array",
                    "items": {
                      "type": "object",
                      "properties": {
                        "label": { "type": "string" },
                        "value": { "type": "number" }
                      },
                      "required": ["label", "value"]
                    }
                  }
                },
                "required": ["label", "value"]
              }
            },
            "path": { "type": "string" }
          }
        }
      },
      "required": ["type", "chartData"]
    },
    "Modal": {
      "type": "object",
      "description": "A modal dialog (shadcn/ui Dialog). Use for confirmations, detail views, or secondary forms that shouldn't navigate away from the current surface.",
      "properties": {
        "entryPointChild": { "type": "string" },
        "contentChild": { "type": "string" }
      },
      "required": ["entryPointChild", "contentChild"]
    }
  }
}
```

### Using the Custom Catalog with A2uiSchemaManager

To use a custom catalog instead of BasicCatalog:

```python
from a2ui.core.schema.catalog import CatalogConfig
from a2ui.core.schema.catalog_provider import A2uiCatalogProvider

class FileCatalogProvider(A2uiCatalogProvider):
    """Loads a catalog from a local JSON file."""
    def __init__(self, path: str):
        self.path = path

    def load(self):
        import json
        with open(self.path) as f:
            return json.load(f)

schema_manager = A2uiSchemaManager(
    VERSION_0_8,
    catalogs=[
        CatalogConfig(
            name="shadcn-a2ui",
            provider=FileCatalogProvider("catalogs/shadcn_catalog.json"),
            examples_path="examples",
        )
    ],
    schema_modifiers=[remove_strict_validation],
)
```

### Example: shadcn-Style Contact Card

```json
[
  {
    "beginRendering": {
      "surfaceId": "contact-card",
      "root": "card-wrapper",
      "styles": {
        "primaryColor": "#18181B",
        "primaryForeground": "#FAFAFA",
        "mutedColor": "#F4F4F5",
        "mutedForeground": "#71717A",
        "borderColor": "#E4E4E7",
        "backgroundColor": "#FFFFFF",
        "foregroundColor": "#09090B",
        "cardColor": "#FFFFFF",
        "radius": "0.5rem",
        "font": "Inter"
      }
    }
  },
  {
    "surfaceUpdate": {
      "surfaceId": "contact-card",
      "components": [
        {
          "id": "card-wrapper",
          "component": { "Card": { "child": "card-content" } }
        },
        {
          "id": "card-content",
          "component": {
            "Column": {
              "children": {
                "explicitList": ["header-row", "divider", "details-col", "actions-row"]
              }
            }
          }
        },
        {
          "id": "header-row",
          "component": {
            "Row": {
              "children": { "explicitList": ["avatar-img", "header-text-col"] },
              "alignment": "center"
            }
          }
        },
        {
          "id": "avatar-img",
          "component": {
            "Image": {
              "url": { "path": "/avatarUrl" },
              "usageHint": "avatar",
              "fit": "cover"
            }
          }
        },
        {
          "id": "header-text-col",
          "component": {
            "Column": {
              "children": { "explicitList": ["contact-name", "contact-role"] }
            }
          }
        },
        {
          "id": "contact-name",
          "component": {
            "Text": { "usageHint": "h3", "text": { "path": "/name" } }
          }
        },
        {
          "id": "contact-role",
          "component": {
            "Text": { "usageHint": "caption", "text": { "path": "/role" } }
          }
        },
        {
          "id": "divider",
          "component": { "Divider": { "axis": "horizontal" } }
        },
        {
          "id": "details-col",
          "component": {
            "Column": {
              "children": { "explicitList": ["email-row", "phone-row", "location-row"] }
            }
          }
        },
        {
          "id": "email-row",
          "component": {
            "Row": {
              "children": { "explicitList": ["email-icon", "email-text"] },
              "alignment": "center"
            }
          }
        },
        {
          "id": "email-icon",
          "component": { "Icon": { "name": { "literalString": "mail" } } }
        },
        {
          "id": "email-text",
          "component": { "Text": { "text": { "path": "/email" } } }
        },
        {
          "id": "phone-row",
          "component": {
            "Row": {
              "children": { "explicitList": ["phone-icon", "phone-text"] },
              "alignment": "center"
            }
          }
        },
        {
          "id": "phone-icon",
          "component": { "Icon": { "name": { "literalString": "phone" } } }
        },
        {
          "id": "phone-text",
          "component": { "Text": { "text": { "path": "/phone" } } }
        },
        {
          "id": "location-row",
          "component": {
            "Row": {
              "children": { "explicitList": ["location-icon", "location-text"] },
              "alignment": "center"
            }
          }
        },
        {
          "id": "location-icon",
          "component": { "Icon": { "name": { "literalString": "locationOn" } } }
        },
        {
          "id": "location-text",
          "component": { "Text": { "usageHint": "caption", "text": { "path": "/location" } } }
        },
        {
          "id": "actions-row",
          "component": {
            "Row": {
              "children": { "explicitList": ["message-btn", "call-btn"] },
              "distribution": "end"
            }
          }
        },
        {
          "id": "message-btn",
          "component": {
            "Button": {
              "child": "message-btn-text",
              "action": {
                "name": "send_message",
                "context": [{ "key": "contactId", "value": { "path": "/id" } }]
              }
            }
          }
        },
        {
          "id": "message-btn-text",
          "component": { "Text": { "text": { "literalString": "Message" } } }
        },
        {
          "id": "call-btn",
          "component": {
            "Button": {
              "child": "call-btn-text",
              "primary": true,
              "action": {
                "name": "start_call",
                "context": [{ "key": "phone", "value": { "path": "/phone" } }]
              }
            }
          }
        },
        {
          "id": "call-btn-text",
          "component": { "Text": { "text": { "literalString": "Call" } } }
        }
      ]
    }
  },
  {
    "dataModelUpdate": {
      "surfaceId": "contact-card",
      "path": "/",
      "contents": [
        { "key": "id", "valueString": "usr_001" },
        { "key": "name", "valueString": "Sarah Chen" },
        { "key": "role", "valueString": "Senior Product Designer" },
        { "key": "email", "valueString": "sarah.chen@company.com" },
        { "key": "phone", "valueString": "+1 (555) 234-5678" },
        { "key": "location", "valueString": "San Francisco, CA" },
        { "key": "avatarUrl", "valueString": "https://example.com/avatars/sarah.jpg" }
      ]
    }
  }
]
```

### Example: shadcn-Style Dashboard Metrics Row

```json
[
  {
    "beginRendering": {
      "surfaceId": "dashboard",
      "root": "dashboard-col",
      "styles": {
        "primaryColor": "#18181B",
        "backgroundColor": "#FFFFFF",
        "cardColor": "#FFFFFF",
        "mutedForeground": "#71717A",
        "borderColor": "#E4E4E7",
        "radius": "0.5rem",
        "font": "Inter"
      }
    }
  },
  {
    "surfaceUpdate": {
      "surfaceId": "dashboard",
      "components": [
        {
          "id": "dashboard-col",
          "component": {
            "Column": {
              "children": { "explicitList": ["page-title", "metrics-row"] }
            }
          }
        },
        {
          "id": "page-title",
          "component": { "Text": { "usageHint": "h1", "text": { "literalString": "Dashboard" } } }
        },
        {
          "id": "metrics-row",
          "component": {
            "Row": {
              "children": { "explicitList": ["metric-revenue", "metric-users", "metric-orders"] },
              "distribution": "spaceBetween"
            }
          }
        },
        {
          "id": "metric-revenue",
          "weight": 1,
          "component": { "Card": { "child": "revenue-col" } }
        },
        {
          "id": "revenue-col",
          "component": {
            "Column": {
              "children": { "explicitList": ["revenue-label", "revenue-value", "revenue-change"] }
            }
          }
        },
        {
          "id": "revenue-label",
          "component": { "Text": { "usageHint": "caption", "text": { "literalString": "Total Revenue" } } }
        },
        {
          "id": "revenue-value",
          "component": { "Text": { "usageHint": "h2", "text": { "path": "/revenue" } } }
        },
        {
          "id": "revenue-change",
          "component": { "Text": { "usageHint": "caption", "text": { "path": "/revenueChange" } } }
        },
        {
          "id": "metric-users",
          "weight": 1,
          "component": { "Card": { "child": "users-col" } }
        },
        {
          "id": "users-col",
          "component": {
            "Column": {
              "children": { "explicitList": ["users-label", "users-value", "users-change"] }
            }
          }
        },
        {
          "id": "users-label",
          "component": { "Text": { "usageHint": "caption", "text": { "literalString": "Active Users" } } }
        },
        {
          "id": "users-value",
          "component": { "Text": { "usageHint": "h2", "text": { "path": "/activeUsers" } } }
        },
        {
          "id": "users-change",
          "component": { "Text": { "usageHint": "caption", "text": { "path": "/usersChange" } } }
        },
        {
          "id": "metric-orders",
          "weight": 1,
          "component": { "Card": { "child": "orders-col" } }
        },
        {
          "id": "orders-col",
          "component": {
            "Column": {
              "children": { "explicitList": ["orders-label", "orders-value", "orders-change"] }
            }
          }
        },
        {
          "id": "orders-label",
          "component": { "Text": { "usageHint": "caption", "text": { "literalString": "Orders" } } }
        },
        {
          "id": "orders-value",
          "component": { "Text": { "usageHint": "h2", "text": { "path": "/orders" } } }
        },
        {
          "id": "orders-change",
          "component": { "Text": { "usageHint": "caption", "text": { "path": "/ordersChange" } } }
        }
      ]
    }
  },
  {
    "dataModelUpdate": {
      "surfaceId": "dashboard",
      "path": "/",
      "contents": [
        { "key": "revenue", "valueString": "$45,231.89" },
        { "key": "revenueChange", "valueString": "+20.1% from last month" },
        { "key": "activeUsers", "valueString": "2,350" },
        { "key": "usersChange", "valueString": "+180 since last hour" },
        { "key": "orders", "valueString": "12,234" },
        { "key": "ordersChange", "valueString": "+19% from last month" }
      ]
    }
  }
]
```

### Example: shadcn-Style Form (Booking/Input)

```json
[
  {
    "beginRendering": {
      "surfaceId": "booking-form",
      "root": "form-card",
      "styles": {
        "primaryColor": "#18181B",
        "primaryForeground": "#FAFAFA",
        "mutedForeground": "#71717A",
        "borderColor": "#E4E4E7",
        "backgroundColor": "#FFFFFF",
        "radius": "0.5rem",
        "font": "Inter"
      }
    }
  },
  {
    "surfaceUpdate": {
      "surfaceId": "booking-form",
      "components": [
        {
          "id": "form-card",
          "component": { "Card": { "child": "form-col" } }
        },
        {
          "id": "form-col",
          "component": {
            "Column": {
              "children": {
                "explicitList": [
                  "form-title", "form-subtitle",
                  "name-field", "email-field", "date-field",
                  "guests-field", "notes-field", "dietary-chips",
                  "submit-row"
                ]
              }
            }
          }
        },
        {
          "id": "form-title",
          "component": { "Text": { "usageHint": "h3", "text": { "literalString": "Book a Table" } } }
        },
        {
          "id": "form-subtitle",
          "component": { "Text": { "usageHint": "caption", "text": { "path": "/restaurantName" } } }
        },
        {
          "id": "name-field",
          "component": {
            "TextField": {
              "label": { "literalString": "Full Name" },
              "text": { "path": "/guestName" },
              "textFieldType": "shortText"
            }
          }
        },
        {
          "id": "email-field",
          "component": {
            "TextField": {
              "label": { "literalString": "Email" },
              "text": { "path": "/email" },
              "textFieldType": "shortText"
            }
          }
        },
        {
          "id": "date-field",
          "component": {
            "DateTimeInput": {
              "label": { "literalString": "Date & Time" },
              "value": { "path": "/reservationDate" },
              "enableDate": true,
              "enableTime": true
            }
          }
        },
        {
          "id": "guests-field",
          "component": {
            "TextField": {
              "label": { "literalString": "Number of Guests" },
              "text": { "path": "/guestCount" },
              "textFieldType": "number"
            }
          }
        },
        {
          "id": "notes-field",
          "component": {
            "TextField": {
              "label": { "literalString": "Special Requests" },
              "text": { "path": "/notes" },
              "textFieldType": "longText"
            }
          }
        },
        {
          "id": "dietary-chips",
          "component": {
            "MultipleChoice": {
              "selections": { "path": "/dietaryPrefs" },
              "options": [
                { "label": { "literalString": "Vegetarian" }, "value": "vegetarian" },
                { "label": { "literalString": "Vegan" }, "value": "vegan" },
                { "label": { "literalString": "Gluten-Free" }, "value": "gluten-free" },
                { "label": { "literalString": "Halal" }, "value": "halal" },
                { "label": { "literalString": "Kosher" }, "value": "kosher" }
              ],
              "variant": "chips"
            }
          }
        },
        {
          "id": "submit-row",
          "component": {
            "Row": {
              "children": { "explicitList": ["submit-btn"] },
              "distribution": "end"
            }
          }
        },
        {
          "id": "submit-btn",
          "component": {
            "Button": {
              "child": "submit-text",
              "primary": true,
              "action": {
                "name": "submit_booking",
                "context": [
                  { "key": "guestName", "value": { "path": "/guestName" } },
                  { "key": "email", "value": { "path": "/email" } },
                  { "key": "date", "value": { "path": "/reservationDate" } },
                  { "key": "guests", "value": { "path": "/guestCount" } },
                  { "key": "notes", "value": { "path": "/notes" } }
                ]
              }
            }
          }
        },
        {
          "id": "submit-text",
          "component": { "Text": { "text": { "literalString": "Reserve Table" } } }
        }
      ]
    }
  },
  {
    "dataModelUpdate": {
      "surfaceId": "booking-form",
      "path": "/",
      "contents": [
        { "key": "restaurantName", "valueString": "The Garden Bistro" },
        { "key": "guestName", "valueString": "" },
        { "key": "email", "valueString": "" },
        { "key": "reservationDate", "valueString": "" },
        { "key": "guestCount", "valueString": "2" },
        { "key": "notes", "valueString": "" }
      ]
    }
  }
]
```

### Integrating Design Rules into Your Agent

```python
ROLE_DESCRIPTION = (
    "You are a helpful assistant. Your final output MUST be an A2UI UI JSON response. "
    "Follow the shadcn/ui design system: clean layouts, proper typography hierarchy, "
    "Cards for grouping, Inter font, neutral color palette with zinc tones."
)

UI_DESCRIPTION = DESIGN_SYSTEM_RULES + """
## Template Selection:
- For item lists: Use Card-based list with avatar + details + action button per item.
- For forms: Single Card with stacked fields, submit button right-aligned at bottom.
- For dashboards: Row of metric Cards at top, content below.
- For detail views: Single Card with header, divider, key-value details, action footer.
"""

instruction = schema_manager.generate_system_prompt(
    role_description=ROLE_DESCRIPTION,
    ui_description=UI_DESCRIPTION,
    include_schema=True,
    include_examples=True,
    validate_examples=True,
)
```

## Running Samples

```bash
# Clone A2UI repo
git clone https://github.com/google/A2UI.git && cd A2UI

# Set API key
export GEMINI_API_KEY="your_key"

# Run any ADK sample
cd samples/agent/adk/restaurant_finder  # or contact_lookup, rizzcharts, etc.
uv run .

# Run the web client (separate terminal)
cd renderers/markdown/markdown-it && npm install && npm run build
cd ../../web_core && npm install && npm run build
cd ../lit && npm install && npm run build
cd ../../samples/client/lit/shell && npm install && npm run dev
```

## File Structure for a New A2UI ADK Agent

```
my_agent/
  __init__.py
  __main__.py          # Server entry point (click CLI, uvicorn)
  agent.py             # Agent class with schema manager, LLM agent, stream method
  agent_executor.py    # A2A AgentExecutor bridging protocol to agent
  tools.py             # ADK tool functions
  prompt_builder.py    # Role/UI description constants, prompt generation
  pyproject.toml       # Dependencies including a2ui-agent
  examples/            # A2UI JSON example files for few-shot prompting
  .env.example         # GEMINI_API_KEY template
```
