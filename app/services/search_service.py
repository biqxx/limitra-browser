from __future__ import annotations

import time

import structlog

from app.browser.manager import get_browser_manager
from app.cache.lock import distributed_lock
from app.cache.service import CacheService
from app.observability.metrics import (
    CACHE_OPERATIONS_TOTAL,
    SUPPLIER_REQUEST_DURATION_SECONDS,
    SUPPLIER_REQUESTS_TOTAL,
)
from app.schemas.search import ProductSearchRequest, ProductSearchResponse
from app.suppliers.base import SupplierExecutionContext
from app.suppliers.registry import get_supplier_registry

logger = structlog.get_logger(__name__)


class SearchService:
    def __init__(self, cache_service: CacheService | None = None):
        self.cache = cache_service or CacheService()
        self.registry = get_supplier_registry()
        self.browser_manager = get_browser_manager()

    async def search(
        self,
        request: ProductSearchRequest,
        job_id: str | None = None,
        force_refresh: bool = False,
    ) -> ProductSearchResponse:
        adapter = self.registry.get(request.provider)
        start_time = time.time()

        # 1. Check Cache
        if not force_refresh:
            cached = await self.cache.get_search(
                provider=request.provider,
                query=request.query,
                page=request.page,
                page_size=request.page_size,
                filters=request.filters,
                locale=request.locale,
                country=request.country,
                currency=request.currency,
                version=adapter.converter_version,
            )
            if cached:
                CACHE_OPERATIONS_TOTAL.labels(cache_type="search", status="hit").inc()
                logger.info(
                    "search_cache_hit",
                    provider=request.provider,
                    query=request.query,
                    page=request.page,
                )
                return cached

        CACHE_OPERATIONS_TOTAL.labels(cache_type="search", status="miss").inc()

        # 2. Distributed Lock to prevent duplicate concurrent searches
        lock_key = f"search:{request.provider}:{request.query}:{request.page}:{request.locale}"
        async with distributed_lock(self.cache.redis, lock_key, ttl_seconds=20):
            # Check cache once more after lock
            if not force_refresh:
                cached_recheck = await self.cache.get_search(
                    provider=request.provider,
                    query=request.query,
                    page=request.page,
                    page_size=request.page_size,
                    filters=request.filters,
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
                include_raw=request.include_raw,
                force_refresh=force_refresh,
                browser_manager=self.browser_manager,
            )

            try:
                response = await adapter.search(request, context)
                duration = time.time() - start_time

                SUPPLIER_REQUESTS_TOTAL.labels(
                    provider=request.provider,
                    operation="search",
                    status="success",
                ).inc()
                SUPPLIER_REQUEST_DURATION_SECONDS.labels(
                    provider=request.provider,
                    operation="search",
                    strategy="adapter",
                ).observe(duration)

                # Store in cache
                await self.cache.set_search(
                    provider=request.provider,
                    query=request.query,
                    page=request.page,
                    page_size=request.page_size,
                    filters=request.filters,
                    locale=request.locale,
                    country=request.country,
                    currency=request.currency,
                    response=response,
                    version=adapter.converter_version,
                )

                logger.info(
                    "search_completed",
                    provider=request.provider,
                    query=request.query,
                    results_count=len(response.items),
                    duration_ms=int(duration * 1000),
                )
                return response

            except Exception as e:
                SUPPLIER_REQUESTS_TOTAL.labels(
                    provider=request.provider,
                    operation="search",
                    status="failed",
                ).inc()
                logger.error(
                    "search_failed",
                    provider=request.provider,
                    query=request.query,
                    error=str(e),
                )
                raise
