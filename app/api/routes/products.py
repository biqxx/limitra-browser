from __future__ import annotations

from fastapi import APIRouter

from app.api.dependencies import CurrentClientDep, ProductServiceDep
from app.schemas.products import NormalizedProduct, ProductRetrievalRequest

router = APIRouter(prefix="/api/v1/products", tags=["Products"])


@router.post("/retrieve", response_model=NormalizedProduct, summary="Retrieve Normalized Product")
async def retrieve_product(
    request: ProductRetrievalRequest,
    client: CurrentClientDep,
    service: ProductServiceDep,
) -> NormalizedProduct:
    """
    Retrieve complete normalized product by URL or by (provider, external_id).
    Supports force_refresh, variant inclusion, and shipping extraction.
    """
    return await service.retrieve_product(request)


@router.get(
    "/{provider}/{external_id}", response_model=NormalizedProduct, summary="Get Product by ID"
)
async def get_product_by_id(
    provider: str,
    external_id: str,
    client: CurrentClientDep,
    service: ProductServiceDep,
    country: str = "US",
    locale: str = "en-US",
    currency: str = "USD",
    force_refresh: bool = False,
) -> NormalizedProduct:
    """
    Convenience endpoint for product lookup by provider and external ID.
    """
    req = ProductRetrievalRequest(
        provider=provider,
        external_id=external_id,
        country=country,
        locale=locale,
        currency=currency,
        force_refresh=force_refresh,
    )
    return await service.retrieve_product(req)
