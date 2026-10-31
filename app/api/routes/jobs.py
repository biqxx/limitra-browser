from __future__ import annotations

from fastapi import APIRouter, Header, Response, status

from app.api.dependencies import CurrentClientDep, JobManagerDep
from app.schemas.jobs import JobCreateResponse, JobStatusResponse, JobType
from app.schemas.products import ProductRetrievalRequest
from app.schemas.search import ProductSearchRequest

router = APIRouter(prefix="/api/v1/jobs", tags=["Asynchronous Jobs"])


@router.post(
    "/search",
    response_model=JobCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue Search Job",
)
async def queue_search_job(
    request: ProductSearchRequest,
    client: CurrentClientDep,
    manager: JobManagerDep,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> JobCreateResponse:
    """
    Queue an asynchronous search job for long-running queries or deep extraction.
    """
    job_id = await manager.create_job(
        job_type=JobType.SEARCH,
        provider=request.provider,
        request_payload=request.model_dump(mode="json"),
        client_id=client.client_id,
        idempotency_key=idempotency_key,
    )
    return JobCreateResponse(job_id=job_id, idempotency_key=idempotency_key)


@router.post(
    "/product",
    response_model=JobCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue Product Retrieval Job",
)
async def queue_product_job(
    request: ProductRetrievalRequest,
    client: CurrentClientDep,
    manager: JobManagerDep,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> JobCreateResponse:
    """
    Queue an asynchronous product extraction job for pages with extensive variants or slow networks.
    """
    provider = request.provider
    if not provider and request.url:
        try:
            from app.suppliers.registry import get_supplier_registry

            adapter = get_supplier_registry().find_by_url(request.url)
            provider = adapter.provider
        except Exception:
            provider = "unknown"
    provider = provider or "unknown"

    job_id = await manager.create_job(
        job_type=JobType.PRODUCT,
        provider=provider,
        request_payload=request.model_dump(mode="json"),
        client_id=client.client_id,
        idempotency_key=idempotency_key,
    )
    return JobCreateResponse(job_id=job_id, idempotency_key=idempotency_key)


@router.get("/{job_id}", response_model=JobStatusResponse, summary="Get Job Status")
async def get_job_status(
    job_id: str,
    client: CurrentClientDep,
    manager: JobManagerDep,
) -> JobStatusResponse:
    """
    Check the current status, progress stages, and result/error of an asynchronous job.
    """
    return await manager.get_job(job_id)


@router.delete("/{job_id}", summary="Cancel Job", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_job(
    job_id: str,
    client: CurrentClientDep,
    manager: JobManagerDep,
) -> Response:
    """
    Cancel a running or queued job.
    """
    await manager.cancel_job(job_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
