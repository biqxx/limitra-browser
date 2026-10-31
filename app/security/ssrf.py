from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

import idna

from app.api.errors import InvalidProductUrlException, UnsupportedDomainException
from app.config import get_settings

BLOCKED_IP_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local & cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("192.88.99.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),  # Multicast
    ipaddress.ip_network("240.0.0.0/4"),  # Reserved
    ipaddress.ip_network("255.255.255.255/32"),
    # IPv6
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),  # Loopback
    ipaddress.ip_network("fc00::/7"),  # Unique local
    ipaddress.ip_network("fe80::/10"),  # Link local
    ipaddress.ip_network("ff00::/8"),  # Multicast
]


def is_ip_blocked(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        for net in BLOCKED_IP_NETWORKS:
            if ip in net:
                return True
        return False
    except ValueError:
        return True


def normalize_and_validate_url(
    url: str,
    allowed_domains: set[str] | None = None,
    allow_dev_hosts: bool = True,
    verify_dns: bool = True,
) -> str:
    """
    Validates and normalizes supplier URLs with strong SSRF protection:
    - URL length check (<= 2048 chars)
    - Scheme validation (HTTPS only, unless dev host allowed)
    - Rejection of embedded credentials (user:pass@host)
    - IDN normalization
    - Allowed supplier domain matching
    - DNS resolution to block private/internal/cloud metadata IPs
    """
    settings = get_settings()

    if not url or not isinstance(url, str):
        raise InvalidProductUrlException("URL must be a non-empty string.")

    if len(url) > settings.MAX_URL_LENGTH:
        raise InvalidProductUrlException(
            f"URL exceeds maximum allowed length of {settings.MAX_URL_LENGTH} characters."
        )

    try:
        parts = urlsplit(url.strip())
    except Exception as e:
        raise InvalidProductUrlException(f"Malformed URL: {e}") from e

    # Rejection of embedded credentials
    if parts.username or parts.password:
        raise InvalidProductUrlException(
            "URLs containing embedded username or password credentials are forbidden."
        )

    # Rejection of non-http/https schemes
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise InvalidProductUrlException(
            f"Invalid URL scheme '{scheme}'. Only HTTP and HTTPS are permitted."
        )

    hostname = parts.hostname
    if not hostname:
        raise InvalidProductUrlException("URL does not contain a valid hostname.")

    # IDN normalization
    try:
        ascii_hostname = idna.encode(hostname).decode("ascii").lower()
    except Exception as e:
        raise InvalidProductUrlException(
            f"Invalid internationalized domain name: {hostname}"
        ) from e

    dev_hosts = set(settings.allowed_dev_hosts_list)
    is_dev = ascii_hostname in dev_hosts

    # Enforce HTTPS unless allowed dev host
    if settings.ENFORCE_HTTPS_ONLY and not is_dev and scheme != "https":
        raise InvalidProductUrlException(f"Only HTTPS is allowed for external supplier URLs: {url}")

    # Check against allowed domains for the supplier adapter
    if allowed_domains and not is_dev:
        matched = False
        for domain in allowed_domains:
            d = domain.lower()
            if ascii_hostname == d or ascii_hostname.endswith("." + d):
                matched = True
                break
        if not matched:
            raise UnsupportedDomainException(ascii_hostname)

    # DNS check against private/internal IPs (SSRF protection)
    if verify_dns and not is_dev:
        try:
            addr_info = socket.getaddrinfo(ascii_hostname, None)
            for item in addr_info:
                ip_str = str(item[4][0])
                if is_ip_blocked(ip_str):
                    raise InvalidProductUrlException(
                        f"Access to resolved IP '{ip_str}' is forbidden for host '{ascii_hostname}'."
                    )
        except socket.gaierror as e:
            raise InvalidProductUrlException(
                f"DNS resolution failed for hostname '{ascii_hostname}': {e}"
            ) from e

    # Reconstruct normalized URL
    normalized = urlunsplit(
        (
            scheme,
            f"{ascii_hostname}:{parts.port}" if parts.port else ascii_hostname,
            parts.path or "/",
            parts.query,
            "",  # Strip fragment
        )
    )
    return normalized
