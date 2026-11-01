"""Builds the structured data-quality report for a retrieved product.

Nothing here invents data: it only *describes* what the supplier did and did not supply.
"""

from __future__ import annotations

from app.schemas.products import (
    DataQuality,
    NormalizedProduct,
    PriceStatus,
    ProductAvailability,
)
from app.services.image_validation import sanitize_images

_PRICE_STATUS_ORDER = (PriceStatus.AVAILABLE, PriceStatus.FREE)


def assess_product(
    product: NormalizedProduct,
    requested_currency: str | None = None,
    extra_warnings: list[str] | None = None,
) -> NormalizedProduct:
    """Sanitise images and attach ``data_quality`` / ``warnings`` to ``product``."""
    from app.api.compat.mappers import extract_weight_from_product

    warnings: list[str] = list(product.warnings)
    warnings.extend(extra_warnings or [])

    # Images: https, absolute, deduplicated, supplier hosted.
    images, image_warnings = sanitize_images(product.images, product.provider)
    warnings.extend(image_warnings)

    # Price status is derived strictly from supplier data.
    if product.current_price is not None and product.current_price > 0:
        price_status = PriceStatus.AVAILABLE
    elif product.current_price == 0:
        # A zero price is only legitimate if the parser flagged an explicit "Free".
        price_status = (
            PriceStatus.FREE
            if product.source_metadata.get("price_explicit_free") is not None
            else PriceStatus.UNAVAILABLE
        )
    elif product.price_range:
        price_status = PriceStatus.RANGE
    else:
        price_status = PriceStatus.UNAVAILABLE

    price = product.current_price
    if price_status == PriceStatus.UNAVAILABLE and price == 0:
        price = None  # never expose an unverified zero

    price_verified = price_status in _PRICE_STATUS_ORDER and bool(product.currency)
    currency_verified = bool(product.currency)

    detected = product.currency
    requested = requested_currency.upper() if requested_currency else None
    if requested and detected and requested != detected.upper():
        msg = f"requested {requested} but supplier returned {detected.upper()}"
        if msg not in warnings:
            warnings.append(msg)

    if price_status == PriceStatus.RANGE and product.price_range:
        warnings.append(
            "supplier returned a price range "
            f"({product.price_range.get('min')}-{product.price_range.get('max')}); "
            "no single price was selected"
        )
    if price_status == PriceStatus.UNAVAILABLE:
        warnings.append("supplier did not provide a reliable price")
    if price is not None and not product.currency:
        warnings.append("supplier price has no detectable currency")

    missing: list[str] = []
    if price_status == PriceStatus.UNAVAILABLE:
        missing.append("price")
    elif price_status == PriceStatus.RANGE:
        missing.append("price")
    if not product.currency:
        missing.append("currency")
    if not images:
        missing.append("images")
    if not product.description and not product.bullet_points:
        missing.append("description")
    if product.availability == ProductAvailability.UNKNOWN:
        missing.append("availability")
    weight_lb, _ = extract_weight_from_product(product)
    if weight_lb is None:
        missing.append("weight")
    if not product.shipping_options:
        missing.append("shipping")

    variants_complete = all(
        v.price is not None and v.availability != ProductAvailability.UNKNOWN
        for v in product.variants
    )
    if product.variants and not variants_complete:
        warnings.append("some variants are missing a price or availability")

    quality = DataQuality(
        complete=(
            not missing
            and price_verified
            and bool(images)
            and variants_complete
            and not any(w.startswith("requested ") for w in warnings)
        ),
        missing_fields=missing,
        warnings=list(dict.fromkeys(warnings)),
        price_verified=price_verified,
        currency_verified=currency_verified,
        images_verified=bool(images),
        variants_complete=variants_complete,
        price_status=price_status,
        requested_currency=requested,
        detected_currency=detected.upper() if detected else None,
        price_range=product.price_range,
    )
    return product.model_copy(
        update={
            "images": images,
            "current_price": price,
            "warnings": quality.warnings,
            "data_quality": quality,
        }
    )
