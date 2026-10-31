from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.api.errors import (
    ProductNotFoundException,
    SupplierRateLimitedException,
    UnauthorizedException,
)
from app.suppliers.ebay.api_client import EbayApiClient


@pytest.fixture
def configured_ebay_client() -> EbayApiClient:
    with patch("app.suppliers.ebay.api_client.get_settings") as mock_settings:
        mock_settings.return_value.EBAY_API_ENABLED = True
        mock_settings.return_value.EBAY_ENVIRONMENT = "PRODUCTION"
        mock_settings.return_value.EBAY_APP_ID = "test_client_id_123"
        mock_settings.return_value.EBAY_CERT_ID = "test_client_secret_456"
        return EbayApiClient()


@pytest.mark.asyncio
async def test_ebay_token_basic_auth_and_caching(configured_ebay_client: EbayApiClient) -> None:
    client = configured_ebay_client

    mock_token_resp = MagicMock(spec=httpx.Response)
    mock_token_resp.status_code = 200
    mock_token_resp.json.return_value = {
        "access_token": "v^1.1#token_sample_abc123",
        "expires_in": 7200,
        "token_type": "Application Access Token",
    }

    mock_http_client = AsyncMock(spec=httpx.AsyncClient)
    mock_http_client.post.return_value = mock_token_resp

    token1 = await client._get_access_token(mock_http_client)
    assert token1 == "v^1.1#token_sample_abc123"
    assert mock_http_client.post.call_count == 1

    # Check basic auth header was sent
    args, kwargs = mock_http_client.post.call_args
    auth_header = kwargs.get("headers", {}).get("Authorization", "")
    assert auth_header.startswith("Basic ")

    # Subsequent call within validity window should use cached token without post
    token2 = await client._get_access_token(mock_http_client)
    assert token2 == "v^1.1#token_sample_abc123"
    assert mock_http_client.post.call_count == 1


@pytest.mark.asyncio
async def test_ebay_concurrent_token_refresh(configured_ebay_client: EbayApiClient) -> None:
    client = configured_ebay_client

    mock_token_resp = MagicMock(spec=httpx.Response)
    mock_token_resp.status_code = 200
    mock_token_resp.json.return_value = {
        "access_token": "concurrent_token_789",
        "expires_in": 7200,
        "token_type": "Application Access Token",
    }

    mock_http_client = AsyncMock(spec=httpx.AsyncClient)

    async def delayed_post(*args, **kwargs):
        await asyncio.sleep(0.05)
        return mock_token_resp

    mock_http_client.post.side_effect = delayed_post

    # Fire 5 concurrent requests
    tokens = await asyncio.gather(*[client._get_access_token(mock_http_client) for _ in range(5)])

    assert all(t == "concurrent_token_789" for t in tokens)
    # asyncio.Lock ensures HTTP post was only called once
    assert mock_http_client.post.call_count == 1


@pytest.mark.asyncio
async def test_ebay_error_sanitization_and_redaction(configured_ebay_client: EbayApiClient) -> None:
    client = configured_ebay_client
    client._token = "valid_cached_token"
    client._token_expires_at = time.time() + 3600

    # 1. Test 401 Unauthorized
    client._token = "valid_cached_token"
    client._token_expires_at = time.time() + 3600
    resp_401 = MagicMock(spec=httpx.Response)
    resp_401.status_code = 401
    resp_401.is_success = False
    resp_401.json.return_value = {
        "errors": [{"errorId": 1001, "message": "Invalid access token"}]
    }

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = resp_401
        with pytest.raises(UnauthorizedException) as exc_info:
            await client.search("test")

        err_msg = str(exc_info.value)
        assert "Invalid access token" in err_msg
        assert "test_client_secret_456" not in err_msg
        assert "valid_cached_token" not in err_msg

    # 2. Test 404 Not Found
    client._token = "valid_cached_token"
    client._token_expires_at = time.time() + 3600
    resp_404 = MagicMock(spec=httpx.Response)
    resp_404.status_code = 404
    resp_404.is_success = False
    resp_404.json.return_value = {
        "errors": [{"errorId": 11001, "message": "Item not found"}]
    }

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = resp_404
        with pytest.raises(ProductNotFoundException):
            await client.get_item("nonexistent_id")

    # 3. Test 429 Rate Limit
    client._token = "valid_cached_token"
    client._token_expires_at = time.time() + 3600
    resp_429 = MagicMock(spec=httpx.Response)
    resp_429.status_code = 429
    resp_429.is_success = False
    resp_429.headers = {"Retry-After": "45"}
    resp_429.json.return_value = {
        "errors": [{"errorId": 10001, "message": "Call limit exceeded"}]
    }

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = resp_429
        with pytest.raises(SupplierRateLimitedException) as rate_exc_info:
            await client.search("laptop")

        assert rate_exc_info.value.details.get("retry_after_seconds") == 45

