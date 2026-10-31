from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import text

from app.api.compat.auth import verify_internal_secret
from app.api.compat.mappers import (
    map_normalized_product_to_legacy,
    map_search_item_to_legacy,
)
from app.api.compat.schemas import (
    AvailabilityRequest,
    AvailabilityResult,
    DependencyStatus,
    DetailedHealthResponse,
    LegacyHealthResponse,
    LegacyProduct,
    LegacySearchResult,
    LegacyUnifiedSearchResult,
    OrderPayload,
    QuickBuyPayload,
    QuickBuyVerifyResponse,
)
from app.api.compat.services import (
    get_availability_service,
    get_quick_buy_service,
)
from app.api.errors import (
    ProductNotFoundException,
    RateLimitExceededException,
)
from app.cache.service import get_redis_client
from app.config import get_settings
from app.database.session import get_db_session
from app.schemas.products import ProductRetrievalRequest
from app.schemas.search import ProductSearchRequest
from app.services.product_service import ProductService
from app.services.search_service import SearchService
from app.suppliers.registry import get_supplier_registry

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Legacy Compatibility Layer"])


# ---------------------------------------------------------------------------
# Public Health Endpoints (Bypasses internal auth)
# ---------------------------------------------------------------------------


@router.get("/health", response_model=LegacyHealthResponse, summary="Legacy Health Check")
@router.head("/health", summary="Legacy Health Check HEAD")
async def legacy_health() -> LegacyHealthResponse:
    return LegacyHealthResponse(status="ok", service="limitra-supplier-service")


@router.get(
    "/health/detailed",
    response_model=DetailedHealthResponse,
    summary="Legacy Detailed Health Check",
)
@router.head("/health/detailed", summary="Legacy Detailed Health Check HEAD")
async def legacy_health_detailed() -> DetailedHealthResponse:
    deps: list[DependencyStatus] = []

    # 1. Postgres
    pg_dep = DependencyStatus(name="postgres", status="unknown")
    try:
        t0 = time.time()
        async with get_db_session() as session:
            await session.execute(text("SELECT 1"))
        latency = f"{round((time.time() - t0) * 1000, 2)}ms"
        pg_dep.status = "ok"
        pg_dep.latency = latency
    except Exception as e:
        pg_dep.status = "error"
        pg_dep.error = str(e)
    deps.append(pg_dep)

    # 2. Redis
    redis_dep = DependencyStatus(name="redis", status="unknown")
    try:
        t0 = time.time()
        redis = get_redis_client()
        if redis:
            await redis.ping()
            redis_dep.status = "ok"
            redis_dep.latency = f"{round((time.time() - t0) * 1000, 2)}ms"
        else:
            redis_dep.status = "error"
            redis_dep.error = "redis client not initialized"
    except Exception as e:
        redis_dep.status = "error"
        redis_dep.error = str(e)
    deps.append(redis_dep)

    # 3. Amazon Adapter
    registry = get_supplier_registry()
    amazon_dep = DependencyStatus(name="amazon", status="unknown")
    try:
        adapter = registry.get("amazon")
        amazon_dep.status = "ok" if adapter else "error"
    except Exception as e:
        amazon_dep.status = "error"
        amazon_dep.error = str(e)
    deps.append(amazon_dep)

    # 4. eBay Adapter
    ebay_dep = DependencyStatus(name="ebay", status="unknown")
    try:
        adapter = registry.get("ebay")
        ebay_dep.status = "ok" if adapter else "error"
    except Exception as e:
        ebay_dep.status = "error"
        ebay_dep.error = str(e)
    deps.append(ebay_dep)

    # 5. Background Workers
    worker_dep = DependencyStatus(name="workers", status="unknown")
    try:
        from app.workers.redis_queue import check_active_workers

        has_workers = await check_active_workers()
        settings = get_settings()
        if settings.ENVIRONMENT.lower() == "production" and not has_workers:
            worker_dep.status = "warning"
            worker_dep.error = "no active worker heartbeats detected in redis"
        else:
            worker_dep.status = "ok"
    except Exception as e:
        worker_dep.status = "error"
        worker_dep.error = str(e)
    deps.append(worker_dep)

    # Overall status
    has_error = any(d.status == "error" for d in deps)
    overall = "error" if has_error else "ok"

    return DetailedHealthResponse(
        service="limitra-supplier-service",
        status=overall,
        dependencies=deps,
        checked_at=datetime.now(timezone.utc).isoformat(),
    )



# ---------------------------------------------------------------------------
# Authenticated Compatibility Endpoints (Requires X-Internal-Service-Secret)
# ---------------------------------------------------------------------------


