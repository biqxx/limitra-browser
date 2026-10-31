from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog

logger = structlog.get_logger(__name__)

# In-memory lock table for fallback
_IN_MEMORY_LOCKS: dict[str, asyncio.Lock] = {}


@asynccontextmanager
async def distributed_lock(
    redis_client: object | None,
    lock_key: str,
    ttl_seconds: int = 30,
    wait_timeout: float = 10.0,
) -> AsyncGenerator[bool, None]:
    """
    Acquires a distributed lock using Redis (with uuid ownership and Lua script release),
    or falls back to an asyncio.Lock if Redis client is unavailable or disconnected.
    """
    owner_id = str(uuid.uuid4())
    acquired = False
    used_redis = False

    if redis_client is not None:
        full_key = f"lock:{lock_key}"
        try:
            start_time = asyncio.get_event_loop().time()
            while (asyncio.get_event_loop().time() - start_time) < wait_timeout:
                # SET key owner_id NX EX ttl
                ok = await redis_client.set(full_key, owner_id, nx=True, ex=ttl_seconds)  # type: ignore
                if ok:
                    acquired = True
                    used_redis = True
                    break
                await asyncio.sleep(0.1)

            if not acquired:
                logger.warning(
                    "distributed_lock_timeout", lock_key=lock_key, wait_timeout=wait_timeout
                )

        except (ConnectionError, OSError, Exception) as e:
            logger.warning("redis_lock_failed_fallback_memory", lock_key=lock_key, error=str(e))
            used_redis = False

    if used_redis:
        try:
            yield acquired
        except GeneratorExit:
            raise
        except Exception:
            raise
        finally:
            if acquired:
                lua_release = """
                if redis.call("get", KEYS[1]) == ARGV[1] then
                    return redis.call("del", KEYS[1])
                else
                    return 0
                end
                """
                try:
                    await redis_client.eval(lua_release, 1, full_key, owner_id)  # type: ignore
                except Exception as e:
                    logger.error("distributed_lock_release_failed", error=str(e), lock_key=lock_key)
    else:
        # In-memory lock fallback
        if lock_key not in _IN_MEMORY_LOCKS:
            _IN_MEMORY_LOCKS[lock_key] = asyncio.Lock()
        mem_lock = _IN_MEMORY_LOCKS[lock_key]

        try:
            try:
                await asyncio.wait_for(mem_lock.acquire(), timeout=wait_timeout)
                acquired = True
            except TimeoutError:
                acquired = False

            yield acquired
        except GeneratorExit:
            raise
        except Exception:
            raise
        finally:
            if acquired and mem_lock.locked():
                mem_lock.release()
