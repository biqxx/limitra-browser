from __future__ import annotations

import pytest

from app.config import get_settings
from app.suppliers.ebay.api_client import EbayApiClient


@pytest.mark.asyncio
async def test_ebay_live_acceptance_read_only() -> None:
    """
    Live acceptance test against official eBay Buy Browse API.
    Rules:
    - Strictly read-only (search & get_item). No purchase, bid, or cart operations.
    - Credentials are never logged, printed, or exposed.
    - If credentials are not present in environment, skips gracefully.
    - Validates returned item fields and variants without inventing missing values.
    """
    if not get_settings().RUN_LIVE_SUPPLIER_TESTS:
        pytest.skip("Set RUN_LIVE_SUPPLIER_TESTS=true to run live supplier acceptance tests.")

    client = EbayApiClient()
    if not client.is_configured:
        pytest.skip(
            "eBay API credentials not configured in environment. "
            "Set EBAY_APP_ID and EBAY_CERT_ID to run live acceptance test."
        )

    try:
        # 1. Search for a common product
        search_query = "laptop stand"
        search_results = await client.search(search_query, page_size=5)

        assert search_results is not None, "Search response should not be None"
        assert len(search_results.items) > 0, f"Expected at least one item for query '{search_query}'"

        first_item = search_results.items[0]
        assert first_item.title, "First item must have a title"
        assert first_item.external_id, "First item must have an external_id"
        assert first_item.url and first_item.url.startswith("http"), "First item must have a valid URL"
        assert first_item.price is None or first_item.price > 0, "Price must be positive or truthful None"

        # 2. Retrieve detailed item by ID
        item_id = first_item.external_id
        detailed_product = await client.get_item(item_id)
        assert detailed_product is not None, f"Failed to retrieve detailed item for ID {item_id}"

        # 3. Verify fields
        assert detailed_product.title, "Item detail must have a title"
        assert detailed_product.external_id == item_id, "Item detail external_id must match requested ID"
        assert detailed_product.canonical_url and detailed_product.canonical_url.startswith("http"), "Item detail must have valid URL"

        # 4. If variants exist, validate them truthfully without inventing values
        if detailed_product.variants:
            assert detailed_product.variant_retrieval is True
            for variant in detailed_product.variants:
                # Variant attributes must be authentic
                assert variant.selected_options is not None
                # Price should either be a real float or None (never synthetic default)
                if variant.price is not None:
                    assert isinstance(variant.price, float)
                    assert variant.price >= 0
                # Availability should be valid enum
                assert variant.availability is not None
        else:
            # If no variants, variant_retrieval should be False
            assert detailed_product.variant_retrieval is False


    except Exception as e:
        # Report sanitized error without leaking credentials
        err_str = str(e)
        if client.settings.EBAY_CERT_ID:
            assert client.settings.EBAY_CERT_ID not in err_str, "Client secret must never leak in error messages!"
        pytest.fail(f"eBay live acceptance test failed with sanitized error: {err_str}")
    finally:
        await client.close()
