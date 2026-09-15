from __future__ import annotations

import inspect
import runpy
from pathlib import Path

import pytest
import yaml
from app.agent_testing.suite import inspect_agent_test_suite
from app.runtime.json_types import JsonObject
from app.runtime.managed_agent_policy import require_runtime_workspace_policy
from app.services.agent_native_candidate_mapping import (
    NativeCandidateMappingError,
    native_agent_data_entries,
    native_agent_data_from_harness,
)


def _schema() -> JsonObject:
    return {
        "type": "object",
        "required": ["name", "context_config", "react_config"],
        "properties": {
            "name": {"type": "string"},
            "system_prompt": {"type": "string", "default": "default prompt"},
            "context_config": {
                "type": "object",
                "properties": {"trigger_ratio": {"type": "number", "exclusiveMinimum": 0, "maximum": 0.9}},
            },
            "react_config": {
                "type": "object",
                "properties": {"max_iters": {"type": "integer", "minimum": 1}},
            },
            "invite_config": {
                "type": "object",
                "properties": {"invitable": {"type": "boolean"}},
            },
        },
    }


def test_native_agent_data_maps_once_to_safe_harness_files(tmp_path: Path) -> None:
    entries = native_agent_data_entries(
        agent_id="analyst",
        agent_data={
            "name": "Analyst",
            "system_prompt": "Review evidence.",
            "context_config": {"trigger_ratio": 0.8},
            "react_config": {"max_iters": 12},
            "invite_config": {"invitable": False},
        },
        schema=_schema(),
    )

    assert [entry.relative_path.as_posix() for entry in entries] == [
        "agent.yaml",
        "AGENT.md",
        "tests/README.md",
        "tests/test_native_agent_harness_contract.py",
    ]
    for entry in entries:
        target = tmp_path / entry.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(entry.content)
    manifest = yaml.safe_load((tmp_path / "agent.yaml").read_text(encoding="utf-8"))
    assert manifest["agent"]["id"] == "analyst"
    assert manifest["agent"]["name"] == "Analyst"
    assert manifest["context_config"] == {"trigger_ratio": 0.8}
    assert manifest["react_config"] == {"max_iters": 12}
    assert manifest["invite_config"] == {"invitable": False}
    assert (tmp_path / "AGENT.md").read_text(encoding="utf-8") == "Review evidence."
    suite = inspect_agent_test_suite(tmp_path, agent_id="analyst", commit_sha="candidate")
    assert suite.runnable is True
    assert suite.test_files == ["tests/test_native_agent_harness_contract.py"]
    generated = runpy.run_path(str(tmp_path / suite.test_files[0]))
    generated["test_native_agent_harness_contract"]()
    # 这里只验证构建出的真实调用入口，不提供假 Agent；实际调用由容器发布测试执行。
    assert tuple(inspect.signature(generated["test_native_agent_responds"]).parameters) == ("agent",)
    source = inspect.getsource(generated["test_native_agent_responds"])
    assert "result = agent.run(" in source
    assert "assert not result.errors" in source
    require_runtime_workspace_policy(
        workspace=tmp_path,
        agent_id="analyst",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "native-id"),
        ("provider", "private-provider"),
        ("credential", "secret-ref"),
    ],
)
def test_native_agent_data_rejects_backend_or_unknown_top_level_fields(field: str, value: object) -> None:
    payload = {"name": "Analyst", "context_config": {}, "react_config": {}, field: value}
    with pytest.raises(NativeCandidateMappingError, match="Unsupported AgentData fields"):
        native_agent_data_entries(agent_id="analyst", agent_data=payload, schema=_schema())


def test_native_agent_data_rejects_unmapped_nested_fields() -> None:
    payload = {
        "name": "Analyst",
        "context_config": {"summary_schema": {"type": "object"}},
        "react_config": {},
    }
    with pytest.raises(NativeCandidateMappingError, match="Unsupported AgentData.context_config fields"):
        native_agent_data_entries(agent_id="analyst", agent_data=payload, schema=_schema())


