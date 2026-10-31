from __future__ import annotations

import pytest

from app.suppliers.amazon.adapter import AmazonAdapter
from app.suppliers.base import SupplierAdapter, SupplierHealth
from app.suppliers.ebay.adapter import EbayAdapter
from app.suppliers.registry import get_supplier_registry

ADAPTERS = [
    EbayAdapter(),
    AmazonAdapter(),
]


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.provider)
def test_adapter_implements_supplier_contract(adapter: SupplierAdapter):
    assert isinstance(adapter.provider, str)
    assert len(adapter.provider) > 0

    assert isinstance(adapter.supported_domains, set)
    assert len(adapter.supported_domains) > 0

    assert isinstance(adapter.supported_locales, set)
    assert len(adapter.supported_locales) > 0

    caps = adapter.capabilities
    assert isinstance(caps, dict)
    assert "product_search" in caps
    assert "product_retrieval" in caps
    assert "variant_retrieval" in caps
    assert "shipping_information" in caps
    assert "seller_information" in caps
    assert "reviews_summary" in caps


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.provider)
async def test_adapter_healthcheck_contract(adapter: SupplierAdapter):
    health = await adapter.healthcheck()
    assert isinstance(health, SupplierHealth)
    assert health.provider == adapter.provider
    assert health.status in ("healthy", "degraded", "unhealthy")
    assert len(health.supported_domains) > 0


def test_ebay_adapter_url_contract():
    adapter = EbayAdapter()
    valid_url = "https://www.ebay.com/itm/123456789012"
    assert adapter.recognizes_url(valid_url) is True
    assert adapter.recognizes_url("https://www.google.com") is False

    ext_id = adapter.extract_external_id(valid_url)
    assert ext_id == "123456789012"

    norm_url = adapter.normalize_url(valid_url)
    assert norm_url == "https://www.ebay.com/itm/123456789012"


def test_amazon_adapter_url_contract():
    adapter = AmazonAdapter()
    valid_url = "https://www.amazon.com/dp/B08N5WRWNW"
    assert adapter.recognizes_url(valid_url) is True
    assert adapter.recognizes_url("https://www.walmart.com/ip/123") is False

    asin = adapter.extract_external_id(valid_url)
    assert asin == "B08N5WRWNW"

    norm_url = adapter.normalize_url("https://www.amazon.com/Sony-Headphones/dp/B08N5WRWNW?ref=abc")
    assert norm_url == "https://www.amazon.com/dp/B08N5WRWNW"


def test_registry_registration():
    registry = get_supplier_registry()
    providers = registry.list_providers()
    assert "ebay" in providers
    assert "amazon" in providers

    adapter = registry.find_by_url("https://www.ebay.com/itm/987654321")
    assert adapter.provider == "ebay"

    adapter_amz = registry.find_by_url("https://www.amazon.com/dp/B08N5WRWNW")
    assert adapter_amz.provider == "amazon"
