from __future__ import annotations

import asyncio
import signal
import time
import uuid
from types import FrameType

import structlog

from app.browser.manager import get_browser_manager
from app.config import get_settings
from app.database.session import init_db
from app.observability.logging import setup_logging
from app.workers.queue import get_job_manager
from app.workers.redis_queue import (
    claim_next_job,
    clear_worker_heartbeat,
    dequeue_job_id,
    ensure_consumer_group,
    record_worker_heartbeat,
    recover_stale_jobs,
)

logger = structlog.get_logger(__name__)


class StandaloneWorker:
    def __init__(self, worker_id: str | None = None) -> None:
        self.settings = get_settings()
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.running = True
        self.job_manager = get_job_manager()
        self.browser_manager = get_browser_manager()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._last_pel_check = 0.0

    def stop(self) -> None:
        logger.info("worker_stopping_signal_received", worker_id=self.worker_id)
        self.running = False

    async def _heartbeat_loop(self) -> None:
        while self.running:
            try:
                await record_worker_heartbeat(self.worker_id, ttl_seconds=30)
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("heartbeat_error", error=str(e))
                await asyncio.sleep(5)

    async def run(self) -> None:
        logger.info("starting_standalone_queue_worker", worker_id=self.worker_id)
        setup_logging()
        await init_db()
        await self.browser_manager.initialize()
        await ensure_consumer_group()

        # Start heartbeat loop in background
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        logger.info("queue_worker_ready_listening_for_jobs", worker_id=self.worker_id)

        while self.running:
            try:
                # 1. Periodic PEL recovery for crashed or timed-out workers
                now = time.monotonic()
                if now - self._last_pel_check > 60:
                    self._last_pel_check = now
                    stale = await recover_stale_jobs(self.worker_id, min_idle_time_ms=180000)
                    for stale_msg_id, stale_jid in stale:
                        logger.info("processing_recovered_pel_job", job_id=stale_jid, message_id=stale_msg_id)
                        await self.job_manager.process_job(stale_jid, message_id=stale_msg_id)

                # 2. Claim next job from durable stream consumer group
                msg_id, job_id = await claim_next_job(self.worker_id, block_ms=2000)

                # 3. Fallback to legacy queue list only if explicitly enabled
                if not job_id and self.settings.LEGACY_QUEUE_FALLBACK_ENABLED:
                    job_id = await dequeue_job_id(timeout=1)

                if not job_id:
                    continue

                logger.info("worker_claimed_job", job_id=job_id, worker_id=self.worker_id, message_id=msg_id)
                await self.job_manager.process_job(job_id, message_id=msg_id)
                await self.browser_manager.recycle_if_needed()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("worker_loop_error", error=str(e), exc_info=True)
                await asyncio.sleep(1)

        # Teardown
        logger.info("shutting_down_queue_worker", worker_id=self.worker_id)
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        await clear_worker_heartbeat(self.worker_id)
        await self.browser_manager.shutdown()
        logger.info("queue_worker_stopped", worker_id=self.worker_id)


def main() -> None:
    worker = StandaloneWorker()

    def handle_signal(sig: int, frame: FrameType | None) -> None:
        worker.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        asyncio.run(worker.run())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
