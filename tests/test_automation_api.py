"""四阶段改进治理 W2-a：/api/automation-policy + /api/improvements/{id}/auto-advance API 验收。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from test_api_execution_optimizer import _load_app


def _create(client, title: str = "auto 改进事项") -> str:
    return client.post("/api/improvements", json={"agent_id": "soc-ops", "title": title}).json()["improvement_id"]


def test_policy_default_off_and_set_get(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        assert client.get("/api/automation-policy", params={"agent_id": "soc-ops"}).json()["mode"] == "off"
        assert client.put("/api/automation-policy", json={"agent_id": "soc-ops", "mode": "semi"}).status_code == 200
        assert client.get("/api/automation-policy", params={"agent_id": "soc-ops"}).json()["mode"] == "semi"
        # 非法 mode 400。
        assert client.put("/api/automation-policy", json={"agent_id": "soc-ops", "mode": "bogus"}).status_code == 400


def test_auto_advance_respects_policy(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        improvement_id = _create(client)
        # 默认 off：不推进。
        off = client.post(f"/api/improvements/{improvement_id}/auto-advance")
        assert off.status_code == 200
        assert off.json()["stopped_reason"] == "policy_off"
        assert off.json()["improvement"]["improvement_stage"] == "feedback_intake"

        # semi：自动推进到 attribution 门停。
        client.put("/api/automation-policy", json={"agent_id": "soc-ops", "mode": "semi"})
        semi = client.post(f"/api/improvements/{improvement_id}/auto-advance").json()
        assert semi["applied_stages"] == ["triage", "attribution"]
        assert semi["stopped_reason"] == "gate_confirmation"
        assert semi["improvement"]["improvement_stage"] == "attribution"

        # full：从 attribution 继续推进到 regression（发布门禁前）。
        client.put("/api/automation-policy", json={"agent_id": "soc-ops", "mode": "full"})
        full = client.post(f"/api/improvements/{improvement_id}/auto-advance").json()
        assert full["applied_stages"] == ["optimization", "execution", "regression"]
        assert full["stopped_reason"] == "release_gate"
        assert full["improvement"]["improvement_stage"] == "regression"


def test_auto_advance_unknown_is_404(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        assert client.post("/api/improvements/imp-unknown/auto-advance").status_code == 404
