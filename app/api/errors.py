from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_500_INTERNAL_SERVER_ERROR,
    HTTP_502_BAD_GATEWAY,
    HTTP_503_SERVICE_UNAVAILABLE,
    HTTP_504_GATEWAY_TIMEOUT,
)

from app.schemas.common import ErrorCode, ErrorDetail, ErrorResponse


class SupplierServiceException(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        provider: str | None = None,
        status_code: int = HTTP_400_BAD_REQUEST,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.details = details or {}


class InvalidRequestException(SupplierServiceException):
    def __init__(
        self, message: str, provider: str | None = None, details: dict[str, Any] | None = None
    ):
        super().__init__(
            ErrorCode.INVALID_REQUEST,
            message,
            provider,
            HTTP_400_BAD_REQUEST,
            retryable=False,
            details=details,
        )


class UnsupportedProviderException(SupplierServiceException):
    def __init__(self, provider: str):
        super().__init__(
            ErrorCode.UNSUPPORTED_PROVIDER,
            f"Provider '{provider}' is not supported.",
            provider=provider,
            status_code=HTTP_400_BAD_REQUEST,
            retryable=False,
        )


class UnsupportedDomainException(SupplierServiceException):
    def __init__(self, domain: str, provider: str | None = None):
        super().__init__(
            ErrorCode.UNSUPPORTED_DOMAIN,
            f"Domain '{domain}' is not supported or not allowed.",
            provider=provider,
            status_code=HTTP_400_BAD_REQUEST,
            retryable=False,
            details={"domain": domain},
        )


class InvalidProductUrlException(SupplierServiceException):
    def __init__(self, message: str, provider: str | None = None):
        super().__init__(
            ErrorCode.INVALID_PRODUCT_URL,
            message,
            provider=provider,
            status_code=HTTP_400_BAD_REQUEST,
            retryable=False,
        )


class ProductNotFoundException(SupplierServiceException):
    def __init__(self, external_id: str, provider: str):
        super().__init__(
            ErrorCode.PRODUCT_NOT_FOUND,
            f"Product with ID '{external_id}' was not found on supplier '{provider}'.",
            provider=provider,
            status_code=HTTP_404_NOT_FOUND,
            retryable=False,
            details={"external_id": external_id},
        )


class ProductUnavailableException(SupplierServiceException):
    def __init__(self, message: str, provider: str, details: dict[str, Any] | None = None):
        super().__init__(
            ErrorCode.PRODUCT_UNAVAILABLE,
            message,
            provider=provider,
            status_code=HTTP_404_NOT_FOUND,
            retryable=False,
            details=details,
        )


class SupplierTimeoutException(SupplierServiceException):
    def __init__(self, message: str, provider: str):
        super().__init__(
            ErrorCode.SUPPLIER_TIMEOUT,
            message,
            provider=provider,
            status_code=HTTP_504_GATEWAY_TIMEOUT,
            retryable=True,
        )


class SupplierRateLimitedException(SupplierServiceException):
    def __init__(self, message: str, provider: str, retry_after_seconds: int | None = None):
        details = {}
        if retry_after_seconds is not None:
            details["retry_after_seconds"] = retry_after_seconds
        super().__init__(
            ErrorCode.SUPPLIER_RATE_LIMITED,
            message,
            provider=provider,
            status_code=HTTP_429_TOO_MANY_REQUESTS,
            retryable=True,
            details=details,
        )


class SupplierBlockedException(SupplierServiceException):
    """
    Raised when access is denied, CAPTCHA is encountered, or automated access is blocked.
    Compliance requirement:
    Never attempt to bypass CAPTCHA/bot detection.
    Return structured supplier_blocked error and recommend official API.
    """

    def __init__(
        self,
        provider: str,
        reason: str = "Automated access blocked by supplier anti-bot or CAPTCHA.",
    ):
        message = (
            f"The supplier '{provider}' rejected automated browser access ({reason}). "
            f"Please configure and use the supplier's official API credentials."
        )
        super().__init__(
            ErrorCode.SUPPLIER_BLOCKED,
            message,
            provider=provider,
            status_code=HTTP_403_FORBIDDEN,
            retryable=False,
            details={"recommendation": "Use official supplier API"},
        )


class OfficialApiUnavailableException(SupplierServiceException):
    def __init__(self, provider: str, reason: str):
        super().__init__(
            ErrorCode.OFFICIAL_API_UNAVAILABLE,
            f"Official API for supplier '{provider}' is unavailable: {reason}",
            provider=provider,
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            retryable=True,
        )


class SupplierNotConfiguredException(SupplierServiceException):
    """Official API credentials are missing or the integration is disabled."""

    def __init__(self, provider: str, reason: str, env_vars: list[str] | None = None):
        super().__init__(
            ErrorCode.SUPPLIER_NOT_CONFIGURED,
            f"Supplier '{provider}' is not configured: {reason}",
            provider=provider,
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            retryable=False,
            details={"required_env_vars": env_vars or []},
        )


class SupplierAuthenticationException(SupplierServiceException):
    """The supplier rejected OUR credentials (distinct from the internal-secret 401)."""

    def __init__(self, provider: str, reason: str, upstream_status: int | None = None):
        super().__init__(
            ErrorCode.SUPPLIER_AUTH_FAILED,
            f"Supplier '{provider}' rejected the configured API credentials: {reason}",
            provider=provider,
            status_code=HTTP_502_BAD_GATEWAY,
            retryable=False,
            details={"upstream_status": upstream_status},
        )


class SupplierAccessForbiddenException(SupplierServiceException):
    """The supplier refused access (HTTP 403). Never retried via browser scraping."""

    def __init__(self, provider: str, reason: str, upstream_status: int | None = 403):
        super().__init__(
            ErrorCode.SUPPLIER_ACCESS_FORBIDDEN,
            f"Supplier '{provider}' denied access: {reason}",
            provider=provider,
            status_code=HTTP_502_BAD_GATEWAY,
            retryable=False,
            details={"upstream_status": upstream_status},
        )


class ParsingFailedException(SupplierServiceException):
    def __init__(self, message: str, provider: str, details: dict[str, Any] | None = None):
        super().__init__(
            ErrorCode.PARSING_FAILED,
            message,
            provider=provider,
            status_code=HTTP_502_BAD_GATEWAY,
            retryable=False,
            details=details,
        )


class JobNotFoundException(SupplierServiceException):
    def __init__(self, job_id: str):
        super().__init__(
            ErrorCode.JOB_NOT_FOUND,
            f"Job '{job_id}' was not found.",
            status_code=HTTP_404_NOT_FOUND,
            retryable=False,
            details={"job_id": job_id},
        )


class UnauthorizedException(SupplierServiceException):
    def __init__(self, message: str = "Missing or invalid API key."):
        super().__init__(
            ErrorCode.UNAUTHORIZED,
            message,
            status_code=HTTP_401_UNAUTHORIZED,
            retryable=False,
        )


class ForbiddenException(SupplierServiceException):
    def __init__(self, message: str = "Insufficient permissions."):
        super().__init__(
            ErrorCode.FORBIDDEN,
            message,
            status_code=HTTP_403_FORBIDDEN,
            retryable=False,
        )


class RateLimitExceededException(SupplierServiceException):
    def __init__(self, message: str = "Client rate limit exceeded."):
        super().__init__(
            ErrorCode.RATE_LIMIT_EXCEEDED,
            message,
            status_code=HTTP_429_TOO_MANY_REQUESTS,
            retryable=True,
        )


async def supplier_exception_handler(
    request: Request, exc: SupplierServiceException
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    payload = ErrorResponse(
        error=ErrorDetail(
            code=exc.code,
            message=exc.message,
            provider=exc.provider,
            retryable=exc.retryable,
            request_id=request_id,
            details=exc.details,
        )
    )
    return JSONResponse(status_code=exc.status_code, content=payload.model_dump(mode="json"))


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    clean_errors = []
    for err in exc.errors():
        clean_errors.append(
            {
                "loc": err.get("loc"),
                "msg": err.get("msg"),
                "type": err.get("type"),
            }
        )
    payload = ErrorResponse(
        error=ErrorDetail(
            code=ErrorCode.INVALID_REQUEST,
            message="Request validation error.",
            retryable=False,
            request_id=request_id,
            details={"validation_errors": clean_errors},
        )
    )
    return JSONResponse(status_code=HTTP_400_BAD_REQUEST, content=payload.model_dump(mode="json"))


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    # Never expose internal traceback or secrets in production responses
    payload = ErrorResponse(
        error=ErrorDetail(
            code=ErrorCode.INTERNAL_ERROR,
            message="An internal server error occurred.",
            retryable=False,
            request_id=request_id,
            details={},
        )
    )
    return JSONResponse(
        status_code=HTTP_500_INTERNAL_SERVER_ERROR, content=payload.model_dump(mode="json")
    )
