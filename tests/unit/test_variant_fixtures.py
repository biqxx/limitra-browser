from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.schemas.products import ProductAvailability
from app.suppliers.amazon.parser import AmazonParser
from app.suppliers.ebay.api_client import EbayApiClient
from app.suppliers.ebay.parser import EbayParser

VARIANTS_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "variants"


@pytest.fixture
def amazon_parser() -> AmazonParser:
    return AmazonParser()


@pytest.fixture
def ebay_parser() -> EbayParser:
    return EbayParser()


def test_fixture_one_variation_group(amazon_parser: AmazonParser):
    html = (VARIANTS_FIXTURES_DIR / "one_group.html").read_text(encoding="utf-8")
    product = amazon_parser.parse_product_page(html, "https://www.amazon.com/dp/B00BASE123", asin="B00BASE123")

    assert product.variant_retrieval is True
    assert len(product.variants) == 2

    v1, v2 = product.variants
    assert v1.external_id == "B001BLACK"
    assert v1.selected_options == {"Color": "Black"}
    assert v1.price == 49.99
    assert len(v1.images) == 1
    assert "black.jpg" in v1.images[0].url

    assert v2.external_id == "B002WHITE"
    assert v2.selected_options == {"Color": "White"}
    assert v2.price == 54.99


def test_fixture_multiple_variation_groups_no_cartesian_explosion(amazon_parser: AmazonParser):
    """
    DOM has 2 Colors and 3 Sizes (Cartesian would be 6 combos).
    Supplier script explicitly specifies only 3 valid combinations.
    Confirms only the 3 valid combinations are extracted, not 6.
    """
    html = (VARIANTS_FIXTURES_DIR / "multiple_groups.html").read_text(encoding="utf-8")
    product = amazon_parser.parse_product_page(html, "https://www.amazon.com/dp/B00BASE123", asin="B00BASE123")

    assert product.variant_retrieval is True
    # Must be 3, NEVER 6!
    assert len(product.variants) == 3

    combos = [(v.selected_options.get("Color"), v.selected_options.get("Size")) for v in product.variants]
    assert ("Black", "Medium") in combos
    assert ("Black", "Large") in combos
    assert ("White", "Small") in combos
    # Impossible combinations not identified by supplier must NOT be present
    assert ("White", "Large") not in combos
    assert ("White", "Medium") not in combos


def test_fixture_missing_variant_price(amazon_parser: AmazonParser):
    """
    When variant price is unknown, variant.price MUST remain None.
    It must NEVER be populated with 0 or the base product price ($120.00).
    """
    html = (VARIANTS_FIXTURES_DIR / "missing_variant_price.html").read_text(encoding="utf-8")
    product = amazon_parser.parse_product_page(html, "https://www.amazon.com/dp/B00BASE123", asin="B00BASE123")

    assert product.current_price == 120.00
    assert len(product.variants) == 2
    for v in product.variants:
        assert v.price is None  # Truthfully None, never 120.00 or 0.0


def test_fixture_missing_availability(amazon_parser: AmazonParser):
    """
    When availability is unknown, variant.availability MUST remain UNKNOWN.
    It must NEVER default to IN_STOCK.
    """
    html = (VARIANTS_FIXTURES_DIR / "missing_availability.html").read_text(encoding="utf-8")
    product = amazon_parser.parse_product_page(html, "https://www.amazon.com/dp/B00BASE123", asin="B00BASE123")

    assert len(product.variants) == 1
    assert product.variants[0].availability == ProductAvailability.UNKNOWN


def test_fixture_unavailable_combinations(amazon_parser: AmazonParser):
    """
    Unavailable/omitted combinations from supplier variation map are omitted.
    """
    html = (VARIANTS_FIXTURES_DIR / "unavailable_combinations.html").read_text(encoding="utf-8")
    product = amazon_parser.parse_product_page(html, "https://www.amazon.com/dp/B00BASE123", asin="B00BASE123")

    assert len(product.variants) == 2
    combos = [v.selected_options for v in product.variants]
    assert {"Color": "Red", "Size": "Medium"} in combos
    assert {"Color": "Blue", "Size": "Medium"} in combos
    # Omitted combo Red Large does not exist
    assert {"Color": "Red", "Size": "Large"} not in combos


def test_fixture_variant_specific_images(amazon_parser: AmazonParser):
    """
    Confirms variant-specific images are extracted and preserved on each variant.
    """
    html = (VARIANTS_FIXTURES_DIR / "variant_specific_images.html").read_text(encoding="utf-8")
    product = amazon_parser.parse_product_page(html, "https://www.amazon.com/dp/B00BASE123", asin="B00BASE123")

    assert len(product.variants) == 2
    red_v = next(v for v in product.variants if v.selected_options.get("Color") == "Red")
    blue_v = next(v for v in product.variants if v.selected_options.get("Color") == "Blue")

    assert len(red_v.images) == 1
    assert "red_large.jpg" in red_v.images[0].url
    assert len(blue_v.images) == 1
    assert "blue_large.jpg" in blue_v.images[0].url


