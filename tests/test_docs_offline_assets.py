"""`/docs` 与 `/redoc` 必须完全自托管。

FastAPI 默认的 docs_url/redoc_url 会生成硬依赖公网 CDN 的 HTML
（cdn.jsdelivr.net、fonts.googleapis.com、fastapi.tiangolo.com）。这与
「离线模式是产品不变量；必需工作流不得依赖远程服务」冲突：内网浏览器取不到
swagger-ui-bundle.js 时，页面渲染被整条串行依赖门控，停在空白页——服务端本身
只占该页面总耗时的不到 1%。

本文件把「文档页不得引用外部主机」固化为契约。

已知残留（有意不处理）：ReDoc 运行时会取 `cdn.redoc.ly` 上的水印 logo，该 URL 硬编码在
redoc bundle 内部，不出现在 HTML 里，因此不被下列断言覆盖。它是渲染完成后加载的装饰性图片，
断网时只表现为裂图，不阻塞页面。消除它需要改写 vendored 资产，会在每次版本升级时留下补丁
维护负担，收益不成比例。
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from test_api_execution_optimizer import _load_app

# HTML 中出现的绝对 URL；协议相对（//host/path）同样算外部引用。
_ABSOLUTE_URL = re.compile(r"""(?:src|href)\s*=\s*["'](?:https?:)?//[^"']+["']""", re.IGNORECASE)
_LOCAL_ASSET = re.compile(r"""(?:src|href)\s*=\s*["'](/static/docs/[^"']+)["']""")

_DOC_ROUTES = ("/docs", "/redoc")


@pytest.fixture()
def client(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as test_client:
        yield test_client


@pytest.mark.parametrize("route", _DOC_ROUTES)
def test_doc_page_references_no_external_host(client: TestClient, route: str) -> None:
    response = client.get(route)
    assert response.status_code == 200

    external = [match.group(0) for match in _ABSOLUTE_URL.finditer(response.text)]
    assert not external, f"{route} 引用了外部资源，离线/内网无法渲染: {external}"


@pytest.mark.parametrize("route", _DOC_ROUTES)
def test_doc_page_assets_are_served_locally(client: TestClient, route: str) -> None:
    """页面引用的每个本地资源都必须真的能取到，避免只是把 CDN 换成 404。"""

    response = client.get(route)
    assert response.status_code == 200

    referenced = _LOCAL_ASSET.findall(response.text)
    assert referenced, f"{route} 未引用任何本地 /static/docs 资源"

    for asset in referenced:
        asset_response = client.get(asset)
        assert asset_response.status_code == 200, f"{route} 引用的 {asset} 取不到"
        assert asset_response.content, f"{asset} 为空文件"


def test_swagger_page_still_points_at_openapi_schema(client: TestClient) -> None:
    """自托管不得改变文档页与 OpenAPI 契约的绑定。"""

    response = client.get("/docs")
    assert response.status_code == 200
    assert "/openapi.json" in response.text


def test_google_fonts_not_referenced_by_redoc(client: TestClient) -> None:
    """ReDoc 默认注入 fonts.googleapis.com；必须由 with_google_fonts=False 关掉。"""

    response = client.get("/redoc")
    assert response.status_code == 200
    assert "fonts.googleapis.com" not in response.text
