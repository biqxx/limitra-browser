from __future__ import annotations

import time

import structlog

from app.api.errors import (
    InvalidProductUrlException,
    ProductNotFoundException,
    ProductUnavailableException,
)
from app.browser.manager import get_browser_manager
from app.cache.lock import distributed_lock
from app.cache.service import CacheService
from app.observability.metrics import (
    CACHE_OPERATIONS_TOTAL,
    SUPPLIER_REQUEST_DURATION_SECONDS,
    SUPPLIER_REQUESTS_TOTAL,
)
from app.schemas.products import NormalizedProduct, ProductRetrievalRequest
from app.services.data_quality import assess_product
from app.suppliers.base import SupplierExecutionContext
from app.suppliers.registry import get_supplier_registry

logger = structlog.get_logger(__name__)


class ProductService:
    def __init__(self, cache_service: CacheService | None = None):
        self.cache = cache_service or CacheService()
        self.registry = get_supplier_registry()
        self.browser_manager = get_browser_manager()

    async def retrieve_product(
        self,
        request: ProductRetrievalRequest,
        job_id: str | None = None,
    ) -> NormalizedProduct:
        start_time = time.time()

        # 1. Resolve Adapter
        if request.url:
            adapter = self.registry.find_by_url(request.url)
            provider = adapter.provider
            external_id = adapter.extract_external_id(request.url)
            if not external_id:
                raise InvalidProductUrlException(
                    f"Could not extract external product ID from URL: {request.url}",
                    provider=provider,
                )
        elif request.provider and request.external_id:
            provider = request.provider.lower().strip()
            adapter = self.registry.get(provider)
            external_id = request.external_id
        else:
            raise InvalidProductUrlException(
                "Either 'url' or both 'provider' and 'external_id' must be specified."
            )

        # 2. Check Negative Cache
        if not request.force_refresh:
            if await self.cache.is_negatively_cached(
                provider, external_id, request.locale, request.country
            ):
                logger.info("negative_cache_hit", provider=provider, external_id=external_id)
                raise ProductUnavailableException(
                    f"Product '{external_id}' was previously marked unavailable on {provider}.",
                    provider=provider,
                )

        # 3. Check Positive Product Cache
        if not request.force_refresh:
            cached = await self.cache.get_product(
                provider=provider,
                external_id=external_id,
                locale=request.locale,
                country=request.country,
                currency=request.currency,
                version=adapter.converter_version,
            )
            if cached:
                CACHE_OPERATIONS_TOTAL.labels(cache_type="product", status="hit").inc()
                logger.info("product_cache_hit", provider=provider, external_id=external_id)
                return cached

        CACHE_OPERATIONS_TOTAL.labels(cache_type="product", status="miss").inc()

        # 4. Distributed Lock to prevent duplicate concurrent extractions
        lock_key = f"product:{provider}:{external_id}:{request.locale}"
        async with distributed_lock(self.cache.redis, lock_key, ttl_seconds=30):
            # Recheck cache inside lock
            if not request.force_refresh:
                cached_recheck = await self.cache.get_product(
                    provider=provider,
                    external_id=external_id,
                    locale=request.locale,
                    country=request.country,
                    currency=request.currency,
                    version=adapter.converter_version,
                )
                if cached_recheck:
                    return cached_recheck

            context = SupplierExecutionContext(
                job_id=job_id,
                locale=request.locale,
                country=request.country,
                currency=request.currency,
                include_variants=request.include_variants,
                include_shipping=request.include_shipping,
                include_raw=request.include_raw,
                force_refresh=request.force_refresh,
                browser_manager=self.browser_manager,
            )

            try:
                product = await adapter.get_product(request, context)
                product = assess_product(product, requested_currency=request.currency)
                duration = time.time() - start_time

                SUPPLIER_REQUESTS_TOTAL.labels(
                    provider=provider,
                    operation="get_product",
                    status="success",
                ).inc()
                SUPPLIER_REQUEST_DURATION_SECONDS.labels(
                    provider=provider,
                    operation="get_product",
                    strategy="adapter",
                ).observe(duration)

                # Store in cache
                await self.cache.set_product(
                    product,
                    version=adapter.converter_version,
                    requested_currency=request.currency,
                )

                logger.info(
                    "product_retrieval_completed",
                    provider=provider,
                    external_id=external_id,
                    price=product.current_price,
                    currency=product.currency,
                    duration_ms=int(duration * 1000),
                )
                return product

            except (ProductNotFoundException, ProductUnavailableException):
                # Negative cache for unavailable items
                await self.cache.set_negative_cache(
                    provider, external_id, request.locale, request.country
                )
                SUPPLIER_REQUESTS_TOTAL.labels(
                    provider=provider, operation="get_product", status="not_found"
                ).inc()
                raise
            except Exception as e:
                SUPPLIER_REQUESTS_TOTAL.labels(
                    provider=provider, operation="get_product", status="failed"
                ).inc()
                logger.error(
                    "product_retrieval_failed",
                    provider=provider,
                    external_id=external_id,
                    error=str(e),
                )
                raise
