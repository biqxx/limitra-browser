from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.browser.manager import BrowserManager


def browser_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "BROWSER_HEADLESS": True,
        "BROWSER_MAX_GLOBAL_CONCURRENCY": 1,
        "BROWSER_MAX_PER_SUPPLIER_CONCURRENCY": 1,
        "BROWSER_RECYCLE_PAGE_LIMIT": 100,
        "BROWSER_PROXY_SERVER": "",
        "BROWSER_PROXY_USERNAME": "",
        "BROWSER_PROXY_PASSWORD": "",
        "BROWSER_PROXY_BYPASS": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_browser_launch_uses_authenticated_proxy_without_logging_credentials() -> None:
    settings = browser_settings(
        BROWSER_PROXY_SERVER="http://proxy.example:8080",
        BROWSER_PROXY_USERNAME="proxy-user",
        BROWSER_PROXY_PASSWORD="proxy-password",
        BROWSER_PROXY_BYPASS="localhost,redis",
    )
    browser = MagicMock()
    browser.is_connected.return_value = True
    launch = AsyncMock(return_value=browser)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    starter = MagicMock()
    starter.start = AsyncMock(return_value=playwright)

    with (
        patch("app.browser.manager.get_settings", return_value=settings),
        patch("app.browser.manager.async_playwright", return_value=starter),
        patch("app.browser.manager.logger") as logger,
    ):
        manager = BrowserManager()
        await manager.initialize()

    launch.assert_awaited_once()
    launch_call = launch.await_args
    assert launch_call is not None
    assert launch_call.kwargs["proxy"] == {
        "server": "http://proxy.example:8080",
        "username": "proxy-user",
        "password": "proxy-password",
        "bypass": "localhost,redis",
    }
    logger.info.assert_any_call("browser_pool_initialized", proxy_enabled=True)
    assert "proxy-password" not in str(logger.mock_calls)


@pytest.mark.asyncio
async def test_browser_launch_omits_proxy_when_not_configured() -> None:
    settings = browser_settings()
    browser = MagicMock()
    browser.is_connected.return_value = True
    launch = AsyncMock(return_value=browser)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    starter = MagicMock()
    starter.start = AsyncMock(return_value=playwright)

    with (
        patch("app.browser.manager.get_settings", return_value=settings),
        patch("app.browser.manager.async_playwright", return_value=starter),
    ):
        manager = BrowserManager()
        await manager.initialize()

    launch.assert_awaited_once()
    launch_call = launch.await_args
    assert launch_call is not None
    assert "proxy" not in launch_call.kwargs
