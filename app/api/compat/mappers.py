from __future__ import annotations

import re
from datetime import datetime, timezone

from app.api.compat.schemas import LegacyProduct, LegacyVariant
from app.schemas.products import NormalizedProduct, ProductAvailability, ProductVariant
from app.schemas.search import SearchResultItem


def parse_weight_to_lb(value: float, unit: str) -> float:
    """
    Converts a weight in the given unit to pounds (matching Go limitra-supplier-service).
    Returns 0.0 for unknown units.
    """
    u = unit.strip().upper()
    if u in ("LB", "LBS", "POUND", "POUNDS"):
        return value
    if u in ("OZ", "OUNCE", "OUNCES"):
        return value * 0.0625
    if u in ("KG", "KILOGRAM", "KILOGRAMS"):
        return value * 2.205
    if u in ("G", "GRAM", "GRAMS"):
        return value * 0.002205
    return 0.0


def extract_weight_from_product(
    product: NormalizedProduct,
) -> tuple[float | None, float | None]:
    """
    Extracts weight_lb and weight_kg from product specifications or attributes.
    Returns (weight_lb, weight_kg) or (None, None) if not stated by supplier.
    Never invents default weight values.
    """
    weight_regex = re.compile(
        r"([\d.]+)\s*(lbs?|pounds?|oz|ounces?|kg|kilograms?|g|grams?)",
        re.IGNORECASE,
    )

    weight_keys = [
        "Item Weight",
        "item_weight",
        "Package Weight",
        "package_weight",
        "Weight",
        "weight",
        "Shipping Weight",
        "shipping_weight",
    ]

    for key in weight_keys:
        val = product.specifications.get(key)
        if val:
            match = weight_regex.search(str(val))
            if match:
                num = float(match.group(1))
                unit = match.group(2)
                lb = parse_weight_to_lb(num, unit)
                if lb > 0:
                    kg = lb / 2.205
                    return round(lb, 4), round(kg, 4)

    # Search through all specs values if not found in explicit keys
    for k, v in product.specifications.items():
        if "weight" in k.lower():
            match = weight_regex.search(str(v))
            if match:
                num = float(match.group(1))
                unit = match.group(2)
                lb = parse_weight_to_lb(num, unit)
                if lb > 0:
                    kg = lb / 2.205
                    return round(lb, 4), round(kg, 4)

    # Unknown facts must remain None - never assign default 1 lb
    return None, None


def parse_date_to_days(date_str: str) -> int:
    """
    Parses an ISO 8601 date string and returns the number of days from today.
    """
    if not date_str:
        return 0

    now = datetime.now(timezone.utc)
    today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)

    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(date_str, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            target = datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)
            days = int((target - today).total_seconds() / 86400)
            return max(0, days)
        except ValueError:
            continue
    return 0



def extract_delivery_days(product: NormalizedProduct) -> tuple[int | None, int | None]:
    """
    Extracts us_delivery_days_min and us_delivery_days_max.
    Returns (min_days, max_days) or (None, None) if not stated by supplier.
    Never invents arbitrary delivery day ranges.
    """
    min_days: int | None = None
    max_days: int | None = None

    for opt in product.shipping_options:
        if opt.estimated_days_min is not None:
            min_days = (
                min(min_days, opt.estimated_days_min)
                if min_days is not None
                else opt.estimated_days_min
            )
        if opt.estimated_days_max is not None:
            max_days = (
                max(max_days, opt.estimated_days_max)
                if max_days is not None
                else opt.estimated_days_max
            )

    if min_days is not None and max_days is not None:
        return max(1, min_days), max(min_days, max_days)

    if product.estimated_delivery:
        days = parse_date_to_days(product.estimated_delivery)
        if days > 0:
            return max(1, days - 2), days

    # Unknown delivery window must remain None
    return None, None


def map_variant(v: ProductVariant) -> LegacyVariant:
    if v.selected_options:
        label = " / ".join(v.selected_options.keys())
        value = " / ".join(v.selected_options.values())
    else:
        label = "Option"
        value = v.title or v.sku or (v.external_id or "Default")

    avail_bool: bool | None = None
    if v.availability in (ProductAvailability.IN_STOCK, ProductAvailability.LIMITED_STOCK):
        avail_bool = True
    elif v.availability == ProductAvailability.OUT_OF_STOCK:
        avail_bool = False

    return LegacyVariant(
        id=v.external_id,
        label=label,
        value=value,
        price=round(v.price, 2) if v.price is not None else None,
        available=avail_bool,
    )


def map_normalized_product_to_legacy(product: NormalizedProduct) -> LegacyProduct:
    weight_lb, weight_kg = extract_weight_from_product(product)
    min_days, max_days = extract_delivery_days(product)
    available = product.availability in (
        ProductAvailability.IN_STOCK,
        ProductAvailability.LIMITED_STOCK,
    )
    image_url = product.images[0].url if product.images else ""
    has_variants = bool(product.variant_retrieval and len(product.variants) > 0)

    return LegacyProduct(
        supplier_id=product.external_id,
        supplier=product.provider.lower(),
        title=product.title,
        description=product.description or "",
        price=round(product.current_price, 2),
        currency=product.currency or "USD",
        image_url=image_url,
        product_url=product.canonical_url,
        available=available,
        variants=[map_variant(v) for v in product.variants],
        variant_retrieval=has_variants,
        fetched_at=product.retrieved_at,
        weight_lb=weight_lb,
        weight_kg=weight_kg,
        us_delivery_days_min=min_days,
        us_delivery_days_max=max_days,
    )


def map_search_item_to_legacy(item: SearchResultItem) -> LegacyProduct:
    return LegacyProduct(
        supplier_id=item.external_id,
        supplier=item.provider.lower(),
        title=item.title,
        description="",
        price=round(item.price, 2),
        currency=item.currency or "USD",
        image_url=item.thumbnail_url or "",
        product_url=item.url,
        available=item.in_stock,
        variants=[],
        weight_lb=None,
        weight_kg=None,
        us_delivery_days_min=None,
        us_delivery_days_max=None,
    )
