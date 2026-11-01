import socket
from collections.abc import Callable, Coroutine
from typing import Any
from urllib.parse import urlsplit

import structlog
from playwright.async_api import Request, Route

from app.security.ssrf import is_ip_blocked

logger = structlog.get_logger(__name__)

# Tracker, ad, and analytics domains to abort to save bandwidth and prevent blocking
BLOCKED_URL_PATTERNS = [
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "scorecardresearch.com",
    "adsystem.com",
    "amazon-adsystem.com",
    "criteo.net",
    "hotjar.com",
    "clarity.ms",
    "analytics",
    "telemetry",
    "advertising",
]

SUPPLIER_ALLOWED_DOMAINS: dict[str, set[str]] = {
    "amazon": {
        "amazon.com",
        "media-amazon.com",
        "ssl-images-amazon.com",
        "a-cdn.com",
        "amazon.co.uk",
        "amazon.de",
        "amazon.ca",
        "amazon.co.jp",
        "amazon.com.au",
        "amazon.in",
        "amazon.fr",
        "amazon.it",
        "amazon.es",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
    },
    "ebay": {
        "ebay.com",
        "ebayimg.com",
        "ebaystatic.com",
        "ebayrtm.com",
        "ebaydesc.com",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
    },
}

_DNS_VALIDATION_CACHE: dict[str, bool] = {}


def is_subrequest_safe(
    url: str,
    resource_type: str = "other",
    provider: str | None = None,
    allow_dev_hosts: bool = False,
) -> tuple[bool, str]:
    """
    Evaluates whether a browser subrequest should be permitted or aborted.
    Blocks SSRF risks (cloud metadata, loopback, RFC1918), forbidden schemes,
    embedded credentials, tracking scripts, and heavy streaming media.
    """
    url_lower = url.lower().strip()

    # Allow inline data / blob for images and fonts
    if url_lower.startswith("data:") or url_lower.startswith("blob:"):
        if resource_type in ("image", "font"):
            return True, "allowed_inline_asset"
        return False, "forbidden_data_blob_scheme"

    # Only http and https allowed
    if not (url_lower.startswith("http://") or url_lower.startswith("https://")):
        return False, f"forbidden_scheme_{url[:10]}"

    try:
        parts = urlsplit(url)
    except Exception:
        return False, "malformed_url"

    # Block embedded credentials
    if parts.username or parts.password:
        return False, "embedded_credentials_forbidden"

    hostname = parts.hostname
    if not hostname:
        return False, "missing_hostname"

    hostname_lower = hostname.lower()

    # Check dev hosts
    from app.config import get_settings

    settings = get_settings()
    if allow_dev_hosts and hostname_lower in settings.allowed_dev_hosts_list:
        return True, "allowed_dev_host"

    # Direct IP literal check
    import ipaddress

    try:
        ipaddress.ip_address(hostname_lower)
        if is_ip_blocked(hostname_lower):
            return False, f"blocked_ip_{hostname_lower}"
    except ValueError:
        pass

    # Tracker / ad blocking
    for pattern in BLOCKED_URL_PATTERNS:
        if pattern in url_lower:
            return False, f"blocked_ad_tracker_{pattern}"

    # Streaming media blocking
    if resource_type == "media":
        return False, "blocked_streaming_media"

    # Supplier domain allowlist enforcement if provider configured
    if provider and provider.lower() in SUPPLIER_ALLOWED_DOMAINS:
        allowed = SUPPLIER_ALLOWED_DOMAINS[provider.lower()]
        matches = any(
            hostname_lower == dom or hostname_lower.endswith("." + dom) for dom in allowed
        )
        if not matches:
            return False, f"provider_{provider}_domain_not_allowed_{hostname_lower}"

    # DNS check (cached)
    if hostname_lower not in _DNS_VALIDATION_CACHE:
        try:
            addr_info = socket.getaddrinfo(hostname_lower, None)
            is_safe = True
            for item in addr_info:
                ip_str = str(item[4][0])
                if is_ip_blocked(ip_str):
                    is_safe = False
                    break
            _DNS_VALIDATION_CACHE[hostname_lower] = is_safe
        except Exception:
            _DNS_VALIDATION_CACHE[hostname_lower] = False

    if not _DNS_VALIDATION_CACHE[hostname_lower]:
        return False, f"dns_resolves_to_blocked_ip_{hostname_lower}"

    return True, "allowed"


async def secure_browser_route_handler(
    route: Route,
    request: Request,
    provider: str | None = None,
) -> None:
    """
    Playwright route handler enforcing subrequest SSRF security,
    ad-blocking, and media suppression.
    """
    url = request.url
    resource_type = request.resource_type

    safe, reason = is_subrequest_safe(url, resource_type, provider=provider)
    if not safe:
        logger.debug("browser_subrequest_blocked", url=url[:120], reason=reason)
        await route.abort("blockedbyclient")
        return

    await route.continue_()


def create_browser_route_handler(
    provider: str | None = None,
) -> Callable[[Route, Request], Coroutine[Any, Any, None]]:
    async def _handler(route: Route, request: Request) -> None:
        await secure_browser_route_handler(route, request, provider=provider)

    return _handler


async def block_unnecessary_resources(route: Route, request: Request) -> None:
    """Backward compatibility alias for the secure route handler."""
    await secure_browser_route_handler(route, request, provider=None)


def get_browser_context_options(
    locale: str = "en-US",
    country: str = "US",
    timezone_id: str = "America/New_York",
    user_agent: str | None = None,
) -> dict[str, Any]:
    """
    Standard browser context parameters with consistent client hints and navigation headers.
    No bot-detection evasion or deceptive spoofing is applied.
    """
    from app.config import get_settings

    settings = get_settings()

    ua = user_agent or settings.BROWSER_USER_AGENT

    # Natural desktop client hints and navigation headers matching standard Chrome
    lang_pref = (
        f"{locale},{locale.split('-')[0]};q=0.9,en;q=0.8" if "-" in locale else f"{locale},en;q=0.9"
    )
    extra_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": lang_pref,
        "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }

    return {
        "viewport": {"width": 1440, "height": 900},
        "locale": locale,
        "timezone_id": timezone_id,
        "user_agent": ua,
        "extra_http_headers": extra_headers,
        "java_script_enabled": True,
        "ignore_https_errors": False,
        "bypass_csp": False,
    }
