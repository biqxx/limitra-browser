from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, cast

import structlog

from app.cache.service import get_redis_client

logger = structlog.get_logger(__name__)

JOB_QUEUE_KEY = "supplier_browser:job_queue"
STREAM_KEY = "supplier:jobs:stream"
CONSUMER_GROUP = "extraction_workers"
PEL_IDLE_TIME_MS = 180000  # 3 minutes


DEAD_LETTER_STREAM_KEY = "supplier:jobs:dead_letter"


async def ensure_consumer_group(
    stream_key: str = STREAM_KEY,
    group_name: str = CONSUMER_GROUP,
) -> None:
    """Creates consumer group on the Redis stream if it does not already exist."""
    redis = get_redis_client()
    if redis is None:
        return
    try:
        await redis.xgroup_create(stream_key, group_name, id="0", mkstream=True)
        logger.info("redis_consumer_group_created", stream=stream_key, group=group_name)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            logger.debug("redis_consumer_group_init_note", note=str(e))


async def enqueue_job(
    job_id: str,
    payload: dict[str, Any] | None = None,
    stream_key: str = STREAM_KEY,
) -> str | None:
    """
    Appends a background job to the durable Redis Stream via XADD.
    Redis Streams is the only queue for newly created jobs.
    Returns the message ID or None if Redis is unavailable.
    """
    redis = get_redis_client()
    if redis is None:
        return None

    try:
        data: dict[Any, Any] = {"job_id": job_id}
        if payload:
            import json

            data["payload"] = json.dumps(payload)

        msg_id = await redis.xadd(stream_key, data)
        clean_id = msg_id.decode("utf-8") if isinstance(msg_id, bytes) else str(msg_id)
        logger.info("job_enqueued_to_redis_stream", job_id=job_id, stream=stream_key, message_id=clean_id)
        return clean_id
    except Exception as e:
        logger.warning("redis_stream_enqueue_failed", job_id=job_id, error=str(e))
        return None


async def publish_dead_letter(
    job_id: str,
    error_payload: dict[str, Any],
    stream_key: str = DEAD_LETTER_STREAM_KEY,
) -> str | None:
    """
    Publishes permanently failed or exhausted jobs to the dead-letter Redis Stream.
    """
    redis = get_redis_client()
    if redis is None:
        return None

    try:
        import json

        data: dict[Any, Any] = {
            "job_id": job_id,
            "error": json.dumps(error_payload),
            "failed_at": datetime.now(timezone.utc).isoformat(),
        }
        msg_id = await redis.xadd(stream_key, data)
        clean_id = msg_id.decode("utf-8") if isinstance(msg_id, bytes) else str(msg_id)
        logger.info(
            "job_published_to_dead_letter_stream",
            job_id=job_id,
            stream=stream_key,
            message_id=clean_id,
        )
        return clean_id
    except Exception as e:
        logger.warning("dead_letter_publish_failed", job_id=job_id, error=str(e))
        return None


async def enqueue_job_id(job_id: str) -> bool:
    """
    Compatibility wrapper for enqueueing a job. Returns True on success, False on failure.
    """
    res = await enqueue_job(job_id)
    return res is not None


async def claim_next_job(
    worker_id: str,
    block_ms: int = 2000,
    stream_key: str = STREAM_KEY,
    group_name: str = CONSUMER_GROUP,
) -> tuple[str | None, str | None]:
    """
    Atomically reads and claims the next unread job from the stream consumer group.
    Returns (message_id, job_id) or (None, None).
    """
    redis = get_redis_client()
    if redis is None:
        await asyncio.sleep(block_ms / 1000.0)
        return None, None

    try:
        entries = await redis.xreadgroup(
            groupname=group_name,
            consumername=worker_id,
            streams={stream_key: ">"},
            count=1,
            block=block_ms,
        )
        if entries and isinstance(entries, list):
            for stream_entry in cast(list[Any], entries):
                if isinstance(stream_entry, (list, tuple)) and len(stream_entry) == 2:
                    messages = stream_entry[1]
                    if isinstance(messages, list):
                        for msg in messages:
                            if isinstance(msg, (list, tuple)) and len(msg) == 2:
                                msg_id, fields = msg[0], msg[1]
                                clean_msg_id = (
                                    msg_id.decode("utf-8")
                                    if isinstance(msg_id, bytes)
                                    else str(msg_id)
                                )
                                if isinstance(fields, dict):
                                    raw_jid = fields.get(b"job_id") or fields.get("job_id")
                                    if raw_jid:
                                        job_id = (
                                            raw_jid.decode("utf-8")
                                            if isinstance(raw_jid, bytes)
                                            else str(raw_jid)
                                        )
                                        return clean_msg_id, job_id
        return None, None
    except Exception as e:
        if "NOGROUP" in str(e):
            await ensure_consumer_group(stream_key, group_name)
        else:
            logger.warning("xreadgroup_failed", error=str(e))
        await asyncio.sleep(block_ms / 1000.0)
        return None, None


