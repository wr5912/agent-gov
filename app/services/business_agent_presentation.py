from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.runtime.agent_governance_schemas import (
    AgentPresentationResponse,
    AgentStarterPromptResponse,
)
from app.runtime.stores.agent_registry_store import AgentRegistryRecord

logger = logging.getLogger(__name__)

_MAX_MANIFEST_BYTES = 128 * 1024
_MetadataText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
_SummaryText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
_WelcomeText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1600)]
_PlaceholderText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
_PromptLabel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=48)]
_PromptText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1200)]
_CapabilityText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class _ManifestAgent(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    version: _MetadataText | None = None
    language: _MetadataText | None = None
    runtime: _MetadataText | None = None


class _ManifestStarterPrompt(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    label: _PromptLabel
    prompt: _PromptText


class _ManifestPresentation(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    summary: _SummaryText | None = None
    welcome_message: _WelcomeText | None = None
    composer_placeholder: _PlaceholderText | None = None
    starter_prompts: list[_ManifestStarterPrompt] = Field(default_factory=list, max_length=4)


class _AgentManifest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    agent: _ManifestAgent = Field(default_factory=_ManifestAgent)
    capabilities: list[_CapabilityText] = Field(default_factory=list, max_length=64)
    presentation: _ManifestPresentation = Field(default_factory=_ManifestPresentation)


def business_agent_presentation(record: AgentRegistryRecord) -> AgentPresentationResponse:
    """Project the optional Workspace manifest without exposing unrelated configuration."""

    manifest_path = Path(record.workspace_dir) / "agent.yaml"
    manifest = _read_manifest(manifest_path, agent_id=record.agent_id)
    if manifest is None:
        return AgentPresentationResponse(
            agent_id=record.agent_id,
            name=record.name,
            source="registry_fallback",
        )

    presentation = manifest.presentation
    return AgentPresentationResponse(
        agent_id=record.agent_id,
        name=record.name,
        version=manifest.agent.version,
        language=manifest.agent.language,
        runtime=manifest.agent.runtime,
        capabilities=list(manifest.capabilities),
        summary=presentation.summary,
        welcome_message=presentation.welcome_message,
        composer_placeholder=presentation.composer_placeholder,
        starter_prompts=[AgentStarterPromptResponse(label=item.label, prompt=item.prompt) for item in presentation.starter_prompts],
        source="agent_yaml",
    )


def _read_manifest(path: Path, *, agent_id: str) -> _AgentManifest | None:
    try:
        if path.is_symlink():
            _warn_fallback(agent_id, "symlink")
            return None
        stat = path.stat()
        if not path.is_file():
            _warn_fallback(agent_id, "not_regular")
            return None
        if stat.st_size > _MAX_MANIFEST_BYTES:
            _warn_fallback(agent_id, "too_large")
            return None
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            _warn_fallback(agent_id, "not_mapping")
            return None
        return _AgentManifest.model_validate(payload)
    except FileNotFoundError:
        _warn_fallback(agent_id, "missing")
    except (OSError, UnicodeError):
        _warn_fallback(agent_id, "unreadable")
    except yaml.YAMLError:
        _warn_fallback(agent_id, "invalid_yaml")
    except ValidationError:
        _warn_fallback(agent_id, "invalid_schema")
    return None


def _warn_fallback(agent_id: str, reason: str) -> None:
    logger.warning("AGENT_PRESENTATION_FALLBACK agent_id=%s reason=%s", agent_id, reason)
