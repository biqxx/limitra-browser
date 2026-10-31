from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from typing import Any

from pydantic import BaseModel, Field


class ErrorCode(str, Enum):
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_PROVIDER = "unsupported_provider"
    UNSUPPORTED_DOMAIN = "unsupported_domain"
    INVALID_PRODUCT_URL = "invalid_product_url"
    PRODUCT_NOT_FOUND = "product_not_found"
    PRODUCT_UNAVAILABLE = "product_unavailable"
    SUPPLIER_TIMEOUT = "supplier_timeout"
    SUPPLIER_RATE_LIMITED = "supplier_rate_limited"
    SUPPLIER_BLOCKED = "supplier_blocked"
    OFFICIAL_API_UNAVAILABLE = "official_api_unavailable"
    PARSING_FAILED = "parsing_failed"
    PARTIAL_RESULT = "partial_result"
    JOB_NOT_FOUND = "job_not_found"
    INTERNAL_ERROR = "internal_error"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"


class ErrorDetail(BaseModel):
    code: ErrorCode
    message: str
    provider: str | None = None
    retryable: bool = False
    request_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorDetail


class FieldProvenance(BaseModel):
    source: str = Field(
        description="e.g. official_api, json_ld, structured_state, css_selector, fallback"
    )
    selector: str | None = Field(
        default=None, description="CSS selector or JSON path expression used"
    )
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0"
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)

