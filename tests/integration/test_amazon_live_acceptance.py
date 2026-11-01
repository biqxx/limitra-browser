from __future__ import annotations

import pytest

from app.browser.manager import get_browser_manager
from app.config import get_settings
from app.schemas.products import ProductRetrievalRequest
from app.schemas.search import ProductSearchRequest
from app.services.product_service import ProductService
from app.services.search_service import SearchService


@pytest.mark.asyncio
async def test_amazon_live_acceptance_read_only() -> None:
    """Read-only search and details check; never adds to cart or purchases."""
    if not get_settings().RUN_LIVE_SUPPLIER_TESTS:
        pytest.skip("Set RUN_LIVE_SUPPLIER_TESTS=true to run live supplier acceptance tests.")

    try:
        search = await SearchService().search(
            ProductSearchRequest(
                provider="amazon",
                query="wireless headphones",
                country="US",
                locale="en-US",
                currency="USD",
                page_size=10,
            ),
            force_refresh=True,
        )
        assert search.items
        priced = next((item for item in search.items if item.price is not None), None)
        assert priced is not None
        assert priced.price is not None and priced.price > 0
        assert priced.currency == "USD"

        product = await ProductService().retrieve_product(
            ProductRetrievalRequest(
                provider="amazon",
                external_id=priced.external_id,
                country="US",
                locale="en-US",
                currency="USD",
                force_refresh=True,
            )
        )
        assert product.title
        assert product.current_price is not None and product.current_price > 0
        assert product.currency == "USD"
        assert product.data_quality.price_verified is True
        assert product.images
    finally:
        await get_browser_manager().shutdown()
