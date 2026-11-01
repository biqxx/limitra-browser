from __future__ import annotations

import pytest

from app.services.price_parser import parse_price


@pytest.mark.parametrize(
    ("raw", "amount", "currency"),
    [
        ("NGN94,203.74", 94203.74, "NGN"),
        ("₦94,203.74", 94203.74, "NGN"),
        ("$1,299.99", 1299.99, "USD"),
        ("US $49.99", 49.99, "USD"),
        ("EUR 45,50", 45.5, "EUR"),
        ("1.450,50 EUR", 1450.5, "EUR"),
        ("£199", 199.0, "GBP"),
        ("₹1,299", 1299.0, "INR"),
    ],
)
def test_parse_supported_prices(raw: str, amount: float, currency: str) -> None:
    parsed = parse_price(raw)

    assert parsed.amount == amount
    assert parsed.currency == currency
    assert parsed.usable is True


def test_missing_price_remains_missing() -> None:
    parsed = parse_price(None)

    assert parsed.amount is None
    assert parsed.currency is None


def test_numeric_zero_is_not_treated_as_a_free_product() -> None:
    parsed = parse_price("$0.00")

    assert parsed.amount is None
    assert parsed.zero_rejected is True


def test_explicit_free_is_preserved() -> None:
    parsed = parse_price("Free")

    assert parsed.amount == 0.0
    assert parsed.explicit_free is True


def test_price_range_is_not_collapsed_to_one_value() -> None:
    parsed = parse_price("$49.99 - $79.99")

    assert parsed.amount is None
    assert parsed.is_range is True
    assert parsed.range_min == 49.99
    assert parsed.range_max == 79.99


def test_explicit_currency_wins_over_marketplace_hint() -> None:
    parsed = parse_price("NGN94,203.74", marketplace_currency="USD")

    assert parsed.amount == 94203.74
    assert parsed.currency == "NGN"


def test_marketplace_hint_resolves_ambiguous_dollar_symbol() -> None:
    parsed = parse_price("$29.99", marketplace_currency="CAD")

    assert parsed.amount == 29.99
    assert parsed.currency == "CAD"