def test_native_agent_data_preserves_backend_owned_manifest_fields() -> None:
    base = yaml.safe_load(
        native_agent_data_entries(
            agent_id="analyst",
            agent_data={"name": "Old", "context_config": {}, "react_config": {}},
            schema=_schema(),
        )[0].content
    )
    base["workspace_policy"]["allowed_tools"] = ["Read"]
    base["presentation"] = {"summary": "governance-owned"}

    entries = native_agent_data_entries(
        agent_id="analyst",
        agent_data={"name": "New", "context_config": {}, "react_config": {}, "system_prompt": "new"},
        schema=_schema(),
        base_manifest=base,
    )

    manifest = yaml.safe_load(entries[0].content)
    assert manifest["workspace_policy"]["allowed_tools"] == ["Read"]
    assert manifest["presentation"] == {"summary": "governance-owned"}
    assert manifest["agent"]["name"] == "New"
    assert [entry.relative_path.as_posix() for entry in entries] == ["agent.yaml", "AGENT.md"]


def test_native_agent_data_rejects_blank_system_prompt() -> None:
    with pytest.raises(NativeCandidateMappingError, match="system_prompt must be non-empty"):
        native_agent_data_entries(
            agent_id="analyst",
            agent_data={"name": "Analyst", "system_prompt": "  \n", "context_config": {}, "react_config": {}},
            schema=_schema(),
        )


def test_native_agent_data_rejects_schema_field_drift() -> None:
    schema = _schema()
    schema["properties"]["model_provider"] = {"type": "string"}  # type: ignore[index]
    with pytest.raises(NativeCandidateMappingError, match="mapping review is required"):
        native_agent_data_entries(
            agent_id="analyst",
            agent_data={"name": "Analyst", "context_config": {}, "react_config": {}},
            schema=schema,
        )


def test_native_agent_data_preserves_native_invite_invariant() -> None:
    with pytest.raises(NativeCandidateMappingError, match="invite_description is required"):
        native_agent_data_entries(
            agent_id="analyst",
            agent_data={
                "name": "Analyst",
                "context_config": {},
                "react_config": {},
                "invite_config": {"invitable": True},
            },
            schema=_schema(),
        )


def test_native_agent_data_projection_returns_only_reviewed_fields() -> None:
    manifest: JsonObject = {
        "schema_version": 1,
        "agent": {
            "id": "analyst",
            "name": "Analyst",
            "system_prompt": "AGENT.md",
            "provider_secret": "must-not-leak",
        },
        "context_config": {"trigger_ratio": 0.7},
        "react_config": {"max_iters": 8},
        "invite_config": {"invitable": False},
        "mcp": {"headers": {"Authorization": "must-not-leak"}},
        "paths": {"workspace": "/private/workspace"},
    }

    projected = native_agent_data_from_harness(
        agent_id="analyst",
        manifest=manifest,
        system_prompt="Review evidence.",
        schema=_schema(),
    ).model_dump(mode="json", exclude_unset=True)

    assert projected == {
        "name": "Analyst",
        "system_prompt": "Review evidence.",
        "context_config": {"trigger_ratio": 0.7},
        "react_config": {"max_iters": 8},
        "invite_config": {"invitable": False},
    }
    assert "must-not-leak" not in str(projected)
    assert "/private/workspace" not in str(projected)


def test_native_agent_data_projection_rejects_unmapped_native_drift() -> None:
    manifest: JsonObject = {
        "schema_version": 1,
        "agent": {"id": "analyst", "name": "Analyst", "system_prompt": "AGENT.md"},
        "context_config": {"summary_schema": {"type": "object"}},
        "react_config": {},
        "invite_config": {},
    }

    with pytest.raises(NativeCandidateMappingError, match="Unsupported AgentData.context_config fields"):
        native_agent_data_from_harness(
            agent_id="analyst",
            manifest=manifest,
            system_prompt="Review evidence.",
            schema=_schema(),
        )
