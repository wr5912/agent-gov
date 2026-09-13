from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from runtime_loopback import serve_loopback
from test_agent_workspace_packages import (
    _bind_runtime_client,
    _candidate_workspace,
    _load_app,
    _native_schema_runtime,
    _run_git,
    _seed_active_agent,
)


def _native_active_agent(module, *, agent_id: str, name: str) -> tuple[Path, str]:
    workspace = _seed_active_agent(module, agent_id=agent_id, name=name)
    manifest_path = workspace / "agent.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["agent"]["name"] = name
    manifest["context_config"] = {}
    manifest["react_config"] = {}
    manifest["invite_config"] = {"invitable": False}
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    _run_git(workspace, "add", "-A", "--", ".")
    _run_git(workspace, "commit", "-m", "Prepare native AgentData source")
    return workspace, _run_git(workspace, "rev-parse", "HEAD")


def _start_candidate(client: TestClient, *, agent_id: str, prompt: str) -> tuple[dict, dict]:
    live_source = client.get(f"/api/agent-registry/{agent_id}/native-candidate-source")
    assert live_source.status_code == 200, live_source.text
    source_body = live_source.json()
    assert source_body["change_set_id"] is None
    agent_data = deepcopy(source_body["agent_data"])
    agent_data["system_prompt"] = prompt
    created = client.post(
        f"/api/agent-registry/{agent_id}/native-candidate",
        json={
            "agent_data": agent_data,
            "expected_current_commit_sha": source_body["current_commit_sha"],
        },
    )
    assert created.status_code == 200, created.text
    return source_body, created.json()


def test_native_form_continues_one_open_candidate_without_mutating_live(
    process_environment,
    tmp_path: Path,
) -> None:
    module = _load_app(process_environment, tmp_path)
    workspace, live_commit = _native_active_agent(
        module,
        agent_id="native-continuation",
        name="Native Continuation",
    )

    with serve_loopback(_native_schema_runtime(tmp_path)) as runtime_url:
        _bind_runtime_client(module, runtime_url)
        with TestClient(module.app) as client:
            live_source, first = _start_candidate(
                client,
                agent_id="native-continuation",
                prompt="candidate prompt v1",
            )
            candidate_source = client.get(
                "/api/agent-registry/native-continuation/native-candidate-source",
            )
            assert candidate_source.status_code == 200, candidate_source.text
            source_body = candidate_source.json()
            assert source_body["change_set_id"] == first["change_set_id"]
            assert source_body["current_commit_sha"] == first["candidate_commit_sha"]
            assert source_body["agent_data"]["system_prompt"] == "candidate prompt v1"

            updated_data = deepcopy(source_body["agent_data"])
            updated_data["system_prompt"] = "candidate prompt v2"
            updated = client.post(
                "/api/agent-registry/native-continuation/native-candidate",
                json={
                    "agent_data": updated_data,
                    "change_set_id": source_body["change_set_id"],
                    "expected_candidate_commit_sha": source_body["current_commit_sha"],
                },
            )
            refreshed = client.get(
                "/api/agent-registry/native-continuation/native-candidate-source",
            )

    assert updated.status_code == 200, updated.text
    updated_body = updated.json()
    assert updated_body["change_set_id"] == first["change_set_id"]
    assert updated_body["base_commit_sha"] == live_source["current_commit_sha"] == live_commit
    assert updated_body["candidate_commit_sha"] != first["candidate_commit_sha"]
    assert refreshed.status_code == 200
    assert refreshed.json()["current_commit_sha"] == updated_body["candidate_commit_sha"]
    assert refreshed.json()["agent_data"]["system_prompt"] == "candidate prompt v2"
    assert _run_git(workspace, "rev-parse", "HEAD") == live_commit
    assert (workspace / "AGENT.md").read_text(encoding="utf-8") != "candidate prompt v2"
    assert (_candidate_workspace(module, updated) / "AGENT.md").read_text(encoding="utf-8") == "candidate prompt v2"
    change_sets = module.agent_governance.list_change_sets(agent_id="native-continuation")
    assert [item["change_set_id"] for item in change_sets] == [first["change_set_id"]]


