from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from app.api.errors import InvalidRequestException, SupplierTimeoutException
from app.database.models import ExtractionAttempt, ExtractionJob
from app.database.session import get_db_session, init_db
from app.schemas.jobs import JobStatus, JobType
from app.workers.queue import (
    JobQueueManager,
    calculate_backoff_with_jitter,
    classify_extraction_error,
)


@pytest.fixture(autouse=True)
async def setup_db():
    await init_db()


def test_classify_extraction_error():
    # Transient / Retryable errors
    assert classify_extraction_error(TimeoutError())[0] is True
    assert classify_extraction_error(httpx.ConnectTimeout("timeout"))[0] is True
    assert classify_extraction_error(httpx.NetworkError("network err"))[0] is True
    assert classify_extraction_error(SupplierTimeoutException("timed out", "ebay"))[0] is True

    # Permanent errors
    assert classify_extraction_error(InvalidRequestException("bad url"))[0] is False
    assert classify_extraction_error(ValueError("invalid format"))[0] is False
    assert classify_extraction_error(KeyError("missing_field"))[0] is False


def test_calculate_backoff_with_jitter():
    # Test attempt 1 with base 1.0s -> ~1.0s (within jitter range 0.8 to 1.2)
    b1 = calculate_backoff_with_jitter(1, base=1.0, max_backoff=30.0)
    assert 0.7 <= b1 <= 1.3

    # Test attempt 2 -> base * 2 = 2.0s -> ~2.0s (1.6 to 2.4)
    b2 = calculate_backoff_with_jitter(2, base=1.0, max_backoff=30.0)
    assert 1.5 <= b2 <= 2.5

    # Test attempt 3 -> base * 4 = 4.0s -> ~4.0s (3.2 to 4.8)
    b3 = calculate_backoff_with_jitter(3, base=1.0, max_backoff=30.0)
    assert 3.0 <= b3 <= 5.0

    # Test capped at max_backoff
    b_max = calculate_backoff_with_jitter(10, base=1.0, max_backoff=10.0)
    assert b_max <= 12.5  # 10.0 * 1.2 max jitter


@pytest.mark.asyncio
async def test_transient_failure_triggers_retry_and_preserves_history():
    """
    Proves that a retryable error increments attempt count, sets status to retrying,
    preserves attempt history in database, and does not immediately mark as dead letter.
    """
    manager = JobQueueManager()
    job_id = await manager.create_job(
        job_type=JobType.SEARCH,
        provider="ebay",
        request_payload={"provider": "ebay", "query": "retry test"},
    )

    mock_error = SupplierTimeoutException("eBay gateway timeout", "ebay")
    mock_search = AsyncMock(side_effect=mock_error)
    mock_ack = AsyncMock(return_value=True)

    with (
        patch.object(manager.search_service, "search", mock_search),
        patch("app.workers.queue.acknowledge_job", mock_ack),
        patch.object(manager, "_delayed_re_enqueue", AsyncMock()),
    ):
        success = await manager.process_job(job_id, message_id="msg-attempt-1")
        assert success is False
        # Redis message must be acknowledged only after DB update is committed
        mock_ack.assert_awaited_with("msg-attempt-1")

    # Verify Database state
    async with get_db_session() as session:
        stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
        res = await session.execute(stmt)
        db_job = res.scalar_one_or_none()
        assert db_job is not None
        assert db_job.status == "retrying"
        assert db_job.attempt_count == 1
        assert db_job.error_payload is not None
        assert db_job.error_payload["retryable"] is True

        # Check attempt history
        att_stmt = select(ExtractionAttempt).where(ExtractionAttempt.job_id == job_id)
        att_res = await session.execute(att_stmt)
        attempts = att_res.scalars().all()
        assert len(attempts) == 1
        assert attempts[0].attempt_number == 1
        assert attempts[0].status == "failed"
        assert "gateway timeout" in str(attempts[0].error_message)


