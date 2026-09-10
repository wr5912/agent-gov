from __future__ import annotations

import asyncio
import hashlib
import io
import tarfile
from email import policy
from email.parser import BytesParser

import httpx
import pytest
import yaml
from agentscope_runtime.policy_middleware import AgentGovPolicyMiddleware
from agentscope_runtime.subagent_templates import load_subagent_templates
from app.runtime.agent_governance_schemas import AgentDeleteResponse, AgentDeletionImpact, AgentSummaryResponse
from app.runtime.agent_workspace_package_schemas import WorkspaceImportResponse
from app.runtime.config_mapping import RUNTIME_CONTRACT
from app.runtime_gateway.provisioning import agent_payload_from_workspace, session_settings_from_workspace
from fastapi.testclient import TestClient
from scripts import runtime_acceptance_fixture as fixture

from app_test_utils import load_test_app


def _agent(agent_id: str) -> AgentSummaryResponse:
    return AgentSummaryResponse(
        agent_id=agent_id,
        name="通用 Runtime 临时验收",
        category="business",
        workspace_dir="/isolated/workspace",
        created_at="2026-01-01T00:00:00Z",
    )


def _package_from_request(request: httpx.Request) -> bytes:
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content,
    )
    parts = {part.get_param("name", header="content-disposition"): part for part in message.iter_parts()}
    assert set(parts) == {"name", "package"}
    assert parts["package"].get_filename().endswith(".tar.gz")
    assert int(request.headers["content-length"]) == len(request.content)
    return parts["package"].get_payload(decode=True)


class FixtureApi:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.import_action = "created"
        self.import_status = 200
        self.delete_status = 200
        self.delete_fields: dict[str, object] = {}
        self.omit_cleanup = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        agent_id = request.url.path.split("/")[3]
        assert agent_id.startswith("runtime-acceptance-")
        if request.method == "POST":
            assert request.url.path.endswith("/workspace/import")
            package = _package_from_request(request)
            result = WorkspaceImportResponse(
                action=self.import_action,
                agent=_agent(agent_id),
                current_commit_sha="a" * 40,
                package_sha256=hashlib.sha256(package).hexdigest(),
                tree_sha256="b" * 64,
                import_record_id="import-one",
                test_suite_status="warning",
                test_file_count=0,
            )
            return httpx.Response(self.import_status, json=result.model_dump(mode="json"))
        assert request.method == "DELETE"
        result = AgentDeleteResponse(
            deleted=_agent(agent_id),
            impact=AgentDeletionImpact(runs=1, feedback_signals=1),
            workspace_removed=True,
            cleanup_complete=True,
        ).model_dump(mode="json")
        result.update(self.delete_fields)
        if self.omit_cleanup:
            result.pop("cleanup_complete")
        return httpx.Response(self.delete_status, json=result)


@pytest.fixture
def fixture_api(monkeypatch):
    monkeypatch.setenv(fixture.ACTIVE_ENV, "1")
    monkeypatch.setenv(fixture.RUN_ID_ENV, "fixture-unit-test")
    monkeypatch.setenv(fixture.PROFILE_ENV, "langfuse")
    return FixtureApi()


def test_fixture_package_satisfies_runtime_policy_without_external_capabilities(tmp_path) -> None:
    agent_id = "runtime-acceptance-package"
    package = fixture.build_fixture_package(agent_id)
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        assert set(archive.getnames()) == {"workspace", "workspace/AGENT.md", "workspace/agent.yaml"}
        assert all(member.isdir() or member.isfile() for member in archive.getmembers())
        for member in archive.getmembers():
            if member.isfile():
                content = archive.extractfile(member)
                assert content is not None
                (tmp_path / member.name.split("/")[-1]).write_bytes(content.read())
    manifest = yaml.safe_load((tmp_path / "agent.yaml").read_text())
    assert manifest["agent"]["id"] == agent_id
    assert manifest["agent"]["runtime_contract"] == RUNTIME_CONTRACT
    policy_config = manifest["workspace_policy"]
    assert policy_config["allowed_tools"] == policy_config["writable_paths"] == policy_config["allowed_network_domains"] == []
    assert policy_config["allow_for_run"] is False
    assert load_subagent_templates(tmp_path, "a" * 64) == {}
    AgentGovPolicyMiddleware(tmp_path)
    assert session_settings_from_workspace(tmp_path) == ("dont_ask", ".", "default")
    assert agent_payload_from_workspace(tmp_path, display_name="fixture")["name"] == "fixture"


