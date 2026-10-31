from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import quote_plus

import structlog

from app.api.errors import (
    InvalidProductUrlException,
    OfficialApiUnavailableException,
    ProductNotFoundException,
)
from app.browser.navigation import safe_navigate
from app.schemas.products import NormalizedProduct, ProductRetrievalRequest
from app.schemas.search import ProductSearchRequest, ProductSearchResponse
from app.suppliers.base import SupplierAdapter, SupplierExecutionContext, SupplierHealth
from app.suppliers.config_schema import SupplierConfigSchema
from app.suppliers.ebay.api_client import EbayApiClient
from app.suppliers.ebay.parser import EbayParser

logger = structlog.get_logger(__name__)


class EbayAdapter(SupplierAdapter):
    provider: str = "ebay"
    supported_domains: set[str] = {
        "ebay.com",
        "www.ebay.com",
        "ebay.co.uk",
        "www.ebay.co.uk",
        "ebay.de",
        "www.ebay.de",
        "ebay.ca",
        "www.ebay.ca",
        "ebay.com.au",
        "www.ebay.com.au",
    }
    supported_locales: set[str] = {"en-US", "en-GB", "de-DE"}
    converter_version: int = 1

    def __init__(self, config_path: str | None = None) -> None:
        self.config: SupplierConfigSchema | None
        if config_path is None:
            config_path = str(Path(__file__).parent / "selectors.yaml")
        if os.path.exists(config_path):
            self.config = SupplierConfigSchema.load_from_file(config_path)
            self.supported_domains = set(self.config.domains)
            self.converter_version = self.config.version
        else:
            self.config = None

        self.parser = EbayParser(self.config)
        self.api_client = EbayApiClient()

    @property
    def capabilities(self) -> dict[str, bool]:
        return {
            "product_search": True,
            "product_retrieval": True,
            "variant_retrieval": True,
            "shipping_information": True,
            "seller_information": True,
            "reviews_summary": True,
            "official_api_support": self.api_client.is_configured,
            "browser_fallback_support": True,
        }

    def recognizes_url(self, url: str) -> bool:
        u = url.lower()
        return any(d in u for d in self.supported_domains)

    def extract_external_id(self, url: str) -> str | None:
        # Formats: /itm/123456789012 or /itm/item-title/123456789012 or /itm/v1|123456789012|0
        m = re.search(r"/itm/(?:[^/?#]+/)?(\d+)", url)
        if m:
            return m.group(1)
        m_v1 = re.search(r"/itm/v1\|(\d+)\|", url)
        if m_v1:
            return m_v1.group(1)
        return None

    def normalize_url(self, url: str) -> str:
        external_id = self.extract_external_id(url)
        if not external_id:
            raise InvalidProductUrlException(
                f"Could not extract eBay item ID from URL: {url}", provider="ebay"
            )
        return f"https://www.ebay.com/itm/{external_id}"

    async def search(
        self,
        request: ProductSearchRequest,
        context: SupplierExecutionContext,
    ) -> ProductSearchResponse:
        # Priority 1: Official API if configured
        if self.api_client.is_configured:
            try:
                return await self.api_client.search(
                    query=request.query,
                    page=request.page,
                    page_size=request.page_size,
                    country=request.country,
                    currency=request.currency,
                    locale=request.locale,
                )
            except Exception as e:
                logger.warning("ebay_official_api_search_fallback", error=str(e))

        # Priority 2: Browser fallback
        if not context.browser_manager:
            raise OfficialApiUnavailableException(
                "ebay", "eBay official API is not configured and browser execution is disabled."
            )

        encoded_q = quote_plus(request.query)
        search_url = f"https://www.ebay.com/sch/i.html?_nkw={encoded_q}&_pgn={request.page}"

        async with await context.browser_manager.get_session(
            provider="ebay",
            locale=request.locale,
            country=request.country,
            job_id=context.job_id,
        ) as page:
            await safe_navigate(
                page, search_url, provider="ebay", allowed_domains=self.supported_domains
            )
            html = await page.content()
            items = self.parser.parse_search_results(html, provider="ebay")

            return ProductSearchResponse(
                provider="ebay",
                query=request.query,
                page=request.page,
                page_size=request.page_size,
                total_results=len(items),
                items=items,
                applied_filters=request.filters.model_dump(exclude_none=True)
                if request.filters
                else {},
                cache_status="miss",
            )

    async def get_product(
        self,
        request: ProductRetrievalRequest,
        context: SupplierExecutionContext,
    ) -> NormalizedProduct:
        external_id = request.external_id
        target_url = request.url

        if target_url:
            external_id = self.extract_external_id(target_url)
            if not external_id:
                raise InvalidProductUrlException(
                    f"Invalid eBay product URL: {target_url}", provider="ebay"
                )
            target_url = self.normalize_url(target_url)
        elif external_id:
            target_url = f"https://www.ebay.com/itm/{external_id}"
        else:
            raise InvalidProductUrlException(
                "Either 'url' or 'external_id' must be provided.", provider="ebay"
            )

        # Priority 1: Official API if configured
        if self.api_client.is_configured:
            try:
                return await self.api_client.get_item(
                    item_id=external_id,
                    country=request.country,
                    currency=request.currency,
                    locale=request.locale,
                )
            except ProductNotFoundException:
                raise
            except Exception as e:
                logger.warning("ebay_official_api_get_item_fallback", error=str(e))

        # Priority 2: Browser fallback
        if not context.browser_manager:
            raise OfficialApiUnavailableException(
                "ebay", "eBay official API is not configured and browser execution is disabled."
            )

        async with await context.browser_manager.get_session(
            provider="ebay",
            locale=request.locale,
            country=request.country,
            job_id=context.job_id,
        ) as page:
            await safe_navigate(
                page, target_url, provider="ebay", allowed_domains=self.supported_domains
            )
            html = await page.content()
            return self.parser.parse_product_page(
                html=html,
                url=target_url,
                external_id=external_id,
                locale=request.locale,
                country=request.country,
                currency=request.currency,
            )

    async def healthcheck(self) -> SupplierHealth:
        api_ok = self.api_client.is_configured
        return SupplierHealth(
            provider="ebay",
            status="healthy" if api_ok else "degraded",
            official_api_configured=api_ok,
            official_api_healthy=api_ok,
            browser_fallback_available=True,
            supported_domains=list(self.supported_domains),
            supported_locales=list(self.supported_locales),
            error_message=None
            if api_ok
            else "Official API credentials not configured; browser fallback only.",
        )
