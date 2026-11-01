"""Supplier image URL validation: https, absolute, deduplicated and supplier-hosted."""

from __future__ import annotations

from urllib.parse import urlparse

from app.schemas.products import ProductImage

# Hosts (suffix match) each provider legitimately serves product imagery from.
SUPPLIER_IMAGE_HOSTS: dict[str, tuple[str, ...]] = {
    "amazon": (
        "media-amazon.com",
        "ssl-images-amazon.com",
        "images-amazon.com",
        "amazon.com",
        "amazon.co.uk",
        "amazon.de",
        "amazon.ca",
        "amazon.co.jp",
        "amazon.com.au",
        "amazon.in",
        "amazon.fr",
        "amazon.it",
        "amazon.es",
    ),
    "ebay": ("ebayimg.com", "ebaystatic.com", "ebay.com", "ebay.co.uk", "ebay.de", "ebay.ca"),
}


def _host_allowed(provider: str, host: str) -> bool:
    suffixes = SUPPLIER_IMAGE_HOSTS.get(provider.lower())
    if suffixes is None:
        return True  # unknown provider: scheme/absolute checks only
    host = host.lower()
    return any(host == s or host.endswith("." + s) for s in suffixes)


def normalize_image_url(url: str | None, provider: str) -> tuple[str | None, str | None]:
    """Return ``(clean_url, rejection_reason)``."""
    if not url or not isinstance(url, str):
        return None, "empty"
    candidate = url.strip()
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None, "not_absolute_http_url"
    if parsed.scheme == "http":
        candidate = "https://" + candidate[len("http://") :]
        parsed = urlparse(candidate)
    if not _host_allowed(provider, parsed.hostname or ""):
        return None, "not_supplier_hosted"
    if parsed.path.lower().endswith((".gif",)) and "pixel" in parsed.path.lower():
        return None, "tracking_pixel"
    return candidate, None


def sanitize_images(
    images: list[ProductImage], provider: str
) -> tuple[list[ProductImage], list[str]]:
    """Dedupe, force https and drop non-supplier-hosted images. Returns (images, warnings)."""
    seen: set[str] = set()
    clean: list[ProductImage] = []
    dropped = 0
    for img in images:
        url, reason = normalize_image_url(img.url, provider)
        if url is None:
            dropped += 1
            continue
        if url in seen:
            continue
        seen.add(url)
        thumb, _ = normalize_image_url(img.thumbnail_url, provider)
        clean.append(
            img.model_copy(update={"url": url, "thumbnail_url": thumb, "position": len(clean)})
        )
    warnings: list[str] = []
    if dropped:
        warnings.append(f"{dropped} image URL(s) were dropped (not https/absolute/supplier-hosted)")
    return clean, warnings
