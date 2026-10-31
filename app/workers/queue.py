from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import structlog
from sqlalchemy import and_, or_, select, update

from app.api.errors import JobNotFoundException, SupplierServiceException
from app.config import Settings, get_settings
from app.database.models import ExtractionAttempt, ExtractionJob
from app.database.session import get_db_session
from app.schemas.jobs import JobProgress, JobStatus, JobStatusResponse, JobType
from app.schemas.products import ProductRetrievalRequest
from app.schemas.search import ProductSearchRequest
from app.services.product_service import ProductService
from app.services.search_service import SearchService
from app.workers.redis_queue import (
    acknowledge_job,
    enqueue_job,
    enqueue_job_id,
    publish_dead_letter,
)

logger = structlog.get_logger(__name__)


def classify_extraction_error(exc: Exception) -> tuple[bool, str]:
    """
    Distinguishes retryable failures from permanent failures.
    Returns (is_retryable, error_category).
    """
    if isinstance(exc, SupplierServiceException):
        return exc.retryable, exc.code.value

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True, "timeout"

    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectTimeout, httpx.ReadTimeout)):
        return True, "supplier_timeout"

    if isinstance(exc, (httpx.NetworkError, httpx.ConnectError, ConnectionError, OSError)):
        return True, "network_error"

    if hasattr(exc, "status_code"):
        code = exc.status_code
        if code in (429, 500, 502, 503, 504):
            return True, f"http_{code}"
        return False, f"http_{code}"

    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return False, "invalid_request"

    err_str = str(exc).lower()
    if any(k in err_str for k in ("timeout", "rate limit", "temporar", "connection reset", "econnreset")):
        return True, "transient_error"

    return False, type(exc).__name__


def calculate_backoff_with_jitter(
    attempt: int,
    base: float = 1.0,
    max_backoff: float = 30.0,
) -> float:
    """
    Computes exponential backoff with jitter to prevent thundering herd.
    """
    backoff = min(max_backoff, base * (2 ** max(0, attempt - 1)))
    jitter = random.uniform(0.8, 1.2)
    return float(round(backoff * jitter, 2))



class JobQueueManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.search_service = SearchService()
        self.product_service = ProductService()
        self._active_tasks: set[asyncio.Task[Any]] = set()

    async def create_job(
        self,
        job_type: JobType,
        provider: str,
        request_payload: dict[str, Any],
        client_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        async with get_db_session() as session:
            # Check idempotency
            if idempotency_key:
                stmt = select(ExtractionJob).where(
                    ExtractionJob.idempotency_key == idempotency_key,
                    ExtractionJob.status.notin_(["failed", "cancelled"]),
                )
                res = await session.execute(stmt)
                existing = res.scalar_one_or_none()
                if existing:
                    logger.info(
                        "job_idempotency_hit", idempotency_key=idempotency_key, job_id=existing.id
                    )
                    return str(existing.id)

            job = ExtractionJob(
                job_type=job_type.value,
                provider=provider,
                request_payload=request_payload,
                status=JobStatus.QUEUED.value,
                stage="queued",
                progress_pct=0,
                attempt_count=0,
                max_attempts=self.settings.MAX_JOB_ATTEMPTS,
                client_id=client_id,
                idempotency_key=idempotency_key,
            )
            session.add(job)
            await session.commit()
            await session.refresh(job)

            # Enqueue to durable Redis Stream queue
            enqueued = await enqueue_job_id(str(job.id))
            if not enqueued and self.settings.ENVIRONMENT != "testing":
                logger.info("redis_queue_bypassed_using_in_memory_task", job_id=job.id)
                task = asyncio.create_task(self.process_job(str(job.id)))
                self._active_tasks.add(task)
                task.add_done_callback(self._active_tasks.discard)

            return str(job.id)

    async def get_job(self, job_id: str) -> JobStatusResponse:
        async with get_db_session() as session:
            stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
            res = await session.execute(stmt)
            job = res.scalar_one_or_none()

            if not job:
                raise JobNotFoundException(job_id)

            return JobStatusResponse(
                job_id=str(job.id),
                job_type=JobType(job.job_type),
                status=JobStatus(job.status),
                provider=str(job.provider),
                progress=JobProgress(stage=str(job.stage), percentage=job.progress_pct),
                created_at=job.created_at,
                started_at=job.started_at,
                completed_at=job.completed_at,
                result=job.result_payload,
                error=job.error_payload,
                idempotency_key=job.idempotency_key,
            )

    async def cancel_job(self, job_id: str) -> None:
        async with get_db_session() as session:
            stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
            res = await session.execute(stmt)
            job = res.scalar_one_or_none()

            if not job:
                raise JobNotFoundException(job_id)

            if job.status in [JobStatus.COMPLETED.value, JobStatus.FAILED.value]:
                return

            job.status = JobStatus.CANCELLED.value
            job.completed_at = datetime.now(timezone.utc)
            await session.commit()

    async def update_progress(self, job_id: str, stage: str, pct: int) -> None:
        async with get_db_session() as session:
            stmt = (
                update(ExtractionJob)
                .where(ExtractionJob.id == job_id)
                .values(stage=stage, progress_pct=pct)
            )
            await session.execute(stmt)
            await session.commit()

    async def claim_job(self, job_id: str) -> tuple[bool, ExtractionJob | None]:
        """
        Atomically claims a database job before processing it using a conditional update.
        Only a job in 'queued' or 'retrying' (or timed-out running past stale threshold)
        can transition to 'running'.
        Returns (claimed, job_instance).
        """
        async with get_db_session() as session:
            now = datetime.now(timezone.utc)
            stale_threshold = now - timedelta(
                seconds=self.settings.JOB_STALE_RUNNING_TIMEOUT_SECONDS
            )

            stmt = (
                update(ExtractionJob)
                .where(
                    ExtractionJob.id == job_id,
                    or_(
                        ExtractionJob.status.in_([JobStatus.QUEUED.value, "retrying"]),
                        and_(
                            ExtractionJob.status == JobStatus.RUNNING.value,
                            ExtractionJob.started_at < stale_threshold,
                        ),
                    ),
                )
                .values(
                    status=JobStatus.RUNNING.value,
                    stage="initializing",
                    started_at=now,
                    attempt_count=ExtractionJob.attempt_count + 1,
                )
            )
            res = await session.execute(stmt)
            await session.commit()

            rowcount = getattr(res, "rowcount", 0)
            if rowcount == 0:
                return False, None


            fetch_stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
            fetch_res = await session.execute(fetch_stmt)
            claimed_job = fetch_res.scalar_one_or_none()
            return True, claimed_job

    async def _delayed_re_enqueue(self, job_id: str, delay_s: float) -> None:
        """Helper to re-enqueue a retrying job to the Redis stream after backoff."""
        try:
            await asyncio.sleep(delay_s)
            await enqueue_job(job_id)
            logger.info("job_re_enqueued_to_redis_stream", job_id=job_id)
        except Exception as e:
            logger.warning("job_re_enqueue_failed", job_id=job_id, error=str(e))

    async def process_job(self, job_id: str, message_id: str | None = None) -> bool:
        """
        Processes a background job atomically with durable retry & dead-letter handling.
        Does not acknowledge a Redis Stream message until the job state has been safely persisted.
        """
        start_ts = datetime.now(timezone.utc)
        logger.info("attempting_to_claim_job", job_id=job_id)

        claimed, job = await self.claim_job(job_id)
        if not claimed or not job:
            # Check if job was already completed, cancelled, or failed
            async with get_db_session() as session:
                chk_stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
                chk_res = await session.execute(chk_stmt)
                existing = chk_res.scalar_one_or_none()
                if existing and existing.status in (
                    JobStatus.COMPLETED.value,
                    JobStatus.CANCELLED.value,
                    JobStatus.FAILED.value,
                ):
                    logger.info(
                        "job_already_terminal_ignoring_duplicate_message",
                        job_id=job_id,
                        status=existing.status,
                        message_id=message_id,
                    )
                    if message_id:
                        await acknowledge_job(message_id)
                else:
                    logger.info(
                        "job_ineligible_for_claim_currently_running",
                        job_id=job_id,
                        status=existing.status if existing else "unknown",
                    )
            return False

        logger.info(
            "job_claimed_by_worker",
            job_id=job_id,
            attempt=job.attempt_count,
            max_attempts=job.max_attempts,
        )

        job_type = job.job_type
        payload = dict(job.request_payload)
        current_attempt = job.attempt_count
        max_attempts = job.max_attempts

        try:
            # Execution
            await self.update_progress(job_id, stage="executing_extraction", pct=40)

            if job_type == JobType.SEARCH.value:
                req_search = ProductSearchRequest.model_validate(payload)
                search_res = await self.search_service.search(req_search, job_id=job_id)
                result_data = search_res.model_dump(mode="json")
            else:
                req_prod = ProductRetrievalRequest.model_validate(payload)
                prod_res = await self.product_service.retrieve_product(req_prod, job_id=job_id)
                result_data = prod_res.model_dump(mode="json")

            await self.update_progress(job_id, stage="finalizing", pct=90)

            # Record success and commit DB
            duration_ms = int((datetime.now(timezone.utc) - start_ts).total_seconds() * 1000)
            async with get_db_session() as session:
                stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
                res = await session.execute(stmt)
                db_job = res.scalar_one_or_none()
                if db_job and db_job.status != JobStatus.CANCELLED.value:
                    db_job.status = JobStatus.COMPLETED.value
                    db_job.stage = "completed"
                    db_job.progress_pct = 100
                    db_job.result_payload = result_data
                    db_job.completed_at = datetime.now(timezone.utc)

                    attempt = ExtractionAttempt(
                        job_id=job_id,
                        attempt_number=current_attempt,
                        strategy_used="service_pipeline",
                        duration_ms=duration_ms,
                        status="success",
                    )
                    session.add(attempt)
                    await session.commit()

            # Acknowledge Redis Stream message ONLY after job state is safely persisted in DB
            if message_id:
                await acknowledge_job(message_id)

            logger.info("job_completed_successfully", job_id=job_id, attempt=current_attempt)
            return True

        except asyncio.CancelledError:
            logger.info("job_task_cancelled", job_id=job_id)
            return False

        except Exception as e:
            duration_ms = int((datetime.now(timezone.utc) - start_ts).total_seconds() * 1000)
            retryable, category = classify_extraction_error(e)
            logger.error(
                "background_job_failed",
                job_id=job_id,
                error=str(e),
                category=category,
                retryable=retryable,
                attempt=current_attempt,
                max_attempts=max_attempts,
            )

            async with get_db_session() as session:
                stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
                res = await session.execute(stmt)
                db_job = res.scalar_one_or_none()
                if not db_job or db_job.status == JobStatus.CANCELLED.value:
                    if message_id:
                        await acknowledge_job(message_id)
                    return False

                # Record attempt history
                attempt = ExtractionAttempt(
                    job_id=job_id,
                    attempt_number=current_attempt,
                    strategy_used="service_pipeline",
                    duration_ms=duration_ms,
                    status="failed",
                    error_category=category,
                    error_message=str(e),
                )
                session.add(attempt)

                if retryable and current_attempt < max_attempts:
                    db_job.status = "retrying"
                    db_job.stage = f"retrying_attempt_{current_attempt}"
                    db_job.error_payload = {
                        "message": str(e),
                        "category": category,
                        "attempt": current_attempt,
                        "max_attempts": max_attempts,
                        "retryable": True,
                    }
                    await session.commit()

                    # Safely persisted state before ACK
                    if message_id:
                        await acknowledge_job(message_id)

                    delay = calculate_backoff_with_jitter(
                        current_attempt,
                        base=self.settings.JOB_RETRY_BASE_BACKOFF_SECONDS,
                        max_backoff=self.settings.JOB_RETRY_MAX_BACKOFF_SECONDS,
                    )
                    logger.info(
                        "scheduling_job_retry",
                        job_id=job_id,
                        attempt=current_attempt,
                        delay_seconds=delay,
                    )
                    task = asyncio.create_task(self._delayed_re_enqueue(job_id, delay))
                    self._active_tasks.add(task)
                    task.add_done_callback(self._active_tasks.discard)
                    return False
                else:
                    # Final failure / Dead-letter
                    db_job.status = JobStatus.FAILED.value
                    db_job.stage = "dead_letter"
                    db_job.error_payload = {
                        "message": str(e),
                        "category": category,
                        "attempt": current_attempt,
                        "max_attempts": max_attempts,
                        "retryable": False,
                        "final": True,
                    }
                    db_job.completed_at = datetime.now(timezone.utc)
                    await session.commit()

                    # Move/record in dead-letter structure
                    await publish_dead_letter(job_id, db_job.error_payload)

                    if message_id:
                        await acknowledge_job(message_id)
                    return False


_GLOBAL_JOB_MANAGER: JobQueueManager | None = None


def get_job_manager() -> JobQueueManager:
    global _GLOBAL_JOB_MANAGER
    if _GLOBAL_JOB_MANAGER is None:
        _GLOBAL_JOB_MANAGER = JobQueueManager()
    return _GLOBAL_JOB_MANAGER
