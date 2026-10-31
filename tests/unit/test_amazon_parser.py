from __future__ import annotations

from app.schemas.products import ProductAvailability
from app.suppliers.amazon.parser import AmazonParser


def test_amazon_parser_search_results(amazon_search_html: str):
    parser = AmazonParser()
    items = parser.parse_search_results(amazon_search_html, provider="amazon")

    assert len(items) == 2

    item1 = items[0]
    assert item1.external_id == "B09XS7JWHH"
    assert "Sony WH-1000XM5" in item1.title
    assert item1.price == 398.00
    assert item1.original_price == 449.99
    assert item1.currency == "USD"
    assert item1.rating == 4.6
    assert item1.review_count == 8450
    assert item1.free_shipping is True

    item2 = items[1]
    assert item2.external_id == "B07G4MN6KP"
    assert "Bose QuietComfort" in item2.title
    assert item2.price == 199.00


def test_amazon_parser_product_page(amazon_product_html: str):
    parser = AmazonParser()
    product = parser.parse_product_page(
        html=amazon_product_html,
        url="https://www.amazon.com/dp/B09XS7JWHH",
        asin="B09XS7JWHH",
        currency="USD",
    )

    assert product.provider == "amazon"
    assert product.external_id == "B09XS7JWHH"
    assert "Sony WH-1000XM5" in product.title
    assert product.brand == "Sony"
    assert product.current_price == 398.00
    assert product.original_price == 449.99
    assert product.currency == "USD"
    assert product.availability == ProductAvailability.IN_STOCK
    assert product.seller.name == "Amazon.com"
    assert len(product.bullet_points) == 4
    assert "noise cancellation" in product.bullet_points[0].lower()
    assert product.specifications.get("Model Name") == "WH-1000XM5"
    assert product.specifications.get("Color") == "Black"
    assert len(product.images) > 0
    assert product.identifiers.get("asin") == "B09XS7JWHH"

    # Provenance check
    assert "title" in product.source_metadata
    assert product.source_metadata["title"].source == "css_selector"
    assert "current_price" in product.source_metadata
