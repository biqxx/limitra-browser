from __future__ import annotations

from app.schemas.products import ProductAvailability
from app.suppliers.ebay.parser import EbayParser


def test_ebay_parser_search_results(ebay_search_html: str):
    parser = EbayParser()
    items = parser.parse_search_results(ebay_search_html, provider="ebay")

    assert len(items) == 2

    item1 = items[0]
    assert item1.external_id == "112233445566"
    assert "Sony WH-1000XM5" in item1.title
    assert item1.price == 298.00
    assert item1.original_price == 399.99
    assert item1.currency == "USD"
    assert item1.free_shipping is True
    assert item1.rating == 4.8
    assert item1.review_count == 1420

    item2 = items[1]
    assert item2.external_id == "223344556677"
    assert "Bose QuietComfort 45" in item2.title
    assert item2.price == 249.00
    assert item2.free_shipping is False
    assert item2.rating == 4.6
    assert item2.review_count == 512


def test_ebay_parser_product_page(ebay_product_html: str):
    parser = EbayParser()
    product = parser.parse_product_page(
        html=ebay_product_html,
        url="https://www.ebay.com/itm/112233445566",
        external_id="112233445566",
        currency="USD",
    )

    assert product.provider == "ebay"
    assert product.external_id == "112233445566"
    assert "Sony WH-1000XM5" in product.title
    assert product.brand == "Sony"
    assert product.current_price == 298.00
    assert product.original_price == 399.99
    assert product.currency == "USD"
    assert product.availability == ProductAvailability.LIMITED_STOCK
    assert product.available_quantity == 5
    assert product.seller.name == "TechStoreDirect"
    assert product.condition == "new"
    assert len(product.images) >= 2
    assert product.specifications.get("Model") == "WH-1000XM5"
    assert product.specifications.get("Connectivity") == "Bluetooth, 3.5mm"
    assert len(product.shipping_options) > 0
    assert product.shipping_options[0].free_shipping is True

    # Provenance check
    assert "title" in product.source_metadata
    assert product.source_metadata["title"].source in ("json_ld", "css_selector")
    assert "current_price" in product.source_metadata
