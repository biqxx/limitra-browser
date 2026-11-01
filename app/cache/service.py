from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

import structlog
from redis.asyncio import Redis

from app.config import get_settings
from app.schemas.products import NormalizedProduct
from app.schemas.search import ProductSearchResponse

logger = structlog.get_logger(__name__)

_SENTINEL = object()
_GLOBAL_REDIS_CLIENT: Redis | None = None


def get_redis_client() -> Redis | None:
    global _GLOBAL_REDIS_CLIENT
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if _GLOBAL_REDIS_CLIENT is not None:
        pool = getattr(_GLOBAL_REDIS_CLIENT, "connection_pool", None)
        pool_loop = getattr(pool, "_loop", None)
        if pool_loop is not None and (pool_loop.is_closed() or (loop and pool_loop != loop)):
            _GLOBAL_REDIS_CLIENT = None

    if _GLOBAL_REDIS_CLIENT is None and loop is not None:
        try:
            settings = get_settings()
            _GLOBAL_REDIS_CLIENT = Redis.from_url(
                settings.REDIS_URL,
                max_connections=settings.REDIS_MAX_CONNECTIONS,
                decode_responses=False,
                socket_connect_timeout=0.5,
                socket_timeout=1.0,
            )
        except Exception as e:
            logger.warning("redis_client_init_failed", error=str(e))
            _GLOBAL_REDIS_CLIENT = None
    return _GLOBAL_REDIS_CLIENT


class CacheService:
    def __init__(self, redis_client: Any = _SENTINEL):
        self.settings = get_settings()
        self.redis = get_redis_client() if redis_client is _SENTINEL else redis_client
        self._memory_cache: dict[str, tuple[str, float]] = {}  # key -> (json_str, expire_ts)

    def _hash_filters(self, filters: Any) -> str:
        if not filters:
            return "all"
        if hasattr(filters, "model_dump"):
            data = filters.model_dump(exclude_none=True)
        elif isinstance(filters, dict):
            data = filters
        else:
            data = str(filters)
        raw = json.dumps(data, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    def make_product_cache_key(
        self,
        provider: str,
        external_id: str,
        locale: str,
        country: str,
        currency: str,
        version: int = 1,
    ) -> str:
        return f"cache:{provider}:product:{external_id}:{locale}:{country}:{currency}:v{version}"

    def make_search_cache_key(
        self,
        provider: str,
        query: str,
        page: int,
        page_size: int,
        filters: Any,
        locale: str,
        country: str,
        currency: str,
        version: int = 1,
    ) -> str:
        q_norm = query.strip().lower()
        q_hash = hashlib.sha256(q_norm.encode("utf-8")).hexdigest()[:16]
        f_hash = self._hash_filters(filters)
        return f"cache:{provider}:search:{q_hash}:{f_hash}:p{page}:s{page_size}:{locale}:{country}:{currency}:v{version}"

    def make_negative_cache_key(
        self,
        provider: str,
        external_id: str,
        locale: str,
        country: str,
    ) -> str:
        return f"cache:{provider}:negative:{external_id}:{locale}:{country}"

    async def get(self, key: str) -> str | None:
        now = time.time()
        # Check Redis first
        if self.redis is not None:
            try:
                val = await self.redis.get(key)
                if val:
                    return val.decode("utf-8") if isinstance(val, bytes) else str(val)
                return None
            except Exception as e:
                logger.warning("redis_get_failed_fallback_memory", key=key, error=str(e))

        # Check in-memory
        if key in self._memory_cache:
            val, expire_ts = self._memory_cache[key]
            if expire_ts > now:
                return val
            else:
                del self._memory_cache[key]
        return None

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        expire_ts = time.time() + ttl_seconds
        # Write to in-memory
        self._memory_cache[key] = (value, expire_ts)

        # Write to Redis
        if self.redis is not None:
            try:
                await self.redis.set(key, value, ex=ttl_seconds)
            except Exception as e:
                logger.warning("redis_set_failed", key=key, error=str(e))

    async def delete(self, key: str) -> None:
        if key in self._memory_cache:
            del self._memory_cache[key]
        if self.redis is not None:
            try:
                await self.redis.delete(key)
            except Exception as e:
                logger.warning("redis_delete_failed", key=key, error=str(e))

    # Product Cache
    async def get_product(
        self,
        provider: str,
        external_id: str,
        locale: str,
        country: str,
        currency: str,
        version: int = 1,
    ) -> NormalizedProduct | None:
        key = self.make_product_cache_key(provider, external_id, locale, country, currency, version)
        raw = await self.get(key)
        if raw:
            try:
                return NormalizedProduct.model_validate_json(raw)
            except Exception as e:
                logger.error("product_cache_unmarshal_failed", key=key, error=str(e))
        return None

    async def set_product(
        self,
        product: NormalizedProduct,
        version: int = 1,
        ttl_seconds: int | None = None,
        requested_currency: str | None = None,
    ) -> None:
        ttl = ttl_seconds or self.settings.CACHE_PRODUCT_TTL_SECONDS
        key = self.make_product_cache_key(
            product.provider,
            product.external_id,
            product.locale,
            product.country,
            requested_currency or product.currency or "UNKNOWN",
            version,
        )
        val = product.model_dump_json()
        await self.set(key, val, ttl)

    # Search Cache
    async def get_search(
        self,
        provider: str,
        query: str,
        page: int,
        page_size: int,
        filters: Any,
        locale: str,
        country: str,
        currency: str,
        version: int = 1,
    ) -> ProductSearchResponse | None:
        key = self.make_search_cache_key(
            provider, query, page, page_size, filters, locale, country, currency, version
        )
        raw = await self.get(key)
        if raw:
            try:
                resp = ProductSearchResponse.model_validate_json(raw)
                resp.cache_status = "hit"
                return resp
            except Exception as e:
                logger.error("search_cache_unmarshal_failed", key=key, error=str(e))
        return None

    async def set_search(
        self,
        provider: str,
        query: str,
        page: int,
        page_size: int,
        filters: Any,
        locale: str,
        country: str,
        currency: str,
        response: ProductSearchResponse,
        version: int = 1,
        ttl_seconds: int | None = None,
    ) -> None:
        ttl = ttl_seconds or self.settings.CACHE_SEARCH_TTL_SECONDS
        key = self.make_search_cache_key(
            provider, query, page, page_size, filters, locale, country, currency, version
        )
        val = response.model_dump_json()
        await self.set(key, val, ttl)

    # Negative Caching
    async def is_negatively_cached(
        self,
        provider: str,
        external_id: str,
        locale: str,
        country: str,
    ) -> bool:
        key = self.make_negative_cache_key(provider, external_id, locale, country)
        val = await self.get(key)
        return val is not None

    async def set_negative_cache(
        self,
        provider: str,
        external_id: str,
        locale: str,
        country: str,
        ttl_seconds: int | None = None,
    ) -> None:
        ttl = ttl_seconds or self.settings.CACHE_NEGATIVE_TTL_SECONDS
        key = self.make_negative_cache_key(provider, external_id, locale, country)
        await self.set(key, "unavailable", ttl)
