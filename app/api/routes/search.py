from __future__ import annotations

from fastapi import APIRouter

from app.api.dependencies import CurrentClientDep, SearchServiceDep
from app.schemas.search import ProductSearchRequest, ProductSearchResponse

router = APIRouter(prefix="/api/v1/search", tags=["Search"])


@router.post("", response_model=ProductSearchResponse, summary="Search Products on Supplier")
async def search_products(
    request: ProductSearchRequest,
    client: CurrentClientDep,
    service: SearchServiceDep,
) -> ProductSearchResponse:
    """
    Search for products across supported suppliers with normalized response format.
    Requires API key authentication.
    """
    return await service.search(request, force_refresh=request.force_refresh)
