from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.workers.redis_queue import (
    acknowledge_job,
    check_active_workers,
    claim_next_job,
    clear_worker_heartbeat,
    enqueue_job,
    ensure_consumer_group,
    record_worker_heartbeat,
    recover_stale_jobs,
)


@pytest.mark.asyncio
async def test_ensure_consumer_group_creates_stream():
    mock_redis = AsyncMock()
    with patch("app.workers.redis_queue.get_redis_client", return_value=mock_redis):
        await ensure_consumer_group("test:stream", "test_group")
        mock_redis.xgroup_create.assert_awaited_once_with(
            "test:stream", "test_group", id="0", mkstream=True
        )


@pytest.mark.asyncio
async def test_ensure_consumer_group_ignores_busygroup():
    mock_redis = AsyncMock()
    mock_redis.xgroup_create.side_effect = Exception("BUSYGROUP Consumer Group name already exists")
    with patch("app.workers.redis_queue.get_redis_client", return_value=mock_redis):
        # Should not raise exception
        await ensure_consumer_group("test:stream", "test_group")
        mock_redis.xgroup_create.assert_awaited_once()


@pytest.mark.asyncio
async def test_enqueue_job_xadd():
    mock_redis = AsyncMock()
    mock_redis.xadd.return_value = b"1720000000000-0"
    with patch("app.workers.redis_queue.get_redis_client", return_value=mock_redis):
        msg_id = await enqueue_job("job-abc-123", payload={"action": "test"})
        assert msg_id == "1720000000000-0"
        mock_redis.xadd.assert_awaited_once()


@pytest.mark.asyncio
async def test_claim_next_job_xreadgroup():
    mock_redis = AsyncMock()
    mock_redis.xreadgroup.return_value = [
        ("supplier:jobs:stream", [(b"1720000000000-1", {b"job_id": b"job-xyz-789"})])
    ]
    with patch("app.workers.redis_queue.get_redis_client", return_value=mock_redis):
        msg_id, job_id = await claim_next_job("worker-1", block_ms=100)
        assert msg_id == "1720000000000-1"
        assert job_id == "job-xyz-789"


@pytest.mark.asyncio
async def test_acknowledge_job_xack():
    mock_redis = AsyncMock()
    mock_redis.xack.return_value = 1
    with patch("app.workers.redis_queue.get_redis_client", return_value=mock_redis):
        success = await acknowledge_job("1720000000000-1")
        assert success is True
        mock_redis.xack.assert_awaited_once()


@pytest.mark.asyncio
async def test_pel_recovery_stale_jobs():
    mock_redis = AsyncMock()
    # Mock xpending_range returning an entry with idle_time = 200000ms (> 180000ms)
    mock_redis.xpending_range.return_value = [
        {"message_id": b"1720000000000-2", "idle_time": 200000, "consumer": "dead-worker"}
    ]
    # Mock xclaim claiming that message
    mock_redis.xclaim.return_value = [
        (b"1720000000000-2", {b"job_id": b"recovered-job-999"})
    ]

    with patch("app.workers.redis_queue.get_redis_client", return_value=mock_redis):
        claimed = await recover_stale_jobs("worker-active", min_idle_time_ms=180000)
        assert len(claimed) == 1
        assert claimed[0] == ("1720000000000-2", "recovered-job-999")
        mock_redis.xclaim.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_heartbeat_and_active_check():
    mock_redis = AsyncMock()
    mock_redis.keys.return_value = [b"worker:heartbeat:worker-abc"]

    with patch("app.workers.redis_queue.get_redis_client", return_value=mock_redis):
        await record_worker_heartbeat("worker-abc", ttl_seconds=30)
        mock_redis.set.assert_awaited_once()

        has_workers = await check_active_workers()
        assert has_workers is True

        await clear_worker_heartbeat("worker-abc")
        mock_redis.delete.assert_awaited_once_with("worker:heartbeat:worker-abc")
