from __future__ import annotations

import uuid

import pytest
from agentgov_agentscope_contract import session_creation_token, session_workspace_id, version_workspace_id
from agentscope_runtime.workspace_manager import AgentGovWorkspaceManager
from app.runtime_gateway.release_activation import _release_probe_workspace_id


def test_release_probe_binding_is_accepted_by_runtime_and_stable_on_retry() -> None:
    """发布端生成的探针标识必须经过 Runtime 实际校验，不能用替身 API 放行。"""
    source_id = "published-" + "a" * 48
    digest = "b" * 64
    workspace_id = f"{source_id}--v-{digest}"

    probe_id = _release_probe_workspace_id(workspace_id)
    parsed_id, parsed_source, parsed_digest = AgentGovWorkspaceManager._parse_workspace_binding(probe_id)

    assert (parsed_id, parsed_source, parsed_digest) == (probe_id, source_id, digest)
    assert _release_probe_workspace_id(workspace_id) == probe_id
    another_probe_id = _release_probe_workspace_id(f"{source_id}--v-{'c' * 64}")
    assert another_probe_id != probe_id
    assert AgentGovWorkspaceManager._parse_workspace_binding(another_probe_id)[2] == "c" * 64


@pytest.mark.parametrize("source_id,digest", [("../published-source", "b" * 64), ("published-source", "not-a-digest")])
def test_release_probe_does_not_relax_runtime_binding_validation(source_id: str, digest: str) -> None:
    probe_id = _release_probe_workspace_id(f"{source_id}--v-{digest}")

    with pytest.raises(ValueError, match="workspace_id must be"):
        AgentGovWorkspaceManager._parse_workspace_binding(probe_id)


def test_session_workspace_contract_preserves_existing_intent_identity() -> None:
    identity = uuid.uuid5(uuid.NAMESPACE_URL, "agentgov:session-contract")
    version_binding = f"published-source--v-{'b' * 64}"
    token = session_creation_token(identity)
    workspace_id = session_workspace_id(version_binding, str(identity))

    assert workspace_id == f"{version_binding}--s-{token}"
    assert token == f"session-intent-{identity}"
    assert version_workspace_id(workspace_id) == version_binding
    assert version_workspace_id(version_binding) == version_binding
    assert AgentGovWorkspaceManager._parse_workspace_binding(workspace_id)[1:] == ("published-source", "b" * 64)


@pytest.mark.parametrize(
    "token",
    [
        "release-probe-00000000-0000-0000-0000-000000000001",
        "session-intent-not-a-uuid",
        "session-intent-00000000-0000-0000-0000-00000000000G",
        "session-intent-00000000-0000-0000-0000-000000000001/../escape",
        "",
    ],
)
def test_runtime_rejects_invalid_session_tokens(token: str) -> None:
    with pytest.raises(ValueError, match="invalid Session creation token"):
        AgentGovWorkspaceManager._parse_workspace_binding(f"published-source--v-{'b' * 64}--s-{token}")