@router.get(
    "/search",
    response_model=LegacyUnifiedSearchResult,
    dependencies=[Depends(verify_internal_secret)],
    summary="Unified Search",
)
async def unified_search(
    q: str = Query(..., description="Search query"),
    page: int = Query(1, ge=1, description="Page number"),
    supplier: str = Query("all", description="Supplier: all, amazon, ebay"),
) -> LegacyUnifiedSearchResult:
    query = q.strip()
    if not query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "q is required"},
        )

    supplier_norm = supplier.strip().lower()
    if supplier_norm not in ("all", "amazon", "ebay"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "supplier must be all, amazon, or ebay"},
        )

    search_service = SearchService()

    async def _search_provider(
        prov: str,
    ) -> tuple[str, list[LegacyProduct], int, Exception | None]:
        req = ProductSearchRequest(
            provider=prov,
            query=query,
            page=page,
            page_size=20,
        )
        try:
            resp = await search_service.search(req)
            prods = [map_search_item_to_legacy(item) for item in resp.items]
            total_items = resp.total_results if resp.total_results is not None else len(prods)
            return prov, prods, total_items, None
        except Exception as exc:
            logger.error("compat_search_error", provider=prov, query=query, error=str(exc))
            return prov, [], 0, exc

    if supplier_norm in ("amazon", "ebay"):
        prov, prods, total, err = await _search_provider(supplier_norm)
        if err is not None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error": f"{supplier_norm} search failed"},
            )
        return LegacyUnifiedSearchResult(
            products=prods,
            total=total,
            page=page,
            supplier=supplier_norm,
        )

    # supplier_norm == "all": Concurrent search
    results = await asyncio.gather(
        _search_provider("amazon"),
        _search_provider("ebay"),
        return_exceptions=False,
    )

    combined_products: list[LegacyProduct] = []
    total_count = 0
    warnings: list[str] = []
    failures = 0

    for prov, prods, tot, err in results:
        if err is not None:
            failures += 1
            warnings.append(f"{prov}: unavailable")
            continue
        combined_products.extend(prods)
        total_count += tot

    if failures == len(results):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "all suppliers failed"},
        )

    return LegacyUnifiedSearchResult(
        products=combined_products,
        total=total_count,
        page=page,
        supplier="all",
        warnings=warnings if warnings else None,
    )


@router.get(
    "/search/amazon",
    response_model=LegacySearchResult,
    dependencies=[Depends(verify_internal_secret)],
    summary="Amazon Search",
)
async def search_amazon(
    q: str = Query(..., description="Search query"),
    page: int = Query(1, ge=1, description="Page number"),
) -> LegacySearchResult:
    query = q.strip()
    if not query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "q is required"},
        )

    search_service = SearchService()
    req = ProductSearchRequest(
        provider="amazon",
        query=query,
        page=page,
        page_size=20,
    )
    try:
        resp = await search_service.search(req)
        prods = [map_search_item_to_legacy(item) for item in resp.items]
        total_items = resp.total_results if resp.total_results is not None else len(prods)
        return LegacySearchResult(products=prods, total=total_items, page=page)
    except Exception as exc:
        logger.error("compat_search_amazon_failed", query=query, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "amazon search failed"},
        ) from exc


@router.get(
    "/search/ebay",
    response_model=LegacySearchResult,
    dependencies=[Depends(verify_internal_secret)],
    summary="eBay Search",
)
async def search_ebay(
    q: str = Query(..., description="Search query"),
    page: int = Query(1, ge=1, description="Page number"),
) -> LegacySearchResult:
    query = q.strip()
    if not query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "q is required"},
        )

    search_service = SearchService()
    req = ProductSearchRequest(
        provider="ebay",
        query=query,
        page=page,
        page_size=20,
    )
    try:
        resp = await search_service.search(req)
        prods = [map_search_item_to_legacy(item) for item in resp.items]
        total_items = resp.total_results if resp.total_results is not None else len(prods)
        return LegacySearchResult(products=prods, total=total_items, page=page)
    except Exception as exc:
        logger.error("compat_search_ebay_failed", query=query, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "ebay search failed"},
        ) from exc


# ---------------------------------------------------------------------------
# Details and Import Endpoints
# ---------------------------------------------------------------------------


async def _fetch_product_details(supplier: str, external_id: str) -> LegacyProduct:
    supp = supplier.strip().lower()
    ext_id = external_id.strip()

    if supp not in ("amazon", "ebay"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "unknown supplier"},
        )

    product_service = ProductService()
    req = ProductRetrievalRequest(provider=supp, external_id=ext_id)
    try:
        normalized = await product_service.retrieve_product(req)
        return map_normalized_product_to_legacy(normalized)
    except ProductNotFoundException as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "product not found"},
        ) from e
    except RateLimitExceededException as e:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "rate limited"},
        ) from e
    except Exception as e:
        logger.error("compat_details_failed", supplier=supp, external_id=ext_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": f"{supp} fetch failed"},
        ) from e


