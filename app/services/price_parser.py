"""Currency-aware price parsing.

Design rules (root causes of earlier bugs this module fixes):

* A currency is only reported when it is actually present in the text (or when an
  *ambiguous* symbol such as ``$`` / ``¥`` can be resolved by the marketplace hint).
  It is never defaulted to USD merely because USD was requested.
* An absent, unparsable or numerically-zero price is ``None``, never ``0.0``.
  Zero is only returned when the supplier literally says the item is "Free".
* Price ranges are reported as ranges (``amount=None``) rather than silently picking
  the lowest or highest value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ISO codes we recognise as explicit text (matched case-insensitively, not inside words).
ISO_CODES: frozenset[str] = frozenset(
    {"USD", "NGN", "GBP", "EUR", "CAD", "AUD", "JPY", "CNY", "INR", "NZD", "SGD", "MXN"}
)

# Unambiguous symbols. Longest first so that "US$" wins over "$".
_UNAMBIGUOUS_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("US $", "USD"),
    ("US$", "USD"),
    ("CA $", "CAD"),
    ("CA$", "CAD"),
    ("C $", "CAD"),
    ("C$", "CAD"),
    ("AU $", "AUD"),
    ("AU$", "AUD"),
    ("A $", "AUD"),
    ("A$", "AUD"),
    ("NZ$", "NZD"),
    ("S$", "SGD"),
    ("JP¥", "JPY"),
    ("CN¥", "CNY"),
    ("RMB", "CNY"),
    ("₦", "NGN"),
    ("£", "GBP"),
    ("€", "EUR"),
    ("₹", "INR"),
)

# Ambiguous symbols -> candidate ISO codes. Resolved only through the marketplace hint.
_AMBIGUOUS_SYMBOLS: dict[str, tuple[str, ...]] = {
    "$": ("USD", "CAD", "AUD", "NZD", "SGD", "MXN"),
    "¥": ("JPY", "CNY"),
    "￥": ("JPY", "CNY"),
}

# Bare "$" means USD when there is no marketplace hint (this is the documented convention).
_AMBIGUOUS_DEFAULT: dict[str, str | None] = {"$": "USD", "¥": None, "￥": None}

_NUMBER_RE = re.compile(r"\d[\d.,\u00a0\u202f ]*\d|\d")
_RANGE_JOINER_RE = re.compile(r"^\s*(?:-|–|—|to|a)\s*[^\d]{0,8}$", re.IGNORECASE)
_FREE_RE = re.compile(r"^\W*(?:free|gratis|kostenlos)\W*$", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedPrice:
    """Result of parsing a raw price string."""

    amount: float | None
    currency: str | None
    is_range: bool = False
    range_min: float | None = None
    range_max: float | None = None
    explicit_free: bool = False
    zero_rejected: bool = False
    ambiguous_currency: bool = False
    raw: str | None = None

    @property
    def usable(self) -> bool:
        """True when ``amount`` is a trustworthy single value."""
        return self.amount is not None and not self.is_range


def detect_currency(text: str, hint: str | None = None) -> tuple[str | None, bool]:
    """Return ``(currency, ambiguous)``.

    ``ambiguous`` is True when a symbol was present but could not be resolved.
    """
    upper = text.upper()

    # 1. Explicit ISO code, e.g. "NGN94,203.74" or "1.450,50 EUR".
    for code in sorted(ISO_CODES):
        if re.search(rf"(?<![A-Z]){code}(?![A-Z])", upper):
            return code, False

    # 2. Unambiguous symbols.
    for symbol, code in _UNAMBIGUOUS_SYMBOLS:
        if symbol in text:
            return code, False

    # 3. Ambiguous symbols are resolved only by the marketplace hint.
    for symbol, candidates in _AMBIGUOUS_SYMBOLS.items():
        if symbol in text:
            if hint and hint.upper() in candidates:
                return hint.upper(), False
            default = _AMBIGUOUS_DEFAULT.get(symbol)
            if default:
                return default, False
            return None, True

    return None, False


def _normalise_number(token: str, currency: str | None) -> float | None:
    """Convert a numeric token with arbitrary grouping into a float."""
    cleaned = re.sub(r"[\s\u00a0\u202f]", "", token)
    if not cleaned or not re.search(r"\d", cleaned):
        return None

    has_dot = "." in cleaned
    has_comma = "," in cleaned

    if has_dot and has_comma:
        # The right-most separator is the decimal separator.
        if cleaned.rfind(".") > cleaned.rfind(","):
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(".", "").replace(",", ".")
    elif has_comma:
        parts = cleaned.split(",")
        if len(parts) > 2:
            cleaned = cleaned.replace(",", "")  # 1,234,567
        elif len(parts[1]) == 3 and currency != "EUR":
            cleaned = cleaned.replace(",", "")  # 1,299 -> thousands
        elif len(parts[1]) == 3:
            # "1,299 EUR" is far more likely thousands than a 3-digit decimal.
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = f"{parts[0]}.{parts[1]}"  # 45,50 -> decimal
    elif has_dot:
        parts = cleaned.split(".")
        if len(parts) > 2:
            cleaned = cleaned.replace(".", "")  # 1.234.567
        elif len(parts[1]) == 3 and currency in {"EUR", "JPY"}:
            cleaned = cleaned.replace(".", "")  # 1.299 EUR -> thousands

    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_price(raw: str | None, marketplace_currency: str | None = None) -> ParsedPrice:
    """Parse a supplier price string.

    ``marketplace_currency`` is *only* used to resolve ambiguous symbols
    (``$`` on amazon.ca is CAD, ``¥`` on amazon.co.jp is JPY). It never overrides an
    explicit currency and is never applied when the text carries no currency symbol.
    """
    if raw is None or not isinstance(raw, str) or not raw.strip():
        return ParsedPrice(amount=None, currency=None, raw=raw)

    text = raw.strip()

    if _FREE_RE.match(text):
        return ParsedPrice(amount=0.0, currency=None, explicit_free=True, raw=raw)

    currency, ambiguous = detect_currency(text, marketplace_currency)

    # Strip ISO codes so their letters do not interfere with number scanning.
    scan = text
    matches = list(_NUMBER_RE.finditer(scan))
    if not matches:
        return ParsedPrice(amount=None, currency=currency, ambiguous_currency=ambiguous, raw=raw)

    # Range detection: two numbers joined by "-", "–", "—" or "to".
    if len(matches) >= 2:
        between = scan[matches[0].end() : matches[1].start()]
        if _RANGE_JOINER_RE.match(between):
            low = _normalise_number(matches[0].group(0), currency)
            high = _normalise_number(matches[1].group(0), currency)
            if low is not None and high is not None:
                lo, hi = (low, high) if low <= high else (high, low)
                return ParsedPrice(
                    amount=None,
                    currency=currency,
                    is_range=True,
                    range_min=round(lo, 2),
                    range_max=round(hi, 2),
                    ambiguous_currency=ambiguous,
                    raw=raw,
                )

    value = _normalise_number(matches[0].group(0), currency)
    if value is None:
        return ParsedPrice(amount=None, currency=currency, ambiguous_currency=ambiguous, raw=raw)

    if value == 0:
        # A numeric zero is not a reliable statement that the item is free.
        return ParsedPrice(
            amount=None,
            currency=currency,
            zero_rejected=True,
            ambiguous_currency=ambiguous,
            raw=raw,
        )

    return ParsedPrice(
        amount=round(value, 2),
        currency=currency,
        ambiguous_currency=ambiguous,
        raw=raw,
    )