def test_fixture_package_is_accepted_by_public_import_api(monkeypatch, tmp_path) -> None:
    module = load_test_app(monkeypatch, tmp_path)
    agent_id = "runtime-acceptance-import"
    with TestClient(module.app) as client:
        response = client.post(
            f"/api/agent-registry/{agent_id}/workspace/import",
            data={"name": "通用 Runtime 临时验收"},
            files={"package": ("fixture.tar.gz", fixture.build_fixture_package(agent_id), "application/gzip")},
        )
    assert response.status_code == 200
    imported = WorkspaceImportResponse.model_validate(response.json())
    assert imported.action == "created"
    assert imported.agent.agent_id == agent_id
    assert imported.agent.status == "active"
    assert imported.test_suite_status == "warning"


def _exercise(api: FixtureApi, *, primary_error: BaseException | None = None) -> str:
    async def run() -> str:
        async with httpx.AsyncClient(base_url="http://public-api.test", transport=httpx.MockTransport(api)) as client:
            async with fixture.temporary_runtime_agent(client) as imported:
                if primary_error is not None:
                    raise primary_error
                return imported.agent.agent_id

    return asyncio.run(run())


def test_fixture_creates_unique_agents_and_confirms_public_cleanup(fixture_api) -> None:
    first, second = _exercise(fixture_api), _exercise(fixture_api)
    assert first != second
    assert [request.method for request in fixture_api.requests] == ["POST", "DELETE", "POST", "DELETE"]
    assert fixture_api.requests[1].url.path == f"/api/agent-registry/{first}"
    assert fixture_api.requests[3].url.path == f"/api/agent-registry/{second}"


def test_fixture_cleans_up_when_primary_run_fails(fixture_api) -> None:
    error = RuntimeError("primary execution failed")
    with pytest.raises(RuntimeError) as caught:
        _exercise(fixture_api, primary_error=error)
    assert caught.value is error
    assert fixture_api.requests[-1].method == "DELETE"


@pytest.mark.parametrize("primary_error", [RuntimeError("original"), asyncio.CancelledError()])
def test_fixture_cleanup_failure_preserves_primary_error_without_sensitive_output(fixture_api, primary_error, capsys) -> None:
    fixture_api.delete_status = 503
    fixture_api.delete_fields = {"detail": "sensitive response text must not be printed"}
    with pytest.raises(type(primary_error)) as caught:
        _exercise(fixture_api, primary_error=primary_error)
    assert caught.value is primary_error
    output = capsys.readouterr().err
    assert "AGENTSCOPE_FIXTURE_CLEANUP_FAIL" in output
    assert "sensitive response" not in output


@pytest.mark.parametrize("bad_field", ["cleanup_complete", "workspace_removed", "missing_cleanup", "status"])
def test_fixture_cleanup_is_fail_closed_without_an_original_error(fixture_api, bad_field) -> None:
    if bad_field == "status":
        fixture_api.delete_status = 503
    elif bad_field == "missing_cleanup":
        fixture_api.omit_cleanup = True
    else:
        fixture_api.delete_fields[bad_field] = False
    with pytest.raises(fixture.FixtureAgentError, match="清理失败"):
        _exercise(fixture_api)


@pytest.mark.parametrize("action", ["overwritten", "unchanged"])
def test_fixture_never_deletes_an_unconfirmed_new_identity(fixture_api, action) -> None:
    fixture_api.import_action = action
    with pytest.raises(fixture.FixtureAgentError, match="全新独立 Harness"):
        _exercise(fixture_api)
    assert [request.method for request in fixture_api.requests] == ["POST"]


def test_fixture_import_conflict_does_not_delete_existing_agent(fixture_api) -> None:
    fixture_api.import_status = 409
    with pytest.raises(fixture.FixtureAgentError, match="公共导入失败"):
        _exercise(fixture_api)
    assert [request.method for request in fixture_api.requests] == ["POST"]


@pytest.mark.parametrize("missing", [fixture.ACTIVE_ENV, fixture.RUN_ID_ENV, fixture.PROFILE_ENV])
def test_fixture_rejects_execution_outside_isolated_runner(fixture_api, monkeypatch, missing) -> None:
    monkeypatch.delenv(missing)
    with pytest.raises(fixture.FixtureAgentError, match="公共 Make"):
        _exercise(fixture_api)
    assert fixture_api.requests == []