@router.get(
    "/details/amazon/{asin}",
    response_model=LegacyProduct,
    dependencies=[Depends(verify_internal_secret)],
    summary="Amazon Product Details",
)
async def details_amazon(asin: str) -> LegacyProduct:
    return await _fetch_product_details("amazon", asin)


@router.post(
    "/import/amazon/{asin}",
    response_model=LegacyProduct,
    dependencies=[Depends(verify_internal_secret)],
    summary="Amazon Import (Details Alias)",
)
async def import_amazon(asin: str) -> LegacyProduct:
    return await _fetch_product_details("amazon", asin)


@router.get(
    "/details/ebay/{itemId}",
    response_model=LegacyProduct,
    dependencies=[Depends(verify_internal_secret)],
    summary="eBay Product Details",
)
async def details_ebay(itemId: str) -> LegacyProduct:
    return await _fetch_product_details("ebay", itemId)


@router.post(
    "/import/ebay/{itemId}",
    response_model=LegacyProduct,
    dependencies=[Depends(verify_internal_secret)],
    summary="eBay Import (Details Alias)",
)
async def import_ebay(itemId: str) -> LegacyProduct:
    return await _fetch_product_details("ebay", itemId)


@router.get(
    "/details/{supplier}/{external_id}",
    response_model=LegacyProduct,
    dependencies=[Depends(verify_internal_secret)],
    summary="Generic Supplier Product Details",
)
async def details_supplier(supplier: str, external_id: str) -> LegacyProduct:
    return await _fetch_product_details(supplier, external_id)


@router.post(
    "/import/{supplier}/{external_id}",
    response_model=LegacyProduct,
    dependencies=[Depends(verify_internal_secret)],
    summary="Generic Supplier Product Import",
)
async def import_supplier(supplier: str, external_id: str) -> LegacyProduct:
    return await _fetch_product_details(supplier, external_id)


# ---------------------------------------------------------------------------
# Availability Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/availability",
    response_model=AvailabilityResult,
    dependencies=[Depends(verify_internal_secret)],
    summary="Check Availability",
)
async def check_availability(req: AvailabilityRequest) -> AvailabilityResult:
    supp = req.supplier.strip().lower()
    if supp not in ("amazon", "ebay"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "supplier must be amazon or ebay"},
        )

    avail_svc = get_availability_service()
    try:
        return await avail_svc.check_availability(req)
    except ProductNotFoundException:
        return AvailabilityResult(
            supplier=supp,
            supplier_id=req.supplier_id,
            available=False,
            price=0.0,
        )
    except Exception as e:
        logger.error(
            "compat_availability_error",
            supplier=supp,
            supplier_id=req.supplier_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "availability check failed"},
        ) from e


# ---------------------------------------------------------------------------
# Quick Buy Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/quick-buy/payload",
    response_model=QuickBuyPayload,
    dependencies=[Depends(verify_internal_secret)],
    summary="Sign Quick Buy Payload",
)
async def quick_buy_sign(order: OrderPayload) -> QuickBuyPayload:
    supp = order.supplier.strip().lower()
    if supp not in ("amazon", "ebay"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "supplier must be amazon or ebay"},
        )

    quick_buy_svc = get_quick_buy_service()
    try:
        return await quick_buy_svc.sign_payload(order)
    except Exception as e:
        logger.error("compat_quickbuy_sign_error", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "sign payload failed"},
        ) from e


@router.post(
    "/quick-buy/verify",
    response_model=QuickBuyVerifyResponse,
    dependencies=[Depends(verify_internal_secret)],
    summary="Verify Quick Buy Payload",
)
async def quick_buy_verify(payload: QuickBuyPayload) -> QuickBuyVerifyResponse:
    quick_buy_svc = get_quick_buy_service()
    valid = await quick_buy_svc.verify_payload(payload)
    return QuickBuyVerifyResponse(valid=valid)


# ---------------------------------------------------------------------------
# Order Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/order",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    dependencies=[Depends(verify_internal_secret)],
    summary="Place Order Session (Not Implemented)",
)
async def place_order(payload: OrderPayload) -> None:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "error": "Automated supplier checkout is not implemented. Direct automated purchasing is disabled in production to protect sourcing integrity."
        },
    )


@router.post(
    "/order/{sessionId}/confirm",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    dependencies=[Depends(verify_internal_secret)],
    summary="Confirm Order Session (Not Implemented)",
)
async def confirm_order(
    sessionId: str,
    x_confirm_token: str | None = Header(None, alias="X-Confirm-Token"),
) -> None:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "error": "Automated supplier checkout is not implemented. Direct automated purchasing is disabled in production to protect sourcing integrity."
        },
    )
