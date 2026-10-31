from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta
from typing import Any

import structlog

from app.api.compat.mappers import map_normalized_product_to_legacy
from app.api.compat.schemas import (
    AvailabilityRequest,
    AvailabilityResult,
    OrderPayload,
    OrderResult,
    OrderSession,
    QuickBuyPayload,
    utc_now,
)
from app.cache.service import get_redis_client
from app.config import get_settings
from app.schemas.products import ProductRetrievalRequest
from app.services.product_service import ProductService

logger = structlog.get_logger(__name__)


class QuickBuyService:
    def __init__(self, signing_secret: str | None = None) -> None:
        self.settings = get_settings()
        self.signing_secret = signing_secret or self.settings.QUICK_BUY_SIGNING_SECRET
        self._consumed_nonces: dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    def _compute_signature(self, to_sign_dict: dict[str, Any]) -> str:
        # Canonical compact JSON serialization
        canonical = json.dumps(to_sign_dict, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        return hmac.new(
            self.signing_secret.encode("utf-8"),
            canonical,
            hashlib.sha256,
        ).hexdigest()

    async def sign_payload(self, order: OrderPayload) -> QuickBuyPayload:
        nonce = secrets.token_hex(16)
        expires_at = utc_now() + timedelta(minutes=15)

        to_sign = {
            "order_payload": order.model_dump(),
            "nonce": nonce,
            "expires_at": expires_at.isoformat(),
        }

        signature = self._compute_signature(to_sign)

        return QuickBuyPayload(
            order_payload=order,
            nonce=nonce,
            signature=signature,
            expires_at=expires_at,
        )

    async def verify_payload(self, payload: QuickBuyPayload) -> bool:
        now = utc_now()
        if now > payload.expires_at:
            logger.warning("quickbuy_payload_expired", expires_at=payload.expires_at)
            return False

        if not payload.nonce or not payload.signature:
            logger.warning("quickbuy_missing_nonce_or_signature")
            return False

        to_sign = {
            "order_payload": payload.order_payload.model_dump(),
            "nonce": payload.nonce,
            "expires_at": payload.expires_at.isoformat(),
        }

        expected_sig = self._compute_signature(to_sign)
        if not hmac.compare_digest(payload.signature.strip(), expected_sig):
            logger.warning("quickbuy_signature_mismatch")
            return False

        # Replay protection: 1. Check local in-memory consumed nonces
        async with self._lock:
            if payload.nonce in self._consumed_nonces:
                logger.warning("quickbuy_nonce_replayed", nonce=payload.nonce)
                return False

        # 2. Check/consume in Redis for distributed multi-worker consistency
        redis = get_redis_client()
        if redis:
            try:
                key = f"quickbuy:nonce:{payload.nonce}"
                remaining_seconds = max(1, int((payload.expires_at - now).total_seconds()))
                acquired = await redis.set(key, "consumed", ex=remaining_seconds, nx=True)
                if not acquired:
                    logger.warning("quickbuy_nonce_replayed", nonce=payload.nonce)
                    return False
            except Exception as e:
                logger.warning("redis_nonce_check_failed_fallback_to_memory", error=str(e))

        # 3. Record in local memory
        async with self._lock:
            self._consumed_nonces[payload.nonce] = payload.expires_at
            # Prune expired nonces
            self._consumed_nonces = {
                n: exp for n, exp in self._consumed_nonces.items() if exp > now
            }

        return True


class AvailabilityService:
    def __init__(self, product_service: ProductService | None = None) -> None:
        self.product_service = product_service or ProductService()
        self._mem_cache: dict[str, tuple[AvailabilityResult, float]] = {}
        self._lock = asyncio.Lock()

    async def check_availability(self, req: AvailabilityRequest) -> AvailabilityResult:
        cache_key = f"avail:{req.supplier.lower()}:{req.supplier_id}"
        now_ts = time.time()

        # 1. Check Redis cache (150s TTL)
        redis = get_redis_client()
        if redis:
            try:
                cached_data = await redis.get(cache_key)
                if cached_data:
                    raw = (
                        cached_data.decode("utf-8")
                        if isinstance(cached_data, bytes)
                        else cached_data
                    )
                    data = json.loads(raw)
                    return AvailabilityResult(
                        supplier=data["supplier"],
                        supplier_id=data["supplier_id"],
                        available=data["available"],
                        price=data["price"],
                        checked_at=datetime.fromisoformat(data["checked_at"]),
                    )
            except Exception as e:
                logger.warning("availability_cache_read_error", error=str(e))

        # Check in-memory fallback cache
        async with self._lock:
            cached_mem = self._mem_cache.get(cache_key)
            if cached_mem:
                res, exp_ts = cached_mem
                if exp_ts > now_ts:
                    return res
                del self._mem_cache[cache_key]

        # 2. Live Supplier Fetch
        product_req = ProductRetrievalRequest(
            provider=req.supplier.lower(),
            external_id=req.supplier_id,
        )
        normalized = await self.product_service.retrieve_product(product_req)
        legacy = map_normalized_product_to_legacy(normalized)

        result = AvailabilityResult(
            supplier=req.supplier.lower(),
            supplier_id=req.supplier_id,
            available=legacy.available,
            price=legacy.price,
            checked_at=utc_now(),
        )

        # 3. Store in Redis and in-memory cache (150-second TTL)
        if redis:
            try:
                payload = json.dumps(
                    {
                        "supplier": result.supplier,
                        "supplier_id": result.supplier_id,
                        "available": result.available,
                        "price": result.price,
                        "checked_at": result.checked_at.isoformat(),
                    }
                )
                await redis.set(cache_key, payload, ex=150)
            except Exception as e:
                logger.warning("availability_cache_write_error", error=str(e))

        async with self._lock:
            self._mem_cache[cache_key] = (result, now_ts + 150)

        return result


class OrderService:
    def __init__(self, availability_service: AvailabilityService | None = None) -> None:
        self.avail_service = availability_service or AvailabilityService()
        self._sessions: dict[str, OrderSession] = {}
        self._lock = asyncio.Lock()
        self._session_ttl = timedelta(minutes=30)

    async def place_order(self, payload: OrderPayload) -> OrderSession:
        # Verify availability first
        avail = await self.avail_service.check_availability(
            AvailabilityRequest(
                supplier=payload.supplier,
                supplier_id=payload.supplier_id,
            )
        )

        if not avail.available:
            raise ValueError("product is unavailable")

        session_id = secrets.token_hex(16)
        confirm_token = secrets.token_hex(32)

        session = OrderSession(
            session_id=session_id,
            supplier=payload.supplier,
            supplier_id=payload.supplier_id,
            status="pending_confirmation",
            order_payload=payload,
            confirm_token=confirm_token,
            created_at=utc_now(),
        )

        async with self._lock:
            # Prune expired
            now = utc_now()
            self._sessions = {
                k: s for k, s in self._sessions.items() if (now - s.created_at) < self._session_ttl
            }
            self._sessions[session_id] = session

        return session

    async def confirm_order(self, session_id: str, confirm_token: str) -> OrderResult:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("session not found")

            if (utc_now() - session.created_at) > self._session_ttl:
                del self._sessions[session_id]
                raise KeyError("session expired")

            if not hmac.compare_digest(session.confirm_token, confirm_token.strip()):
                raise PermissionError("invalid confirm token")

            session.status = "confirmed"

        return OrderResult(
            session_id=session_id,
            supplier_order_ref=f"SUP-{secrets.token_hex(4).upper()}",
            status="confirmed",
            confirmed_at=utc_now(),
        )


# Global singletons
_quick_buy_service: QuickBuyService | None = None
_availability_service: AvailabilityService | None = None
_order_service: OrderService | None = None


def get_quick_buy_service() -> QuickBuyService:
    global _quick_buy_service
    if _quick_buy_service is None:
        _quick_buy_service = QuickBuyService()
    return _quick_buy_service


def get_availability_service() -> AvailabilityService:
    global _availability_service
    if _availability_service is None:
        _availability_service = AvailabilityService()
    return _availability_service


def get_order_service() -> OrderService:
    global _order_service
    if _order_service is None:
        _order_service = OrderService(get_availability_service())
    return _order_service