@pytest.mark.asyncio
async def test_permanent_failure_moves_directly_to_dead_letter():
    """
    Proves that non-retryable errors (like InvalidRequestException) do NOT retry
    and are immediately moved to dead-letter state with dead-letter publication.
    """
    manager = JobQueueManager()
    job_id = await manager.create_job(
        job_type=JobType.PRODUCT,
        provider="amazon",
        request_payload={"product_id": "invalid-asin"},
    )

    mock_error = InvalidRequestException("Invalid Amazon ASIN format", "amazon")
    mock_retrieve = AsyncMock(side_effect=mock_error)
    mock_dead_letter = AsyncMock(return_value="dl-msg-1")
    mock_ack = AsyncMock(return_value=True)

    with (
        patch.object(manager.product_service, "retrieve_product", mock_retrieve),
        patch("app.workers.queue.publish_dead_letter", mock_dead_letter),
        patch("app.workers.queue.acknowledge_job", mock_ack),
    ):
        success = await manager.process_job(job_id, message_id="msg-perm-fail")
        assert success is False
        mock_dead_letter.assert_awaited_once()
        mock_ack.assert_awaited_with("msg-perm-fail")

    # Verify Database State
    async with get_db_session() as session:
        stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
        res = await session.execute(stmt)
        db_job = res.scalar_one_or_none()
        assert db_job is not None
        assert db_job.status == JobStatus.FAILED.value
        assert db_job.stage == "dead_letter"
        assert db_job.error_payload is not None
        assert db_job.error_payload["final"] is True
        assert db_job.error_payload["retryable"] is False



@pytest.mark.asyncio
async def test_max_attempts_exhausted_moves_to_dead_letter():
    """
    Proves that when retryable errors reach max_attempts, the job is transitioned
    to failed (dead_letter) and published to the dead-letter stream.
    """
    manager = JobQueueManager()
    # Set job already at attempt 2 with max_attempts = 3
    async with get_db_session() as session:
        job = ExtractionJob(
            job_type=JobType.SEARCH.value,
            provider="ebay",
            request_payload={"query": "exhausted attempts"},
            status="retrying",
            stage="retrying_attempt_2",
            attempt_count=2,
            max_attempts=3,
        )
        session.add(job)
        await session.commit()
        job_id = str(job.id)

    mock_error = httpx.ReadTimeout("Read timeout")
    mock_search = AsyncMock(side_effect=mock_error)
    mock_dead_letter = AsyncMock(return_value="dl-msg-exhausted")
    mock_ack = AsyncMock(return_value=True)

    with (
        patch.object(manager.search_service, "search", mock_search),
        patch("app.workers.queue.publish_dead_letter", mock_dead_letter),
        patch("app.workers.queue.acknowledge_job", mock_ack),
    ):
        # Attempt 3 (reaches max_attempts 3)
        success = await manager.process_job(job_id, message_id="msg-attempt-3")
        assert success is False
        mock_dead_letter.assert_awaited_once()
        mock_ack.assert_awaited_with("msg-attempt-3")

    async with get_db_session() as session:
        stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
        res = await session.execute(stmt)
        db_job = res.scalar_one_or_none()
        assert db_job is not None
        assert db_job.status == JobStatus.FAILED.value
        assert db_job.stage == "dead_letter"
        assert db_job.attempt_count == 3
        assert db_job.error_payload is not None
        assert db_job.error_payload["final"] is True



@pytest.mark.asyncio
async def test_worker_crash_before_ack_idempotency():
    """
    Simulates a worker crashing AFTER job extraction completed and database updated,
    but BEFORE acknowledging the Redis stream message.
    When another worker recovers the unacknowledged message from PEL, reprocessing is idempotent.
    """
    manager = JobQueueManager()
    job_id = await manager.create_job(
        job_type=JobType.SEARCH,
        provider="ebay",
        request_payload={"query": "crash test"},
    )

    # Worker 1 runs extraction, updates DB to completed, but crashes before acknowledge_job
    async with get_db_session() as session:
        stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
        res = await session.execute(stmt)
        db_job = res.scalar_one_or_none()
        assert db_job is not None
        db_job.status = JobStatus.COMPLETED.value
        db_job.stage = "completed"
        db_job.result_payload = {"total": 5, "items": []}
        db_job.attempt_count = 1
        await session.commit()

    # Worker 2 now recovers this message from PEL
    mock_search = AsyncMock()
    mock_ack = AsyncMock(return_value=True)

    with (
        patch.object(manager.search_service, "search", mock_search),
        patch("app.workers.queue.acknowledge_job", mock_ack),
    ):
        # Worker 2 processes the unacknowledged message
        recovered_success = await manager.process_job(job_id, message_id="unacked-msg-pel")

        # It must NOT re-execute extraction
        assert recovered_success is False
        mock_search.assert_not_called()

        # It MUST acknowledge the stale message now
        mock_ack.assert_awaited_with("unacked-msg-pel")
