from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.database.session import init_db
from app.main import app

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def pytest_configure(config):
    os.environ["ENVIRONMENT"] = "testing"
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
    os.environ["ENFORCE_API_KEYS"] = "true"
    os.environ["BOOTSTRAP_ADMIN_API_KEY"] = "test_admin_api_key_12345"
    os.environ["EBAY_API_ENABLED"] = "false"
    os.environ["AMAZON_API_ENABLED"] = "false"
    os.environ["DIAGNOSTIC_MODE_ENABLED"] = "false"
    get_settings.cache_clear()
    from app.database.session import reset_db_engine

    reset_db_engine()


@pytest.fixture(scope="session", autouse=True)
def setup_test_env():
    os.environ["ENVIRONMENT"] = "testing"
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
    os.environ["ENFORCE_API_KEYS"] = "true"
    os.environ["BOOTSTRAP_ADMIN_API_KEY"] = "test_admin_api_key_12345"
    os.environ["EBAY_API_ENABLED"] = "false"
    os.environ["AMAZON_API_ENABLED"] = "false"
    os.environ["DIAGNOSTIC_MODE_ENABLED"] = "false"
    get_settings.cache_clear()
    from app.database.session import reset_db_engine

    reset_db_engine()


@pytest.fixture
async def async_client() -> AsyncGenerator[AsyncClient, None]:
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": "test_admin_api_key_12345"}


@pytest.fixture
def internal_auth_headers() -> dict[str, str]:
    settings = get_settings()
    return {"X-Internal-Service-Secret": settings.INTERNAL_SERVICE_SECRET}


@pytest.fixture
def ebay_search_html() -> str:
    return (FIXTURES_DIR / "ebay_search.html").read_text(encoding="utf-8")


@pytest.fixture
def ebay_product_html() -> str:
    return (FIXTURES_DIR / "ebay_product.html").read_text(encoding="utf-8")


@pytest.fixture
def amazon_search_html() -> str:
    return (FIXTURES_DIR / "amazon_search.html").read_text(encoding="utf-8")


@pytest.fixture
def amazon_product_html() -> str:
    return (FIXTURES_DIR / "amazon_product.html").read_text(encoding="utf-8")
