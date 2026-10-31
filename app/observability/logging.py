from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def setup_logging(log_level: str = "INFO") -> None:
    """
    Configures structured JSON logging with security sanitization.
    """
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        sanitize_sensitive_data,
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]

    structlog.configure(
        processors=shared_processors,  # type: ignore
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(log_level.upper())


def redact_sensitive_url_params(url_str: str) -> str:
    """Redacts sensitive query parameters like token, key, secret, signature from URLs."""
    if not isinstance(url_str, str) or ("http://" not in url_str and "https://" not in url_str and "?" not in url_str):
        return url_str
    try:
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        parts = urlsplit(url_str)
        if not parts.query:
            return url_str
        sensitive_params = {
            "token",
            "access_token",
            "refresh_token",
            "secret",
            "api_key",
            "key",
            "password",
            "signature",
            "sig",
            "auth",
            "credential",
        }
        query_pairs = parse_qsl(parts.query, keep_blank_values=True)
        redacted_pairs = []
        for k, v in query_pairs:
            if any(sp in k.lower() for sp in sensitive_params):
                redacted_pairs.append((k, "<redacted>"))
            else:
                redacted_pairs.append((k, v))
        new_query = urlencode(redacted_pairs)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
    except Exception:
        return url_str


def sanitize_sensitive_data(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """
    Ensures sensitive fields like passwords, api keys, tokens, cookies,
    and raw page HTML are never leaked into structured logs.
    Also redacts sensitive query parameters from URLs.
    """
    sensitive_keys = {
        "password",
        "api_key",
        "secret",
        "token",
        "authorization",
        "cookie",
        "cookies",
        "html",
        "raw_html",
        "page_content",
    }
    for k in list(event_dict.keys()):
        val = event_dict[k]
        if any(s in k.lower() for s in sensitive_keys):
            if k in ("html", "raw_html", "page_content"):
                event_dict[k] = "<omitted_html_content>"
            else:
                event_dict[k] = "<redacted>"
        elif isinstance(val, str) and ("http://" in val or "https://" in val or "?" in val):
            event_dict[k] = redact_sensitive_url_params(val)
    return event_dict

