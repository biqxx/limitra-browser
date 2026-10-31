from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager as AsyncContextManager

import structlog
from playwright.async_api import Browser, Page, Playwright, async_playwright

from app.browser.context import isolated_browser_session
from app.config import get_settings
from app.observability.metrics import BROWSER_POOL_RECYCLES_TOTAL

logger = structlog.get_logger(__name__)


class BrowserManager:
    """
    Manages Playwright lifecycle, Chromium browser process,
    global and per-supplier concurrency semaphores, and process recycling.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._page_count: int = 0
        self._lock = asyncio.Lock()

        # Global concurrency limiter
        self._global_semaphore = asyncio.Semaphore(self.settings.BROWSER_MAX_GLOBAL_CONCURRENCY)

        # Per-supplier concurrency limiters
        self._provider_semaphores: dict[str, asyncio.Semaphore] = {}

    def get_provider_semaphore(self, provider: str) -> asyncio.Semaphore:
        if provider not in self._provider_semaphores:
            self._provider_semaphores[provider] = asyncio.Semaphore(
                self.settings.BROWSER_MAX_PER_SUPPLIER_CONCURRENCY
            )
        return self._provider_semaphores[provider]

    async def initialize(self) -> None:
        async with self._lock:
            await self._initialize_under_lock()

    async def _initialize_under_lock(self) -> None:
        if self._browser is None or not self._browser.is_connected():
            logger.info("initializing_browser_pool")
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.settings.BROWSER_HEADLESS,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                ],
            )
            self._page_count = 0
            logger.info("browser_pool_initialized")

    async def recycle_if_needed(self) -> None:
        """
        Recycles the browser process when maximum page threshold is reached
        to prevent memory leaks.
        """
        if self._page_count >= self.settings.BROWSER_RECYCLE_PAGE_LIMIT:
            async with self._lock:
                # Re-check under lock
                if self._page_count >= self.settings.BROWSER_RECYCLE_PAGE_LIMIT:
                    logger.info(
                        "recycling_browser_process",
                        page_count=self._page_count,
                        limit=self.settings.BROWSER_RECYCLE_PAGE_LIMIT,
                    )
                    BROWSER_POOL_RECYCLES_TOTAL.labels(reason="page_limit").inc()
                    await self._close_browser()
                    await self._initialize_under_lock()

    async def _close_browser(self) -> None:
        if self._browser:
            try:
                await self._browser.close()
            except Exception as e:
                logger.warning("error_closing_browser", error=str(e))
            self._browser = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception as e:
                logger.warning("error_stopping_playwright", error=str(e))
            self._playwright = None

    async def shutdown(self) -> None:
        async with self._lock:
            logger.info("shutting_down_browser_pool")
            await self._close_browser()

    async def get_session(
        self,
        provider: str,
        locale: str = "en-US",
        country: str = "US",
        timezone_id: str = "America/New_York",
        job_id: str | None = None,
    ) -> AsyncContextManager[Page]:

        await self.recycle_if_needed()
        if self._browser is None or not self._browser.is_connected():
            await self.initialize()

        assert self._browser is not None

        self._page_count += 1
        provider_sem = self.get_provider_semaphore(provider)

        return isolated_browser_session(
            browser=self._browser,
            provider=provider,
            global_sem=self._global_semaphore,
            provider_sem=provider_sem,
            locale=locale,
            country=country,
            timezone_id=timezone_id,
            job_id=job_id,
        )

    async def healthcheck(self) -> bool:
        try:
            if self._browser is not None and self._browser.is_connected():
                return True
            return False
        except Exception:
            return False


_GLOBAL_BROWSER_MANAGER: BrowserManager | None = None


def get_browser_manager() -> BrowserManager:
    global _GLOBAL_BROWSER_MANAGER
    if _GLOBAL_BROWSER_MANAGER is None:
        _GLOBAL_BROWSER_MANAGER = BrowserManager()
    return _GLOBAL_BROWSER_MANAGER
