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
from app.suppliers.amazon.api_client import AmazonApiClient
from app.suppliers.amazon.parser import AmazonParser
from app.suppliers.base import SupplierAdapter, SupplierExecutionContext, SupplierHealth
from app.suppliers.config_schema import SupplierConfigSchema

logger = structlog.get_logger(__name__)


class AmazonAdapter(SupplierAdapter):
    provider: str = "amazon"
    supported_domains: set[str] = {
        "amazon.com",
        "www.amazon.com",
        "amazon.co.uk",
        "www.amazon.co.uk",
        "amazon.de",
        "www.amazon.de",
        "amazon.ca",
        "www.amazon.ca",
        "amazon.co.jp",
        "www.amazon.co.jp",
    }
    supported_locales: set[str] = {"en-US", "en-GB", "de-DE", "ja-JP"}
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

        self.parser = AmazonParser(self.config)
        self.api_client = AmazonApiClient()

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
        # ASIN is 10 alphanumeric characters (e.g. B08N5WRWNW)
        # Patterns: /dp/B08N5WRWNW, /gp/product/B08N5WRWNW, /product/B08N5WRWNW, /d/B08N5WRWNW
        m = re.search(r"/(?:dp|gp/product|product|d)/([A-Z0-9]{10})", url, re.I)
        if m:
            return m.group(1).upper()
        # Direct asin parameter
        m_param = re.search(r"[?&]asin=([A-Z0-9]{10})", url, re.I)
        if m_param:
            return m_param.group(1).upper()
        return None

    def normalize_url(self, url: str) -> str:
        asin = self.extract_external_id(url)
        if not asin:
            raise InvalidProductUrlException(
                f"Could not extract Amazon ASIN from URL: {url}", provider="amazon"
            )
        return f"https://www.amazon.com/dp/{asin}"

    async def search(
        self,
        request: ProductSearchRequest,
        context: SupplierExecutionContext,
    ) -> ProductSearchResponse:
        # Priority 1: Official PA-API if configured
        if self.api_client.is_configured:
            try:
                return await self.api_client.search_items(
                    query=request.query,
                    page=request.page,
                    page_size=request.page_size,
                    currency=request.currency,
                    locale=request.locale,
                )
            except Exception as e:
                logger.warning("amazon_official_api_search_fallback", error=str(e))

        # Priority 2: Browser fallback
        if not context.browser_manager:
            raise OfficialApiUnavailableException(
                "amazon",
                "Amazon official PA-API is not configured and browser execution is disabled.",
            )

        encoded_q = quote_plus(request.query)
        search_url = f"https://www.amazon.com/s?k={encoded_q}&page={request.page}"

        async with await context.browser_manager.get_session(
            provider="amazon",
            locale=request.locale,
            country=request.country,
            job_id=context.job_id,
        ) as page:
            await safe_navigate(
                page, search_url, provider="amazon", allowed_domains=self.supported_domains
            )
            html = await page.content()
            items = self.parser.parse_search_results(html, provider="amazon")

            return ProductSearchResponse(
                provider="amazon",
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
        asin = request.external_id
        target_url = request.url

        if target_url:
            asin = self.extract_external_id(target_url)
            if not asin:
                raise InvalidProductUrlException(
                    f"Invalid Amazon product URL: {target_url}", provider="amazon"
                )
            target_url = self.normalize_url(target_url)
        elif asin:
            target_url = f"https://www.amazon.com/dp/{asin.upper()}"
            asin = asin.upper()
        else:
            raise InvalidProductUrlException(
                "Either 'url' or 'external_id' (ASIN) must be provided.", provider="amazon"
            )

        # Priority 1: Official PA-API if configured
        if self.api_client.is_configured:
            try:
                return await self.api_client.get_items(
                    asin=asin,
                    currency=request.currency,
                    locale=request.locale,
                )
            except ProductNotFoundException:
                raise
            except Exception as e:
                logger.warning("amazon_official_api_get_items_fallback", error=str(e))

        # Priority 2: Browser fallback
        if not context.browser_manager:
            raise OfficialApiUnavailableException(
                "amazon",
                "Amazon official PA-API is not configured and browser execution is disabled.",
            )

        async with await context.browser_manager.get_session(
            provider="amazon",
            locale=request.locale,
            country=request.country,
            job_id=context.job_id,
        ) as page:
            await safe_navigate(
                page, target_url, provider="amazon", allowed_domains=self.supported_domains
            )
            html = await page.content()
            return self.parser.parse_product_page(
                html=html,
                url=target_url,
                asin=asin,
                locale=request.locale,
                country=request.country,
                currency=request.currency,
            )

    async def healthcheck(self) -> SupplierHealth:
        api_ok = self.api_client.is_configured
        return SupplierHealth(
            provider="amazon",
            status="healthy" if api_ok else "degraded",
            official_api_configured=api_ok,
            official_api_healthy=api_ok,
            browser_fallback_available=True,
            supported_domains=list(self.supported_domains),
            supported_locales=list(self.supported_locales),
            error_message=None
            if api_ok
            else "Official PA-API credentials not configured; browser fallback only.",
        )
