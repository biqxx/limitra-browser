from __future__ import annotations

from app.schemas.products import ProductAvailability
from app.services.normalization_service import (
    FieldMergeCandidate,
    parse_availability,
    parse_price_and_currency,
    parse_rating_and_reviews,
    select_best_field,
)


def test_parse_price_and_currency_standard():
    price, curr = parse_price_and_currency("$199.99")
    assert price == 199.99
    assert curr == "USD"


def test_parse_price_and_currency_euro():
    price, curr = parse_price_and_currency("45,50 €")
    assert price == 45.50
    assert curr == "EUR"


def test_parse_price_and_currency_comma_thousands():
    price, curr = parse_price_and_currency("US $1,299.00")
    assert price == 1299.00
    assert curr == "USD"


def test_parse_price_empty():
    price, curr = parse_price_and_currency(None, default_currency="EUR")
    assert price is None
    assert curr == "EUR"


def test_parse_availability_in_stock():
    avail, qty = parse_availability("In Stock")
    assert avail == ProductAvailability.IN_STOCK
    assert qty is None


def test_parse_availability_limited():
    avail, qty = parse_availability("Only 3 left in stock - order soon.")
    assert avail == ProductAvailability.LIMITED_STOCK
    assert qty == 3


def test_parse_availability_out_of_stock():
    avail, qty = parse_availability("Currently unavailable.")
    assert avail == ProductAvailability.OUT_OF_STOCK
    assert qty == 0


def test_parse_rating_and_reviews():
    rating, reviews = parse_rating_and_reviews("4.8 out of 5 stars", "1,420 ratings")
    assert rating == 4.8
    assert reviews == 1420


def test_select_best_field_by_confidence():
    c1 = FieldMergeCandidate("Low confidence title", source="css_selector", confidence=0.6)
    c2 = FieldMergeCandidate("JSON-LD title", source="json_ld", confidence=0.95)
    c3 = FieldMergeCandidate("Official API title", source="official_api", confidence=0.99)

    best_val, best_prov = select_best_field([c1, c2, c3])
    assert best_val == "Official API title"
    assert best_prov is not None
    assert best_prov.source == "official_api"
    assert best_prov.confidence == 0.99


def test_select_best_field_skips_empty():
    c1 = FieldMergeCandidate("", source="official_api", confidence=0.99)
    c2 = FieldMergeCandidate("Valid fallback title", source="css_selector", confidence=0.7)

    best_val, best_prov = select_best_field([c1, c2])
    assert best_val == "Valid fallback title"
    assert best_prov is not None
    assert best_prov.source == "css_selector"
