"""四阶段改进治理 W3：/api/assets Registry + 跨 Agent 继承复用 API 验收。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from test_api_execution_optimizer import _load_app


def test_asset_registry_and_inheritance(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = client.post("/api/assets", json={"agent_id": "soc-ops", "asset_type": "methodology", "title": "误报归因法", "body": "步骤"})
        assert created.status_code == 201
        asset_id = created.json()["asset_id"]
        # 列表按 agent + type 过滤。
        assert asset_id in {a["asset_id"] for a in client.get("/api/assets", params={"agent_id": "soc-ops"}).json()}
        assert asset_id in {a["asset_id"] for a in client.get("/api/assets", params={"agent_id": "soc-ops", "asset_type": "methodology"}).json()}
        assert client.get("/api/assets", params={"agent_id": "soc-ops", "asset_type": "regression"}).status_code == 400
        assert (
            client.post(
                "/api/assets",
                json={"agent_id": "soc-ops", "asset_type": "regression", "title": "旧独立回归资产"},
            ).status_code
            == 400
        )
        dataset = client.post(
            "/api/assets",
            json={"agent_id": "soc-ops", "asset_type": "test_dataset", "title": "时间窗口测试数据集", "body": '{"test_dataset_id":"tds-1"}'},
        )
        assert dataset.status_code == 400
        assert client.get("/api/assets", params={"agent_id": "soc-ops", "asset_type": "test_dataset"}).status_code == 400
        # 详情 404。
        assert client.get("/api/assets/ast-nope").status_code == 404
        # 非法类型 400。
        assert client.post("/api/assets", json={"agent_id": "soc-ops", "asset_type": "bogus", "title": "x"}).status_code == 400
        # 继承到另一个 Agent：复利。
        inherited = client.post(f"/api/assets/{asset_id}/inherit", json={"target_agent_id": "shop-bot"})
        assert inherited.status_code == 201
        body = inherited.json()
        assert body["agent_id"] == "shop-bot" and body["inherited_from"] == asset_id and body["title"] == "误报归因法"
        # 同 Agent 继承 400；未知资产继承 404。
        assert client.post(f"/api/assets/{asset_id}/inherit", json={"target_agent_id": "soc-ops"}).status_code == 400
        assert client.post("/api/assets/ast-nope/inherit", json={"target_agent_id": "x"}).status_code == 404
