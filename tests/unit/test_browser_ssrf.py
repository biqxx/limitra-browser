from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.browser.policies import (
    create_browser_route_handler,
    is_subrequest_safe,
)


def test_blocked_schemes():
    safe, reason = is_subrequest_safe("file:///etc/passwd")
    assert not safe
    assert "forbidden_scheme" in reason

    safe, reason = is_subrequest_safe("gopher://127.0.0.1:70")
    assert not safe
    assert "forbidden_scheme" in reason

    safe, reason = is_subrequest_safe("ftp://ftp.example.com/file.txt")
    assert not safe
    assert "forbidden_scheme" in reason


def test_blocked_embedded_credentials():
    safe, reason = is_subrequest_safe("https://admin:secret123@amazon.com/product")
    assert not safe
    assert "embedded_credentials_forbidden" in reason


def test_blocked_cloud_metadata_and_internal_ips():
    # AWS/Cloud metadata IP
    safe, reason = is_subrequest_safe("http://169.254.169.254/latest/meta-data/")
    assert not safe
    assert "blocked_ip" in reason

    # IPv4 Loopback
    safe, reason = is_subrequest_safe("http://127.0.0.1:8000/internal")
    assert not safe
    assert "blocked_ip" in reason

    # RFC1918 Private ranges
    safe, reason = is_subrequest_safe("http://10.1.2.3/secret")
    assert not safe
    assert "blocked_ip" in reason

    safe, reason = is_subrequest_safe("http://192.168.1.1/admin")
    assert not safe
    assert "blocked_ip" in reason


def test_blocked_trackers_and_heavy_media():
    safe, reason = is_subrequest_safe("https://www.google-analytics.com/analytics.js")
    assert not safe
    assert "blocked_ad_tracker" in reason

    safe, reason = is_subrequest_safe("https://m.media-amazon.com/stream.mp4", resource_type="media")
    assert not safe
    assert "blocked_streaming_media" in reason


def test_allowed_supplier_cdns():
    safe, reason = is_subrequest_safe(
        "https://m.media-amazon.com/images/I/71abc.jpg",
        resource_type="image",
        provider="amazon",
    )
    assert safe
    assert reason == "allowed"

    safe, reason = is_subrequest_safe(
        "https://images-na.ssl-images-amazon.com/images/G/01/nav2/images/gui/amazon-logo.png",
        resource_type="image",
        provider="amazon",
    )
    assert safe
    assert reason == "allowed"

    safe, reason = is_subrequest_safe(
        "https://i.ebayimg.com/images/g/abc/s-l1600.jpg",
        resource_type="image",
        provider="ebay",
    )
    assert safe
    assert reason == "allowed"


def test_inline_data_and_blob_assets():
    safe, reason = is_subrequest_safe("data:image/png;base64,iVBORw0KGgoAAA", resource_type="image")
    assert safe
    assert reason == "allowed_inline_asset"

    safe, reason = is_subrequest_safe("data:text/html,<script>alert(1)</script>", resource_type="script")
    assert not safe
    assert "forbidden_data_blob_scheme" in reason


@pytest.mark.asyncio
async def test_route_handler_aborts_dangerous_request():
    route = AsyncMock()
    request = MagicMock()
    request.url = "http://169.254.169.254/latest/meta-data/"
    request.resource_type = "fetch"

    handler = create_browser_route_handler(provider="amazon")
    await handler(route, request)

    route.abort.assert_awaited_once_with("blockedbyclient")
    route.continue_.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_handler_continues_safe_request():
    route = AsyncMock()
    request = MagicMock()
    request.url = "https://m.media-amazon.com/images/I/71abc.jpg"
    request.resource_type = "image"

    handler = create_browser_route_handler(provider="amazon")
    await handler(route, request)

    route.continue_.assert_awaited_once()
    route.abort.assert_not_awaited()
