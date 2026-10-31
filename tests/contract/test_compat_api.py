from __future__ import annotations

from unittest.mock import AsyncMock, patch

from httpx import AsyncClient

from app.api.errors import ProductNotFoundException
from app.schemas.products import (
    NormalizedProduct,
    ProductAvailability,
    ProductImage,
    ProductVariant,
    ShippingOption,
)
from app.schemas.search import ProductSearchResponse, SearchResultItem


def _make_mock_product(
    provider: str,
    external_id: str,
    price: float = 29.99,
    weight_spec: str = "2.5 lbs",
) -> NormalizedProduct:
    return NormalizedProduct(
        provider=provider,
        external_id=external_id,
        canonical_url=f"https://www.{provider}.com/item/{external_id}",
        title=f"Sample {provider.title()} Product {external_id}",
        description="A great product description.",
        current_price=price,
        currency="USD",
        availability=ProductAvailability.IN_STOCK,
        images=[ProductImage(url=f"https://img.{provider}.com/{external_id}.jpg")],
        specifications={"Item Weight": weight_spec},
        shipping_options=[
            ShippingOption(
                name="Ground",
                price=0.0,
                estimated_days_min=2,
                estimated_days_max=5,
            )
        ],
        variants=[
            ProductVariant(
                external_id=f"{external_id}-v1",
                title="Color Black",
                selected_options={"Color": "Black", "Size": "M"},
                price=price,
                availability=ProductAvailability.IN_STOCK,
            )
        ],
    )


def _make_mock_search_response(
    provider: str,
    query: str,
    count: int = 2,
) -> ProductSearchResponse:
    items = [
        SearchResultItem(
            provider=provider,
            external_id=f"{provider}-item-{i}",
            title=f"{provider.title()} {query} Item #{i}",
            url=f"https://www.{provider}.com/item/{provider}-item-{i}",
            price=19.99 + i * 10,
            currency="USD",
            thumbnail_url=f"https://img.{provider}.com/{i}.jpg",
            in_stock=True,
        )
        for i in range(1, count + 1)
    ]
    return ProductSearchResponse(
        provider=provider,
        query=query,
        page=1,
        page_size=20,
        total_results=count,
        items=items,
    )


async def test_compat_health_endpoints(async_client: AsyncClient):
    # GET /health
    resp = await async_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "limitra-supplier-service"}

    # HEAD /health
    head_resp = await async_client.head("/health")
    assert head_resp.status_code == 200

    # GET /health/detailed
    det_resp = await async_client.get("/health/detailed")
    assert det_resp.status_code == 200
    data = det_resp.json()
    assert data["service"] == "limitra-supplier-service"
    assert "status" in data
    assert isinstance(data["dependencies"], list)
    assert any(d["name"] == "postgres" for d in data["dependencies"])
    assert any(d["name"] == "redis" for d in data["dependencies"])
    assert any(d["name"] == "amazon" for d in data["dependencies"])
    assert any(d["name"] == "ebay" for d in data["dependencies"])


async def test_compat_internal_auth_enforcement(async_client: AsyncClient):
    # Missing secret header -> 401
    resp_missing = await async_client.get("/search?q=test")
    assert resp_missing.status_code == 401
    assert "missing internal service secret" in resp_missing.text

    # Invalid secret header -> 401
    resp_invalid = await async_client.get(
        "/search?q=test",
        headers={"X-Internal-Service-Secret": "wrong-secret"},
    )
    assert resp_invalid.status_code == 401
    assert "invalid internal service secret" in resp_invalid.text


async def test_compat_search_validation(
    async_client: AsyncClient, internal_auth_headers: dict[str, str]
):
    # Missing query parameter
    resp_no_q = await async_client.get("/search", headers=internal_auth_headers)
    assert resp_no_q.status_code == 422 or resp_no_q.status_code == 400

    # Invalid supplier
    resp_bad_sup = await async_client.get(
        "/search?q=test&supplier=walmart",
        headers=internal_auth_headers,
    )
    assert resp_bad_sup.status_code == 400
    assert "supplier must be all, amazon, or ebay" in resp_bad_sup.text


