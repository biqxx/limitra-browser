from __future__ import annotations

from app.schemas.products import NormalizedProduct, PriceStatus, ProductImage
from app.services.data_quality import assess_product


def test_assessment_reports_currency_mismatch_without_relabelling() -> None:
    product = NormalizedProduct(
        provider="amazon",
        external_id="B0TEST1234",
        canonical_url="https://www.amazon.com/dp/B0TEST1234",
        title="Test product",
        current_price=94203.74,
        currency="NGN",
        images=[ProductImage(url="https://m.media-amazon.com/images/I/test.jpg")],
    )

    assessed = assess_product(product, requested_currency="USD")

    assert assessed.current_price == 94203.74
    assert assessed.currency == "NGN"
    assert assessed.data_quality.price_verified is True
    assert assessed.data_quality.detected_currency == "NGN"
    assert "requested USD but supplier returned NGN" in assessed.warnings


def test_assessment_never_exposes_unverified_zero() -> None:
    product = NormalizedProduct(
        provider="amazon",
        external_id="B0TEST1234",
        canonical_url="https://www.amazon.com/dp/B0TEST1234",
        title="Test product",
        current_price=0.0,
        currency="USD",
    )

    assessed = assess_product(product, requested_currency="USD")

    assert assessed.current_price is None
    assert assessed.data_quality.price_status == PriceStatus.UNAVAILABLE
    assert "price" in assessed.data_quality.missing_fields
