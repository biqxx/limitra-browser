from __future__ import annotations

import pytest

from app.api.errors import InvalidProductUrlException, UnsupportedDomainException
from app.security.ssrf import is_ip_blocked, normalize_and_validate_url


def test_is_ip_blocked():
    # Loopback
    assert is_ip_blocked("127.0.0.1") is True
    assert is_ip_blocked("127.0.0.2") is True
    assert is_ip_blocked("::1") is True

    # Cloud metadata & Link-local
    assert is_ip_blocked("169.254.169.254") is True
    assert is_ip_blocked("169.254.1.1") is True

    # Private networks (RFC 1918)
    assert is_ip_blocked("10.0.0.1") is True
    assert is_ip_blocked("172.16.0.1") is True
    assert is_ip_blocked("172.31.255.255") is True
    assert is_ip_blocked("192.168.1.1") is True

    # Multicast
    assert is_ip_blocked("224.0.0.1") is True

    # Public valid IP
    assert is_ip_blocked("93.184.216.34") is False
    assert is_ip_blocked("8.8.8.8") is False


def test_normalize_and_validate_url_valid():
    allowed = {"ebay.com", "www.ebay.com"}
    url = "https://www.ebay.com/itm/1234567890?param=test#frag"
    normalized = normalize_and_validate_url(url, allowed_domains=allowed, verify_dns=False)
    # Fragment stripped, https preserved
    assert normalized == "https://www.ebay.com/itm/1234567890?param=test"


def test_reject_unsupported_domain():
    allowed = {"ebay.com", "www.ebay.com"}
    with pytest.raises(UnsupportedDomainException):
        normalize_and_validate_url(
            "https://malicious-site.com/steal-data",
            allowed_domains=allowed,
            verify_dns=False,
        )


def test_reject_embedded_credentials():
    allowed = {"ebay.com"}
    with pytest.raises(InvalidProductUrlException) as exc:
        normalize_and_validate_url(
            "https://user:password@www.ebay.com/itm/123",
            allowed_domains=allowed,
            verify_dns=False,
        )
    assert "embedded username or password" in str(exc.value)


def test_reject_non_http_schemes():
    allowed = {"ebay.com"}
    for bad_scheme in [
        "file:///etc/passwd",
        "ftp://ebay.com/file",
        "gopher://ebay.com",
        "data:text/html,alert(1)",
    ]:
        with pytest.raises(InvalidProductUrlException):
            normalize_and_validate_url(bad_scheme, allowed_domains=allowed, verify_dns=False)


def test_reject_oversized_url():
    huge_url = "https://www.ebay.com/" + "a" * 3000
    with pytest.raises(InvalidProductUrlException) as exc:
        normalize_and_validate_url(huge_url, verify_dns=False)
    assert "exceeds maximum allowed length" in str(exc.value)


def test_dev_hosts_allowed_http():
    # Dev hosts like localhost or 127.0.0.1 should be accepted when allow_dev_hosts=True
    url = "http://localhost:8000/test"
    norm = normalize_and_validate_url(url, allow_dev_hosts=True, verify_dns=False)
    assert "http://localhost:8000/test" in norm
