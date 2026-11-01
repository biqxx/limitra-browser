"""Amazon marketplace metadata (country -> domain / locale / currency / PA-API host).

The marketplace currency is the currency Amazon is *expected* to display. It is used to
request the right storefront and to detect a mismatch. It is never used to relabel a price.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AmazonMarketplace:
    country: str
    domain: str
    paapi_host: str
    paapi_region: str
    locale: str
    accept_language: str
    currency: str
    # Value of the ``lc-main`` cookie used by the storefront.
    lc_main: str

    @property
    def base_url(self) -> str:
        return f"https://{self.domain}"


_MARKETPLACES: dict[str, AmazonMarketplace] = {
    m.country: m
    for m in (
        AmazonMarketplace(
            "US", "www.amazon.com", "webservices.amazon.com", "us-east-1",
            "en-US", "en-US,en;q=0.9", "USD", "en_US",
        ),
        AmazonMarketplace(
            "CA", "www.amazon.ca", "webservices.amazon.ca", "us-east-1",
            "en-CA", "en-CA,en;q=0.9,fr-CA;q=0.6", "CAD", "en_CA",
        ),
        AmazonMarketplace(
            "GB", "www.amazon.co.uk", "webservices.amazon.co.uk", "eu-west-1",
            "en-GB", "en-GB,en;q=0.9", "GBP", "en_GB",
        ),
        AmazonMarketplace(
            "DE", "www.amazon.de", "webservices.amazon.de", "eu-west-1",
            "de-DE", "de-DE,de;q=0.9,en;q=0.6", "EUR", "de_DE",
        ),
        AmazonMarketplace(
            "FR", "www.amazon.fr", "webservices.amazon.fr", "eu-west-1",
            "fr-FR", "fr-FR,fr;q=0.9,en;q=0.6", "EUR", "fr_FR",
        ),
        AmazonMarketplace(
            "IT", "www.amazon.it", "webservices.amazon.it", "eu-west-1",
            "it-IT", "it-IT,it;q=0.9,en;q=0.6", "EUR", "it_IT",
        ),
        AmazonMarketplace(
            "ES", "www.amazon.es", "webservices.amazon.es", "eu-west-1",
            "es-ES", "es-ES,es;q=0.9,en;q=0.6", "EUR", "es_ES",
        ),
        AmazonMarketplace(
            "JP", "www.amazon.co.jp", "webservices.amazon.co.jp", "us-west-2",
            "ja-JP", "ja-JP,ja;q=0.9,en;q=0.6", "JPY", "ja_JP",
        ),
        AmazonMarketplace(
            "AU", "www.amazon.com.au", "webservices.amazon.com.au", "us-west-2",
            "en-AU", "en-AU,en;q=0.9", "AUD", "en_AU",
        ),
        AmazonMarketplace(
            "IN", "www.amazon.in", "webservices.amazon.in", "eu-west-1",
            "en-IN", "en-IN,en;q=0.9", "INR", "en_IN",
        ),
    )
}

# Countries Amazon has no storefront for fall back to the US storefront (still reported
# truthfully: the *returned* currency is detected from the page, not assumed).
DEFAULT_COUNTRY = "US"

AMAZON_DOMAINS: frozenset[str] = frozenset(
    d for m in _MARKETPLACES.values() for d in (m.domain, m.domain.removeprefix("www."))
)


def get_marketplace(country: str | None) -> AmazonMarketplace:
    key = (country or DEFAULT_COUNTRY).strip().upper()
    if key == "UK":
        key = "GB"
    return _MARKETPLACES.get(key, _MARKETPLACES[DEFAULT_COUNTRY])


def marketplace_for_domain(domain: str) -> AmazonMarketplace | None:
    host = domain.lower().removeprefix("www.")
    for m in _MARKETPLACES.values():
        if m.domain.removeprefix("www.") == host:
            return m
    return None


def marketplace_for_url(url: str) -> AmazonMarketplace | None:
    from urllib.parse import urlparse

    host = urlparse(url).hostname
    return marketplace_for_domain(host) if host else None


def all_marketplaces() -> list[AmazonMarketplace]:
    return list(_MARKETPLACES.values())
