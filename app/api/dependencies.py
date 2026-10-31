from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request

from app.security.auth import AuthenticatedClient, authenticate_request
from app.services.product_service import ProductService
from app.services.search_service import SearchService
from app.workers.queue import JobQueueManager, get_job_manager


async def get_current_client(
    request: Request,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> AuthenticatedClient:
    return await authenticate_request(request, x_api_key=x_api_key, require_admin=False)


async def get_admin_client(
    request: Request,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> AuthenticatedClient:
    return await authenticate_request(request, x_api_key=x_api_key, require_admin=True)


def get_search_service() -> SearchService:
    return SearchService()


def get_product_service() -> ProductService:
    return ProductService()


def get_job_manager_dep() -> JobQueueManager:
    return get_job_manager()


CurrentClientDep = Annotated[AuthenticatedClient, Depends(get_current_client)]
AdminClientDep = Annotated[AuthenticatedClient, Depends(get_admin_client)]
SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]
ProductServiceDep = Annotated[ProductService, Depends(get_product_service)]
JobManagerDep = Annotated[JobQueueManager, Depends(get_job_manager_dep)]
