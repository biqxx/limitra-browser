from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.errors import SupplierBlockedException
from app.browser.context import isolated_browser_session
from app.browser.navigation import check_for_supplier_block
from app.cache.service import CacheService
from app.schemas.products import NormalizedProduct, ProductAvailability, ProductVariant, SellerInfo
from app.security.auth import check_sliding_window_rate_limit
from app.suppliers.amazon.parser import AmazonParser
from app.suppliers.ebay.parser import EbayParser


def test_missing_and_changed_selectors_fallback():
    # HTML where traditional selectors are missing, but fallback/generic tags exist
    broken_html = """
    <html>
      <body>
        <div class="unknown-wrapper">
          <h1>Universal Title Found Here</h1>
          <span class="random-price-class">$49.95</span>
          <p class="random-desc">A great description of the item.</p>
        </div>
      </body>
    </html>
    """
    ebay_parser = EbayParser()
    prod_ebay = ebay_parser.parse_product_page(broken_html, "https://www.ebay.com/itm/999", "999")
    # Title falls back to generic h1
    assert "Universal Title Found Here" in prod_ebay.title
    assert prod_ebay.current_price == 0.0 or prod_ebay.current_price == 49.95

    amz_parser = AmazonParser()
    prod_amz = amz_parser.parse_product_page(
        broken_html, "https://www.amazon.com/dp/B000000000", "B000000000"
    )
    assert prod_amz.external_id == "B000000000"


def test_partial_product_data():
    minimal_html = "<html><body><h1>Minimal Item</h1></body></html>"
    ebay_parser = EbayParser()
    prod = ebay_parser.parse_product_page(minimal_html, "https://www.ebay.com/itm/111", "111")
    assert prod.title == "Minimal Item"
    assert prod.current_price == 0.0
    assert prod.availability == ProductAvailability.UNKNOWN
    assert prod.brand is None
    assert prod.description == ""
    assert len(prod.images) == 0


def test_product_with_many_variants():
    # Construct a product with 50 variant combinations
    variants = []
    colors = ["Red", "Blue", "Green", "Black", "White"]
    sizes = ["S", "M", "L", "XL", "XXL", "3XL", "4XL", "5XL", "6XL", "7XL"]
    idx = 1
    for c in colors:
        for s in sizes:
            variants.append(
                ProductVariant(
                    external_id=f"var_{idx}",
                    sku=f"SKU-{c[:2].upper()}-{s}",
                    title=f"T-Shirt {c} {s}",
                    selected_options={"color": c, "size": s},
                    price=19.99 + (1.0 if "XL" in s else 0.0),
                    currency="USD",
                    availability=ProductAvailability.IN_STOCK,
                )
            )
            idx += 1

    prod = NormalizedProduct(
        provider="ebay",
        external_id="12345678",
        canonical_url="https://www.ebay.com/itm/12345678",
        title="Custom Multi-Variant T-Shirt",
        current_price=19.99,
        currency="USD",
        variants=variants,
        seller=SellerInfo(name="ApparelDirect"),
    )

    assert len(prod.variants) == 50
    assert prod.variants[0].selected_options == {"color": "Red", "size": "S"}
    assert prod.variants[-1].selected_options == {"color": "White", "size": "7XL"}
    assert prod.variants[-1].price == 20.99


def test_sliding_window_rate_limiting():
    key = "test_rate_limit_client"
    # Allow 3 requests per 10 seconds
    assert check_sliding_window_rate_limit(key, max_requests=3, window_seconds=10) is True
    assert check_sliding_window_rate_limit(key, max_requests=3, window_seconds=10) is True
    assert check_sliding_window_rate_limit(key, max_requests=3, window_seconds=10) is True
    # 4th should exceed limit
    assert check_sliding_window_rate_limit(key, max_requests=3, window_seconds=10) is False


@pytest.mark.asyncio
async def test_cache_service_in_memory_fallback():
    cache = CacheService(redis_client=None)
    prod = NormalizedProduct(
        provider="amazon",
        external_id="B08N5WRWNW",
        canonical_url="https://www.amazon.com/dp/B08N5WRWNW",
        title="Cache Test Product",
        current_price=49.99,
        currency="USD",
        seller=SellerInfo(name="Amazon"),
    )

    # Set and get
    await cache.set_product(prod, version=1, ttl_seconds=60)
    cached = await cache.get_product("amazon", "B08N5WRWNW", "en-US", "US", "USD", version=1)
    assert cached is not None
    assert cached.title == "Cache Test Product"

    # Negative caching
    assert await cache.is_negatively_cached("amazon", "UNAVAIL123", "en-US", "US") is False
    await cache.set_negative_cache("amazon", "UNAVAIL123", "en-US", "US", ttl_seconds=60)
    assert await cache.is_negatively_cached("amazon", "UNAVAIL123", "en-US", "US") is True


@pytest.mark.asyncio
async def test_block_detection_raises_supplier_blocked():
    mock_page = MagicMock()
    mock_page.title = AsyncMock(return_value="Robot Check - Amazon.com")
    mock_page.content = AsyncMock(
        return_value="<html><body>Type the characters you see in this image</body></html>"
    )

    with pytest.raises(SupplierBlockedException) as exc:
        await check_for_supplier_block(mock_page, provider="amazon", status_code=200)

    assert "supplier_blocked" == exc.value.code.value
    assert "Use official supplier API" in exc.value.details.get("recommendation", "")


@pytest.mark.asyncio
async def test_browser_session_cleanup():
    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()

    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_page.route = AsyncMock()
    mock_page.close = AsyncMock()
    mock_context.close = AsyncMock()

    global_sem = asyncio.Semaphore(5)
    provider_sem = asyncio.Semaphore(2)

    async with isolated_browser_session(
        browser=mock_browser,
        provider="ebay",
        global_sem=global_sem,
        provider_sem=provider_sem,
    ) as page:
        assert page is mock_page

    # Verify cleanup occurred in finally block
    mock_page.close.assert_awaited_once()
    mock_context.close.assert_awaited_once()
    assert global_sem._value == 5
    assert provider_sem._value == 2
