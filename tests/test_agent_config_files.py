from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from app.routers.agent_config_files import create_agent_config_files_router
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.stores.feedback_store import FeedbackStore
from app.services.agent_candidate_writer import AgentCandidateWriteError, AgentCandidateWriter
from app.services.agent_governance import AgentGovernanceService
from fastapi import FastAPI
from fastapi.testclient import TestClient

from feedback_store_test_utils import _settings


def _services(tmp_path: Path):
    settings = _settings(tmp_path)
    version_store = GitAgentVersionStore(
        repository_dir=settings.default_workspace_dir,
        worktrees_dir=settings.agent_git_worktrees_dir,
        releases_dir=settings.agent_release_archives_dir,
    )
    feedback_store = FeedbackStore(data_dir=settings.data_dir, workspace_dir=settings.default_workspace_dir)
    governance = AgentGovernanceService(
        feedback_store=feedback_store,
        agent_version_store=version_store,
        runtime_mode="local-debug",
    )
    writer = AgentCandidateWriter(governance)
    change_set = governance.create_change_set(title="候选配置编辑", operator="tester")
    return settings, governance, writer, change_set


def _app(writer: AgentCandidateWriter) -> FastAPI:
    app = FastAPI()
    app.include_router(create_agent_config_files_router(candidate_writer=writer, require_api_key=lambda: None))
    return app


def test_candidate_file_route_commits_without_mutating_live_workspace(tmp_path: Path) -> None:
    settings, _governance, writer, change_set = _services(tmp_path)
    change_set_id = str(change_set["change_set_id"])
    live_prompt = settings.default_workspace_dir / "AGENT.md"
    original_live = live_prompt.read_text(encoding="utf-8")

    with TestClient(_app(writer)) as client:
        current = client.get(f"/api/agent-change-sets/{change_set_id}/files", params={"path": "AGENT.md"})
        response = client.put(
            f"/api/agent-change-sets/{change_set_id}/files",
            json={
                "expected_candidate_commit_sha": current.json()["candidate_commit_sha"],
                "files": [{"path": "AGENT.md", "content": "# Candidate only\n", "expected_sha256": current.json()["sha256"]}],
                "operator": "tester",
            },
        )
        removed_route = client.get(
            "/api/agent-config-file",
            params={"agent_id": DEFAULT_BUSINESS_AGENT_ID, "path": "AGENT.md"},
        )

    assert current.status_code == response.status_code == 200
    assert response.json()["change_set_id"] == change_set_id
    assert response.json()["published"] is False
    assert response.json()["changed_paths"] == ["AGENT.md"]
    assert response.json()["candidate_commit_sha"] != change_set["base_commit_sha"]
    assert live_prompt.read_text(encoding="utf-8") == original_live
    assert removed_route.status_code == 404


def test_candidate_batch_is_one_commit_and_rejects_stale_or_unsafe_input(tmp_path: Path) -> None:
    _settings_value, governance, writer, change_set = _services(tmp_path)
    change_set_id = str(change_set["change_set_id"])
    base = str(change_set["base_commit_sha"])
    url = f"/api/agent-change-sets/{change_set_id}/files"
    body = {
        "expected_candidate_commit_sha": base,
        "files": [
            {"path": "AGENT.md", "content": "# Reviewed candidate\n"},
            {"path": "mcp/reviewed.json", "content": '{"mcp_config":{"type":"http_mcp","url":"https://example.invalid/mcp"},"credential_refs":[]}\n'},
        ],
        "operator": "tester",
    }

    with TestClient(_app(writer)) as client:
        written = client.put(url, json=body)
        stale = client.put(
            url,
            json={"expected_candidate_commit_sha": base, "files": [{"path": "AGENT.md", "content": "# Different stale content\n"}]},
        )
        unsafe = client.put(
            url,
            json={"expected_candidate_commit_sha": written.json()["candidate_commit_sha"], "files": [{"path": "mcp/../escape.json", "content": "{}"}]},
        )
        process_spawn = client.put(
            url,
            json={"expected_candidate_commit_sha": written.json()["candidate_commit_sha"], "files": [{"path": "mcp/process.json", "content": '{"command":"node","args":[]}'}]},
        )

    assert written.status_code == 200
    candidate = written.json()["candidate_commit_sha"]
    assert governance._store_for(DEFAULT_BUSINESS_AGENT_ID).version_summary(candidate)["parent_version_id"] == base
    assert stale.status_code == 409
    assert unsafe.status_code == 422
    assert process_spawn.status_code == 422


def test_candidate_write_retry_recovers_lost_response_without_duplicate_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_value, governance, writer, change_set = _services(tmp_path)
    base = str(change_set["base_commit_sha"])
    original = governance.mark_candidate_committed
    lost = False

    def lose_first_response(*args, **kwargs):
        nonlocal lost
        result = original(*args, **kwargs)
        if not lost:
            lost = True
            raise RuntimeError("simulated response loss")
        return result

    monkeypatch.setattr(governance, "mark_candidate_committed", lose_first_response)
    request = dict(
        change_set_id=str(change_set["change_set_id"]),
        files=(("AGENT.md", "# Response-loss candidate\n", None, 0o644),),
        expected_candidate_commit_sha=base,
        operator="tester",
        note=None,
    )
    with pytest.raises(RuntimeError, match="response loss"):
        writer.write_text_files(**request)
    recovered = writer.write_text_files(**request)

    candidate = str(recovered["candidate_commit_sha"])
    assert recovered["published"] is False
    assert governance._store_for(DEFAULT_BUSINESS_AGENT_ID).version_summary(candidate)["parent_version_id"] == base


def test_candidate_compare_and_swap_allows_only_one_concurrent_command(tmp_path: Path) -> None:
    _settings_value, _governance, writer, change_set = _services(tmp_path)
    request = {
        "change_set_id": str(change_set["change_set_id"]),
        "expected_candidate_commit_sha": str(change_set["base_commit_sha"]),
        "operator": "tester",
        "note": None,
    }

    def write(label: str):
        try:
            return writer.write_text_files(files=(("AGENT.md", f"# {label}\n", None, 0o644),), **request)
        except AgentCandidateWriteError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ("first", "second")))

    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, AgentCandidateWriteError) for result in results) == 1