async def acknowledge_job(
    message_id: str,
    stream_key: str = STREAM_KEY,
    group_name: str = CONSUMER_GROUP,
) -> bool:
    """Acknowledges successful processing of a stream message via XACK."""
    redis = get_redis_client()
    if redis is None:
        return False
    try:
        res = await redis.xack(stream_key, group_name, message_id)
        return bool(res > 0)
    except Exception as e:
        logger.warning("redis_xack_failed", message_id=message_id, error=str(e))
        return False


async def recover_stale_jobs(
    worker_id: str,
    min_idle_time_ms: int = PEL_IDLE_TIME_MS,
    count: int = 10,
    stream_key: str = STREAM_KEY,
    group_name: str = CONSUMER_GROUP,
) -> list[tuple[str, str]]:
    """
    Recovers unacknowledged jobs from the Pending Entries List (PEL) whose
    original worker failed or timed out past min_idle_time_ms.
    Returns list of claimed (message_id, job_id).
    """
    redis = get_redis_client()
    if redis is None:
        return []

    claimed_jobs: list[tuple[str, str]] = []
    try:
        pending = await redis.xpending_range(
            stream_key,
            group_name,
            min="-",
            max="+",
            count=count,
        )
        for p in cast(list[Any], pending):
            msg_id = (
                p.get("message_id")
                if isinstance(p, dict)
                else (p[0] if isinstance(p, (list, tuple)) and len(p) > 0 else None)
            )
            idle_raw = (
                p.get("idle_time")
                if isinstance(p, dict)
                else (p[2] if isinstance(p, (list, tuple)) and len(p) > 2 else 0)
            )
            try:
                idle_ms = int(idle_raw) if idle_raw is not None else 0
            except (ValueError, TypeError):
                idle_ms = 0

            if msg_id and idle_ms >= min_idle_time_ms:
                claimed = await redis.xclaim(
                    stream_key,
                    group_name,
                    worker_id,
                    min_idle_time_ms,
                    [msg_id],
                )
                if claimed and isinstance(claimed, list):
                    for c_entry in cast(list[Any], claimed):
                        if isinstance(c_entry, (list, tuple)) and len(c_entry) == 2:
                            c_msg_id, c_fields = c_entry[0], c_entry[1]
                            clean_id = (
                                c_msg_id.decode("utf-8")
                                if isinstance(c_msg_id, bytes)
                                else str(c_msg_id)
                            )
                            if isinstance(c_fields, dict):
                                raw_jid = c_fields.get(b"job_id") or c_fields.get("job_id")
                                if raw_jid:
                                    job_id = (
                                        raw_jid.decode("utf-8")
                                        if isinstance(raw_jid, bytes)
                                        else str(raw_jid)
                                    )
                                    claimed_jobs.append((clean_id, job_id))
                                    logger.info(
                                        "recovered_stale_job_from_pel",
                                        message_id=clean_id,
                                        job_id=job_id,
                                    )
    except Exception as e:
        logger.debug("pel_recovery_error", error=str(e))

    return claimed_jobs


async def record_worker_heartbeat(worker_id: str, ttl_seconds: int = 30) -> None:
    """Updates worker heartbeat key with current timestamp and expiration."""
    redis = get_redis_client()
    if redis:
        try:
            key = f"worker:heartbeat:{worker_id}"
            now_iso = datetime.now(timezone.utc).isoformat()
            await redis.set(key, now_iso, ex=ttl_seconds)
        except Exception as e:
            logger.debug("worker_heartbeat_failed", error=str(e))


async def clear_worker_heartbeat(worker_id: str) -> None:
    redis = get_redis_client()
    if redis:
        try:
            await redis.delete(f"worker:heartbeat:{worker_id}")
        except Exception:
            pass


async def check_active_workers() -> bool:
    """Checks whether any active background workers have reported heartbeats."""
    redis = get_redis_client()
    if not redis:
        return False
    try:
        keys = await redis.keys("worker:heartbeat:*")
        return len(keys) > 0
    except Exception:
        return False


async def dequeue_job_id(timeout: int = 2) -> str | None:
    """
    Legacy queue pop for backward compatibility with list-based tests and consumers.
    """
    redis = get_redis_client()
    if redis is None:
        await asyncio.sleep(timeout)
        return None

    try:
        res = await redis.brpop(JOB_QUEUE_KEY, timeout=timeout)
        if res:
            _, raw_job_id = res
            if isinstance(raw_job_id, bytes):
                return raw_job_id.decode("utf-8")
            return str(raw_job_id)
        return None
    except Exception as e:
        logger.warning("redis_dequeue_failed", error=str(e))
        await asyncio.sleep(timeout)
        return None
