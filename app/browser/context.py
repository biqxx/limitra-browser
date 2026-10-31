from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from playwright.async_api import Browser, BrowserContext, Page

from app.browser.diagnostics import DiagnosticManager
from app.browser.policies import create_browser_route_handler, get_browser_context_options
from app.config import get_settings
from app.observability.metrics import BROWSER_POOL_ACTIVE_CONTEXTS

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def isolated_browser_session(
    browser: Browser,
    provider: str,
    global_sem: asyncio.Semaphore,
    provider_sem: asyncio.Semaphore,
    locale: str = "en-US",
    country: str = "US",
    timezone_id: str = "America/New_York",
    job_id: str | None = None,
) -> AsyncGenerator[Page, None]:
    """
    Creates an isolated browser context and page per operation:
    - Enforces global and per-supplier concurrency semaphores
    - Sets network route blocking for ads and trackers
    - Always closes pages and contexts in finally blocks
    - Captures diagnostic artifacts on failure if diagnostic mode enabled
    """
    settings = get_settings()
    diag_manager = DiagnosticManager()
    context: BrowserContext | None = None
    page: Page | None = None

    # 1. Acquire concurrency locks with guaranteed cleanup
    await global_sem.acquire()
    try:
        await provider_sem.acquire()
        try:
            BROWSER_POOL_ACTIVE_CONTEXTS.labels(provider=provider).inc()
            try:
                options = get_browser_context_options(
                    locale=locale, country=country, timezone_id=timezone_id
                )
                context = await browser.new_context(**options)

                # Tracing if diagnostic mode enabled
                if settings.DIAGNOSTIC_MODE_ENABLED:
                    await context.tracing.start(screenshots=True, snapshots=True, sources=True)

                page = await context.new_page()

                # Route blocking & SSRF defense
                await page.route("**/*", create_browser_route_handler(provider=provider))

                yield page

            except Exception:
                # Capture diagnostics on error
                if settings.DIAGNOSTIC_MODE_ENABLED and page and context:
                    await diag_manager.capture_artifacts(
                        provider=provider,
                        operation="session_error",
                        context=context,
                        page=page,
                        job_id=job_id,
                    )
                raise

            finally:
                # Cleanup page & context unconditionally
                if page:
                    try:
                        await page.close()
                    except Exception as e:
                        logger.debug("page_close_error", error=str(e))

                if context:
                    try:
                        await context.close()
                    except Exception as e:
                        logger.debug("context_close_error", error=str(e))
        finally:
            provider_sem.release()
            BROWSER_POOL_ACTIVE_CONTEXTS.labels(provider=provider).dec()
    finally:
        global_sem.release()
