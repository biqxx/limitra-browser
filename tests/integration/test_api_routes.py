from __future__ import annotations

from unittest.mock import AsyncMock, patch

from httpx import AsyncClient

from app.schemas.products import NormalizedProduct, ProductAvailability, SellerInfo
from app.schemas.search import ProductSearchResponse, SearchResultItem


async def test_health_endpoints(async_client: AsyncClient):
    # Liveness
    resp_live = await async_client.get("/health/live")
    assert resp_live.status_code == 200
    assert resp_live.json() == {"status": "alive"}

    # Readiness
    resp_ready = await async_client.get("/health/ready")
    assert resp_ready.status_code == 200
    assert resp_ready.json()["status"] == "ready"

    # Metrics
    resp_metrics = await async_client.get("/metrics")
    assert resp_metrics.status_code == 200
    assert "supplier_requests_total" in resp_metrics.text


async def test_api_key_authentication_enforcement(async_client: AsyncClient):
    # Missing API Key -> 401 Unauthorized
    resp_unauth = await async_client.get("/api/v1/providers")
    assert resp_unauth.status_code == 401
    data = resp_unauth.json()
    assert data["error"]["code"] == "unauthorized"

    # Invalid API Key -> 401 Unauthorized
    resp_bad = await async_client.get(
        "/api/v1/providers",
        headers={"X-API-Key": "invalid_key_xyz"},
    )
    assert resp_bad.status_code == 401
    assert resp_bad.json()["error"]["code"] == "unauthorized"

    # Valid Bootstrap Key -> 200 OK
    resp_ok = await async_client.get(
        "/api/v1/providers",
        headers={"X-API-Key": "test_admin_api_key_12345"},
    )
    assert resp_ok.status_code == 200
    providers = resp_ok.json()
    assert any(p["provider"] == "ebay" for p in providers)
    assert any(p["provider"] == "amazon" for p in providers)


async def test_provider_health_endpoint(async_client: AsyncClient, auth_headers: dict[str, str]):
    resp = await async_client.get("/api/v1/providers/ebay/health", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "ebay"
    assert "status" in data
    assert "supported_domains" in data


async def test_search_endpoint_with_mocked_adapter(
    async_client: AsyncClient, auth_headers: dict[str, str]
):
    mock_response = ProductSearchResponse(
        provider="ebay",
        query="wireless headphones",
        page=1,
        page_size=10,
        total_results=1,
        items=[
            SearchResultItem(
                provider="ebay",
                external_id="123456",
                title="Mock Test Headphones",
                url="https://www.ebay.com/itm/123456",
                price=99.99,
                currency="USD",
            )
        ],
    )

    with patch(
        "app.suppliers.ebay.adapter.EbayAdapter.search",
        new=AsyncMock(return_value=mock_response),
    ):
        req_payload = {
            "provider": "ebay",
            "query": "wireless headphones",
            "page": 1,
            "page_size": 10,
        }
        resp = await async_client.post("/api/v1/search", json=req_payload, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["provider"] == "ebay"
        assert len(data["items"]) == 1
        assert data["items"][0]["title"] == "Mock Test Headphones"


async def test_product_retrieve_endpoint_with_mocked_adapter(
    async_client: AsyncClient, auth_headers: dict[str, str]
):
    mock_product = NormalizedProduct(
        provider="amazon",
        external_id="B08N5WRWNW",
        canonical_url="https://www.amazon.com/dp/B08N5WRWNW",
        title="Mock Amazon Product",
        current_price=299.99,
        currency="USD",
        availability=ProductAvailability.IN_STOCK,
        seller=SellerInfo(name="Amazon.com"),
    )

    with patch(
        "app.suppliers.amazon.adapter.AmazonAdapter.get_product",
        new=AsyncMock(return_value=mock_product),
    ):
        req_payload = {
            "provider": "amazon",
            "external_id": "B08N5WRWNW",
        }
        resp = await async_client.post(
            "/api/v1/products/retrieve", json=req_payload, headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["provider"] == "amazon"
        assert data["external_id"] == "B08N5WRWNW"
        assert data["current_price"] == 299.99


async def test_structured_error_format(async_client: AsyncClient, auth_headers: dict[str, str]):
    # Unsupported provider
    req_payload = {
        "provider": "nonexistent_ecommerce_xyz",
        "query": "test query",
    }
    resp = await async_client.post("/api/v1/search", json=req_payload, headers=auth_headers)
    assert resp.status_code == 400
    data = resp.json()
    assert "error" in data
    assert data["error"]["code"] == "unsupported_provider"
    assert "request_id" in data["error"]
    assert "details" in data["error"]
