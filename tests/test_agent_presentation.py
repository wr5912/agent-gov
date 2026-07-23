from __future__ import annotations

import logging
from pathlib import Path

from fastapi.testclient import TestClient

from app_test_utils import load_test_app
from business_agent_test_utils import ORDINARY_TEST_AGENT_ID


def _load_agent(monkeypatch, tmp_path: Path):
    module = load_test_app(
        monkeypatch,
        tmp_path,
        extra_agent_ids=(ORDINARY_TEST_AGENT_ID,),
    )
    record = module.agent_registry_store.get_agent(ORDINARY_TEST_AGENT_ID)
    assert record is not None
    return module, record, Path(record.workspace_dir)


def test_presentation_projects_only_whitelisted_manifest_fields_and_registry_identity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module, record, workspace = _load_agent(monkeypatch, tmp_path)
    workspace.joinpath("agent.yaml").write_text(
        """
agent:
  id: hostile-agent-id
  name: Hostile Agent Name
  version: 2.3.4
  language: zh-CN
  runtime: claude-code
capabilities:
  - analysis
  - report_generation
presentation:
  summary: 结构化展示摘要
  welcome_message: |
    **静态开场内容**

    请提供任务背景。
  composer_placeholder: 输入任务背景和目标
  starter_prompts:
    - label: 开始分析
      prompt: 请分析我接下来提供的问题。
paths:
  workspace: /private/workspace
mcp:
  api_key: must-not-be-projected
""".lstrip(),
        encoding="utf-8",
    )

    with TestClient(module.app) as client:
        response = client.get(f"/api/agent-registry/{ORDINARY_TEST_AGENT_ID}/presentation")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "agent_id": ORDINARY_TEST_AGENT_ID,
        "name": record.name,
        "version": "2.3.4",
        "language": "zh-CN",
        "runtime": "claude-code",
        "capabilities": ["analysis", "report_generation"],
        "summary": "结构化展示摘要",
        "welcome_message": "**静态开场内容**\n\n请提供任务背景。",
        "composer_placeholder": "输入任务背景和目标",
        "starter_prompts": [{"label": "开始分析", "prompt": "请分析我接下来提供的问题。"}],
        "source": "agent_yaml",
    }
    serialized = response.text
    assert "hostile-agent-id" not in serialized
    assert "Hostile Agent Name" not in serialized
    assert "must-not-be-projected" not in serialized
    assert "/private/workspace" not in serialized


def test_presentation_missing_or_invalid_manifest_returns_registry_fallback(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    module, record, workspace = _load_agent(monkeypatch, tmp_path)

    with caplog.at_level(logging.WARNING, logger="app.services.business_agent_presentation"):
        with TestClient(module.app) as client:
            missing = client.get(f"/api/agent-registry/{ORDINARY_TEST_AGENT_ID}/presentation")
            workspace.joinpath("agent.yaml").write_text("presentation: [invalid", encoding="utf-8")
            invalid = client.get(f"/api/agent-registry/{ORDINARY_TEST_AGENT_ID}/presentation")

    for response in (missing, invalid):
        assert response.status_code == 200
        assert response.json() == {
            "agent_id": ORDINARY_TEST_AGENT_ID,
            "name": record.name,
            "version": None,
            "language": None,
            "runtime": None,
            "capabilities": [],
            "summary": None,
            "welcome_message": None,
            "composer_placeholder": None,
            "starter_prompts": [],
            "source": "registry_fallback",
        }
    assert f"agent_id={ORDINARY_TEST_AGENT_ID} reason=missing" in caplog.text
    assert f"agent_id={ORDINARY_TEST_AGENT_ID} reason=invalid_yaml" in caplog.text


def test_presentation_symlink_and_oversized_manifest_fail_closed(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    module, _, workspace = _load_agent(monkeypatch, tmp_path)
    manifest = workspace / "agent.yaml"
    outside = tmp_path / "outside-agent.yaml"
    outside.write_text("presentation:\n  summary: must not be read\n", encoding="utf-8")
    manifest.symlink_to(outside)

    with caplog.at_level(logging.WARNING, logger="app.services.business_agent_presentation"):
        with TestClient(module.app) as client:
            symlinked = client.get(f"/api/agent-registry/{ORDINARY_TEST_AGENT_ID}/presentation")
            manifest.unlink()
            manifest.write_text("#" * (129 * 1024), encoding="utf-8")
            oversized = client.get(f"/api/agent-registry/{ORDINARY_TEST_AGENT_ID}/presentation")

    assert symlinked.status_code == 200
    assert oversized.status_code == 200
    assert symlinked.json()["source"] == "registry_fallback"
    assert oversized.json()["source"] == "registry_fallback"
    assert "must not be read" not in symlinked.text
    assert f"agent_id={ORDINARY_TEST_AGENT_ID} reason=symlink" in caplog.text
    assert f"agent_id={ORDINARY_TEST_AGENT_ID} reason=too_large" in caplog.text


def test_presentation_unknown_agent_returns_404(monkeypatch, tmp_path: Path) -> None:
    module, _, _ = _load_agent(monkeypatch, tmp_path)

    with TestClient(module.app) as client:
        response = client.get("/api/agent-registry/unknown-agent/presentation")

    assert response.status_code == 404
    assert response.json()["error_code"] == "NOT_FOUND"
