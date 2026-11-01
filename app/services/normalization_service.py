from __future__ import annotations

import re
from typing import Any

from app.schemas.common import FieldProvenance
from app.schemas.products import ProductAvailability
from app.services.price_parser import ParsedPrice, parse_price

__all__ = [
    "FieldMergeCandidate",
    "ParsedPrice",
    "parse_availability",
    "parse_price",
    "parse_price_and_currency",
    "parse_rating_and_reviews",
    "select_best_field",
]


def parse_price_and_currency(
    raw_price: str | None,
    default_currency: str | None = None,
    marketplace_currency: str | None = None,
) -> tuple[float | None, str | None]:
    """
    Extracts a numeric price and ISO 4217 currency from supplier text.

    The currency is whatever the text actually states. ``default_currency`` is only
    returned when the text contains no currency at all and the caller knowingly
    inherits it from the same page (e.g. original price next to a detected current
    price). Ranges and unusable values return ``None`` for the amount.
    """
    parsed = parse_price(raw_price, marketplace_currency=marketplace_currency)
    return parsed.amount, parsed.currency or default_currency


def parse_availability(raw_text: str | None) -> tuple[ProductAvailability, int | None]:
    """
    Parses availability status and available quantity from text:
    e.g. 'In Stock' -> (IN_STOCK, None)
    e.g. 'Only 3 left in stock' -> (LIMITED_STOCK, 3)
    e.g. 'Currently unavailable' -> (OUT_OF_STOCK, 0)
    """
    if not raw_text:
        return ProductAvailability.UNKNOWN, None

    text = raw_text.strip().lower()

    # Check limited stock
    qty_match = re.search(r"only\s+(\d+)\s+left", text)
    if qty_match:
        qty = int(qty_match.group(1))
        return ProductAvailability.LIMITED_STOCK, qty

    if any(s in text for s in ("out of stock", "currently unavailable", "sold out", "unavailable")):
        return ProductAvailability.OUT_OF_STOCK, 0

    if any(s in text for s in ("pre-order", "preorder", "backorder")):
        return ProductAvailability.PREORDER, None

    if any(s in text for s in ("in stock", "available", "ships from")):
        return ProductAvailability.IN_STOCK, None

    return ProductAvailability.UNKNOWN, None


def parse_rating_and_reviews(
    raw_rating: str | None, raw_reviews: str | None
) -> tuple[float | None, int | None]:
    """
    Parses numerical rating and review count:
    e.g. '4.6 out of 5 stars', '12,431 ratings' -> (4.6, 12431)
    """
    rating: float | None = None
    reviews: int | None = None

    if raw_rating:
        r_match = re.search(r"(\d+(?:\.\d+)?)", raw_rating)
        if r_match:
            try:
                rating = float(r_match.group(1))
            except ValueError:
                pass

    if raw_reviews:
        rev_match = re.search(r"([\d,]+)", raw_reviews)
        if rev_match:
            try:
                clean_rev = rev_match.group(1).replace(",", "")
                reviews = int(clean_rev)
            except ValueError:
                pass

    return rating, reviews


class FieldMergeCandidate:
    def __init__(
        self, value: Any, source: str, selector: str | None = None, confidence: float = 0.5
    ):
        self.value = value
        self.provenance = FieldProvenance(source=source, selector=selector, confidence=confidence)


def select_best_field(candidates: list[FieldMergeCandidate]) -> tuple[Any, FieldProvenance | None]:
    """
    Selects the field value with highest confidence among valid candidates.
    Priority hierarchy:
    1. Official supplier API (confidence ~0.99)
    2. Embedded JSON-LD (confidence ~0.95)
    3. Public structured state (confidence ~0.85)
    4. Stable semantic HTML (confidence ~0.75)
    5. Supplier selector configuration (confidence ~0.60)
    """
    valid_candidates = [
        c
        for c in candidates
        if c.value is not None and (not isinstance(c.value, str) or c.value.strip() != "")
    ]
    if not valid_candidates:
        return None, None

    best = max(valid_candidates, key=lambda c: c.provenance.confidence)
    return best.value, best.provenance
