from __future__ import annotations

import re

import structlog
from playwright.async_api import Page, Response
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.api.errors import (
    InvalidProductUrlException,
    SupplierBlockedException,
    SupplierTimeoutException,
)
from app.config import get_settings
from app.security.ssrf import normalize_and_validate_url

logger = structlog.get_logger(__name__)

# Common indicators of supplier anti-bot / access restrictions / CAPTCHA
BLOCK_PATTERNS = [
    re.compile(r"enter the characters you see", re.I),
    re.compile(r"type the characters you see in this image", re.I),
    re.compile(r"robot check", re.I),
    re.compile(r"automated access", re.I),
    re.compile(r"captcha", re.I),
    re.compile(r"access denied", re.I),
    re.compile(r"cf-chl-bypass", re.I),
    re.compile(r"cloudflare", re.I),
    re.compile(r"security check to continue", re.I),
    re.compile(r"unusual traffic from your computer network", re.I),
    re.compile(r"sign in to your account to view this page", re.I),
    re.compile(r"cs_503", re.I),
    re.compile(r"dogsofamazon", re.I),
    re.compile(r"something went wrong on our end", re.I),
]


async def check_for_supplier_block(
    page: Page, provider: str, status_code: int | None = None
) -> None:
    """
    Checks if supplier returned an access-denied, CAPTCHA, or anti-bot block page.
    Compliance requirement:
    Never attempt to bypass CAPTCHA/bot detection.
    Return structured supplier_blocked error and recommend official API.
    """
    if status_code in (403, 429, 503):
        logger.warning("supplier_http_block_detected", provider=provider, status=status_code)
        raise SupplierBlockedException(
            provider=provider,
            reason=f"HTTP {status_code} Access Denied / Automated Access Restricted",
        )

    try:
        title = await page.title()
        title_lower = title.lower()
        if (
            "robot check" in title_lower
            or "access denied" in title_lower
            or "captcha" in title_lower
            or "sorry! something went wrong" in title_lower
        ):
            logger.warning("supplier_title_block_detected", provider=provider, title=title)
            raise SupplierBlockedException(provider=provider, reason=f"Page Title Block: {title}")

        # Check body text snippet
        content = await page.content()
        content_lower = content[:50000].lower()  # inspect first 50KB

        for pattern in BLOCK_PATTERNS:
            if pattern.search(content_lower):
                logger.warning(
                    "supplier_content_block_detected", provider=provider, pattern=pattern.pattern
                )
                raise SupplierBlockedException(
                    provider=provider,
                    reason=f"Block signature match: {pattern.pattern}",
                )

    except SupplierBlockedException:
        raise
    except Exception as e:
        logger.debug("block_check_non_fatal_error", error=str(e))


async def safe_navigate(
    page: Page,
    url: str,
    provider: str,
    allowed_domains: set[str],
    timeout_ms: int | None = None,
) -> Response | None:
    """
    Safely navigates page to target URL:
    - Pre-validates URL via SSRF protection
    - Sets up navigation timeout
    - Tracks redirects and validates redirect URLs against allowlist
    - Detects supplier access restrictions and raises SupplierBlockedException
    """
    settings = get_settings()
    timeout = timeout_ms or settings.BROWSER_NAVIGATION_TIMEOUT_MS

    # Pre-validate URL
    validated_url = normalize_and_validate_url(url, allowed_domains=allowed_domains)

    # Listen to navigation responses / redirects
    async def handle_response(response: Response) -> None:
        if response.frame == page.main_frame and response.request.is_navigation_request():
            resp_url = response.url
            try:
                normalize_and_validate_url(resp_url, allowed_domains=allowed_domains)
            except Exception as e:
                logger.error("redirect_to_unauthorized_domain", target=resp_url, error=str(e))
                raise InvalidProductUrlException(
                    f"Redirected to disallowed domain: {resp_url}", provider=provider
                ) from e

    page.on("response", handle_response)

    try:
        response = await page.goto(
            validated_url,
            wait_until="domcontentloaded",
            timeout=timeout,
        )

        # Validate final URL after all redirects
        final_url = page.url
        if final_url and final_url != "about:blank":
            try:
                normalize_and_validate_url(final_url, allowed_domains=allowed_domains)
            except Exception as e:
                logger.error("final_page_url_disallowed", final_url=final_url, error=str(e))
                raise InvalidProductUrlException(
                    f"Redirected to disallowed domain: {final_url}", provider=provider
                ) from e

        status_code = response.status if response else None

        # Check for block/CAPTCHA
        await check_for_supplier_block(page, provider=provider, status_code=status_code)

        return response

    except PlaywrightTimeoutError as e:
        raise SupplierTimeoutException(
            f"Navigation to '{url}' timed out after {timeout}ms.",
            provider=provider,
        ) from e

    except SupplierBlockedException:
        raise
    except InvalidProductUrlException:
        raise
    except Exception as e:
        logger.error("browser_navigation_failed", url=url, error=str(e))
        raise
    finally:
        page.remove_listener("response", handle_response)