def test_fixture_json_ld_variants(ebay_parser: EbayParser):
    """
    Confirms extraction of variants from JSON-LD hasVariant.
    Preserves explicit price on first variant, None on second, and stock states.
    """
    html = (VARIANTS_FIXTURES_DIR / "json_ld_variants.html").read_text(encoding="utf-8")
    product = ebay_parser.parse_product_page(html, "https://www.ebay.com/itm/112233", external_id="112233")

    assert product.variant_retrieval is True
    assert len(product.variants) == 2

    v1, v2 = product.variants
    assert v1.external_id == "HEADSET-STEALTH"
    assert v1.price == 99.99
    assert v1.availability == ProductAvailability.IN_STOCK
    assert v1.selected_options == {"Color": "Black"}

    assert v2.external_id == "HEADSET-ARCTIC"
    assert v2.price is None  # Missing price must be None
    assert v2.availability == ProductAvailability.OUT_OF_STOCK
    assert v2.selected_options == {"Color": "White"}


def test_fixture_embedded_script_variants(ebay_parser: EbayParser):
    """
    Confirms extraction of MSKU variants from inline script (itemVariationsMap).
    Extracts aspects, price, stock, images without inventing synthetic IDs.
    """
    html = (VARIANTS_FIXTURES_DIR / "embedded_script_variants.html").read_text(encoding="utf-8")
    product = ebay_parser.parse_product_page(html, "https://www.ebay.com/itm/1122334455", external_id="1122334455")

    assert product.variant_retrieval is True
    assert len(product.variants) == 2

    v1, v2 = product.variants
    assert v1.external_id == "1122334455"
    assert v1.selected_options == {"Color": "Black", "Size": "42"}
    assert v1.price == 115.00
    assert v1.availability == ProductAvailability.IN_STOCK
    assert len(v1.images) == 1
    assert "boots_black.jpg" in v1.images[0].url

    assert v2.external_id == "BOOT-BRN-43"
    assert v2.selected_options == {"Color": "Brown", "Size": "43"}
    assert v2.price is None  # Missing price is None
    assert v2.availability == ProductAvailability.OUT_OF_STOCK


@pytest.mark.asyncio
async def test_fixture_official_ebay_api_variants():
    """
    Confirms official eBay Buy Browse API variant extraction:
    - itemId or None (no synthetic item_id-idx generated)
    - price or None (never base price)
    - stockStatus mapping (IN_STOCK, OUT_OF_STOCK, or UNKNOWN)
    - variationAspects preserved
    """
    api_client = EbayApiClient()
    fixture_data = json.loads((VARIANTS_FIXTURES_DIR / "official_ebay_api_variants.json").read_text(encoding="utf-8"))

    from unittest.mock import MagicMock

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture_data

    with (
        patch.object(api_client, "_get_access_token", AsyncMock(return_value="mock_token")),
        patch("httpx.AsyncClient.get", AsyncMock(return_value=mock_resp)),
    ):
        product = await api_client.get_item("2233445566")

    assert product.variant_retrieval is True
    assert len(product.variants) == 2

    v1 = product.variants[0]
    assert v1.external_id == "v1|2233445566|1001"
    assert v1.price == 209.99
    assert v1.availability == ProductAvailability.IN_STOCK
    assert v1.selected_options == {"Band Color": "Silver", "Case Size": "44mm"}
    assert len(v1.images) == 1
    assert "watch_silver.jpg" in v1.images[0].url

    v2 = product.variants[1]
    assert v2.external_id is None  # No synthetic itemId generated!
    assert v2.price is None  # Missing price truthfully None, NOT base 199.99!
    assert v2.availability == ProductAvailability.OUT_OF_STOCK
    assert v2.selected_options == {"Band Color": "Space Gray", "Case Size": "40mm"}


def test_variant_retrieval_flag_false_when_no_variants(amazon_parser: AmazonParser):
    """
    Confirms variant_retrieval is False when no meaningful variants exist.
    """
    html = "<html><body><span id='productTitle'>Simple Mug</span><span id='priceblock_ourprice'>$9.99</span></body></html>"
    product = amazon_parser.parse_product_page(html, "https://www.amazon.com/dp/B00SIMPLE", asin="B00SIMPLE")

    assert len(product.variants) == 0
    assert product.variant_retrieval is False