async def test_compat_search_single_provider(
    async_client: AsyncClient, internal_auth_headers: dict[str, str]
):
    mock_resp = _make_mock_search_response("amazon", "iphone", 2)
    with patch(
        "app.services.search_service.SearchService.search",
        new=AsyncMock(return_value=mock_resp),
    ):
        resp = await async_client.get(
            "/search?supplier=amazon&q=iphone",
            headers=internal_auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["supplier"] == "amazon"
        assert data["total"] == 2
        assert len(data["products"]) == 2
        p0 = data["products"][0]
        assert p0["supplier"] == "amazon"
        assert p0["price"] > 0
        assert p0["us_delivery_days_max"] is None
        assert p0["weight_lb"] is None


async def test_compat_search_all_fanout(
    async_client: AsyncClient, internal_auth_headers: dict[str, str]
):
    amz_mock = _make_mock_search_response("amazon", "laptop", 2)
    ebay_mock = _make_mock_search_response("ebay", "laptop", 3)

    async def _mock_search(req, *args, **kwargs):
        if req.provider == "amazon":
            return amz_mock
        return ebay_mock

    with patch(
        "app.services.search_service.SearchService.search",
        new=AsyncMock(side_effect=_mock_search),
    ):
        resp = await async_client.get(
            "/search?supplier=all&q=laptop",
            headers=internal_auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["supplier"] == "all"
        assert data["total"] == 5
        assert len(data["products"]) == 5
        assert data.get("warnings") is None


async def test_compat_search_all_partial_failure(
    async_client: AsyncClient, internal_auth_headers: dict[str, str]
):
    amz_mock = _make_mock_search_response("amazon", "shoes", 2)

    async def _mock_search(req, *args, **kwargs):
        if req.provider == "amazon":
            return amz_mock
        raise RuntimeError("eBay upstream connection timeout")

    with patch(
        "app.services.search_service.SearchService.search",
        new=AsyncMock(side_effect=_mock_search),
    ):
        resp = await async_client.get(
            "/search?supplier=all&q=shoes",
            headers=internal_auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["supplier"] == "all"
        assert data["total"] == 2
        assert len(data["products"]) == 2
        assert "ebay: unavailable" in data.get("warnings", [])


async def test_compat_search_all_all_failed(
    async_client: AsyncClient, internal_auth_headers: dict[str, str]
):
    with patch(
        "app.services.search_service.SearchService.search",
        new=AsyncMock(side_effect=RuntimeError("Connection refused")),
    ):
        resp = await async_client.get(
            "/search?supplier=all&q=shoes",
            headers=internal_auth_headers,
        )
        assert resp.status_code == 502
        assert "all suppliers failed" in resp.text


async def test_compat_search_dedicated_endpoints(
    async_client: AsyncClient, internal_auth_headers: dict[str, str]
):
    mock_amz = _make_mock_search_response("amazon", "watch", 1)
    mock_ebay = _make_mock_search_response("ebay", "watch", 1)

    with patch(
        "app.services.search_service.SearchService.search",
        new=AsyncMock(return_value=mock_amz),
    ):
        resp_amz = await async_client.get(
            "/search/amazon?q=watch",
            headers=internal_auth_headers,
        )
        assert resp_amz.status_code == 200
        assert resp_amz.json()["total"] == 1

    with patch(
        "app.services.search_service.SearchService.search",
        new=AsyncMock(return_value=mock_ebay),
    ):
        resp_ebay = await async_client.get(
            "/search/ebay?q=watch",
            headers=internal_auth_headers,
        )
        assert resp_ebay.status_code == 200
        assert resp_ebay.json()["total"] == 1


async def test_compat_details_and_import(
    async_client: AsyncClient, internal_auth_headers: dict[str, str]
):
    mock_prod = _make_mock_product("amazon", "B08N5WRWNW", 99.99, "3.5 lbs")

    with patch(
        "app.services.product_service.ProductService.retrieve_product",
        new=AsyncMock(return_value=mock_prod),
    ):
        # GET /details/amazon/:asin
        resp_details = await async_client.get(
            "/details/amazon/B08N5WRWNW",
            headers=internal_auth_headers,
        )
        assert resp_details.status_code == 200
        data = resp_details.json()
        assert data["supplier"] == "amazon"
        assert data["supplier_id"] == "B08N5WRWNW"
        assert data["price"] == 99.99
        assert data["weight_lb"] == 3.5
        assert round(data["weight_kg"], 2) == round(3.5 / 2.205, 2)
        assert data["us_delivery_days_min"] == 2
        assert data["us_delivery_days_max"] == 5
        assert len(data["variants"]) == 1
        assert data["variants"][0]["id"] == "B08N5WRWNW-v1"

        # POST /import/amazon/:asin (compatibility alias)
        resp_import = await async_client.post(
            "/import/amazon/B08N5WRWNW",
            headers=internal_auth_headers,
        )
        assert resp_import.status_code == 200
        assert resp_import.json()["supplier_id"] == "B08N5WRWNW"


async def test_compat_details_missing_facts_remain_null(
    async_client: AsyncClient, internal_auth_headers: dict[str, str]
):
    # Product with no weight specifications and no shipping delivery estimates
    mock_prod = NormalizedProduct(
        provider="amazon",
        external_id="B09XYZ1234",
        canonical_url="https://www.amazon.com/dp/B09XYZ1234",
        title="Product Without Stated Weight or Delivery",
        current_price=19.99,
        currency="USD",
        availability=ProductAvailability.IN_STOCK,
        specifications={},
        shipping_options=[],
        estimated_delivery=None,
    )

    with patch(
        "app.services.product_service.ProductService.retrieve_product",
        new=AsyncMock(return_value=mock_prod),
    ):
        resp = await async_client.get(
            "/details/amazon/B09XYZ1234",
            headers=internal_auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["weight_lb"] is None
        assert data["weight_kg"] is None
        assert data["us_delivery_days_min"] is None
        assert data["us_delivery_days_max"] is None


async def test_compat_details_not_found(
    async_client: AsyncClient, internal_auth_headers: dict[str, str]
):
    with patch(
        "app.services.product_service.ProductService.retrieve_product",
        new=AsyncMock(side_effect=ProductNotFoundException("Not found", provider="amazon")),
    ):
        resp = await async_client.get(
            "/details/amazon/NONEXISTENT",
            headers=internal_auth_headers,
        )
        assert resp.status_code == 404
        assert "product not found" in resp.text


async def test_compat_availability(
    async_client: AsyncClient, internal_auth_headers: dict[str, str]
):
    mock_prod = _make_mock_product("amazon", "B08N5WRWNW", 45.0)

    with patch(
        "app.services.product_service.ProductService.retrieve_product",
        new=AsyncMock(return_value=mock_prod),
    ):
        payload = {"supplier": "amazon", "supplier_id": "B08N5WRWNW"}
        resp = await async_client.post(
            "/availability",
            json=payload,
            headers=internal_auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["supplier"] == "amazon"
        assert data["supplier_id"] == "B08N5WRWNW"
        assert data["available"] is True
        assert data["price"] == 45.0
        assert "checked_at" in data


async def test_compat_quick_buy_sign_and_verify(
    async_client: AsyncClient, internal_auth_headers: dict[str, str]
):
    order_payload = {
        "supplier": "amazon",
        "supplier_id": "B08N5WRWNW",
        "quantity": 1,
        "address": {
            "full_name": "John Doe",
            "line1": "123 Main St",
            "city": "New York",
            "state": "NY",
            "zip_code": "10001",
        },
    }

    # 1. Sign payload
    resp_sign = await async_client.post(
        "/quick-buy/payload",
        json=order_payload,
        headers=internal_auth_headers,
    )
    assert resp_sign.status_code == 200
    signed = resp_sign.json()
    assert signed["nonce"] != ""
    assert signed["signature"] != ""
    assert "expires_at" in signed

    # 2. Verify payload (first time -> valid)
    resp_verify = await async_client.post(
        "/quick-buy/verify",
        json=signed,
        headers=internal_auth_headers,
    )
    assert resp_verify.status_code == 200
    assert resp_verify.json()["valid"] is True

    # 3. Replay attack: verify second time with same nonce -> invalid
    resp_replay = await async_client.post(
        "/quick-buy/verify",
        json=signed,
        headers=internal_auth_headers,
    )
    assert resp_replay.status_code == 200
    assert resp_replay.json()["valid"] is False

    # 4. Tampered payload -> invalid
    tampered = dict(signed)
    tampered["signature"] = "tampered_signature_hex_12345"
    resp_tampered = await async_client.post(
        "/quick-buy/verify",
        json=tampered,
        headers=internal_auth_headers,
    )
    assert resp_tampered.status_code == 200
    assert resp_tampered.json()["valid"] is False


async def test_compat_order_flow(async_client: AsyncClient, internal_auth_headers: dict[str, str]):
    mock_prod = _make_mock_product("amazon", "B08N5WRWNW", 59.99)

    with patch(
        "app.services.product_service.ProductService.retrieve_product",
        new=AsyncMock(return_value=mock_prod),
    ):
        order_payload = {
            "supplier": "amazon",
            "supplier_id": "B08N5WRWNW",
            "quantity": 1,
            "address": {
                "full_name": "Jane Smith",
                "line1": "456 Oak Ave",
                "city": "Austin",
                "state": "TX",
                "zip_code": "78701",
            },
        }

        # 1. Place order -> 501 Not Implemented
        resp_order = await async_client.post(
            "/order",
            json=order_payload,
            headers=internal_auth_headers,
        )
        assert resp_order.status_code == 501
        assert "not implemented" in resp_order.json()["detail"]["error"].lower()

        # 2. Confirm order -> 501 Not Implemented
        resp_confirm = await async_client.post(
            "/order/dummy-session/confirm",
            headers=internal_auth_headers,
        )
        assert resp_confirm.status_code == 501
        assert "not implemented" in resp_confirm.json()["detail"]["error"].lower()
