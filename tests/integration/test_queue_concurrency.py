from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.database.models import ExtractionJob
from app.database.session import get_db_session, init_db
from app.schemas.jobs import JobStatus, JobType
from app.schemas.products import NormalizedProduct, ProductAvailability
from app.workers.queue import JobQueueManager


@pytest.fixture(autouse=True)
async def setup_db():
    await init_db()


@pytest.mark.asyncio
async def test_atomic_claim_two_racing_workers():
    """
    Proves that when two workers attempt to claim the same job concurrently,
    atomic conditional update ensures exactly ONE worker succeeds and the other fails.
    """
    manager = JobQueueManager()
    job_id = await manager.create_job(
        job_type=JobType.SEARCH,
        provider="ebay",
        request_payload={"query": "phone case"},
    )

    # Race two concurrent claim calls on the same job_id
    res1, res2 = await asyncio.gather(
        manager.claim_job(job_id),
        manager.claim_job(job_id),
    )

    claimed_1, job_1 = res1
    claimed_2, job_2 = res2

    # Exactly one claim succeeds
    assert (claimed_1 and not claimed_2) or (claimed_2 and not claimed_1)

    winning_job = job_1 if claimed_1 else job_2
    assert winning_job is not None
    assert winning_job.status == JobStatus.RUNNING.value
    assert winning_job.attempt_count == 1


@pytest.mark.asyncio
async def test_duplicate_redis_messages_cannot_cause_duplicate_extraction():
    """
    Proves that duplicate Redis messages for an already-completed or running job
    cannot trigger duplicate supplier extraction.
    """
    manager = JobQueueManager()
    job_id = await manager.create_job(
        job_type=JobType.PRODUCT,
        provider="ebay",
        request_payload={"product_id": "123456789"},
    )

    dummy_product = NormalizedProduct(
        provider="ebay",
        external_id="123456789",
        canonical_url="https://www.ebay.com/itm/123456789",
        title="Test Product",
        current_price=29.99,
        currency="USD",
        availability=ProductAvailability.IN_STOCK,
    )

    mock_extract = AsyncMock(return_value=dummy_product)
    mock_ack = AsyncMock(return_value=True)

    with (
        patch.object(manager.product_service, "retrieve_product", mock_extract),
        patch("app.workers.queue.acknowledge_job", mock_ack),
    ):
        # 1. First message processing
        first_success = await manager.process_job(job_id, message_id="stream-msg-1")
        assert first_success is True
        assert mock_extract.await_count == 1
        mock_ack.assert_awaited_with("stream-msg-1")

        # Verify job is completed in DB
        async with get_db_session() as session:
            stmt = select(ExtractionJob).where(ExtractionJob.id == job_id)
            res = await session.execute(stmt)
            db_job = res.scalar_one_or_none()
            assert db_job is not None
            assert db_job.status == JobStatus.COMPLETED.value

        # 2. Duplicate Redis message arrives for the same job
        mock_ack.reset_mock()
        second_success = await manager.process_job(job_id, message_id="stream-msg-duplicate-2")

        # Second message must not execute extraction again!
        assert second_success is False
        assert mock_extract.await_count == 1  # Unchanged! Still only 1 call
        # But duplicate message must be acknowledged so PEL does not hold it
        mock_ack.assert_awaited_with("stream-msg-duplicate-2")


@pytest.mark.asyncio
async def test_ineligible_jobs_cannot_be_claimed():
    """
    Proves that jobs with status completed, cancelled, or failed-final cannot transition to running.
    """
    manager = JobQueueManager()

    for terminal_status in (JobStatus.COMPLETED.value, JobStatus.CANCELLED.value, JobStatus.FAILED.value):
        async with get_db_session() as session:
            job = ExtractionJob(
                job_type=JobType.SEARCH.value,
                provider="amazon",
                request_payload={"query": "test"},
                status=terminal_status,
                stage=terminal_status,
                attempt_count=1,
            )
            session.add(job)
            await session.commit()
            j_id = str(job.id)

        claimed, claimed_job = await manager.claim_job(j_id)
        assert claimed is False
        assert claimed_job is None

        # Verify status did not change
        async with get_db_session() as session:
            stmt = select(ExtractionJob).where(ExtractionJob.id == j_id)
            res = await session.execute(stmt)
            current = res.scalar_one_or_none()
            assert current is not None
            assert current.status == terminal_status


@pytest.mark.asyncio
async def test_stale_running_job_recovery_vs_active_running_job():
    """
    Verifies that actively running jobs cannot be stolen, but timed-out running jobs
    (past stale threshold) can be safely reclaimed during crash recovery.
    """
    manager = JobQueueManager()
    now = datetime.now(timezone.utc)


    # 1. Fresh actively running job (started 10s ago)
    async with get_db_session() as session:
        active_job = ExtractionJob(
            job_type=JobType.SEARCH.value,
            provider="ebay",
            request_payload={"query": "active"},
            status=JobStatus.RUNNING.value,
            stage="running",
            started_at=now - timedelta(seconds=10),
            attempt_count=1,
        )
        session.add(active_job)
        await session.commit()
        active_id = str(active_job.id)

    claimed_active, _ = await manager.claim_job(active_id)
    assert claimed_active is False  # Cannot steal an actively running job

    # 2. Stale crashed job (started 300s ago > 180s timeout)
    async with get_db_session() as session:
        crashed_job = ExtractionJob(
            job_type=JobType.SEARCH.value,
            provider="ebay",
            request_payload={"query": "stale"},
            status=JobStatus.RUNNING.value,
            stage="running",
            started_at=now - timedelta(seconds=300),
            attempt_count=1,
        )
        session.add(crashed_job)
        await session.commit()
        crashed_id = str(crashed_job.id)

    claimed_stale, reclaimed_instance = await manager.claim_job(crashed_id)
    assert claimed_stale is True
    assert reclaimed_instance is not None
    assert reclaimed_instance.attempt_count == 2