def test_native_candidate_continuation_rejects_stale_owner_status_and_ambiguous_refs(
    process_environment,
    tmp_path: Path,
) -> None:
    module = _load_app(process_environment, tmp_path)
    _native_active_agent(module, agent_id="native-owner-a", name="Native Owner A")
    _native_active_agent(module, agent_id="native-owner-b", name="Native Owner B")

    with serve_loopback(_native_schema_runtime(tmp_path)) as runtime_url:
        _bind_runtime_client(module, runtime_url)
        with TestClient(module.app) as client:
            _, first_a = _start_candidate(client, agent_id="native-owner-a", prompt="owner a v1")
            _, first_b = _start_candidate(client, agent_id="native-owner-b", prompt="owner b v1")
            source_a = client.get("/api/agent-registry/native-owner-a/native-candidate-source").json()
            next_data = deepcopy(source_a["agent_data"])
            next_data["system_prompt"] = "owner a v2"
            duplicate = client.post(
                "/api/agent-registry/native-owner-a/native-candidate",
                json={
                    "agent_data": next_data,
                    "expected_current_commit_sha": first_a["base_commit_sha"],
                },
            )
            advanced = client.post(
                "/api/agent-registry/native-owner-a/native-candidate",
                json={
                    "agent_data": next_data,
                    "change_set_id": first_a["change_set_id"],
                    "expected_candidate_commit_sha": first_a["candidate_commit_sha"],
                },
            )
            wrong_owner = client.post(
                "/api/agent-registry/native-owner-b/native-candidate",
                json={
                    "agent_data": next_data,
                    "change_set_id": first_a["change_set_id"],
                    "expected_candidate_commit_sha": advanced.json()["candidate_commit_sha"],
                },
            )
            stale = client.post(
                "/api/agent-registry/native-owner-a/native-candidate",
                json={
                    "agent_data": {**next_data, "system_prompt": "stale overwrite"},
                    "change_set_id": first_a["change_set_id"],
                    "expected_candidate_commit_sha": first_a["candidate_commit_sha"],
                },
            )
            incomplete = client.post(
                "/api/agent-registry/native-owner-a/native-candidate",
                json={"agent_data": next_data, "change_set_id": first_a["change_set_id"]},
            )
            ambiguous = client.post(
                "/api/agent-registry/native-owner-a/native-candidate",
                json={
                    "agent_data": next_data,
                    "expected_current_commit_sha": first_a["base_commit_sha"],
                    "change_set_id": first_a["change_set_id"],
                    "expected_candidate_commit_sha": advanced.json()["candidate_commit_sha"],
                },
            )
            module.agent_governance.reject_change_set(first_a["change_set_id"], operator="reviewer")
            terminal = client.post(
                "/api/agent-registry/native-owner-a/native-candidate",
                json={
                    "agent_data": next_data,
                    "change_set_id": first_a["change_set_id"],
                    "expected_candidate_commit_sha": advanced.json()["candidate_commit_sha"],
                },
            )

    assert advanced.status_code == 200, advanced.text
    assert duplicate.status_code == 409
    assert duplicate.json()["error_code"] == "CANDIDATE_CONTINUATION_REQUIRED"
    assert wrong_owner.status_code == 409
    assert wrong_owner.json()["error_code"] == "CANDIDATE_CHANGE_SET_OWNER_CONFLICT"
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "CANDIDATE_COMMIT_CONFLICT"
    assert incomplete.status_code == 422
    assert ambiguous.status_code == 422
    assert terminal.status_code == 409
    assert terminal.json()["error_code"] == "CANDIDATE_CHANGE_SET_NOT_EDITABLE"
    assert len(module.agent_governance.list_change_sets(agent_id="native-owner-a")) == 1
    assert len(module.agent_governance.list_change_sets(agent_id="native-owner-b")) == 1
    assert first_a["change_set_id"] != first_b["change_set_id"]
